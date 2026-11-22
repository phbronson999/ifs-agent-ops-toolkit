"""Deployed-path validation (Phase 11) and the runtime-validation status engine.

``validate`` checks one package against one target in three layers:

- **static**: deploy.yaml is valid; the generated connectors and skills are current;
- **platform**: the environment matches deploy.yaml (no planned changes, no manual steps left), the
  solution exports, and the Power Apps checker reports no critical or high issues;
- **runtime**: calls through the deployed path. They go through a flow, so they arrive with Phase 12;
  until then this layer reports "not run".

The **status engine** then sets each capability's ``runtime_validation.status`` from its findings (Phase 9b
testing against IFS) and the deployed-path result:

- ``failed``: the latest finding of some requirement failed;
- ``validated``: every requirement's latest finding passed and the deployed-path checks passed;
- ``conditional``: some requirements passed, but not all, or the deployed path is not green;
- ``untested``: nothing has passed yet.

It records when, against which target and on what basis (``runtime_validation.evaluation``). Status and
evaluation are excluded from approval hashes: validation never grants or revokes an approval.
Each run is saved locally in ``<package>/.ifs-agent-ops/runs/`` (never committed).
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ifs_agent_ops.deploy import powerplatform as ppa
from ifs_agent_ops.deploy.intent import DeployIntent, validate as validate_intent
from ifs_agent_ops.deploy.pac import Pac, PacError
from ifs_agent_ops.deploy.targets import Target
from ifs_agent_ops.workspace.package import LOCAL_STATE_DIR, Package

PASS, WARN, FAIL, NOT_RUN = "PASS", "WARN", "FAIL", "NOT RUN"
BLOCKING_LEVELS = ("Critical", "High")
# Checker rules that are expected for these connectors, with the reason (reported, never failing).
KNOWN_EXCEPTIONS = {
    "connector-validate-swagger:HostNameNotValid":
        "the host is @environmentVariables(...) by design: each environment supplies the IFS host",
}


@dataclass
class Check:
    layer: str      # static | platform | runtime
    name: str
    status: str     # PASS | WARN | FAIL | NOT RUN
    detail: str = ""


@dataclass
class CapabilityResult:
    capability: str
    revision: int
    previous: str
    status: str
    basis: str
    recorded: bool = False


@dataclass
class Run:
    package: str
    target: str
    environment: str
    started_at: str
    checks: list[Check] = field(default_factory=list)
    capabilities: list[CapabilityResult] = field(default_factory=list)
    path: Optional[Path] = None

    @property
    def deployed_ok(self) -> bool:
        """No failure, and no skipped static or platform check (the runtime layer waits for Phase 12)."""
        return not any(c.status == FAIL or (c.status == NOT_RUN and c.layer != "runtime") for c in self.checks)


# ── checks ───────────────────────────────────────────────────────────────────

def _static(run: Run, intent: Optional[DeployIntent], package: Package) -> None:
    from ifs_agent_ops.generators.connector import generate as connectors
    from ifs_agent_ops.generators.skills import generate as skills

    add = run.checks.append
    if intent is None or intent.power_platform is None:
        add(Check("static", "deploy.yaml", FAIL, "no deploy.yaml with a power_platform section"))
        return
    errors, warnings = validate_intent(intent, package)
    add(Check("static", "deploy.yaml", FAIL if errors else WARN if warnings else PASS,
              "; ".join(errors or warnings) or "valid"))
    results = [r for r in connectors(package, check=True).results if r.action != "unchanged"]
    stale = [r for r in results if ppa.deployed_file(r.path)]
    reports = [r for r in results if not ppa.deployed_file(r.path)]
    add(Check("static", "connectors generated", FAIL if stale else WARN if reports else PASS,
              "; ".join(f"{r.action} {r.path.parent.name}/{r.path.name}" for r in stale + reports) or "current"))
    stale_skills = [r for r in skills(package, check=True) if r.action.startswith("would-")]
    add(Check("static", "skills generated", WARN if stale_skills else PASS,
              "; ".join(f"{r.capability}: {r.action}" for r in stale_skills) or "current"))


def _sarif_findings(report: dict) -> list[tuple[str, str, str]]:
    """(severity, rule id, message) for each result of a SARIF report from the Power Apps checker."""
    out = []
    for run in report.get("runs", []):
        for res in run.get("results", []):
            severity = (res.get("properties") or {}).get("severity", "Informational")
            out.append((severity, res.get("ruleId", "?"), (res.get("message") or {}).get("text", "")))
    return out


def _known(rule: str, message: str) -> Optional[str]:
    for key, reason in KNOWN_EXCEPTIONS.items():
        rule_id, _, code = key.partition(":")
        if rule == rule_id and (not code or message.startswith(code)):
            return reason
    return None


def _platform(run: Run, intent: DeployIntent, package: Package, target: Target, pac: Pac,
              solution_check: bool) -> None:
    add = run.checks.append
    try:
        plan = ppa.plan(intent, package, target, pac)
    except (ppa.DeployError, PacError, ppa.FlowApiError) as exc:
        add(Check("platform", "deployed state", FAIL, str(exc)))
        return
    add(Check("platform", "deployed state", FAIL if plan.changes else PASS,
              "; ".join(f"{i.kind} {i.name}: {i.action}" for i in plan.changes)
              or f"matches deploy.yaml (solution {plan.version})"))
    add(Check("platform", "manual steps", FAIL if plan.manual else PASS,
              "; ".join(i.detail for i in plan.manual) or "none left (connections attached, values set)"))
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / f"{plan.solution}.zip"
        try:
            pac.export_solution(plan.solution, zip_path)
        except PacError as exc:
            add(Check("platform", "solution export", FAIL, str(exc)))
            return
        add(Check("platform", "solution export", PASS, "exports (required for promotion)"))
        if not solution_check:
            add(Check("platform", "solution checker", NOT_RUN, "skipped (--skip-solution-check)"))
            return
        try:
            report = pac.check_solution(zip_path, Path(tmp) / "check")
        except PacError as exc:
            add(Check("platform", "solution checker", FAIL, str(exc)))
            return
    findings = _sarif_findings(report)
    blocking = [f for f in findings if f[0] in BLOCKING_LEVELS]
    known = [(f, _known(f[1], f[2])) for f in findings if f[0] not in BLOCKING_LEVELS]
    counts = {}
    for sev, _, _ in findings:
        counts[sev] = counts.get(sev, 0) + 1
    summary = ", ".join(f"{n} {s.lower()}" for s, n in sorted(counts.items())) or "no issues"
    add(Check("platform", "solution checker", FAIL if blocking else WARN if findings else PASS, summary))
    for sev, rule, message in blocking:
        add(Check("platform", f"checker {rule}", FAIL, f"{sev}: {message}"))
    for (sev, rule, message), reason in known:
        add(Check("platform", f"checker {rule}", WARN, f"{sev}: {message}"
                  + (f" (expected: {reason})" if reason else "")))


# ── status engine ────────────────────────────────────────────────────────────

def decide(cap, findings, deployed_ok: bool, target: str) -> tuple[str, str]:
    """(status, basis) for one capability from its findings and the deployed-path result."""
    from ifs_agent_ops.operations.findings import requirement_status

    rows = requirement_status(cap, findings)
    total = len(rows)
    passed = [r for r in rows if r.status == "passed"]
    failed = [r for r in rows if r.status == "failed"]
    rest = total - len(passed) - len(failed)
    tally = f"{len(passed)} of {total} requirements passed in testing against IFS"
    if failed:
        return "failed", (f"{tally}; requirement(s) {', '.join(str(r.number) for r in failed)} failed "
                          f"({', '.join(r.latest.id for r in failed)})")
    if total and len(passed) == total and deployed_ok:
        return "validated", f"{tally}; deployed-path checks on {target} passed"
    if passed:
        why = [f"{rest} not yet passed"] if rest else []
        if not deployed_ok:
            why.append(f"deployed-path checks on {target} did not pass")
        return "conditional", f"{tally}; " + "; ".join(why)
    return "untested", "no requirement has passed yet"


def _deployed_capabilities(package: Package) -> list[str]:
    """Approved capabilities whose operations the generated connectors carry."""
    from ifs_agent_ops.generators.connector import build

    try:
        built = build(package)
    except Exception:  # noqa: BLE001 - the static layer already reports generation problems
        return []
    return sorted({c for b in built.values() for e in b.entries for c in e.sel.capabilities})


def _record(run: Run, package: Package, target: Target, record: bool) -> None:
    from ifs_agent_ops.operations.capability_store import caps_dir, load_capabilities, save_capability
    from ifs_agent_ops.operations.yaml_persist import persist_runtime_result
    from ifs_agent_ops.operations.findings import load_findings
    from ifs_agent_ops.operations.model import Evaluation

    caps = load_capabilities(package)
    for cap_id in _deployed_capabilities(package):
        cap = caps[cap_id]
        status, basis = decide(cap, load_findings(package, cap_id), run.deployed_ok, target.name)
        result = CapabilityResult(cap_id, cap.revision, cap.runtime_validation.status, status, basis)
        if record:
            hash_before = cap.content_hash()
            cap.runtime_validation.status = status
            cap.runtime_validation.evaluation = Evaluation(
                evaluated_at=run.started_at, target=target.name, basis=basis,
                run=run.path.name if run.path else "")
            assert cap.content_hash() == hash_before, "runtime results must never change the approval hash"
            persist_runtime_result(caps_dir(package) / f"{cap_id}.yaml", cap,
                                   lambda cap=cap: save_capability(package, cap))
            result.recorded = True
        run.capabilities.append(result)


# ── run ──────────────────────────────────────────────────────────────────────

def runs_dir(package: Package) -> Path:
    return package.root / LOCAL_STATE_DIR / "runs"


def validate(intent: Optional[DeployIntent], package: Package, target: Target, pac: Optional[Pac] = None, *,
             solution_check: bool = True, record: bool = True) -> Run:
    pac = pac or Pac(target.environment_url)
    started = datetime.now(timezone.utc).replace(microsecond=0)
    run = Run(package.name, target.name, target.environment, started.isoformat())
    run.path = runs_dir(package) / f"{started.strftime('%Y%m%d-%H%M%S')}-validate-{target.name}.json"
    _static(run, intent, package)
    if not any(c.status == FAIL for c in run.checks):
        _platform(run, intent, package, target, pac, solution_check)
    else:
        run.checks.append(Check("platform", "deployed state", NOT_RUN, "static checks failed"))
    run.checks.append(Check("runtime", "calls through the deployed path", NOT_RUN,
                            "runtime calls go through a flow; they arrive with Phase 12 (dispatcher flow)"))
    _record(run, package, target, record)
    run.path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(run)
    data["path"] = None
    data["deployed_ok"] = run.deployed_ok
    run.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    return run


def describe(run: Run) -> list[str]:
    lines = [f"Validation of {run.package} on {run.target} ({run.environment}), {run.started_at}"]
    for layer in ("static", "platform", "runtime"):
        lines.append(f"[{layer}]")
        lines += [f"  {c.status:<8} {c.name}" + (f"  - {c.detail}" if c.detail else "")
                  for c in run.checks if c.layer == layer]
    lines.append("Deployed-path checks: " + ("passed" if run.deployed_ok else "FAILED"))
    if run.capabilities:
        lines.append("[runtime validation status]")
        for c in run.capabilities:
            change = c.status if c.status == c.previous else f"{c.previous} -> {c.status}"
            lines.append(f"  {c.capability} (revision {c.revision}): {change}"
                         + ("" if c.recorded else " (not recorded: --no-record)") + f"  - {c.basis}")
    return lines
