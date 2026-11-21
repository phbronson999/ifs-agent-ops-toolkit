"""Technical approval of operations: what may be approved, and applying it. Shared by the CLI and the TUI.

Approval itself always needs a human (see ``cli.main.human_confirmation`` and the TUI's confirmation
dialog); this module only decides what is blocked or pending and records approvals once a human confirmed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ifs_agent_ops.operations import service
from ifs_agent_ops.operations.store import approve, load_operations
from ifs_agent_ops.operations.validate import approval_blockers
from ifs_agent_ops.workspace.package import Package


class ApprovalChanged(RuntimeError):
    """Something changed between showing the review and the human's confirmation."""


def reviewer_name(root=None) -> str:
    """The name recorded with an approval: git ``user.name`` for the package repo, else the OS login.

    The human gate is the one-time code typed back; the name is attribution, so it is not asked for."""
    import getpass
    import subprocess

    try:
        out = subprocess.run(["git", "config", "user.name"], cwd=root, capture_output=True, text=True, timeout=5,
                             check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser raises various errors when no login name is available
        return "unknown"


@dataclass
class OperationSetPlan:
    """The operations a capability uses, split by what technical approval can do with them."""
    capability: str
    review_set: dict
    report_name: str
    blocked: list[tuple[str, list[str]]] = field(default_factory=list)  # (operation id, reasons)
    pending: list[dict] = field(default_factory=list)                   # reviews needing (re-)approval
    current: list[dict] = field(default_factory=list)                   # reviews already currently approved


def blockers(package: Package, op_id: str, include_questions: bool = True) -> list[str]:
    """Why an operation cannot be approved: validation errors, and open technical model questions (these block
    approving a capability's operation set; an individual approval is where they get decided)."""
    errors, questions = approval_blockers(load_operations(package)[op_id])
    return [f"{f.rule}: {f.message}" for f in errors] + (
        [f"open technical model question: {q}" for q in questions] if include_questions else [])


def plan_operation_set(package: Package, cap_id: str) -> OperationSetPlan:
    review_set, name = service.operation_reviews(package, None, cap_id)
    plan = OperationSetPlan(cap_id, review_set, name)
    for r in review_set["operations"]:
        reasons = blockers(package, r["id"])
        if reasons:
            plan.blocked.append((r["id"], reasons))
        elif r["status"] == "approved" and r["approval_current"]:
            plan.current.append(r)
        else:
            plan.pending.append(r)
    return plan


def apply_approvals(package: Package, reviews: list[dict], reviewer: str) -> list:
    """Approve the reviewed operations, refusing all of them if any changed since its review was shown.
    Each operation records its own approval (reviewer, time, revision, content hash)."""
    latest = load_operations(package)
    changed = [r["id"] for r in reviews if r["id"] not in latest or latest[r["id"]].revision != r["revision"]
               or latest[r["id"]].technical_hash() != r["content_sha256"]]
    if changed:
        raise ApprovalChanged("changed since the review was shown: " + ", ".join(changed))
    return [approve(package, r["id"], reviewer) for r in reviews]
