"""MCP tools for validation findings (Phase 9b): what Claude's tests against IFS showed, per requirement."""
from __future__ import annotations

from typing import Any

from ifs_agent_ops.operations.capability_store import load_capabilities
from ifs_agent_ops.operations.findings import (
    FindingError,
    load_findings,
    record_finding,
    status_summary,
)
from ifs_agent_ops.workspace.discovery import find_package
from ifs_agent_ops.workspace.package import PackageError


def _capability(package: str, capability: str):
    pkg = find_package(package)
    caps = load_capabilities(pkg)
    if capability not in caps:
        raise FindingError(f"no capability {capability!r} in {pkg.name} (have: {', '.join(sorted(caps)) or 'none'})")
    return pkg, caps[capability]


async def get_status(package: str, capability: str) -> str:
    try:
        pkg, cap = _capability(package, capability)
        return status_summary(cap, load_findings(pkg, cap.id))
    except (PackageError, KeyError, FindingError, ValueError) as exc:
        return f"error: {exc}"


async def record(package: str, capability: str, requirement: int, result: str, summary: str,
                 test_log_refs: list[str] | None, test_log: Any = None) -> str:
    """``test_log`` (the server's CallLog, when logging is on) is used to check that every ref exists."""
    try:
        pkg, cap = _capability(package, capability)
        if test_log is not None:
            missing = [r for r in (test_log_refs or []) if test_log.get(r.strip()) is None]
            if missing:
                return (f"error: not in this machine's test log: {', '.join(missing)}. Use the test_log_ref "
                        "returned by call_protected_api for the calls that show the result.")
        f = record_finding(pkg, cap, requirement=requirement, result=result, summary=summary,
                           test_log_refs=test_log_refs)
    except (PackageError, KeyError, FindingError, ValueError) as exc:
        return f"error: {exc}"
    return (f"Recorded {f.id}: requirement {f.requirement} {f.result.upper()} for {cap.id} revision "
            f"{cap.revision} (validation/{cap.id}.findings.yaml; commit it with the package).\n\n"
            + status_summary(cap, load_findings(pkg, cap.id)))
