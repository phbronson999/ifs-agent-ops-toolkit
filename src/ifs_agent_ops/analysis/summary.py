"""Markdown summary of the operation-group view (output of the parse_har_file tool)."""
from __future__ import annotations

from ifs_agent_ops.analysis.grouping import OperationGroup, ParsedHar


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
