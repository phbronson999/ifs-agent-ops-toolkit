import json
import re
from pathlib import Path
from typing import Optional

from ifs_agent_ops.config import get_skills_dir


def _get_resources_dir() -> Path:
    skills_dir = get_skills_dir()
    if skills_dir:
        return Path(skills_dir).resolve()
    from ifs_agent_ops.mcp_server.resources.index import GUIDANCE_DIR

    return GUIDANCE_DIR  # legacy fallback: the bundled guide folder


def _extract_sections(content: str) -> list[str]:
    return [
        l.lstrip("#").strip()
        for l in content.splitlines()
        if l.startswith("## ")
    ]


def _extract_fields(content: str) -> set[str]:
    return set(re.findall(r"`([A-Z][A-Za-z0-9_]+)`", content))


def _count_examples(content: str) -> int:
    return sum(
        1 for l in content.splitlines()
        if re.match(r"^\s*(GET\s+)?/main/ifsapplications", l)
    )


def _build_change_summary(old: str, new: str) -> str:
    old_sections = _extract_sections(old)
    new_sections = _extract_sections(new)
    added_sections = [s for s in new_sections if s not in old_sections]
    removed_sections = [s for s in old_sections if s not in new_sections]

    old_fields = _extract_fields(old)
    new_fields = _extract_fields(new)
    added_fields = sorted(new_fields - old_fields)
    removed_fields = sorted(old_fields - new_fields)

    old_examples = _count_examples(old)
    new_examples = _count_examples(new)
    example_delta = new_examples - old_examples

    lines: list[str] = []
    if added_sections:
        lines.append(f"+ Sections added: {', '.join(added_sections)}")
    if removed_sections:
        lines.append(f"- Sections removed: {', '.join(removed_sections)}")
    if added_fields:
        lines.append(f"+ Fields added: {', '.join(added_fields)}")
    if removed_fields:
        lines.append(f"- Fields removed: {', '.join(removed_fields)}")
    if example_delta > 0:
        lines.append(f"+ {example_delta} new example(s) added")
    elif example_delta < 0:
        lines.append(f"- {abs(example_delta)} example(s) removed")

    if not lines:
        delta = len(new) - len(old)
        if delta == 0:
            return "No changes detected."
        lines.append(
            f"Content expanded by ~{delta} characters"
            if delta > 0
            else f"Content reduced by ~{abs(delta)} characters"
        )

    return "\n".join(lines)


async def handler(filename: str, content: str) -> str:
    if not filename or not content:
        return json.dumps({"error": "filename and content are required"})

    if not filename.endswith(".md"):
        filename += ".md"

    resources_dir = _get_resources_dir()
    if not resources_dir.exists():
        return json.dumps({"error": f"Resources directory not found: {resources_dir}"})

    dest_path = (resources_dir / filename).resolve()
    if not str(dest_path).startswith(str(resources_dir.resolve())):
        return json.dumps({"error": "Invalid filename: must not contain path traversal sequences"})

    is_update = dest_path.exists()
    change_summary: Optional[str] = None

    if is_update:
        old_content = dest_path.read_text(encoding="utf-8")
        from ifs_agent_ops.generators.skills import is_generated

        if is_generated(old_content):
            return json.dumps({"error": f"{filename} is generated from an approved capability and cannot be edited "
                                        "here; change the capability and run `ifs-agent-ops skills generate`"})
        change_summary = _build_change_summary(old_content, content)

    dest_path.write_text(content, encoding="utf-8")

    heading = next(
        (l.lstrip("# ").strip() for l in content.splitlines() if l.startswith("# ")),
        filename,
    )

    result: dict = {
        "success": True,
        "action": "updated" if is_update else "created",
        "skill": heading,
        "filename": filename,
        "path": str(dest_path),
        "note": "The skill is available immediately — no restart needed.",
    }
    if change_summary:
        result["changes"] = change_summary

    return json.dumps(result, indent=2)
