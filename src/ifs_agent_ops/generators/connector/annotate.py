"""Agent-facing annotations rendered from metadata facts (plan §5a C8, C9).

- C8: the ``$filter`` description of a read shows the IFS enum literal syntax
  ``Namespace.EnumType'Member'`` for each enum property of the entity type actually returned (each
  enum with its own namespace), plus the filters observed in the recordings.
- C9: the ``$select`` description of a read lists the available fields (GET only, capped), making
  up for C3 removing the ``$select`` enum.
- OData specification links (``see [OData ...](...)``) are removed from descriptions.

Descriptions never name a definition that pruning might remove: they are plain text, not ``$ref``.
"""
from __future__ import annotations

import re
from typing import Optional

from ifs_agent_ops.ifs.metadata.edmx import EdmxModel
from ifs_agent_ops.operations.model import Operation

FIELD_CAP = 60
EXCLUDED_FIELDS = {"luname", "keyref", "Objgrants"}
_LINK = re.compile(r",?\s*see \[[^\]]*\]\([^)]*\)")


def strip_links(param: dict) -> bool:
    desc = param.get("description")
    if not desc or "](" not in desc:
        return False
    param["description"] = _LINK.sub("", desc).strip()
    return True


def _returned_type(op: Operation, edmx: EdmxModel) -> Optional[str]:
    if op.response.type and op.response.type in edmx.entity_types:
        return op.response.type
    return op.entity_type if op.entity_type in edmx.entity_types else None


def filter_description(op: Operation, edmx: EdmxModel) -> Optional[str]:
    type_fqn = _returned_type(op, edmx)
    if type_fqn is None:
        return None
    text = ["Filter items by property values (OData $filter), e.g. CustomerNo eq 'ABC'."]
    for prop, enum in edmx.enum_properties(type_fqn).items():
        members = ", ".join(enum.members[:20]) + (f" (and {len(enum.members) - 20} more)"
                                                  if len(enum.members) > 20 else "")
        text.append(f"{prop} is an enum: write {prop} eq {enum.fqn}'<Member>'; members: {members}.")
    if op.query.filter_patterns:
        text.append("Observed: " + "; ".join(op.query.filter_patterns) + ".")
    return " ".join(text)


def select_description(op: Operation, edmx: EdmxModel) -> Optional[str]:
    type_fqn = _returned_type(op, edmx)
    if type_fqn is None:
        return None
    fields = [p.name for p in edmx.entity_types[type_fqn].properties if p.name not in EXCLUDED_FIELDS]
    shown = ", ".join(fields[:FIELD_CAP])
    more = f" (and {len(fields) - FIELD_CAP} more)" if len(fields) > FIELD_CAP else ""
    observed = f" Observed: {', '.join(op.query.select)}." if op.query.select else ""
    return f"Comma-separated properties to return. AVAILABLE FIELDS: {shown}{more}.{observed}"


def annotate_read(op_obj: dict, op: Operation, edmx: EdmxModel) -> int:
    """Annotate the inline ``$filter``/``$select`` parameters of a GET; returns the number changed."""
    changed = 0
    for param in op_obj.get("parameters", []):
        name = param.get("name")
        text = (filter_description(op, edmx) if name == "$filter"
                else select_description(op, edmx) if name == "$select" else None)
        if text:
            param["description"] = text
            changed += 1
    return changed
