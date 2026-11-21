"""Evidence report: per requirement of a capability, what testing showed and the logged calls behind it.

Combines the committed findings (``validation/<capability>.findings.yaml``) with the local test log, so a
reviewer can see what was validated, what failed, what is left, and open the exact IFS calls a finding
cites. The written report quotes endpoints from the raw log, so it stays in the package's local folder
(``.ifs-agent-ops/review/``), never in committed source.

Like findings, the report never changes the capability's runtime-validation status or its approval.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ifs_agent_ops.ifs.paths import PROJECTION_ROOT
from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.operations.findings import (
    RequirementStatus,
    ValidationFinding,
    load_findings,
    requirement_status,
)
from ifs_agent_ops.testlog import CallLog
from ifs_agent_ops.workspace.package import LOCAL_STATE_DIR, Package

STATUSES = ("passed", "failed", "inconclusive", "not tested")


@dataclass
class LoggedCall:
    ref: str
    entry: dict | None  # the full test-log entry, None when it is not in this machine's log

    @property
    def found(self) -> bool:
        return self.entry is not None

    def line(self) -> str:
        """One line: time, method, status, endpoint (without the projection root)."""
        if self.entry is None:
            return f"{self.ref}  (not in this machine's test log)"
        e = self.entry
        status = e["status"] if e["status"] is not None else e.get("error") or "-"
        return (f"{self.ref}  {e['at'][:19].replace('T', ' ')}  {e['method']} {status}  "
                f"{e['endpoint'].removeprefix(PROJECTION_ROOT)}")


@dataclass
class RequirementEvidence:
    number: int
    text: str
    status: str
    latest: ValidationFinding | None
    findings: list[ValidationFinding]          # against the current wording, newest first
    outdated: list[ValidationFinding]          # against earlier wording, newest first
    calls: dict[str, LoggedCall] = field(default_factory=dict)  # every ref these findings cite


@dataclass
class CapabilityEvidence:
    capability: str
    title: str
    revision: int
    runtime_status: str
    requirements: list[RequirementEvidence]
    log_folder: Path | None

    @property
    def counts(self) -> Counter:
        return Counter(r.status for r in self.requirements)

    def counts_text(self) -> str:
        c = self.counts
        return ", ".join(f"{c[s]} {s}" for s in STATUSES if c[s]) or "no requirements"


def build_evidence(package: Package, cap: Capability, log: CallLog | None) -> CapabilityEvidence:
    rows: list[RequirementStatus] = requirement_status(cap, load_findings(package, cap.id))
    out = []
    for r in rows:
        ev = RequirementEvidence(r.number, r.text, r.status, r.latest, list(reversed(r.history)),
                                 list(reversed(r.outdated)))
        for f in ev.findings + ev.outdated:
            for ref in f.test_log_refs:
                if ref not in ev.calls:
                    ev.calls[ref] = LoggedCall(ref, log.get(ref) if log else None)
        out.append(ev)
    return CapabilityEvidence(cap.id, cap.title, cap.revision, cap.runtime_validation.status, out,
                              log.folder if log else None)


def render_markdown(ev: CapabilityEvidence) -> str:
    L = [f"# Validation evidence: {ev.title}", "",
         f"`{ev.capability}` revision {ev.revision}: {ev.counts_text()}.", "",
         (f"> Findings are a report. The runtime-validation status stays **{ev.runtime_status}**; the execution "
          "engine sets it. This report quotes the local test log, so it is not committed."), ""]
    if ev.log_folder:
        L += [f"Test log: `{ev.log_folder}`", ""]
    L += ["| # | Requirement | Status | Latest finding |", "|---|---|---|---|"]
    for r in ev.requirements:
        latest = f"{r.latest.id} ({r.latest.recorded_at[:10]})" if r.latest else "-"
        L.append(f"| {r.number} | {_cell(r.text)} | **{r.status}** | {latest} |")
    for r in ev.requirements:
        L += ["", f"## {r.number}. {r.text}", "", f"Status: **{r.status}**", ""]
        if not r.findings and not r.outdated:
            L.append("_No findings yet._")
        for f in r.findings:
            L += [f"### {f.id}: {f.result} ({f.recorded_at[:10]}, {f.recorded_by}, revision {f.capability_revision})",
                  "", f.summary, ""]
            L += [f"- `{r.calls[ref].line()}`" for ref in f.test_log_refs] or ["- _no calls cited_"]
        if r.outdated:
            L += ["", f"{len(r.outdated)} earlier finding(s) were recorded against different wording and no longer "
                  "count: " + ", ".join(f"{f.id} ({f.result})" for f in r.outdated) + "."]
    return "\n".join(L) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def write_report(package: Package, ev: CapabilityEvidence) -> Path:
    folder = package.root / LOCAL_STATE_DIR / "review"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ev.capability}.validation.md"
    path.write_text(render_markdown(ev), encoding="utf-8", newline="\n")
    return path


def active_log() -> CallLog | None:
    """The test log of the active profile (what the MCP server writes to), or None without a profile."""
    from ifs_agent_ops import profiles as prof

    try:
        name = prof.resolve_profile(None).name
    except prof.ProfileError:
        return None
    return CallLog.beside_session(prof.session_file(name), name)
