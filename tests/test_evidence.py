"""Phase 9b: the evidence report (findings with the logged calls behind them), in the CLI and the TUI."""
import asyncio

import yaml
from test_approval_workflow import ready  # noqa: F401
from test_operations import pkg  # noqa: F401
from test_tui import until
from textual.widgets import OptionList, Tree

from ifs_agent_ops import profiles as prof
from ifs_agent_ops.cli import main as cli
from ifs_agent_ops.operations.capability_store import caps_dir, load_capabilities
from ifs_agent_ops.operations.evidence import build_evidence, render_markdown
from ifs_agent_ops.operations.findings import record_finding
from ifs_agent_ops.testlog import CallLog
from ifs_agent_ops.tui import status as st
from ifs_agent_ops.tui.app import PackageScreen, ReviewApp

EP = "/main/ifsapplications/projection/v1/OrderHandling.svc/OrderSet(OrderNo='C1')"
REQS = ["Order state after submit is Planned", "Pricing is established by IFS", "Warnings are returned"]


def with_evidence(package, tmp_path, monkeypatch):
    """Requirements on the package's capability, a local test log with one call, and findings citing it."""
    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path / "home"))
    path = caps_dir(package) / "create-order.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["runtime_validation"] = {"status": "untested", "requirements": REQS}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    name = prof.resolve_profile(None).name
    log = CallLog.beside_session(prof.session_file(name), name)
    ref = log.record(method="GET", endpoint=EP, request_headers={"Accept": "application/json"}, request_body=None,
                     status=200, ok=True, response_headers={"ETag": 'W/"1"'},
                     response_body={"Objstate": "Planned"}, duration_ms=40)
    cap = load_capabilities(package)["create-order"]
    record_finding(package, cap, requirement=1, result="passed", summary="C1 read back as Planned",
                   test_log_refs=[ref])
    record_finding(package, cap, requirement=2, result="failed", summary="SaleUnitPrice came back empty",
                   test_log_refs=["20990101-0001"])                 # recorded on another machine
    return log, ref


def test_evidence_resolves_cited_calls_from_the_local_log(ready, tmp_path, monkeypatch):  # noqa: F811
    log, ref = with_evidence(ready, tmp_path, monkeypatch)
    ev = build_evidence(ready, load_capabilities(ready)["create-order"], log)
    assert ev.counts_text() == "1 passed, 1 failed, 1 not tested"
    r1, r2, r3 = ev.requirements
    assert r1.calls[ref].found and r1.calls[ref].entry["response_headers"] == {"ETag": 'W/"1"'}
    assert "GET 200  /OrderHandling.svc/OrderSet(OrderNo='C1')" in r1.calls[ref].line()
    assert not r2.calls["20990101-0001"].found and "not in this machine's test log" in r2.calls["20990101-0001"].line()
    assert r3.status == "not tested" and not r3.calls
    md = render_markdown(ev)
    assert "| 1 | Order state after submit is Planned | **passed** | F-0001" in md
    assert "### F-0002: failed" in md and "SaleUnitPrice came back empty" in md and "_No findings yet._" in md
    assert "runtime-validation status stays **untested**" in md


def test_cli_report_is_written_locally(ready, tmp_path, monkeypatch, capsys):  # noqa: F811
    with_evidence(ready, tmp_path, monkeypatch)
    assert cli.main(["validation", "report", "create-order", "--package", "ifs-orders"]) == 0
    out = capsys.readouterr().out
    path = ready.root / ".ifs-agent-ops" / "review" / "create-order.validation.md"
    assert str(path.name) in out and path.exists() and "1 passed, 1 failed, 1 not tested" in out
    assert "not in this machine's test log: 20990101-0001" in out


def test_overview_counts_testing_and_names_failures_first(ready, tmp_path, monkeypatch):  # noqa: F811
    with_evidence(ready, tmp_path, monkeypatch)
    view = st.overview(ready)
    assert view.capabilities[0].testing_text() == "1 passed, 1 failed, 1 not tested"
    assert any("1 requirement(s) FAILED in testing" in s for s in view.next_steps)
    failed = next(i for i, s in enumerate(view.next_steps) if "FAILED in testing" in s)
    approve = next(i for i, s in enumerate(view.next_steps) if s.startswith("When testing is done"))
    assert failed < approve                                           # testing before approval


def test_tui_testing_tab_shows_requirements_findings_and_calls(ready, tmp_path, monkeypatch):  # noqa: F811
    _, ref = with_evidence(ready, tmp_path, monkeypatch)

    async def scenario():
        app = ReviewApp()
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, PackageScreen) and app.screen.caps, "the package")
            screen = app.screen
            await pilot.press("5")
            await until(pilot, lambda: screen.tab == "testing", "the Testing tab")
            tree = screen.query_one("#test-tree", Tree)
            await until(pilot, lambda: len(tree.root.children) == 3, "the requirements")
            labels = [str(n.label) for n in tree.root.children]
            assert "PASSED" in labels[0] and "FAILED" in labels[1] and "NOT TESTED" in labels[2]
            finding = tree.root.children[0].children[0]
            assert "F-0001" in str(finding.label) and "C1 read back as Planned" in str(finding.label)
            call = finding.children[0]
            assert ref in str(call.label)
            call.expand()
            await until(pilot, lambda: len(call.children) > 0, "the call details")
            details = "\n".join(str(n.label) for n in call.children)
            assert "Response headers" in details and '"ETag"' in details and "Planned" in details
            missing = tree.root.children[1].children[0].children[0]
            assert "not in this machine's test log" in str(missing.label) and not missing.allow_expand
    asyncio.run(scenario())
