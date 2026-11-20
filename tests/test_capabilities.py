"""Phase 7b: curated capabilities, their validation, review report and the human approval gate."""
import copy

import pytest
import yaml
from pydantic import ValidationError

from ifs_agent_ops.operations import service
from ifs_agent_ops.operations.capability import Capability, Source
from ifs_agent_ops.operations.capability_review import render_review
from ifs_agent_ops.operations.capability_store import (
    ApprovalRefused, approve_capability, caps_dir, load_capabilities, save_capability,
)
from ifs_agent_ops.operations.capability_validate import field_sources, validate_capability
from ifs_agent_ops.operations.store import approve, load_operations, load_workflows, save_operation
from test_operations import pkg  # noqa: F401  (fixture: synthetic OrderHandling package with recordings)

P = "OrderHandling"
BASE = {
    "id": "create-order", "title": "Create an order",
    "config": {"site": {"type": "string", "value": "S1"}},
    "inputs": [
        {"name": "customer_no", "type": "string", "source": "document", "required": True, "resolve": "find", "confirm": True},
        {"name": "wanted", "type": "date", "source": "user", "required": True},
        {"name": "lines", "type": "list", "source": "document", "required": True,
         "items": [{"name": "line_no", "type": "string", "source": "document", "required": True},
                   {"name": "qty", "type": "decimal", "source": "document", "required": True}]},
    ],
    "questions": [{"id": "ask-date", "asks": "wanted", "when": {"missing": True}}],
    "actions": [
        {"name": "prepare", "steps": ["find"]},
        {"name": "submit", "requires_confirmation": True, "confirm": ["customer_no", "lines", "order_no"],
         "steps": ["find", "defaults", "create-header", "create-line"]},
    ],
    "steps": [
        {"id": "find", "operation": f"{P}.query.OrderSet", "role": "lookup",
         "bindings": {"filter.Objstate": {"fixed": "Planned"}}},
        {"id": "defaults", "operation": f"{P}.function.GetDefaults", "role": "defaults",
         "bindings": {"path.CustomerNo": {"input": "customer_no"}, "path.TaxCode": {"fixed": None}}},
        {"id": "create-header", "operation": f"{P}.create.OrderSet", "role": "write",
         "body_from": ["defaults"],
         "bindings": {"body.CustomerNo": {"input": "customer_no"},
                      "body.WantedDate": {"input": "wanted", "transform": "datetime"}}},
        {"id": "create-line", "operation": f"{P}.create.OrderSet.LinesArray", "role": "write", "for_each": "lines",
         "bindings": {"path.OrderNo": {"step": "create-header", "field": "OrderNo"},
                      "body.OrderNo": {"step": "create-header", "field": "OrderNo"},
                      "body.LineNo": {"item": "line_no"}, "body.Qty": {"item": "qty"}}},
    ],
    "outputs": [{"name": "order_no", "step": "create-header", "field": "OrderNo"}],
}
USED = [f"{P}.query.OrderSet", f"{P}.function.GetDefaults", f"{P}.create.OrderSet", f"{P}.create.OrderSet.LinesArray"]


@pytest.fixture
def drafted(pkg):  # noqa: F811
    service.draft(pkg)
    return pkg


def cap(**changes) -> Capability:
    data = copy.deepcopy(BASE)
    data.update(changes)
    return Capability.model_validate(data)


def rules(c, ops):
    return {(f.where, f.rule, f.severity) for f in validate_capability(c, ops)}


def approve_ops(pkg, ids=USED):  # noqa: F811
    for op_id in ids:
        approve(pkg, op_id, "tester")


def test_source_needs_exactly_one_kind():
    assert Source(fixed=None).kind == "fixed" and Source(fixed=False).kind == "fixed"
    with pytest.raises(ValidationError, match="exactly one"):
        Source(input="a", config="b")
    with pytest.raises(ValidationError, match="exactly one"):
        Source()
    with pytest.raises(ValidationError, match="needs `field`"):
        Source(step="x")


def test_unapproved_operations_block_and_approved_ones_pass(drafted):
    ops = load_operations(drafted)
    assert ("step find", "operation-not-approved", "error") in rules(cap(), ops)
    approve_ops(drafted)
    errors = [f for f in validate_capability(cap(), load_operations(drafted)) if f.severity == "error"]
    assert errors == []


def test_structural_rules(drafted):
    approve_ops(drafted)
    ops = load_operations(drafted)
    bad = copy.deepcopy(BASE)
    s = {x["id"]: x for x in bad["steps"]}
    s["create-header"]["bindings"]["body.Nope"] = {"input": "customer_no"}             # unknown target
    s["create-header"]["bindings"]["body.CustomerNo"] = {"input": "lines.qty"}         # decimal -> Edm.String
    s["create-line"]["bindings"]["body.Qty"] = {"step": "create-line", "field": "Qty"}  # not earlier
    s["defaults"]["body_from"] = ["create-header"]                                     # body_from later step
    s["create-header"]["bindings"].pop("body.WantedDate")
    bad["actions"][1]["requires_confirmation"] = False                                 # write w/o confirmation
    bad["actions"][1]["confirm"] = ["nonsense"]
    bad["questions"] = [{"id": "q", "asks": "nope"}]                                   # unknown input; user input unasked
    bad["config"]["site"]["value"] = None
    found = rules(Capability.model_validate(bad), ops)
    for expected in [
        ("step create-header", "unknown-target", "error"),
        ("step create-header", "type-mismatch", "error"),
        ("step create-line", "bad-step-reference", "error"),
        ("step defaults", "bad-body-from", "error"),
        ("action submit", "write-without-confirmation", "error"),
        ("action submit", "unknown-confirm", "error"),
        ("question q", "unknown-input", "error"),
        ("input wanted", "user-input-without-question", "error"),
        ("config.site", "config-unset", "error"),
        ("input wanted", "unused-input", "warning"),
    ]:
        assert expected in found, expected


def test_write_steps_must_bind_required_and_path_parameters(drafted):
    approve_ops(drafted, USED + [f"{P}.action.ValidateThing"])
    ops = load_operations(drafted)
    c = cap(steps=BASE["steps"] + [{"id": "v", "operation": f"{P}.action.ValidateThing", "role": "write"}],
            actions=BASE["actions"] + [{"name": "check", "requires_confirmation": True, "steps": ["v"]}])
    assert ("step v", "required-unbound", "error") in rules(c, ops)  # ValidateThing.Must is non-nullable
    no_path = copy.deepcopy(BASE)
    no_path["steps"][3]["bindings"].pop("path.OrderNo")
    assert ("step create-line", "path-unbound", "error") in rules(Capability.model_validate(no_path), ops)


def test_field_sources_classify_every_canonical_field(drafted):
    ops = load_operations(drafted)
    c = cap()
    header = field_sources(c, c.step("create-header"), ops[f"{P}.create.OrderSet"], ops)
    kinds = {f.field: f.kind for f in header}
    assert kinds == {"CustomerNo": "input", "WantedDate": "input"}
    line = {f.field: f.kind for f in field_sources(c, c.step("create-line"), ops[f"{P}.create.OrderSet.LinesArray"], ops)}
    assert line == {"OrderNo": "step", "LineNo": "item", "Qty": "item"}
    seeded = cap(steps=[dict(BASE["steps"][0]), dict(BASE["steps"][1]),
                        {**BASE["steps"][2], "bindings": {}, "body_from": ["find"]}, BASE["steps"][3]])
    by = {f.field: f.kind for f in field_sources(seeded, seeded.step("create-header"), ops[f"{P}.create.OrderSet"], ops)}
    assert by["CustomerNo"] == "seeded"  # the lookup's response record supplies CustomerNo


def test_approval_pins_content_and_operation_revisions(drafted):
    save_capability(drafted, cap())
    with pytest.raises(ApprovalRefused, match="validation error"):
        approve_capability(drafted, "create-order", "Pat")        # operations not approved yet
    approve_ops(drafted)
    approved = approve_capability(drafted, "create-order", "Pat")
    assert approved.status == "approved" and approved.approval.approved_by == "Pat"
    assert approved.approval.operations == {i: 1 for i in USED}
    ops = load_operations(drafted)
    assert not [f for f in validate_capability(load_capabilities(drafted)["create-order"], ops) if f.severity == "error"]

    # any content change after approval invalidates it
    path = caps_dir(drafted) / "create-order.yaml"
    data = yaml.safe_load(path.read_text())
    data["inputs"][0]["source"] = "agent"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert ("approval", "changed-since-approval", "error") in rules(load_capabilities(drafted)["create-order"], ops)

    # a referenced operation changing (new revision) also invalidates it
    data["inputs"][0]["source"] = "document"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    op = ops[f"{P}.create.OrderSet"]
    save_operation(drafted, op.model_copy(update={"revision": 2}))
    assert ("approval", "operation-changed-since-approval", "error") in rules(
        load_capabilities(drafted)["create-order"], load_operations(drafted))


def test_cli_approval_requires_a_human(drafted, monkeypatch, capsys):
    from ifs_agent_ops.cli import main as cli

    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(drafted.root))
    approve_ops(drafted)
    save_capability(drafted, cap())
    argv = ["capabilities", "approve", "create-order", "--package", "ifs-orders"]
    assert cli.main(argv) == 1 and "interactive terminal" in capsys.readouterr().err       # automation

    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirmation_code", lambda: "ABC123")

    def eof(prompt):
        raise EOFError
    monkeypatch.setattr(cli, "_ask", eof)                                                   # null stdin
    assert cli.main(argv) == 1 and "interactive input" in capsys.readouterr().err

    answers = iter(["WRONG1"])
    monkeypatch.setattr(cli, "_ask", lambda p: next(answers))
    assert cli.main(argv) == 1 and "code did not match" in capsys.readouterr().err
    assert load_capabilities(drafted)["create-order"].status == "draft"

    answers = iter(["abc123", "create-order", "Pat Reviewer"])
    monkeypatch.setattr(cli, "_ask", lambda p: next(answers))
    assert cli.main(argv) == 0
    approved = load_capabilities(drafted)["create-order"]
    assert approved.status == "approved" and approved.approval.approved_by == "Pat Reviewer"


def test_review_report_is_local_and_complete(drafted, monkeypatch):
    from ifs_agent_ops.cli.main import main

    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(drafted.root))
    save_capability(drafted, cap(review_notes=["Which site?"]))
    assert main(["capabilities", "review", "create-order", "--package", "ifs-orders"]) == 0
    report = (drafted.root / ".ifs-agent-ops" / "review" / "create-order.md").read_text(encoding="utf-8")
    for section in ("## 1. Decisions for the reviewer", "Which site?", "## 3. Actions", "## 4. Agent-facing inputs",
                    "## 7. Steps", "canonical body fields", "## 9. Operations used", "capabilities approve create-order"):
        assert section in report
    text = render_review(cap(), load_operations(drafted), load_workflows(drafted))
    assert "operation-not-approved" in text


def test_no_agent_tool_can_approve():
    import asyncio

    from ifs_agent_ops.mcp_server import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert not any("approve" in n for n in names)


def test_capability_files_must_match_their_id(drafted):
    caps_dir(drafted).mkdir(parents=True, exist_ok=True)
    (caps_dir(drafted) / "other.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
    with pytest.raises(ValueError, match="must match capability id"):
        load_capabilities(drafted)
