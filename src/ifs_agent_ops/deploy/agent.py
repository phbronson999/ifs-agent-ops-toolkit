"""Agent adapter (Phase 13): render the generated agent project for one target, machine-local.

``render`` copies ``<package>/agent/m365-agent/`` to ``~/.ifs-agent-ops/agents/<package>/<target>/`` and fills in
the target's values: the flows' server and trigger paths (recorded by ``deploy apply``), the sign-in tenant, and an
``env/.env.<target>`` for the Agents Toolkit. Values the toolkit writes during provision (``TEAMS_APP_ID``, the OAuth
registration id, title ids, share link) are kept across renders. Then a person runs ``atk provision --env <target>``
in that folder: it signs in interactively and asks for the Entra app's client id and secret for the OAuth
registration (the secret is never stored by ifs-agent-ops).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from ifs_agent_ops.deploy.targets import Target
from ifs_agent_ops.generators.agent import (REGISTRATION_VAR, REPORT_FILE, SERVER_TOKEN, SPEC_FILE, TENANT_TOKEN,
                                            agent_root, generate, path_token)
from ifs_agent_ops.profiles import toolkit_home
from ifs_agent_ops.workspace.package import Package

DEVELOPER_DEFAULTS = {"name": "IFS Agent Ops", "email": "publisher-email@example.com",
                      "website_url": "https://www.example.com", "privacy_url": "https://www.example.com/privacy",
                      "terms_url": "https://www.example.com/termsofuse"}
MANAGED = ("TEAMSFX_ENV", "APP_NAME_SUFFIX", "AGENT_SCOPE", "APP_VERSION", "APP_CONTENT_SHA256", "DEVELOPER_NAME", "DEVELOPER_EMAIL",
           "DEVELOPER_WEBSITE_URL", "DEVELOPER_PRIVACY_URL", "DEVELOPER_TERMS_URL")


class AgentError(RuntimeError):
    pass


@dataclass
class Rendered:
    folder: Path
    env_file: Path
    provisioned: bool          # the env file already has a Teams app id and an OAuth registration id


def work_dir(package: Package, target: Target) -> Path:
    return toolkit_home() / "agents" / package.name / target.name


def tenant_id(target: Target) -> str:
    if target.agent.get("tenant_id"):
        return str(target.agent["tenant_id"])
    az = shutil.which("az") or shutil.which("az.cmd")
    if az:
        proc = subprocess.run([az, "account", "show", "--query", "tenantId", "-o", "tsv"], capture_output=True,
                              text=True, timeout=60, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    raise AgentError(f"no tenant id: add agent.tenant_id to {target.path}, or sign in with az login")


def _read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
    return out


def _version_tuple(text: str) -> tuple[int, int, int]:
    parts = [int(p) for p in re.findall(r"\d+", text or "")][:3]
    return tuple(parts + [0] * (3 - len(parts)))  # type: ignore[return-value]


def _next_version(folder: Path, env: dict[str, str]) -> tuple[str, str]:
    """The app version for this render. Publishing an update needs a higher version every time, so the version goes
    up by one patch whenever the rendered app package changed since the last render (its content hash, the version
    itself left out); it follows the generated version when that is higher, and it never goes down."""
    manifest = json.loads((folder / "appPackage" / "manifest.json").read_text(encoding="utf-8"))
    base = _version_tuple(manifest.get("version", "1.0.0"))
    h = hashlib.sha256()
    for path in sorted(p for p in (folder / "appPackage").rglob("*") if p.is_file() and "build" not in p.parts):
        data = path.read_bytes()
        if path.name == "manifest.json":
            data = json.dumps({**manifest, "version": None}, sort_keys=True).encode("utf-8")
        h.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0" + data + b"\0")
    digest = h.hexdigest()
    previous = env.get("APP_VERSION")
    if not previous:
        version = base
    else:
        prev = _version_tuple(previous)
        bumped = (prev[0], prev[1], prev[2] + 1) if digest != env.get("APP_CONTENT_SHA256") else prev
        version = max(base, bumped)
    return ".".join(map(str, version)), digest


def render(package: Package, target: Target, tenant: Optional[str] = None) -> Rendered:
    source = agent_root(package)
    stale = [r for r in generate(package, check=True).results if r.action != "unchanged"]
    if stale:
        raise AgentError("the generated agent is not current; run: ifs-agent-ops agent generate --package "
                         f"{package.name}\n  " + "\n  ".join(f"{r.action} {r.path}" for r in stale))
    spec = (source / SPEC_FILE).read_text(encoding="utf-8")
    flows = sorted(set(re.findall(r"%%TRIGGER_PATH:([A-Za-z0-9_]+)%%", spec)))
    missing = [f for f in flows if f not in target.flow_triggers]
    if missing:
        raise AgentError(f"no trigger URL for {', '.join(missing)} in {target.path} (ifs-agent-ops deploy apply "
                         "records them)")
    hosts = set()
    for f in flows:
        url = urlsplit(target.flow_triggers[f])
        default_port = (url.scheme == "https" and url.port == 443) or (url.scheme == "http" and url.port == 80)
        hosts.add(f"{url.scheme}://{url.hostname if default_port else url.netloc}")   # as in the proven agent
        if parse_qs(url.query).get("sig"):
            raise AgentError(f"{f}: the trigger URL carries a SAS signature; the agent needs a trigger restricted "
                             "to signed-in users (flow_trigger_users)")
        spec = spec.replace(path_token(f), url.path)
    if len(hosts) != 1:
        raise AgentError(f"the flows' trigger URLs use {len(hosts)} hosts; one API description needs one server")
    spec = spec.replace(SERVER_TOKEN, hosts.pop()).replace(TENANT_TOKEN, tenant or tenant_id(target))

    folder = work_dir(package, target)
    folder.mkdir(parents=True, exist_ok=True)
    keep = {"env", "appPackage/build", ".gitignore"}
    for path in sorted(folder.rglob("*"), reverse=True):        # drop files the package no longer generates
        rel = path.relative_to(folder).as_posix()
        if path.is_file() and not any(rel == k or rel.startswith(k + "/") for k in keep):
            path.unlink()
    for path in source.rglob("*"):
        if path.is_file() and path.name != REPORT_FILE:
            dest = folder / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    (folder / SPEC_FILE).write_text(spec, encoding="utf-8", newline="\n")

    env_file = folder / "env" / f".env.{target.name}"
    env = _read_env(env_file)
    version, digest = _next_version(folder, env)
    manifest_path = folder / "appPackage" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                             newline="\n")
    env.update({"APP_VERSION": version, "APP_CONTENT_SHA256": digest})
    dev = {**DEVELOPER_DEFAULTS, **(target.agent.get("developer") or {})}
    env.update({"TEAMSFX_ENV": target.name, "APP_NAME_SUFFIX": str(target.agent.get("app_name_suffix", "")),
                "AGENT_SCOPE": str(target.agent.get("scope", "personal")),
                "DEVELOPER_NAME": dev["name"], "DEVELOPER_EMAIL": dev["email"],
                "DEVELOPER_WEBSITE_URL": dev["website_url"], "DEVELOPER_PRIVACY_URL": dev["privacy_url"],
                "DEVELOPER_TERMS_URL": dev["terms_url"]})
    env_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Agents Toolkit environment '{target.name}' for {package.name} (rendered by ifs-agent-ops; "
             "machine-local). Values below the managed block are written by atk during provision."]
    lines += [f"{k}={env[k]}" for k in MANAGED]
    lines += [f"{k}={v}" for k, v in env.items() if k not in MANAGED]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    (folder / ".gitignore").write_text("env/.env.*.user\nappPackage/build/\n", encoding="utf-8", newline="\n")
    return Rendered(folder, env_file, bool(env.get("TEAMS_APP_ID") and env.get(REGISTRATION_VAR)))
