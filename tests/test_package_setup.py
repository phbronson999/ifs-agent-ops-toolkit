"""Setting up without the command line: create and register a package, and remove a recording to record it again."""
import asyncio
import os

import pytest
from test_capture import demonstrate, store  # noqa: F401
from test_operations import pkg  # noqa: F401
from test_tui import until
from textual.widgets import Button, Input, OptionList

from ifs_agent_ops import env
from ifs_agent_ops.capture import service as capture
from ifs_agent_ops.capture.session import CaptureError
from ifs_agent_ops.operations import service as ops_service
from ifs_agent_ops.operations.store import approve, load_operations, load_workflows
from ifs_agent_ops.recordings.remove import (
    RemovalRefused,
    discard_session,
    plan_removal,
    remove_recording,
)
from ifs_agent_ops.tui import status as st
from ifs_agent_ops.tui.app import (
    ConfirmDialog,
    NewPackageDialog,
    PackagePicker,
    PackageScreen,
    PickDialog,
    ReviewApp,
)
from ifs_agent_ops.workspace import discovery
from ifs_agent_ops.workspace.package import PackageError, init_package

VAR = discovery.ENV_VAR


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """A toolkit .env that supplied IFS_AGENT_OPS_PACKAGES to this process (as load_env does at startup)."""
    existing = init_package(tmp_path / "packages" / "ifs-orders", "ifs-orders")
    path = tmp_path / "toolkit.env"
    path.write_text(f"# toolkit settings\nIFS_PROFILE=default\n{VAR}={existing.root}\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(env, "REPO_ENV_FILE", path)
    monkeypatch.setattr(env, "_FROM_FILE", {})
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.setattr(discovery.os, "pathsep", ";")
    env.load_env()
    yield path
    os.environ.pop(VAR, None)


def test_a_title_suggests_a_package_name():
    assert discovery.suggest_name("Purchase Requisitions") == "ifs-purchase-requisitions"
    assert discovery.suggest_name("IFS Shop-Floor  Clockings!") == "ifs-shop-floor-clockings"
    assert discovery.suggest_name("  ") == ""


def test_a_running_process_follows_edits_of_env(env_file, tmp_path):
    assert [p.name for p in discovery.discover_packages()[0]] == ["ifs-orders"]
    later = init_package(tmp_path / "elsewhere" / "ifs-later", "ifs-later")
    text = env_file.read_text(encoding="utf-8").replace(f"{VAR}=", f"{VAR}={later.root};")
    env_file.write_text(text, encoding="utf-8")
    assert [p.name for p in discovery.discover_packages()[0]] == ["ifs-later", "ifs-orders"]   # no restart


def test_the_process_environment_wins_over_env(env_file, tmp_path, monkeypatch):
    monkeypatch.setenv(VAR, str(tmp_path / "set-by-the-terminal"))
    assert discovery.configured_paths() == [tmp_path / "set-by-the-terminal"]


def test_create_package_writes_the_skeleton_and_lists_it_in_env(env_file, tmp_path):
    parent = discovery.default_parent()
    assert parent == tmp_path / "packages"
    p, reg = discovery.create_package(tmp_path / "new place" / "ifs-pr", "ifs-pr", display_name="Purchase Requisitions")
    assert (p.root / "package.yaml").exists() and p.display_name == "Purchase Requisitions"
    assert reg.env_file == env_file and not reg.note
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# toolkit settings" and lines[1] == "IFS_PROFILE=default" and lines[3] == "OTHER=1"
    assert lines[2].startswith(f"{VAR}='") and lines[2].endswith("ifs-pr'")   # quoted: the folder has a space
    assert [x.name for x in discovery.discover_packages()[0]] == ["ifs-orders", "ifs-pr"]

    from dotenv import dotenv_values

    assert dotenv_values(env_file)[VAR].split(";")[-1] == str(p.root)   # backslashes survive the quoting

    q, reg = discovery.create_package(parent / "ifs-next", "ifs-next")   # next to a listed package: one more entry
    assert reg.env_file == env_file
    assert discovery.register_package(q.root).env_file is None           # already listed: nothing written


def test_create_package_refuses_a_taken_name_or_a_folder_in_use(env_file, tmp_path):
    with pytest.raises(PackageError, match="already exists at"):
        discovery.create_package(tmp_path / "dup", "ifs-orders")
    used = tmp_path / "used"
    used.mkdir()
    (used / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PackageError, match="not empty"):
        discovery.create_package(used, "ifs-used")
    assert not (used / "package.yaml").exists()


def test_a_variable_from_the_terminal_gets_a_note(env_file, tmp_path, monkeypatch):
    monkeypatch.setenv(VAR, str(tmp_path / "packages" / "ifs-orders"))
    monkeypatch.setattr(env, "_FROM_FILE", {})
    _, reg = discovery.create_package(tmp_path / "x" / "ifs-x", "ifs-x")
    assert "set in this terminal's environment" in reg.note
    assert str(tmp_path / "x" / "ifs-x") in env_file.read_text(encoding="utf-8")


# ── removing a recording to record it again ──────────────────────────────────

def imported(store, pkg):  # noqa: F811
    sid = demonstrate(store)
    capture.apply(store, sid, "stop", ts=109.0)
    result, _ = capture.import_session(store, sid, pkg)
    ops_service.draft(pkg)
    return sid, result.recording.relative_to(pkg.root).as_posix()


def test_removing_a_recording_takes_out_what_was_drafted_only_from_it(store, pkg):  # noqa: F811
    sid, rel = imported(store, pkg)
    before = load_operations(pkg)
    plan = plan_removal(pkg, rel, store)
    assert plan.allowed and plan.sessions == [sid] and plan.workflows
    assert plan.operations_deleted or plan.operations_trimmed

    remove_recording(pkg, rel, store)
    assert not (pkg.root / rel).exists()
    assert not (pkg.root / rel.replace(".har", ".annotations.yaml")).exists()
    after = load_operations(pkg)
    assert set(after) == set(before) - set(plan.operations_deleted)
    assert all(e.recording != rel for op in after.values() for e in op.evidence)
    assert all(wf.recording != rel for wf in load_workflows(pkg).values())
    assert [s.id for s in store.list()] == []                     # the raw capture is gone too
    assert (pkg.root / "recordings" / "create-order" / "full.har").exists()   # other recordings stay


def test_a_recording_approved_work_rests_on_is_not_removed(store, pkg):  # noqa: F811
    _, rel = imported(store, pkg)
    cited = next(op for op in load_operations(pkg).values() if any(e.recording == rel for e in op.evidence))
    approve(pkg, cited.id, "reviewer")
    plan = plan_removal(pkg, rel, store)
    assert f"the approved operation {cited.id} cites it as evidence" in plan.blockers
    with pytest.raises(RemovalRefused, match="cannot be removed"):
        remove_recording(pkg, rel, store)
    assert (pkg.root / rel).exists()


def test_only_package_recordings_can_be_removed(pkg):  # noqa: F811
    with pytest.raises(PackageError, match="not a package recording"):
        plan_removal(pkg, "package.yaml")
    with pytest.raises(PackageError, match="not a package recording"):
        plan_removal(pkg, "recordings/../package.yaml.har")


def test_a_session_still_recording_is_not_discarded(store):  # noqa: F811
    sid = capture.create(store, "Teach requisitions", ["Header"], workflow="create-pr")["id"]
    capture.apply(store, sid, "start", ifs_origin="https://ifs-test.local", ts=1.0)
    with pytest.raises(CaptureError, match="still recording"):
        discard_session(store, sid)
    capture.apply(store, sid, "stop", ts=2.0)
    discard_session(store, sid)
    assert store.list() == []


def test_discardable_sessions_are_unimported_recordings_and_unstarted_plans(store, pkg):  # noqa: F811
    stopped = demonstrate(store)
    capture.apply(store, stopped, "stop", ts=109.0)
    planned = capture.create(store, "Teach requisitions", ["Header"], workflow="create-pr")["id"]
    capture.create(store, "Not this package", ["Header"], workflow="x", package="another-package")
    assert [s.id for s in st.discardable_sessions(pkg)] == [stopped, planned]


# ── in the TUI ───────────────────────────────────────────────────────────────

def test_tui_creates_a_package_and_opens_it(env_file, tmp_path, store):  # noqa: F811
    async def scenario():
        app = ReviewApp()
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await until(pilot, lambda: isinstance(app.screen, NewPackageDialog), "the new package dialog")
            dialog = app.screen
            await pilot.press(*"Purchase Requisitions")
            await until(pilot, lambda: dialog.query_one("#name", Input).value == "ifs-purchase-requisitions",
                        "the suggested name")
            assert dialog.query_one("#folder", Input).value == str(tmp_path / "packages" / "ifs-purchase-requisitions")
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, PackageScreen), "the new package")
            assert app.screen.package.name == "ifs-purchase-requisitions"
            await pilot.press("escape")
            await until(pilot, lambda: isinstance(app.screen, PackagePicker), "the package list")
            options = app.screen.query_one("#packages", OptionList)
            assert [options.get_option_at_index(i).id for i in range(options.option_count)] == [
                "ifs-orders", "ifs-purchase-requisitions"]

    asyncio.run(scenario())
    assert "ifs-purchase-requisitions" in env_file.read_text(encoding="utf-8")


def test_tui_refuses_a_taken_name_inside_the_dialog(env_file):
    async def scenario():
        app = ReviewApp()
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await until(pilot, lambda: isinstance(app.screen, NewPackageDialog), "the new package dialog")
            await pilot.press(*"Orders", "enter")
            await until(pilot, lambda: "already exists" in str(app.screen.query_one("#error").render()), "the error")
            assert isinstance(app.screen, NewPackageDialog)

    asyncio.run(scenario())


def test_tui_removes_a_recording(store, pkg, monkeypatch):  # noqa: F811
    _, rel = imported(store, pkg)
    monkeypatch.setenv(VAR, str(pkg.root))

    async def scenario():
        app = ReviewApp()
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, PackageScreen) and app.screen.view, "the package")
            screen = app.screen
            await pilot.press("2")
            await until(pilot, lambda: screen.tab == "recordings", "the Recordings tab")
            screen.selected["recordings"] = rel
            await pilot.press("delete")
            await until(pilot, lambda: isinstance(app.screen, PickDialog), "the pick dialog")
            pick = app.screen.query_one("#pick", OptionList)
            assert pick.get_option_at_index(0).id == f"rec:{rel}"     # the selected recording comes first
            pick.highlighted = 0
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, ConfirmDialog), "the confirmation")
            app.screen.query_one("#yes", Button).press()
            await until(pilot, lambda: rel not in screen.recs, "the refreshed list")

    asyncio.run(scenario())
    assert not (pkg.root / rel).exists()
