"""The sidecar ``<recording>.annotations.yaml`` written by ``record import`` (and its recording plan)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml


def annotations_path(recording: Path) -> Path:
    return Path(recording).with_suffix(".annotations.yaml")


def load_annotations(recording: Path) -> dict:
    path = annotations_path(recording)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_plan(recording: Path) -> Optional[dict]:
    """The demonstrator's recording plan (intent, ordered tasks with entry ranges), if the recording has one."""
    plan = load_annotations(recording).get("plan")
    return plan if isinstance(plan, dict) else None


def entry_tasks(plan: Optional[dict]) -> dict[int, str]:
    """0-based HAR entry index -> plan task id."""
    out: dict[int, str] = {}
    for t in (plan or {}).get("tasks", []):
        if t.get("entries"):
            first, last = t["entries"]
            out.update({i: t["id"] for i in range(first, last + 1)})
    return out
