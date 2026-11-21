"""Capture operations shared by the CLI, the MCP tools and the local capture API (one behaviour, three surfaces)."""
from __future__ import annotations

import json
from collections import Counter
from typing import Optional

from ifs_agent_ops.capture.har_export import export
from ifs_agent_ops.capture.session import CaptureError, CaptureSession, new_session
from ifs_agent_ops.capture.store import RAW_HAR_FILE, CaptureStore, clean_event

PROTOCOL_VERSION = 1


def view(store: CaptureStore, session: CaptureSession, with_counts: bool = True) -> dict:
    """JSON-ready session state for every surface: plan, current task, progress, calls per task."""
    data = session.model_dump(mode="json")
    data["progress"] = session.progress()
    cur = session.current()
    data["current_task"] = {"id": cur.id, "title": cur.title} if cur else None
    if with_counts:
        counts = Counter(session.task_at(ts) for ts, _ in store.requests(session.id))
        data["requests"] = {"total": sum(counts.values()), "unassigned": counts.get(None, 0),
                            "by_task": {t.id: counts.get(t.id, 0) for t in session.tasks}}
    data["next_step"] = _next_step(session)
    return data


def _next_step(s: CaptureSession) -> str:
    if s.status == "planning":
        return ("Review the plan, then open the IFS web client in Edge/Chrome, open the IFS Agent Ops side panel, "
                "select this session and press Start recording.")
    if s.status == "recording":
        cur = s.current()
        return (f"Perform '{cur.title}' in IFS, then mark it complete (or skip it)." if cur else
                "Every planned task is done: add another task or stop the recording.")
    return (f"Import it: ifs-agent-ops capture import {s.id} --package <package>"
            + ("" if s.workflow else " --workflow <workflow>") if not s.imports else
            f"Imported as {', '.join(s.imports)}. Start a new planned recording when more evidence is needed.")


def resolve(store: CaptureStore, session_id: Optional[str]) -> str:
    if session_id:
        return session_id
    s = store.latest_open()
    if s is None:
        raise CaptureError("no open capture session; create one first")
    return s.id


def create(store: CaptureStore, intent: str, tasks: list[str], workflow: Optional[str] = None,
           package: Optional[str] = None, optional: Optional[list[str]] = None) -> dict:
    return view(store, store.create(new_session(intent, tasks, workflow, package, optional=optional or ())))


def apply(store: CaptureStore, session_id: Optional[str], action: str, **kw) -> dict:
    """Run one session action under the lock and return the new state."""
    sid = resolve(store, session_id)
    with store.edit(sid) as s:
        if action == "intent":
            s.set_intent(kw["intent"])
        elif action == "add":
            s.add_task(kw["title"], kw.get("description", ""), kw.get("position"), ts=kw.get("ts"),
                       optional=bool(kw.get("optional")))
        elif action == "edit":
            s.edit_task(kw["task_id"], kw.get("title"), kw.get("description"), kw.get("optional"))
        elif action == "remove":
            s.remove_task(kw["task_id"])
        elif action == "move":
            s.move_task(kw["task_id"], int(kw["position"]))
        elif action == "set_plan":
            _set_plan(s, kw["tasks"], kw.get("optional"))
        elif action == "start":
            s.start(kw["ifs_origin"], kw.get("ts"))
        elif action == "complete":
            s.complete_current(kw.get("ts"), kw.get("task_id"))
        elif action == "skip":
            s.skip_current(kw.get("ts"), kw.get("task_id"))
        elif action == "stop":
            s.stop(kw.get("ts"))
        else:
            raise CaptureError(f"unknown action {action!r}")
    return view(store, store.load(sid))


def _set_plan(s: CaptureSession, titles: list[str], optional: Optional[list[str]] = None) -> None:
    """Replace the plan before recording, keeping the ids of tasks whose title is unchanged. ``optional`` lists
    the titles to mark optional (every other task becomes required); None keeps each kept task's flag."""
    if s.status != "planning":
        raise CaptureError("the whole plan can only be replaced before recording; add or skip tasks instead")
    marked = None if optional is None else {t.strip() for t in optional}
    unknown = (marked or set()) - {t.strip() for t in titles}
    if unknown:
        raise CaptureError(f"optional task(s) not in the plan: {', '.join(sorted(unknown))}")
    old = {t.title: t for t in s.tasks}
    s.tasks = []
    for title in titles:
        if title.strip() in old:
            task = old.pop(title.strip())
            s.tasks.append(task)
        else:
            task = s.add_task(title)  # appends
        if marked is not None:
            task.optional = title.strip() in marked


def ingest(store: CaptureStore, session_id: str, events: list[dict]) -> dict:
    """Store events from the browser. Requests are accepted while recording, and after a stop when they were
    sent before it (a stop from Claude or the CLI can overtake events still on their way); responses to
    recorded requests are accepted whenever they arrive."""
    with store.lock(session_id):
        s = store.load(session_id)
        if s.status == "planning":
            raise CaptureError(f"session {session_id} is not recording yet")
        events = [clean_event(e) for e in events]  # malformed events are refused, not dropped
        known = {rid for _, rid in store.requests(session_id)}
        accepted = []
        for e in events:
            if e.get("type") == "request":
                if s.status != "recording" and not (s.stopped_at and float(e.get("ts", 0)) < s.stopped_at):
                    continue
                if s.ifs_origin and not str(e.get("url", "")).startswith(s.ifs_origin + "/"):
                    continue  # only the IFS web client being recorded
                known.add(e.get("request_id"))
            elif e.get("request_id") not in known:
                continue
            accepted.append(e)
        stored = store.append_events(session_id, accepted)
    cur = s.current()
    return {"accepted": stored, "ignored": len(events) - stored, "status": s.status,
            "current_task": cur.id if cur else None}


def import_session(store: CaptureStore, session_id: str, package, workflow: Optional[str] = None,
                   name: Optional[str] = None, redact: Optional[bool] = None, force: bool = False):
    """Export the stopped session as a local raw HAR and import it through ``record import`` with its plan."""
    from ifs_agent_ops.recordings.importer import import_recording

    s = store.load(session_id)
    if s.status != "stopped":
        raise CaptureError(f"session {session_id} is {s.status}; stop the recording before importing it")
    workflow = workflow or s.workflow
    if not workflow:
        raise CaptureError("no workflow for this session; pass --workflow (the package recordings folder)")
    events = store.events(session_id)
    har, plan = export(s, events)
    if not har["log"]["entries"]:
        raise CaptureError(f"session {session_id} recorded no IFS calls; nothing to import")
    raw = store.dir(session_id) / RAW_HAR_FILE
    raw.write_text(json.dumps(har, ensure_ascii=False), encoding="utf-8", newline="\n")
    result = import_recording(raw, package, workflow, name or s.id, redact=redact, description=s.intent,
                              force=force, plan=plan)
    rel = result.recording.relative_to(package.root).as_posix()
    with store.edit(session_id) as live:
        if rel not in live.imports:
            live.imports.append(rel)
    return result, plan
