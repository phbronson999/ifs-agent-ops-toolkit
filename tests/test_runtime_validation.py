"""Approval and runtime validation are separate states."""
import copy
import io

from ifs_agent_ops.cli import review_view as view
from ifs_agent_ops.operations import service
from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.operations.capability_review import render_review
from ifs_agent_ops.operations.capability_store import approve_capability, load_capabilities, save_capability
from ifs_agent_ops.operations.capability_validate import validate_capability
from ifs_agent_ops.operations.model import RuntimeValidation
from ifs_agent_ops.operations.store import approve, load_operations, save_operation
from ifs_agent_ops.operations.validate import validate_operation
from test_capabilities import BASE, USED
from test_operations import pkg  # noqa: F401

OP = "OrderHandling.create.OrderSet"


def _with_runtime(pkg, reqs, status="untested"):  # noqa: F811
    service.draft(pkg)
    op = load_operations(pkg)[OP]
    save_operation(pkg, op.model_copy(update={"runtime_validation": RuntimeValidation(status=status, requirements=reqs)}))
    return load_operations(pkg)[OP]


def test_runtime_requirements_do_not_make_an_operation_unresolved(pkg):  # noqa: F811
    _with_runtime(pkg, ["standalone POST not executed"])
    rs, _ = service.operation_reviews(pkg, [OP])
    r = rs["operations"][0]
    assert r["state"] in ("READY", "CHECK") and r["attention"]["open"] == 0
    assert r["runtime_validation"] == {"status": "untested", "requirements": ["standalone POST not executed"]}
    assert any(f["level"] == "runtime" for f in r["findings"])
    out = io.StringIO()
    view.render_operation(view.make_console(out), r, "ifs-orders")
    text = out.getvalue()
    assert "RUNTIME VALIDATION (untested; not part of technical approval)" in text
    assert "> standalone POST not executed" in text and "UNRESOLVED" not in text
    out = io.StringIO()
    view.render_approval(view.make_console(out), r, "ifs-orders", "appendix.md")
    assert "Runtime validation stays untested after this approval" in out.getvalue()
    assert any("runtime validation" in x for x in r["not_approving"])


def test_runtime_status_never_touches_approval(pkg):  # noqa: F811
    _with_runtime(pkg, ["x"])
    approved = approve(pkg, OP, "Pat")
    validated = approved.model_copy(update={"runtime_validation": RuntimeValidation(status="validated",
                                                                                   requirements=["x", "y"])})
    assert validated.technical_hash() == approved.technical_hash()
    assert not [f for f in validate_operation(validated) if f.rule == "changed-since-approval"]
    assert validated.status == "approved" and validated.runtime_validation.status == "validated"
    service.draft(pkg)  # re-draft keeps runtime_validation
    save_operation(pkg, validated)
    service.draft(pkg)
    assert load_operations(pkg)[OP].runtime_validation.status == "validated"


def test_capability_runtime_status_is_outside_the_approval_hash(pkg):  # noqa: F811
    service.draft(pkg)
    for op_id in USED:
        approve(pkg, op_id, "t")
    data = copy.deepcopy(BASE)
    data["runtime_validation"] = {"status": "untested", "requirements": ["standalone line insert"]}
    save_capability(pkg, Capability.model_validate(data))
    approved = approve_capability(pkg, "create-order", "Pat")
    assert approved.runtime_validation.status == "untested"  # approval does not claim runtime validation
    later = approved.model_copy(update={"runtime_validation": RuntimeValidation(status="failed",
                                                                               requirements=["standalone line insert"])})
    assert later.content_hash() == approved.content_hash()
    assert not [f for f in validate_capability(later, load_operations(pkg)) if f.severity == "error"]
    changed = approved.model_copy(update={"runtime_validation": RuntimeValidation(requirements=["other"])})
    assert changed.content_hash() != approved.content_hash()  # the requirements themselves are reviewed content
    assert load_capabilities(pkg)["create-order"].status == "approved"


def test_capability_review_separates_categories(pkg):  # noqa: F811
    service.draft(pkg)
    data = copy.deepcopy(BASE)
    data.update(review_notes=["Which site?"], deferred=["Charges"],
                excluded_calls=[{"call": "CreateChangeRequest", "classification": "validate-at-runtime",
                                 "reason": "bracketed the insert"}],
                runtime_validation={"requirements": ["standalone line insert"]})
    cap = Capability.model_validate(data)
    text = render_review(cap, load_operations(pkg), {})
    for expected in ("Curation decisions for the reviewer", "Which site?", "Deliberately out of scope", "Charges",
                     "Observed web-client calls not included", "validate-at-runtime",
                     "Runtime validation (separate from approval): **untested**", "standalone line insert",
                     "Neither approval claims runtime validation"):
        assert expected in text, expected
    rules = {(f.rule, f.severity) for f in validate_capability(cap, load_operations(pkg))}
    assert ("runtime-validation", "info") in rules and ("open-question", "info") in rules


def test_excluded_call_cannot_also_be_a_step(pkg):  # noqa: F811
    service.draft(pkg)
    data = copy.deepcopy(BASE)
    data["excluded_calls"] = [{"call": "GetDefaults", "classification": "excluded", "reason": "x"}]
    rules = {f.rule for f in validate_capability(Capability.model_validate(data), load_operations(pkg))}
    assert "excluded-but-used" in rules
