"""Guided TUI: pick a package, then follow the builder's path from recording to a deployed agent.

The path: plan and record in the browser side panel; import here; choose the IFS services and pin their metadata;
draft; work the recording into a capability with Claude and test it against IFS TEST (through the MCP server);
approve; generate; deploy; prove the deployed path; publish the agent. The Overview tab always names the next step.

Screens: package picker -> package (tabs: Overview, Recordings, Operations, Capabilities, Testing, Generate, Deploy).
The picker creates a package (n) and registers it in the toolkit's .env, so the side panel and Claude find it without
a restart. A recording nothing approved rests on can be removed (Delete) and recorded again.
Writes to IFS never start here: runtime cases that write run only from the command line with --allow-writes.
Approvals use the same rules as the CLI (``operations.approval``, ``approve_capability``). The TUI confirms with
a dialog (Enter approves): an agent cannot drive it, so no code is typed here; the CLI keeps the one-time code.
The reviewer's name is taken from git (``user.name``) or the OS login. Letter keys work with Caps Lock on.
"""
from __future__ import annotations

import io
import textwrap
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
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
    Tree,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

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
    color = STATE_STYLE.get(state, "green" if state == "current" else "yellow")
    return Text.assemble(("● ", color), (title, "bold"), "\n  ", (subtitle, "dim"))


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


def call_detail_lines(entry: dict, max_lines: int = 40) -> list[Text]:
    """A logged call's request and response, for the Testing tree: headers, bodies (cut), timing."""
    import json

    def block(title: str, value) -> list[Text]:
        if value in (None, {}, ""):
            return []
        text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
        rows = text.splitlines()
        out = [Text(title, style="bold")] + [Text("  " + r) for r in rows[:max_lines]]
        if len(rows) > max_lines:
            out.append(Text(f"  ... {len(rows) - max_lines} more line(s): ifs-agent-ops testlog show {entry['ref']}",
                            style="dim"))
        return out

    lines = [Text.assemble((f"{entry['method']} ", "bold"), entry["endpoint"],
                           (f"  -> {entry['status']}  {entry.get('duration_ms')} ms", "dim"))]
    if entry.get("error") and not entry.get("ok"):
        lines.append(Text(f"{entry['error']}: {entry.get('message') or ''}", style="red"))
    lines += block("Request headers", entry.get("request_headers"))
    lines += block("Request body", entry.get("request_body"))
    lines += block("Response headers", entry.get("response_headers"))
    lines += block("Response body", entry.get("response_body"))
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
    for n, label, color in parts:
        if n:
            t.append(f"{n} {label}{'s' if n > 1 else ''}  ", style=color)
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


class PickDialog(ModalScreen[str | None]):
    """Pick one item from a list (Enter picks, Esc cancels). Returns the item's id."""
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, text: str, items: list[tuple[str, Text | str]]) -> None:
        super().__init__()
        self.title_text, self.text, self.items = title, text, items

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(self.text, classes="dialog-text")
            yield OptionList(*[Option(prompt, id=key) for key, prompt in self.items], id="pick")

    def on_mount(self) -> None:
        self.query_one("#pick", OptionList).focus()

    @on(OptionList.OptionSelected, "#pick")
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ServicesDialog(ModalScreen[list[str] | None]):
    """Choose the IFS services (projections) the package uses. Space toggles, Enter (or OK) confirms."""
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel"),
                                         Binding("enter", "ok", "OK", priority=True)]

    def __init__(self, services: list[st.ServiceUse]) -> None:
        super().__init__()
        self.services = services

    def compose(self) -> ComposeResult:
        preselect = {s.name for s in self.services if s.listed} or (
            {self.services[0].name} if self.services and self.services[0].writes else set())
        with Vertical(classes="dialog"):
            yield Label("IFS services this package uses", classes="dialog-title")
            yield Static("The recordings call these IFS services, busiest writers first. Pick the ones the "
                         "capability is about: they go into package.yaml, and their metadata is pinned in the "
                         "package (needs the IFS sign-in). The web client also calls its own services (bookmarks, "
                         "settings, navigation); leave those out. Space toggles, Enter confirms. Services already "
                         "listed stay listed.", classes="dialog-text")
            yield SelectionList[str](*[
                Selection(Text.assemble((s.name, "bold"), f"   {s.calls} call(s), {s.writes} write(s)",
                                        ("   listed" if s.listed else "", "green"),
                                        ("   metadata pinned" if s.snapshot else "", "dim")),
                          s.name, s.name in preselect, disabled=s.listed)
                for s in self.services], id="services")
            with Horizontal(classes="dialog-buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#services", SelectionList).focus()

    @on(Button.Pressed, "#ok")
    def action_ok(self) -> None:
        chosen = set(self.query_one("#services", SelectionList).selected)
        self.dismiss([s.name for s in self.services if s.name in chosen or s.listed])

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ApprovalDialog(ModalScreen[str | None]):
    """The human approval gate in the TUI: review what is being approved, then Enter (or Approve) confirms.

    No code is typed here: an agent cannot drive the TUI, so a person at the keyboard is already the gate.
    (The CLI approve commands, which an agent could run, keep the one-time code.) Returns the reviewer's name
    (git user.name, else the OS login), or None when canceled."""
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, meaning: str, details: str | Text, reviewer: str) -> None:
        super().__init__()
        self.title_text, self.meaning, self.details, self.reviewer = title, meaning, details, reviewer

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog approval"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(self.meaning, classes="dialog-text")
            with VerticalScroll(classes="dialog-body"):
                yield Static(self.details)
            yield Static(Text.assemble(("Approving as ", "dim"), self.reviewer,
                                       ("   Enter approves, Esc cancels", "dim")), id="code")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Approve", variant="success", id="approve")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#approve", Button).focus()

    @on(Button.Pressed, "#approve")
    def submit(self) -> None:
        self.dismiss(self.reviewer)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class NewPackageDialog(ModalScreen[tuple | None]):
    """Create a package: a title, its permanent name and its folder. The name and folder follow the title until
    they are edited. Creating writes the package skeleton and lists the folder in IFS_AGENT_OPS_PACKAGES (.env).
    Returns (package, registration), or None when canceled."""
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        from ifs_agent_ops.workspace.discovery import default_parent

        self.parent_dir = default_parent()
        self.auto = {"name": "", "folder": ""}  # the values filled in from the title (kept while not edited)

    def compose(self) -> ComposeResult:
        example = "ifs-purchase-requisitions"
        with Vertical(classes="dialog"):
            yield Label("New package", classes="dialog-title")
            yield Static("A package holds the recordings, operations, capabilities and generated files for one area "
                         "of IFS work (for example purchase requisitions). Tab moves between fields, Enter creates.",
                         classes="dialog-text")
            yield Label("Title (shown in lists; can be renamed later)")
            yield Input(placeholder="Purchase Requisitions", id="display")
            yield Label("Package name (permanent: deployments and agents key on it)")
            yield Input(placeholder=example, id="name")
            yield Label("Folder")
            yield Input(placeholder=str(self.parent_dir / example) if self.parent_dir else f"<folder>/{example}",
                        id="folder")
            yield Label("Description (optional)")
            yield Input(id="description")
            yield Static("", id="error", classes="warn")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#display", Input).focus()

    @on(Input.Changed, "#display")
    def follow_title(self, event: Input.Changed) -> None:
        from ifs_agent_ops.workspace.discovery import suggest_name

        name = suggest_name(event.value)
        fields = {"name": name, "folder": str(self.parent_dir / name) if self.parent_dir and name else ""}
        for key, value in fields.items():
            box = self.query_one(f"#{key}", Input)
            if box.value == self.auto[key]:  # not edited by hand: keep following the title
                box.value = value
            self.auto[key] = value

    @on(Input.Submitted)
    @on(Button.Pressed, "#create")
    def create(self) -> None:
        from pathlib import Path

        from ifs_agent_ops.workspace.discovery import create_package
        from ifs_agent_ops.workspace.package import PackageError

        value = {k: self.query_one(f"#{k}", Input).value.strip() for k in ("display", "name", "folder", "description")}
        error = self.query_one("#error", Static)
        if not value["name"] or not value["folder"]:
            error.update("A package name and a folder are needed.")
            return
        try:
            result = create_package(Path(value["folder"]).expanduser(), value["name"], display_name=value["display"],
                                    description=value["description"])
        except (PackageError, OSError) as exc:
            error.update(str(exc))
            return
        self.dismiss(result)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ── package picker ──────────────────────────────────────────────────────────

LOGO = (
    "▄▀█ █▀▀ █▀▀ █▄ █ ▀█▀   █▀█ █▀█ █▀▀",
    "█▀█ █▄█ ██▄ █ ▀█  █    █▄█ █▀▀ ▄▄█",
)
PIPELINE = ("record", "test", "approve", "generate", "deploy")
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
    t.append(f"\n\nfrom a recorded task to a deployed agent{f'  ·  v{ver}' if ver else ''}", style="dim italic")
    return t


class PackagePicker(Screen):
    BINDINGS: ClassVar[list[Binding]] = [Binding("n,N", "new_package", "New package")]

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
                yield Static("No packages yet: press n to create one.", classes="warn")
            yield Static("n creates a new package and registers it, so the side panel and Claude see it right away "
                         "(no restart).", classes="muted")
            for problem in problems:
                yield Static(problem, classes="warn")
        yield Footer()

    @on(OptionList.OptionSelected, "#packages")
    def open_package(self, event: OptionList.OptionSelected) -> None:
        package = next(p for p in self.packages if p.name == event.option.id)
        self.app.push_screen(PackageScreen(package))

    def action_new_package(self) -> None:
        self.app.push_screen(NewPackageDialog(), self.created)

    async def created(self, result: tuple | None) -> None:
        if result is None:
            return
        package, registration = result
        await self.recompose()
        self.app.push_screen(PackageScreen(package))
        if registration.note:
            self.app.push_screen(MessageDialog("Package created", f"Created {package.name} at {package.root}.\n\n"
                                               + registration.note))
        else:
            where = f" and listed it in {registration.env_file}" if registration.env_file else ""
            self.notify(f"Created {package.name}{where}. The side panel and Claude see it now.")


# ── package ─────────────────────────────────────────────────────────────────

TABS = ("overview", "recordings", "operations", "capabilities", "testing", "generate", "deploy")
TEST_STYLE = {"passed": "green", "failed": "bold red", "inconclusive": "yellow", "not tested": "dim"}


class PackageScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Packages"),
        Binding("r,R", "refresh", "Refresh"),
        Binding("d,D", "draft", "Draft operations"),
        Binding("x,X", "redraft", "Redraft drifted"),
        Binding("f,F", "filter", "Filter"),
        Binding("a,A", "approve", "Approve"),
        Binding("o,O", "approve_operations", "Approve its operations"),
        Binding("w,W", "write_review", "Review report"),
        Binding("g,G", "generate", "Generate"),
        Binding("i,I", "import_session", "Import from side panel"),
        Binding("m,M", "services", "Services and metadata"),
        Binding("s,S", "plan", "Plan / status"),
        Binding("y,Y", "apply", "Deploy"),
        Binding("v,V", "validate", "Validate"),
        Binding("n,N", "render_agent", "Render agent"),
        Binding("delete", "remove_recording", "Remove recording"),
        *[Binding(str(i), f"tab('{t}')", t.capitalize(), show=False) for i, t in enumerate(TABS, 1)],
    ]
    # action -> tabs where it applies (others are hidden from the footer)
    ACTION_TABS: ClassVar[dict[str, set[str]]] = {
        "draft": {"operations", "recordings"}, "redraft": {"operations"}, "filter": {"operations"},
        "approve": {"operations", "capabilities"}, "approve_operations": {"capabilities"},
        "write_review": {"capabilities", "testing"}, "generate": {"generate"},
        "import_session": {"recordings", "overview"}, "services": {"recordings", "overview"},
        "remove_recording": {"recordings", "overview"},
        "plan": {"deploy"}, "apply": {"deploy"}, "validate": {"deploy"}, "render_agent": {"deploy"}}

    def __init__(self, package: Package) -> None:
        super().__init__()
        self.package = package
        self.reviews: dict[str, dict] = {}
        self.caps: dict[str, st.CapabilityState] = {}
        self.recs: dict[str, st.RecordingInfo] = {}
        self.selected: dict[str, str | None] = {"recordings": None, "operations": None, "capabilities": None,
                                                 "testing": None, "deploy": None}
        self.view: st.Overview | None = None
        self.test_log = None  # the active profile's test log, resolved when the Testing tab first loads
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
            with TabPane("5 Testing", id="testing"), Horizontal():
                yield OptionList(id="test-list", classes="side")
                with Vertical(classes="detail"):
                    yield Static("Select a capability to see what testing against IFS has shown.", id="test-head")
                    yield Tree("requirements", id="test-tree")
            with TabPane("6 Generate", id="generate"):
                yield VerticalScroll(Static("Open this tab to check what generation would change.", id="gen-body"))
            with TabPane("7 Deploy", id="deploy"), Horizontal():
                yield OptionList(id="deploy-list", classes="side")
                yield VerticalScroll(Static("", id="deploy-detail"), classes="detail")
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

    # the widget that gets focus when a tab is opened with its number key (arrows then act on it)
    TAB_FOCUS: ClassVar[dict[str, str]] = {"recordings": "#rec-list", "operations": "#op-table",
                                           "capabilities": "#cap-list", "testing": "#test-list",
                                           "deploy": "#deploy-list"}

    def action_tab(self, tab: str) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        # Move focus out of the current pane first: otherwise Textual refocuses a neighbor of the now hidden
        # widget (e.g. the scrollable capability detail) and TabbedContent switches back to that widget's tab.
        tabs.query_one(Tabs).focus()
        tabs.active = tab
        target = self.TAB_FOCUS.get(tab)
        if target:
            self.call_after_refresh(self.query_one(target).focus)

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
        self.view = view
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
        if view.sessions:
            body.append("\nRecorded in the side panel, not imported yet\n", style="bold underline")
            for s in view.sessions:
                body.append(f"  {s.id}  ", style="yellow")
                body.append(f"{s.intent}\n", style="dim")
        body.append("\nRecordings\n", style="bold underline")
        for r in view.recordings:
            body.append(f"  {r.state:<12}", style=STATE_STYLE.get(r.state, ""))
            body.append(f" {r.workflow}/{r.name}\n")
        listed = [s for s in view.services if s.listed]
        if view.services:
            body.append("\nIFS services\n", style="bold underline")
            for s in listed:
                body.append(f"  {s.name}  ", style="bold")
                body.append("metadata pinned\n" if s.snapshot else "metadata NOT pinned (m)\n",
                            style="green" if s.snapshot else "yellow")
            if not listed:
                body.append("  none chosen yet (Recordings tab, m)\n", style="yellow")
            others = len(view.services) - len(listed)
            if others:
                body.append(f"  {others} other service(s) the web client called (not part of the package)\n",
                            style="dim")
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
            body.append(f", uses {len(c.operations)} operation(s); testing: {c.testing_text()}\n")
        d = view.delivery
        if d is not None:
            body.append("\nDelivery\n", style="bold underline")
            stale = [k for k, ok in d.generated.items() if not ok]
            body.append("  generated: ")
            if stale:
                body.append(f"out of date: {', '.join(stale)}\n", style="yellow")
            else:
                body.append("skills, connector, flows and agent up to date\n", style="green")
            for t in d.targets:
                body.append(f"  target {t.name} ({t.environment}): ")
                body.append("flows deployed" if t.flows_deployed else "not deployed",
                            style="green" if t.flows_deployed else "yellow")
                if t.agent:
                    body.append(f", agent {t.agent}", style="green" if t.agent == "provisioned" else "yellow")
                body.append("\n")
            for cap, status in d.runtime.items():
                body.append(f"  {cap}: runtime ")
                body.append(status, style="green" if status == "validated" else "yellow")
                body.append("\n")
        body.append("\nKeys: 1-7 switch tabs, arrows move, r refreshes, Esc goes back to the package list. "
                    "Testing against IFS happens in Claude (through the toolkit server).\n", style="dim")
        self.query_one("#overview-body", Static).update(body)
        self.fill_deploy_list()

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
        self.fill_side_list("test-list", "testing", [
            (c.id, item(c.id, "current" if c.requirements and c.testing["passed"] == c.requirements
                        else "needs approval", f"{c.testing_text()} · rev {c.revision}"))
            for c in view.capabilities])
        self.show_testing(self.selected["testing"])

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
        elif event.option_list.id == "test-list":
            self.selected["testing"] = key
            self.show_testing(key)
        elif event.option_list.id == "deploy-list":
            self.selected["deploy"] = key
            self.show_deploy(key)

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

    # ── testing (validation findings with the logged calls behind them) ──

    @work(thread=True, exclusive=True, group="testing")
    def show_testing(self, cap_id: str | None) -> None:
        from ifs_agent_ops.operations.capability_store import load_capabilities
        from ifs_agent_ops.operations.evidence import active_log, build_evidence

        cap = load_capabilities(self.package).get(cap_id or "")
        if cap is None:
            return
        if self.test_log is None:
            self.test_log = active_log()
        ev = build_evidence(self.package, cap, self.test_log)
        self.app.call_from_thread(self.fill_testing, ev)

    def fill_testing(self, ev) -> None:
        """Requirements, then the findings for each (newest first), then the logged calls each finding cites.
        A call's request and response are only built when it is expanded."""
        head = Text.assemble((f"{ev.title}\n", "bold"), f"{ev.capability} rev {ev.revision}: ")
        for status in ("passed", "failed", "inconclusive", "not tested"):
            if ev.counts[status]:
                head.append(f"{ev.counts[status]} {status}  ", style=TEST_STYLE[status])
        head.append(f"\nFindings are a report: runtime-validation status stays '{ev.runtime_status}'. Claude records "
                    "findings while testing; w writes the evidence report.", style="dim")
        self.query_one("#test-head", Static).update(head)
        tree = self.query_one("#test-tree", Tree)
        tree.clear()
        tree.show_root = False
        for r in ev.requirements:
            label = Text.assemble((f"{r.number}. ", "bold"), (f"{r.status.upper():<13}", TEST_STYLE[r.status]),
                                  r.text)
            req = tree.root.add(label, expand=False, allow_expand=bool(r.findings or r.outdated))
            for f, outdated in [(f, False) for f in r.findings] + [(f, True) for f in r.outdated]:
                fl = Text.assemble((f"{f.id} ", "bold"), (f"{f.result} ", TEST_STYLE[f.result]),
                                   (f"{f.recorded_at[:10]} {f.recorded_by}, rev {f.capability_revision}"
                                    + ("  OUTDATED (earlier wording)" if outdated else ""), "dim"),
                                   "  ", f.summary)
                long_summary = len(f.summary) > 90  # tree labels are one line: show the whole text underneath too
                node = req.add(fl, expand=False, allow_expand=bool(f.test_log_refs) or long_summary)
                if long_summary:
                    for part in textwrap.wrap(f.summary, 100):
                        node.add_leaf(Text(part, style="italic"))
                for ref in f.test_log_refs:
                    call = r.calls[ref]
                    node.add(Text(call.line(), style="" if call.found else "yellow"), data={"call": call},
                             expand=False, allow_expand=call.found)

    @on(Tree.NodeExpanded, "#test-tree")
    def expand_call(self, event: Tree.NodeExpanded) -> None:
        """A logged call's request and response, built the first time it is expanded."""
        node, data = event.node, event.node.data
        if data and "call" in data and not data.get("loaded"):
            data["loaded"] = True
            for line in call_detail_lines(data["call"].entry):
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

    def action_redraft(self) -> None:
        """Re-derive the selected DRIFTED approved operation from the current recordings (back to draft)."""
        op_id = self.selected["operations"]
        if op_id not in self.drift:
            self.notify("Select a DRIFTED operation: only approved operations the recordings now differ from "
                        "can be redrafted.", severity="warning")
            return

        def go(confirmed: bool) -> None:
            if not confirmed:
                return
            from ifs_agent_ops.operations import service

            try:
                op = service.redraft(self.package, op_id)
            except (ValueError, KeyError) as exc:
                self.app.push_screen(MessageDialog("Refused", str(exc)))
                return
            self.notify(f"Redrafted {op.id}: revision {op.revision}, draft. Review and approve it again.")
            self.action_refresh()

        self.app.push_screen(ConfirmDialog(
            "Redraft a drifted operation",
            f"{op_id}\n\nThe newer recordings differ in: {', '.join(self.drift[op_id])}.\n\nRedrafting takes the "
            "definition derived from the current recordings, keeps your title, description, review notes and "
            "runtime requirements, bumps the revision and puts it back to draft without an approval. Review and "
            "approve it again; capabilities that use it need re-approval too.", "Redraft"), go)

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
        if self.tab == "testing":
            from ifs_agent_ops.operations.evidence import (
                active_log,
                build_evidence,
                write_report,
            )

            cap_id = self.selected["testing"]
            if cap_id in caps:
                ev = build_evidence(self.package, caps[cap_id], self.test_log or active_log())
                self.app.push_screen(MessageDialog("Evidence report written (local, not committed)",
                                                   str(write_report(self.package, ev))))
            return
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
        from ifs_agent_ops.generators.agent import generate as agent
        from ifs_agent_ops.generators.connector import generate as connector
        from ifs_agent_ops.generators.flow import generate as flow
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
        flows = flow(self.package, check=check)
        t.append("\nPower Automate flows (one per capability action)\n", style="bold underline")
        for r in flows.results:
            line(t, r, rel(r.path))
        for e in flows.errors:
            t.append(f"  error: {e}\n", style="red")
        ag = agent(self.package, check=check)
        t.append("\nMicrosoft 365 agent (calls the flows)\n", style="bold underline")
        for r in ag.results:
            line(t, r, rel(r.path))
        if check:
            t.append("\nOnly approved capabilities and operations generate. Press g to write these files "
                     "(nothing is committed). Then deploy them on the Deploy tab (7).\n", style="dim")
        return t

    def action_generate(self) -> None:
        def go(confirmed: bool) -> None:
            if confirmed:
                self.run_generate()

        self.app.push_screen(ConfirmDialog(
            "Generate", "Write the agent skills, the Power Platform connector, the flows and the Microsoft 365 agent "
            "into the package from the approved capabilities. Nothing is committed.", "Generate"), go)

    @work(thread=True, exclusive=True, group="generate")
    def run_generate(self) -> None:
        text = self.generation_text(check=False)
        self.app.call_from_thread(self.app.push_screen, MessageDialog("Generated", text))
        self.app.call_from_thread(self.check_generation)
        self.app.call_from_thread(self.load_overview)

    # ── side panel sessions, IFS services and metadata (Recordings tab) ──

    def action_import_session(self) -> None:
        sessions = self.view.sessions if self.view else []
        if not sessions:
            self.notify("No stopped side-panel recordings to import. Record in Edge (IFS Agent Ops side panel), "
                        "click Stop recording, then press r here.")
            return

        def confirm(session_id: str | None) -> None:
            s = next((x for x in sessions if x.id == session_id), None)
            if s is None:
                return
            self.app.push_screen(ConfirmDialog(
                "Import the recording",
                f"Import {s.id} into {self.package.name} as recordings/{s.workflow}/.\n\nIntent: {s.intent}\n\n"
                "The import removes credentials and server names and redacts business values, then writes the "
                "recording and its plan into the package. Nothing is committed: review it on this tab first.",
                "Import"), lambda ok: ok and self.run_import(s.id))

        if len(sessions) == 1:
            confirm(sessions[0].id)
        else:
            self.app.push_screen(PickDialog("Import which recording?", "Stopped recordings from the side panel:",
                                            [(s.id, Text.assemble((s.id, "bold"), "\n  ", (s.intent, "dim")))
                                             for s in sessions]), confirm)

    @work(thread=True, exclusive=True, group="import")
    def run_import(self, session_id: str) -> None:
        from ifs_agent_ops.capture import service
        from ifs_agent_ops.capture.service import CaptureError
        from ifs_agent_ops.capture.store import CaptureStore
        from ifs_agent_ops.recordings.importer import ImportRefused

        try:
            result, plan = service.import_session(CaptureStore(), session_id, self.package)
        except (CaptureError, ImportRefused) as exc:
            self.app.call_from_thread(self.app.push_screen, MessageDialog("Import refused", str(exc)))
            return
        text = Text(f"{result.recording.relative_to(self.package.root).as_posix()}\n\n", style="bold")
        text.append("plan complete\n" if plan["complete"] else "plan INCOMPLETE\n",
                    style="green" if plan["complete"] else "yellow")
        for t in plan["tasks"]:
            text.append(f"  {t['title']}: {t['status']}, {t['calls']} call(s)\n")
        for w in result.warnings:
            text.append(f"warning: {w}\n", style="yellow")
        text.append("\nNext: check the steps and calls here, then choose the IFS services (m).", style="dim")
        self.app.call_from_thread(self.app.push_screen, MessageDialog("Imported", text))
        self.app.call_from_thread(self.action_refresh)

    def action_remove_recording(self) -> None:
        """Remove a recording to record it again: an imported one nothing approved rests on, or a side-panel
        recording that was not imported (or a plan never recorded)."""
        from ifs_agent_ops.capture.store import CaptureStore
        from ifs_agent_ops.recordings.remove import plan_removal
        from ifs_agent_ops.workspace.package import PackageError

        store = CaptureStore()
        plans, items, blocked = {}, [], []
        recs = sorted(self.recs.values(), key=lambda r: r.rel != self.selected["recordings"])  # selected first
        for r in recs:
            try:
                plan = plan_removal(self.package, r.rel, store)
            except PackageError:
                continue
            if plan.allowed:
                plans[r.rel] = plan
                items.append((f"rec:{r.rel}", item(f"{r.workflow}/{r.name}", r.state,
                                                    f"in the package · {r.state}")))
            else:
                blocked.append(f"{r.workflow}/{r.name}: " + "; ".join(plan.blockers))
        sessions = {s.id: s for s in st.discardable_sessions(self.package)}
        for s in sessions.values():
            where = "side panel, not imported" if s.status == "stopped" else "side panel, plan never recorded"
            items.append((f"ses:{s.id}", item(s.id, "new", f"{where} · {s.intent}")))
        if not items:
            self.notify("Nothing to remove." + (" Approved work rests on every recording: "
                                                + " / ".join(blocked) if blocked else ""), severity="warning")
            return
        text = "Remove a recording to record the task again. Removing never touches approved work."
        if blocked:
            text += "\n\nNot listed (approved or curated work rests on them):\n" + "\n".join(f"  {b}" for b in blocked)

        def picked(key: str | None) -> None:
            if key is None:
                return
            kind, _, ident = key.partition(":")
            if kind == "rec":
                plan = plans[ident]
                body = ("This deletes from the package:\n  " + "\n  ".join(plan.describe()) + "\n\nNothing is "
                        "committed: files already committed show as deletions in the package repository. Record the "
                        "task again in the side panel, then import it (i).")
                self.app.push_screen(ConfirmDialog("Remove the recording", body, "Remove"),
                                     lambda ok: ok and self.run_remove(ident))
            else:
                s = sessions[ident]
                body = (f"{s.id}\nIntent: {s.intent}\n\nThis deletes the side-panel recording (its plan and raw "
                        "capture) from this computer. "
                        + ("It was never imported, so nothing in the package changes." if s.status == "stopped"
                           else "It was never recorded."))
                self.app.push_screen(ConfirmDialog("Discard the side-panel recording", body, "Discard"),
                                     lambda ok: ok and self.run_discard(ident))

        self.app.push_screen(PickDialog("Remove which recording?", text, items), picked)

    def run_remove(self, rel: str) -> None:
        from ifs_agent_ops.capture.store import CaptureStore
        from ifs_agent_ops.recordings.remove import RemovalRefused, remove_recording
        from ifs_agent_ops.workspace.package import PackageError

        try:
            plan = remove_recording(self.package, rel, CaptureStore())
        except (RemovalRefused, PackageError, OSError) as exc:
            self.app.push_screen(MessageDialog("Not removed", str(exc)))
            return
        self.notify(f"Removed {plan.recording}" + (f" and {len(plan.operations_deleted)} draft operation(s)"
                                                   if plan.operations_deleted else "") + ".")
        self.action_refresh()

    def run_discard(self, session_id: str) -> None:
        from ifs_agent_ops.capture.session import CaptureError
        from ifs_agent_ops.capture.store import CaptureStore
        from ifs_agent_ops.recordings.remove import discard_session

        try:
            discard_session(CaptureStore(), session_id)
        except (CaptureError, OSError) as exc:
            self.app.push_screen(MessageDialog("Not discarded", str(exc)))
            return
        self.notify(f"Discarded {session_id}.")
        self.action_refresh()

    def action_services(self) -> None:
        services = st.recorded_services(self.package)
        if not services:
            self.notify("No recordings yet: nothing to choose from.")
            return

        def chosen(names: list[str] | None) -> None:
            if names:
                self.run_services(names)

        self.app.push_screen(ServicesDialog(services), chosen)

    @work(thread=True, exclusive=True, group="services")
    def run_services(self, names: list[str]) -> None:
        import asyncio

        from ifs_agent_ops import profiles as prof
        from ifs_agent_ops.ifs.errors import AuthenticationRequired, IfsApiError
        from ifs_agent_ops.ifs.metadata.snapshot import (
            SnapshotRefused,
            fetch_snapshot,
            snapshot_dir,
        )

        added = st.add_projections(self.package, names)
        missing = [n for n in names if not (snapshot_dir(self.package, n) / "metadata.xml").exists()]
        text = Text()
        if added:
            text.append(f"Added to package.yaml: {', '.join(added)}\n", style="green")
        problems: list[str] = []
        if missing:
            from ifs_agent_ops.mcp_server.runtime import Runtime

            async def pin() -> None:
                rt = Runtime.for_profile(None)
                try:
                    for name in missing:
                        try:
                            await fetch_snapshot(rt.client, self.package, name, realm=rt.settings.realm)
                            text.append(f"Metadata pinned: {name}\n", style="green")
                        except (IfsApiError, SnapshotRefused) as exc:
                            problems.append(f"{name}: {exc}")
                finally:
                    await rt.client.aclose()

            try:
                asyncio.run(pin())
            except AuthenticationRequired:
                problems.append("Not signed in to IFS. Ask Claude to sign you in (start_oauth), then press m again.")
            except prof.ProfileError as exc:
                problems.append(str(exc))
        elif not added:
            text.append("Nothing to do: the chosen services are listed and their metadata is pinned.\n")
        for p in problems:
            text.append(f"{p}\n", style="red")
        if not problems:
            text.append("\nNext: draft operations (Operations tab, d).", style="dim")
        self.app.call_from_thread(self.app.push_screen, MessageDialog("IFS services", text))
        self.app.call_from_thread(self.action_refresh)

    # ── deploy (Deploy tab) ──

    def fill_deploy_list(self) -> None:
        from ifs_agent_ops.deploy.targets import list_targets

        names = list_targets(self.package.name)
        states = {t.name: t for t in (self.view.delivery.targets if self.view and self.view.delivery else [])}
        self.fill_side_list("deploy-list", "deploy", [
            (n, item(n, "current" if states.get(n) and states[n].flows_deployed else "needs approval",
                     (states[n].environment if n in states else "deployment target")
                     + (" · flows deployed" if states.get(n) and states[n].flows_deployed else "")))
            for n in names])
        self.show_deploy(self.selected["deploy"])

    def deploy_intro(self, name: str | None) -> Text:
        pkg = self.package.name
        t = Text()
        d = self.view.delivery if self.view else None
        if d is None:
            t.append("Deploying comes after every capability is approved and generated (Overview: next steps).\n\n",
                     style="yellow")
        if name is None:
            t.append("No deployment target yet. On the command line:\n", style="bold")
            t.append(f"  ifs-agent-ops deploy init --package {pkg} --publisher <publisher> --prefix <prefix>\n"
                     f"  ifs-agent-ops targets init --package {pkg} --target dev\n")
            t.append("Then add flow_trigger_users to the target file and press r.\n", style="dim")
            return t
        ts = next((x for x in d.targets if x.name == name), None) if d else None
        t.append(f"Target {name}", style="bold")
        if ts:
            t.append(f"  {ts.environment}\n")
            t.append("  flows: ")
            t.append("deployed\n" if ts.flows_deployed else "not deployed\n",
                     style="green" if ts.flows_deployed else "yellow")
            if ts.agent:
                t.append("  agent: ")
                t.append(f"{ts.agent}\n", style="green" if ts.agent == "provisioned" else "yellow")
        else:
            t.append("\n")
        for cap, status in (d.runtime.items() if d else []):
            t.append(f"  {cap}: runtime ")
            t.append(f"{status}\n", style="green" if status == "validated" else "yellow")
        t.append("\ns plan and status (read-only)   y deploy (asks first)   v prove the deployed path (read-only "
                 "cases)   n render the agent\n", style="dim")
        t.append(f"Cases that write to IFS: ifs-agent-ops deploy validate --package {pkg} --target {name} "
                 "--allow-writes (command line, asks first).\n", style="dim")
        return t

    def show_deploy(self, name: str | None, extra: Text | None = None) -> None:
        body = self.deploy_intro(name)
        if extra is not None:
            body.append("\n")
            body.append_text(extra)
        self.query_one("#deploy-detail", Static).update(body)

    def _deploy_context(self):
        from ifs_agent_ops.deploy.intent import load_intent
        from ifs_agent_ops.deploy.targets import load_target

        name = self.selected["deploy"]
        if name is None:
            raise ValueError("no deployment target: create one with ifs-agent-ops targets init")
        intent = load_intent(self.package)
        if intent is None:
            raise ValueError("no deploy.yaml: create one with ifs-agent-ops deploy init")
        return load_target(self.package.name, name), intent

    def deploy_errors(self):
        from ifs_agent_ops.deploy import powerplatform as ppa
        from ifs_agent_ops.deploy.intent import IntentError
        from ifs_agent_ops.deploy.targets import TargetError

        return (ValueError, TargetError, IntentError, ppa.DeployError, ppa.PacError, ppa.FlowApiError)

    def action_plan(self) -> None:
        self.show_deploy(self.selected["deploy"], Text("Asking the environment (pac)...", style="dim"))
        self.run_plan()

    @work(thread=True, exclusive=True, group="deploy")
    def run_plan(self) -> None:
        from ifs_agent_ops.deploy import powerplatform as ppa

        try:
            target, intent = self._deploy_context()
            current = ppa.plan(intent, self.package, target)
            text = Text("\n".join(ppa.describe(current)) + "\n")
            steps = ppa.checklist(current)
            if steps:
                text.append("\nManual steps (a person does these in the environment):\n", style="bold")
                for n, s in enumerate(steps, 1):
                    text.append(f"  {n}. {s}\n")
            text.append(f"\n{len(current.changes)} change(s) to deploy." + (" Press y to deploy them."
                        if current.changes else ""), style="dim")
        except self.deploy_errors() as exc:
            text = Text(f"error: {exc}", style="red")
        self.app.call_from_thread(self.show_deploy, self.selected["deploy"], text)

    def action_apply(self) -> None:
        self.show_deploy(self.selected["deploy"], Text("Planning (pac)...", style="dim"))
        self.run_apply_plan()

    @work(thread=True, exclusive=True, group="deploy")
    def run_apply_plan(self) -> None:
        from ifs_agent_ops.deploy import powerplatform as ppa

        try:
            target, intent = self._deploy_context()
            current = ppa.plan(intent, self.package, target)
        except self.deploy_errors() as exc:
            self.app.call_from_thread(self.show_deploy, self.selected["deploy"], Text(f"error: {exc}", style="red"))
            return
        described = "\n".join(ppa.describe(current))
        self.app.call_from_thread(self.show_deploy, self.selected["deploy"], Text(described))
        if not current.changes:
            self.app.call_from_thread(self.notify, "Nothing to deploy: the environment matches the package.")
            return
        self.app.call_from_thread(self.app.push_screen, ConfirmDialog(
            f"Deploy to {target.environment}",
            f"Import {len(current.changes)} change(s) into {target.environment}, turn the flows on and record their "
            f"trigger URLs in the target file.\n\n{described}", "Deploy"),
            lambda ok: ok and self.run_apply())

    @work(thread=True, exclusive=True, group="deploy")
    def run_apply(self) -> None:
        from ifs_agent_ops.deploy import powerplatform as ppa

        self.app.call_from_thread(self.show_deploy, self.selected["deploy"], Text("Deploying...", style="dim"))
        try:
            target, intent = self._deploy_context()
            result = ppa.apply(intent, self.package, target)
            text = Text(f"Imported: {', '.join(result.imported) or '-'}\n", style="green")
            if result.turned_on:
                text.append(f"Turned on: {', '.join(result.turned_on)}\n")
            if result.triggers_recorded:
                text.append(f"Trigger URLs recorded in {target.path}\n")
            text.append("Verified: " + ppa.describe(result.after)[-1] + "\n")
            steps = ppa.checklist(result.after)
            if steps:
                text.append("\nManual steps (a person does these in the environment):\n", style="bold")
                for n, s in enumerate(steps, 1):
                    text.append(f"  {n}. {s}\n")
        except self.deploy_errors() as exc:
            text = Text(f"error: {exc}", style="red")
        self.app.call_from_thread(self.load_overview)
        self.app.call_from_thread(self.show_deploy, self.selected["deploy"], text)

    def action_validate(self) -> None:
        self.app.push_screen(ConfirmDialog(
            "Prove the deployed path",
            "Run deployed-path validation: static and platform checks (this uploads the solution file to "
            "Microsoft's Power Apps checker), then the runtime cases that only read from IFS, through the deployed "
            "flows. The capability's runtime status is recorded in its file, for you to review and commit.\n\n"
            "Cases that write to IFS are not run from here.", "Validate"),
            lambda ok: ok and self.run_validate())

    @work(thread=True, exclusive=True, group="deploy")
    def run_validate(self) -> None:
        from ifs_agent_ops.deploy import validate as dv

        self.app.call_from_thread(self.show_deploy, self.selected["deploy"],
                                  Text("Validating (this can take a few minutes)...", style="dim"))
        try:
            target, intent = self._deploy_context()
            run = dv.validate(intent, self.package, target, solution_check=True, record=True, allow_writes=False)
            text = Text("\n".join(dv.describe(run)) + f"\n\nRun record (local): {run.path}\n")
        except self.deploy_errors() as exc:
            text = Text(f"error: {exc}", style="red")
        self.app.call_from_thread(self.load_overview)
        self.app.call_from_thread(self.show_deploy, self.selected["deploy"], text)

    def action_render_agent(self) -> None:
        self.run_render()

    @work(thread=True, exclusive=True, group="deploy")
    def run_render(self) -> None:
        from ifs_agent_ops.deploy.agent import AgentError, render

        try:
            target, _ = self._deploy_context()
            out = render(self.package, target)
            text = Text(f"Rendered the agent for {target.name} in {out.folder}\n", style="green")
            text.append("\nNext, in a terminal (signs in to Microsoft 365" + ("" if out.provisioned else
                        "; asks for the Entra app's client id and secret") + "):\n", style="bold")
            text.append(f'  cd "{out.folder}"\n  atk provision --env {target.name}\n')
        except (AgentError, *self.deploy_errors()) as exc:
            text = Text(f"error: {exc}", style="red")
        self.app.call_from_thread(self.load_overview)
        self.app.call_from_thread(self.show_deploy, self.selected["deploy"], text)


class OperationDetail(Screen[str | None]):
    """One operation's full review, full screen. ← → step through the list in its order; Esc returns to it."""
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Back to list"),
        Binding("left", "step(-1)", "Previous", priority=True),
        Binding("right", "step(1)", "Next", priority=True),
        Binding("a,A", "approve", "Approve"),
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
        Binding("q,Q", "quit", "Quit"),
        Binding("p,P", "command_palette", "Palette", show=False, tooltip="Open the command palette"),
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
    MessageDialog, ConfirmDialog, ApprovalDialog, PickDialog, NewPackageDialog { align: center middle; }
    NewPackageDialog Input { margin-bottom: 1; }
    .dialog-title { text-style: bold; margin-bottom: 1; }
    .dialog-text { margin-bottom: 1; }
    .dialog-body { height: auto; max-height: 20; margin-bottom: 1; }
    .dialog-buttons { height: auto; margin-top: 1; }
    .dialog-buttons Button { margin-right: 2; }
    #code { margin: 1 0; }
    """

    def on_mount(self) -> None:
        self.push_screen(PackagePicker())
