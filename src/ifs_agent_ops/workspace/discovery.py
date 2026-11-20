"""Find the operation packages this toolkit installation works with.

``IFS_AGENT_OPS_PACKAGES`` is a list of paths separated by ``os.pathsep`` (``;`` on Windows).
Each path is either a package folder (contains ``package.yaml``) or a parent folder whose
immediate children are scanned for packages. Invalid packages are reported, not silently used.
"""
from __future__ import annotations

import os
from pathlib import Path

from ifs_agent_ops.workspace.package import MANIFEST, Package, PackageError, load_package

ENV_VAR = "IFS_AGENT_OPS_PACKAGES"


def configured_paths() -> list[Path]:
    raw = os.environ.get(ENV_VAR, "")
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
