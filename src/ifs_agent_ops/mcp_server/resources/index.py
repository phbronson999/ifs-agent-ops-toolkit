from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import mcp.types as mcp_types

from ifs_agent_ops.config import get_skills_dir
from ifs_agent_ops.workspace.discovery import discover_packages

# Toolkit-wide guidance (skill authoring guide, OData reference).
GUIDANCE_DIR = Path(__file__).resolve().parents[2] / "guidance"


@dataclass
class MCPResource:
    definition: mcp_types.Resource
    path: str


def _skill_dirs() -> list[Path]:
    """Where skills come from, highest precedence first: SKILLS_DIR, then operation packages."""
    dirs: list[Path] = []
    skills_dir = get_skills_dir()
    if skills_dir:
        resolved = Path(skills_dir).resolve()
        if resolved.exists():
            dirs.append(resolved)
    packages, _problems = discover_packages()
    dirs.extend(p.skills_dir for p in packages if p.skills_dir.is_dir())
    return dirs


def _markdown_files(directory: Path) -> list[Path]:
    return [p for p in sorted(directory.iterdir()) if p.suffix == ".md"] if directory.is_dir() else []


def _parse_metadata(content: str, filename: str) -> tuple[str, str, str]:
    lines = content.splitlines()
    heading = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), None)
    name = heading if heading else filename.replace(".md", "")
    desc_line = next(
        (l.strip() for l in lines if l.strip() and not l.startswith("#")),
        None,
    )
    description = desc_line.replace("`", "").replace("*", "").replace("_", "") if desc_line else name
    slug = filename.replace(".md", "")
    return name, description, slug


def get_resources() -> list[MCPResource]:
    """Scan resource directories fresh on every call (hot-reload)."""
    # Skills from all sources, ordered by file name (a name seen first wins), then guidance.
    # With only SKILLS_DIR set this is exactly the legacy order: SKILLS_DIR sorted, then bundled.
    skills: dict[str, Path] = {}
    for d in _skill_dirs():
        for p in _markdown_files(d):
            skills.setdefault(p.name, p)
    seen: dict[str, Path] = {name: skills[name] for name in sorted(skills)}
    for p in _markdown_files(GUIDANCE_DIR):
        seen.setdefault(p.name, p)

    result: list[MCPResource] = []
    for filename, path in seen.items():
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        name, description, slug = _parse_metadata(content, filename)
        uri = f"ifs://{slug}/guide"
        result.append(MCPResource(
            definition=mcp_types.Resource(
                uri=uri,
                name=name,
                description=description,
                mimeType="text/markdown",
            ),
            path=str(path),
        ))
    return result
