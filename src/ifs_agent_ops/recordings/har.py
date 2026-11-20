"""Load a browser HAR file into an ordered list of observed calls.

This is the *evidence* layer: every entry is kept, in recorded order, with request and
response headers, bodies and status codes. Nothing is filtered, grouped or anonymised here;
analysis (``ifs_agent_ops.analysis``) builds views on top of this list.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


@dataclass
class ObservedCall:
    index: int                      # position in the HAR (0-based); the recorded order
    method: str
    url: str
    status: int
    started: Optional[str] = None   # startedDateTime
    time_ms: Optional[float] = None
    pageref: Optional[str] = None
    resource_type: Optional[str] = None  # Chrome's _resourceType (xhr, fetch, document, ...)
    request_headers: list[tuple[str, str]] = field(default_factory=list)
    request_body: Optional[str] = None
    request_mime: Optional[str] = None
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    response_body: Optional[str] = None      # content.text exactly as stored
    response_encoding: Optional[str] = None  # content.encoding (e.g. "base64")
    response_mime: Optional[str] = None
    part: Optional[int] = None  # 1-based position inside a $batch (index is then the batch call's)

    def request_header(self, name: str) -> Optional[str]:
        return _header(self.request_headers, name)

    def response_header(self, name: str) -> Optional[str]:
        return _header(self.response_headers, name)

    def response_text(self) -> Optional[str]:
        """Response body decoded from base64 when the HAR stored it that way."""
        if self.response_body is None or self.response_encoding != "base64":
            return self.response_body
        try:
            return base64.b64decode(self.response_body).decode("utf-8", "replace")
        except Exception:
            return None

    @property
    def ifs_traced(self) -> bool:
        """True when IFS server tracing wrapped the response in an ``ifs-trace`` envelope."""
        return _trace_envelope(body_json(self.response_text())) is not None

    def payload(self) -> tuple[int, Optional[str]]:
        """(status, body) of the actual IFS response.

        With IFS server tracing on, the browser receives ``{"request", "response", "server",
        "ifs-trace"}`` and the real OData body is the string ``response.body`` with status
        ``response.status.code``. Otherwise this is simply (HTTP status, response text).
        """
        text = self.response_text()
        env = _trace_envelope(body_json(text))
        if env is None:
            return self.status, text
        resp = env["response"]
        code = str((resp.get("status") or {}).get("code", ""))
        body = resp.get("body")
        if body is not None and not isinstance(body, str):
            body = json.dumps(body)
        return (int(code) if code.isdigit() else self.status), body


def _trace_envelope(data) -> Optional[dict]:
    if isinstance(data, dict) and "ifs-trace" in data and isinstance(data.get("response"), dict):
        return data
    return None


def _header(headers: list[tuple[str, str]], name: str) -> Optional[str]:
    lname = name.lower()
    return next((v for k, v in headers if k.lower() == lname), None)


def _pairs(items) -> list[tuple[str, str]]:
    return [(str(h.get("name", "")), str(h.get("value", ""))) for h in (items or []) if isinstance(h, dict)]


def calls_from_har(har: dict) -> list[ObservedCall]:
    calls: list[ObservedCall] = []
    for i, entry in enumerate(har.get("log", {}).get("entries", [])):
        req = entry.get("request", {}) or {}
        resp = entry.get("response", {}) or {}
        post = req.get("postData") or {}
        content = resp.get("content") or {}
        calls.append(ObservedCall(
            index=i,
            method=str(req.get("method", "GET")).upper(),
            url=str(req.get("url", "")),
            status=resp.get("status", 0),
            started=entry.get("startedDateTime"),
            time_ms=entry.get("time"),
            pageref=entry.get("pageref"),
            resource_type=entry.get("_resourceType"),
            request_headers=_pairs(req.get("headers")),
            request_body=post.get("text") or None,
            request_mime=post.get("mimeType"),
            response_headers=_pairs(resp.get("headers")),
            response_body=content.get("text") or None,
            response_encoding=content.get("encoding"),
            response_mime=content.get("mimeType"),
        ))
    return calls


def load_har(path: Union[str, Path]) -> list[ObservedCall]:
    with open(path, encoding="utf-8") as f:
        return calls_from_har(json.load(f))


def _response_content_type(call: ObservedCall) -> Optional[str]:
    env = _trace_envelope(body_json(call.response_text()))
    if env is not None:
        headers = (env.get("response") or {}).get("headers") or {}
        return next((v for k, v in headers.items() if str(k).lower() == "content-type"), None)
    return call.response_mime or call.response_header("content-type")


def batch_parts(call: ObservedCall) -> list[ObservedCall]:
    """The requests inside an OData ``$batch`` call, paired with their responses by position."""
    from ifs_agent_ops.recordings import multipart

    if not call.url.split("?")[0].endswith("$batch") or not multipart.is_multipart(call.request_body):
        return []
    requests = multipart.http_parts(multipart.parse(call.request_body, call.request_mime
                                                    or call.request_header("content-type")))
    _status, payload = call.payload()
    responses = multipart.http_parts(multipart.parse(payload, _response_content_type(call))) \
        if multipart.is_multipart(payload) else []
    service_root = call.url.split("?")[0][: -len("$batch")]
    parts = []
    for n, req in enumerate(requests, start=1):
        resp = responses[n - 1] if n - 1 < len(responses) else None
        url = req.url or ""
        parts.append(ObservedCall(
            index=call.index, part=n, method=(req.method or "GET").upper(),
            url=url if re.match(r"^https?://", url) else service_root + url.lstrip("/"),
            status=(resp.status or 0) if resp else 0, started=call.started, pageref=call.pageref,
            resource_type=call.resource_type, request_headers=req.headers, request_body=req.body or None,
            request_mime=req.header("Content-Type"), response_headers=resp.headers if resp else [],
            response_body=(resp.body or None) if resp else None,
            response_mime=resp.header("Content-Type") if resp else None,
        ))
    return parts


def expand_batches(calls: list[ObservedCall]) -> list[ObservedCall]:
    """Calls in recorded order with each ``$batch`` followed by its parts. Idempotent."""
    if any(c.part is not None for c in calls):
        return list(calls)
    out: list[ObservedCall] = []
    for call in calls:
        out.append(call)
        out.extend(batch_parts(call))
    return out


def body_json(text: Optional[str]):
    """Parse a body as JSON; None when absent or not JSON."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None
