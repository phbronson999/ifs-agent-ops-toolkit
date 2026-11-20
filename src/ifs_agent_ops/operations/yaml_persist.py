"""Small, reviewable YAML updates for approval state.

Approving an operation or capability changes two top-level entries: ``status`` and ``approval``.
Re-serialising the whole document would expand flow mappings, unfold folded text and drop
comments, so a one-line decision becomes a diff of hundreds of lines. Instead the two entries are
replaced (or inserted) as text, and the result is re-parsed and compared with the intended model.
If the patched text does not mean exactly the intended model, nothing clever is attempted: the
caller's full serialiser writes the file instead.

Content hashes are computed on the parsed model, never on file text, so formatting never changes
an operation's or capability's identity.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel

_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][\w-]*):(\s|$)")


def _span(lines: list[str], key: str) -> Optional[tuple[int, int]]:
    """Line range [start, end) of a top-level entry, without trailing blank lines."""
    start = next((i for i, line in enumerate(lines) if line.startswith(f"{key}:")
                  and _TOP_LEVEL_KEY.match(line)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not _TOP_LEVEL_KEY.match(lines[end]):
        end += 1
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1
    return start, end


def _render(key: str, value: Any) -> list[str]:
    text = yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True, width=120)
    return text.splitlines(keepends=True)


def set_top_level(text: str, key: str, value: Any, after: Optional[str] = None) -> str:
    """Replace a top-level ``key`` entry, or insert it after ``after`` (else at the end)."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    block = _render(key, value)
    span = _span(lines, key)
    if span:
        lines[span[0]:span[1]] = block
    else:
        anchor = _span(lines, after) if after else None
        at = anchor[1] if anchor else len(lines)
        lines[at:at] = block
    return "".join(lines)


def remove_top_level(text: str, key: str) -> str:
    lines = text.splitlines(keepends=True)
    span = _span(lines, key)
    if span:
        del lines[span[0]:span[1]]
    return "".join(lines)


def persist_approval(path: Path, model: BaseModel, full_write: Callable[[], Path]) -> str:
    """Write ``model``'s status and approval into ``path`` with a minimal text change.

    Returns "patched" or "rewritten" (fallback through ``full_write`` when the patched text would
    not parse back to exactly ``model``)."""
    original = path.read_text(encoding="utf-8")
    text = set_top_level(original, "status", model.status, after="id")
    approval = getattr(model, "approval", None)
    text = (set_top_level(text, "approval", approval.model_dump(mode="json", exclude_none=True))
            if approval is not None else remove_top_level(text, "approval"))
    try:
        parsed = type(model).model_validate(yaml.safe_load(text))
        same = parsed.model_dump(mode="json") == model.model_dump(mode="json")
    except Exception:
        same = False
    if not same:
        full_write()
        return "rewritten"
    path.write_text(text, encoding="utf-8", newline="\n")
    return "patched"
