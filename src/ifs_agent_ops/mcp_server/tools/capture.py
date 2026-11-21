"""MCP tools for guided capture: intent -> recording plan -> task-bucketed recording -> package import."""
from __future__ import annotations

import json
from typing import Optional

from ifs_agent_ops.capture import service
from ifs_agent_ops.capture.api import get_store
from ifs_agent_ops.capture.session import CaptureError


def _out(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _run(fn, *args, **kw) -> str:
    try:
        return _out(fn(*args, **kw))
    except CaptureError as exc:
        return _out({"error": str(exc)})


async def create_session(intent: str, tasks: list[str], workflow: str = "", package: str = "",
                         optional_tasks: Optional[list[str]] = None) -> str:
    return _run(service.create, get_store(), intent, tasks, workflow or None, package or None, optional_tasks)


async def set_plan(session_id: str, tasks: list[str], intent: str = "",
                   optional_tasks: Optional[list[str]] = None) -> str:
    def go():
        if intent:
            service.apply(get_store(), session_id, "intent", intent=intent)
        return service.apply(get_store(), session_id, "set_plan", tasks=tasks, optional=optional_tasks)
    return _run(go)


async def status(session_id: str = "") -> str:
    def go():
        store = get_store()
        return service.view(store, store.load(service.resolve(store, session_id or None)))
    return _run(go)


async def list_sessions() -> str:
    store = get_store()
    return _out({"sessions": [{"id": s.id, "intent": s.intent, "status": s.status, "workflow": s.workflow,
                               "progress": s.progress(), "imports": s.imports} for s in store.list()]})


async def complete_task(session_id: str = "", task_id: str = "") -> str:
    return _run(service.apply, get_store(), session_id or None, "complete", task_id=task_id or None)


async def skip_task(session_id: str = "", task_id: str = "") -> str:
    return _run(service.apply, get_store(), session_id or None, "skip", task_id=task_id or None)


async def add_task(title: str, session_id: str = "", description: str = "", position: str = "",
                   optional: bool = False) -> str:
    pos: Optional[object] = int(position) if position.isdigit() else (position or None)
    return _run(service.apply, get_store(), session_id or None, "add", title=title, description=description,
                position=pos, optional=optional)


async def stop(session_id: str = "") -> str:
    return _run(service.apply, get_store(), session_id or None, "stop")


async def import_session(session_id: str, package: str, workflow: str = "", name: str = "",
                         redact_values: bool = True) -> str:
    from ifs_agent_ops.recordings.importer import ImportRefused
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        pkg = find_package(package)
        result, plan = service.import_session(get_store(), session_id, pkg, workflow or None, name or None,
                                              redact=redact_values)
    except (CaptureError, ImportRefused, PackageError, FileNotFoundError) as exc:
        return _out({"error": str(exc)})
    return _out({"recording": str(result.recording), "annotations": str(result.annotations),
                 "values_redacted": result.values_redacted, "calls": result.calls, "ifs_calls": result.ifs_calls,
                 "data_flow_links": result.data_flow_links, "warnings": result.warnings,
                 "plan": {"intent": plan["intent"], "complete": plan["complete"],
                          "tasks": [{k: t[k] for k in ("id", "title", "status", "calls")} for t in plan["tasks"]]},
                 "next_step": "Analyze it with analyze_recording (calls are grouped by plan task), then "
                              "draft_operations for the package."})
