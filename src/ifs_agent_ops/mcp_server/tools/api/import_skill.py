import json
from pathlib import Path
from typing import Optional

import httpx

from ifs_agent_ops.config import get_skills_dir

_FETCH_TIMEOUT_S = 15.0
_MAX_REDIRECTS = 3


def _get_resources_dir() -> Path:
    skills_dir = get_skills_dir()
    if skills_dir:
        return Path(skills_dir).resolve()
    from ifs_agent_ops.mcp_server.resources.index import GUIDANCE_DIR

    return GUIDANCE_DIR  # legacy fallback: the bundled guide folder


async def _fetch_https(url: str) -> str:
    """Fetch content from an HTTPS URL, enforcing HTTPS-only for all redirects."""
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS URLs are allowed for remote skill import")

    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_S,
        follow_redirects=False,  # we handle redirects manually to enforce HTTPS
        max_redirects=0,
    ) as client:
        redirect_count = 0
        current_url = url
        while True:
            resp = await client.get(current_url)
            if resp.is_redirect:
                redirect_count += 1
                if redirect_count > _MAX_REDIRECTS:
                    raise ValueError("Too many redirects")
                location = resp.headers.get("location", "")
                if not location.startswith("https://"):
                    raise ValueError("Only HTTPS redirect targets are allowed")
                current_url = location
                continue
            resp.raise_for_status()
            return resp.text


async def handler(source: str, filename: Optional[str]) -> str:
    if not source:
        return json.dumps({"error": "source is required"})

    # Resolve filename
    raw_filename = filename
    if not raw_filename:
        segment = source.replace("\\", "/").rstrip("/").split("/")[-1]
        raw_filename = segment if "." in segment else f"{segment}.md"
    if not raw_filename.endswith(".md"):
        raw_filename += ".md"

    # Fetch content
    try:
        if source.startswith("http://") or source.startswith("https://"):
            content = await _fetch_https(source)
        else:
            content = Path(source).read_text(encoding="utf-8")
    except Exception as exc:
        return json.dumps({"error": f"Could not read source: {exc}"})

    # Basic validation
    if not content.strip().startswith("#"):
        return json.dumps({
            "error": "Source does not appear to be a valid skill file (must start with a # heading)"
        })

    resources_dir = _get_resources_dir()
    if not resources_dir.exists():
        return json.dumps({"error": f"Resources directory not found: {resources_dir}"})

    dest_path = (resources_dir / raw_filename).resolve()
    # Path traversal guard
    if not str(dest_path).startswith(str(resources_dir.resolve())):
        return json.dumps({"error": "Invalid filename: must not contain path traversal sequences"})

    dest_path.write_text(content, encoding="utf-8")

    heading = next(
        (l.lstrip("# ").strip() for l in content.splitlines() if l.startswith("# ")),
        raw_filename,
    )

    return json.dumps({
        "success": True,
        "skill": heading,
        "saved_as": raw_filename,
        "path": str(dest_path),
        "note": "The skill is available immediately — no restart needed.",
    }, indent=2)
