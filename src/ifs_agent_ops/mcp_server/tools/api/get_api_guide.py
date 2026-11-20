import json
from typing import Optional

from ifs_agent_ops.mcp_server.resources.index import get_resources


async def handler(guide: Optional[str]) -> str:
    resources = get_resources()

    if not guide:
        available = [
            {
                "name": r.definition.name,
                "uri": str(r.definition.uri),
                "description": r.definition.description,
            }
            for r in resources
        ]
        return json.dumps({"available_guides": available}, indent=2)

    needle = str(guide).lower()
    resource = next(
        (
            r for r in resources
            if needle in str(r.definition.uri).lower()
            or needle in (r.definition.name or "").lower()
            or needle in (r.definition.description or "").lower()
        ),
        None,
    )

    if not resource:
        return json.dumps({
            "error": f"No guide found for '{guide}'.",
            "available_guides": [str(r.definition.uri) for r in resources],
            "instruction": (
                "No existing skill guide was found for that projection. "
                "If the user wants to BUILD a new skill for this projection, use the "
                "build_ifs_skill_from_projection prompt (pass the projection name) — it will "
                "fetch the live OpenAPI spec and guide you through authoring the skill. "
                "If the user just wants to USE an existing skill that doesn't exist yet, tell the user: "
                "'I don't have a skill for that workflow yet. You can create one using the "
                "build_ifs_skill_from_projection prompt, or by recording a HAR file from the browser "
                "and using build_ifs_skill_from_har.' "
                "Do not attempt to guess or discover IFS endpoints without a guide or skill-building session."
            ),
        }, indent=2)

    from pathlib import Path
    content = Path(resource.path).read_text(encoding="utf-8")
    return content
