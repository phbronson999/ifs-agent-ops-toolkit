import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote


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


def _anonymise_filter(f: str) -> str:
    f = re.sub(r"eq '(?!IfsApp)[^']*'", "eq '{value}'", f)
    f = re.sub(r"ne '(?!IfsApp)[^']*'", "ne '{value}'", f)
    f = re.sub(r"contains\(([^,]+),\s*'[^']*'\)", r"contains(\1, '{value}')", f)
    f = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "{date}", f)
    f = re.sub(r"\beq\s+[\d.]+\b", "eq {number}", f)
    f = re.sub(r"\bgt\s+[\d.]+\b", "gt {number}", f)
    f = re.sub(r"\blt\s+[\d.]+\b", "lt {number}", f)
    return f


def _extract_entity_or_function(after_svc: str) -> tuple[str, bool]:
    parts = after_svc.split("/")
    first = parts[0].split("(")[0]
    action_part = next((p for p in parts if p.startswith("IfsApp.")), None)
    if action_part:
        action = action_part.split(".")[-1]
        return action, True
    return first, False


def _safe_json(text: str) -> Optional[dict]:
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


def _flatten_response_sample(body: dict) -> dict:
    value = body.get("value")
    if isinstance(value, list) and value:
        return _type_skeleton(value[0])
    return _type_skeleton(body)


def parse_har(file_path: str) -> ParsedHar:
    with open(file_path, encoding="utf-8") as f:
        har = json.load(f)

    entries: list = har.get("log", {}).get("entries", [])
    total_entries = len(entries)
    skipped = 0
    group_map: dict[str, OperationGroup] = {}

    proj_re = re.compile(r"/projection/v1/([^/]+)\.svc/(.+?)(\?|$)")

    for entry in entries:
        url: str = entry.get("request", {}).get("url", "")
        method: str = entry.get("request", {}).get("method", "GET").upper()
        status: int = entry.get("response", {}).get("status", 0)

        m = proj_re.search(url)
        if not m or not (200 <= status < 300):
            skipped += 1
            continue

        service = m.group(1)
        after_svc = unquote(m.group(2))
        entity, is_action = _extract_entity_or_function(after_svc)

        if entity in ("$metadata", "$batch"):
            skipped += 1
            continue

        key = f"{service}::{entity}::{method}"
        if key not in group_map:
            group_map[key] = OperationGroup(
                service=service,
                entity_or_function=entity,
                method=method,
                is_action=is_action,
            )
        g = group_map[key]
        g.call_count += 1

        # Parse query params
        q_idx = url.find("?")
        if q_idx != -1:
            qs = parse_qs(url[q_idx + 1:], keep_blank_values=True)

            for f in (qs.get("$select", [""])[0] or "").split(","):
                f = f.strip()
                if f and f not in g.select_fields:
                    g.select_fields.append(f)

            raw_filter = qs.get("$filter", [""])[0]
            if raw_filter:
                pattern = _anonymise_filter(unquote(raw_filter))
                if pattern not in g.filter_patterns:
                    g.filter_patterns.append(pattern)

            for e in (qs.get("$expand", [""])[0] or "").split(","):
                e = e.strip()
                if e and e not in g.expand:
                    g.expand.append(e)

            ob = qs.get("$orderby", [""])[0]
            if ob and ob not in g.orderby:
                g.orderby.append(ob)

            top_val = qs.get("$top", [None])[0]
            if top_val:
                try:
                    g.top = int(top_val)
                except ValueError:
                    pass

        # Request body keys
        post_text = (entry.get("request", {}).get("postData") or {}).get("text", "")
        if post_text:
            body = _safe_json(post_text)
            if body:
                for k in body:
                    if k not in g.request_body_keys:
                        g.request_body_keys.append(k)

        # First response sample
        if g.response_sample is None:
            resp_text = (entry.get("response", {}).get("content") or {}).get("text", "")
            if resp_text:
                rbody = _safe_json(resp_text)
                if rbody:
                    g.response_sample = _flatten_response_sample(rbody)

    return ParsedHar(
        total_entries=total_entries,
        filtered_count=len(group_map),
        skipped_count=skipped,
        groups=list(group_map.values()),
    )


def summarise_har(parsed: ParsedHar) -> str:
    service_map: dict[str, list[OperationGroup]] = {}
    for g in parsed.groups:
        service_map.setdefault(g.service, []).append(g)

    lines = [
        f"HAR file contained {parsed.total_entries} total requests.",
        f"Found {len(parsed.groups)} distinct IFS API operation(s) across "
        f"{len(service_map)} projection service(s). "
        f"Skipped {parsed.skipped_count} non-IFS or failed requests.",
        "",
    ]

    for service, groups in service_map.items():
        lines.append(f"### {service}.svc")
        for g in groups:
            tag = " [action]" if g.is_action else ""
            plural = "s" if g.call_count > 1 else ""
            lines.append(f"- **{g.method} {g.entity_or_function}**{tag} ({g.call_count} call{plural})")
            if g.select_fields:
                lines.append(f"  - Fields: {', '.join(g.select_fields)}")
            for p in g.filter_patterns:
                lines.append(f"  - Filter: `{p}`")
            if g.request_body_keys:
                lines.append(f"  - Body keys: {', '.join(g.request_body_keys)}")
        lines.append("")

    return "\n".join(lines)
