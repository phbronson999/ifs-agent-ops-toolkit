"""Recognise IFS projection calls in URLs and parse their OData query options."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote

_PROJECTION_RE = re.compile(r"/projection/v1/([^/]+)\.svc/(.+?)(\?|$)")


@dataclass
class ProjectionTarget:
    service: str             # projection name, e.g. CustomerOrderHandling
    path: str                # URL-decoded part after "<service>.svc/" (no query string)
    entity_or_function: str  # first segment name, or the bound action/function name
    is_action: bool          # an IfsApp.* bound action/function segment is present


@dataclass
class ODataQuery:
    select: list[str] = field(default_factory=list)
    filter: Optional[str] = None   # URL-decoded
    expand: list[str] = field(default_factory=list)
    orderby: Optional[str] = None
    top: Optional[int] = None
    raw: dict[str, list[str]] = field(default_factory=dict)


def extract_entity_or_function(after_svc: str) -> tuple[str, bool]:
    parts = after_svc.split("/")
    first = parts[0].split("(")[0]
    action_part = next((p for p in parts if p.startswith("IfsApp.")), None)
    if action_part:
        return action_part.split(".")[-1], True
    return first, False


def parse_projection_url(url: str) -> Optional[ProjectionTarget]:
    m = _PROJECTION_RE.search(url)
    if not m:
        return None
    after_svc = unquote(m.group(2))
    name, is_action = extract_entity_or_function(after_svc)
    return ProjectionTarget(service=m.group(1), path=after_svc, entity_or_function=name, is_action=is_action)


def parse_query(url: str) -> ODataQuery:
    q = ODataQuery()
    q_idx = url.find("?")
    if q_idx == -1:
        return q
    qs = parse_qs(url[q_idx + 1:], keep_blank_values=True)
    q.raw = qs
    q.select = [s.strip() for s in (qs.get("$select", [""])[0] or "").split(",") if s.strip()]
    raw_filter = qs.get("$filter", [""])[0]
    q.filter = unquote(raw_filter) if raw_filter else None
    q.expand = [e.strip() for e in (qs.get("$expand", [""])[0] or "").split(",") if e.strip()]
    q.orderby = qs.get("$orderby", [""])[0] or None
    top_val = qs.get("$top", [None])[0]
    if top_val:
        try:
            q.top = int(top_val)
        except ValueError:
            pass
    return q


def anonymise_filter(f: str) -> str:
    """Replace literal values in a $filter with placeholders; IfsApp enum literals are kept."""
    f = re.sub(r"eq '(?!IfsApp)[^']*'", "eq '{value}'", f)
    f = re.sub(r"ne '(?!IfsApp)[^']*'", "ne '{value}'", f)
    f = re.sub(r"contains\(([^,]+),\s*'[^']*'\)", r"contains(\1, '{value}')", f)
    f = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "{date}", f)
    f = re.sub(r"\beq\s+[\d.]+\b", "eq {number}", f)
    f = re.sub(r"\bgt\s+[\d.]+\b", "gt {number}", f)
    f = re.sub(r"\blt\s+[\d.]+\b", "lt {number}", f)
    return f
