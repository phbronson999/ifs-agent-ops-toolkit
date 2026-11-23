"""Capture session and recording plan: the human demonstrator's intent and ordered tasks.

A session starts from an intent ("I want to teach the builder how to create a customer order") and
an ordered plan of tasks agreed before capture starts. While recording, exactly one task is current;
completing or skipping it closes its interval and makes the next pending task current. Tasks can be
added during recording. Stopping keeps the plan, the task boundaries and every event.

The plan is semantic evidence, not UI state: it travels into the package recording's annotations,
the analysis and the observed workflows (roadmap §1.1).

Task intervals are half-open ``[started_at, ended_at)`` in epoch seconds. Events are attributed to
the task whose interval contains the time the request was sent. The browser and the toolkit run on
the same machine and use the same clock.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1
TaskStatus = Literal["pending", "active", "complete", "skipped", "incomplete"]
SessionStatus = Literal["planning", "recording", "stopped"]
_DONE = ("complete", "skipped")


class CaptureError(ValueError):
    pass


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanTask(_M):
    id: str = Field(description="Stable id within the session (t1, t2, ...); never reused")
    title: str
    description: str = ""
    status: TaskStatus = "pending"
    optional: bool = Field(False, description="The demonstrator marked this step optional: the business action "
                                              "can be done without it, so it never makes the plan incomplete")
    added_during_recording: bool = False
    started_at: Optional[float] = Field(None, description="Epoch seconds when the task became current")
    ended_at: Optional[float] = Field(None, description="Epoch seconds when it was completed, skipped or stopped")


class CaptureSession(_M):
    schema_version: int = SCHEMA_VERSION
    id: str
    intent: str
    workflow: Optional[str] = Field(None, description="Package workflow folder the recording is imported into")
    package: Optional[str] = Field(None, description="Target package (name or path), if known")
    status: SessionStatus = "planning"
    ifs_origin: Optional[str] = Field(None, description="Origin of the IFS web client being recorded")
    created_at: str
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    tasks: list[PlanTask] = Field(default_factory=list)
    next_task_number: int = 1
    imports: list[str] = Field(default_factory=list, description="Package recordings created from this session")

    # ── queries ──────────────────────────────────────────────────────────────

    def current(self) -> Optional[PlanTask]:
        return next((t for t in self.tasks if t.status == "active"), None)

    def task(self, task_id: str) -> PlanTask:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise CaptureError(f"no task {task_id!r} in session {self.id}")

    @property
    def plan_complete(self) -> bool:
        """Every required task was completed or skipped (optional tasks may be left out or cut off)."""
        return bool(self.tasks) and all(t.status in _DONE for t in self.tasks if not t.optional)

    def progress(self) -> dict:
        counts = {s: sum(1 for t in self.tasks if t.status == s)
                  for s in ("pending", "active", "complete", "skipped", "incomplete")}
        cur = self.current()
        return {"total": len(self.tasks), **counts, "done": counts["complete"] + counts["skipped"],
                "current": cur.id if cur else None, "plan_complete": self.plan_complete}

    def task_at(self, ts: float) -> Optional[str]:
        """The task whose interval contains ``ts`` (None before the first task or between intervals)."""
        for t in self.tasks:
            if t.started_at is None or ts < t.started_at:
                continue
            if t.ended_at is None or ts < t.ended_at:
                return t.id
        return None

    # ── plan editing ─────────────────────────────────────────────────────────

    def _editable(self) -> None:
        if self.status == "stopped":
            raise CaptureError(f"session {self.id} is stopped; start a new planned recording instead")

    def set_intent(self, intent: str) -> None:
        self._editable()
        if not intent.strip():
            raise CaptureError("intent must not be empty")
        self.intent = intent.strip()

    def add_task(self, title: str, description: str = "", position: Optional[int | str] = None,
                 ts: Optional[float] = None, optional: bool = False) -> PlanTask:
        """Add a task. ``position`` is 1-based or ``"end"``; while recording ``"next"`` (the default) places
        it right after the current task. Tasks added while recording are marked as such."""
        self._editable()
        if not title.strip():
            raise CaptureError("task title must not be empty")
        task = PlanTask(id=f"t{self.next_task_number}", title=title.strip(), description=description.strip(),
                        optional=optional, added_during_recording=self.status == "recording")
        self.next_task_number += 1
        if position in (None, "end") and self.status == "planning" or position == "end":
            index = len(self.tasks)
        elif position in (None, "next"):
            cur = self.current()
            index = self.tasks.index(cur) + 1 if cur else self._first_movable()
        else:
            index = self._checked_index(int(position) - 1, inserting=True)
        self.tasks.insert(index, task)
        if self.status == "recording" and self.current() is None:
            self._activate(task, ts or _now())
        return task

    def edit_task(self, task_id: str, title: Optional[str] = None, description: Optional[str] = None,
                  optional: Optional[bool] = None) -> PlanTask:
        """Change a task's title, description or optional flag. Finished tasks keep their title and description
        (they are evidence), but can still be marked optional or required until the session stops."""
        self._editable()
        task = self.task(task_id)
        if (title is not None or description is not None) and task.status in _DONE:
            raise CaptureError(f"{task_id} is {task.status}; finished tasks keep their title")
        if title is not None and not title.strip():
            raise CaptureError("task title must not be empty")
        if optional is not None:
            task.optional = bool(optional)
        if title is not None:
            if not title.strip():
                raise CaptureError("task title must not be empty")
            task.title = title.strip()
        if description is not None:
            task.description = description.strip()
        return task

    def remove_task(self, task_id: str) -> None:
        self._editable()
        task = self.task(task_id)
        if task.status != "pending":
            raise CaptureError(f"{task_id} is {task.status}; only pending tasks can be removed (skip it instead)")
        self.tasks.remove(task)

    def move_task(self, task_id: str, position: int) -> None:
        self._editable()
        task = self.task(task_id)
        if task.status != "pending":
            raise CaptureError(f"{task_id} is {task.status}; only pending tasks can be reordered")
        original = self.tasks.index(task)
        self.tasks.remove(task)
        try:
            index = self._checked_index(position - 1, inserting=True)
        except CaptureError:
            self.tasks.insert(original, task)  # a refused move changes nothing
            raise
        self.tasks.insert(index, task)

    def _first_movable(self) -> int:
        """Index of the first position a pending task may take (after every started task)."""
        started = [i for i, t in enumerate(self.tasks) if t.status != "pending"]
        return (started[-1] + 1) if started else 0

    def _checked_index(self, index: int, inserting: bool) -> int:
        lo, hi = self._first_movable(), len(self.tasks)
        if not lo <= index <= hi:
            raise CaptureError(f"position {index + 1} is out of range ({lo + 1}–{hi + 1}); "
                               "tasks that have started keep their place")
        return index

    # ── recording ────────────────────────────────────────────────────────────

    def _activate(self, task: PlanTask, ts: float) -> None:
        task.status, task.started_at = "active", ts

    def start(self, ifs_origin: str, ts: Optional[float] = None) -> PlanTask:
        if self.status != "planning":
            raise CaptureError(f"session {self.id} is {self.status}; it can only be started once")
        if not self.tasks:
            raise CaptureError("the recording plan is empty; add at least one task before recording")
        if not re.match(r"^https?://[^/\s]+$", ifs_origin or ""):
            raise CaptureError(f"invalid IFS origin {ifs_origin!r} (expected e.g. https://ifs.example)")
        ts = ts or _now()
        self.status, self.started_at, self.ifs_origin = "recording", ts, ifs_origin
        self._activate(self.tasks[0], ts)
        return self.tasks[0]

    def _finish_current(self, status: TaskStatus, ts: Optional[float], task_id: Optional[str]) -> Optional[PlanTask]:
        if self.status != "recording":
            raise CaptureError(f"session {self.id} is {self.status}, not recording")
        cur = self.current()
        if cur is None:
            raise CaptureError("no current task (every planned task is done); add a task or stop the recording")
        if task_id and task_id != cur.id:
            raise CaptureError(f"{task_id} is not the current task (current: {cur.id} {cur.title!r})")
        ts = max(ts or _now(), cur.started_at or 0)
        cur.status, cur.ended_at = status, ts
        nxt = next((t for t in self.tasks if t.status == "pending"), None)
        if nxt:
            self._activate(nxt, ts)
        return nxt

    def complete_current(self, ts: Optional[float] = None, task_id: Optional[str] = None) -> Optional[PlanTask]:
        return self._finish_current("complete", ts, task_id)

    def skip_current(self, ts: Optional[float] = None, task_id: Optional[str] = None) -> Optional[PlanTask]:
        return self._finish_current("skipped", ts, task_id)

    def stop(self, ts: Optional[float] = None) -> None:
        if self.status == "stopped":
            raise CaptureError(f"session {self.id} is already stopped")
        ts = ts or _now()
        cur = self.current()
        if cur:  # stopped before the user marked it done: its events are kept, the task is incomplete
            cur.status, cur.ended_at = "incomplete", max(ts, cur.started_at or 0)
        self.status, self.stopped_at = "stopped", ts


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


_INTENT_LEAD = re.compile(r"^(?:i\s+(?:want|would like|need)\s+to\s+)?(?:teach|show)\s+(?:the\s+builder|the\s+agent|"
                          r"claude|you|it)?\s*how\s+(?:to\s+)?|^how\s+to\s+", re.IGNORECASE)
_ARTICLES = {"a", "an", "the"}


def workflow_from_intent(intent: str, limit: int = 40) -> str:
    """A workflow folder name from the intent, used when the demonstrator left it empty:
    "I want to teach the builder how to create a Purchase Requisition" -> "create-purchase-requisition"."""
    words = [w for w in re.split(r"[^a-z0-9]+", _INTENT_LEAD.sub("", intent.strip()).lower())
             if w and w not in _ARTICLES]
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > limit:
            break
        out = f"{out}-{w}" if out else w
    return out or "recording"


def new_session_id(workflow: Optional[str], now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", (workflow or "").lower()).strip("-")[:40]
    return f"{stamp}-{slug}" if slug else stamp


def new_session(intent: str, tasks: list[str] = (), workflow: Optional[str] = None,
                package: Optional[str] = None, now: Optional[datetime] = None,
                optional: Iterable[str] = ()) -> CaptureSession:
    """A planning session. ``optional`` lists the titles of tasks the demonstrator marked optional."""
    if not intent.strip():
        raise CaptureError("intent must not be empty")
    if workflow and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", workflow):
        raise CaptureError(f"invalid workflow name {workflow!r}: use letters, digits, '.', '-' or '_'")
    now = now or datetime.now(timezone.utc)
    workflow = workflow or workflow_from_intent(intent)
    s = CaptureSession(id=new_session_id(workflow, now), intent=intent.strip(), workflow=workflow,
                       package=package or None, created_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    marked = {t.strip() for t in optional}
    unknown = marked - {t.strip() for t in tasks}
    if unknown:
        raise CaptureError(f"optional task(s) not in the plan: {', '.join(sorted(unknown))}")
    for title in tasks:
        s.add_task(title, optional=title.strip() in marked)
    return s
