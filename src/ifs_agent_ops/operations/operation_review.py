"""Technical review of canonical operations, for the human who approves them.

``build_operation_review`` gathers everything a reviewer needs as plain, JSON-serialisable data
(the core result). Every presentation (terminal views in ``cli.review_view``, Markdown in
``operations.review_markdown``, JSON) is rendered from that result and nothing else. Observed examples come
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
        members = ", ".join(t.members[:8]) + (", ..." if len(t.members) > 8 else "")
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




# ── findings, grouping and state ─────────────────────────────────────────────
#
# Every finding has a level. The order below is also the order of attention:
#   blocking  validation error; approval is refused
#   open      an open technical question recorded on the operation (review_notes)
#   check     something the reviewer must consciously confirm (validator warnings, review checks)
#   info      informational validator findings
#   known     classified web-client behaviour, excluded from the canonical contract
LEVELS = ("blocking", "open", "check", "info", "known")
_VALIDATOR_LEVEL = {"error": "blocking", "warning": "check", "info": "info"}

# States, derived only from findings and approval data (never from a judgement):
#   BLOCKED     at least one blocking finding
#   APPROVED    approved, approval current, nothing blocking
#   UNRESOLVED  at least one open question
#   CHECK       reviewer checks to confirm, no open questions
#   READY       only informational or known findings
STATES = ("BLOCKED", "UNRESOLVED", "CHECK", "READY", "APPROVED")

NOT_APPROVING = ["agent exposure", "connector exposure", "every field as agent input", "business authorization",
                 "capability use"]

GROUP_SUMMARY = {
    "numeric-as-string": "{n} decimal field(s) observed using IEEE754Compatible string serialization; callers "
                         "must keep that Content-Type convention or send JSON numbers.",
    "write-datetime-format": "{n} date/time field(s) were sent in a format IFS accepted; keep that format.",
    "datetime-zone": "{n} Edm.DateTimeOffset field(s): confirm how IFS interprets the time zone.",
    "null-unquoted-key": "{n} quoted string key(s) were sent as an unquoted null; callers must emit null "
                         "without quotes.",
    "composite-parameter": "{n} composite IFS structure(s); required members and their sources must be "
                           "proven before a caller can build them.",
    "client-prefer-header": "Prefer wait header sent by the IFS web client (client behaviour, excluded from "
                            "the API contract).",
    "required-not-observed": "{n} required parameter(s) never observed in a recording.",
}


def _finding(source: str, code: str, level: str, message: str, subject: Optional[str] = None) -> dict:
    return {"source": source, "code": code, "level": level, "subject": subject, "message": message}


def _review_checks(op: Operation, model: Optional[EdmxModel], obs: dict) -> list[dict]:
    """Things a reviewer should consciously confirm, plus classified client behaviour."""
    out = []
    for p in op.path_parameters:
        if any(pl.alias for pl in p.placements):
            out.append(_finding("review", "alias-parameter", "check",
                                f"{p.name} is sent as a parameter alias ({p.name}=@{p.name}) with its value as "
                                f"JSON in the query string (@{p.name}=<json>).", p.name))
        if p.type and p.type.edm == "Edm.String" and "null" in obs["path_values"].get(p.name, []):
            out.append(_finding("review", "null-unquoted-key", "check",
                                f"{p.name} is a quoted string key but was sent as an unquoted null; callers must "
                                "emit null without quotes.", p.name))
        if p.type and p.type.enum and not any(pl.quoted for pl in p.placements):
            out.append(_finding("review", "enum-literal-key", "check",
                                f"{p.name} is an enum key; send the fully qualified literal "
                                f"{p.type.enum}'<Member>', not a bare string.", p.name))
        if _structure(model, p.type.edm if p.type else None):
            out.append(_finding("review", "composite-parameter", "check",
                                f"{p.name} is a composite IFS structure; its required members and their sources "
                                "must be proven before a caller can build it.", p.name))
    for b in op.body:
        if _structure(model, b.type.edm if b.type else None):
            out.append(_finding("review", "composite-parameter", "check",
                                f"body {b.name} is a composite IFS structure; its required members and their "
                                "sources must be proven before a caller can build it.", b.name))
        if b.type and b.type.edm == "Edm.DateTimeOffset" and b.observed:
            out.append(_finding("review", "datetime-zone", "check",
                                f"body {b.name} is Edm.DateTimeOffset, observed as {', '.join(b.observed)}; "
                                "confirm how IFS interprets the time zone for this field.", b.name))
    if "prefer" in obs["request_headers"]:
        out.append(_finding("review", "client-prefer-header", "known",
                            "Prefer (" + ", ".join(obs["request_headers"]["prefer"]) + ") was sent by the IFS web "
                            "client; it is client behaviour, not part of the API contract.", "Prefer"))
    if op.method == "POST" and op.kind == "create" and "etag" in obs["response_headers"]:
        out.append(_finding("review", "create-returns-etag", "info",
                            "Create returns an ETag; a later update or bound action on the new record must send it "
                            "as If-Match.", "ETag"))
    return out


def group_findings(findings: list[dict]) -> list[dict]:
    """Collapse equivalent findings (same level and code) without losing their members."""
    groups: dict[tuple[str, str], dict] = {}
    for f in findings:
        g = groups.setdefault((f["level"], f["code"]), {"level": f["level"], "code": f["code"], "count": 0,
                                                        "subjects": [], "messages": []})
        g["count"] += 1
        if f["subject"] and f["subject"] not in g["subjects"]:
            g["subjects"].append(f["subject"])
        g["messages"].append(f["message"])
    out = []
    for g in groups.values():
        n = len(g["subjects"]) or g["count"]
        template = GROUP_SUMMARY.get(g["code"], "{n} x " + g["code"] + ": " + g["messages"][0])
        g["summary"] = g["messages"][0] if g["count"] == 1 else template.format(n=n)
        out.append(g)
    return sorted(out, key=lambda g: (LEVELS.index(g["level"]), g["code"]))


def derive_state(findings: list[dict], status: str, approval_current: bool) -> str:
    levels = {f["level"] for f in findings}
    if "blocking" in levels:
        return "BLOCKED"
    if status == "approved" and approval_current:
        return "APPROVED"
    if "open" in levels:
        return "UNRESOLVED"
    if "check" in levels:
        return "CHECK"
    return "READY"


def _request_usage(op: Operation, ops: dict[str, Operation], capabilities: dict) -> list[dict]:
    """How each capability step that uses the operation supplies its request (from the capability)."""
    from ifs_agent_ops.operations.capability_validate import field_sources

    types = {b.name: (b.type.edm if b.type else None) for b in op.body}
    types.update({p.name: (p.type.edm if p.type else None) for p in op.path_parameters})
    out = []
    for cap in capabilities.values():
        for step in cap.steps:
            if step.operation != op.id:
                continue
            explicit = []
            for target, src in step.bindings.items():
                where, _, name = target.partition(".")
                value = getattr(src, src.kind)
                detail = f"{src.step}.{src.field}" if src.kind == "step" else ("null" if value is None else str(value))
                explicit.append({"target": target, "location": where, "name": name, "source": src.kind,
                                 "detail": detail, "type": types.get(name)})
            counts: dict[str, int] = {}
            kinds: dict[str, str] = {}
            if op.body and step.role == "write":
                for fs in field_sources(cap, step, op, ops):
                    counts[fs.kind] = counts.get(fs.kind, 0) + 1
                    kinds[fs.field] = fs.kind
            out.append({"capability": cap.id, "capability_status": cap.status, "step": step.id, "role": step.role,
                        "for_each": step.for_each, "body_from": step.body_from, "bindings": explicit,
                        "body_field_sources": dict(sorted(counts.items())), "body_field_kinds": kinds})
    return out


def build_operation_review(package: Package, op: Operation, ops: dict[str, Operation],
                           model: Optional[EdmxModel] = None, capabilities: Optional[dict] = None,
                           cache: Optional[dict] = None) -> dict:
    """Structured technical review data for one operation (JSON-serialisable, deterministic)."""
    cache = {} if cache is None else cache
    capabilities = capabilities or {}
    obs = _observations(package, op, cache)
    et = model.entity_types.get(op.entity_type or "") if model else None
    findings = [_finding("validator", f.rule, "open" if f.rule == "open-question" else _VALIDATOR_LEVEL[f.severity],
                         f.message, f.subject) for f in validate_operation(op)]
    findings += _review_checks(op, model, obs)
    approval_current = bool(op.approval and op.approval.revision == op.revision and
                            op.approval.content_sha256 in (None, op.technical_hash()))
    usage = _request_usage(op, ops, capabilities)
    return {
        "id": op.id,
        "name": op.target,
        "kind": op.kind,
        "projection": op.projection,
        "method": op.method,
        "path": projection_path(op.projection) + op.path_template,
        "path_template": op.path_template,
        "entity_type": op.entity_type,
        "recorded_facts": op.description,
        "status": op.status,
        "revision": op.revision,
        "content_sha256": op.technical_hash(),
        "approval": op.approval.model_dump(exclude_none=True) if op.approval else None,
        "approval_current": approval_current,
        "state": derive_state(findings, op.status, approval_current),
        "attention": {level: sum(f["level"] == level for f in findings) for level in LEVELS},
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
        "request_usage": usage,
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
            "shape": op.response.shape, "type": op.response.type, "field_count": len(op.response.fields),
            "keys": et.key if (et and op.response.shape in ("entity", "collection")) else [],
            "returned_identifiers": sorted(k for k in obs["response_headers"] if k in ("location", "odata-entityid")),
            "fields": [{"name": f, "observed": obs["response_values"].get(f, [])} for f in op.response.fields],
        },
        "enums": sorted({f"{n}: {_type_text(t)}" for n, t in
                         [*((p.name, p.type) for p in op.path_parameters if p.type and p.type.enum),
                          *((b.name, b.type) for b in op.body if b.type and b.type.enum),
                          *((k, t) for k, t in op.query.fields.items() if t.enum)]}),
        "evidence": [{"recording": e.recording, "call": f"#{e.call_index}" + (f".{e.part}" if e.part else ""),
                      "status": e.status} for e in op.evidence],
        "observed_statuses": obs["statuses"],
        "depends_on": op.depends_on,
        "feeds": sorted(o.id for o in ops.values() if op.id in o.depends_on),
        "used_by": [{"capability": u["capability"], "capability_status": u["capability_status"], "step": u["step"],
                     "role": u["role"]} for u in usage],
        "open_questions": list(op.review_notes),
        "findings": findings,
        "finding_groups": group_findings(findings),
        "meaning": MEANING,
        "not_approving": NOT_APPROVING,
    }


def build_review_set(reviews: list[dict], capability: Optional[Any] = None) -> dict:
    """The result of one `operations review` run: operations plus what they share."""
    shared: dict[str, dict] = {}
    for r in reviews:
        for f in r["findings"]:
            if f["level"] == "known":
                s = shared.setdefault(f["code"], {"code": f["code"], "summary": GROUP_SUMMARY.get(f["code"], f["message"]),
                                                  "operations": []})
                if r["id"] not in s["operations"]:
                    s["operations"].append(r["id"])
    return {
        "schema": "ifs-agent-ops/operation-review",
        "schema_version": 1,
        "capability": ({"id": capability.id, "title": capability.title, "status": capability.status,
                        "revision": capability.revision} if capability is not None else None),
        "summary": {
            "operations": len(reviews),
            "states": {s: sum(r["state"] == s for r in reviews) for s in STATES},
            "validation_errors": sum(r["attention"]["blocking"] for r in reviews),
            "open_questions": sum(r["attention"]["open"] for r in reviews),
        },
        "shared_known_behaviour": sorted(shared.values(), key=lambda s: s["code"]),
        "operations": reviews,
        "meaning": MEANING,
    }


def to_json(review_set: dict) -> str:
    return json.dumps(review_set, indent=2, ensure_ascii=False)


def review_dir(package: Package) -> Path:
    return package.root / LOCAL_STATE_DIR / "review" / "operations"
