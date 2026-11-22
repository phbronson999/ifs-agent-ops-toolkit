"""Capability-based readiness checks (``ifs-agent-ops doctor``).

Doctor inspects and reports; it never installs software, signs in, creates resources or changes
configuration. Each check belongs to a *capability* (what the machine is being used for), so a
machine that only records and analyses IFS workflows is not failed for missing Power Platform or
SPFx tooling.

Results: PASS, WARN (works, but something needs attention), FAIL (the capability will not work),
NOT REQUIRED (capability not requested). Every non-PASS result carries a concrete fix.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

PASS, WARN, FAIL, NOT_REQUIRED = "PASS", "WARN", "FAIL", "NOT REQUIRED"
CORE = ("toolkit", "ifs", "packages")
OPTIONAL = ("power-platform", "agents", "spfx")
CAPABILITIES = CORE + OPTIONAL
MIN_PYTHON = (3, 11)
NODE_FOR_AGENTS = ((20, 19), (22, 12))  # Agents Toolkit CLI: 20.19+ on 20.x, or 22.12+


@dataclass
class Check:
    capability: str
    name: str
    status: str
    detail: str
    fix: str = ""


def _run(cmd: list[str], timeout: float = 20.0) -> Optional[str]:
    exe = shutil.which(cmd[0])
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (out.stdout or "") + (out.stderr or "")


def _version_tuple(text: str) -> Optional[tuple[int, ...]]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


# ── toolkit ─────────────────────────────────────────────────────────────────

def check_toolkit() -> list[Check]:
    c = "toolkit"
    out: list[Check] = []
    py = sys.version_info[:3]
    out.append(Check(c, "python", PASS if py[:2] >= MIN_PYTHON else FAIL,
                     f"Python {'.'.join(map(str, py))} ({sys.executable})",
                     "" if py[:2] >= MIN_PYTHON else "install Python 3.11+ and recreate the virtual environment"))
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    out.append(Check(c, "virtual environment", PASS if in_venv else WARN,
                     "active" if in_venv else "running outside a virtual environment",
                     "" if in_venv else "python -m venv .venv && .venv/Scripts/pip install -e .[dev]"))
    try:
        out.append(Check(c, "toolkit package", PASS, f"ifs-agent-ops-toolkit {version('ifs-agent-ops-toolkit')}"))
    except PackageNotFoundError:
        out.append(Check(c, "toolkit package", FAIL, "not installed", "pip install -e . (from the toolkit clone)"))

    def dep(name: str, dist: str, ok: Callable[[tuple], bool] = lambda v: True, rule: str = "") -> None:
        try:
            v = version(dist)
        except PackageNotFoundError:
            out.append(Check(c, f"dependency {name}", FAIL, "missing", "pip install -e . to restore dependencies"))
            return
        good = ok(_version_tuple(v) or (0,))
        out.append(Check(c, f"dependency {name}", PASS if good else FAIL, f"{dist} {v}",
                         "" if good else f"requires {rule}; pip install -e . to restore pinned versions"))

    dep("mcp", "mcp", lambda v: v[0] < 2, "mcp < 2 (SDK 2.x renamed FastMCP)")
    dep("httpx", "httpx")
    dep("pydantic", "pydantic", lambda v: v[0] >= 2, "pydantic >= 2")
    dep("pyyaml", "PyYAML")
    dep("truststore", "truststore")

    git = _run(["git", "--version"])
    out.append(Check(c, "git", PASS if git else WARN, (git or "not found").strip(),
                     "" if git else "install Git (winget install --id Git.Git); packages are git repositories"))

    from ifs_agent_ops.profiles import toolkit_home
    home = toolkit_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        out.append(Check(c, "toolkit home", PASS, f"{home} (writable)"))
    except OSError as exc:
        out.append(Check(c, "toolkit home", FAIL, f"{home}: {exc}", "set IFS_AGENT_OPS_HOME to a writable folder"))
    return out


# ── ifs ─────────────────────────────────────────────────────────────────────

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def check_ifs(online: bool = True) -> list[Check]:
    from ifs_agent_ops import profiles as prof
    from ifs_agent_ops.ifs.auth.token_store import FileTokenStore

    c = "ifs"
    out: list[Check] = []
    try:
        profile = prof.resolve_profile()
    except prof.ProfileError as exc:
        return [Check(c, "profile", FAIL, str(exc), f"fix {prof.profiles_file()} or unset IFS_PROFILE")]
    source = ".env/environment" if profile.source == "env" else "profiles.toml"
    out.append(Check(c, "profile", PASS, f"'{profile.name}' from {source}"))
    missing = [k for k in ("base_url", "realm", "client_id") if not getattr(profile, k)]
    if missing:
        fix = ("copy .env.example to .env and set API_BASE_URL, OAUTH_REALM, OAUTH_CLIENT_ID"
               if profile.source == "env" else f"complete [profiles.{profile.name}] in {prof.profiles_file()}")
        out.append(Check(c, "profile settings", FAIL, "missing: " + ", ".join(missing), fix))
        return out
    host = urlsplit(profile.base_url).hostname or ""
    out.append(Check(c, "profile settings", PASS if profile.base_url.startswith("https://") else WARN,
                     f"host {host}, realm {profile.realm}, callback port {profile.callback_port}",
                     "" if profile.base_url.startswith("https://") else "use an https:// base URL"))

    verify = profile.verify()
    tls_detail = ("verification OFF" if verify is False else
                  f"CA bundle {verify}" if isinstance(verify, str) else "OS trust store")
    out.append(Check(c, "tls setting", WARN if verify is False else PASS, tls_detail,
                     "remove IFS_TLS_VERIFY=false / tls_verify = false" if verify is False else ""))

    if online:
        import httpx
        try:
            httpx.get(profile.base_url.rstrip("/") + "/", verify=verify, timeout=15.0)
            out.append(Check(c, "reachability", PASS, f"https://{host} answered (TLS verified)"
                             if verify is not False else f"https://{host} answered"))
        except httpx.ConnectError as exc:
            msg = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in msg:
                out.append(Check(c, "reachability", FAIL, "certificate not trusted",
                                 "trust the issuing CA in the OS store, or set IFS_CA_BUNDLE / ca_bundle"))
            else:
                out.append(Check(c, "reachability", FAIL, f"cannot connect: {msg[:120]}",
                                 "check VPN/network and the base URL"))
        except httpx.HTTPError as exc:
            out.append(Check(c, "reachability", FAIL, f"{type(exc).__name__}: {str(exc)[:120]}",
                             "check VPN/network and the base URL"))
    else:
        out.append(Check(c, "reachability", NOT_REQUIRED, "skipped (--offline)"))

    store = FileTokenStore(prof.session_file(profile.name))
    sid = store.current_session_id()
    store.load()
    data = store.get(sid) if sid else None
    if data is None:
        out.append(Check(c, "sign-in", WARN, "not signed in",
                         "call the start_oauth MCP tool (opens the IFS login in your browser)"))
    else:
        left = data.expires_at - int(time.time() * 1000)
        if left > 0:
            out.append(Check(c, "sign-in", PASS, f"signed in ({left // 60000} min left)"))
        elif data.refresh_token:
            out.append(Check(c, "sign-in", PASS, "access token expired; refreshes automatically on next call"))
        else:
            out.append(Check(c, "sign-in", WARN, "session expired and cannot refresh", "call start_oauth to sign in"))

    free = _port_free(profile.callback_port)
    out.append(Check(c, "oauth callback port", PASS if free else WARN,
                     f"127.0.0.1:{profile.callback_port} " + ("free" if free else "in use"),
                     "" if free else "fine if this toolkit's MCP server is running; otherwise another process "
                                     "holds the port and IFS sign-in cannot complete"))
    return out


# ── packages ────────────────────────────────────────────────────────────────

def check_packages() -> list[Check]:
    from ifs_agent_ops.ifs.metadata.snapshot import snapshot_dir
    from ifs_agent_ops.workspace.discovery import ENV_VAR, discover_packages
    import yaml

    c = "packages"
    if not os.environ.get(ENV_VAR):
        return [Check(c, "configuration", WARN, f"{ENV_VAR} is not set (no packages discovered)",
                      f"set {ENV_VAR} to your package folders (see .env.example), or create one with "
                      "`ifs-agent-ops package init`")]
    packages, problems = discover_packages()
    out = [Check(c, "configuration", PASS if packages else WARN, f"{len(packages)} package(s) discovered",
                 "" if packages else f"check the paths in {ENV_VAR}")]
    out += [Check(c, "discovery", FAIL, p, f"fix the path or manifest named in {ENV_VAR}") for p in problems]

    for pkg in packages:
        label = f"{pkg.name}"
        gi = pkg.root / ".gitignore"
        ignored = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        missing = [x for x in ("*.raw.har", ".ifs-agent-ops/") if x not in ignored]
        out.append(Check(c, f"{label}: .gitignore", WARN if missing else PASS,
                         "missing " + ", ".join(missing) if missing else "raw captures and local state excluded",
                         "add the missing lines to the package .gitignore" if missing else ""))

        tracked = _run(["git", "-C", str(pkg.root), "ls-files"])
        if tracked is None or "not a git repository" in (tracked or ""):
            out.append(Check(c, f"{label}: git", WARN, "not a git repository", f"git init {pkg.root}"))
            tracked_files: list[str] = []
        else:
            tracked_files = tracked.splitlines()
            out.append(Check(c, f"{label}: git", PASS, f"{len(tracked_files)} tracked file(s)"))

        bad = []
        hars = sorted(pkg.root.rglob("*.har"))
        for har in hars:
            rel = har.relative_to(pkg.root).as_posix()
            notes = har.with_suffix(".annotations.yaml")
            sanitized = False
            if rel.startswith("recordings/") and notes.exists():
                data = yaml.safe_load(notes.read_text(encoding="utf-8")) or {}
                sanitized = bool((data.get("evidence") or {}).get("sanitized"))
            if not sanitized:
                bad.append(rel)
        out.append(Check(c, f"{label}: recordings", FAIL if bad else PASS,
                         f"{len(bad)} HAR file(s) not imported through `record import`: {', '.join(bad[:3])}"
                         if bad else (f"{len(hars)} sanitized recording(s)" if hars else "no recordings yet"),
                         "remove them; import raw captures with `ifs-agent-ops record import`" if bad else ""))

        used = set()
        for rec in pkg.recordings_dir.rglob("*.har") if pkg.recordings_dir.is_dir() else []:
            used.update(re.findall(r"/projection/v1/([A-Za-z0-9_]+)\.svc/", rec.read_text(encoding="utf-8",
                                                                                         errors="replace")))
        used &= set(pkg.projections)
        missing_snaps = sorted(p for p in used if not (snapshot_dir(pkg, p) / "metadata.xml").exists())
        out.append(Check(c, f"{label}: metadata snapshots", WARN if missing_snaps else PASS,
                         "missing for " + ", ".join(missing_snaps) if missing_snaps
                         else f"{len(used)} projection(s) with recordings have snapshots",
                         f"ifs-agent-ops metadata snapshot --package {pkg.name} --projection <name>"
                         if missing_snaps else ""))

        from ifs_agent_ops.operations.store import load_operations
        from ifs_agent_ops.operations.validate import validate_all
        try:
            ops = load_operations(pkg)
        except Exception as exc:  # malformed YAML or schema violation
            out.append(Check(c, f"{label}: operations", FAIL, f"cannot load: {str(exc)[:120]}",
                             "fix the operation file named in the error"))
            continue
        errors = [f for f in validate_all(ops) if f.severity == "error"]
        approved = sum(1 for o in ops.values() if o.status == "approved")
        out.append(Check(c, f"{label}: operations", WARN if errors else PASS,
                         f"{len(ops)} operation(s), {approved} approved, {len(errors)} validation error(s)",
                         f"ifs-agent-ops operations validate --package {pkg.name}" if errors else ""))
    return out


# ── optional capabilities ───────────────────────────────────────────────────

def check_power_platform() -> list[Check]:
    c = "power-platform"
    out = []
    pac = _run(["pac", "help"])
    if pac is None:
        out.append(Check(c, "pac", FAIL, "Power Platform CLI not found",
                         "winget install Microsoft.PowerAppsCLI, then pac install latest (or: dotnet tool install --global "
                         "Microsoft.PowerApps.CLI.Tool)"))
    else:
        v = re.search(r"Version:\s*([\d.]+\S*)", pac)
        out.append(Check(c, "pac", PASS, f"Power Platform CLI {v.group(1) if v else '(version unknown)'}"))
        auth = _run(["pac", "auth", "list"]) or ""
        has_profile = bool(re.search(r"^\s*\[\d+\]", auth, re.M))
        out.append(Check(c, "pac auth", PASS if has_profile else WARN,
                         "auth profile(s) present" if has_profile else "no auth profile",
                         "" if has_profile else "pac auth create (interactive sign-in)"))
    dotnet = _run(["dotnet", "--version"])
    out.append(Check(c, "dotnet", PASS if dotnet else WARN, (dotnet or "not found").strip(),
                     "" if dotnet else "needed only for the .NET tool install of pac (.NET 10 recommended)"))
    out += _deployment_targets(c, pac is not None)
    return out


def _deployment_targets(c: str, have_pac: bool) -> list[Check]:
    """Packages with a deploy.yaml: each needs a target, and each target's environment must be visible to pac."""
    import json

    from ifs_agent_ops.deploy.intent import IntentError, load_intent
    from ifs_agent_ops.deploy.targets import TargetError, list_targets, load_target
    from ifs_agent_ops.workspace.discovery import discover_packages

    packages = []
    for pkg in discover_packages()[0]:
        try:
            intent = load_intent(pkg)
        except IntentError as exc:
            packages.append((pkg, None, str(exc)))
            continue
        if intent is not None and intent.power_platform is not None:
            packages.append((pkg, intent, ""))
    if not packages:
        return [Check(c, "deployment targets", NOT_REQUIRED, "no package declares Power Platform deployment "
                      "(deploy.yaml)")]
    visible: Optional[set[str]] = None
    if have_pac:
        text = _run(["pac", "env", "list", "--json"], timeout=60) or ""
        try:
            envs = json.loads(text[text.index("["):text.rindex("]") + 1])
            visible = {e.get("EnvironmentIdentifier", {}).get("Id", "").lower() for e in envs}
        except ValueError:
            visible = None
    out = []
    for pkg, intent, problem in packages:
        if intent is None:
            out.append(Check(c, f"{pkg.name} deploy.yaml", FAIL, problem,
                             f"ifs-agent-ops deploy check --package {pkg.name}"))
            continue
        names = list_targets(pkg.name)
        if not names:
            out.append(Check(c, f"{pkg.name} targets", WARN, "no deployment target",
                             f"ifs-agent-ops targets init --package {pkg.name} --target dev"))
        for name in names:
            label = f"{pkg.name} target {name}"
            try:
                t = load_target(pkg.name, name)
            except TargetError as exc:
                out.append(Check(c, label, FAIL, str(exc), f"fix or recreate {name} with targets init"))
                continue
            if visible is None:
                out.append(Check(c, label, WARN, f"{t.environment}: not checked (pac unavailable or not signed in)",
                                 "pac auth create"))
            elif t.environment_id.lower() in visible:
                out.append(Check(c, label, PASS, f"{t.environment} reachable"))
            else:
                out.append(Check(c, label, FAIL, f"{t.environment} is not visible to the signed-in pac profile",
                                 "pac auth create (an account with access), or recreate the target"))
    return out


def _node() -> tuple[Optional[str], Optional[tuple[int, ...]]]:
    text = _run(["node", "--version"])
    return (text.strip() if text else None), _version_tuple(text or "")


def check_agents() -> list[Check]:
    c = "agents"
    out = []
    text, v = _node()
    if v is None:
        out.append(Check(c, "node", FAIL, "Node.js not found", "install Node.js 22 LTS (winget install OpenJS.NodeJS.LTS)"))
    else:
        ok = (v[0] == 20 and v[:2] >= NODE_FOR_AGENTS[0]) or (v[0] >= 22 and (v[0] > 22 or v[:2] >= NODE_FOR_AGENTS[1]))
        out.append(Check(c, "node", PASS if ok else FAIL, f"Node.js {text}",
                         "" if ok else "Agents Toolkit CLI needs Node 20.19+ or 22.12+"))
    atk = _run(["atk", "--version"])
    out.append(Check(c, "atk", PASS if atk else FAIL, f"Agents Toolkit CLI {atk.strip()}" if atk else "not found",
                     "" if atk else "npm install -g @microsoft/m365agentstoolkit-cli"))
    out.append(Check(c, "agent targets", NOT_REQUIRED, "sign-in and environment checks arrive with Phase 13"))
    return out


def check_spfx() -> list[Check]:
    text, v = _node()
    return [Check("spfx", "node", PASS if v else WARN, f"Node.js {text}" if v else "Node.js not found",
                  "" if v else "SPFx projects pin their own Node version; install per that project"),
            Check("spfx", "spfx toolchain", NOT_REQUIRED,
                  "owned by consuming SPFx projects, not the toolkit (check inside that project)")]


CHECKS: dict[str, Callable[..., list[Check]]] = {
    "toolkit": check_toolkit, "ifs": check_ifs, "packages": check_packages,
    "power-platform": check_power_platform, "agents": check_agents, "spfx": check_spfx,
}


def run(requested: Optional[list[str]] = None, *, online: bool = True) -> list[Check]:
    wanted = set(requested or CORE)
    results: list[Check] = []
    for cap in CAPABILITIES:
        if cap not in wanted:
            results.append(Check(cap, "-", NOT_REQUIRED, "not requested (use --for " + cap + ")"))
            continue
        try:
            checks = CHECKS[cap](online=online) if cap == "ifs" else CHECKS[cap]()
        except Exception as exc:  # a bug in a check must not hide the others
            checks = [Check(cap, "doctor", FAIL, f"check crashed: {type(exc).__name__}: {exc}",
                            "report this as a toolkit bug")]
        results.extend(checks)
    return results


def render(results: list[Check]) -> str:
    lines = []
    current = None
    width = max((len(r.name) for r in results), default=10) + 2
    for r in results:
        if r.capability != current:
            current = r.capability
            lines.append(f"\n[{current}]")
        lines.append(f"  {r.status:<12} {r.name:<{width}} {r.detail}")
        if r.fix and r.status in (WARN, FAIL):
            lines.append(f"  {'':<12} {'':<{width}} fix: {r.fix}")
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, WARN, FAIL)}
    lines.append(f"\n{counts[PASS]} passed, {counts[WARN]} warning(s), {counts[FAIL]} failed")
    return "\n".join(lines).lstrip("\n")


def as_dicts(results: list[Check]) -> list[dict]:
    return [asdict(r) for r in results]


def exit_code(results: list[Check]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0
