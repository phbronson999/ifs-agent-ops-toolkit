"""Capability-scoped operation approval and minimal-diff approval persistence."""
import difflib

import pytest
import yaml

from ifs_agent_ops.cli import main as cli
from ifs_agent_ops.operations import service
from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.operations.capability_store import approve_capability, caps_dir, load_capabilities
from ifs_agent_ops.operations.model import RuntimeValidation
from ifs_agent_ops.operations.store import approve, load_operations, ops_dir, save_operation
from ifs_agent_ops.operations.validate import validate_operation
from test_capabilities import BASE, USED
from test_operations import pkg  # noqa: F401

ARGV = ["operations", "approve", "--capability", "create-order", "--package", "ifs-orders"]


@pytest.fixture
def ready(pkg, monkeypatch):  # noqa: F811
    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    service.draft(pkg)
    caps_dir(pkg).mkdir(parents=True, exist_ok=True)
    (caps_dir(pkg) / "create-order.yaml").write_text(yaml.safe_dump(BASE, sort_keys=False), encoding="utf-8")
    # the shared fixture deliberately contains an enum-filter error; correct it so the set is approvable
    query = load_operations(pkg)[USED[0]]
    fixed = query.query.model_copy(update={"filter_patterns": ["Objstate eq IfsApp.OrderHandling.OrderState'{value}'"]})
    changed(pkg, USED[0], query=fixed)
    assert not [f for f in validate_operation(load_operations(pkg)[USED[0]]) if f.severity == "error"]
    return pkg


def human(monkeypatch, answers):
    calls = []
    it = iter(answers)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirmation_code", lambda: "C0DE42")
    monkeypatch.setattr(cli, "_ask", lambda prompt: calls.append(prompt) or next(it))
    return calls


def changed(pkg, op_id, **update):  # noqa: F811
    op = load_operations(pkg)[op_id]
    save_operation(pkg, op.model_copy(update=update))


def test_one_ceremony_approves_the_set_with_individual_records(ready, monkeypatch, capsys):
    changed(ready, USED[2], runtime_validation=RuntimeValidation(requirements=["standalone POST unproven"]))
    before = {i: (o.revision, o.technical_hash()) for i, o in load_operations(ready).items() if i in USED}
    calls = human(monkeypatch, ["c0de42"])
    assert cli.main(ARGV) == 0
    out = capsys.readouterr().out
    assert len(calls) == 1                                   # one ceremony: the one-time code
    assert "YOU ARE APPROVING these 4 operation(s)" in out and "CHECK ITEMS YOU ARE CONFIRMING" in out
    assert all(i in out for i in USED)
    ops = load_operations(ready)
    for op_id in USED:
        a = ops[op_id].approval
        assert ops[op_id].status == "approved" and a.approved_by == "Pat Reviewer" and a.approved_at
        assert (a.revision, a.content_sha256) == before[op_id]  # each operation's own revision and hash
    # runtime requirements did not block and are untouched by approval
    rv = ops[USED[2]].runtime_validation
    assert rv.status == "untested" and rv.requirements == ["standalone POST unproven"]
    # running again: nothing pending, no ceremony
    assert cli.main(ARGV) == 0 and "already have a current technical approval" in capsys.readouterr().out


def test_set_approval_refuses_validation_errors_and_model_questions(ready, monkeypatch, capsys):
    op = load_operations(ready)[USED[2]]
    body = [b.model_copy(update={"in_metadata": False}) if b.name == "CustomerNo" else b for b in op.body]
    changed(ready, USED[2], body=body)                            # unknown-parameter error
    changed(ready, USED[1], review_notes=["does the template need a key?"])
    calls = human(monkeypatch, ["C0DE42"])
    assert cli.main(ARGV) == 1
    err = capsys.readouterr().err
    assert "unknown-parameter" in err and "open technical model question" in err and calls == []
    assert all(load_operations(ready)[i].status == "draft" for i in USED)


def test_set_approval_is_human_only(ready, monkeypatch, capsys):
    assert cli.main(ARGV) == 1 and "interactive terminal" in capsys.readouterr().err   # scripted / piped
    human(monkeypatch, ["WRONG1"])                                                      # wrong code
    assert cli.main(ARGV) == 1 and "did not match" in capsys.readouterr().err

    def eof(prompt):
        raise EOFError
    monkeypatch.setattr(cli, "_ask", eof)
    assert cli.main(ARGV) == 1
    assert all(load_operations(ready)[i].status == "draft" for i in USED)


def test_a_later_change_invalidates_only_that_operation(ready, monkeypatch, capsys):
    human(monkeypatch, ["C0DE42"])
    assert cli.main(ARGV) == 0
    query = load_operations(ready)[USED[0]].query
    changed(ready, USED[0], query=query.model_copy(update={"top": 5}))   # a technical change
    ops = load_operations(ready)
    stale = {i for i in USED if any(f.rule == "changed-since-approval" for f in validate_operation(ops[i]))}
    assert stale == {USED[0]}
    # the changed operation can be re-approved individually (approval bookkeeping is not a blocker)
    capsys.readouterr()
    human(monkeypatch, ["C0DE42", USED[0], "Pat"])
    assert cli.main(["operations", "approve", USED[0], "--package", "ifs-orders"]) == 0
    assert not [f for f in validate_operation(load_operations(ready)[USED[0]]) if f.severity == "error"]


def test_operation_approval_diff_is_small(ready):
    path = ops_dir(ready) / f"{USED[0]}.yaml"
    before = path.read_text(encoding="utf-8")
    approve(ready, USED[0], "Pat")
    after = path.read_text(encoding="utf-8")
    diff = [d for d in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
            if d[:1] in "+-" and d[:3] not in ("+++", "---")]
    assert "-status: draft" in diff and "+status: approved" in diff and "+approval:" in diff
    assert len(diff) <= 8


def test_capability_approval_keeps_formatting_and_hash(ready):
    for op_id in USED:
        approve(ready, op_id, "Pat")
    path = caps_dir(ready) / "create-order.yaml"
    authored = ("# hand-authored capability\n" + yaml.safe_dump(BASE, sort_keys=False, default_flow_style=None)
                + "description: >-\n  folded text that a serializer\n  would rewrite\n")
    path.write_text(authored, encoding="utf-8")
    hash_before = load_capabilities(ready)["create-order"].content_hash()
    approve_capability(ready, "create-order", "Pat")
    after = path.read_text(encoding="utf-8")
    diff = [d for d in difflib.unified_diff(authored.splitlines(), after.splitlines(), lineterm="", n=0)
            if d[:1] in "+-" and d[:3] not in ("+++", "---")]
    assert all(d.startswith("+") for d in diff) and len(diff) <= 12   # only status + approval added
    assert "# hand-authored capability" in after and "folded text that a serializer" in after
    cap = load_capabilities(ready)["create-order"]
    assert cap.status == "approved" and cap.content_hash() == hash_before == cap.approval.content_sha256


def test_no_agent_tool_can_approve():
    import asyncio

    from ifs_agent_ops.mcp_server import server

    assert not any("approve" in t.name for t in asyncio.run(server.mcp.list_tools()))
