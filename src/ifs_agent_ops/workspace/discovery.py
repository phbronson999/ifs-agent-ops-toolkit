"""Find the operation packages this toolkit installation works with.

``IFS_AGENT_OPS_PACKAGES`` is a list of paths separated by ``os.pathsep`` (``;`` on Windows).
Each path is either a package folder (contains ``package.yaml``) or a parent folder whose
immediate children are scanned for packages. Invalid packages are reported, not silently used.

When ``.env`` supplies the setting, it is re-read on every discovery, so a package registered with
``register_package`` (the TUI's "new package") is found by a running toolkit server without a restart.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ifs_agent_ops import env
from ifs_agent_ops.workspace.package import (
    MANIFEST,
    Package,
    PackageError,
    init_package,
    load_package,
)

ENV_VAR = "IFS_AGENT_OPS_PACKAGES"


def configured_paths() -> list[Path]:
    raw = env.live_value(ENV_VAR)
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def discover_packages() -> tuple[list[Package], list[str]]:
    """Return (packages sorted by name, problems). Duplicate package names: first wins."""
    candidates: list[Path] = []
    problems: list[str] = []
    for path in configured_paths():
        if not path.exists():
            problems.append(f"{ENV_VAR} entry does not exist: {path}")
        elif (path / MANIFEST).exists():
            candidates.append(path)
        elif path.is_dir():
            candidates.extend(sorted(c for c in path.iterdir() if (c / MANIFEST).exists()))
    packages: dict[str, Package] = {}
    for root in candidates:
        try:
            pkg = load_package(root)
        except PackageError as exc:
            problems.append(str(exc))
            continue
        if pkg.name in packages:
            problems.append(f"Duplicate package name {pkg.name!r} at {root} (using {packages[pkg.name].root})")
            continue
        packages[pkg.name] = pkg
    return sorted(packages.values(), key=lambda p: p.name), problems


def find_package(name_or_path: str) -> Package:
    path = Path(name_or_path)
    if (path / MANIFEST).exists():
        return load_package(path)
    packages, _ = discover_packages()
    for pkg in packages:
        if pkg.name == name_or_path:
            return pkg
    names = ", ".join(p.name for p in packages) or "(none configured)"
    raise PackageError(f"Package {name_or_path!r} not found as a path or in {ENV_VAR}. Known: {names}")


# ── creating and registering a package ─────────────────────────────────────

def suggest_name(display_name: str) -> str:
    """A package name from a human title: "Purchase Requisitions" -> "ifs-purchase-requisitions"."""
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    if not slug:
        return ""
    return slug if slug == "ifs" or slug.startswith("ifs-") else f"ifs-{slug}"


def default_parent() -> Path | None:
    """Where a new package folder goes by default: next to the configured packages."""
    for path in configured_paths():
        if (path / MANIFEST).exists():
            return path.parent
        if path.is_dir():
            return path
    return None


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def is_registered(root: Path) -> bool:
    """The folder is listed itself, or its parent is listed (a listed parent is scanned for packages)."""
    return any(_same(p, root) or _same(p, Path(root).parent) for p in configured_paths())


def _env_line_value(value: str) -> str:
    # Single quotes when quoting is needed: python-dotenv takes them literally (in double quotes a Windows
    # path's backslashes would be read as escapes).
    return f"'{value}'" if re.search(r"""[\s#"]""", value) else value


@dataclass
class Registration:
    root: Path
    env_file: Path | None   # the .env file that now lists it (None: already listed, nothing written)
    note: str = ""             # set when other processes will not see it without a restart


def register_package(root: Path, env_file: Path | None = None) -> Registration:
    """List a package folder in IFS_AGENT_OPS_PACKAGES, in the toolkit's .env file and in this process.

    Other lines of .env are kept as they are. When the process environment (not .env) sets the variable,
    .env is still updated, but processes started with that environment keep their own value (see ``note``)."""
    root = Path(root).resolve()
    if is_registered(root):
        return Registration(root, None)
    env_file = Path(env_file) if env_file else env.REPO_ENV_FILE
    follows_file = env.from_env_file(ENV_VAR) or ENV_VAR not in os.environ
    entries = [p for p in env.live_value(ENV_VAR).split(os.pathsep) if p.strip()] + [str(root)]
    if "'" in str(root):
        raise PackageError(f"Folder names with a quote (') cannot be listed in .env: {root}")
    value = os.pathsep.join(entries)
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    setting = re.compile(rf"^\s*(export\s+)?{ENV_VAR}\s*=")
    new_line = f"{ENV_VAR}={_env_line_value(value)}"
    for i, line in enumerate(lines):
        if setting.match(line):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.environ[ENV_VAR] = value
    if follows_file and env_file == env.REPO_ENV_FILE:
        env._FROM_FILE[ENV_VAR] = value  # this process keeps following the file
    note = "" if follows_file else (
        f"{ENV_VAR} is set in this terminal's environment, not only in .env. The toolkit server and VS Code "
        "started from such an environment keep their own value: add the folder there too, or restart them "
        "without the variable.")
    return Registration(root, env_file, note)


def create_package(root: Path, name: str, *, display_name: str = "", description: str = "",
                   env_file: Path | None = None) -> tuple[Package, Registration]:
    """Create the package skeleton and register it, so the TUI, the side panel and Claude find it."""
    known = {p.name: p.root for p in discover_packages()[0]}
    if name in known:
        raise PackageError(f"A package named {name!r} already exists at {known[name]}")
    root = Path(root).expanduser().resolve()
    if "'" in str(root):
        raise PackageError(f"Folder names with a quote (') cannot be listed in .env: {root}")
    if root.exists() and not root.is_dir():
        raise PackageError(f"{root} is a file, not a folder")
    if root.is_dir() and any(root.iterdir()) and not (root / MANIFEST).exists():
        raise PackageError(f"{root} already exists and is not empty: choose a new folder")
    package = init_package(root, name, description=description, display_name=display_name)
    return package, register_package(package.root, env_file)
