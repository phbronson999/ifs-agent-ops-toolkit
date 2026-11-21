"""Turn a stopped capture session into a HAR 1.2 document plus the task plan with entry ranges.

Entries are ordered by send time, then the browser sequence number (the order requests were sent). Each request
is attributed to the task whose interval contains its send time. Tasks are sequential, so every
task's entries are one contiguous range. The HAR is the input of the existing ``record import``
boundary, which keeps entry order, so the ranges stay valid in the sanitized package recording.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

from ifs_agent_ops.capture.session import CaptureSession

CREATOR = {"name": "ifs-agent-ops capture", "version": "1"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{int(ts * 1000) % 1000:03d}Z"


def _headers(h: Optional[dict]) -> list[dict]:
    return [{"name": k, "value": v} for k, v in (h or {}).items()]


def _header(h: Optional[dict], name: str) -> Optional[str]:
    return next((v for k, v in (h or {}).items() if k.lower() == name), None)


def assemble(events: list[dict]) -> list[dict]:
    """Merge request/response/failed events into exchanges, ordered by request sequence."""
    exchanges: dict[str, dict] = {}
    for e in events:
        rid = e["request_id"]
        if e["type"] == "request":
            exchanges.setdefault(rid, {})["request"] = e
        else:
            exchanges.setdefault(rid, {})[e["type"]] = e
    out = [x for x in exchanges.values() if "request" in x]
    # Send time first (wall clock, one machine), then the browser sequence: stays ordered if the side panel
    # is reopened during a recording (a new capture core restarts its sequence).
    out.sort(key=lambda x: (x["request"]["ts"], x["request"]["seq"]))
    return out


def _entry(x: dict) -> dict:
    req, resp, failed = x["request"], x.get("response"), x.get("failed")
    url = req.get("url", "")
    post = req.get("post_data")
    entry = {
        "startedDateTime": _iso(req["ts"]),
        "time": max(0.0, ((resp or failed or req)["ts"] - req["ts"]) * 1000),
        "request": {
            "method": (req.get("method") or "GET").upper(), "url": url, "httpVersion": "HTTP/1.1",
            "headers": _headers(req.get("headers")), "cookies": [],
            "queryString": [{"name": k, "value": v} for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)],
            "headersSize": -1, "bodySize": len(post.encode("utf-8")) if post else 0,
        },
        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
    }
    if post is not None:
        entry["request"]["postData"] = {"mimeType": _header(req.get("headers"), "content-type") or "", "text": post}
    if resp:
        content = {"size": len(resp.get("body") or ""), "mimeType": resp.get("mime_type") or ""}
        if resp.get("body") is not None:
            content["text"] = resp["body"]
            if resp.get("base64"):
                content["encoding"] = "base64"
        entry["response"] = {"status": int(resp.get("status") or 0), "statusText": resp.get("status_text", ""),
                             "httpVersion": "HTTP/1.1", "headers": _headers(resp.get("headers")), "cookies": [],
                             "content": content, "redirectURL": "", "headersSize": -1, "bodySize": -1}
    else:  # failed or never answered before the recording stopped
        entry["response"] = {"status": 0, "statusText": (failed or {}).get("error_text", "no response recorded"),
                             "httpVersion": "", "headers": [], "cookies": [], "content": {"size": 0, "mimeType": ""},
                             "redirectURL": "", "headersSize": -1, "bodySize": -1}
    return entry


def export(session: CaptureSession, events: list[dict]) -> tuple[dict, dict]:
    """The HAR document and the plan annotation (task statuses with 0-based entry ranges)."""
    exchanges = assemble(events)
    owners = [session.task_at(x["request"]["ts"]) for x in exchanges]
    har = {"log": {"version": "1.2", "creator": CREATOR, "pages": [],
                   "entries": [_entry(x) for x in exchanges]}}
    return har, plan_annotation(session, owners)


def plan_annotation(session: CaptureSession, owners: list[Optional[str]]) -> dict:
    tasks = []
    for t in session.tasks:
        idx = [i for i, o in enumerate(owners) if o == t.id]
        tasks.append({"id": t.id, "title": t.title, **({"description": t.description} if t.description else {}),
                      "status": t.status, "added_during_recording": t.added_during_recording,
                      "entries": [idx[0], idx[-1]] if idx else None, "calls": len(idx)})
    unassigned = [i for i, o in enumerate(owners) if o is None]
    return {"session": session.id, "intent": session.intent, "complete": session.plan_complete,
            "tasks": tasks, "unassigned_entries": _ranges(unassigned)}


def _ranges(indices: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for i in indices:
        if out and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out

