"""The guided builder path: side panel -> TUI -> testing with Claude -> approve -> generate -> deploy -> agent."""
import asyncio

import yaml
from test_capture import demonstrate, store  # noqa: F401
from test_operations import (
    EDMX,
    pkg,  # noqa: F401
)
from test_tui import until
from textual.widgets import Button, OptionList, Static

from ifs_agent_ops.capture import service
from ifs_agent_ops.ifs.metadata import snapshot as snap
from ifs_agent_ops.tui import status as st
from ifs_agent_ops.tui.app import (
    ConfirmDialog,
    MessageDialog,
    PackageScreen,
    ReviewApp,
    ServicesDialog,
)
from ifs_agent_ops.workspace.package import init_package


def stopped(store, **kw):  # noqa: F811
    sid = demonstrate(store)
    service.apply(store, sid, "stop", ts=109.0)
    return sid


def test_an_empty_package_starts_with_planning_in_the_side_panel(tmp_path):
    p = init_package(tmp_path / "po", "ifs-po")
    steps = st.overview(p).next_steps
    assert steps[0].startswith("Plan and record the task: in Edge, open the IFS Agent Ops side panel")


def test_a_stopped_side_panel_recording_is_offered_until_it_is_imported(store, pkg):  # noqa: F811
    sid = stopped(store)
    other = service.create(store, "someone else's task", ["Do it"], workflow="other", package="another-package")["id"]
    service.apply(store, other, "start", ifs_origin="https://ifs-test.local", ts=1.0)
    service.apply(store, other, "stop", ts=2.0)

    assert [s.id for s in st.pending_sessions(pkg)] == [sid]              # not the one meant for another package
    assert st.overview(pkg).next_steps[0] == ("Import 1 stopped recording(s) from the side panel: Recordings tab, "
                                              "press i.")
    service.import_session(store, sid, pkg)
    assert st.pending_sessions(pkg) == []
    assert not any("side panel" in s for s in st.overview(pkg).next_steps)


def test_a_new_package_chooses_its_services_and_pins_their_metadata(store, tmp_path):  # noqa: F811
    p = init_package(tmp_path / "po", "ifs-po")
    service.import_session(store, stopped(store), p)

    [svc] = st.recorded_services(p)
    assert (svc.name, svc.calls, svc.writes, svc.listed, svc.snapshot) == ("OrderHandling", 5, 3, False, False)
    choose = ("Choose the IFS services this package uses and pin their metadata (OrderHandling): Recordings tab, "
              "press m (needs the IFS sign-in).")
    assert choose in st.overview(p).next_steps

    assert st.add_projections(p, ["OrderHandling", "OrderHandling"]) == ["OrderHandling"]
    assert st.add_projections(p, ["OrderHandling"]) == []                 # only adds, never twice
    manifest = yaml.safe_load((p.root / "package.yaml").read_text(encoding="utf-8"))
    assert manifest["projections"] == ["OrderHandling"] and manifest["name"] == "ifs-po"
    assert choose in st.overview(p).next_steps                            # listed, metadata not pinned yet

    snap.write_snapshot(p, "OrderHandling", metadata_xml=EDMX, openapi_json=None, base_url="https://ifs.example",
                        realm="", source="test")
    steps = st.overview(p).next_steps
    assert not any("pin their metadata" in s for s in steps)
    assert "Draft operations from 1 recording(s): Operations tab, press d." in steps


def test_after_approval_the_path_is_generate_deploy_prove_publish():
    d = st.Delivery(generated={"skills": True, "connector": False, "flows": False, "agent": True})
    assert st.delivery_steps("ifs-po", d) == [
        "Generate the connector, flows from the approved capabilities: Generate tab, press g."]

    d.generated = dict.fromkeys(d.generated, True)
    assert "deploy init --package ifs-po" in st.delivery_steps("ifs-po", d)[0]
    d.deploy_file = True
    assert st.delivery_steps("ifs-po", d)[0].startswith("List the generated flows in deploy.yaml")
    d.flows_in_deploy = True
    assert "targets init --package ifs-po --target dev" in st.delivery_steps("ifs-po", d)[0]

    d.targets = [st.TargetState("dev", "Dev environment")]
    assert st.delivery_steps("ifs-po", d)[0].startswith(
        "Deploy to dev (Dev environment): Deploy tab, press s for the plan, then y to deploy.")
    d.targets[0].flows_deployed = True
    d.runtime, d.runtime_cases = {"create-po": "conditional"}, {"create-po": False}
    assert st.delivery_steps("ifs-po", d)[0].startswith("Ask Claude to write validation/create-po.runtime.yaml")
    d.runtime_cases["create-po"] = True
    [prove] = st.delivery_steps("ifs-po", d)
    assert "Deploy tab, press v" in prove and "--allow-writes" in prove   # writes only from the command line

    d.runtime["create-po"] = "validated"
    d.targets[0].agent = "not rendered"
    assert st.delivery_steps("ifs-po", d) == ["Render the Copilot agent for dev: Deploy tab, press n."]
    d.targets[0].agent, d.targets[0].agent_folder = "rendered", "C:/agents/ifs-po/dev"
    assert st.delivery_steps("ifs-po", d) == [
        'Publish the agent to Microsoft 365, in a terminal: cd "C:/agents/ifs-po/dev" ; atk provision --env dev']
    d.targets[0].agent = "provisioned"
    assert st.delivery_steps("ifs-po", d) == []


class FakeRuntime:
    class settings:
        realm = ""

    class client:
        @staticmethod
        async def aclose():
            return None


def test_tui_imports_the_side_panel_recording_and_pins_the_chosen_service(store, tmp_path, monkeypatch):  # noqa: F811
    from ifs_agent_ops.mcp_server import runtime

    p = init_package(tmp_path / "po", "ifs-po")
    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(p.root))
    sid = stopped(store)

    async def fake_fetch(client, package, projection, *, realm, include_openapi=True, source="live"):
        return snap.write_snapshot(package, projection, metadata_xml=EDMX, openapi_json=None,
                                   base_url="https://ifs.example", realm=realm, source="test")

    monkeypatch.setattr(snap, "fetch_snapshot", fake_fetch)
    monkeypatch.setattr(runtime.Runtime, "for_profile", classmethod(lambda cls, name=None, home=None: FakeRuntime))

    async def scenario():
        app = ReviewApp()
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, PackageScreen) and app.screen.view, "the package")
            screen = app.screen
            body = str(screen.query_one("#overview-body", Static).render())
            assert "Import 1 stopped recording(s)" in body and sid in body

            await pilot.press("2")
            await until(pilot, lambda: screen.tab == "recordings", "the Recordings tab")
            await pilot.press("i")
            await until(pilot, lambda: isinstance(app.screen, ConfirmDialog), "the import dialog")
            app.screen.query_one("#yes", Button).press()
            await until(pilot, lambda: isinstance(app.screen, MessageDialog), "the import result")
            await pilot.press("escape")
            await until(pilot, lambda: screen.recs, "the imported recording")
            assert (p.recordings_dir / "create-order" / f"{sid}.har").exists()

            await pilot.press("m")
            await until(pilot, lambda: isinstance(app.screen, ServicesDialog), "the services dialog")
            await pilot.press("enter")                       # the busiest writer is preselected
            await until(pilot, lambda: isinstance(app.screen, MessageDialog), "the services result")
            assert "Metadata pinned: OrderHandling" in str(app.screen.query_one(".dialog-body Static").render())
            await pilot.press("escape")

            await pilot.press("7")
            await until(pilot, lambda: screen.tab == "deploy", "the Deploy tab")
            assert "No deployment target yet" in str(screen.query_one("#deploy-detail", Static).render())

    asyncio.run(scenario())
    assert yaml.safe_load((p.root / "package.yaml").read_text(encoding="utf-8"))["projections"] == ["OrderHandling"]
    assert (snap.snapshot_dir(p, "OrderHandling") / "metadata.xml").exists()
