"""Guided review TUI: package overview, next steps, and approvals through the human confirmation dialog."""
import asyncio

from test_approval_workflow import ready  # noqa: F401
from test_capabilities import USED
from test_operations import pkg  # noqa: F401
from textual.command import CommandPalette
from textual.widgets import Input, OptionList, Static

from ifs_agent_ops.cli import main as cli
from ifs_agent_ops.operations.capability_store import load_capabilities
from ifs_agent_ops.operations.store import load_operations
from ifs_agent_ops.tui import status as st
from ifs_agent_ops.tui.app import (
    ApprovalDialog,
    OperationDetail,
    PackageScreen,
    ReviewApp,
)


def test_without_a_terminal_no_arguments_still_print_help_and_tui_refuses(capsys):
    assert cli.main([]) == 0 and "usage: ifs-agent-ops" in capsys.readouterr().out
    assert cli.main(["tui"]) == 2 and "interactive terminal" in capsys.readouterr().err


def test_overview_puts_testing_with_claude_before_any_approval(ready):  # noqa: F811
    import yaml

    from ifs_agent_ops.operations.capability_store import caps_dir

    view = st.overview(ready)
    assert not view.git and [r.state for r in view.recordings] == ["drafted"]   # not a git repository
    assert not view.recordings[0].in_capability
    cap = view.capabilities[0]
    assert cap.id == "create-order" and not cap.approval_current
    assert sorted(cap.unapproved_operations) == sorted(USED)
    approve = (f"When testing is done: approve the {len(USED)} operation(s) create-order uses without a current "
               "approval (Capabilities tab, press o).")
    assert view.next_steps == [("Work 1 recording(s) into a capability with Claude and test the flow against "
                                "IFS TEST. Approve only after that."), approve]

    path = caps_dir(ready) / "create-order.yaml"                      # the capability now uses the recording
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["evidence"] = ["workflows/create-order.full.yaml"]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    view = st.overview(ready)
    assert view.recordings[0].in_capability and view.next_steps == [approve]


def test_drift_is_found_in_memory_and_nothing_is_written(ready):  # noqa: F811
    from test_approval_workflow import changed

    from ifs_agent_ops.operations.store import approve, ops_dir

    assert st.drifted_operations(ready) == {}                    # nothing approved yet
    approve(ready, USED[1], "Pat Reviewer")
    assert st.drifted_operations(ready) == {}                    # approved and still what the recordings say
    changed(ready, USED[1], depends_on=["somewhere.else"])       # the approved definition and the recordings differ
    before = {p.name: p.read_bytes() for p in ops_dir(ready).glob("*.yaml")}
    assert st.drifted_operations(ready) == {USED[1]: ["depends_on"]}
    assert {p.name: p.read_bytes() for p in ops_dir(ready).glob("*.yaml")} == before   # read-only
    assert any("DRIFTED" in s for s in st.overview(ready).next_steps)


async def until(pilot, condition, what: str) -> None:
    for _ in range(600):  # up to 30 s: generous for a busy machine
        await pilot.pause(0.05)
        if condition():
            return
    raise AssertionError(f"timed out waiting for {what}")


def test_tui_approves_operations_then_the_capability_through_the_dialog(ready):  # noqa: F811
    async def scenario():
        app = ReviewApp()
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")                                   # pick the package
            await until(pilot, lambda: isinstance(app.screen, PackageScreen) and app.screen.caps, "the package")
            screen = app.screen
            await pilot.press("4")                                       # Capabilities tab
            await until(pilot, lambda: screen.tab == "capabilities", "the tab")

            await pilot.press("o")                                       # approve the operations it uses
            await until(pilot, lambda: isinstance(app.screen, ApprovalDialog), "the approval dialog")
            await pilot.press("escape")                                  # cancel: nothing approved
            await until(pilot, lambda: not isinstance(app.screen, ApprovalDialog), "the dialog to close")
            assert all(load_operations(ready)[o].status != "approved" for o in USED)

            await pilot.press("o")
            await until(pilot, lambda: isinstance(app.screen, ApprovalDialog), "the approval dialog again")
            await pilot.press("enter")                                   # Approve has focus: Enter approves
            await until(pilot, lambda: not screen.caps["create-order"].unapproved_operations, "the refresh")
            ops = load_operations(ready)
            assert all(ops[o].status == "approved" and ops[o].approval.approved_by == "Pat Reviewer" for o in USED)

            await pilot.press("A")                                       # Caps Lock on: A approves too
            await until(pilot, lambda: isinstance(app.screen, ApprovalDialog), "the capability dialog")
            await pilot.press("enter")
            await until(pilot, lambda: screen.caps["create-order"].approval_current, "the capability approval")
            assert load_capabilities(ready)["create-order"].approval.approved_by == "Pat Reviewer"

    asyncio.run(scenario())


def test_recording_tree_starts_collapsed_and_builds_call_details_on_expand(ready):  # noqa: F811
    from textual.widgets import Tree

    async def scenario():
        app = ReviewApp()
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, PackageScreen) and app.screen.recs, "the package")
            screen = app.screen
            await pilot.press("2")
            tree = screen.query_one("#rec-tree", Tree)
            await until(pilot, lambda: len(tree.root.children) > 1, "the recording tree")
            steps = tree.root.children
            assert not any(n.is_expanded for n in steps)                 # everything starts collapsed
            assert "Data flow" in str(steps[-1].label)
            calls = steps[0].children
            batches = [c for c in calls if "[$batch]" in str(c.label)]
            assert batches and all(str(p.label).startswith(f"#{p.parent.data['view'].index}.")
                                   for b in batches for p in b.children)  # $batch parts nest under their batch
            assert all(not c.children for c in calls if c not in batches)  # details are not built yet
            call = next(c for c in calls if c.allow_expand)
            call.expand()
            await until(pilot, lambda: bool(call.children), "the call details")
            assert any(str(line.label).startswith(("Body keys", "Headers", "Filter", "Select", "receives"))
                       for line in call.children)
            await pilot.press("p")                                       # p opens the command palette
            await until(pilot, lambda: isinstance(app.screen, CommandPalette), "the palette")
            await pilot.press("p", "q")                                  # ... whose search box takes p and q
            assert isinstance(app.screen, CommandPalette) and app.screen.query_one(Input).value == "pq"
            await pilot.press("escape")
            await until(pilot, lambda: app.screen is screen, "the palette to close")
            await pilot.press("q")                                       # q quits from any screen
            await until(pilot, lambda: not app.is_running, "quit")

    asyncio.run(scenario())


def test_operation_details_open_full_screen_and_step_through_the_list(ready):  # noqa: F811
    async def scenario():
        app = ReviewApp()
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, PackageScreen) and app.screen.reviews, "the reviews")
            screen = app.screen
            await pilot.press("3")
            await until(pilot, lambda: screen.tab == "operations", "the tab")
            screen.query_one("#op-table").focus()
            await pilot.press("enter")                                   # details of the first operation
            await until(pilot, lambda: isinstance(app.screen, OperationDetail), "the detail view")
            detail = app.screen
            assert detail.op_id == screen.op_order[0]
            assert screen.op_order[0] in str(detail.query_one("#detail-nav", Static).render())
            await pilot.press("right", "right", "left")                  # next, next, previous
            assert detail.op_id == screen.op_order[1]
            await pilot.press("left", "left")                            # stops at the first one
            assert detail.op_id == screen.op_order[0]
            await pilot.press("right", "escape")                         # back: the list follows
            await until(pilot, lambda: app.screen is screen, "the list")
            assert screen.selected["operations"] == screen.op_order[1]

    asyncio.run(scenario())


def test_number_keys_switch_tabs_even_with_focus_in_the_capability_list(ready):  # noqa: F811
    """Regression: with the Capabilities list focused, the Generate key showed Generate for a split second and then Textual
    refocused the (scrollable) capability detail, which switched the tab back."""
    async def scenario():
        app = ReviewApp()
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, PackageScreen) and app.screen.caps, "the package")
            screen = app.screen
            for start, focused in (("4", "cap-list"), ("2", "rec-list"), ("3", "op-table")):
                await pilot.press(start)
                await until(pilot, lambda f=focused: app.focused is not None and app.focused.id == f, f"focus {focused}")
                await pilot.press("6")                                   # Generate
                await pilot.pause(0.3)                                   # long enough for a refocus to switch back
                assert screen.tab == "generate", f"from tab {start}"
    asyncio.run(scenario())
