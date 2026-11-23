"""What the TUI shows about a package: recordings, approval state, delivery state and the next steps. No UI here.

The next steps follow the builder's path end to end: plan and record in the browser side panel, import, choose
the IFS services and pin their metadata, draft, work the recording into a capability with Claude and test it
against IFS TEST, approve (operations, then the capability), generate, deploy, prove the deployed path, publish
the agent. Everything here is read-only except ``add_projections``, which the TUI calls after the user confirms.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ifs_agent_ops.generators.gating import GenerationRefused, gate_capability
from ifs_agent_ops.operations.capability_store import load_capabilities
from ifs_agent_ops.operations.findings import load_findings, requirement_status
from ifs_agent_ops.operations.store import load_operations, load_workflows
from ifs_agent_ops.recordings.annotations import load_plan
from ifs_agent_ops.workspace.package import Package


def git_changes(root: Path) -> dict[str, str] | None:
    """Uncommitted changes in the package repository: {path relative to the root: porcelain code}.
    None when the package is not a git repository (or git is unavailable)."""
    try:
        out = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                             capture_output=True, text=True, timeout=20, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    changes = {}
    for line in out.splitlines():
        if len(line) > 3:
            path = line[3:].split(" -> ")[-1].strip('"')
            changes[path] = line[:2]
    return changes


@dataclass
class RecordingInfo:
    path: Path
    rel: str                     # relative to the package root, with forward slashes
    workflow: str                # recordings/<workflow>/...
    name: str
    committed: bool | None       # None: not a git repository
    drafted: bool                # a drafted workflow references it
    plan: dict | None
    in_capability: bool = False  # a capability lists its drafted workflow as evidence

    @property
    def state(self) -> str:
        if self.committed is False:
            return "new"
        return "drafted" if self.drafted else "not drafted"


def recordings(package: Package, changes: dict[str, str] | None = None) -> list[RecordingInfo]:
    workflow_files = {wf.recording.replace("\\", "/"): f"workflows/{wf.id}.yaml"
                      for wf in load_workflows(package).values()}
    evidence = {e.replace("\\", "/") for cap in load_capabilities(package).values() for e in cap.evidence}
    out = []
    for har in sorted(package.recordings_dir.glob("*/*.har")):
        rel = har.relative_to(package.root).as_posix()
        out.append(RecordingInfo(
            path=har, rel=rel, workflow=har.parent.name, name=har.stem,
            committed=None if changes is None else rel not in changes,
            drafted=rel in workflow_files, plan=load_plan(har),
            in_capability=workflow_files.get(rel) in evidence))
    return out


def drifted_operations(package: Package) -> dict[str, list[str]]:
    """Approved operations whose drafting from the current recordings differs from what was approved:
    {operation id: the parts that differ}. Read-only: drafting runs in memory and nothing is written (the
    approved definitions stay as approved until a human re-reviews them)."""
    from ifs_agent_ops.operations.draft import draft_package
    from ifs_agent_ops.operations.service import projections_with_snapshots

    try:
        drafted = draft_package(package, projections_with_snapshots(package)).operations
    except FileNotFoundError:  # a metadata snapshot is missing; drafting reports that itself
        return {}
    out = {}
    for op_id, op in load_operations(package).items():
        new = drafted.get(op_id)
        if op.status == "approved" and new is not None:
            old_d, new_d = op.derived(), new.derived()
            if old_d != new_d:
                out[op_id] = sorted(k for k in old_d if old_d[k] != new_d.get(k))
    return out


@dataclass
class CapabilityState:
    id: str
    title: str
    status: str
    revision: int
    approval_current: bool
    reason: str                       # why it does not generate, when it does not
    operations: list[str]             # referenced operation ids
    unapproved_operations: list[str]  # referenced operations without a current technical approval
    open_questions: int
    testing: Counter = field(default_factory=Counter)  # requirement status -> count (validation findings)

    @property
    def requirements(self) -> int:
        return sum(self.testing.values())

    def testing_text(self) -> str:
        order = ("passed", "failed", "inconclusive", "not tested")
        return ", ".join(f"{self.testing[s]} {s}" for s in order if self.testing[s]) or "no requirements"


def _operation_current(op) -> bool:
    return bool(op.status == "approved" and op.approval and op.approval.revision == op.revision
                and op.approval.content_sha256 in (None, op.technical_hash()))


def capabilities(package: Package) -> list[CapabilityState]:
    ops = load_operations(package)
    out = []
    for cap in load_capabilities(package).values():
        try:
            gate_capability(cap, ops)
            current, reason = True, ""
        except GenerationRefused as exc:
            current, reason = False, str(exc)
        referenced = list(dict.fromkeys(s.operation for s in cap.steps))
        out.append(CapabilityState(
            id=cap.id, title=cap.title, status=cap.status, revision=cap.revision, approval_current=current,
            reason=reason, operations=referenced,
            unapproved_operations=[o for o in referenced if o not in ops or not _operation_current(ops[o])],
            open_questions=len(cap.review_notes),
            testing=Counter(r.status for r in requirement_status(cap, load_findings(package, cap.id)))))
    return out


# ── before review: capture sessions, IFS services, metadata ────────────────

WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")


@dataclass
class ServiceUse:
    """An IFS service (projection) the package's recordings call."""
    name: str
    calls: int
    writes: int
    listed: bool          # in package.yaml projections
    snapshot: bool        # metadata pinned in the package


def recorded_services(package: Package) -> list[ServiceUse]:
    """Every IFS service the recordings call, busiest writers first. The IFS web client calls many services
    (navigation, settings, lookups); the ones with writes are usually the ones a capability is about."""
    from ifs_agent_ops.analysis.odata import parse_projection_url
    from ifs_agent_ops.ifs.metadata.snapshot import snapshot_dir
    from ifs_agent_ops.recordings.har import load_har

    calls: Counter = Counter()
    writes: Counter = Counter()
    for har in sorted(package.recordings_dir.glob("*/*.har")) if package.recordings_dir.is_dir() else []:
        for call in load_har(har):
            target = parse_projection_url(call.url)
            if target:
                calls[target.service] += 1
                if call.method.upper() in WRITE_METHODS:
                    writes[target.service] += 1
    listed = set(package.projections)
    return sorted((ServiceUse(s, calls[s], writes[s], s in listed,
                              (snapshot_dir(package, s) / "metadata.xml").exists()) for s in calls),
                  key=lambda u: (-u.writes, -u.calls, u.name))


def add_projections(package: Package, names: list[str]) -> list[str]:
    """Add services to package.yaml ``projections`` (never removes any). Returns the ones added."""
    import yaml

    from ifs_agent_ops.workspace.package import MANIFEST

    path = package.root / MANIFEST
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    current = list(data.get("projections") or [])
    added = [n for n in dict.fromkeys(names) if n not in current]
    if added:
        data["projections"] = current + added
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")
        package.projections[:] = data["projections"]
    return added


def pending_sessions(package: Package) -> list:
    """Stopped capture sessions that can be imported into this package: meant for it (or for no package, as the
    side panel creates them), with a workflow folder, and not imported here yet."""
    from ifs_agent_ops.capture.store import CaptureStore

    try:
        sessions = CaptureStore().list()
    except OSError:
        return []
    names = {package.name, str(package.root)}
    out = []
    for s in sessions:
        if s.status != "stopped" or (s.package and s.package not in names):
            continue
        if any((package.root / rel).exists() for rel in s.imports):
            continue
        out.append(s)
    return out


def discardable_sessions(package: Package) -> list:
    """Side-panel sessions the builder can throw away from this package's view: stopped recordings not imported
    here (``pending_sessions``) and plans never started, meant for this package or for none."""
    from ifs_agent_ops.capture.store import CaptureStore

    try:
        sessions = CaptureStore().list()
    except OSError:
        return []
    names = {package.name, str(package.root)}
    planned = [s for s in sessions if s.status == "planning" and s.started_at is None
               and (not s.package or s.package in names)]
    return pending_sessions(package) + planned


# ── after approval: generation and delivery ─────────────────────────────────

@dataclass
class TargetState:
    name: str
    environment: str = ""
    flows_deployed: bool = False     # deploy apply recorded the flows' trigger URLs
    agent: str = ""                  # "" (no agent generated) | not rendered | rendered | provisioned
    agent_folder: str = ""
    error: str = ""


@dataclass
class Delivery:
    generated: dict[str, bool] = field(default_factory=dict)   # skills/connector/flows/agent -> up to date
    deploy_file: bool = False
    deploy_error: str = ""
    flows_in_deploy: bool = False
    targets: list[TargetState] = field(default_factory=list)
    runtime: dict[str, str] = field(default_factory=dict)     # capability -> runtime_validation.status
    runtime_cases: dict[str, bool] = field(default_factory=dict)  # capability -> validation/<id>.runtime.yaml exists


def _stale(results) -> bool:
    return any(r.action.startswith("would-") for r in results)


def generation_state(package: Package) -> dict[str, bool]:
    """Whether each generated artifact matches the approved capabilities (check mode: nothing is written)."""
    from ifs_agent_ops.generators.agent import generate as agent
    from ifs_agent_ops.generators.connector import generate as connector
    from ifs_agent_ops.generators.flow import generate as flow
    from ifs_agent_ops.generators.skills import generate as skills

    return {"skills": not _stale(skills(package, check=True)),
            "connector": not _stale(connector(package, check=True).results),
            "flows": not _stale(flow(package, check=True).results),
            "agent": not _stale(agent(package, check=True).results)}


def delivery(package: Package, caps: list[CapabilityState]) -> Delivery:
    """Generation, deployment intent, targets, deployed-path status and the agent. Local files only: no pac,
    no network (the Deploy tab runs the live checks on request)."""
    from ifs_agent_ops.deploy.agent import _read_env, work_dir
    from ifs_agent_ops.deploy.intent import IntentError, load_intent
    from ifs_agent_ops.deploy.targets import TargetError, list_targets, load_target
    from ifs_agent_ops.generators.agent import REGISTRATION_VAR, REPORT_FILE, agent_root

    d = Delivery(generated=generation_state(package))
    try:
        intent = load_intent(package)
        d.deploy_file = intent is not None
        d.flows_in_deploy = bool(intent and intent.power_platform and intent.power_platform.flows)
    except IntentError as exc:
        d.deploy_file, d.deploy_error = True, str(exc)
    loaded = load_capabilities(package)
    for c in caps:
        if c.approval_current and c.id in loaded:
            d.runtime[c.id] = loaded[c.id].runtime_validation.status
            d.runtime_cases[c.id] = (package.root / "validation" / f"{c.id}.runtime.yaml").exists()
    has_agent = (agent_root(package) / REPORT_FILE).exists()
    for name in list_targets(package.name):
        try:
            t = load_target(package.name, name)
        except TargetError as exc:
            d.targets.append(TargetState(name, error=str(exc)))
            continue
        ts = TargetState(name, t.environment, flows_deployed=bool(t.flow_triggers))
        if has_agent:
            folder = work_dir(package, t)
            env = _read_env(folder / "env" / f".env.{name}")
            ts.agent_folder = str(folder)
            ts.agent = ("not rendered" if not env else "provisioned"
                        if env.get("TEAMS_APP_ID") and env.get(REGISTRATION_VAR) else "rendered")
        d.targets.append(ts)
    return d


@dataclass
class Overview:
    package: Package
    git: bool
    uncommitted: list[str]
    recordings: list[RecordingInfo]
    operations: Counter
    capabilities: list[CapabilityState]
    drifted: dict[str, list[str]] = field(default_factory=dict)
    sessions: list = field(default_factory=list)              # stopped capture sessions to import
    services: list[ServiceUse] = field(default_factory=list)
    delivery: Delivery | None = None                          # once every capability is approved
    next_steps: list[str] = field(default_factory=list)


def overview(package: Package) -> Overview:
    changes = git_changes(package.root)
    recs = recordings(package, changes)
    ops = load_operations(package)
    caps = capabilities(package)
    view = Overview(package=package, git=changes is not None, uncommitted=sorted(changes or {}),
                    recordings=recs, operations=Counter(op.status for op in ops.values()), capabilities=caps,
                    drifted=drifted_operations(package), sessions=pending_sessions(package),
                    services=recorded_services(package))
    if caps and all(c.approval_current for c in caps):
        view.delivery = delivery(package, caps)
    view.next_steps = next_steps(view)
    return view


def services_needing_attention(view: Overview) -> tuple[list[ServiceUse], list[ServiceUse]]:
    """(services to choose from while package.yaml lists none, listed services without pinned metadata).

    Only a package with no projections is asked to choose: the web client also writes to its own services
    (bookmarks, user settings), so once the builder has chosen, unlisted services are left alone."""
    unlisted = [] if view.package.projections else view.services[:3]
    return unlisted, [s for s in view.services if s.listed and not s.snapshot]


def next_steps(view: Overview) -> list[str]:
    """The builder's path, in order: what to do now, and where (TUI key, side panel, Claude or a command)."""
    steps = []
    if view.sessions:
        steps.append(f"Import {len(view.sessions)} stopped recording(s) from the side panel: Recordings tab, "
                     "press i.")
    elif not view.recordings:
        steps.append("Plan and record the task: in Edge, open the IFS Agent Ops side panel and fill in Create plan "
                     "(or tell Claude your intent), then Start recording, do each task in IFS, and Stop recording.")
    new = [r for r in view.recordings if r.committed is False]
    if new:
        steps.append(f"Review {len(new)} new recording(s) on the Recordings tab, then commit them in the "
                     "package repository.")
    unlisted, unpinned = services_needing_attention(view)
    if unlisted or unpinned:
        names = ", ".join(s.name for s in (unlisted or unpinned)[:3])
        steps.append(f"Choose the IFS services this package uses and pin their metadata ({names}): Recordings "
                     "tab, press m (needs the IFS sign-in).")
    undrafted = [r for r in view.recordings if not r.drafted]
    if undrafted:
        steps.append(f"Draft operations from {len(undrafted)} recording(s): Operations tab, press d.")
    # Approval comes after testing (development loop): work new evidence into a capability with Claude and try
    # the flow against IFS TEST first. Approving is never the step right after recording.
    untested = [r for r in view.recordings if r.drafted and not r.in_capability]
    if view.capabilities and untested:
        steps.append(f"Work {len(untested)} recording(s) into a capability with Claude and test the flow against "
                     "IFS TEST. Approve only after that.")
    if view.drifted:
        steps.append(f"{len(view.drifted)} approved operation(s) differ from the newer recordings (DRIFTED on the "
                     "Operations tab). Drafting never changes an approved operation, so the approved definitions "
                     "still apply; decide with Claude, while testing, whether the new evidence should change them.")
    for cap in view.capabilities:
        if cap.testing["failed"]:
            steps.append(f"{cap.id}: {cap.testing['failed']} requirement(s) FAILED in testing. Work the findings "
                         "into the capability with Claude and test again (Testing tab).")
        elif cap.testing["not tested"] or cap.testing["inconclusive"]:
            done = cap.testing["passed"]
            steps.append(f"Test {cap.id} with Claude against IFS TEST: {done} of {cap.requirements} requirement(s) "
                         "passed so far (Testing tab).")
    if not view.capabilities:
        steps.append("Write a capability (capabilities/<id>.yaml) with Claude's help, and test it against IFS TEST "
                     "before approving. It references operations and states where every value comes from.")
    for cap in view.capabilities:
        if cap.unapproved_operations:
            steps.append(f"When testing is done: approve the {len(cap.unapproved_operations)} operation(s) {cap.id} "
                         "uses without a current approval (Capabilities tab, press o).")
        elif not cap.approval_current:
            steps.append(f"When testing is done: approve capability {cap.id} (Capabilities tab, press a).")
    if view.delivery is not None:
        steps += delivery_steps(view.package.name, view.delivery)
    if view.git and view.uncommitted and not new:
        steps.append(f"Commit the {len(view.uncommitted)} changed file(s) in the package repository.")
    if not steps:
        steps.append("Done: generated, deployed, validated and the agent is provisioned. Try it in Copilot.")
    return steps


def delivery_steps(pkg: str, d: Delivery) -> list[str]:
    """After approval: generate, deploy, prove the deployed path, publish the agent. One step at a time: each
    stage waits for the one before it."""
    stale = [k for k, ok in d.generated.items() if not ok]
    if stale:
        return [f"Generate the {', '.join(stale)} from the approved capabilities: Generate tab, press g."]
    if not d.deploy_file:
        return [(f"Describe the deployment (once per package), on the command line: ifs-agent-ops deploy init "
                f"--package {pkg} --publisher <publisher> --prefix <prefix>, then list the flows in deploy.yaml.")]
    if d.deploy_error:
        return [f"Fix deploy.yaml: {d.deploy_error}"]
    if not d.flows_in_deploy:
        return [(f"List the generated flows in deploy.yaml under power_platform.flows (power-platform/flows/<capability>), "
                f"then check it: ifs-agent-ops deploy check --package {pkg}.")]
    if not d.targets:
        return [(f"Create a deployment target, on the command line: ifs-agent-ops targets init --package {pkg} "
                "--target dev (picks the environment), then add flow_trigger_users to the target file.")]
    steps = []
    for t in d.targets:
        if t.error:
            steps.append(f"Fix target {t.name}: {t.error}")
        elif not t.flows_deployed:
            steps.append(f"Deploy to {t.name} ({t.environment}): Deploy tab, press s for the plan, then y to deploy. "
                         "Then create the connection the plan lists as a manual step.")
    if steps:
        return steps
    for cap, status in d.runtime.items():
        if status != "validated":
            if not d.runtime_cases.get(cap):
                steps.append(f"Ask Claude to write validation/{cap}.runtime.yaml (runtime cases for the deployed "
                             "flows), then prove the deployed path on the Deploy tab (v).")
            else:
                steps.append(f"Prove the deployed path for {cap} (runtime status: {status}): Deploy tab, press v. "
                             f"Cases that write to IFS run only from the command line: ifs-agent-ops deploy validate "
                             f"--package {pkg} --target <target> --allow-writes.")
    if steps:
        return steps
    for t in d.targets:
        if t.agent == "not rendered":
            steps.append(f"Render the Copilot agent for {t.name}: Deploy tab, press n.")
        elif t.agent == "rendered":
            steps.append(f'Publish the agent to Microsoft 365, in a terminal: cd "{t.agent_folder}" ; '
                         f"atk provision --env {t.name}")
    return steps
