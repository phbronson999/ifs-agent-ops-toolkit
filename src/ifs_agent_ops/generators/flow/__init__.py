"""Flow generator (Phase 12b): approved capability actions -> Power Automate cloud flows.

One flow per capability action (``prepare``, ``submit``), written to
``<package>/power-platform/flows/<capability>/<action>.json`` in the solution's workflow format, plus a
``report.md``. A flow is generated from the action's inputs, steps, bindings and outputs (not as a mirror of
connector parameters) and calls IFS through the package's generated connectors and the connection
references in ``deploy.yaml``. See ``build.py`` for the shape of a flow.

Deterministic and offline. Two values depend on the environment and are filled in by ``deploy apply``:
``%%CONNECTOR_API:<connector>%%`` (the deployed connector's API name) and ``%%TRIGGER_USERS%%`` (the users
allowed to call the HTTP trigger, from the deployment target). Neither is ever written into the package.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from ifs_agent_ops.generators.flow.build import (
    USERS_TOKEN,
    ConnectorUse,
    FlowBuilder,
    api_token,
    pascal,
)
from ifs_agent_ops.generators.gating import GenerationRefused, gate_capability
from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.operations.capability_store import load_capabilities
from ifs_agent_ops.operations.model import Operation
from ifs_agent_ops.operations.store import load_operations
from ifs_agent_ops.workspace.package import Package

GENERATOR = "ifs-agent-ops flow generator v1"
REPORT_FILE = "report.md"
MARKER = "<!-- ifs-agent-ops:generated flows -->"

__all__ = [
    "GENERATOR",
    "REPORT_FILE",
    "USERS_TOKEN",
    "FlowResult",
    "Generation",
    "api_token",
    "flow_dir",
    "flow_name",
    "flows_root",
    "generate",
]


def flows_root(package: Package) -> Path:
    return package.root / "power-platform" / "flows"


def flow_dir(package: Package, capability: str) -> Path:
    return flows_root(package) / capability


def flow_name(capability: str, action: str) -> str:
    """Display name of a deployed flow: create-customer-order + prepare -> CreateCustomerOrder_Prepare."""
    return pascal(capability).replace("_", "") + "_" + pascal(action).replace("_", "")


@dataclass
class FlowResult:
    path: Path
    action: str   # created | updated | unchanged | removed | would-create | would-update | would-remove | refused
    message: str = ""


@dataclass
class Generation:
    results: list[FlowResult]
    flows: dict[str, dict[str, dict]] = field(default_factory=dict)   # capability -> action -> workflow
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _connector_uses(package: Package) -> dict[str, ConnectorUse]:
    from ifs_agent_ops.deploy.intent import IntentError, load_intent
    from ifs_agent_ops.deploy.powerplatform import connector_name
    from ifs_agent_ops.generators.connector import DEFINITION_FILE, MAP_FILE

    try:
        intent = load_intent(package)
    except IntentError as exc:
        raise GenerationRefused(str(exc)) from exc
    if intent is None or intent.power_platform is None:
        raise GenerationRefused(f"{package.name} has no deploy.yaml: flows call IFS through its connectors and "
                                f"connection references (ifs-agent-ops deploy init --package {package.name})")
    pp = intent.power_platform
    out = {}
    for c in pp.connectors:
        folder = package.root / c.source
        if not (folder / MAP_FILE).exists() or not (folder / DEFINITION_FILE).exists():
            raise GenerationRefused(f"connector {c.logical_name} is not generated yet (ifs-agent-ops connector "
                                    f"generate --package {package.name})")
        id_map = yaml.safe_load((folder / MAP_FILE).read_text(encoding="utf-8")) or {}
        ref = next((r.logical_name for r in pp.connection_references if r.connector == c.logical_name), None)
        if ref is None:
            continue
        projection = id_map.get("projection") or folder.name
        out[projection] = ConnectorUse(
            projection, c.logical_name, connector_name(pp.solution.prefix, c.logical_name), ref,
            json.loads((folder / DEFINITION_FILE).read_text(encoding="utf-8")),
            {k: v["operation_id"] for k, v in (id_map.get("operations") or {}).items()})
    return out


def _render(workflow: dict) -> str:
    return json.dumps(workflow, indent=2, ensure_ascii=False) + "\n"


def _report(package: Package, cap: Capability, built: dict[str, tuple[dict, FlowBuilder]]) -> str:
    how = [
        ("HTTP trigger with `triggerAuthenticationType: User`: only the users listed in the deployment target "
         "(`flow_trigger_users`) may call it. The list is filled in on deploy and never stored in the package."),
        ("The steps run in the action's order inside the `Try` scope. Each runs only while no error is recorded; "
         "lookups and checks run only when the values they search with are supplied (a skipped lookup counts as "
         "no match). Per-line steps run in one loop, one line at a time."),
        ("Values come from the trigger body (inputs), the current line (items), earlier steps' results "
         "(recorded in variables), configuration and fixed values. Records overlay earlier results in order; a "
         "later null does not overwrite an earlier value. Body fields seeded with `body_from` follow the same "
         "rule; omitted and unbound fields are not sent."),
        ("Every connector call has a failure handler that records the capability's error meaning (by HTTP status "
         "class), the status and the IFS response. A recorded error skips the remaining steps; the response still "
         "carries what was done (for example the order and lines already created)."),
        ("The flow always answers with HTTP 200 and JSON: `ok`, `error`, the action's outputs or proposal, "
         "questions and warnings."),
    ]
    used = {cu.connector: cu for _, b in built.values() for cu in b.used.values()}
    placeholders = [f"`{USERS_TOKEN}`: the target's `flow_trigger_users`."]
    placeholders += [(f"`{api_token(cu.connector)}`: the API name of connector `{cu.schema_name}` in the "
                      f"environment (connection reference `{cu.reference}`).") for cu in used.values()]
    lines = [MARKER, f"# Flows for capability `{cap.id}` (revision {cap.revision})", "",
             (f"Generated by {GENERATOR} from the approved capability; do not edit. Regenerate with "
              f"`ifs-agent-ops flow generate --package {package.name}`."), "",
             "| Flow | Action | Steps | Returns | Writes |", "|---|---|---|---|---|"]
    for action, (_, b) in built.items():
        writes = "yes, after `confirmed: true`" if any(s.role == "write" for s in b.steps) else "no"
        lines.append(f"| `{flow_name(cap.id, action)}` | {action} | {len(b.steps)} | "
                     f"{', '.join(b.action.returns)} | {writes} |")
    lines += ["", "## How a flow runs", ""] + [f"- {h}" for h in how]
    lines += ["", "## Placeholders filled in by `deploy apply`", ""] + [f"- {p}" for p in placeholders]
    lines += ["", "## Steps", ""]
    for action, (_, b) in built.items():
        lines.append(f"**{action}**: " + " → ".join(
            (f"`{s.id}` (per {s.for_each})" if s.for_each else f"`{s.id}`") for s in b.steps))
        lines.append("")
    notes = sorted({n for _, b in built.values() for n in b.notes})
    if notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    lines += ["## Not yet validated", "",
              ("Generated flows are not runtime-validated until `deploy validate` runs them through their "
               "triggers (Phase 12c)."), ""]
    return "\n".join(lines)


def build(package: Package) -> dict[str, dict[str, tuple[dict, FlowBuilder]]]:
    caps = load_capabilities(package)
    ops: dict[str, Operation] = load_operations(package)
    approved = [c for c in caps.values() if c.status == "approved"]
    if not approved:
        return {}
    connectors = _connector_uses(package)
    out = {}
    for cap in approved:
        gate_capability(cap, ops)
        out[cap.id] = {}
        for action in cap.actions:
            b = FlowBuilder(cap, action, ops, connectors)
            out[cap.id][action.name] = (b.build(), b)
    return out


def generate(package: Package, check: bool = False) -> Generation:
    root = flows_root(package)
    try:
        built = build(package)
    except GenerationRefused as exc:
        return Generation([FlowResult(root, "refused", str(exc))], errors=[str(exc)])
    files: dict[Path, str] = {}
    for cap_id, actions in built.items():
        folder = flow_dir(package, cap_id)
        report = folder / REPORT_FILE
        if folder.is_dir() and any(folder.glob("*.json")) and (
                not report.exists() or MARKER not in report.read_text(encoding="utf-8")):
            msg = f"{folder} holds flows that were not generated; not overwriting them"
            return Generation([FlowResult(folder, "refused", msg)], errors=[msg])
        for action, (wf, _) in actions.items():
            files[folder / f"{action}.json"] = _render(wf)
        cap = load_capabilities(package)[cap_id]
        files[report] = _report(package, cap, actions)
    results = []
    for path, text in files.items():
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old == text:
            results.append(FlowResult(path, "unchanged"))
        elif check:
            results.append(FlowResult(path, "would-create" if old is None else "would-update"))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            results.append(FlowResult(path, "created" if old is None else "updated"))
    if root.is_dir():
        for folder in sorted(d for d in root.iterdir() if d.is_dir()):
            report = folder / REPORT_FILE
            if not report.exists() or MARKER not in report.read_text(encoding="utf-8"):
                continue
            for path in sorted(folder.iterdir()):
                if path in files or not path.is_file():
                    continue
                if check:
                    results.append(FlowResult(path, "would-remove"))
                else:
                    path.unlink()
                    results.append(FlowResult(path, "removed"))
    return Generation(results, {c: {a: wf for a, (wf, _) in acts.items()} for c, acts in built.items()})


def load_flow(package: Package, capability: str, action: str) -> Optional[dict]:
    path = flow_dir(package, capability) / f"{action}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
