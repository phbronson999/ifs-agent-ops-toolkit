"""Approval gates shared by the generators: only current human approvals produce artifacts."""
from __future__ import annotations

from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.operations.capability_validate import validate_capability
from ifs_agent_ops.operations.model import Operation


class GenerationRefused(RuntimeError):
    pass


def gate_capability(cap: Capability, ops: dict[str, Operation]) -> None:
    """Refuse a capability that is not approved, or whose approval is no longer current."""
    if cap.status != "approved" or cap.approval is None:
        raise GenerationRefused(f"{cap.id} is {cap.status}; only approved capabilities are generated")
    errors = [f for f in validate_capability(cap, ops) if f.severity == "error"]
    if errors:
        raise GenerationRefused(f"{cap.id} has {len(errors)} validation error(s) (e.g. {errors[0].rule}: "
                                f"{errors[0].message}); its approval is not current")


def gate_operation(op: Operation) -> None:
    """Refuse an operation without a current technical approval."""
    if op.status != "approved" or op.approval is None:
        raise GenerationRefused(f"{op.id} is {op.status}; only approved operations are generated")
    if op.approval.content_sha256 and op.approval.content_sha256 != op.technical_hash():
        raise GenerationRefused(f"{op.id} changed after its approval (revision {op.approval.revision}); "
                                "re-review and re-approve it")
