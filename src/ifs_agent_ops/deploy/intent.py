"""Deployment intent: ``<package>/deploy.yaml`` declares *what should exist*, by logical name only.

Environment-independent and committed with the package. It never holds environment IDs or URLs, the IFS
host, connection IDs or credentials: those are per target (``~/.ifs-agent-ops/targets/``) or live in the
environment itself (environment variable values, connections). Example::

    power_platform:
      solution:
        unique_name: IfsCustomerOrders        # stable once deployed
        display_name: IFS Customer Orders
        publisher: ContosoAdmin                # an existing publisher's unique name
        prefix: ti                            # that publisher's customization prefix
      connectors:
        - logical_name: customer-order-handling   # one connector per IFS service
          display_name: IFS Customer Order Handling   # at most 30 characters
          source: power-platform/connectors/CustomerOrderHandling
          host_from: ti_IFSDomain             # host = @environmentVariables("ti_IFSDomain") on deploy
      environment_variables:
        - schema_name: ti_IFSDomain           # added to the solution; created only if missing
          display_name: IFSDomain
          type: string
          description: The domain of the URL for our IFS instance
      connection_references:
        - logical_name: ti_IfsCustomerOrderHandling
          display_name: IFS Customer Order Handling
          connector: customer-order-handling
      flows:                                  # generated flows (one per capability action), turned on
        - source: power-platform/flows/create-customer-order
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from ifs_agent_ops.workspace.package import Package

DEPLOY_FILE = "deploy.yaml"
VARIABLE_TYPES = ("string", "number", "boolean", "json")
MAX_CONNECTOR_DISPLAY_NAME = 30  # Power Platform refuses longer connector names on import
_SOLUTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,7}$")
_LOGICAL_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SCHEMA_TAIL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_CONNECTOR_FILES = ("apiDefinition.swagger.json", "apiProperties.json")


class IntentError(ValueError):
    pass


@dataclass
class Solution:
    unique_name: str
    display_name: str
    publisher: str
    prefix: str


@dataclass
class Connector:
    logical_name: str
    display_name: str
    source: str
    host_from: Optional[str] = None


@dataclass
class EnvironmentVariable:
    schema_name: str
    display_name: str
    type: str = "string"
    description: str = ""


@dataclass
class ConnectionReference:
    logical_name: str
    display_name: str
    connector: str


@dataclass
class Flows:
    source: str               # folder of generated flows: power-platform/flows/<capability>


@dataclass
class PowerPlatformIntent:
    solution: Solution
    connectors: list[Connector] = field(default_factory=list)
    environment_variables: list[EnvironmentVariable] = field(default_factory=list)
    connection_references: list[ConnectionReference] = field(default_factory=list)
    flows: list[Flows] = field(default_factory=list)


@dataclass
class DeployIntent:
    path: Path
    power_platform: Optional[PowerPlatformIntent] = None


def deploy_path(package: Package) -> Path:
    return package.root / DEPLOY_FILE


# ── parsing ──────────────────────────────────────────────────────────────────

def _mapping(value, where: str, keys: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict):
        raise IntentError(f"{where} must be a mapping")
    unknown = sorted(set(value) - keys)
    if unknown:
        raise IntentError(f"{where} has unknown key(s): {', '.join(unknown)}")
    missing = sorted(k for k in required if value.get(k) in (None, ""))
    if missing:
        raise IntentError(f"{where} is missing: {', '.join(missing)}")
    return {k: (str(v) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else v)
            for k, v in value.items()}


def _items(value, where: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise IntentError(f"{where} must be a list")
    return value


def parse(data, path: Path) -> DeployIntent:
    top = _mapping(data or {}, DEPLOY_FILE, {"power_platform"}, set())
    pp = top.get("power_platform")
    if pp is None:
        return DeployIntent(path)
    pp = _mapping(pp, "power_platform", {"solution", "connectors", "environment_variables",
                                         "connection_references", "flows"}, {"solution"})
    sol = Solution(**_mapping(pp["solution"], "power_platform.solution",
                              {"unique_name", "display_name", "publisher", "prefix"},
                              {"unique_name", "display_name", "publisher", "prefix"}))
    connectors = [Connector(**_mapping(c, f"power_platform.connectors[{i}]",
                                       {"logical_name", "display_name", "source", "host_from"},
                                       {"logical_name", "display_name", "source"}))
                  for i, c in enumerate(_items(pp.get("connectors"), "power_platform.connectors"))]
    variables = [EnvironmentVariable(**_mapping(v, f"power_platform.environment_variables[{i}]",
                                                {"schema_name", "display_name", "type", "description"},
                                                {"schema_name", "display_name"}))
                 for i, v in enumerate(_items(pp.get("environment_variables"), "power_platform.environment_variables"))]
    references = [ConnectionReference(**_mapping(r, f"power_platform.connection_references[{i}]",
                                                 {"logical_name", "display_name", "connector"},
                                                 {"logical_name", "display_name", "connector"}))
                  for i, r in enumerate(_items(pp.get("connection_references"),
                                               "power_platform.connection_references"))]
    flows = [Flows(**_mapping(f, f"power_platform.flows[{i}]", {"source"}, {"source"}))
             for i, f in enumerate(_items(pp.get("flows"), "power_platform.flows"))]
    return DeployIntent(path, PowerPlatformIntent(sol, connectors, variables, references, flows))


def load_intent(package: Package) -> Optional[DeployIntent]:
    """The package's deployment intent, or None when it has no deploy.yaml. Raises IntentError if malformed."""
    path = deploy_path(package)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise IntentError(f"could not parse {path}: {exc}") from exc
    return parse(data, path)


# ── validation ───────────────────────────────────────────────────────────────

def _duplicates(names: list[str]) -> list[str]:
    seen, dup = set(), []
    for n in names:
        if n.lower() in seen and n not in dup:
            dup.append(n)
        seen.add(n.lower())
    return dup


def validate(intent: DeployIntent, package: Package) -> tuple[list[str], list[str]]:
    """Check the intent against itself and the package. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    pp = intent.power_platform
    if pp is None:
        return errors, ["no power_platform section: nothing to deploy"]
    sol = pp.solution
    if not _SOLUTION_RE.match(sol.unique_name):
        errors.append(f"solution.unique_name {sol.unique_name!r}: letters, digits and '_', starting with a letter")
    if not _PREFIX_RE.match(sol.prefix) or sol.prefix.startswith("mscrm"):
        errors.append(f"solution.prefix {sol.prefix!r}: 2-8 lowercase letters or digits, starting with a letter")
    prefixed = f"{sol.prefix}_"

    connectors = {c.logical_name: c for c in pp.connectors}
    variables = {v.schema_name: v for v in pp.environment_variables}
    for kind, names in (("connector", [c.logical_name for c in pp.connectors]),
                        ("environment variable", [v.schema_name for v in pp.environment_variables]),
                        ("connection reference", [r.logical_name for r in pp.connection_references])):
        for name in _duplicates(names):
            errors.append(f"{kind} {name!r} is declared more than once")

    for c in pp.connectors:
        if not _LOGICAL_RE.match(c.logical_name):
            errors.append(f"connector {c.logical_name!r}: logical_name must be lowercase letters, digits and '-'")
        if len(c.display_name) > MAX_CONNECTOR_DISPLAY_NAME:
            errors.append(f"connector {c.logical_name!r}: display_name is {len(c.display_name)} characters "
                          f"(Power Platform allows {MAX_CONNECTOR_DISPLAY_NAME})")
        folder = (package.root / c.source).resolve()
        if package.root.resolve() not in folder.parents:
            errors.append(f"connector {c.logical_name!r}: source {c.source!r} must be a folder inside the package")
        else:
            missing = [f for f in _CONNECTOR_FILES if not (folder / f).exists()]
            if missing:
                warnings.append(f"connector {c.logical_name!r}: {', '.join(missing)} not generated yet "
                                f"(ifs-agent-ops connector generate --package {package.name})")
        if c.host_from is not None:
            var = variables.get(c.host_from)
            if var is None:
                errors.append(f"connector {c.logical_name!r}: host_from {c.host_from!r} is not a declared "
                              "environment variable")
            elif var.type != "string":
                errors.append(f"connector {c.logical_name!r}: host_from {c.host_from!r} must be a string variable")
        else:
            warnings.append(f"connector {c.logical_name!r}: no host_from, so the deployed host stays the "
                            "ifs.example placeholder")

    for v in pp.environment_variables:
        if not v.schema_name.startswith(prefixed) or not _SCHEMA_TAIL_RE.match(v.schema_name[len(prefixed):]):
            errors.append(f"environment variable {v.schema_name!r}: schema_name must be {prefixed}<Name> "
                          "(letters, digits, '_')")
        if v.type not in VARIABLE_TYPES:
            errors.append(f"environment variable {v.schema_name!r}: type must be one of {', '.join(VARIABLE_TYPES)} "
                          "(secret variables are not supported in v1)")

    for r in pp.connection_references:
        if not r.logical_name.startswith(prefixed) or not _SCHEMA_TAIL_RE.match(r.logical_name[len(prefixed):]):
            errors.append(f"connection reference {r.logical_name!r}: logical_name must be {prefixed}<Name> "
                          "(letters, digits, '_')")
        if r.connector not in connectors:
            errors.append(f"connection reference {r.logical_name!r}: connector {r.connector!r} is not declared")
    from ifs_agent_ops.generators.flow import MARKER, REPORT_FILE

    for name in _duplicates([f.source for f in pp.flows]):
        errors.append(f"flows {name!r} is declared more than once")
    for f in pp.flows:
        folder = (package.root / f.source).resolve()
        if package.root.resolve() not in folder.parents:
            errors.append(f"flows {f.source!r} must be a folder inside the package")
            continue
        report = folder / REPORT_FILE
        if not any(folder.glob("*.json")) or not report.exists() or MARKER not in report.read_text(encoding="utf-8"):
            warnings.append(f"flows {f.source!r}: not generated yet (ifs-agent-ops flow generate --package "
                            f"{package.name})")
    for c in pp.connectors:
        if not any(r.connector == c.logical_name for r in pp.connection_references):
            warnings.append(f"connector {c.logical_name!r} has no connection reference: flows could not use it "
                            "from the solution")
    return errors, warnings


# ── scaffold ─────────────────────────────────────────────────────────────────

def solution_name(package: Package) -> str:
    """Default solution unique name from the package name: ifs-customer-orders -> IfsCustomerOrders."""
    return "".join(part.capitalize() for part in package.name.split("-") if part)


def _words(camel: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", camel)


def _kebab(camel: str) -> str:
    return _words(camel).lower().replace(" ", "-")


def connector_display_name(projection: str) -> str:
    """CustomerOrderHandling -> 'IFS Customer Order Handling', kept within the 30-character limit."""
    for name in (f"IFS {_words(projection)}", f"IFS {projection}", projection):
        if len(name) <= MAX_CONNECTOR_DISPLAY_NAME:
            return name
    return projection[:MAX_CONNECTOR_DISPLAY_NAME]


def generated_projections(package: Package) -> list[str]:
    """Services with a generated connector folder; else the package's projections."""
    from ifs_agent_ops.generators.connector import MAP_FILE, connectors_root

    root = connectors_root(package)
    found = sorted(d.name for d in root.iterdir() if (d / MAP_FILE).exists()) if root.is_dir() else []
    return found or list(package.projections)


def scaffold(package: Package, publisher: str, prefix: str, host_variable: Optional[str] = None) -> str:
    """The text of a first deploy.yaml for the package (validated by the caller before writing):
    one connector and one connection reference per IFS service."""
    from ifs_agent_ops.generators.connector import connector_dir

    sol = solution_name(package)
    title = package.display_name or package.name
    title = title if title.lower().startswith("ifs") else f"IFS {title}"
    host_variable = host_variable or f"{prefix}_IFSDomain"
    projections = generated_projections(package)
    connectors = [{"logical_name": _kebab(p), "display_name": connector_display_name(p),
                   "source": connector_dir(package, p).relative_to(package.root).as_posix(),
                   "host_from": host_variable} for p in projections]
    references = [{"logical_name": f"{prefix}_Ifs{p}", "display_name": connector_display_name(p),
                   "connector": _kebab(p)} for p in projections]
    data = {"power_platform": {
        "solution": {"unique_name": sol, "display_name": title, "publisher": publisher, "prefix": prefix},
        "connectors": connectors,
        "environment_variables": [{"schema_name": host_variable, "display_name": host_variable[len(prefix) + 1:],
                                   "type": "string",
                                   "description": "The domain of the URL for our IFS instance"}],
        "connection_references": references,
    }}
    header = ("# Deployment intent: what should exist, by logical name only (ifs-agent-ops, Phase 10).\n"
              "# No environment IDs, hosts, connection IDs or credentials here: those are per target or live in\n"
              "# the environment. Check with: ifs-agent-ops deploy check --package " + package.name + "\n")
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
