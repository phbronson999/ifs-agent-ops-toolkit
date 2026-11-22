"""IFS operation packages: independently versioned folders holding evidence and generated artifacts.

Layout::

    <package>/
    ├── package.yaml
    ├── recordings/<workflow>/<task>.har            sanitized evidence (record import)
    ├── recordings/<workflow>/<task>.annotations.yaml
    ├── metadata/<Projection>/                      pinned $metadata / $openapi snapshots
    ├── operations/                                 canonical operation definitions
    ├── workflows/
    ├── agent/skills/   agent/instructions/
    ├── power-platform/connectors/<Projection>/   one custom connector per IFS service
    └── dispatcher/
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

MANIFEST = "package.yaml"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_KNOWN_KEYS = {"name", "display_name", "version", "description", "projections", "ifs_release", "toolkit",
               "evidence"}
# Machine-local folder inside a package (deployed-state caches, local runs). Never committed.
LOCAL_STATE_DIR = ".ifs-agent-ops"
LAYOUT_DIRS = ("recordings", "metadata", "operations", "workflows", "agent/skills", "agent/instructions",
               "power-platform/connectors", "dispatcher")

PACKAGE_GITIGNORE = """\
# Raw captures never enter a package repo: import them with `ifs-agent-ops record import`,
# which writes the sanitized representation under recordings/.
*.raw.har
raw/
local/
.env
# Machine-local and deployed-state information (reserved for the toolkit; never canonical source).
.ifs-agent-ops/
"""


class PackageError(ValueError):
    pass


@dataclass
class Package:
    """An operation package.

    ``name`` is the package's permanent logical identity: deployment intent, logical resource
    names and consumers key on it, so it must not change once the package is established.
    ``display_name`` is the renameable human-facing title.
    """
    root: Path
    name: str
    version: str
    display_name: str = ""
    description: str = ""
    projections: list[str] = field(default_factory=list)
    ifs_release: Optional[str] = None
    redact_values_default: bool = True

    @property
    def skills_dir(self) -> Path:
        return self.root / "agent" / "skills"

    @property
    def recordings_dir(self) -> Path:
        return self.root / "recordings"

    def recording_path(self, workflow: str, task: str) -> Path:
        for label, seg in (("workflow", workflow), ("task", task)):
            if not _SEGMENT_RE.match(seg):
                raise PackageError(f"Invalid {label} name {seg!r}: use letters, digits, '.', '-' or '_'.")
        return self.recordings_dir / workflow / f"{task}.har"


def load_package(root: Path) -> Package:
    root = Path(root)
    path = root / MANIFEST
    if not path.exists():
        raise PackageError(f"No {MANIFEST} in {root}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PackageError(f"Could not parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PackageError(f"{path} must be a mapping")
    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise PackageError(f"{path} has unknown key(s): {', '.join(unknown)}")
    name, version = str(data.get("name", "")), str(data.get("version", ""))
    if not _NAME_RE.match(name):
        raise PackageError(f"{path}: 'name' must be lowercase letters, digits and '-' (got {name!r})")
    if not version:
        raise PackageError(f"{path}: 'version' is required")
    projections = data.get("projections") or []
    if not isinstance(projections, list) or not all(isinstance(p, str) for p in projections):
        raise PackageError(f"{path}: 'projections' must be a list of projection names")
    evidence = data.get("evidence") or {}
    return Package(
        root=root, name=name, version=version, display_name=str(data.get("display_name") or name),
        description=str(data.get("description", "")),
        projections=projections, ifs_release=data.get("ifs_release"),
        redact_values_default=bool(evidence.get("redact_values", True)),
    )


def init_package(root: Path, name: str, *, description: str = "", projections: Optional[list[str]] = None,
                 display_name: str = "") -> Package:
    """Create the package skeleton. Refuses to overwrite an existing manifest."""
    root = Path(root)
    if (root / MANIFEST).exists():
        raise PackageError(f"{root} already contains {MANIFEST}")
    if not _NAME_RE.match(name):
        raise PackageError(f"Package name must be lowercase letters, digits and '-' (got {name!r})")
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,  # permanent logical identity: do not change once established
        "display_name": display_name or name,
        "version": "0.1.0",
        "description": description,
        "projections": projections or [],
        "ifs_release": None,
        "evidence": {"redact_values": True},
    }
    (root / MANIFEST).write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
    for d in LAYOUT_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
        keep = root / d / ".gitkeep"
        if not any((root / d).iterdir()):
            keep.write_text("", encoding="utf-8", newline="\n")
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(PACKAGE_GITIGNORE, encoding="utf-8", newline="\n")
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(
            f"# {name}\n\n{description or 'IFS operation package.'}\n\n"
            "Managed with ifs-agent-ops-toolkit. Recordings under `recordings/` are sanitized "
            "(credentials, internal infrastructure and the IFS host removed) and, by default, "
            "have business values redacted. Raw captures stay local.\n",
            encoding="utf-8",
        )
    return load_package(root)
