"""Guided review and approval TUI: pick a package, then follow the path from recording to generation.

Screens: package picker -> package (tabs: Overview, Recordings, Operations, Capabilities, Generate).
Approvals use the same rules as the CLI (``operations.approval``, ``approve_capability``) and the same
human confirmation: a one-time code shown only on screen and typed back; the reviewer's name is taken
from git (``user.name``) or the OS login.
"""
from __future__ import annotations

import io
import secrets
from collections import Counter
from collections.abc import Callable
from typing import ClassVar

from rich.console import Console
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.option_list import Option

from ifs_agent_ops.operations.approval import reviewer_name
from ifs_agent_ops.tui import status as st
from ifs_agent_ops.workspace.package import Package

STATE_STYLE = {"APPROVED": "green", "READY": "cyan", "CHECK": "yellow", "UNRESOLVED": "magenta", "BLOCKED": "red",
               "DRIFTED": "bold #ff8700",
               "new": "yellow", "drafted": "green", "not drafted": "cyan"}
# Operations a capability uses are the ones that ever need approval; the rest is web-client evidence.
FILTERS = ("used by a capability", "all", "needs approval", "approved")


def rich_text(render: Callable, *args, width: int = 110) -> Text:
    """Render one of the CLI's Rich views into Text for a TUI pane."""
    buf = io.StringIO()
    console = Console(file=buf, width=width, force_terminal=True, color_system="truecolor", highlight=False,
                      emoji=False, soft_wrap=False)
    render(console, *args)
    return Text.from_ansi(buf.getvalue())


def styled(value: str) -> Text:
    return Text(value, style=STATE_STYLE.get(value, ""))


def item(title: str, state: str, subtitle: str) -> Text:
    """A two-line entry for the single-column lists: a status dot and the name, then a dim subtitle."""
    colour = STATE_STYLE.get(state, "green" if state == "current" else "yellow")
    return Text.assemble(("● ", colour), (title, "bold"), "\n  ", (subtitle, "dim"))


WRITES = ("POST", "PATCH", "PUT", "DELETE")
METHOD_STYLE = {"GET": "cyan", "POST": "bold yellow", "PATCH": "bold magenta", "PUT": "bold magenta",
                "DELETE": "bold red"}


def call_label(v) -> Text:
    """One line per recorded call: #ref METHOD service/target -> status response."""
    name = f"{v.service}/{v.target}" if v.service else "(non-IFS)"
    tag = " [action]" if v.is_action else (" [$batch]" if v.kind == "ifs-batch" else "")
    t = Text.assemble((f"#{v.ref} ", "dim"), (f"{v.method} ", METHOD_STYLE.get(v.method, "")),
                      (name, "bold" if v.method in WRITES else ""), (tag, "dim"), "  -> ")
    t.append(f"{v.status}", style="red" if v.failed else "green")
    t.append(f" {v.response}" + (" +etag" if v.returns_etag else ""), style="dim")
    if v.failed:
        t.append("  FAILED", style="bold red")
    return t


def has_details(v, into: dict) -> bool:
    return bool(v.filter_pattern or v.select or v.expand or v.body_keys or v.headers or into.get(v.ref))


def detail_lines(data: dict) -> list[Text]:
    """A call's details (filter, select, expand, body keys, headers, values it received), or the data flow."""
    if "flow" in data:
        return [Text.assemble((f"#{ln.to_ref} ", "dim"), ln.to_place, (" <- ", "dim"),
                              (f"#{ln.from_ref} ", "dim"), ln.from_field) for ln in data["flow"]]
    v, lines = data["view"], []
    for label, value in (("Filter", v.filter_pattern), ("Select", ", ".join(v.select)),
                         ("Expand", ", ".join(v.expand)), ("Body keys", ", ".join(v.body_keys)),
                         ("Headers", ", ".join(v.headers))):
        if value:
            lines.append(Text.assemble((f"{label}: ", "bold"), value))
    for ln in data["links"]:
        lines.append(Text.assemble(("receives ", "dim"), ln.to_place, (" <- ", "dim"),
                                   (f"#{ln.from_ref} ", "dim"), ln.from_field))
    return lines


def used_by(r: dict) -> list[str]:
    """The capabilities whose steps use this operation."""
    return sorted({u["capability"] for u in r.get("request_usage", [])})


def attention(r: dict) -> Text:
    """What a reviewer must look at in an operation, from its findings."""
    a = r["attention"]
    parts = [(a.get("blocking", 0), "blocking", "red"), (a.get("open", 0), "open question", "magenta"),
             (a.get("check", 0), "check", "yellow")]
    t = Text()
    for n, label, colour in parts:
        if n:
            t.append(f"{n} {label}{'s' if n > 1 else ''}  ", style=colour)
    return t


# ── dialogs ─────────────────────────────────────────────────────────────────

class MessageDialog(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape,enter", "dismiss", "Close")]

    def __init__(self, title: str, body: str | Text) -> None:
        super().__init__()
        self.title_text, self.body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Static(self.body)
            yield Button("Close", variant="primary", id="close")

    @on(Button.Pressed, "#close")
    def close(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str, confirm: str) -> None:
        super().__init__()
        self.title_text, self.body, self.confirm = title, body, confirm

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(self.body, classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.confirm, variant="primary", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ApprovalDialog(ModalScreen[str | None]):
    """The human approval gate: the one-time code shown here and typed back (Enter approves).
    Returns the reviewer's name (git user.name, else the OS login), or None when cancelled."""
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, meaning: str, details: str | Text, reviewer: str) -> None:
        super().__init__()
        self.title_text, self.meaning, self.details, self.reviewer = title, meaning, details, reviewer
        self.code = secrets.token_hex(3).upper()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog approval"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(self.meaning, classes="dialog-text")
            with VerticalScroll(classes="dialog-body"):
                yield Static(self.details)
            yield Static(Text.assemble("One-time confirmation code: ", (self.code, "bold reverse"),
                                       ("   approving as ", "dim"), self.reviewer), id="code")
            yield Input(placeholder="Type the confirmation code, then Enter", id="in-code")
            yield Static("", id="problem")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Approve", variant="success", id="approve")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#in-code", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#approve")
    def submit(self) -> None:
        if self.query_one("#in-code", Input).value.strip().upper() != self.code:
            self.query_one("#problem", Static).update(Text("The confirmation code does not match.", style="bold red"))
            return
        self.dismiss(self.reviewer)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ── package picker ──────────────────────────────────────────────────────────

LOGO = (
    "▄▀█ █▀▀ █▀▀ █▄ █ ▀█▀   █▀█ █▀█ █▀▀",
    "█▀█ █▄█ ██▄ █ ▀█  █    █▄█ █▀▀ ▄▄█",
)
PIPELINE = ("record", "draft", "approve", "generate")
GRADIENT = ("#5fd7ff", "#5fafff", "#5f87ff", "#875fff", "#af5fff")


def banner() -> Text:
    """The start screen's logotype: a quiet left-to-right gradient, and the review path as a pipeline."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        ver = version("ifs-agent-ops-toolkit")
    except PackageNotFoundError:
        ver = ""
    width = max(len(line) for line in LOGO)
    t = Text()
    t.append("i f s\n", style="dim #5fafff")
    for line in LOGO:
        for i, ch in enumerate(line):
            t.append(ch, style=GRADIENT[min(i * len(GRADIENT) // width, len(GRADIENT) - 1)])
        t.append("\n")
    t.append("\n")
    for n, stage in enumerate(PIPELINE):
        t.append("◆" if n == 0 else "──◆", style=GRADIENT[n + 1] if n + 1 < len(GRADIENT) else GRADIENT[-1])
        t.append(f" {stage} ", style="dim")
    t.append(f"\n\nreview and approval{f'  ·  v{ver}' if ver else ''}", style="dim italic")
    return t


class PackagePicker(Screen):
    def compose(self) -> ComposeResult:
        from ifs_agent_ops.workspace.discovery import discover_packages

        self.packages, problems = discover_packages()
        yield Header()
        with Vertical(id="picker"):
            yield Static(banner(), id="banner")
            yield Label("Choose a package to review", classes="heading")
            yield Static("Use the arrow keys and Enter. Packages come from IFS_AGENT_OPS_PACKAGES.", classes="muted")
            yield OptionList(*[Option(Text.assemble((p.display_name or p.name, "bold"), f"   {p.name}\n",
                                                    (str(p.root), "dim")), id=p.name) for p in self.packages],
                             id="packages")
            if not self.packages:
                yield Static("No packages found. Set IFS_AGENT_OPS_PACKAGES in the toolkit's .env "
                             "(paths separated by ';').", classes="warn")
            for problem in problems:
                yield Static(problem, classes="warn")
        yield Footer()

    @on(OptionList.OptionSelected, "#packages")
    def open_package(self, event: OptionList.OptionSelected) -> None:
        package = next(p for p in self.packages if p.name == event.option.id)
        self.app.push_screen(PackageScreen(package))


# ── package ─────────────────────────────────────────────────────────────────

TABS = ("overview", "recordings", "operations", "capabilities", "generate")


class PackageScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Packages"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "draft", "Draft operations"),
        Binding("f", "filter", "Filter"),
        Binding("a", "approve", "Approve"),
        Binding("o", "approve_operations", "Approve its operations"),
        Binding("w", "write_review", "Review report"),
        Binding("g", "generate", "Generate"),
        *[Binding(str(i), f"tab('{t}')", t.capitalize(), show=False) for i, t in enumerate(TABS, 1)],
    ]
    # action -> tabs where it applies (others are hidden from the footer)
    ACTION_TABS: ClassVar[dict[str, set[str]]] = {"draft": {"operations", "recordings"}, "filter": {"operations"}, "approve": {"operations",
                   "capabilities"}, "approve_operations": {"capabilities"}, "write_review": {"capabilities"},
                   "generate": {"generate"}}

    def __init__(self, package: Package) -> None:
        super().__init__()
        self.package = package
        self.reviews: dict[str, dict] = {}
        self.caps: dict[str, st.CapabilityState] = {}
        self.recs: dict[str, st.RecordingInfo] = {}
        self.selected: dict[str, str | None] = {"recordings": None, "operations": None, "capabilities": None}
        self.op_order: list[str] = []  # operation ids in list order (the detail view steps through them)
        self.analyses: dict[str, tuple] = {}  # recording -> (views, links), parsed once per refresh
        self.drift: dict[str, list[str]] = {}  # approved operations the newer recordings disagree with
        self.filter = FILTERS[0]

    def op_state(self, r: dict) -> str:
        """The review state, with DRIFTED for an approved operation whose recordings now differ."""
        return "DRIFTED" if r["state"] == "APPROVED" and r["id"] in self.drift else r["state"]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane("1 Overview", id="overview"):
                yield VerticalScroll(Static("Loading...", id="overview-body"))
            with TabPane("2 Recordings", id="recordings"), Horizontal():
                yield OptionList(id="rec-list", classes="side")
                with Vertical(classes="detail"):
                    yield Static("Select a recording to see its steps and calls.", id="rec-head")
                    yield Tree("recording", id="rec-tree")
            with TabPane("3 Operations", id="operations"), Vertical():
                yield Static("Loading reviews...", id="op-filter", classes="muted")
                yield DataTable(id="op-table", cursor_type="row", zebra_stripes=True)
            with TabPane("4 Capabilities", id="capabilities"), Horizontal():
                yield OptionList(id="cap-list", classes="side")
                yield VerticalScroll(Static("", id="cap-detail"), classes="detail")
            with TabPane("5 Generate", id="generate"):
                yield VerticalScroll(Static("Open this tab to check what generation would change.", id="gen-body"))
        yield Footer()

    def on_mount(self) -> None:
        self.title = self.package.display_name or self.package.name
        self.sub_title = str(self.package.root)
        self.query_one("#op-table", DataTable).add_columns("State", "Operation", "Used by", "Call", "Rev",
                                                           "Needs attention")
        self.action_refresh()

    # ── tabs and bindings ──

    @property
    def tab(self) -> str:
        return self.query_one("#tabs", TabbedContent).active or "overview"

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        tabs = self.ACTION_TABS.get(action)
        if tabs is None:
            return True
        try:
            return self.tab in tabs
        except NoMatches:  # before the tabs exist
            return False

    @on(TabbedContent.TabActivated)
    def tab_changed(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_bindings()
        if self.tab == "generate":
            self.check_generation()

    def action_back(self) -> None:
        self.app.pop_screen()

    # ── loading ──

    def action_refresh(self) -> None:
        self.analyses.clear()
        self.load_overview()
        self.load_reviews()

    @work(thread=True, exclusive=True, group="overview")
    def load_overview(self) -> None:
        view = st.overview(self.package)
        self.app.call_from_thread(self.show_overview, view)

    def show_overview(self, view: st.Overview) -> None:
        self.caps = {c.id: c for c in view.capabilities}
        self.recs = {r.rel: r for r in view.recordings}
        self.drift = view.drifted
        if self.reviews:
            self.fill_operations()
        body = Text()
        body.append("Next steps\n", style="bold underline")
        for n, step in enumerate(view.next_steps, 1):
            body.append(f"  {n}. {step}\n")
        body.append("\nPackage\n", style="bold underline")
        body.append(f"  {self.package.name} {self.package.version}  {self.package.root}\n")
        if view.git:
            body.append(f"  {len(view.uncommitted)} uncommitted file(s) in the package repository\n",
                        style="yellow" if view.uncommitted else "green")
        else:
            body.append("  not a git repository\n", style="dim")
        body.append("\nRecordings\n", style="bold underline")
        for r in view.recordings:
            body.append(f"  {r.state:<12}", style=STATE_STYLE.get(r.state, ""))
            body.append(f" {r.workflow}/{r.name}\n")
        body.append("\nOperations\n", style="bold underline")
        body.append("  " + ", ".join(f"{n} {s}" for s, n in sorted(view.operations.items())))
        if view.drifted:
            body.append(f"  ({len(view.drifted)} approved one(s) DRIFTED from the newer recordings)",
                        style=STATE_STYLE["DRIFTED"])
        body.append("\n")
        body.append("\nCapabilities\n", style="bold underline")
        for c in view.capabilities:
            body.append(f"  {c.id}: {c.status} rev {c.revision}, ")
            body.append("approval current" if c.approval_current else "approval NOT current",
                        style="green" if c.approval_current else "yellow")
            body.append(f", uses {len(c.operations)} operation(s)\n")
        body.append("\nKeys: 1-5 switch tabs, arrows move, r refreshes, Esc goes back to the package list.\n",
                    style="dim")
        self.query_one("#overview-body", Static).update(body)

        self.fill_side_list("rec-list", "recordings", [
            (r.rel, item(r.name, r.state, f"{r.state} · {r.workflow}"
                         + (" · not in a capability yet" if r.drafted and not r.in_capability else "")))
            for r in view.recordings])
        self.fill_side_list("cap-list", "capabilities", [
            (c.id, item(c.id, "current" if c.approval_current else "needs approval",
                        f"{c.status} rev {c.revision} · " + (
                            f"{len(c.unapproved_operations)} operation(s) need approval" if c.unapproved_operations
                            else "approval current" if c.approval_current else "capability needs approval")))
            for c in view.capabilities])
        self.show_capability(self.selected["capabilities"])

    def fill_side_list(self, list_id: str, kind: str, items: list[tuple[str, Text]]) -> None:
        """Refill a single-column list, keeping the selection (keys act on the first item until it moves)."""
        options = self.query_one(f"#{list_id}", OptionList)
        options.clear_options()
        options.add_options([Option(prompt, id=key) for key, prompt in items])
        keys = [key for key, _ in items]
        if self.selected[kind] not in keys:
            self.selected[kind] = keys[0] if keys else None
        if self.selected[kind] is not None:
            options.highlighted = keys.index(self.selected[kind])

    @work(thread=True, exclusive=True, group="reviews")
    def load_reviews(self) -> None:
        from ifs_agent_ops.operations import service
        from ifs_agent_ops.operations.store import load_operations

        ids = sorted(load_operations(self.package))
        review_set, _ = service.operation_reviews(self.package, ids) if ids else ({"operations": []}, "")
        self.app.call_from_thread(self.show_reviews, {r["id"]: r for r in review_set["operations"]})

    def show_reviews(self, reviews: dict[str, dict]) -> None:
        self.reviews = reviews
        self.fill_operations()
        if isinstance(self.app.screen, OperationDetail):  # an approval from the detail view: show the new state
            self.app.screen.show()

    def fill_operations(self) -> None:
        table = self.query_one("#op-table", DataTable)
        table.clear()
        fallback = self.filter == FILTERS[0] and not any(used_by(r) for r in self.reviews.values())
        state = self.op_state
        shown = [r for r in self.reviews.values()
                 if self.filter == "all" or fallback
                 or (self.filter == FILTERS[0] and used_by(r))
                 or (self.filter == "approved" and state(r) == "APPROVED")
                 or (self.filter == "needs approval" and state(r) != "APPROVED")]
        order = {s: i for i, s in enumerate(("DRIFTED", "BLOCKED", "UNRESOLVED", "CHECK", "READY", "APPROVED"))}
        rows = sorted(shown, key=lambda r: (order.get(state(r), 9), r["id"]))
        self.op_order = [r["id"] for r in rows]
        for r in rows:
            needs = attention(r)
            if r["id"] in self.drift:
                needs = Text.assemble((f"recordings differ: {', '.join(self.drift[r['id']])}  ", STATE_STYLE["DRIFTED"]),
                                      needs)
            users = used_by(r)
            table.add_row(styled(state(r)), r["id"].split(".", 1)[-1],
                          Text(", ".join(users) if users else "evidence only", style="dim"),
                          r["method"], f"{r['status']} r{r['revision']}", needs, key=r["id"])
        if rows and self.selected["operations"] not in self.op_order:
            self.selected["operations"] = rows[0]["id"]  # keys act on the first row until the cursor moves
        if self.selected["operations"] in self.op_order:
            table.move_cursor(row=self.op_order.index(self.selected["operations"]))
        counts = Counter(state(r) for r in self.reviews.values())
        label = "all (no capability uses any operation yet)" if fallback else self.filter
        line = Text(f"Showing {label}: {len(shown)} of {len(self.reviews)}   ")
        for s, n in sorted(counts.items(), key=lambda kv: order.get(kv[0], 9)):
            line.append(f"{n} {s}  ", style=STATE_STYLE.get(s, ""))
        line.append("\nEnter: details   f: filter   a: approve one.   'Evidence only' operations are other calls the "
                    "IFS web client made; they need no approval unless a capability starts using them. Approve a "
                    "capability's operations as a set on the Capabilities tab (o), after testing.", style="dim")
        self.query_one("#op-filter", Static).update(line)

    # ── selection ──

    @on(OptionList.OptionHighlighted)
    def option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        key = event.option.id
        if event.option_list.id == "rec-list":
            self.selected["recordings"] = key
            self.show_recording(key)
        elif event.option_list.id == "cap-list":
            self.selected["capabilities"] = key
            self.show_capability(key)

    @on(DataTable.RowHighlighted, "#op-table")
    def operation_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.selected["operations"] = event.row_key.value if event.row_key else None

    @on(DataTable.RowSelected, "#op-table")
    def open_operation(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value in self.op_order:
            self.app.push_screen(OperationDetail(self, self.op_order.index(event.row_key.value)), self.back_to_list)

    def back_to_list(self, op_id: str | None) -> None:
        """Back from the detail view: the list cursor follows the last operation looked at."""
        if op_id in self.op_order:
            self.selected["operations"] = op_id
            self.query_one("#op-table", DataTable).move_cursor(row=self.op_order.index(op_id))

    @work(thread=True, exclusive=True, group="recording")
    def show_recording(self, rel: str | None) -> None:
        from ifs_agent_ops.analysis.sequence import build_sequence
        from ifs_agent_ops.recordings.har import load_har

        rec = self.recs.get(rel or "")
        if rec is None:
            return
        if rel not in self.analyses:  # parse each recording once per refresh
            self.analyses[rel] = build_sequence(load_har(rec.path))
        views, links = self.analyses[rel]
        self.app.call_from_thread(self.fill_recording, rec, views, links)

    def fill_recording(self, rec: st.RecordingInfo, views: list, links: list) -> None:
        """The recording as a tree: plan steps, then each step's calls ($batch parts under their batch), then
        the data flow. Steps and calls start collapsed; a call's details are only built when it is expanded."""
        from ifs_agent_ops.recordings.annotations import entry_tasks

        ifs = [v for v in views if v.kind != "other"]
        status = {"new": (("New: not committed yet. Check the steps and calls, then commit it in the package "
                           "repository."), "yellow"),
                  "not drafted": ("Committed, not drafted yet: press d to draft operations.", "cyan"),
                  "drafted": ("Committed and drafted.", "green")}[rec.state]
        plan = rec.plan or {}
        counts = (f"{len(ifs)} IFS calls ({sum(v.failed for v in ifs)} failed), "
                  f"{len(views) - len(ifs)} other, {len(links)} data-flow links")
        head = Text.assemble((f"{rec.workflow}/{rec.name}\n", "bold"), (status[0] + "\n", status[1]),
                             (counts, "dim"))
        if plan:
            head.append(f"\nIntent: {plan.get('intent', '')}   ")
            head.append("plan complete" if plan.get("complete") else "plan INCOMPLETE",
                        style="green" if plan.get("complete") else "yellow")
        head.append("\nEnter or click expands a step or a call.", style="dim")
        self.query_one("#rec-head", Static).update(head)

        tree = self.query_one("#rec-tree", Tree)
        tree.clear()
        tree.show_root = False
        into: dict[str, list] = {}
        for ln in links:
            into.setdefault(ln.to_ref, []).append(ln)
        owner = entry_tasks(plan)
        tasks = {t["id"]: t for t in plan.get("tasks", [])}
        groups: dict = {}
        for v in ifs:
            groups.setdefault(owner.get(v.index) if plan else "all", []).append(v)
        for t in plan.get("tasks", []):  # planned steps without calls still show (e.g. a skipped optional step)
            groups.setdefault(t["id"], [])
        order = [t["id"] for t in plan.get("tasks", [])] + [k for k in groups if k not in tasks]
        for key in order:
            calls = groups.get(key, [])
            writes = sum(1 for v in calls if v.method in WRITES and v.kind == "ifs")
            if key in tasks:
                t = tasks[key]
                label = Text.assemble((f"{t['title']}", "bold"), f"  {t['status']}",
                                      (", optional" if t.get("optional") else "", "dim"))
            else:
                label = Text("Outside any step" if key is None else "Calls", style="bold")
            label.append(f"   {len(calls)} call(s), {writes} write(s)", style="dim")
            step = tree.root.add(label, expand=False, allow_expand=bool(calls))
            batch = None
            for v in calls:
                parent = batch if (v.part is not None and batch is not None) else step
                node = parent.add(call_label(v), data={"view": v, "links": into.get(v.ref, [])},
                                  expand=False, allow_expand=v.kind == "ifs-batch" or has_details(v, into))
                if v.kind == "ifs-batch":
                    batch = node
                elif v.part is None:
                    batch = None
        flow_note = f"   {len(links)} link(s): value sent <- earlier response field"
        tree.root.add(Text.assemble(("Data flow", "bold"), (flow_note, "dim")),
                      data={"flow": links}, expand=False, allow_expand=bool(links))

    @on(Tree.NodeExpanded, "#rec-tree")
    def expand_node(self, event: Tree.NodeExpanded) -> None:
        """Build a call's details (or the data-flow list) the first time it is expanded."""
        node, data = event.node, event.node.data
        if data and not data.get("loaded") and ("view" in data or "flow" in data):
            data["loaded"] = True
            for line in detail_lines(data):
                node.add_leaf(line)

    def show_capability(self, cap_id: str | None) -> None:
        cap = self.caps.get(cap_id or "")
        detail = self.query_one("#cap-detail", Static)
        if cap is None:
            detail.update("No capabilities yet. A capability is written (with Claude's help) in "
                          "capabilities/<id>.yaml and references approved operations.")
            return
        t = Text()
        t.append(f"{cap.title}\n", style="bold")
        t.append(f"{cap.id}  {cap.status} rev {cap.revision}\n\n")
        if cap.approval_current:
            t.append("Approval is current: the capability can be generated.\n", style="green")
        else:
            t.append(f"Not generated: {cap.reason}\n", style="yellow")
        if cap.open_questions:
            t.append(f"{cap.open_questions} open question(s) recorded by the author; see the review report (w).\n",
                     style="magenta")
        t.append(f"\nOperations used ({len(cap.operations)})\n", style="bold underline")
        for op in cap.operations:
            ok = op not in cap.unapproved_operations
            t.append("  ✓ " if ok else "  ✗ ", style="green" if ok else "yellow")
            t.append(op.split(".", 1)[-1] + ("" if ok else "  (needs approval)"))
            if op in self.drift:
                t.append(f"  DRIFTED: newer recordings differ in {', '.join(self.drift[op])}",
                         style=STATE_STYLE["DRIFTED"])
            t.append("\n")
        t.append("\nKeys: o approves the operations it uses, a approves the capability, w writes the review "
                 "report.\n", style="dim")
        detail.update(t)

    # ── actions ──

    def action_filter(self) -> None:
        self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
        self.fill_operations()

    def action_draft(self) -> None:
        def go(confirmed: bool) -> None:
            if confirmed:
                self.run_draft()

        self.app.push_screen(ConfirmDialog(
            "Draft operations",
            "Draft operations and observed workflows from every recording in the package.\n\n"
            "This writes into the package (draft operations and workflows). Approved operations keep their "
            "approval until their technical content changes. Nothing is committed.", "Draft"), go)

    @work(thread=True, exclusive=True, group="draft")
    def run_draft(self) -> None:
        from ifs_agent_ops.operations import service

        report, skipped = service.draft(self.package)
        summary = service.draft_summary(self.package, report, skipped)
        self.app.call_from_thread(self.app.push_screen, MessageDialog("Draft finished", summary))
        self.app.call_from_thread(self.action_refresh)

    def action_approve(self) -> None:
        if self.tab == "operations":
            self.approve_operation(self.selected["operations"])
        elif self.tab == "capabilities":
            self.approve_capability(self.selected["capabilities"])

    def approve_operation(self, op_id: str | None) -> None:
        from ifs_agent_ops.cli.review_view import render_approval
        from ifs_agent_ops.operations.approval import (
            ApprovalChanged,
            apply_approvals,
            blockers,
        )

        r = self.reviews.get(op_id or "")
        if r is None:
            return
        if r["state"] == "APPROVED":
            self.notify(f"{op_id} already has a current technical approval." + (
                " It is DRIFTED: drafting does not change approved operations, so there is nothing new to approve "
                "here yet." if op_id in self.drift else ""))
            return
        reasons = blockers(self.package, r["id"], include_questions=False)
        if reasons:
            self.app.push_screen(MessageDialog("Cannot approve: validation errors", "\n".join(reasons)))
            return

        def done(reviewer: str | None) -> None:
            if reviewer is None:
                return
            try:
                op = apply_approvals(self.package, [r], reviewer)[0]
            except ApprovalChanged as exc:
                self.app.push_screen(MessageDialog("Refused", str(exc)))
                return
            self.notify(f"Approved {op.id} (revision {op.revision}) by {reviewer}.")
            self.action_refresh()

        self.app.push_screen(ApprovalDialog(
            "Technical approval", f"Technical approval of {r['id']}: this is a technically correct canonical "
            "representation of the IFS operation. It does not claim runtime validation.",
            rich_text(render_approval, r, self.package.name, "(Operations tab: Enter for the full review)", width=96),
            reviewer_name(self.package.root)), done)

    def action_approve_operations(self) -> None:
        from ifs_agent_ops.cli.review_view import render_operation_set_approval
        from ifs_agent_ops.operations.approval import (
            ApprovalChanged,
            apply_approvals,
            plan_operation_set,
        )
        from ifs_agent_ops.operations.review_markdown import write_review_artifacts

        cap_id = self.selected["capabilities"]
        if not cap_id:
            return
        plan = plan_operation_set(self.package, cap_id)
        write_review_artifacts(self.package, plan.review_set, plan.report_name)
        if plan.blocked:
            lines = [f"{op}\n" + "\n".join(f"  - {r}" for r in reasons) for op, reasons in plan.blocked]
            self.app.push_screen(MessageDialog(
                f"Cannot approve: {len(plan.blocked)} operation(s) are blocked",
                "Resolve these first (or approve them one by one on the Operations tab):\n\n" + "\n".join(lines)))
            return
        if not plan.pending:
            self.notify(f"All {len(plan.current)} operation(s) used by {cap_id} already have a current approval.")
            return

        def done(reviewer: str | None) -> None:
            if reviewer is None:
                return
            try:
                approved = apply_approvals(self.package, plan.pending, reviewer)
            except ApprovalChanged as exc:
                self.app.push_screen(MessageDialog("Refused", str(exc)))
                return
            self.notify(f"{len(approved)} operation(s) approved by {reviewer}.")
            self.action_refresh()

        self.app.push_screen(ApprovalDialog(
            "Technical approval of an operation set",
            f"Technical approval of {len(plan.pending)} operation(s) used by {cap_id}, each recorded separately.",
            rich_text(render_operation_set_approval, cap_id, plan.pending, plan.current, width=96),
            reviewer_name(self.package.root)), done)

    def approve_capability(self, cap_id: str | None) -> None:
        from ifs_agent_ops.operations.capability_review import write_review
        from ifs_agent_ops.operations.capability_store import (
            ApprovalRefused,
            approve_capability,
            load_capabilities,
        )
        from ifs_agent_ops.operations.store import load_operations, load_workflows

        caps = load_capabilities(self.package)
        if cap_id not in caps:
            return
        cap = caps[cap_id]
        path = write_review(self.package, cap, load_operations(self.package), load_workflows(self.package))
        details = Text(f"Review report: {path}\n")
        if cap.review_notes:
            details.append(f"\n{len(cap.review_notes)} open question(s) recorded by the author:\n", style="magenta")
            for n in cap.review_notes:
                details.append(f"  - {n}\n")

        def done(reviewer: str | None) -> None:
            if reviewer is None:
                return
            try:
                approved = approve_capability(self.package, cap_id, reviewer)
            except ApprovalRefused as exc:
                self.app.push_screen(MessageDialog("Refused", str(exc)))
                return
            self.notify(f"Approved {approved.id} (revision {approved.revision}) by {reviewer}.")
            self.action_refresh()

        self.app.push_screen(ApprovalDialog(
            "Capability approval",
            "Capability approval: this is the intentional and safe business contract that downstream agents and "
            "applications may use. Approval pins this exact content and the revisions of the operations it uses. "
            f"It does NOT claim runtime validation (runtime status stays '{cap.runtime_validation.status}').",
            details, reviewer_name(self.package.root)), done)

    def action_write_review(self) -> None:
        from ifs_agent_ops.operations.capability_review import write_review
        from ifs_agent_ops.operations.capability_store import load_capabilities
        from ifs_agent_ops.operations.store import load_operations, load_workflows

        caps = load_capabilities(self.package)
        cap_id = self.selected["capabilities"]
        if cap_id in caps:
            path = write_review(self.package, caps[cap_id], load_operations(self.package),
                                load_workflows(self.package))
            self.app.push_screen(MessageDialog("Review report written", str(path)))

    @work(thread=True, exclusive=True, group="generate")
    def check_generation(self) -> None:
        self.app.call_from_thread(lambda: self.query_one("#gen-body", Static).update("Checking..."))
        text = self.generation_text(check=True)
        self.app.call_from_thread(lambda: self.query_one("#gen-body", Static).update(text))

    def generation_text(self, check: bool) -> Text:
        from ifs_agent_ops.generators.connector import generate as connector
        from ifs_agent_ops.generators.skills import generate as skills

        def rel(path) -> str:
            try:
                return path.relative_to(self.package.root).as_posix()
            except ValueError:
                return str(path)

        def line(t: Text, r, label: str) -> None:
            t.append(f"  {r.action:<14}", style="yellow" if r.action.startswith("would-") or r.action == "refused"
                     else "green")
            t.append(f"{label}\n")
            if r.message:
                t.append(f"                {r.message}\n", style="dim")

        t = Text()
        t.append("Agent skills\n", style="bold underline")
        for r in skills(self.package, check=check):
            line(t, r, f"{r.capability}  {rel(r.path)}")
        gen = connector(self.package, check=check)
        t.append("\nPower Platform connector\n", style="bold underline")
        for r in gen.results:
            line(t, r, rel(r.path))
        for e in gen.errors:
            t.append(f"  error: {e}\n", style="red")
        if check:
            t.append("\nOnly approved capabilities and operations generate. Press g to write these files "
                     "(nothing is committed).\n", style="dim")
        return t

    def action_generate(self) -> None:
        def go(confirmed: bool) -> None:
            if confirmed:
                self.run_generate()

        self.app.push_screen(ConfirmDialog(
            "Generate", "Write the agent skills and the Power Platform connector into the package from the approved "
            "capabilities. Nothing is committed.", "Generate"), go)

    @work(thread=True, exclusive=True, group="generate")
    def run_generate(self) -> None:
        text = self.generation_text(check=False)
        self.app.call_from_thread(self.app.push_screen, MessageDialog("Generated", text))
        self.app.call_from_thread(self.check_generation)


class OperationDetail(Screen[str | None]):
    """One operation's full review, full screen. ← → step through the list in its order; Esc returns to it."""
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Back to list"),
        Binding("left", "step(-1)", "Previous", priority=True),
        Binding("right", "step(1)", "Next", priority=True),
        Binding("a", "approve", "Approve"),
    ]

    def __init__(self, owner: PackageScreen, index: int) -> None:
        super().__init__()
        self.owner, self.index = owner, index

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="detail-nav", classes="muted")
        yield VerticalScroll(Static("", id="detail-body"), id="detail-scroll")
        yield Footer()

    def on_mount(self) -> None:
        self.title = self.owner.title
        self.show()

    @property
    def op_id(self) -> str | None:
        order = self.owner.op_order
        return order[self.index] if 0 <= self.index < len(order) else None

    def show(self) -> None:
        from ifs_agent_ops.cli.review_view import render_operation

        order = self.owner.op_order
        self.index = min(self.index, len(order) - 1)
        r = self.owner.reviews.get(self.op_id or "")
        if r is None:
            self.dismiss(None)
            return
        nav = Text.assemble(styled(self.owner.op_state(r)), f"   {r['id']}   ",
                            (f"{self.index + 1} of {len(order)}   ← → previous/next   a approve   Esc back", "dim"))
        self.query_one("#detail-nav", Static).update(nav)
        width = max(60, self.app.size.width - 4)
        body = Text()
        if r["id"] in self.owner.drift:
            body.append(f"DRIFTED: the newer recordings draft this operation differently "
                        f"({', '.join(self.owner.drift[r['id']])}). Drafting never changes an approved operation, so "
                        "the approved definition below still applies; decide with Claude, while testing, whether the "
                        "new evidence should change it.\n\n", style=STATE_STYLE["DRIFTED"])
        body.append_text(rich_text(render_operation, r, self.owner.package.name, "details", width=width))
        self.query_one("#detail-body", Static).update(body)
        self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)

    def action_step(self, delta: int) -> None:
        new = self.index + delta
        if 0 <= new < len(self.owner.op_order):
            self.index = new
            self.show()

    def action_approve(self) -> None:
        self.owner.approve_operation(self.op_id)  # refreshes the reviews, which re-renders this view

    def action_close(self) -> None:
        self.dismiss(self.op_id)


class ReviewApp(App):
    # q quits and p opens the command palette from every screen (ctrl+p is taken in VS Code's terminal). Neither is
    # a priority binding, so a focused text box (the approval dialog, the palette's search) still types q and p;
    # declaring the palette binding here replaces Textual's default, which is a priority binding.
    COMMAND_PALETTE_BINDING: ClassVar[str] = "p"  # the footer shows this binding on its right, as "p Palette"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("p", "command_palette", "Palette", show=False, tooltip="Open the command palette"),
    ]
    TITLE = "IFS Agent Ops"
    SUB_TITLE = "review and approval"
    CSS = """
    #picker { padding: 1 4; }
    #banner { margin-bottom: 2; }
    .heading { text-style: bold; margin-bottom: 1; }
    .muted { color: $text-muted; }
    .warn { color: $warning; }
    #packages { height: auto; max-height: 20; margin-top: 1; }
    .side { width: 44; height: 1fr; border: none; }
    .detail { width: 1fr; border-left: solid $primary-background; padding: 0 1; }
    #op-filter { height: auto; margin-bottom: 1; }
    #op-table { height: 1fr; }
    #rec-head { height: auto; padding-bottom: 1; border-bottom: solid $primary-background; }
    #rec-tree { height: 1fr; }
    #detail-nav { height: 1; padding: 0 1; background: $boost; }
    #detail-scroll { padding: 0 1; }
    .dialog { width: 90%; max-width: 120; height: auto; max-height: 90%; padding: 1 2;
              border: thick $primary; background: $surface; }
    ApprovalDialog .dialog { border: thick $success; }
    MessageDialog, ConfirmDialog, ApprovalDialog { align: center middle; }
    .dialog-title { text-style: bold; margin-bottom: 1; }
    .dialog-text { margin-bottom: 1; }
    .dialog-body { height: auto; max-height: 20; margin-bottom: 1; }
    .dialog-buttons { height: auto; margin-top: 1; }
    .dialog-buttons Button { margin-right: 2; }
    #code { margin: 1 0; }
    """

    def on_mount(self) -> None:
        self.push_screen(PackagePicker())
