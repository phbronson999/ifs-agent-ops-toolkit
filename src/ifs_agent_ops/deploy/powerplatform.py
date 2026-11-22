"""Power Platform deployment adapter: reconcile a package's deploy.yaml with a target environment.

``plan`` reads the environment (the platform is authoritative; nothing is cached) and lists what differs.
``apply`` imports one unmanaged solution file carrying only what must be created or updated, then adds
existing components (environment variables, connection references) to the solution by id, reads the
environment back and fails if anything still differs: a second ``apply`` is a no-op.

What a person does (never automated): create each connection (gateway + IFS username and password) and set
environment variable values the target does not supply. ``status`` and the checklist say what is missing.
"""
from __future__ import annotations

import json
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ifs_agent_ops.deploy import solution_zip as sz
from ifs_agent_ops.deploy.intent import DeployIntent, validate
from ifs_agent_ops.deploy.pac import Pac, PacError  # noqa: F401  (PacError is re-exported for the CLI)
from ifs_agent_ops.deploy.targets import Target
from ifs_agent_ops.workspace.package import Package

API_PREFIX = "/providers/Microsoft.PowerApps/apis/"
CREATE, UPDATE, ADD, OK, MANUAL = "create", "update", "add to solution", "ok", "manual"


class DeployError(RuntimeError):
    pass


@dataclass
class Item:
    kind: str          # solution | connector | environment variable | connection reference | connection | value
    name: str
    action: str        # create | update | add to solution | ok | manual
    detail: str = ""

    @property
    def change(self) -> bool:
        return self.action in (CREATE, UPDATE, ADD)


@dataclass
class Plan:
    package: str
    target: str
    environment: str
    solution: str
    version: Optional[str]                     # deployed version, None when the solution does not exist
    items: list[Item] = field(default_factory=list)

    @property
    def changes(self) -> list[Item]:
        return [i for i in self.items if i.change]

    @property
    def manual(self) -> list[Item]:
        return [i for i in self.items if i.action == MANUAL]


# ── desired state ────────────────────────────────────────────────────────────

def connector_name(prefix: str, logical_name: str) -> str:
    """Schema name of a connector: ti + customer-order-handling -> ti_ifscustomerorderhandling (never changes)."""
    return f"{prefix}_ifs{re.sub(r'[^a-z0-9]', '', logical_name.lower())}"


def _connector_id(solution: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ifs-agent-ops:{solution}:{name}"))


@dataclass
class _Desired:
    part: sz.ConnectorPart
    logical_name: str


def _desired_connectors(intent: DeployIntent, package: Package) -> list[_Desired]:
    pp = intent.power_platform
    out = []
    for c in pp.connectors:
        folder = package.root / c.source
        definition = json.loads((folder / "apiDefinition.swagger.json").read_text(encoding="utf-8"))
        props = json.loads((folder / "apiProperties.json").read_text(encoding="utf-8"))["properties"]
        if c.host_from:
            definition["host"] = f'@environmentVariables("{c.host_from}")'
        definition["info"]["title"] = c.display_name
        name = connector_name(pp.solution.prefix, c.logical_name)
        out.append(_Desired(sz.ConnectorPart(
            connector_id=_connector_id(pp.solution.unique_name, name), name=name, display_name=c.display_name,
            description=definition["info"].get("description", ""), definition=definition,
            connection_parameters=props.get("connectionParameters", {}),
            icon_brand_color=props.get("iconBrandColor", "#007ee5")), c.logical_name))
    return out


# ── reading the environment ──────────────────────────────────────────────────

def _one(rows: list[dict]) -> Optional[dict]:
    return rows[0] if rows else None


def _int(text: str) -> int:
    return int(re.sub(r"\D", "", text or "0") or 0)


@dataclass
class _State:
    solution: Optional[dict]
    publisher: Optional[dict]
    members: set[str]
    connectors: dict[str, Optional[dict]]          # schema name -> connector row
    variables: dict[str, Optional[dict]]           # schema name -> definition row (+ "value")
    references: dict[str, Optional[dict]]          # logical name -> connectionreference row
    reference_type: Optional[int]


def _read(intent: DeployIntent, desired: list[_Desired], pac: Pac) -> _State:
    pp = intent.power_platform
    sol = next((s for s in pac.solutions() if s.get("SolutionUniqueName", "").lower()
                == pp.solution.unique_name.lower()), None)
    publisher = _one(pac.fetch("publisher", ["uniquename", "friendlyname", "customizationprefix",
                                             "customizationoptionvalueprefix"], {"uniquename": pp.solution.publisher}))
    members: set[str] = set()
    if sol:
        members = {r.get("objectid", "").lower() for r in
                   pac.fetch("solutioncomponent", ["objectid"], {"solutionid": sol["Id"]})}
    connectors = {d.part.name: _one(pac.fetch("connector", ["name", "connectorinternalid", "capabilities",
                                                            "displayname"], {"name": d.part.name}))
                  for d in desired}
    variables = {}
    for v in pp.environment_variables:
        row = _one(pac.fetch("environmentvariabledefinition", ["schemaname", "type", "defaultvalue"],
                             {"schemaname": v.schema_name}))
        if row:
            value = _one(pac.fetch("environmentvariablevalue", ["value"],
                                   {"environmentvariabledefinitionid": row["environmentvariabledefinitionid"]}))
            row["value"] = (value or {}).get("value") or row.get("defaultvalue", "")
        variables[v.schema_name] = row
    references = {r.logical_name: _one(pac.fetch("connectionreference", ["connectionreferencelogicalname",
                                                                         "connectorid", "connectionid",
                                                                         "customconnectorid"],
                                                 {"connectionreferencelogicalname": r.logical_name}))
                  for r in pp.connection_references}
    ref_type = None
    if pp.connection_references:
        row = _one(pac.fetch("solutioncomponentdefinition", ["name", "solutioncomponenttype"],
                             {"name": "connectionreference"}))
        ref_type = _int(row["solutioncomponenttype"]) if row else None
    return _State(sol, publisher, members, connectors, variables, references, ref_type)


def _connector_drift(d: _Desired, row: dict, pac: Pac) -> list[str]:
    reasons = []
    if row.get("displayname") != d.part.display_name:
        reasons.append(f"display name is {row.get('displayname')!r}")
    definition, props = pac.download_connector(row["connectorid"])
    if definition != d.part.definition:
        changed = sorted(k for k in set(definition) | set(d.part.definition)
                         if definition.get(k) != d.part.definition.get(k))
        reasons.append("definition differs (" + ", ".join(changed) + ")")
    if props.get("connectionParameters") != d.part.connection_parameters:
        reasons.append("connection parameters differ")
    return reasons


# ── plan ─────────────────────────────────────────────────────────────────────

@dataclass
class _Work:
    plan: Plan
    state: _State
    desired: list[_Desired]
    connectors: list[sz.ConnectorPart] = field(default_factory=list)
    variables: list[sz.VariablePart] = field(default_factory=list)
    references: list[sz.ReferencePart] = field(default_factory=list)
    add: list[tuple[str, int, Item]] = field(default_factory=list)     # (component id, type, item)
    references_after_connectors: bool = False


def deployed_file(path: Path) -> bool:
    """Generated connector files that are deployed (or keep deployed ids stable); report.md is documentation and
    goes stale whenever a runtime-validation status is recorded."""
    from ifs_agent_ops.generators.connector import REPORT_FILE

    return path.name != REPORT_FILE


def _check_ready(intent: Optional[DeployIntent], package: Package) -> DeployIntent:
    if intent is None or intent.power_platform is None:
        raise DeployError(f"{package.name} has no deploy.yaml with a power_platform section "
                          f"(ifs-agent-ops deploy init --package {package.name} ...)")
    errors, warnings = validate(intent, package)
    errors += [w for w in warnings if "not generated" in w]
    if errors:
        raise DeployError("deploy.yaml is not ready:\n  " + "\n  ".join(errors))
    from ifs_agent_ops.generators.connector import generate
    stale = [r for r in generate(package, check=True).results if r.action != "unchanged" and deployed_file(r.path)]
    if stale:
        raise DeployError("generated connectors are not current; run: ifs-agent-ops connector generate --package "
                          f"{package.name}\n  " + "\n  ".join(f"{r.action} {r.path}" for r in stale))
    return intent


def _work(intent: DeployIntent, package: Package, target: Target, pac: Pac) -> _Work:
    pp = intent.power_platform
    desired = _desired_connectors(intent, package)
    st = _read(intent, desired, pac)
    plan = Plan(package.name, target.name, target.environment, pp.solution.unique_name,
                st.solution.get("VersionNumber") if st.solution else None)
    w = _Work(plan, st, desired)
    add = plan.items.append

    if st.publisher is None:
        raise DeployError(f"publisher {pp.solution.publisher!r} does not exist in {target.environment}")
    if st.publisher.get("customizationprefix") != pp.solution.prefix:
        raise DeployError(f"publisher {pp.solution.publisher} has prefix {st.publisher.get('customizationprefix')!r}, "
                          f"deploy.yaml says {pp.solution.prefix!r}")
    if st.solution is None:
        add(Item("solution", pp.solution.unique_name, CREATE, f"unmanaged, publisher {pp.solution.publisher}"))
    elif st.solution.get("IsManaged"):
        raise DeployError(f"{pp.solution.unique_name} is managed in {target.environment}; deploy targets the "
                          "development environment (promotion is done with Power Platform Pipelines)")
    else:
        add(Item("solution", pp.solution.unique_name, OK, f"version {st.solution.get('VersionNumber')}"))

    api_ids: dict[str, str] = {}
    record_ids: dict[str, str] = {}
    for d in desired:
        row = st.connectors[d.part.name]
        if row is None:
            add(Item("connector", d.part.name, CREATE, f"{d.part.display_name}, gateway + Basic auth"))
            w.connectors.append(d.part)
            continue
        api_ids[d.logical_name] = API_PREFIX + row["connectorinternalid"]
        record_ids[d.logical_name] = row["connectorid"]
        d.part.connector_id = row["connectorid"]
        if "gateway" not in row.get("capabilities", "").lower():
            add(Item("connector", d.part.name, MANUAL, "exists without the on-premises data gateway, which cannot "
                     "be turned on after creation: delete the connector in the environment, then deploy again"))
            continue
        reasons = _connector_drift(d, row, pac)
        if reasons:
            add(Item("connector", d.part.name, UPDATE, "; ".join(reasons)))
            w.connectors.append(d.part)
        elif st.solution and row["connectorid"].lower() not in st.members:
            add(Item("connector", d.part.name, ADD, "exists outside the solution"))
            w.connectors.append(d.part)
        else:
            add(Item("connector", d.part.name, OK, d.part.display_name))

    for v in pp.environment_variables:
        row = st.variables[v.schema_name]
        value = target.variables.get(v.schema_name)
        if row is None:
            add(Item("environment variable", v.schema_name, CREATE,
                     "value from the target" if value is not None else "no value yet"))
            w.variables.append(sz.VariablePart(v.schema_name, v.display_name, v.description, v.type, value))
        elif not st.solution or row["environmentvariabledefinitionid"].lower() not in st.members:
            item = Item("environment variable", v.schema_name, ADD, "existing variable, definition left as is")
            add(item)
            w.add.append((row["environmentvariabledefinitionid"], sz.ENVIRONMENT_VARIABLE_COMPONENT, item))
        else:
            add(Item("environment variable", v.schema_name, OK, "in the solution"))
        if (row is None and value is None) or (row is not None and not row.get("value")):
            add(Item("value", v.schema_name, MANUAL, f"set the value of {v.schema_name} in {target.environment} "
                     "(or add it under environment_variables in the target file)"))

    connectors_by_logical = {c.logical_name: c for c in pp.connectors}
    for r in pp.connection_references:
        row = st.references[r.logical_name]
        api_id = api_ids.get(r.connector)
        record_id = record_ids.get(r.connector, "")
        display = connectors_by_logical[r.connector].display_name
        # pac env fetch shows a lookup as the linked record's name, so accept the connector's id or name
        accepted = {record_id.lower(), connector_name(pp.solution.prefix, r.connector).lower()} - {""}
        linked = row is not None and (row.get("customconnectorid") or "").lower() in accepted
        stale = row is not None and api_id and (row.get("connectorid") != api_id or not linked)
        if row is None or stale:
            action = CREATE if row is None else UPDATE
            why = "" if row is None or row.get("connectorid") == api_id else ", connector changed"
            if row is not None and not linked:
                why += ", link to the custom connector missing (the solution cannot be exported without it)"
            add(Item("connection reference", r.logical_name, action, f"for {display}{why}"
                     + ("" if api_id else " (after the connector is created)")))
            if api_id:
                w.references.append(sz.ReferencePart(r.logical_name, r.display_name, api_id, record_id))
            else:
                w.references_after_connectors = True
        elif not st.solution or row["connectionreferenceid"].lower() not in st.members:
            item = Item("connection reference", r.logical_name, ADD, f"for {display}")
            add(item)
            w.add.append((row["connectionreferenceid"], st.reference_type or 0, item))
        else:
            add(Item("connection reference", r.logical_name, OK, f"for {display}"))
        if row is None or not row.get("connectionid"):
            add(Item("connection", r.logical_name, MANUAL,
                     f"create a connection for {display} (pick the on-premises data gateway, enter the IFS username "
                     f"and password) and select it on connection reference {r.logical_name}"))
        else:
            add(Item("connection", r.logical_name, OK, "attached"))
    return w


def plan(intent: Optional[DeployIntent], package: Package, target: Target, pac: Optional[Pac] = None) -> Plan:
    intent = _check_ready(intent, package)
    return _work(intent, package, target, pac or Pac(target.environment_url)).plan


# ── apply ────────────────────────────────────────────────────────────────────

def _bump(version: Optional[str]) -> str:
    if not version:
        return "1.0.0.0"
    parts = [int(p) for p in re.findall(r"\d+", version)][:4]
    parts += [0] * (4 - len(parts))
    parts[3] += 1
    return ".".join(map(str, parts))


def _import(w: _Work, intent: DeployIntent, pac: Pac, connectors, variables, references) -> str:
    pp = intent.power_platform
    pub = w.state.publisher
    version = _bump(w.plan.version)
    sol = sz.SolutionFile(pp.solution.unique_name, pp.solution.display_name, version,
                          sz.Publisher(pub["uniquename"], pub.get("friendlyname") or pub["uniquename"],
                                       pub["customizationprefix"], _int(pub["customizationoptionvalueprefix"])),
                          connectors, variables, references)
    with tempfile.TemporaryDirectory() as tmp:
        path = sz.write_solution(sol, Path(tmp) / f"{pp.solution.unique_name}_{version.replace('.', '_')}.zip")
        pac.import_solution(path)
    return version


@dataclass
class ApplyResult:
    before: Plan
    after: Plan
    imported: list[str] = field(default_factory=list)      # solution versions imported
    added: list[str] = field(default_factory=list)          # components added to the solution
    settings_path: Optional[Path] = None


def apply(intent: Optional[DeployIntent], package: Package, target: Target, pac: Optional[Pac] = None) -> ApplyResult:
    intent = _check_ready(intent, package)
    pac = pac or Pac(target.environment_url)
    w = _work(intent, package, target, pac)
    result = ApplyResult(before=w.plan, after=w.plan)
    if w.plan.changes:
        needs_import = (w.state.solution is None or w.connectors or w.variables or w.references)
        if needs_import:
            result.imported.append(_import(w, intent, pac, w.connectors, w.variables, w.references))
            w = _work(intent, package, target, pac)          # new connectors now have their API ids
        if w.references:
            result.imported.append(_import(w, intent, pac, [], [], w.references))
            w = _work(intent, package, target, pac)
        for component_id, component_type, item in w.add:
            if not component_type:
                raise DeployError(f"cannot add {item.kind} {item.name}: component type unknown in this environment")
            pac.add_component(intent.power_platform.solution.unique_name, component_id, component_type)
            result.added.append(f"{item.kind} {item.name}")
        w = _work(intent, package, target, pac)
        result.after = w.plan
        if w.plan.changes:
            raise DeployError("the environment still differs after deploying:\n  "
                              + "\n  ".join(f"{i.kind} {i.name}: {i.action} ({i.detail})" for i in w.plan.changes))
    result.settings_path = write_settings(intent, target, w.state)
    return result


# ── deployment settings file + checklist ─────────────────────────────────────

def write_settings(intent: DeployIntent, target: Target, state: _State) -> Path:
    """The deployment settings file in pac's format (EnvironmentVariables, ConnectionReferences) for this target.
    Machine-local next to the target file: it can hold environment-specific values."""
    pp = intent.power_platform
    data = {"EnvironmentVariables": [
                {"SchemaName": v.schema_name,
                 "Value": target.variables.get(v.schema_name, (state.variables.get(v.schema_name) or {}).get("value", ""))}
                for v in pp.environment_variables],
            "ConnectionReferences": [
                {"LogicalName": r.logical_name,
                 "ConnectionId": (state.references.get(r.logical_name) or {}).get("connectionid", ""),
                 "ConnectorId": (state.references.get(r.logical_name) or {}).get("connectorid", "")}
                for r in pp.connection_references]}
    target.settings_path.parent.mkdir(parents=True, exist_ok=True)
    target.settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    return target.settings_path


def checklist(p: Plan) -> list[str]:
    return [i.detail for i in p.manual]


def describe(p: Plan) -> list[str]:
    head = f"{p.package} -> {p.target} ({p.environment}), solution {p.solution} " + \
           (f"{p.version}" if p.version else "(not deployed)")
    lines = [head]
    for i in p.items:
        if i.action == MANUAL:
            continue
        lines.append(f"  {i.action:<16} {i.kind} {i.name}" + (f"  - {i.detail}" if i.detail else ""))
    lines.append(f"{len(p.changes)} change(s)" if p.changes else "No changes: the environment matches deploy.yaml.")
    return lines
