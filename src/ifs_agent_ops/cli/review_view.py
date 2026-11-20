"""Terminal presentation of the structured operation review (human interface).

Renders only what ``operations.operation_review`` produced; it computes no review facts.

Terminal behaviour:
- interactive terminal: Rich tables and panels, colour for state only
- redirected output / CI: no colour, ASCII borders, fixed width (deterministic)
- NO_COLOR is honoured (Rich reads it); JSON output never comes through here
"""
from __future__ import annotations

import os
import sys
from typing import Optional, TextIO

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

STATE_STYLE = {"BLOCKED": "bold red", "UNRESOLVED": "bold yellow", "CHECK": "yellow", "READY": "green",
               "APPROVED": "bold green"}
# Markers carry the state without colour (NO_COLOR, redirected output).
STATE_MARK = {"BLOCKED": "XX", "UNRESOLVED": "!!", "CHECK": "?", "READY": "-", "APPROVED": "ok"}
LEVEL_STYLE = {"blocking": "bold red", "open": "bold yellow", "check": "yellow", "info": "dim", "known": "dim"}
LEVEL_MARK = {"blocking": "x", "open": "!", "check": "?", "info": "-", "known": "-"}
LEVEL_TITLE = {"blocking": "BLOCKING", "open": "OPEN QUESTIONS", "check": "CHECKS TO CONFIRM", "info": "INFORMATION",
               "known": "KNOWN WEB-CLIENT BEHAVIOUR (excluded from the contract)"}
DETAIL_LEVELS = ("summary", "details", "full")


def make_console(file: Optional[TextIO] = None) -> Console:
    stream = file or sys.stdout
    interactive = bool(getattr(stream, "isatty", lambda: False)())
    if not interactive and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(errors="replace")  # never crash on a narrow code page when redirected
        except (ValueError, OSError):
            pass
    return Console(file=stream, highlight=False, emoji=False, soft_wrap=False,
                   width=None if interactive else int(os.environ.get("COLUMNS", "120")),
                   force_terminal=interactive or None, no_color=bool(os.environ.get("NO_COLOR")) or None)


def _box(console: Console):
    utf = (console.encoding or "").lower().startswith("utf")
    return box.ROUNDED if console.is_terminal and utf else box.ASCII


def _state_text(state: str) -> Text:
    return Text(f"{STATE_MARK[state]} {state}", style=STATE_STYLE[state])


def _attention_text(r: dict) -> Text:
    a = r["attention"]
    parts = []
    if a["blocking"]:
        parts.append((f"{a['blocking']} blocking", LEVEL_STYLE["blocking"]))
    if a["open"]:
        parts.append((f"{a['open']} open question{'s' if a['open'] != 1 else ''}", LEVEL_STYLE["open"]))
    if a["check"]:
        parts.append((f"{a['check']} check{'s' if a['check'] != 1 else ''}", LEVEL_STYLE["check"]))
    text = Text()
    for i, (label, style) in enumerate(parts):
        text.append(("  " if i else "") + label, style=style)
    return text if parts else Text("-", style="dim")


def _wrap_name(name: str, width: int = 36) -> str:
    """Break long dotted names at the dots (Rich will not fold inside an unbroken word)."""
    lines, line = [], ""
    for part in name.split("."):
        piece = part if not line else "." + part
        if line and len(line) + len(piece) > width:
            lines.append(line)
            line = "." + part
        else:
            line += piece
    return "\n".join(lines + [line])


def _kv(rows: list[tuple[str, object]]) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold", no_wrap=True)
    t.add_column(overflow="fold")
    for k, v in rows:
        t.add_row(k, v if isinstance(v, Text) else Text(str(v)))
    return t


def _heading(console: Console, text: str) -> None:
    console.print()
    console.print(Text(text, style="bold"))


# ── capability / set overview ────────────────────────────────────────────────

def render_review_set(console: Console, rs: dict, package_name: str, detail: str = "summary") -> None:
    cap = rs["capability"]
    s = rs["summary"]
    title = cap["title"] if cap else "Selected operations"
    sub = f"capability {cap['id']} (revision {cap['revision']}, {cap['status']})" if cap else ""
    head = Text()
    head.append(f"{title}\n", style="bold")
    head.append("Technical operation review" + (f": {sub}" if sub else "") + "\n\n")
    head.append(f"{s['operations']} operation(s)   ")
    for state, n in s["states"].items():
        if n:
            head.append(f"{n} {state}   ", style=STATE_STYLE[state])
    head.append(f"\n{s['validation_errors']} validation error(s)   {s['open_questions']} open question(s)")
    console.print(Panel(head, box=_box(console), expand=False))

    table = Table(box=_box(console), show_lines=False, header_style="bold")
    table.add_column("Operation", overflow="fold", min_width=36)
    table.add_column("Call", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Attention", overflow="fold", min_width=16)
    for r in rs["operations"]:
        risky = r["state"] in ("BLOCKED", "UNRESOLVED")
        table.add_row(Text(_wrap_name(r["name"]), style="bold" if risky else ""), f"{r['method']} {r['kind']}",
                      f"{r['status']} r{r['revision']}", _state_text(r["state"]), _attention_text(r))
    console.print(table)
    console.print(Text("States are derived from findings: BLOCKED validation error | UNRESOLVED open question | "
                       "CHECK reviewer checks | READY information only | APPROVED current approval.", style="dim"))

    order = {"BLOCKED": 0, "UNRESOLVED": 1, "CHECK": 2}
    attention = sorted((r for r in rs["operations"] if r["state"] in order),
                       key=lambda r: (order[r["state"]], -r["attention"]["open"]))
    if attention:
        _heading(console, "REQUIRES ATTENTION")
        for r in attention:
            body = Group(*_finding_lines(r, ("blocking", "open", "check"), show_subjects=detail != "summary"),
                         Text(f"\ndrill down: ifs-agent-ops operations review {r['id']} --package {package_name}",
                              style="dim"))
            console.print(Panel(body, title=Text(f"{r['name']}  {r['state']}", style=STATE_STYLE[r["state"]]),
                                title_align="left", border_style=STATE_STYLE[r["state"]].replace("bold ", ""),
                                box=box.HEAVY if r["state"] in ("BLOCKED", "UNRESOLVED") and _box(console) is not box.ASCII
                                else _box(console)))
    ready = [r["name"] for r in rs["operations"] if r["state"] in ("READY", "APPROVED")]
    if ready:
        _heading(console, "NO OPEN QUESTIONS OR CHECKS")
        console.print(Text(", ".join(ready), style="green"))
    if rs["shared_known_behaviour"]:
        _heading(console, "OBSERVED WEB-CLIENT BEHAVIOUR EXCLUDED FROM CANONICAL CONTRACTS")
        for k in rs["shared_known_behaviour"]:
            console.print(Text(f"  - {k['summary']} ({len(k['operations'])} operations)", style="dim"))
    if detail != "summary":
        for r in rs["operations"]:
            console.print()
            render_operation(console, r, package_name, detail)


# ── one operation ────────────────────────────────────────────────────────────

def _finding_lines(r: dict, levels, show_subjects: bool) -> list[Text]:
    lines: list[Text] = []
    for level in levels:
        if level == "open":
            for q in r["open_questions"]:
                lines.append(Text.assemble((f"{LEVEL_MARK['open']} OPEN  ", LEVEL_STYLE["open"]), q))
            continue
        for g in r["finding_groups"]:
            if g["level"] != level:
                continue
            label = {"blocking": "BLOCKING  ", "check": "check  ", "info": "", "known": ""}[level]
            lines.append(Text.assemble((f"{LEVEL_MARK[level]} {label}", LEVEL_STYLE[level]),
                                       (g["summary"], "dim" if level in ("info", "known") else "")))
            if show_subjects and g["count"] > 1 and g["subjects"]:
                lines.append(Text("    fields: " + ", ".join(g["subjects"]), style="dim"))
    return lines


def render_operation(console: Console, r: dict, package_name: str, detail: str = "summary",
                     footer: bool = True) -> None:
    a = r["attention"]
    header = Text.assemble((r["name"], "bold"), "   ", _state_text(r["state"]))
    validation = Text("PASS", style="green") if not a["blocking"] else Text(f"{a['blocking']} error(s)", style="bold red")
    open_q = Text(str(a["open"]), style=LEVEL_STYLE["open"] if a["open"] else "")
    statuses = ", ".join(map(str, r["observed_statuses"])) or "-"
    approval = r["approval"]
    status = r["status"] + (f" by {approval.get('approved_by')} at {approval.get('approved_at')}" if approval else "")
    if approval and not r["approval_current"]:
        status += "  (approval NOT current)"
    grid = _kv([("Id", r["id"]), ("Status", status), ("Revision", r["revision"]), ("Validation", validation),
                ("Evidence", f"{len(r['evidence'])} recorded call(s), status {statuses}"),
                ("Open questions", open_q),
                ("Content hash", r["content_sha256"] if detail != "summary" else r["content_sha256"][:16] + "..."),
                ("Request", Text(f"{r['method']} {r['path']}", style="bold"))])
    console.print(Panel(grid, title=header, title_align="left", box=_box(console),
                        border_style=STATE_STYLE[r["state"]].replace("bold ", "")))

    _heading(console, "REQUEST")
    _request(console, r, detail)
    _heading(console, "RESPONSE")
    resp = r["response"]
    console.print(Text(f"{resp['shape']} {resp['type'] or ''}, {resp['field_count']} field(s); keys "
                       f"{', '.join(resp['keys']) or '-'}; identifiers in "
                       f"{', '.join(resp['returned_identifiers']) or 'body'}; observed status {statuses}"))
    _heading(console, "EVIDENCE AND DATA FLOW")
    used = ", ".join(f"{u['capability']}:{u['step']} ({u['role']})" for u in r["used_by"]) or "no capability"
    console.print(_kv([("Metadata", r["metadata_source"].get("element")),
                       ("Depends on", ", ".join(r["depends_on"]) if detail != "summary" else f"{len(r['depends_on'])} operation(s)"),
                       ("Feeds", ", ".join(r["feeds"]) if detail != "summary" else f"{len(r['feeds'])} operation(s)"),
                       ("Used by", used)]))
    if r["recorded_facts"]:
        _heading(console, "RECORDED FACTS")
        console.print(Text(r["recorded_facts"]))
    lines = _finding_lines(r, ("blocking", "open", "check", "info", "known"), show_subjects=detail != "summary")
    if lines:
        _heading(console, "FINDINGS")
        for line in lines:
            console.print(line)

    if detail in ("details", "full"):
        _details(console, r, detail)
    if not footer:
        return
    console.print()
    console.print(Text(r["meaning"], style="dim"))
    if detail == "summary":
        console.print(Text(f"more: ifs-agent-ops operations review {r['id']} --package {package_name} "
                           "--details | --full", style="dim"))


def _request(console: Console, r: dict, detail: str) -> None:
    if r["body"]:
        req = sum(1 for b in r["body"] if b["required"])
        console.print(Text(f"{len(r['body'])} canonical body field(s), {req} required by metadata"))
    if r["path_parameters"]:
        console.print(Text(f"{len(r['path_parameters'])} path parameter(s): "
                           + ", ".join(p["name"] for p in r["path_parameters"])))
    if r["query"]["filter_patterns"]:
        console.print(Text(f"observed $filter: " + "; ".join(r["query"]["filter_patterns"])))
    for u in r["request_usage"]:
        counts = ", ".join(f"{n} {k}" for k, n in u["body_field_sources"].items())
        console.print(Text(f"capability {u['capability']} step {u['step']} ({u['role']}"
                           + (f", for each {u['for_each']}" if u["for_each"] else "") + ")"
                           + (f": {counts}" if counts else ""), style="bold"))
        if u["bindings"]:
            t = Table(box=_box(console), header_style="bold", show_edge=True)
            t.add_column("Bound by capability", overflow="fold")
            t.add_column("Type", overflow="fold")
            t.add_column("Source", overflow="fold")
            for b in u["bindings"]:
                t.add_row(b["target"], b["type"] or "-", f"{b['source']} {b['detail']}".strip())
            console.print(t)


def _details(console: Console, r: dict, detail: str) -> None:
    if r["path_parameters"]:
        _heading(console, "PATH PARAMETERS")
        t = Table(box=_box(console), header_style="bold")
        for c in ("Name", "Type", "Placement", "Observed"):
            t.add_column(c, overflow="fold")
        for p in r["path_parameters"]:
            t.add_row(p["name"], p["type"], ", ".join(p["placements"]), ", ".join(p["observed"]))
        console.print(t)
    q = r["query"]
    if any(q[k] for k in ("select", "expand", "filter_patterns", "orderby")) or q["top"] is not None:
        _heading(console, "QUERY OPTIONS (observed)")
        for label, key in (("$select", "select"), ("$expand", "expand"), ("$filter", "filter_patterns"),
                           ("$orderby", "orderby")):
            if q[key]:
                console.print(Text(f"{label}: " + "; ".join(q[key])))
    if r["body"]:
        _heading(console, "REQUEST BODY")
        kinds = {}
        for u in r["request_usage"]:
            kinds.update(u.get("body_field_kinds") or {})
        t = Table(box=_box(console), header_style="bold")
        for c in ("Field", "Type", "Required", "Observed", "Capability source"):
            t.add_column(c, overflow="fold")
        for b in r["body"]:
            t.add_row(b["name"] + ("" if b["in_metadata"] else " (NOT in metadata)"), b["type"],
                      {True: "yes", False: "no", None: "-"}[b["required"]], ", ".join(b["observed"]) or "not observed",
                      kinds.get(b["name"], "-"))
        console.print(t)
    _heading(console, "HEADERS, ETAG AND WARNINGS")
    for h in r["headers"]:
        console.print(Text(f"{h['name']}: {'required' if h['required'] else 'optional'} - {h['reason']}"))
    e, w = r["etag"], r["warnings_header"]
    console.print(Text(f"ETag: keys {', '.join(e['entity_keys']) or '-'}; If-Match "
                       f"{'required' if e['if_match_required'] else 'not required'}"
                       f"{' (sent)' if e['if_match_sent'] else ''}; ETag returned "
                       f"{'yes' if e['etag_returned'] else 'no'}"))
    console.print(Text(f"Warnings: X-IFS-Accept-Warnings {', '.join(w['accept_warnings_sent']) or 'not sent'}; "
                       f"returned {', '.join(w['warning_headers_returned']) or 'none observed'}"))
    if r["enums"]:
        _heading(console, "ENUM-TYPED VALUES")
        for x in r["enums"]:
            console.print(Text(x))
    _heading(console, "EVIDENCE")
    for ev in r["evidence"]:
        console.print(Text(f"{ev['recording']} {ev['call']} -> {ev['status']}"))
    console.print(Text(f"metadata snapshot {r['metadata_source'].get('snapshot')} fetched "
                       f"{r['metadata_source'].get('fetched_at') or '-'}"))
    if detail == "full":
        structures = [(p["name"], p["structure"]) for p in r["path_parameters"] if p["structure"]] + \
                     [(b["name"], b["structure"]) for b in r["body"] if b["structure"]]
        for name, s in structures:
            _heading(console, f"COMPOSITE {name}: {s['type']} ({len(s['members'])} members)")
            t = Table(box=_box(console), header_style="bold")
            for c in ("Member", "Type", "Nullable"):
                t.add_column(c, overflow="fold")
            for m in s["members"]:
                t.add_row(m["name"], m["type"], "yes" if m["nullable"] else "no")
            console.print(t)
        if r["response"]["fields"]:
            _heading(console, f"RESPONSE FIELDS ({r['response']['field_count']})")
            t = Table(box=_box(console), header_style="bold")
            t.add_column("Field", overflow="fold")
            t.add_column("Observed", overflow="fold")
            for f in r["response"]["fields"]:
                t.add_row(f["name"], ", ".join(f["observed"]))
            console.print(t)
        _heading(console, "ALL FINDINGS (ungrouped)")
        for f in r["findings"]:
            console.print(Text(f"{f['level']}: {f['code']}" + (f" [{f['subject']}]" if f["subject"] else "")
                               + f": {f['message']}", style=LEVEL_STYLE[f["level"]]))


# ── approval screen ──────────────────────────────────────────────────────────

def render_approval(console: Console, r: dict, package_name: str, report_path: str) -> None:
    render_operation(console, r, package_name, "summary", footer=False)
    console.print(Text(f"full technical detail: {report_path}", style="dim"))
    console.print()
    approving = Text.assemble(("YOU ARE APPROVING\n", "bold"),
                              f'  "This is a technically correct canonical representation of the IFS operation."\n\n',
                              ("YOU ARE NOT APPROVING\n", "bold"),
                              "\n".join(f"  - {x}" for x in r["not_approving"]))
    console.print(Panel(approving, box=_box(console), expand=False))
    if r["open_questions"] or r["attention"]["check"]:
        n = len(r["open_questions"])
        body = Text()
        if n:
            body.append(f"UNRESOLVED: {n} open technical question{'s' if n != 1 else ''} remain"
                        f"{'s' if n == 1 else ''}\n", style=LEVEL_STYLE["open"])
            for q in r["open_questions"]:
                body.append(f"  ! {q}\n")
            body.append("\nApproving now records technical approval while these questions are open.\n",
                        style=LEVEL_STYLE["open"])
        checks = [g for g in r["finding_groups"] if g["level"] == "check"]
        if checks:
            body.append(f"\n{sum(g['count'] for g in checks)} check(s) to confirm:\n", style=LEVEL_STYLE["check"])
            for g in checks:
                body.append(f"  ? {g['summary']}\n")
        console.print(Panel(body, title=Text(f"{r['state']}", style=STATE_STYLE[r["state"]]), title_align="left",
                            border_style=STATE_STYLE[r["state"]].replace("bold ", ""),
                            box=box.HEAVY if _box(console) is not box.ASCII else box.ASCII))
    else:
        console.print(Text("No open questions or checks are recorded for this operation.", style="green"))
