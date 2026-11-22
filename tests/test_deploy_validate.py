"""Deployed-path validation and the runtime-validation status engine (fake environment, no pac)."""
import json

import pytest
from test_connector import pinned  # noqa: F401  (pytest fixture chain: pinned -> approved -> pkg)
from test_deploy_powerplatform import FakeEnvironment, FakePac, setup  # noqa: F401
from test_operations import pkg  # noqa: F401
from test_skills import approved  # noqa: F401

from ifs_agent_ops.deploy import powerplatform as ppa
from ifs_agent_ops.deploy import validate as dv
from ifs_agent_ops.operations.capability_store import load_capabilities
from ifs_agent_ops.operations.findings import load_findings, record_finding


def _sarif(*results):
    return {"runs": [{"results": [{"ruleId": rule, "message": {"text": text}, "properties": {"severity": sev}}
                                  for sev, rule, text in results]}]}


class CheckingPac(FakePac):
    def __init__(self, env, report=None, export_error=None):
        super().__init__(env)
        self.report = report or _sarif()
        self.export_error = export_error
        self.checked = 0

    def export_solution(self, name, path):
        if self.export_error:
            raise ppa.PacError(self.export_error)
        path.write_bytes(b"zip")
        return path

    def check_solution(self, zip_path, out_dir):
        self.checked += 1
        return self.report


@pytest.fixture
def deployed(setup):  # noqa: F811
    package, intent, target, env, pac = setup
    ppa.apply(intent, package, target, pac)
    env.references["ti_IfsOrderHandling"]["connectionid"] = "conn-1"      # a person attached the connection
    return package, intent, target, env


def _pass_all(package):
    cap = load_capabilities(package)["create-order"]
    for n in range(1, len(cap.runtime_validation.requirements) + 1):
        record_finding(package, cap, requirement=n, result="passed", summary="worked",
                       test_log_refs=["20261121-0001"])


def test_green_deployed_path_and_passed_findings_validate_the_capability(deployed):
    package, intent, target, env = deployed
    _pass_all(package)
    cap = load_capabilities(package)["create-order"]
    approved_hash = cap.approval.content_sha256
    pac = CheckingPac(env, _sarif(("Medium", "connector-validate-swagger",
                                   "HostNameNotValid host The 'host' property ...")))
    run = dv.validate(intent, package, target, pac)

    assert run.deployed_ok and pac.checked == 1
    statuses = {c.name: c.status for c in run.checks}
    assert statuses["deployed state"] == statuses["manual steps"] == statuses["solution export"] == dv.PASS
    assert statuses["solution checker"] == dv.WARN
    assert "expected: the host is @environmentVariables" in next(
        c.detail for c in run.checks if c.name == "checker connector-validate-swagger")
    assert statuses["calls through the deployed path"] == dv.NOT_RUN

    [result] = run.capabilities
    assert (result.previous, result.status, result.recorded) == ("untested", "validated", True)
    cap = load_capabilities(package)["create-order"]
    assert cap.runtime_validation.status == "validated"
    ev = cap.runtime_validation.evaluation
    assert (ev.target, ev.run) == ("dev", run.path.name)
    assert "1 of 1 requirements passed" in ev.basis
    # validation never touches the approval
    assert cap.status == "approved" and cap.content_hash() == approved_hash == cap.approval.content_sha256
    data = json.loads(run.path.read_text(encoding="utf-8"))
    assert data["deployed_ok"] and run.path.parent == package.root / ".ifs-agent-ops" / "runs"


def test_missing_connection_or_blocking_checker_issue_keeps_it_conditional(deployed):
    package, intent, target, env = deployed
    _pass_all(package)
    env.references["ti_IfsOrderHandling"]["connectionid"] = ""
    run = dv.validate(intent, package, target, CheckingPac(env))
    assert not run.deployed_ok and run.capabilities[0].status == "conditional"
    assert "did not pass" in run.capabilities[0].basis

    env.references["ti_IfsOrderHandling"]["connectionid"] = "conn-1"
    run = dv.validate(intent, package, target, CheckingPac(env, _sarif(("High", "rule-x", "bad"))))
    assert not run.deployed_ok and any(c.name == "checker rule-x" and c.status == dv.FAIL for c in run.checks)


def test_export_failure_fails_the_platform_layer(deployed):
    package, intent, target, env = deployed
    pac = CheckingPac(env, export_error="Exporting connection reference ... requires the custom connector")
    run = dv.validate(intent, package, target, pac)
    assert not run.deployed_ok and pac.checked == 0
    assert next(c for c in run.checks if c.name == "solution export").status == dv.FAIL


def test_failed_finding_fails_and_untested_stays_untested(deployed):
    package, intent, target, env = deployed
    run = dv.validate(intent, package, target, CheckingPac(env), record=False)
    assert run.capabilities[0].status == "untested" and not run.capabilities[0].recorded
    assert load_capabilities(package)["create-order"].runtime_validation.status == "untested"

    cap = load_capabilities(package)["create-order"]
    record_finding(package, cap, requirement=1, result="failed", summary="IFS refused the line",
                   test_log_refs=["20261121-0002"])
    run = dv.validate(intent, package, target, CheckingPac(env), solution_check=False)
    assert run.capabilities[0].status == "failed" and "F-0001" in run.capabilities[0].basis
    assert next(c for c in run.checks if c.name == "solution checker").status == dv.NOT_RUN
    assert not run.deployed_ok            # a skipped platform check never counts as green


def test_status_is_written_without_reformatting_the_capability(deployed):
    package, intent, target, env = deployed
    _pass_all(package)
    path = package.root / "capabilities" / "create-order.yaml"
    folded = "description: >-\n  Create an order\n  from a document."
    text = path.read_text(encoding="utf-8").replace("description: Create an order from a document.", folded)
    path.write_text(text, encoding="utf-8")       # hand formatting a full rewrite would lose
    dv.validate(intent, package, target, CheckingPac(env))
    after = path.read_text(encoding="utf-8")
    assert folded in after
    changed = [ln for ln in after.splitlines() if ln not in text.splitlines()]
    assert changed[0] == "  status: validated" and all(ln.startswith("  ") for ln in changed)
    assert load_capabilities(package)["create-order"].runtime_validation.evaluation.target == "dev"
    dv.validate(intent, package, target, CheckingPac(env))          # a second run replaces the evaluation
    assert path.read_text(encoding="utf-8").count("evaluation:") == 1


def test_reworded_requirement_needs_new_findings(deployed):
    package, _, _, _ = deployed
    _pass_all(package)
    from ifs_agent_ops.operations.capability_store import save_capability

    cap = load_capabilities(package)["create-order"]
    cap.runtime_validation.requirements[0] = "standalone line insert, reworded"
    save_capability(package, cap)
    status, basis = dv.decide(load_capabilities(package)["create-order"], load_findings(package, "create-order"),
                              True, "dev")
    assert status == "untested" and basis == "no requirement has passed yet"
