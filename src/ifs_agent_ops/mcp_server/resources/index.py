from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import mcp.types as mcp_types

from ifs_agent_ops.config import get_skills_dir

_BUNDLED_DIR = Path(__file__).parent


@dataclass
class MCPResource:
    definition: mcp_types.Resource
    path: str


def _get_resource_dirs() -> list[Path]:
    dirs: list[Path] = []
    skills_dir = get_skills_dir()
    if skills_dir:
        resolved = Path(skills_dir).resolve()
        if resolved.exists():
            dirs.append(resolved)
    if _BUNDLED_DIR.exists():
        dirs.append(_BUNDLED_DIR)
    return dirs


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
    seen: dict[str, Path] = {}  # filename -> path (first seen wins)
    for d in _get_resource_dirs():
        for p in sorted(d.iterdir()):
            if p.suffix == ".md" and p.name not in seen:
                seen[p.name] = p

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
