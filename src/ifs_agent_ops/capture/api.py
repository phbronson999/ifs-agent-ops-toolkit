"""Local capture API for the browser extension: ``/capture/v1/...`` on 127.0.0.1.

Served by the MCP server (same port as ``/mcp``) or standalone by ``ifs-agent-ops capture serve``.

The API accepts only requests without an ``Origin`` header (local tools) or from a browser-extension
origin (``chrome-extension://``, ``extension://``). A web page the user happens to visit can't drive a
recording or inject events, because its browser always sends its own origin. Responses carry no CORS
headers: an extension with host permission for the toolkit address doesn't need them.

    GET    /capture/v1/health
    GET    /capture/v1/sessions                     list
    POST   /capture/v1/sessions                     {intent, tasks[], workflow?, package?}
    GET    /capture/v1/sessions/{id}                state, progress, requests per task
    POST   /capture/v1/sessions/{id}/intent         {intent}
    POST   /capture/v1/sessions/{id}/tasks          {title, description?, position? (1-based | "next" | "end"), ts?}
    PATCH  /capture/v1/sessions/{id}/tasks/{task}   {title?, description?}
    DELETE /capture/v1/sessions/{id}/tasks/{task}
    POST   /capture/v1/sessions/{id}/tasks/{task}/move  {position}
    POST   /capture/v1/sessions/{id}/start          {ifs_origin, ts?}
    POST   /capture/v1/sessions/{id}/complete       {ts?, task_id?}
    POST   /capture/v1/sessions/{id}/skip           {ts?, task_id?}
    POST   /capture/v1/sessions/{id}/stop           {ts?}
    POST   /capture/v1/sessions/{id}/events         {events: [...]}
"""
from __future__ import annotations

import json
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

from ifs_agent_ops.capture import service
from ifs_agent_ops.capture.session import CaptureError
from ifs_agent_ops.capture.store import CaptureStore

PREFIX = "/capture/v1"
ROUTE = PREFIX + "/{rest:path}"
METHODS = ["GET", "POST", "PATCH", "DELETE"]
_EXTENSION_ORIGINS = ("chrome-extension://", "extension://")
MAX_REQUEST_BYTES = 64 * 1024 * 1024

_store: Optional[CaptureStore] = None


def get_store() -> CaptureStore:
    global _store
    if _store is None:
        _store = CaptureStore()
    return _store


def set_store(store: Optional[CaptureStore]) -> None:
    global _store
    _store = store


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def handle(request: Request) -> JSONResponse:
    origin = request.headers.get("origin")
    if origin and not origin.startswith(_EXTENSION_ORIGINS):
        return _error(403, "the capture API accepts requests from the IFS Agent Ops extension or local tools only")
    body: dict = {}
    if request.method in ("POST", "PATCH"):
        raw = await request.body()
        if len(raw) > MAX_REQUEST_BYTES:
            return _error(413, "request too large")
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                return _error(400, "body must be JSON")
            if not isinstance(body, dict):
                return _error(400, "body must be a JSON object")
    parts = [p for p in request.path_params.get("rest", "").split("/") if p]
    try:
        return JSONResponse(_dispatch(request.method, parts, body))
    except CaptureError as exc:
        return _error(404 if str(exc).startswith("no ") else 409, str(exc))
    except (KeyError, TypeError, ValueError) as exc:
        return _error(400, f"invalid request: {exc}")


def _dispatch(method: str, parts: list[str], body: dict) -> dict:
    store = get_store()
    if parts == ["health"] and method == "GET":
        return {"ok": True, "protocol": service.PROTOCOL_VERSION, "service": "ifs-agent-ops capture"}
    if parts == ["sessions"]:
        if method == "GET":
            return {"sessions": [{"id": s.id, "intent": s.intent, "status": s.status, "workflow": s.workflow,
                                  "progress": s.progress(), "created_at": s.created_at} for s in store.list()]}
        if method == "POST":
            return service.create(store, body["intent"], list(body.get("tasks") or []),
                                  body.get("workflow") or None, body.get("package") or None)
    if len(parts) >= 2 and parts[0] == "sessions":
        sid, rest = parts[1], parts[2:]
        if not rest and method == "GET":
            return service.view(store, store.load(sid))
        if rest == ["events"] and method == "POST":
            events = body.get("events")
            if not isinstance(events, list):
                raise ValueError("events must be a list")
            return service.ingest(store, sid, events)
        if len(rest) == 1 and method == "POST" and rest[0] in ("intent", "start", "complete", "skip", "stop"):
            action = rest[0]
            if action == "intent":
                return service.apply(store, sid, "intent", intent=body["intent"])
            if action == "start":
                return service.apply(store, sid, "start", ifs_origin=body["ifs_origin"], ts=body.get("ts"))
            return service.apply(store, sid, action, ts=body.get("ts"), task_id=body.get("task_id"))
        if rest == ["tasks"] and method == "POST":
            return service.apply(store, sid, "add", title=body["title"], description=body.get("description", ""),
                                 position=body.get("position"), ts=body.get("ts"))
        if len(rest) == 2 and rest[0] == "tasks":
            if method == "PATCH":
                return service.apply(store, sid, "edit", task_id=rest[1], title=body.get("title"),
                                     description=body.get("description"))
            if method == "DELETE":
                return service.apply(store, sid, "remove", task_id=rest[1])
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] == "move" and method == "POST":
            return service.apply(store, sid, "move", task_id=rest[1], position=body["position"])
    raise CaptureError(f"no route {method} {PREFIX}/{'/'.join(parts)}")


def app():
    """Standalone ASGI app (``capture serve``)."""
    from starlette.applications import Starlette
    from starlette.routing import Route

    return Starlette(routes=[Route(ROUTE, handle, methods=METHODS)])
