"""Presentation boundaries of the technical review: state, grouping, rendering, JSON, approval."""
import io
import json

import pytest

from ifs_agent_ops.cli import review_view as view
from ifs_agent_ops.operations import service
from ifs_agent_ops.operations.operation_review import derive_state, group_findings
from ifs_agent_ops.operations.store import load_operations, ops_dir, save_operation
from test_operations import pkg  # noqa: F401

OP = "OrderHandling.create.OrderSet"


def f(level, code="x", subject=None):
    return {"source": "test", "code": code, "level": level, "subject": subject, "message": f"{code} {subject}"}


@pytest.mark.parametrize("findings,status,current,expected", [
    ([], "draft", False, "READY"),
    ([f("info"), f("known")], "draft", False, "READY"),
    ([f("check")], "draft", False, "CHECK"),
    ([f("check"), f("open")], "draft", False, "UNRESOLVED"),
    ([f("open"), f("blocking")], "draft", False, "BLOCKED"),
    ([f("open")], "approved", True, "APPROVED"),
    ([f("blocking")], "approved", True, "BLOCKED"),        # e.g. changed since approval
    ([f("open")], "approved", False, "UNRESOLVED"),       # approval not current
])
def test_state_is_derived_from_findings_only(findings, status, current, expected):
    assert derive_state(findings, status, current) == expected
    assert derive_state(list(reversed(findings)), status, current) == expected  # order-independent


def test_equivalent_findings_are_grouped_without_losing_members():
    findings = [f("info", "numeric-as-string", n) for n in ("Qty", "Price", "Cost")] + [f("open", "open-question")]
    groups = group_findings(findings)
    num = next(g for g in groups if g["code"] == "numeric-as-string")
    assert num["count"] == 3 and num["subjects"] == ["Qty", "Price", "Cost"] and len(num["messages"]) == 3
    assert num["summary"].startswith("3 decimal field(s)")
    assert [g["level"] for g in groups] == ["open", "info"]  # attention order


def _review_set(pkg, notes=None):  # noqa: F811
    service.draft(pkg)
    if notes is not None:
        save_operation(pkg, load_operations(pkg)[OP].model_copy(update={"review_notes": notes}))
    rs, _ = service.operation_reviews(pkg, [OP, "OrderHandling.query.OrderSet"])
    return rs


def _render(fn, *args) -> str:
    console = view.make_console(io.StringIO())
    fn(console, *args)
    return console.file.getvalue()


def test_open_questions_stay_visible_and_never_look_clean(pkg):  # noqa: F811
    rs = _review_set(pkg, ["standalone POST unproven"])
    r = next(x for x in rs["operations"] if x["id"] == OP)
    assert r["state"] == "UNRESOLVED" and r["attention"]["open"] == 1
    overview = _render(view.render_review_set, rs, "ifs-orders")
    row = next(line for line in overview.splitlines() if line.startswith("| OrderSet ") or "| OrderSet" in line[:12])
    assert "UNRESOLVED" in row and "READY" not in row and "1 open question" in row
    assert "REQUIRES ATTENTION" in overview and "standalone POST unproven" in overview
    single = _render(view.render_operation, r, "ifs-orders")
    assert "!! UNRESOLVED" in single and "! OPEN  standalone POST unproven" in single
    approval = _render(view.render_approval, r, "ifs-orders", "appendix.md")
    assert "UNRESOLVED: 1 open technical model question" in approval and "standalone POST unproven" in approval
    assert "YOU ARE APPROVING" in approval and "YOU ARE NOT APPROVING" in approval
    assert "No open questions or checks" not in approval


def test_redirected_rendering_is_deterministic_and_plain(pkg, monkeypatch):  # noqa: F811
    monkeypatch.delenv("COLUMNS", raising=False)
    rs = _review_set(pkg, ["q"])
    a = _render(view.render_review_set, rs, "ifs-orders", "details")
    b = _render(view.render_review_set, rs, "ifs-orders", "details")
    assert a == b and "\x1b[" not in a
    assert max(len(line) for line in a.splitlines()) <= 120
    assert all(ord(ch) < 128 for ch in a)  # ASCII borders when redirected


def test_json_contains_every_finding(pkg):  # noqa: F811
    from ifs_agent_ops.operations.operation_review import to_json
    from ifs_agent_ops.operations.validate import validate_operation

    rs = _review_set(pkg, ["q1", "q2"])
    data = json.loads(to_json(rs))
    op = next(x for x in data["operations"] if x["id"] == OP)
    validator = validate_operation(load_operations(pkg)[OP])
    assert len([x for x in op["findings"] if x["source"] == "validator"]) == len(validator)
    assert sum(g["count"] for g in op["finding_groups"]) == len(op["findings"])
    assert op["open_questions"] == ["q1", "q2"]


def test_presentation_never_changes_definitions(pkg, monkeypatch, capsys):  # noqa: F811
    from ifs_agent_ops.cli.main import main

    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    service.draft(pkg)
    before = {p.name: p.read_bytes() for p in ops_dir(pkg).glob("*.yaml")}
    hashes = {i: (o.technical_hash(), o.revision) for i, o in load_operations(pkg).items()}
    for argv in (["operations", "review", OP, "--package", "ifs-orders", "--full"],
                 ["operations", "review", OP, "--package", "ifs-orders", "--output", "json"],
                 ["operations", "approve", OP, "--package", "ifs-orders"]):   # refused: not interactive
        main(argv)
    capsys.readouterr()
    assert {p.name: p.read_bytes() for p in ops_dir(pkg).glob("*.yaml")} == before
    assert {i: (o.technical_hash(), o.revision) for i, o in load_operations(pkg).items()} == hashes
    assert load_operations(pkg)[OP].status == "draft"


def test_approval_gate_unchanged_and_shows_review_first(pkg, monkeypatch, capsys):  # noqa: F811
    from ifs_agent_ops.cli import main as cli

    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    service.draft(pkg)
    argv = ["operations", "approve", OP, "--package", "ifs-orders"]
    assert cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert "YOU ARE NOT APPROVING" in out and "interactive terminal" in err
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirmation_code", lambda: "ABC123")
    answers = iter(["WRONG1"])
    monkeypatch.setattr(cli, "_ask", lambda p: next(answers))
    assert cli.main(argv) == 1 and "code did not match" in capsys.readouterr().err
    assert load_operations(pkg)[OP].status == "draft"
    answers = iter(["abc123"])
    monkeypatch.setattr(cli, "_ask", lambda p: next(answers))
    assert cli.main(argv) == 0
    approved = load_operations(pkg)[OP]
    assert approved.status == "approved" and approved.approval.approved_by == "Pat Reviewer"
    assert approved.approval.content_sha256 == approved.technical_hash()


def test_no_agent_tool_can_approve_or_review_write():
    import asyncio

    from ifs_agent_ops.mcp_server import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert not any("approve" in n for n in names)
