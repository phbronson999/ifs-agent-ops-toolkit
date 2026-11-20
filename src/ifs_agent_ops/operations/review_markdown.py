"""Markdown renderings of the structured operation review (``operation_review.build_review_set``).

Two artifacts, one source:
- a concise review document (decision summary, operations needing attention, short summaries)
- one technical appendix file per operation with the exhaustive field-level detail

Nothing here computes review facts; it only formats the structured result.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ifs_agent_ops.operations.operation_review import LEVELS, review_dir
from ifs_agent_ops.workspace.package import Package

_LEVEL_TITLE = {"blocking": "Blocking (validation errors)", "open": "Open questions", "check": "Checks to confirm",
                "info": "Information", "known": "Known web-client behaviour (excluded from the contract)"}
_STATE_NOTE = {"BLOCKED": "validation errors; approval is refused",
               "UNRESOLVED": "open technical model questions recorded",
               "CHECK": "reviewer checks to confirm",
               "READY": "no open questions or checks",
               "APPROVED": "approved; approval current"}


def _cell(text: Any) -> str:
    return str(text if text not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def _attention(r: dict) -> str:
    a = r["attention"]
    parts = [f"{a['blocking']} blocking" if a["blocking"] else "", f"{a['open']} open" if a["open"] else "",
             f"{a['check']} check(s)" if a["check"] else ""]
    return ", ".join(p for p in parts if p) or "-"


def appendix_name(op_id: str) -> str:
    return f"appendix/{op_id}.md"


# ── concise review document ──────────────────────────────────────────────────

def render_review_document(rs: dict, package_name: str) -> str:
    cap = rs["capability"]
    title = f"capability {cap['id']} (revision {cap['revision']}, {cap['status']})" if cap else "selected operations"
    s = rs["summary"]
    states = ", ".join(f"{n} {st}" for st, n in s["states"].items() if n)
    out = [f"# Technical operation review: {title}", "",
           f"> {rs['meaning']}", "",
           "## Review summary", "",
           f"- {s['operations']} operation(s): {states}",
           f"- {s['validation_errors']} validation error(s); {s['open_questions']} open question(s)", "",
           "| Operation | Method | Status | Rev | State | Attention | Runtime validation |", "|---|---|---|---|---|---|---|"]
    for r in rs["operations"]:
        out.append(f"| [{r['name']}](#{_anchor(r['name'])}) | {r['method']} | {r['status']} | {r['revision']} | "
                   f"**{r['state']}** | {_attention(r)} | {_runtime(r)} |")
    out += ["", "States are derived from findings: BLOCKED (validation error), UNRESOLVED (open technical model "
            "question), CHECK (reviewer checks), READY (information only), APPROVED (current approval). Runtime "
            "validation is a separate status: approval never claims it.", ""]

    attention = [r for r in rs["operations"] if r["state"] in ("BLOCKED", "UNRESOLVED", "CHECK")]
    out += ["## Operations requiring attention", ""]
    if not attention:
        out += ["_None._", ""]
    for r in attention:
        out += [f"### {r['name']}: {r['state']}", ""]
        out += _group_lines(r, levels=("blocking", "open", "check"))
        out.append("")
    if rs["shared_known_behaviour"]:
        out += ["## Observed web-client behaviour excluded from canonical contracts", ""]
        out += [f"- {k['summary']} ({len(k['operations'])} operation(s))" for k in rs["shared_known_behaviour"]]
        out.append("")

    carried = [(r, q) for r in rs["operations"] for q in r["runtime_validation"]["requirements"]]
    out += ["## Runtime validation requirements carried forward (not part of technical approval)", ""]
    out += [f"- `{r['name']}`: {q}" for r, q in carried] or ["_None._"]
    out += ["", "## Operation summaries", ""]
    for r in rs["operations"]:
        out += _summary_md(r, package_name)
    return "\n".join(out)


def _runtime(r: dict) -> str:
    rv = r["runtime_validation"]
    return rv["status"] + (f" ({len(rv['requirements'])})" if rv["requirements"] else "")


def _group_lines(r: dict, levels=LEVELS) -> list[str]:
    lines = []
    for level in levels:
        if level == "open":
            for q in r["open_questions"]:
                lines.append(f"- **Open technical model question:** {q}")
            continue
        if level == "runtime":
            for q in r["runtime_validation"]["requirements"]:
                lines.append(f"- Runtime validation ({r['runtime_validation']['status']}): {q}")
        for g in r["finding_groups"]:
            if g["level"] != level:
                continue
            if level == "runtime" and g["code"] == "runtime-validation":
                continue
            prefix = {"blocking": "**Blocking:** ", "check": "Check: ", "runtime": "Runtime validation: ", "info": "",
                      "known": "Known: "}[level]
            lines.append(f"- {prefix}{g['summary']}")
            if g["count"] > 1 and g["subjects"]:
                lines.append(f"  <details><summary>{len(g['subjects'])} field(s)</summary>{', '.join(g['subjects'])}"
                             "</details>")
    return lines


def _summary_md(r: dict, package_name: str) -> list[str]:
    a = r["attention"]
    hash_short = r["content_sha256"][:12]
    out = [f"### {r['name']}", "",
           f"`{r['id']}`: **{r['state']}** ({_STATE_NOTE[r['state']]})", "",
           f"`{r['method']} {r['path']}`", "",
           "| Status | Revision | Validation | Evidence | Open questions | Content hash |",
           "|---|---|---|---|---|---|",
           f"| {r['status']} | {r['revision']} | {'PASS' if not a['blocking'] else str(a['blocking']) + ' error(s)'} | "
           f"{len(r['evidence'])} call(s), status {', '.join(map(str, r['observed_statuses'])) or '-'} | "
           f"{a['open']} | `{hash_short}` |", ""]
    out += _request_md(r) + _response_md(r)
    out += [f"**Evidence:** {r['metadata_source'].get('element')}; depends on {len(r['depends_on'])}, feeds "
            f"{len(r['feeds'])}; used by " + (", ".join(f"{u['capability']}:{u['step']} ({u['role']})"
                                                       for u in r["used_by"]) or "no capability") + ".", ""]
    if r["recorded_facts"]:
        out += [f"**Recorded facts:** {r['recorded_facts']}", ""]
    groups = _group_lines(r)
    if groups:
        out += ["**Findings**", ""] + groups + [""]
    out += [f"Full technical detail: [{appendix_name(r['id'])}]({appendix_name(r['id'])}) · approve in an interactive "
            f"terminal: `ifs-agent-ops operations approve {r['id']} --package {package_name}`", ""]
    return out


def _request_md(r: dict) -> list[str]:
    out = []
    if r["body"]:
        req = sum(1 for b in r["body"] if b["required"])
        out.append(f"**Request:** {len(r['body'])} canonical body field(s), {req} required by metadata.")
    elif r["path_parameters"] or any(r["query"][k] for k in ("select", "filter_patterns")):
        out.append(f"**Request:** {len(r['path_parameters'])} path parameter(s); "
                   f"{len(r['query']['filter_patterns'])} observed $filter pattern(s).")
    else:
        out.append("**Request:** no parameters.")
    for u in r["request_usage"]:
        counts = ", ".join(f"{n} {k}" for k, n in u["body_field_sources"].items())
        out.append(f"- used by `{u['capability']}` step `{u['step']}` ({u['role']}"
                   + (f", for each {u['for_each']}" if u["for_each"] else "") + ")"
                   + (f": body fields {counts}" if counts else ""))
        if u["bindings"]:
            out += ["", "  | Bound by capability | Type | Source |", "  |---|---|---|"]
            out += [f"  | {b['target']} | {_cell(b['type'])} | {b['source']} {_cell(b['detail'])} |" for b in u["bindings"]]
    return out + [""]


def _response_md(r: dict) -> list[str]:
    resp = r["response"]
    return [f"**Response:** {resp['shape']} {_cell(resp['type'])}, {resp['field_count']} field(s); keys "
            f"{', '.join(resp['keys']) or '-'}; identifiers in {', '.join(resp['returned_identifiers']) or 'body'}.", ""]


def _anchor(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9 -]", "", text.lower()).replace(" ", "-")


# ── exhaustive appendix ──────────────────────────────────────────────────────

def render_appendix(r: dict, package_name: str) -> str:
    out = [f"# Technical appendix: {r['id']}", "", f"> {r['meaning']}", "",
           "| | |", "|---|---|",
           f"| State | **{r['state']}** ({_STATE_NOTE[r['state']]}) |",
           f"| Kind / method | {r['kind']} / `{r['method']}` |",
           f"| Projection | {r['projection']} |",
           f"| Path | `{r['path']}` |",
           f"| Entity type | {_cell(r['entity_type'])} |",
           f"| Status / revision | {r['status']} / {r['revision']} |",
           f"| Runtime validation | {_runtime(r)} (separate from approval) |",
           f"| Approval | {_cell(r['approval'])}{'' if not r['approval'] or r['approval_current'] else ' (NOT current)'} |",
           f"| Content SHA-256 | `{r['content_sha256']}` |",
           f"| Connector operation id | {r['connector_operation_id']} |",
           f"| Metadata | {r['metadata_source'].get('element')}; snapshot `{r['metadata_source'].get('snapshot')}` "
           f"fetched {_cell(r['metadata_source'].get('fetched_at'))}, sha256 "
           f"`{(r['metadata_source'].get('metadata_sha256') or '-')[:16]}` |", ""]
    if r["recorded_facts"]:
        out += ["## Recorded facts", "", r["recorded_facts"], ""]
    out += ["## Findings (every finding, ungrouped)", ""]
    out += [f"- {f['level']}: {f['code']}{' [' + f['subject'] + ']' if f['subject'] else ''}: {f['message']}"
            for f in r["findings"]] or ["_None._"]
    out.append("")
    if r["path_parameters"]:
        out += ["## Path parameters", "", "| Name | Type | Placement | Observed |", "|---|---|---|---|"]
        out += [f"| {p['name']} | {_cell(p['type'])} | {_cell(', '.join(p['placements']))} | "
                f"{_cell(', '.join(p['observed']))} |" for p in r["path_parameters"]]
        out.append("")
        for p in r["path_parameters"]:
            if p["structure"]:
                out += _structure_md(p["name"], p["structure"])
    q = r["query"]
    if any(q[k] for k in ("select", "expand", "filter_patterns", "orderby")) or q["top"] is not None:
        out += ["## Query options (observed)", ""]
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
        out += ["## Request body", "", "| Field | Type | Required | Observed |", "|---|---|---|---|"]
        for b in r["body"]:
            req = {True: "yes", False: "no", None: "-"}[b["required"]]
            out.append(f"| {b['name']}{'' if b['in_metadata'] else ' (NOT in metadata)'} | {_cell(b['type'])} | "
                       f"{req} | {_cell(', '.join(b['observed']) or 'not observed')} |")
        out.append("")
        for b in r["body"]:
            if b["structure"]:
                out += _structure_md(b["name"], b["structure"])
    for u in r["request_usage"]:
        out += [f"## Capability usage: {u['capability']} step {u['step']}", "",
                f"role {u['role']}; for each {u['for_each'] or '-'}; seeded from {', '.join(u['body_from']) or '-'}; "
                "body field sources: " + (", ".join(f"{n} {k}" for k, n in u["body_field_sources"].items()) or "-"), ""]
        if u["bindings"]:
            out += ["| Target | Type | Source | Detail |", "|---|---|---|---|"]
            out += [f"| {b['target']} | {_cell(b['type'])} | {b['source']} | {_cell(b['detail'])} |" for b in u["bindings"]]
            out.append("")
    out += ["## Headers, ETag and warnings", ""]
    out += [f"- {h['name']}: {'required' if h['required'] else 'optional'} - {h['reason']}" for h in r["headers"]]
    e, w = r["etag"], r["warnings_header"]
    out.append(f"- ETag: entity keys {', '.join(e['entity_keys']) or '-'}; If-Match "
               f"{'required' if e['if_match_required'] else 'not required'}{' (sent)' if e['if_match_sent'] else ''}; "
               f"ETag returned: {'yes' if e['etag_returned'] else 'no'}")
    out.append(f"- Warnings: X-IFS-Accept-Warnings sent as {', '.join(w['accept_warnings_sent']) or 'not sent'}; "
               f"warning headers returned: {', '.join(w['warning_headers_returned']) or 'none observed'}")
    out.append("")
    resp = r["response"]
    out += ["## Response", "",
            f"Shape **{resp['shape']}**, type {_cell(resp['type'])}, {resp['field_count']} field(s); keys "
            f"{', '.join(resp['keys']) or '-'}; identifiers in {', '.join(resp['returned_identifiers']) or 'body'}; "
            f"observed statuses {', '.join(map(str, r['observed_statuses'])) or '-'}.", ""]
    if resp["fields"]:
        out += ["| Field | Observed |", "|---|---|"]
        out += [f"| {f['name']} | {_cell(', '.join(f['observed']))} |" for f in resp["fields"]]
        out.append("")
    if r["enums"]:
        out += ["## Enum-typed values", ""] + [f"- {x}" for x in r["enums"]] + [""]
    out += ["## Evidence and data flow", ""]
    out += [f"- {ev['recording']} {ev['call']} -> {ev['status']}" for ev in r["evidence"]]
    out.append(f"- Depends on (observed): {', '.join(r['depends_on']) or '-'}")
    out.append(f"- Feeds (observed): {', '.join(r['feeds']) or '-'}")
    out += ["", f"Approve in an interactive terminal: `ifs-agent-ops operations approve {r['id']} --package "
            f"{package_name}`", ""]
    return "\n".join(out)


def _structure_md(name: str, s: dict) -> list[str]:
    rows = [f"### {name}: composite {s['type']} ({len(s['members'])} members)", "",
            "| Member | Type | Nullable |", "|---|---|---|"]
    rows += [f"| {m['name']} | {m['type']} | {'yes' if m['nullable'] else 'no'} |" for m in s["members"]]
    return rows + [""]


def write_review_artifacts(package: Package, rs: dict, name: str) -> tuple[Path, list[Path]]:
    """Write the concise review document and one appendix file per operation."""
    base = review_dir(package)
    (base / "appendix").mkdir(parents=True, exist_ok=True)
    appendices = []
    for r in rs["operations"]:
        path = base / appendix_name(r["id"])
        path.write_text(render_appendix(r, package.name), encoding="utf-8", newline="\n")
        appendices.append(path)
    doc = base / f"{name}.md"
    doc.write_text(render_review_document(rs, package.name), encoding="utf-8", newline="\n")
    return doc, appendices
