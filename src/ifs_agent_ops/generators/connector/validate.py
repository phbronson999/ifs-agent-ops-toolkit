"""Checks on the generated connector definition (plan §5a C2, C4, C5, C7, C10, C11).

Every problem is reported with a diagnostic. The generator writes nothing when any check fails, so
a broken definition is never produced silently.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ifs_agent_ops.generators.connector.compat import DESCRIPTION_RANGE, MAX_OPERATION_ID
from ifs_agent_ops.generators.connector.prune import closure

DEFAULT_MAX_BYTES = 1_000_000
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")


@dataclass
class SizeReport:
    bytes: int
    limit: int
    largest_paths: list[tuple[str, int]] = field(default_factory=list)
    largest_definitions: list[tuple[str, int]] = field(default_factory=list)


def compact(doc: dict) -> str:
    """C10/C11: the exact text written (compact JSON, UTF-8, non-ASCII kept)."""
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


def _size(node) -> int:
    return len(compact(node).encode("utf-8"))


def size_report(doc: dict, limit: int) -> SizeReport:
    paths = sorted(((p, _size(v)) for p, v in doc.get("paths", {}).items()), key=lambda x: (-x[1], x[0]))
    defs = sorted(((d, _size(v)) for d, v in doc.get("definitions", {}).items()), key=lambda x: (-x[1], x[0]))
    return SizeReport(len(compact(doc).encode("utf-8")), limit, paths[:5], defs[:5])


def _resolve(param: dict, doc: dict) -> dict:
    ref = param.get("$ref", "")
    if ref.startswith("#/parameters/"):
        return doc.get("parameters", {}).get(ref.split("/")[-1], {})
    return param


def check(doc: dict, limit: int = DEFAULT_MAX_BYTES) -> tuple[list[str], SizeReport]:
    errors: list[str] = []
    lo, hi = DESCRIPTION_RANGE
    desc = doc.get("info", {}).get("description", "")
    if not lo <= len(desc) <= hi:
        errors.append(f"C4 info.description is {len(desc)} characters (Power Platform needs {lo}–{hi})")

    ids: dict[str, str] = {}
    for path, item in doc.get("paths", {}).items():
        placeholders = _PLACEHOLDER.findall(path)
        if len(placeholders) != len(set(placeholders)):
            errors.append(f"C1 {path}: a placeholder occurs more than once")
        shared = [_resolve(p, doc) for p in item.get("parameters", [])]
        for method in _METHODS:
            op = item.get(method)
            if op is None:
                continue
            where = f"{method.upper()} {path}"
            params = shared + [_resolve(p, doc) for p in op.get("parameters", [])]
            in_path = {p.get("name") for p in params if p.get("in") == "path"}
            for name in placeholders:
                if name not in in_path:
                    errors.append(f"C2 {where}: placeholder {{{name}}} has no path parameter")
            for name in sorted(in_path - set(placeholders)):
                errors.append(f"C2 {where}: path parameter {name} is not in the path")
            for p in params:
                if p.get("in") == "path" and p.get("required") is not True:
                    errors.append(f"C2 {where}: path parameter {p.get('name')} must be required")
                if p.get("in") == "query" and p.get("type") == "array":
                    errors.append(f"C3 {where}: query parameter {p.get('name')} is still an array")
            keys = [(p.get("in"), p.get("name")) for p in params]
            if len(keys) != len(set(keys)):
                errors.append(f"{where}: duplicate parameters")
            if sum(1 for p in params if p.get("in") == "body") > 1:
                errors.append(f"{where}: more than one body parameter")
            oid = op.get("operationId", "")
            if not oid:
                errors.append(f"C5 {where}: missing operationId")
            elif len(oid) > MAX_OPERATION_ID:
                errors.append(f"C5 {where}: operationId is {len(oid)} characters (limit {MAX_OPERATION_ID})")
            elif oid in ids:
                errors.append(f"C5 operationId {oid} is used by {ids[oid]} and {where}")
            else:
                ids[oid] = where

    components = {s: doc.get(s, {}) for s in ("definitions", "parameters", "responses")}
    _, dangling = closure({"paths": doc.get("paths", {}), **components}, components)
    errors += [f"C7 dangling $ref {r}" for r in sorted(set(dangling))]

    size = size_report(doc, limit)
    if size.bytes > limit:
        errors.append(f"C10 definition is {size.bytes} bytes, {size.bytes - limit} over the {limit}-byte limit; "
                      "largest paths: " + ", ".join(f"{p} ({b})" for p, b in size.largest_paths[:3])
                      + "; largest definitions: " + ", ".join(f"{d} ({b})" for d, b in size.largest_definitions[:3]))
    return errors, size
