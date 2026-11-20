"""Persist operations/workflows in a package and merge re-drafts without breaking identity.

Rules:
- ``id`` is derived (projection + kind + target) and therefore stable across re-drafts.
- ``connector_operation_id`` is assigned on first draft and preserved forever.
- Human-edited text (``title``, ``description``) is preserved.
- A *draft* is updated in place; its ``revision`` increments only when derived content changes.
- An *approved* operation/workflow is never rewritten by drafting; a change is reported as drift.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ifs_agent_ops.operations.model import Approval, Operation, Workflow
from ifs_agent_ops.operations.yaml_persist import persist_approval
from ifs_agent_ops.workspace.package import Package

MAX_CONNECTOR_OPERATION_ID = 300  # Power Platform limit (roadmap requirement C5)


def ops_dir(package: Package) -> Path:
    return package.root / "operations"


def wfs_dir(package: Package) -> Path:
    return package.root / "workflows"


def _dump(path: Path, model) -> None:
    data = model.model_dump(mode="json", exclude_none=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120),
                    encoding="utf-8", newline="\n")


def load_operations(package: Package) -> dict[str, Operation]:
    out = {}
    for p in sorted(ops_dir(package).glob("*.yaml")) if ops_dir(package).is_dir() else []:
        op = Operation.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
        out[op.id] = op
    return out


def load_workflows(package: Package) -> dict[str, Workflow]:
    out = {}
    for p in sorted(wfs_dir(package).glob("*.yaml")) if wfs_dir(package).is_dir() else []:
        wf = Workflow.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
        out[wf.id] = wf
    return out


def save_operation(package: Package, op: Operation) -> Path:
    ops_dir(package).mkdir(parents=True, exist_ok=True)
    path = ops_dir(package) / f"{op.id}.yaml"
    _dump(path, op)
    return path


def save_workflow(package: Package, wf: Workflow) -> Path:
    wfs_dir(package).mkdir(parents=True, exist_ok=True)
    path = wfs_dir(package) / f"{wf.id}.yaml"
    _dump(path, wf)
    return path


def new_connector_operation_id(op: Operation, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", f"{op.kind}_{op.target}")
    if len(base) > MAX_CONNECTOR_OPERATION_ID:
        digest = hashlib.sha1(base.encode()).hexdigest()[:8]
        base = f"{base[:MAX_CONNECTOR_OPERATION_ID - 9]}_{digest}"
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base[:MAX_CONNECTOR_OPERATION_ID - len(str(n)) - 1]}_{n}"
        n += 1
    return candidate


@dataclass
class MergeReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    drifted: list[str] = field(default_factory=list)       # approved, but the recordings now say otherwise
    not_in_recordings: list[str] = field(default_factory=list)


def merge_draft(package: Package, drafted_ops: dict[str, Operation], drafted_wfs: dict[str, Workflow]) -> MergeReport:
    report = MergeReport()
    existing = load_operations(package)
    taken = {op.connector_operation_id for op in existing.values()}
    for op_id, new in sorted(drafted_ops.items()):
        old = existing.get(op_id)
        if old is None:
            new.connector_operation_id = new_connector_operation_id(new, taken)
            taken.add(new.connector_operation_id)
            save_operation(package, new)
            report.created.append(op_id)
            continue
        if old.status == "approved":
            (report.drifted if old.derived() != new.derived() else report.unchanged).append(op_id)
            continue
        merged = new.model_copy(update={
            "connector_operation_id": old.connector_operation_id, "title": old.title,
            "description": old.description, "status": old.status, "approval": old.approval,
            "review_notes": old.review_notes, "runtime_validation": old.runtime_validation,
            "revision": old.revision + (1 if old.derived() != new.derived() else 0),
        })
        if merged.model_dump() == old.model_dump():
            report.unchanged.append(op_id)
        else:
            save_operation(package, merged)
            report.updated.append(op_id)
    report.not_in_recordings = sorted(set(existing) - set(drafted_ops))

    existing_wfs = load_workflows(package)
    for wf_id, new in sorted(drafted_wfs.items()):
        old = existing_wfs.get(wf_id)
        if old is not None and old.status == "approved":
            if old.steps != new.steps:
                report.drifted.append(f"workflow:{wf_id}")
            continue
        if old is not None:
            new = new.model_copy(update={"title": old.title, "description": old.description, "status": old.status})
            if new.model_dump() == old.model_dump():
                report.unchanged.append(f"workflow:{wf_id}")
                continue
        save_workflow(package, new)
        (report.created if old is None else report.updated).append(f"workflow:{wf_id}")
    return report


def approve(package: Package, op_id: str, reviewer: str | None = None) -> Operation:
    """Record technical approval. The CLI only calls this after an interactive human confirmation."""
    ops = load_operations(package)
    if op_id not in ops:
        raise KeyError(f"No operation {op_id!r} in {package.name}")
    op = ops[op_id]
    op = op.model_copy(update={"status": "approved", "approval": Approval(
        approved_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), revision=op.revision,
        approved_by=reviewer, content_sha256=op.technical_hash())})
    path = ops_dir(package) / f"{op.id}.yaml"
    if path.exists():  # small diff: only status and approval change
        persist_approval(path, op, lambda: save_operation(package, op))
    else:
        save_operation(package, op)
    return op
