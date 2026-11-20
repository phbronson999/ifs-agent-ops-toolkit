"""Minimal parser/serializer for OData ``$batch`` multipart bodies (``multipart/mixed``).

A batch body is a sequence of parts separated by ``--<boundary>`` lines and terminated by
``--<boundary>--``. Each part has MIME headers, a blank line and a body. ``application/http``
parts carry an embedded HTTP request (or response); ``multipart/mixed`` parts are changesets with
their own boundary. Header lines and line endings are kept verbatim, so parse → render is
lossless and only deliberately changed lines differ.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_BOUNDARY_RE = re.compile(r"boundary=\"?([^\";\r\n]+)\"?", re.I)


class _Headed:
    """Header lines kept verbatim (e.g. ``Name:value`` without a space)."""

    head_lines: list[str]

    @property
    def headers(self) -> list[tuple[str, str]]:
        return [(k.strip(), v.strip()) for k, _, v in (ln.partition(":") for ln in self.head_lines if ":" in ln)]

    def header(self, name: str) -> Optional[str]:
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)

    def set_header_value(self, index: int, value: str) -> None:
        """Replace the value of header line ``index``, keeping its separator spacing."""
        key, _, old = self.head_lines[index].partition(":")
        lead = old[: len(old) - len(old.lstrip())]
        self.head_lines[index] = f"{key}:{lead}{value}"


@dataclass
class Part(_Headed):
    head_lines: list[str]
    body: str
    nl: str = "\r\n"
    children: Optional["Multipart"] = None  # set for multipart/mixed (changeset) parts


@dataclass
class Multipart:
    boundary: str
    parts: list[Part] = field(default_factory=list)
    preamble: str = ""
    epilogue: str = ""
    nl: str = "\r\n"


@dataclass
class HttpMessage(_Headed):
    """An embedded HTTP request (``start`` = 'POST url HTTP/1.1') or response ('HTTP/1.1 201 Created')."""

    start: str
    head_lines: list[str]
    body: str
    nl: str = "\r\n"

    @property
    def is_request(self) -> bool:
        return not self.start.startswith("HTTP/")

    @property
    def method(self) -> Optional[str]:
        return self.start.split(" ", 1)[0] if self.is_request else None

    @property
    def url(self) -> Optional[str]:
        bits = self.start.split(" ")
        return bits[1] if self.is_request and len(bits) >= 2 else None

    @property
    def status(self) -> Optional[int]:
        bits = self.start.split(" ")
        return int(bits[1]) if not self.is_request and len(bits) >= 2 and bits[1].isdigit() else None

    def set_url(self, url: str) -> None:
        bits = self.start.split(" ")
        bits[1] = url
        self.start = " ".join(bits)


def boundary_of(content_type: Optional[str], body: str) -> Optional[str]:
    m = _BOUNDARY_RE.search(content_type or "")
    if m:
        return m.group(1).strip()
    first = body.lstrip("\r\n").split("\n", 1)[0].strip()
    return first[2:] if first.startswith("--") and len(first) > 2 else None


def is_multipart(text: Optional[str]) -> bool:
    return bool(text) and text.lstrip().startswith("--") and "Content-Type:" in text


def _split_head(text: str) -> tuple[list[str], str, str]:
    nl = "\r\n" if "\r\n" in text else "\n"
    head, sep, body = text.partition(nl + nl)
    if not sep:
        return text.split(nl), "", nl
    return head.split(nl), body, nl


def parse(text: str, content_type: Optional[str] = None) -> Optional[Multipart]:
    boundary = boundary_of(content_type, text)
    if not boundary:
        return None
    nl = "\r\n" if "\r\n" in text else "\n"
    delim = "--" + boundary
    chunks = re.split(r"(?m)^" + re.escape(delim) + r"(--)?[ \t]*\r?$", text)
    # re.split with a group returns [preamble, term?, chunk, term?, chunk, ...]
    mp = Multipart(boundary=boundary, preamble=chunks[0], nl=nl)
    i = 1
    while i < len(chunks):
        terminator, content = chunks[i], chunks[i + 1] if i + 1 < len(chunks) else ""
        i += 2
        if terminator == "--":
            closing = text.rfind(delim + "--")
            mp.epilogue = text[closing + len(delim) + 2:] if closing >= 0 else content
            break
        content = content[len(nl):] if content.startswith(nl) else content.lstrip("\r\n")
        if content.endswith(nl):
            content = content[: -len(nl)]
        lines, body, part_nl = _split_head(content)
        part = Part(head_lines=lines, body=body, nl=part_nl)
        ctype = part.header("Content-Type") or ""
        if ctype.lower().startswith("multipart/"):
            part.children = parse(body, ctype)
        mp.parts.append(part)
    return mp


def render(mp: Multipart) -> str:
    nl = mp.nl
    out = [mp.preamble]
    for part in mp.parts:
        body = render(part.children) if part.children is not None else part.body
        head = part.nl.join(part.head_lines)
        out.append(f"--{mp.boundary}{nl}{head}{part.nl}{part.nl}{body}{nl}")
    out.append(f"--{mp.boundary}--{mp.epilogue}")
    return "".join(out)


def parse_http(text: str) -> HttpMessage:
    lines, body, nl = _split_head(text)
    start = lines[0] if lines else ""
    return HttpMessage(start=start, head_lines=lines[1:], body=body, nl=nl)


def render_http(msg: HttpMessage) -> str:
    head = msg.nl.join([msg.start] + msg.head_lines)
    return f"{head}{msg.nl}{msg.nl}{msg.body}"


def http_parts(mp: Optional[Multipart]) -> list[HttpMessage]:
    """Embedded HTTP messages in document order, descending into changesets."""
    out: list[HttpMessage] = []
    for part in (mp.parts if mp else []):
        if part.children is not None:
            out.extend(http_parts(part.children))
        elif (part.header("Content-Type") or "").lower().startswith("application/http"):
            out.append(parse_http(part.body))
    return out


def map_http_parts(mp: Multipart, fn) -> None:
    """Apply ``fn(HttpMessage) -> HttpMessage`` to every embedded HTTP part, in place."""
    for part in mp.parts:
        if part.children is not None:
            map_http_parts(part.children, fn)
        elif (part.header("Content-Type") or "").lower().startswith("application/http"):
            part.body = render_http(fn(parse_http(part.body)))
