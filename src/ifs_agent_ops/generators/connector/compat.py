"""Power Platform compatibility transforms (plan §5a C1, C3, C4, C5).

These are connector quirks only. None of them is written back to the canonical operation, which
keeps the original IFS path template and logical parameter names.

Path rendering also decides how a value reaches IFS:

- **entity keys** (``Set(Key='{Key}')``) keep the spec's quoting: the caller sends the bare value;
- **function parameters** (``Fn(P='{P}')``) are sent as an **OData literal** placed unquoted:
  ``'text'``, ``null``, ``Ns.Enum'Member'``, a number or a date-time. IFS needs ``null`` for
  parameters a new record does not have yet (the recordings send ``OrderNo=null``), which a quoted
  placeholder cannot express;
- **parameter aliases** (``P=@P``) stay in the path; the JSON value is a query parameter ``@P``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from ifs_agent_ops.generators.gating import GenerationRefused
from ifs_agent_ops.operations.model import Operation, TypeRef

MAX_OPERATION_ID = 300
DESCRIPTION_RANGE = (30, 500)
ENUM_INLINE = 20
ARRAY_HINT_CAP = 30
_PLACEHOLDER = re.compile(r"'\{(\w+)\}'|\{(\w+)\}")
_INTEGER = {"Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte", "Edm.SByte"}
_NUMBER = {"Edm.Decimal", "Edm.Double", "Edm.Single"}


@dataclass
class PathParam:
    name: str          # connector placeholder (unique within the path)
    logical: str       # canonical parameter it carries
    quoted: bool       # the template quotes it ('{v}')
    literal: bool      # the caller sends an OData literal (function parameter)


@dataclass
class RenderedPath:
    path: str
    params: list[PathParam] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)  # C1: "OrderNo -> OrderNo_2"


def _split_args(args: str) -> list[str]:
    out, buf, quoted = [], "", False
    for ch in args:
        if ch == "'":
            quoted = not quoted
        if ch == "," and not quoted:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def render_path(op: Operation) -> RenderedPath:
    """Connector path for ``op`` (C1 unique placeholders; literal function parameters)."""
    segs = [s for s in op.path_template.split("/") if s]
    result = RenderedPath(path="")
    seen: dict[str, int] = {}
    out = []
    for i, seg in enumerate(segs):
        m = re.match(r"^(?P<name>[^(]+)(?:\((?P<args>.*)\))?$", seg)
        if not m or not m.group("args"):
            out.append(seg)
            continue
        is_call = op.kind == "function" and i == len(segs) - 1
        parts = []
        for arg in _split_args(m.group("args")):
            key, sep, value = arg.partition("=")
            if not sep:
                key, value = "", arg
            pm = _PLACEHOLDER.fullmatch(value)
            if pm:
                logical = pm.group(1) or pm.group(2)
                quoted = pm.group(1) is not None
                seen[logical] = seen.get(logical, 0) + 1
                name = logical if seen[logical] == 1 else f"{logical}_{seen[logical]}"
                if name != logical:
                    result.renamed.append(f"{logical} -> {name}")
                result.params.append(PathParam(name, logical, quoted, literal=is_call))
                value = "{" + name + "}" if (is_call or not quoted) else "'{" + name + "}'"
            elif value.startswith("@"):
                result.aliases.append(value[1:])
            parts.append(f"{key}={value}" if key else value)
        out.append(f"{m.group('name')}({','.join(parts)})")
    result.path = "/" + "/".join(out)
    return result


def _members(t: TypeRef) -> str:
    shown = ", ".join(t.members[:ENUM_INLINE])
    more = len(t.members) - ENUM_INLINE
    return shown + (f" (and {more} more; see $metadata)" if more > 0 else "")


def literal_description(t: Optional[TypeRef], quoted: bool) -> str:
    null = "" if t is not None and not t.nullable else ", or null"
    if t is not None and t.enum:
        return (f"OData enum literal {t.enum}'<Member>'{null}. Members: {_members(t)}.")
    edm = t.edm if t else ""
    if edm == "Edm.DateTimeOffset":
        return f"Date-time literal, unquoted, e.g. 2026-12-15T00:00:00Z{null}."
    if edm == "Edm.Date":
        return f"Date literal, unquoted, e.g. 2026-12-15{null}."
    if edm in _INTEGER or edm in _NUMBER:
        return f"Number, unquoted{null}."
    if edm == "Edm.Boolean":
        return f"true or false{null}."
    return f"OData string literal in single quotes, e.g. 'ABC' (write a quote inside as ''){null}."


def path_parameter(param: PathParam, t: Optional[TypeRef]) -> dict:
    p = {"name": param.name, "in": "path", "required": True, "type": "string", "x-ms-summary": param.logical}
    edm = t.edm if t else ""
    if param.literal:
        p["description"] = literal_description(t, param.quoted)
    elif param.quoted:
        p["description"] = "Key value; the connector sends it inside quotes (write a quote inside as '')."
    elif edm in _INTEGER:
        p.update(type="integer", description="Key value (integer).")
    elif edm in _NUMBER:
        p.update(type="number", description="Key value (number).")
    elif t is not None and t.enum:
        p["description"] = f"Key value as an OData enum literal {t.enum}'<Member>'. Members: {_members(t)}."
    else:
        p["description"] = "Key value, unquoted."
    if param.name != param.logical:
        p["description"] += f" Same value as {param.logical} (repeated key in a nested path)."
    return p


def array_query_to_string(param: dict) -> bool:
    """C3: array-typed query options become comma-separated strings (Copilot Studio sends null arrays as
    ``System.Object[]``). Returns True when the parameter was changed."""
    if param.get("in") != "query" or param.get("type") != "array":
        return False
    values = [v for v in (param.get("items") or {}).get("enum", [])]
    for k in ("items", "collectionFormat", "uniqueItems"):
        param.pop(k, None)
    param["type"] = "string"
    hint = "Comma-separated list."
    if values:
        hint += " Values include: " + ", ".join(values[:ARRAY_HINT_CAP]) + (
            f" (and {len(values) - ARRAY_HINT_CAP} more)." if len(values) > ARRAY_HINT_CAP else ".")
    base = param.get("description", "").strip()
    if base and not base.endswith("."):
        base += "."
    param["description"] = (base + " " + hint).strip()
    return True


def info_description(text: str, capabilities: list[str], projections: list[str]) -> str:
    """C4: 30–500 characters, generated from the package manifest (never truncated mid-word)."""
    lo, hi = DESCRIPTION_RANGE
    desc = " ".join((text or "").split())
    desc = (desc + " " if desc else "") + (f"Operations used by the approved capabilities "
                                          f"{', '.join(capabilities)} (IFS Cloud: {', '.join(projections)}).")
    if len(desc) > hi:
        desc = desc[:hi - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    if len(desc) < lo:
        raise GenerationRefused(f"connector description is {len(desc)} characters; Power Platform needs {lo}–{hi}")
    return desc


def operation_id(op: Operation) -> str:
    """C5: the canonical ``connector_operation_id``, shortened deterministically only if over the limit."""
    oid = op.connector_operation_id
    if len(oid) <= MAX_OPERATION_ID:
        return oid
    digest = hashlib.sha1(op.id.encode()).hexdigest()[:8]
    return oid[:MAX_OPERATION_ID - 9] + "_" + digest
