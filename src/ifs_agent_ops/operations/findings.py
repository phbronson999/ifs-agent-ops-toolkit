"""Validation findings: what testing against IFS showed, per runtime-validation requirement of a capability.

Claude records a finding after testing (Phase 9b): which requirement, passed / failed / inconclusive, a
plain-language summary, and the ``test_log_ref`` of each call that shows it (the raw calls stay in the
local test log). Findings are committed with the package in ``validation/<capability>.findings.yaml``
so everyone can see what was validated and what failed.

Findings are a report. They never change ``runtime_validation.status``: that status gates deployment
and is set by the execution engine (Phase 11), which runs the curated sequence itself.

A finding stores the requirement's wording as it was tested. When the wording changes in a later
capability revision, earlier findings are shown as outdated and the requirement counts as not yet
tested again.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.workspace.package import Package

Result = Literal["passed", "failed", "inconclusive"]
RESULTS: tuple[str, ...] = ("passed", "failed", "inconclusive")
REF_RE = re.compile(r"^\d{8}-\d{4}$")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationFinding(_M):
    id: str = Field(description="F-0001, numbered per capability")
    requirement: int = Field(description="1-based number of the requirement in runtime_validation.requirements")
    requirement_text: str = Field(description="The requirement's wording when it was tested")
    result: Result
    summary: str = Field(description="What the test showed, in plain language")
    test_log_refs: list[str] = Field(default_factory=list, description="test_log_ref of each call that shows it")
    capability_revision: int
    recorded_at: str
    recorded_by: str


class FindingsFile(_M):
    schema_version: int = 1
    capability: str
    findings: list[ValidationFinding] = Field(default_factory=list)


class FindingError(ValueError):
    pass


def findings_path(package: Package, cap_id: str) -> Path:
    return package.root / "validation" / f"{cap_id}.findings.yaml"


def load_findings(package: Package, cap_id: str) -> FindingsFile:
    path = findings_path(package, cap_id)
    if not path.exists():
        return FindingsFile(capability=cap_id)
    data = FindingsFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if data.capability != cap_id:
        raise FindingError(f"{path.name}: capability must be {cap_id!r}, found {data.capability!r}")
    return data


def _save(package: Package, data: FindingsFile) -> Path:
    path = findings_path(package, data.capability)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=120),
                    encoding="utf-8", newline="\n")
    return path


def record_finding(package: Package, cap: Capability, *, requirement: int, result: str, summary: str,
                   test_log_refs: list[str] | None = None, recorded_by: str = "claude",
                   now: datetime | None = None) -> ValidationFinding:
    """Append a finding for one requirement of ``cap`` and save the file. Raises FindingError on bad input."""
    reqs = cap.runtime_validation.requirements
    if not 1 <= requirement <= len(reqs):
        raise FindingError(f"requirement must be 1..{len(reqs)} for {cap.id} (it has {len(reqs)} requirement(s))")
    if result not in RESULTS:
        raise FindingError(f"result must be one of {', '.join(RESULTS)}")
    if not summary.strip():
        raise FindingError("a summary of what the test showed is required")
    refs = [r.strip() for r in (test_log_refs or []) if r.strip()]
    bad = [r for r in refs if not REF_RE.match(r)]
    if bad:
        raise FindingError(f"test_log_refs look like 20261121-0007; not: {', '.join(bad)}")
    if result != "inconclusive" and not refs:
        raise FindingError("a passed or failed finding needs the test_log_ref of at least one call that shows it")
    data = load_findings(package, cap.id)
    finding = ValidationFinding(
        id=f"F-{len(data.findings) + 1:04d}", requirement=requirement, requirement_text=reqs[requirement - 1],
        result=result, summary=summary.strip(), test_log_refs=refs, capability_revision=cap.revision,
        recorded_at=(now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds"),
        recorded_by=recorded_by)
    data.findings.append(finding)
    _save(package, data)
    return finding


@dataclass
class RequirementStatus:
    number: int
    text: str
    status: str                          # passed | failed | inconclusive | not tested
    latest: ValidationFinding | None     # latest finding against the current wording
    history: list[ValidationFinding]     # all findings against the current wording, oldest first
    outdated: list[ValidationFinding]    # findings recorded against earlier wording of this requirement


def requirement_status(cap: Capability, data: FindingsFile) -> list[RequirementStatus]:
    """Per requirement: the result of its latest finding against the current wording."""
    out = []
    for n, text in enumerate(cap.runtime_validation.requirements, 1):
        mine = [f for f in data.findings if f.requirement == n]
        current = [f for f in mine if f.requirement_text == text]
        latest = current[-1] if current else None
        out.append(RequirementStatus(n, text, latest.result if latest else "not tested", latest, current,
                                     [f for f in mine if f.requirement_text != text]))
    return out


def status_summary(cap: Capability, data: FindingsFile) -> str:
    """Plain-text report for the user and for Claude: what was validated, what failed, what is left."""
    rows = requirement_status(cap, data)
    counts = {s: sum(1 for r in rows if r.status == s) for s in ("passed", "failed", "inconclusive", "not tested")}
    lines = [f"Validation findings for {cap.id} (revision {cap.revision}): "
             + ", ".join(f"{n} {s}" for s, n in counts.items() if n) + ".",
             (f"Runtime validation status stays '{cap.runtime_validation.status}': findings are a report; the "
              "execution engine sets the status."), ""]
    for r in rows:
        lines.append(f"{r.number}. [{r.status.upper()}] {r.text}")
        if r.latest:
            refs = f" (tests: {', '.join(r.latest.test_log_refs)})" if r.latest.test_log_refs else ""
            lines.append(f"   {r.latest.id} {r.latest.recorded_at[:10]} by {r.latest.recorded_by}, "
                         f"revision {r.latest.capability_revision}: {r.latest.summary}{refs}")
        if r.outdated:
            lines.append(f"   {len(r.outdated)} earlier finding(s) were recorded against different wording "
                         "of this requirement; they no longer count.")
    return "\n".join(lines)
