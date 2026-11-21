"""What the review TUI shows about a package: recordings, approval state and the next steps. No UI here.

Everything is read from the package and its git working tree; nothing is written.
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


@dataclass
class Overview:
    package: Package
    git: bool
    uncommitted: list[str]
    recordings: list[RecordingInfo]
    operations: Counter
    capabilities: list[CapabilityState]
    drifted: dict[str, list[str]] = field(default_factory=dict)
    next_steps: list[str] = field(default_factory=list)


def overview(package: Package) -> Overview:
    changes = git_changes(package.root)
    recs = recordings(package, changes)
    ops = load_operations(package)
    caps = capabilities(package)
    view = Overview(package=package, git=changes is not None, uncommitted=sorted(changes or {}),
                    recordings=recs, operations=Counter(op.status for op in ops.values()), capabilities=caps,
                    drifted=drifted_operations(package))
    view.next_steps = next_steps(view)
    return view


def next_steps(view: Overview) -> list[str]:
    """The review path, in order: what to do now, and where in the TUI."""
    steps = []
    new = [r for r in view.recordings if r.committed is False]
    if new:
        steps.append(f"Review {len(new)} new recording(s) on the Recordings tab, then commit them in the "
                     "package repository.")
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
    if view.git and view.uncommitted and not new:
        steps.append(f"Commit the {len(view.uncommitted)} changed file(s) in the package repository.")
    if not steps:
        steps.append("Everything is approved. Generate the skills and the connector on the Generate tab.")
    return steps
