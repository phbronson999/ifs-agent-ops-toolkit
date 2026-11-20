"""Load/save capabilities and record human approvals.

Approval is a human act. The CLI only approves from an interactive terminal after the reviewer
types the capability id and their name (see ``cli.main``); there is no MCP tool for approval.
An approval pins the reviewed content (hash) and the revision of every referenced operation, so
any later change invalidates it (``changed-since-approval``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from ifs_agent_ops.operations.capability import Capability, CapabilityApproval
from ifs_agent_ops.operations.capability_validate import validate_capability
from ifs_agent_ops.operations.store import load_operations
from ifs_agent_ops.workspace.package import Package


def caps_dir(package: Package) -> Path:
    return package.root / "capabilities"


def load_capabilities(package: Package) -> dict[str, Capability]:
    out = {}
    folder = caps_dir(package)
    for p in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
        cap = Capability.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
        if p.stem != cap.id:
            raise ValueError(f"{p.name}: file name must match capability id {cap.id!r}")
        out[cap.id] = cap
    return out


def save_capability(package: Package, cap: Capability) -> Path:
    caps_dir(package).mkdir(parents=True, exist_ok=True)
    path = caps_dir(package) / f"{cap.id}.yaml"
    data = cap.model_dump(mode="json", exclude_unset=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120),
                    encoding="utf-8", newline="\n")
    return path


class ApprovalRefused(RuntimeError):
    pass


def approve_capability(package: Package, cap_id: str, reviewer: str) -> Capability:
    caps = load_capabilities(package)
    if cap_id not in caps:
        raise KeyError(f"No capability {cap_id!r} in {package.name}")
    if not reviewer.strip():
        raise ApprovalRefused("a reviewer name is required")
    cap = caps[cap_id]
    ops = load_operations(package)
    draft = cap.model_copy(update={"status": "draft", "approval": None})
    errors = [f for f in validate_capability(draft, ops) if f.severity == "error"]
    if errors:
        raise ApprovalRefused(f"{len(errors)} validation error(s); fix them first:\n" +
                              "\n".join(f"  {f.where}: {f.rule}: {f.message}" for f in errors))
    referenced = sorted({s.operation for s in cap.steps})
    approved = cap.model_copy(update={"status": "approved", "approval": CapabilityApproval(
        approved_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        approved_by=reviewer.strip(), revision=cap.revision, content_sha256=cap.content_hash(),
        operations={op_id: ops[op_id].revision for op_id in referenced})})
    save_capability(package, approved)
    return approved
