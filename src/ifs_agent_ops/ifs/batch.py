"""OData $batch with one change set: requests IFS only accepts together (atomically, in one database transaction).

Some IFS saves depend on framework calls in the same request, for example ``PrePostSave(Init true)``, the PATCH and
``PrePostSave(Init false)``: sent one by one the PATCH fails (ORA-06531, the setup state does not survive between
HTTP requests). The IFS web client sends them as a multipart ``$batch`` holding one ``multipart/mixed`` change set,
each part with a ``Content-ID``; IFS answers with the same structure, one HTTP response per part.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

CRLF = "\r\n"
JSON_TYPE = "application/json;IEEE754Compatible=true"
PART_ACCEPT = "application/json;odata.metadata=full;IEEE754Compatible=true"


@dataclass
class BatchRequest:
    """One request inside the change set. ``url`` is relative to the projection service root
    (``ShopOrds(OrderNo='1',...)/OperationArray(...)`` or ``PrePostSave``)."""
    method: str
    url: str
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class BatchPart:
    content_id: Optional[str]
    status: int
    headers: dict[str, str]
    data: Any            # parsed JSON body, the text when it is not JSON, or None when empty
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def build_changeset(requests: list[BatchRequest], *, batch_boundary: str | None = None,
                    changeset_boundary: str | None = None) -> tuple[str, str]:
    """Return (content type, body) of a ``$batch`` with all ``requests`` in one change set, in order."""
    if not requests:
        raise ValueError("a change set needs at least one request")
    b = batch_boundary or f"batch_{uuid.uuid4()}"
    c = changeset_boundary or f"changeset_{uuid.uuid4()}"
    lines = [f"--{b}", f"Content-Type: multipart/mixed;boundary={c}", ""]
    for n, r in enumerate(requests, 1):
        method = r.method.upper()
        if method == "GET":
            raise ValueError("a change set holds writes only (POST, PATCH, PUT, DELETE); read before or after it")
        lines += [f"--{c}", "Content-Type: application/http", "Content-Transfer-Encoding: binary",
                  f"Content-ID: {n}", "", f"{method} {r.url.lstrip('/')} HTTP/1.1", f"Accept: {PART_ACCEPT}"]
        headers = dict(r.headers)
        payload = ""
        if r.body is not None:
            headers.setdefault("Content-Type", JSON_TYPE)
            payload = json.dumps(r.body, separators=(",", ":"))
        lines += [f"{k}: {v}" for k, v in headers.items()]
        lines += ["", payload]
    lines += [f"--{c}--", f"--{b}--", ""]
    return f"multipart/mixed;boundary={b}", CRLF.join(lines)


def _boundary(content_type: str) -> str:
    m = re.search(r'boundary="?([^";]+)"?', content_type or "", re.I)
    if not m:
        raise ValueError(f"no multipart boundary in content type: {content_type!r}")
    return m.group(1).strip()


def _split(text: str, boundary: str) -> list[str]:
    parts = re.split(rf"\r?\n?--{re.escape(boundary)}(?:--)?[ \t]*\r?\n?", text)
    return [p for p in parts if p.strip()]


def _headers_and_body(block: str) -> tuple[dict[str, str], str]:
    pieces = re.split(r"\r?\n\r?\n", block.lstrip("\r\n"), maxsplit=1)
    head, body = pieces[0], (pieces[1] if len(pieces) > 1 else "")
    headers = {}
    for line in re.split(r"\r?\n", head):
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return headers, body


def _http_part(block: str) -> BatchPart:
    outer, inner = _headers_and_body(block)
    status_line, _, rest = inner.lstrip("\r\n").partition("\n")
    m = re.match(r"HTTP/1\.\d\s+(\d{3})", status_line.strip())
    if not m:
        raise ValueError(f"not an HTTP response part: {status_line[:80]!r}")
    headers, body = _headers_and_body(rest)
    body = body.strip()
    data: Any = None
    if body:
        try:
            data = json.loads(body)
        except ValueError:
            data = body
    return BatchPart(outer.get("content-id"), int(m.group(1)), headers, data, body)


def parse_batch_response(text: str, content_type: str) -> list[BatchPart]:
    """Flatten a ``$batch`` response into its HTTP parts (change set parts included, in order)."""
    out: list[BatchPart] = []
    for block in _split(text, _boundary(content_type)):
        headers, body = _headers_and_body(block)
        ctype = headers.get("content-type", "")
        if ctype.lower().startswith("multipart/mixed"):
            out += [_http_part(p) for p in _split(body, _boundary(ctype))]
        elif ctype.lower().startswith("application/http"):
            out.append(_http_part(block))
    return out
