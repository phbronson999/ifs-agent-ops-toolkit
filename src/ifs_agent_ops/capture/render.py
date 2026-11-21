"""Plain-text view of a capture session (CLI)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

MARKS = {"complete": "[x]", "active": "[>]", "pending": "[ ]", "skipped": "[-]", "incomplete": "[!]"}


def render(view: dict) -> str:
    p = view["progress"]
    state = view["status"]
    if state == "stopped":
        state += ", plan " + ("complete" if p["plan_complete"] else "INCOMPLETE")
    lines = [f"Session {view['id']} ({state})", f"Intent: {view['intent']}"]
    if view.get("workflow"):
        lines.append(f"Workflow: {view['workflow']}" + (f"   Package: {view['package']}" if view.get("package") else ""))
    lines.append(f"Progress: {p['done']} of {p['total']} done"
                 + (f"; current: {view['current_task']['title']}" if view.get("current_task") else ""))
    lines.append("")
    by_task = (view.get("requests") or {}).get("by_task", {})
    for n, t in enumerate(view["tasks"], 1):
        extra = [t["status"]] + (["added during recording"] if t["added_during_recording"] else [])
        if view["status"] != "planning":
            extra.append(f"{by_task.get(t['id'], 0)} requests")
        lines.append(f"{MARKS[t['status']]} {n}. {t['title']}  ({t['id']}; {', '.join(extra)})")
    if (view.get("requests") or {}).get("unassigned"):
        lines.append(f"    {view['requests']['unassigned']} request(s) outside any task")
    lines += ["", view["next_step"]]
    return "\n".join(lines)


def extension_dir() -> Optional[Path]:
    """The browser extension folder in a source checkout (load it unpacked in Edge/Chrome)."""
    folder = Path(__file__).resolve().parents[3] / "browser-extension" / "extension"
    return folder if (folder / "manifest.json").exists() else None
