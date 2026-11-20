"""Deduplicated "operation group" view over observed calls (the legacy HAR summary model).

Successful IFS projection calls are grouped by (service, entity-or-action, method) and their
query options, request body keys and a response type-skeleton are merged. This view is
lossy by design (no order, no values, no failed calls, no $batch); the full evidence stays
in the ``ObservedCall`` list it is built from.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ifs_agent_ops.analysis.odata import anonymise_filter, parse_projection_url, parse_query
from ifs_agent_ops.recordings.har import ObservedCall


@dataclass
class OperationGroup:
    service: str
    entity_or_function: str
    method: str
    select_fields: list[str] = field(default_factory=list)
    filter_patterns: list[str] = field(default_factory=list)
    expand: list[str] = field(default_factory=list)
    orderby: list[str] = field(default_factory=list)
    top: Optional[int] = None
    request_body_keys: list[str] = field(default_factory=list)
    response_sample: Optional[dict] = None
    call_count: int = 0
    is_action: bool = False


@dataclass
class ParsedHar:
    total_entries: int
    filtered_count: int
    skipped_count: int
    groups: list[OperationGroup]


def _json_object(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _type_skeleton(obj: object) -> dict:
    if not isinstance(obj, dict):
        return {}
    result = {}
    for k, v in obj.items():
        if v is None:
            result[k] = "null"
        elif isinstance(v, dict):
            result[k] = "{object}"
        else:
            result[k] = type(v).__name__
    return result


def _response_sample(body: dict) -> dict:
    value = body.get("value")
    if isinstance(value, list) and value:
        return _type_skeleton(value[0])
    return _type_skeleton(body)


def _add_unique(target: list, items: Iterable) -> None:
    for item in items:
        if item and item not in target:
            target.append(item)


def group_calls(calls: list[ObservedCall]) -> ParsedHar:
    skipped = 0
    groups: dict[str, OperationGroup] = {}

    for call in calls:
        target = parse_projection_url(call.url)
        if not target or not (200 <= call.status < 300):
            skipped += 1
            continue
        if target.entity_or_function in ("$metadata", "$batch"):
            skipped += 1
            continue

        key = f"{target.service}::{target.entity_or_function}::{call.method}"
        g = groups.get(key)
        if g is None:
            g = groups[key] = OperationGroup(
                service=target.service,
                entity_or_function=target.entity_or_function,
                method=call.method,
                is_action=target.is_action,
            )
        g.call_count += 1

        q = parse_query(call.url)
        _add_unique(g.select_fields, q.select)
        if q.filter:
            _add_unique(g.filter_patterns, [anonymise_filter(q.filter)])
        _add_unique(g.expand, q.expand)
        _add_unique(g.orderby, [q.orderby])
        if q.top is not None:
            g.top = q.top

        body = _json_object(call.request_body)
        if body:
            _add_unique(g.request_body_keys, body.keys())

        if g.response_sample is None:
            rbody = _json_object(call.response_body)
            if rbody:
                g.response_sample = _response_sample(rbody)

    return ParsedHar(
        total_entries=len(calls),
        filtered_count=len(groups),
        skipped_count=skipped,
        groups=list(groups.values()),
    )
