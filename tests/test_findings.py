"""Phase 9b: validation findings per runtime-validation requirement (report only)."""
import asyncio
import json
from datetime import UTC, datetime

import pytest
import yaml
from test_capabilities import BASE
from test_operations import pkg  # noqa: F401

from ifs_agent_ops.cli import main as cli
from ifs_agent_ops.mcp_server import server
from ifs_agent_ops.mcp_server.tools import validation as tools
from ifs_agent_ops.operations.capability_store import caps_dir, load_capabilities
from ifs_agent_ops.operations.findings import (
    FindingError,
    findings_path,
    load_findings,
    record_finding,
    requirement_status,
)

REQS = ["Standalone line insert works", "Pricing is established by IFS"]


@pytest.fixture
def cap_pkg(pkg, monkeypatch):  # noqa: F811
    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    write_cap(pkg, REQS)
    return pkg


def write_cap(pkg, reqs, revision=1):  # noqa: F811
    caps_dir(pkg).mkdir(parents=True, exist_ok=True)
    data = dict(BASE, revision=revision, runtime_validation={"status": "untested", "requirements": reqs})
    (caps_dir(pkg) / "create-order.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def cap(pkg):  # noqa: F811
    return load_capabilities(pkg)["create-order"]


def test_findings_are_numbered_saved_in_the_package_and_summarized(cap_pkg):
    at = datetime(2026, 11, 21, 22, 0, tzinfo=UTC)
    f1 = record_finding(cap_pkg, cap(cap_pkg), requirement=1, result="failed", summary="IFS refused the insert",
                        test_log_refs=["20261121-0003"], now=at)
    f2 = record_finding(cap_pkg, cap(cap_pkg), requirement=1, result="passed", summary="Line 1/1 created",
                        test_log_refs=["20261121-0009", " 20261121-0010 "], now=at)
    assert (f1.id, f2.id) == ("F-0001", "F-0002") and f2.test_log_refs == ["20261121-0009", "20261121-0010"]
    assert findings_path(cap_pkg, "create-order") == cap_pkg.root / "validation" / "create-order.findings.yaml"
    saved = load_findings(cap_pkg, "create-order")
    assert [f.id for f in saved.findings] == ["F-0001", "F-0002"]
    assert saved.findings[0].requirement_text == REQS[0] and saved.findings[0].recorded_by == "claude"
    rows = requirement_status(cap(cap_pkg), saved)
    assert [r.status for r in rows] == ["passed", "not tested"]      # the latest finding counts
    assert len(rows[0].history) == 2


def test_bad_findings_are_refused(cap_pkg):
    c = cap(cap_pkg)
    for kwargs, message in (
            ({"requirement": 3, "result": "passed", "test_log_refs": ["20261121-0001"]}, "requirement must be 1..2"),
            ({"requirement": 1, "result": "validated", "test_log_refs": ["20261121-0001"]}, "result must be one of"),
            ({"requirement": 1, "result": "passed", "test_log_refs": ["abc"]}, "look like 20261121-0007"),
            ({"requirement": 1, "result": "failed", "test_log_refs": []}, "needs the test_log_ref")):
        with pytest.raises(FindingError, match=message):
            record_finding(cap_pkg, c, summary="s", **kwargs)
    with pytest.raises(FindingError, match="summary"):
        record_finding(cap_pkg, c, requirement=1, result="inconclusive", summary="  ")
    record_finding(cap_pkg, c, requirement=2, result="inconclusive", summary="TEST was down")  # no refs needed
    assert not load_findings(cap_pkg, "create-order").findings[0].test_log_refs


def test_changed_requirement_wording_makes_earlier_findings_outdated(cap_pkg):
    record_finding(cap_pkg, cap(cap_pkg), requirement=2, result="passed", summary="0.87 from the agreement",
                   test_log_refs=["20261121-0011"])
    write_cap(cap_pkg, [REQS[0], "Pricing and totals are established by IFS"], revision=2)
    [_, r2] = requirement_status(cap(cap_pkg), load_findings(cap_pkg, "create-order"))
    assert r2.status == "not tested" and len(r2.outdated) == 1


def test_findings_never_change_the_runtime_status_or_approval(cap_pkg):
    before = (caps_dir(cap_pkg) / "create-order.yaml").read_text(encoding="utf-8")
    for n in (1, 2):
        record_finding(cap_pkg, cap(cap_pkg), requirement=n, result="passed", summary="ok",
                       test_log_refs=["20261121-0001"])
    assert (caps_dir(cap_pkg) / "create-order.yaml").read_text(encoding="utf-8") == before
    assert cap(cap_pkg).runtime_validation.status == "untested"


# ── MCP tools and CLI ───────────────────────────────────────────────────────

class _Log:
    def __init__(self, refs):
        self.refs = set(refs)

    def get(self, ref):
        return {"ref": ref} if ref in self.refs else None


def test_mcp_tools_record_and_report(cap_pkg):
    out = asyncio.run(tools.get_status("ifs-orders", "create-order"))
    assert "2 not tested" in out and "1. [NOT TESTED] Standalone line insert works" in out
    out = asyncio.run(tools.record("ifs-orders", "create-order", 1, "passed", "Line created without $batch",
                                   ["20261121-0009"], _Log({"20261121-0009"})))
    assert out.startswith("Recorded F-0001: requirement 1 PASSED") and "[PASSED]" in out and "20261121-0009" in out
    # a ref that is not in this machine's test log is refused when a log is available
    out = asyncio.run(tools.record("ifs-orders", "create-order", 2, "passed", "x", ["20261121-0099"],
                                   _Log({"20261121-0009"})))
    assert out.startswith("error: not in this machine's test log: 20261121-0099")
    assert asyncio.run(tools.get_status("ifs-orders", "nope")).startswith("error: no capability 'nope'")


def test_tools_are_registered_and_none_can_approve():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"get_validation_status", "record_validation_finding"} <= names
    assert not any("approve" in n for n in names)


def test_cli_status(cap_pkg, capsys):
    record_finding(cap_pkg, cap(cap_pkg), requirement=2, result="failed", summary="SaleUnitPrice null",
                   test_log_refs=["20261121-0007"])
    assert cli.main(["validation", "status", "create-order", "--package", "ifs-orders"]) == 0
    out = capsys.readouterr().out
    assert "1 failed, 1 not tested" in out and "2. [FAILED] Pricing is established by IFS" in out
    assert "stays 'untested'" in out
    assert cli.main(["validation", "status", "create-order", "--package", "ifs-orders", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["status"] for r in rows] == ["not tested", "failed"] and rows[1]["latest"]["id"] == "F-0001"
