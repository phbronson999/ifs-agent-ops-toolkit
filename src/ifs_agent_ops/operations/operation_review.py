"""Technical review of canonical operations, for the human who approves them.

``build_operation_review`` gathers everything a reviewer needs as plain, JSON-serialisable data
(the core result); ``render_markdown`` / ``render_summary`` only present it. Observed examples come
from the package's *sanitized* recordings and are reduced to value shapes, so no business values
appear in a review.

Technical approval means: "this is a technically correct canonical representation of the IFS
operation". It does not expose the operation to agents, include it in a connector, authorize it
for a capability or approve its business use; capability approval is the separate business gate.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from ifs_agent_ops.analysis.odata import parse_projection_url
from ifs_agent_ops.ifs.metadata.edmx import EdmxModel
from ifs_agent_ops.ifs.metadata.snapshot import snapshot_dir
from ifs_agent_ops.ifs.paths import projection_path
from ifs_agent_ops.operations.draft import parse_path, value_shape
from ifs_agent_ops.operations.model import Operation, TypeRef
from ifs_agent_ops.operations.validate import validate_operation
from ifs_agent_ops.recordings.har import body_json, expand_batches, load_har
from ifs_agent_ops.workspace.package import LOCAL_STATE_DIR, Package

MEANING = ("Technical approval means: this is a technically correct canonical representation of the IFS "
           "operation. It does NOT expose the operation to an agent, expose every field, include it in a "
           "connector, authorize it for a capability, or approve its business use. Capability approval is "
           "the separate business-contract gate.")

_INTERESTING_RESPONSE_HEADERS = re.compile(r"^(etag|location|odata-entityid|x-ifs-.*)$", re.I)
_INTERESTING_REQUEST_HEADERS = re.compile(r"^(if-match|prefer|x-ifs-.*)$", re.I)


def _type_text(t: Optional[TypeRef]) -> str:
    if t is None:
        return "?"
    out = t.edm
    if t.enum:
        members = ", ".join(t.members[:8]) + (", …" if len(t.members) > 8 else "")
        out += f" (enum: {members})"
    return out + ("" if t.nullable else ", non-nullable")


def _structure(model: Optional[EdmxModel], type_name: Optional[str]) -> Optional[dict]:
    """Members of a complex type, for composite parameters and complex responses."""
    if model is None or not type_name:
        return None
    base = type_name[len("Collection("):-1] if type_name.startswith("Collection(") else type_name
    ct = model.complex_types.get(base)
    if ct is None:
        return None
    return {"type": ct.fqn, "members": [{"name": p.name, "type": p.type, "nullable": p.nullable}
                                         for p in ct.properties]}


def _metadata_source(package: Package, op: Operation, model: Optional[EdmxModel]) -> dict:
    snap = snapshot_dir(package, op.projection)
    info: dict[str, Any] = {"snapshot": snap.relative_to(package.root).as_posix()}
    meta_file = snap / "snapshot.yaml"
    if meta_file.exists():
        data = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
        info["fetched_at"] = data.get("fetched_at")
        info["metadata_sha256"] = ((data.get("files") or {}).get("metadata.xml") or {}).get("sha256")
    if model is None:
        info["element"] = "metadata snapshot not loaded"
    elif op.kind in ("action", "function"):
        imp = model.operation_import(op.target)
        info["element"] = (f"{op.kind.capitalize()} {imp.fqn} ({'bound' if imp.is_bound else 'unbound'}"
                           f"{', returns ' + imp.return_type if imp.return_type else ''})" if imp else
                           f"{op.kind} {op.target} (bound operation)")
    else:
        info["element"] = f"EntityType {op.entity_type}" if op.entity_type else "unresolved"
    return info


def _observations(package: Package, op: Operation, cache: dict) -> dict:
    """Shapes, headers and statuses of the recorded calls, read from the sanitized recordings."""
    path_shapes: dict[str, list[str]] = {}
    response_shapes: dict[str, list[str]] = {}
    req_headers: dict[str, list[str]] = {}
    resp_headers: dict[str, list[str]] = {}
    statuses: list[int] = []
    for ev in op.evidence:
        if ev.recording not in cache:
            path = package.root / ev.recording
            cache[ev.recording] = expand_batches(load_har(path)) if path.exists() else []
        call = next((c for c in cache[ev.recording] if c.index == ev.call_index and c.part == ev.part), None)
        if call is None:
            continue
        status, payload = call.payload()
        statuses.append(status)
        target = parse_projection_url(call.url)
        if target:
            for seg in parse_path(target.path):
                for key, value, quoted in seg.keys:
                    if key:
                        enum_lit = None if quoted else re.fullmatch(r"([\w.]+)'([^']*)'", value)
                        shape = ("alias @" if value.startswith("@") and not quoted else
                                 "null" if value == "null" and not quoted else
                                 f"enum literal {enum_lit.group(1)}'<member>'" if enum_lit else value_shape(value))
                        _add(path_shapes, key, shape)
        for k, v in call.request_headers:
            if _INTERESTING_REQUEST_HEADERS.match(k):
                _add(req_headers, k.lower(), "present" if k.lower() == "if-match" else v[:40])
        for k, v in call.response_headers:
            if _INTERESTING_RESPONSE_HEADERS.match(k):
                _add(resp_headers, k.lower(), "present")
        data = body_json(payload)
        records = (data.get("value") if isinstance(data, dict) and isinstance(data.get("value"), list)
                   else [data] if isinstance(data, dict) else [])
        for rec in records[:20]:
            if isinstance(rec, dict):
                for k, v in rec.items():
                    if "@odata." not in k:
                        _add(response_shapes, k, value_shape(v))
    return {"statuses": sorted(set(statuses)), "path_values": path_shapes, "request_headers": req_headers,
            "response_headers": resp_headers, "response_values": response_shapes}


def _add(target: dict, key: str, value: str) -> None:
    target.setdefault(key, [])
    if value not in target[key] and len(target[key]) < 6:
        target[key].append(value)


def _technical_flags(op: Operation, model: Optional[EdmxModel], obs: dict) -> list[str]:
    """Things a reviewer should consciously check; not errors."""
    flags = []
    for p in op.path_parameters:
        if any(pl.alias for pl in p.placements):
            flags.append(f"{p.name} is sent as a parameter alias ({p.name}=@{p.name}) with its value as JSON in "
                         f"the query string (@{p.name}=<json>).")
        if p.type and p.type.edm == "Edm.String" and "null" in obs["path_values"].get(p.name, []):
            flags.append(f"{p.name} is a quoted string key but was sent as an unquoted null; callers must emit "
                         "`null` without quotes.")
        if p.type and p.type.enum and not any(pl.quoted for pl in p.placements):
            flags.append(f"{p.name} is an enum key; send the fully qualified literal {p.type.enum}'<Member>', "
                         "not a bare string.")
        if _structure(model, p.type.edm if p.type else None):
            flags.append(f"{p.name} is a composite IFS structure; its required members and their sources must "
                         "be proven before a caller can build it.")
    for b in op.body:
        if _structure(model, b.type.edm if b.type else None):
            flags.append(f"body {b.name} is a composite IFS structure; its required members and their sources "
                         "must be proven before a caller can build it.")
        if b.type and b.type.edm == "Edm.DateTimeOffset" and b.observed:
            flags.append(f"body {b.name} is Edm.DateTimeOffset, observed as {', '.join(b.observed)}; confirm "
                         "how IFS interprets the time zone for this field.")
    if "prefer" in obs["request_headers"]:
        flags.append("Prefer (" + ", ".join(obs["request_headers"]["prefer"]) + ") was sent by the IFS web "
                     "client; it is client behaviour, not part of the API contract.")
    if op.method == "POST" and op.kind == "create" and "etag" in obs["response_headers"]:
        flags.append("Create returns an ETag; a later update or bound action on the new record must send it "
                     "as If-Match.")
    return flags


def build_operation_review(package: Package, op: Operation, ops: dict[str, Operation],
                           model: Optional[EdmxModel] = None, capabilities: Optional[dict] = None,
                           cache: Optional[dict] = None) -> dict:
    """Structured technical review data for one operation (JSON-serialisable)."""
    cache = {} if cache is None else cache
    obs = _observations(package, op, cache)
    et = model.entity_types.get(op.entity_type or "") if model else None
    rtype = op.response.type
    used_by = []
    for cap in (capabilities or {}).values():
        for step in cap.steps:
            if step.operation == op.id:
                used_by.append({"capability": cap.id, "capability_status": cap.status, "step": step.id,
                                "role": step.role})
    findings = [{"rule": f.rule, "severity": f.severity, "message": f.message} for f in validate_operation(op)]
    return {
        "id": op.id,
        "kind": op.kind,
        "projection": op.projection,
        "method": op.method,
        "path": projection_path(op.projection) + op.path_template,
        "path_template": op.path_template,
        "entity_type": op.entity_type,
        "description": op.description,
        "status": op.status,
        "revision": op.revision,
        "content_sha256": op.technical_hash(),
        "approval": op.approval.model_dump(exclude_none=True) if op.approval else None,
        "approval_current": bool(op.approval and op.approval.revision == op.revision and
                                 op.approval.content_sha256 in (None, op.technical_hash())),
        "connector_operation_id": op.connector_operation_id,
        "metadata_source": _metadata_source(package, op, model),
        "path_parameters": [{
            "name": p.name, "type": _type_text(p.type),
            "placements": [f"{pl.segment}.{pl.key}" + (" (alias)" if pl.alias else " (quoted)" if pl.quoted else "")
                           for pl in p.placements],
            "observed": obs["path_values"].get(p.name, []),
            "structure": _structure(model, p.type.edm if p.type else None),
        } for p in op.path_parameters],
        "query": {"select": op.query.select, "expand": op.query.expand, "filter_patterns": op.query.filter_patterns,
                  "orderby": [o for o in op.query.orderby if o], "top": op.query.top,
                  "fields": {k: _type_text(t) for k, t in op.query.fields.items()}},
        "body": [{
            "name": b.name, "type": _type_text(b.type), "required": b.required, "in_metadata": b.in_metadata,
            "observed": b.observed, "structure": _structure(model, b.type.edm if b.type else None),
        } for b in op.body],
        "headers": [{"name": h.name, "required": h.required, "reason": h.reason} for h in op.headers],
        "observed_request_headers": obs["request_headers"],
        "observed_response_headers": obs["response_headers"],
        "etag": {
            "entity_keys": et.key if et else [],
            "if_match_required": any(h.name == "If-Match" and h.required for h in op.headers),
            "if_match_sent": "if-match" in obs["request_headers"],
            "etag_returned": "etag" in obs["response_headers"],
        },
        "warnings_header": {
            "accept_warnings_sent": obs["request_headers"].get("x-ifs-accept-warnings", []),
            "warning_headers_returned": sorted(k for k in obs["response_headers"] if k.startswith("x-ifs-")),
        },
        "response": {
            "shape": op.response.shape, "type": rtype, "field_count": len(op.response.fields),
            "keys": et.key if (et and op.response.shape in ("entity", "collection")) else [],
            "returned_identifiers": sorted(k for k in obs["response_headers"] if k in ("location", "odata-entityid")),
            "fields": [{"name": f, "observed": obs["response_values"].get(f, [])} for f in op.response.fields],
        },
        "enums": sorted({f"{x['name']}: {x['type']}" for x in
                         [*({"name": p.name, "type": _type_text(p.type)} for p in op.path_parameters if p.type and p.type.enum),
                          *({"name": b.name, "type": _type_text(b.type)} for b in op.body if b.type and b.type.enum),
                          *({"name": k, "type": _type_text(t)} for k, t in op.query.fields.items() if t.enum)]}),
        "evidence": [{"recording": e.recording, "call": f"#{e.call_index}" + (f".{e.part}" if e.part else ""),
                      "status": e.status} for e in op.evidence],
        "observed_statuses": obs["statuses"],
        "depends_on": op.depends_on,
        "feeds": sorted(o.id for o in ops.values() if op.id in o.depends_on),
        "used_by": used_by,
        "validation": findings,
        "technical_flags": _technical_flags(op, model, obs),
        "review_notes": op.review_notes,
        "meaning": MEANING,
    }


# ── presentation ─────────────────────────────────────────────────────────────

def _cell(text: Any) -> str:
    return str(text if text not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def _approve_line(r: dict) -> str:
    return f"ifs-agent-ops operations approve {r['id']} --package <package>"


def render_markdown(r: dict, package_name: str = "<package>") -> str:
    out = [f"## {r['id']}", ""]
    errors = sum(f["severity"] == "error" for f in r["validation"])
    approval = r["approval"]
    status = r["status"] + (f" by {approval.get('approved_by')} at {approval.get('approved_at')}" if approval else "")
    out += [
        "| | |", "|---|---|",
        f"| Kind / method | {r['kind']} / `{r['method']}` |",
        f"| Projection | {r['projection']} |",
        f"| Path | `{r['path']}` |",
        f"| Entity type | {_cell(r['entity_type'])} |",
        f"| Status | {_cell(status)}{'' if not approval or r['approval_current'] else ' — **approval no longer current**'} |",
        f"| Revision | {r['revision']} |",
        f"| Content SHA-256 | `{r['content_sha256'][:16]}…` |",
        f"| Metadata | {r['metadata_source'].get('element')}; snapshot `{r['metadata_source'].get('snapshot')}`"
        f" fetched {_cell(r['metadata_source'].get('fetched_at'))} |",
        f"| Validation | {errors} error(s), {sum(f['severity'] == 'warning' for f in r['validation'])} warning(s) |",
        "",
    ]
    if r["description"]:
        out += [r["description"], ""]
    if r["review_notes"] or r["technical_flags"]:
        out += ["### Questions for the reviewer", ""]
        out += [f"- **Open:** {n}" for n in r["review_notes"]]
        out += [f"- Check: {n}" for n in r["technical_flags"]]
        out.append("")
    if r["path_parameters"]:
        out += ["### Path parameters", "", "| Name | Type | Placement | Observed |", "|---|---|---|---|"]
        for p in r["path_parameters"]:
            out.append(f"| {p['name']} | {_cell(p['type'])} | {_cell(', '.join(p['placements']))} | "
                       f"{_cell(', '.join(p['observed']))} |")
        out.append("")
        for p in r["path_parameters"]:
            if p["structure"]:
                out += _structure_md(p["name"], p["structure"])
    q = r["query"]
    if any(q[k] for k in ("select", "expand", "filter_patterns", "orderby")) or q["top"] is not None:
        out += ["### Query options (observed)", ""]
        for label, key in (("$select", "select"), ("$expand", "expand"), ("$filter", "filter_patterns"),
                           ("$orderby", "orderby")):
            if q[key]:
                out.append(f"- {label}: " + "; ".join(f"`{x}`" for x in q[key]))
        if q["top"] is not None:
            out.append(f"- $top: {q['top']}")
        if q["fields"]:
            out.append("- field types: " + "; ".join(f"{k} {v}" for k, v in q["fields"].items()))
        out.append("")
    if r["body"]:
        out += ["### Request body", "", "| Field | Type | Required | Observed |", "|---|---|---|---|"]
        for b in r["body"]:
            req = {True: "yes", False: "no", None: "—"}[b["required"]]
            out.append(f"| {b['name']}{'' if b['in_metadata'] else ' ⚠ not in metadata'} | {_cell(b['type'])} | "
                       f"{req} | {_cell(', '.join(b['observed']) or 'not observed')} |")
        out.append("")
        for b in r["body"]:
            if b["structure"]:
                out += _structure_md(b["name"], b["structure"])
    out += ["### Headers, ETag and warnings", ""]
    for h in r["headers"]:
        out.append(f"- {h['name']}: {'required' if h['required'] else 'optional'} — {h['reason']}")
    e, w = r["etag"], r["warnings_header"]
    out.append(f"- ETag: entity keys {', '.join(e['entity_keys']) or '—'}; If-Match "
               f"{'required' if e['if_match_required'] else 'not required'}"
               f"{' (sent)' if e['if_match_sent'] else ''}; ETag returned: {'yes' if e['etag_returned'] else 'no'}")
    out.append(f"- Warnings: X-IFS-Accept-Warnings sent as {', '.join(w['accept_warnings_sent']) or 'not sent'}; "
               f"warning headers returned: {', '.join(w['warning_headers_returned']) or 'none observed'}")
    out.append("")
    resp = r["response"]
    out += ["### Response", "",
            f"Shape **{resp['shape']}**, type {_cell(resp['type'])}, {resp['field_count']} field(s); "
            f"keys {', '.join(resp['keys']) or '—'}; identifiers returned in "
            f"{', '.join(resp['returned_identifiers']) or 'body only'}. Observed statuses: "
            f"{', '.join(map(str, r['observed_statuses'])) or '—'}.", ""]
    if resp["fields"]:
        out += ["<details><summary>Response fields and observed shapes</summary>", "",
                "| Field | Observed |", "|---|---|"]
        out += [f"| {f['name']} | {_cell(', '.join(f['observed']))} |" for f in resp["fields"]]
        out += ["", "</details>", ""]
    if r["enums"]:
        out += ["### Enum-typed values", ""] + [f"- {x}" for x in r["enums"]] + [""]
    out += ["### Evidence and data flow", ""]
    out += [f"- {ev['recording']} {ev['call']} → {ev['status']}" for ev in r["evidence"]]
    out.append(f"- Depends on (observed): {', '.join(r['depends_on']) or '—'}")
    out.append(f"- Feeds (observed): {', '.join(r['feeds']) or '—'}")
    out.append("- Used by capabilities: " + (", ".join(f"{u['capability']} step `{u['step']}` ({u['role']})"
                                                       for u in r["used_by"]) or "—"))
    out.append("")
    if r["validation"]:
        out += ["### Validation findings", ""]
        out += [f"- {f['severity']}: {f['rule']}: {f['message']}" for f in r["validation"]]
        out.append("")
    out += ["### Approve", "", f"> {r['meaning']}", "",
            f"In an interactive terminal: `{_approve_line(r).replace('<package>', package_name)}`", ""]
    return "\n".join(out)


def _structure_md(name: str, s: dict) -> list[str]:
    rows = [f"<details><summary>{name}: composite {s['type']} ({len(s['members'])} members)</summary>", "",
            "| Member | Type | Nullable |", "|---|---|---|"]
    rows += [f"| {m['name']} | {m['type']} | {'yes' if m['nullable'] else 'no'} |" for m in s["members"]]
    return rows + ["", "</details>", ""]


def render_summary(r: dict) -> str:
    """Short terminal view shown by `operations approve` before the human gate."""
    errors = [f for f in r["validation"] if f["severity"] == "error"]
    lines = [f"{r['id']}  (revision {r['revision']}, {r['status']})",
             f"  {r['method']} {r['path']}",
             f"  content sha256 {r['content_sha256']}",
             f"  metadata: {r['metadata_source'].get('element')}",
             f"  path parameters: {len(r['path_parameters'])}, body fields: {len(r['body'])}, "
             f"response: {r['response']['shape']} {r['response']['type'] or ''} ({r['response']['field_count']} fields)",
             f"  evidence: {len(r['evidence'])} recorded call(s); used by: "
             + (", ".join(f"{u['capability']}:{u['step']}" for u in r["used_by"]) or "no capability"),
             f"  validation: {len(errors)} error(s)"]
    if r["review_notes"]:
        lines.append(f"  OPEN QUESTIONS ({len(r['review_notes'])}):")
        lines += [f"    - {n}" for n in r["review_notes"]]
    if r["technical_flags"]:
        lines.append("  check:")
        lines += [f"    - {n}" for n in r["technical_flags"]]
    return "\n".join(lines)


def render_document(reviews: list[dict], title: str, package_name: str) -> str:
    out = [f"# Technical operation review: {title}", "", f"> {MEANING}", "",
           "| Operation | Kind | Status | Rev | Errors | Open questions | Checks |", "|---|---|---|---|---|---|---|"]
    for r in reviews:
        out.append(f"| [{r['id']}](#{_anchor(r['id'])}) | {r['kind']} | {r['status']} | {r['revision']} | "
                   f"{sum(f['severity'] == 'error' for f in r['validation'])} | {len(r['review_notes'])} | "
                   f"{len(r['technical_flags'])} |")
    out.append("")
    for r in reviews:
        out += [render_markdown(r, package_name), "---", ""]
    return "\n".join(out)


def _anchor(text: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", text.lower()).replace(" ", "-")


def review_path(package: Package, name: str) -> Path:
    return package.root / LOCAL_STATE_DIR / "review" / "operations" / f"{name}.md"


def write_document(package: Package, reviews: list[dict], name: str, title: str) -> Path:
    path = review_path(package, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_document(reviews, title, package.name), encoding="utf-8", newline="\n")
    return path


def to_json(reviews: list[dict]) -> str:
    return json.dumps(reviews, indent=2, sort_keys=False)
