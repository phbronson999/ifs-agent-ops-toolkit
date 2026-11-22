"""Deployment targets: where a package deploys to. Machine-local, never committed to a package.

``~/.ifs-agent-ops/targets/<package>/<target>.yaml``::

    power_platform:
      environment: ContosoDev                       # picked from `pac env list`
      environment_id: 00000000-0000-0000-0000-000000000000
      environment_url: https://example.crm.dynamics.com
    environment_variables:                               # optional, non-secret values; used when a variable
      ti_IFSDomain: ifs.example.com                      # is created and in the deployment settings file
    flow_trigger_users:                                  # who may call the generated flows' HTTP triggers
      - maker@contoso.example
    flow_triggers:                                       # written by deploy apply: flow name -> trigger URL
      CreateCustomerOrder_Prepare: https://...

No credentials: pac holds the sign-in, and connections (with the IFS credentials and the gateway) are made by
a person in the environment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ifs_agent_ops.profiles import toolkit_home

_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class TargetError(ValueError):
    pass


@dataclass
class Target:
    package: str
    name: str
    path: Path
    environment: str
    environment_id: str
    environment_url: str
    variables: dict[str, str] = field(default_factory=dict)
    trigger_users: list[str] = field(default_factory=list)
    flow_triggers: dict[str, str] = field(default_factory=dict)

    @property
    def settings_path(self) -> Path:
        """The deployment settings file (pac format) written by deploy apply; machine-local like the target."""
        return self.path.with_suffix(".settings.json")


def targets_dir(package: str) -> Path:
    return toolkit_home() / "targets" / package


def target_path(package: str, name: str) -> Path:
    if not _TARGET_RE.match(name):
        raise TargetError(f"target name {name!r}: lowercase letters, digits and '-'")
    return targets_dir(package) / f"{name}.yaml"


def load_target(package: str, name: str) -> Target:
    path = target_path(package, name)
    if not path.exists():
        raise TargetError(f"no target {name!r} for {package} (create it with: ifs-agent-ops targets init "
                          f"--package {package} --target {name})")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    pp = data.get("power_platform") or {}
    missing = [k for k in ("environment", "environment_id", "environment_url") if not pp.get(k)]
    if missing:
        raise TargetError(f"{path}: power_platform is missing {', '.join(missing)}")
    variables = data.get("environment_variables") or {}
    if not isinstance(variables, dict):
        raise TargetError(f"{path}: environment_variables must be a mapping of schema name to value")
    users = data.get("flow_trigger_users") or []
    if not isinstance(users, list) or not all(isinstance(u, str) and "@" in u for u in users):
        raise TargetError(f"{path}: flow_trigger_users must be a list of sign-in names (user@domain)")
    triggers = data.get("flow_triggers") or {}
    if not isinstance(triggers, dict):
        raise TargetError(f"{path}: flow_triggers must be a mapping of flow name to trigger URL")
    return Target(package, name, path, str(pp["environment"]), str(pp["environment_id"]),
                  str(pp["environment_url"]).rstrip("/"), {str(k): str(v) for k, v in variables.items()},
                  [u.strip() for u in users], {str(k): str(v) for k, v in triggers.items()})


def record_flow_triggers(target: Target, triggers: dict[str, str]) -> bool:
    """Write the deployed flows' trigger URLs into the target file (keeps its header comments). Returns True
    when the file changed."""
    if triggers == target.flow_triggers:
        return False
    text = target.path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    header = "".join(ln for ln in lines[:next((i for i, ln in enumerate(lines) if not ln.startswith("#")),
                                              len(lines))])
    data = yaml.safe_load(text) or {}
    data["flow_triggers"] = dict(sorted(triggers.items()))
    target.path.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")
    target.flow_triggers = dict(triggers)
    return True


def list_targets(package: str) -> list[str]:
    folder = targets_dir(package)
    return sorted(p.stem for p in folder.glob("*.yaml")) if folder.is_dir() else []


def write_target(package: str, name: str, environment: dict, variables: dict[str, str] | None = None) -> Path:
    """Write a target from one `pac env list --json` entry. Refuses to overwrite."""
    path = target_path(package, name)
    if path.exists():
        raise TargetError(f"{path} already exists; edit it or delete it first")
    data = {"power_platform": {"environment": environment["FriendlyName"],
                               "environment_id": environment["EnvironmentIdentifier"]["Id"],
                               "environment_url": environment["EnvironmentUrl"].rstrip("/")},
            "environment_variables": dict(variables or {})}
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# Deployment target {name!r} for package {package} (ifs-agent-ops). Machine-local: never commit.\n"
              "# environment_variables: optional non-secret values, used when a variable is created and in the\n"
              "# deployment settings file. Credentials never go here.\n")
    path.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")
    return path


def match_environment(environments: list[dict], wanted: str) -> dict:
    """Find an environment from `pac env list --json` by display name, id or URL (case-insensitive)."""
    w = wanted.strip().rstrip("/").lower()
    for env in environments:
        keys = {env.get("FriendlyName", "").lower(), env.get("EnvironmentIdentifier", {}).get("Id", "").lower(),
                env.get("EnvironmentUrl", "").rstrip("/").lower(), env.get("UniqueName", "").lower()}
        if w in keys:
            return env
    names = ", ".join(e.get("FriendlyName", "?") for e in environments)
    raise TargetError(f"no environment matches {wanted!r}; available: {names}")
