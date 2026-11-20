"""Ordered view of a recording plus data flow between calls.

Unlike the grouped view, this keeps every call in recorded order (including failures,
``$batch`` and ``$metadata``) and links values a call *sends* (key predicates in the URL,
request body fields, ``If-Match``) to an earlier response that *returned* the same value.
Only field names and call positions are reported, never the values themselves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ifs_agent_ops.analysis.odata import anonymise_filter, parse_projection_url, parse_query
from ifs_agent_ops.recordings.har import ObservedCall, body_json

# Request headers worth surfacing as evidence (standard browser headers are omitted).
EVIDENCE_HEADERS = ("if-match", "if-none-match", "prefer", "x-ifs-accept-warnings",
                    "interceptoroption_ignore_notfound_error")
_KEY_PAIR_RE = re.compile(r"(\w+)=(?:'([^']*)'|([^,)'/]+))")
_IGNORED_VALUES = {"", "true", "false", "null", "*", "0", "1"}
_MAX_ITEMS_INDEXED = 50


@dataclass
class CallView:
    index: int
    method: str
    status: int
    kind: str  # ifs | ifs-batch | ifs-metadata | other
    service: Optional[str] = None
    target: Optional[str] = None
    is_action: bool = False
    filter_pattern: Optional[str] = None
    select: list[str] = field(default_factory=list)
    expand: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)   # evidence header names present
    body_keys: list[str] = field(default_factory=list)
    response: str = ""  # e.g. "collection(3)", "entity", "empty", "text", "error"
    returns_etag: bool = False
    traced: bool = False  # response came wrapped in an IFS server-trace envelope

    @property
    def failed(self) -> bool:
        return not (200 <= self.status < 300)


@dataclass
class DataLink:
    to_index: int
    to_place: str      # "key OrderNo", "body Contract", "If-Match"
    from_index: int
    from_field: str    # response field the value came from ("@odata.etag" for ETags)


def _response_shape(status: int, body: Optional[str], data: Any) -> str:
    if status >= 400:
        return "error"
    if data is None:
        return "text" if body else "empty"
    if isinstance(data, dict) and isinstance(data.get("value"), list):
        return f"collection({len(data['value'])})"
    return "entity" if isinstance(data, dict) else "text"


def _scalars(obj: Any) -> list[tuple[str, str]]:
    """(field, value) pairs of scalar fields in an entity or the first items of a collection."""
    records = obj.get("value") if isinstance(obj, dict) and isinstance(obj.get("value"), list) else [obj]
    out = []
    for rec in records[:_MAX_ITEMS_INDEXED]:
        if isinstance(rec, dict):
            for k, v in rec.items():
                if isinstance(v, bool) or v is None or isinstance(v, (dict, list)):
                    continue
                out.append((k, str(v)))
    return out


def _sent_values(call: ObservedCall, target_path: Optional[str], body: Any) -> list[tuple[str, str]]:
    sent = []
    if target_path:
        for m in _KEY_PAIR_RE.finditer(target_path.split("?")[0]):
            sent.append((f"key {m.group(1)}", m.group(2) if m.group(2) is not None else m.group(3).strip()))
    if isinstance(body, dict):
        for k, v in body.items():
            if not isinstance(v, (dict, list, bool)) and v is not None:
                sent.append((f"body {k}", str(v)))
    if_match = call.request_header("if-match")
    if if_match:
        sent.append(("If-Match", if_match))
    return sent


def build_sequence(calls: list[ObservedCall]) -> tuple[list[CallView], list[DataLink]]:
    views: list[CallView] = []
    links: list[DataLink] = []
    seen: dict[str, tuple[int, str]] = {}  # value -> (call index, response field), latest wins

    for call in calls:
        target = parse_projection_url(call.url)
        req_body = body_json(call.request_body)
        status, payload = call.payload()
        resp_data = body_json(payload)
        if target is None:
            kind = "other"
        elif target.entity_or_function == "$batch":
            kind = "ifs-batch"
        elif target.entity_or_function == "$metadata":
            kind = "ifs-metadata"
        else:
            kind = "ifs"

        view = CallView(index=call.index, method=call.method, status=status, kind=kind,
                        response=_response_shape(status, payload, resp_data), traced=call.ifs_traced)
        if target is not None:
            q = parse_query(call.url)
            view.service, view.target, view.is_action = target.service, target.entity_or_function, target.is_action
            view.filter_pattern = anonymise_filter(q.filter) if q.filter else None
            view.select, view.expand = q.select, q.expand
            view.headers = [h for h in EVIDENCE_HEADERS if call.request_header(h) is not None]
            if isinstance(req_body, dict):
                view.body_keys = list(req_body.keys())

            for place, value in _sent_values(call, target.path, req_body):
                if value in _IGNORED_VALUES or (len(value) < 2 and not value.isdigit()):
                    continue
                if value in seen:
                    src_index, src_field = seen[value]
                    links.append(DataLink(call.index, place, src_index, src_field))

        if kind == "ifs" and not view.failed and isinstance(resp_data, dict):
            for fld, value in _scalars(resp_data):
                if fld == "@odata.etag":
                    view.returns_etag = True
                if value not in _IGNORED_VALUES:
                    seen[value] = (call.index, fld)
        views.append(view)
    return views, links


def render_sequence(views: list[CallView], links: list[DataLink], *, include_other: bool = False) -> str:
    ifs = [v for v in views if v.kind != "other"]
    failed = sum(1 for v in ifs if v.failed)
    lines = [
        f"Recording: {len(views)} entries, {len(ifs)} IFS projection call(s) "
        f"({sum(v.kind == 'ifs-batch' for v in ifs)} $batch, {sum(v.kind == 'ifs-metadata' for v in ifs)} $metadata, "
        f"{failed} failed), {len(views) - len(ifs)} other.",
        *([f"IFS server tracing was on for {sum(v.traced for v in views)} call(s); payloads were unwrapped."]
          if any(v.traced for v in views) else []),
        "",
        "### Sequence (recorded order)",
    ]
    for v in views:
        if v.kind == "other" and not include_other:
            continue
        name = f"{v.service}/{v.target}" if v.service else "(non-IFS)"
        tag = " [action]" if v.is_action else (" [$batch]" if v.kind == "ifs-batch" else "")
        flags = " FAILED" if v.failed else ""
        line = f"- #{v.index} {v.method} {name}{tag} -> {v.status}{flags} {v.response}"
        if v.returns_etag:
            line += " +etag"
        lines.append(line)
        if v.filter_pattern:
            lines.append(f"  - Filter: `{v.filter_pattern}`")
        if v.select:
            lines.append(f"  - Select: {', '.join(v.select)}")
        if v.expand:
            lines.append(f"  - Expand: {', '.join(v.expand)}")
        if v.body_keys:
            lines.append(f"  - Body keys: {', '.join(v.body_keys)}")
        if v.headers:
            lines.append(f"  - Headers: {', '.join(v.headers)}")
    lines += ["", "### Data flow (value sent <- earlier response field)"]
    if links:
        for ln in links:
            lines.append(f"- #{ln.to_index} {ln.to_place} <- #{ln.from_index} {ln.from_field}")
    else:
        lines.append("- none detected")
    return "\n".join(lines)
