"""Remove a recording that nothing approved depends on, so the task can be recorded again.

A builder often wants to redo a recording: a step was done the long way, a field was forgotten, a second line
was skipped. Removing it takes out everything drafted only from it:

- the sanitized recording and its annotations (``recordings/<workflow>/<task>.har`` and ``.annotations.yaml``)
- the observed workflow drafted from it (``workflows/<id>.yaml``)
- draft operations whose only evidence is this recording; draft operations that other recordings also show
  keep their file and lose only this recording's evidence
- the side-panel capture session(s) it was imported from, which hold the raw capture (local, never committed)

It is refused while anything approved or curated rests on the recording: an approved operation or workflow that
cites it, or a capability that lists its workflow as evidence or uses an operation that would be deleted.
Approvals are the human gate, so they are never undone here; a recording behind them is replaced by recording
again, importing and redrafting. Nothing is committed: removed files that were committed show as deletions in
the package repository.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ifs_agent_ops.capture.session import CaptureError
from ifs_agent_ops.capture.store import CaptureStore
from ifs_agent_ops.operations.capability_store import load_capabilities
from ifs_agent_ops.operations.store import (
    load_operations,
    load_workflows,
    ops_dir,
    save_operation,
    wfs_dir,
)
from ifs_agent_ops.recordings.annotations import annotations_path
from ifs_agent_ops.workspace.package import Package, PackageError


class RemovalRefused(ValueError):
    pass


@dataclass
class RemovalPlan:
    recording: str                                               # package-relative, forward slashes
    files: list[Path] = field(default_factory=list)              # recording and annotations
    workflows: list[str] = field(default_factory=list)           # observed workflows to delete
    operations_deleted: list[str] = field(default_factory=list)  # draft operations with no other evidence
    operations_trimmed: list[str] = field(default_factory=list)  # draft operations other recordings also show
    sessions: list[str] = field(default_factory=list)            # capture sessions it was imported from
    blockers: list[str] = field(default_factory=list)            # why it cannot be removed

    @property
    def allowed(self) -> bool:
        return not self.blockers

    def describe(self) -> list[str]:
        lines = [f"recording: {self.recording}"]
        if self.workflows:
            lines.append(f"observed workflow: {', '.join(self.workflows)}")
        if self.operations_deleted:
            lines.append(f"{len(self.operations_deleted)} draft operation(s) only this recording shows: "
                         + ", ".join(self.operations_deleted))
        if self.operations_trimmed:
            lines.append(f"{len(self.operations_trimmed)} draft operation(s) other recordings also show keep their "
                         "file and lose this recording's evidence")
        if self.sessions:
            lines.append(f"side-panel recording (raw capture, local): {', '.join(self.sessions)}")
        return lines


def _rel(package: Package, recording: str | Path) -> str:
    path = Path(recording)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(package.root.resolve())
        except ValueError as exc:
            raise PackageError(f"{recording} is not inside the package {package.name}") from exc
    rel = path.as_posix()
    if not rel.startswith("recordings/") or not rel.endswith(".har") or ".." in Path(rel).parts:
        raise PackageError(f"{recording} is not a package recording (recordings/<workflow>/<task>.har)")
    return rel


def _sessions(store: CaptureStore | None, rel: str) -> list[str]:
    if store is None:
        return []
    try:
        return [s.id for s in store.list() if rel in s.imports and s.status != "recording"]
    except OSError:
        return []


def plan_removal(package: Package, recording: str | Path, store: CaptureStore | None = None) -> RemovalPlan:
    """What removing the recording would delete, and what (if anything) stops it. Read-only."""
    rel = _rel(package, recording)
    har = package.root / rel
    if not har.exists():
        raise PackageError(f"No recording {rel} in {package.name}")
    plan = RemovalPlan(rel, files=[p for p in (har, annotations_path(har)) if p.exists()],
                       sessions=_sessions(store, rel))

    workflows = {wid: wf for wid, wf in load_workflows(package).items() if wf.recording.replace("\\", "/") == rel}
    plan.workflows = sorted(workflows)
    for wid, wf in sorted(workflows.items()):
        if wf.status == "approved":
            plan.blockers.append(f"the observed workflow {wid} drafted from it is approved")

    for op_id, op in sorted(load_operations(package).items()):
        cited = [e for e in op.evidence if e.recording.replace("\\", "/") == rel]
        if not cited:
            continue
        if op.status == "approved":
            plan.blockers.append(f"the approved operation {op_id} cites it as evidence")
        elif len(cited) == len(op.evidence):
            plan.operations_deleted.append(op_id)
        else:
            plan.operations_trimmed.append(op_id)

    wf_files = {f"workflows/{wid}.yaml" for wid in workflows}
    deleted = set(plan.operations_deleted)
    for cap in load_capabilities(package).values():
        if wf_files & {e.replace("\\", "/") for e in cap.evidence}:
            plan.blockers.append(f"the capability {cap.id} lists its observed workflow as evidence")
        used = sorted(deleted & {s.operation for s in cap.steps})
        if used:
            plan.blockers.append(f"the capability {cap.id} uses {', '.join(used)}, which only this recording shows")
    return plan


def remove_recording(package: Package, recording: str | Path, store: CaptureStore | None = None,
                     discard_sessions: bool = True) -> RemovalPlan:
    """Remove the recording and what was drafted only from it (see the module docstring). Refused when anything
    approved or curated rests on it."""
    plan = plan_removal(package, recording, store)
    if plan.blockers:
        raise RemovalRefused(f"{plan.recording} cannot be removed: " + "; ".join(plan.blockers) + ".")
    ops = load_operations(package)
    for op_id in plan.operations_trimmed:
        op = ops[op_id]
        save_operation(package, op.model_copy(update={"evidence": [
            e for e in op.evidence if e.recording.replace("\\", "/") != plan.recording]}))
    for op_id in plan.operations_deleted:
        (ops_dir(package) / f"{op_id}.yaml").unlink(missing_ok=True)
    for wid in plan.workflows:
        (wfs_dir(package) / f"{wid}.yaml").unlink(missing_ok=True)
    for path in plan.files:
        path.unlink(missing_ok=True)
    folder = (package.root / plan.recording).parent
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
    if discard_sessions and store is not None:
        for sid in plan.sessions:
            try:
                discard_session(store, sid)
            except CaptureError:
                pass  # a session that is recording again, or already gone, stays as it is
    else:
        plan.sessions = []
    return plan


def discard_session(store: CaptureStore, session_id: str) -> None:
    """Delete a side-panel capture session (plan and raw capture). Refused while it is recording."""
    session = store.load(session_id)
    if session.status == "recording":
        raise CaptureError(f"{session_id} is still recording: stop it in the side panel first")
    folder = store.dir(session_id)
    with store.lock(session_id):
        for child in folder.iterdir():
            if child.name != ".lock":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.rmtree(folder, ignore_errors=True)
