"""Install missing local tools per capability (``ifs-agent-ops bootstrap``).

Bootstrap changes only the local machine, and only on request: it lists the tools a capability
needs, shows the exact install command for each missing one and runs it after the user confirms it
(or with ``--yes``). It never signs in, creates cloud resources or edits toolkit configuration;
sign-ins are listed as next steps and ``doctor`` checks them.

Installers are winget (Windows) and npm (global packages, after Node.js). Where winget is not
available, bootstrap prints what to install instead of guessing a package manager.

A freshly installed tool is usually not on the PATH of the terminal (or VS Code) that ran the
installer. Bootstrap re-reads the PATH from the registry to confirm the install, uses that PATH for
later steps (npm right after Node.js), and says when a new terminal is needed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from ifs_agent_ops.doctor import CAPABILITIES, CORE


@dataclass(frozen=True)
class Tool:
    key: str
    label: str
    capabilities: tuple[str, ...]
    probe: tuple[str, ...]                    # executables; the tool is present when the first is found
    winget_id: str = ""                       # installed with winget when set
    npm: tuple[str, ...] = ()                 # otherwise installed with npm install -g <these>
    needs: tuple[str, ...] = ()               # tools that must be present first
    manual: str = ""                          # where to get it when winget is unavailable
    next_steps: tuple[str, ...] = ()          # sign-in or setup the user does afterwards
    present_check: tuple[str, ...] | None = None  # extra command that must succeed (e.g. an npm package)


TOOLS: tuple[Tool, ...] = (
    Tool("git", "Git", ("toolkit", "packages"), ("git",), winget_id="Git.Git",
         manual="https://git-scm.com/downloads",
         next_steps=("set user.name/user.email per repository (git config, not --global)",)),
    Tool("node", "Node.js LTS", ("agents", "spfx"), ("node", "npm"), winget_id="OpenJS.NodeJS.LTS",
         manual="https://nodejs.org (LTS)"),
    Tool("pac", "Power Platform CLI", ("power-platform",), ("pac",), winget_id="Microsoft.PowerAppsCLI",
         manual="https://learn.microsoft.com/power-platform/developer/cli/introduction",
         next_steps=("pac auth create (sign in to the Power Platform tenant)",)),
    Tool("az", "Azure CLI", ("power-platform",), ("az",), winget_id="Microsoft.AzureCLI",
         manual="https://learn.microsoft.com/cli/azure/install-azure-cli",
         next_steps=("az login --allow-no-subscriptions (used to read deployed flows and call their triggers)",)),
    Tool("atk", "Microsoft 365 Agents Toolkit CLI", ("agents",), ("atk",),
         npm=("@microsoft/m365agentstoolkit-cli",), needs=("node",),
         next_steps=("atk auth login m365 (before atk provision)",)),
    Tool("spfx", "SPFx project generator (Yeoman)", ("spfx",), ("yo",),
         npm=("yo", "@microsoft/generator-sharepoint"), needs=("node",),
         present_check=("npm", "ls", "-g", "@microsoft/generator-sharepoint"),
         next_steps=("existing SPFx projects build with their own toolchain: npm ci in the project folder",)),
)


def tools_for(capabilities: list[str]) -> list[Tool]:
    wanted = set(capabilities)
    return [t for t in TOOLS if wanted & set(t.capabilities)]


# ── environment (patched in tests) ─────────────────────────────────────────

def _registry_path() -> str | None:
    """The PATH a new terminal would get (machine + user, from the registry); None off Windows."""
    if sys.platform != "win32":
        return None
    import winreg

    parts = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, r"Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
        except OSError:
            continue
    return os.pathsep.join(p for p in parts if p) or None


def _which(cmd: str, path: str | None = None) -> str | None:
    return shutil.which(cmd, path=path)


def _call(cmd: list[str], path: str | None = None) -> int:
    env = dict(os.environ)
    if path:
        env["PATH"] = path
    exe = _which(cmd[0], path) or cmd[0]
    try:
        return subprocess.run([exe, *cmd[1:]], check=False, env=env).returncode
    except OSError:
        return 127


def _quiet(cmd: list[str], path: str | None = None) -> bool:
    exe = _which(cmd[0], path)
    if exe is None:
        return False
    env = dict(os.environ, PATH=path) if path else None
    try:
        return subprocess.run([exe, *cmd[1:]], check=False, capture_output=True, env=env, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ── plan and run ───────────────────────────────────────────────────────────

@dataclass
class Outcome:
    tool: Tool
    status: str  # present | not-on-path | installed | installed-new-terminal | declined | failed | skipped | manual | planned
    detail: str = ""


@dataclass
class Session:
    path: str | None = None                 # PATH used for probes and installs (refreshed after installs)
    present: dict[str, bool] = field(default_factory=dict)


def _present(tool: Tool, path: str | None) -> bool:
    if _which(tool.probe[0], path) is None:
        return False
    return _quiet(list(tool.present_check), path) if tool.present_check else True


def install_command(tool: Tool) -> list[str]:
    if tool.winget_id:
        return ["winget", "install", "--id", tool.winget_id, "--exact", "--source", "winget",
                "--accept-source-agreements", "--accept-package-agreements"]
    return ["npm", "install", "--global", *tool.npm]


def run(capabilities: list[str], *, yes: bool = False, dry_run: bool = False,
        confirm: Callable[[str], bool] = lambda q: input(q + " [y/N] ").strip().lower() in ("y", "yes"),
        out: Callable[[str], None] = print) -> list[Outcome]:
    s = Session(path=os.environ.get("PATH"))
    outcomes: list[Outcome] = []
    have_winget = _which("winget", s.path) is not None
    for tool in tools_for(capabilities):
        if _present(tool, s.path):
            s.present[tool.key] = True
            outcomes.append(Outcome(tool, "present", _which(tool.probe[0], s.path) or ""))
            continue
        fresh = _registry_path()
        if fresh and _present(tool, fresh):
            s.present[tool.key] = True
            outcomes.append(Outcome(tool, "not-on-path",
                                    "installed, but not on this terminal's PATH: open a new terminal (restart VS Code)"))
            continue
        missing_needs = [n for n in tool.needs if not s.present.get(n)]
        if missing_needs:
            outcomes.append(Outcome(tool, "skipped", "needs " + ", ".join(missing_needs) + " first"))
            continue
        if tool.winget_id and not have_winget:
            outcomes.append(Outcome(tool, "manual", f"winget not available; install from {tool.manual}"))
            continue
        cmd = install_command(tool)
        if dry_run:
            outcomes.append(Outcome(tool, "planned", " ".join(cmd)))
            continue
        out(f"\n{tool.label} is missing.\n  {' '.join(cmd)}")
        if not (yes or confirm(f"Install {tool.label}?")):
            outcomes.append(Outcome(tool, "declined", " ".join(cmd)))
            continue
        code = _call(cmd, s.path)
        refreshed = _registry_path()
        if refreshed:
            s.path = os.pathsep.join(p for p in (refreshed, os.environ.get("PATH", "")) if p)
        if code != 0 and not _present(tool, s.path):
            outcomes.append(Outcome(tool, "failed", f"exit code {code}: {' '.join(cmd)}"))
            continue
        s.present[tool.key] = True
        on_old_path = _which(tool.probe[0], os.environ.get("PATH")) is not None
        outcomes.append(Outcome(tool, "installed" if on_old_path else "installed-new-terminal",
                                "" if on_old_path else "open a new terminal (restart VS Code) to use it"))
    return outcomes


LABELS = {"present": "present", "not-on-path": "NOT ON PATH", "installed": "INSTALLED",
          "installed-new-terminal": "INSTALLED",
          "declined": "DECLINED", "failed": "FAILED", "skipped": "SKIPPED", "manual": "MANUAL", "planned": "TO INSTALL"}


def render(capabilities: list[str], outcomes: list[Outcome]) -> str:
    lines = [f"bootstrap for: {', '.join(capabilities)}"]
    if not outcomes:
        lines.append("  nothing to install for these capabilities")
    width = max((len(o.tool.label) for o in outcomes), default=10) + 2
    for o in outcomes:
        lines.append(f"  {LABELS[o.status]:<11} {o.tool.label:<{width}} {o.detail}".rstrip())
    steps = [s for o in outcomes if o.status != "present" or o.tool.key in ("pac", "az", "atk")
             for s in o.tool.next_steps]
    if steps:
        lines.append("\nSign-ins and setup (bootstrap doesn't do these; doctor checks them):")
        lines += [f"  - {s}" for s in dict.fromkeys(steps)]
    if any(o.status in ("installed-new-terminal", "not-on-path") for o in outcomes):
        lines.append("\nOpen a new terminal (or restart VS Code) so the new tools are on PATH.")
    scope = "--all" if set(capabilities) >= set(CAPABILITIES) else " ".join("--for " + c for c in capabilities)
    lines.append(f"Then check: ifs-agent-ops doctor {scope}")
    return "\n".join(lines)


def exit_code(outcomes: list[Outcome]) -> int:
    return 1 if any(o.status in ("declined", "failed", "skipped", "manual") for o in outcomes) else 0


def resolve(requested: list[str], all_: bool) -> list[str]:
    if all_:
        return list(CAPABILITIES)
    return [c for c in CAPABILITIES if c in set(requested or CORE)]
