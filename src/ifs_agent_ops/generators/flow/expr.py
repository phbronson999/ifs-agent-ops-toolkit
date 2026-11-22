"""Workflow Definition Language (Power Automate expression) helpers for the flow generator.

A value is either a constant (a Python value written as JSON) or an expression (WDL text without the
leading ``@``). Constants stay constants as long as possible, so generated flows show literal values where
the capability has them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from ifs_agent_ops.operations.model import TypeRef

_NUMBER = {"Edm.Decimal", "Edm.Double", "Edm.Single", "Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte",
           "Edm.SByte"}
_RAW = {"Edm.Date", "Edm.DateTimeOffset", "Edm.TimeOfDay", "Edm.Duration", "Edm.Guid"} | _NUMBER


def q(text: str) -> str:
    """A WDL string literal."""
    return "'" + str(text).replace("'", "''") + "'"


def lit(value: Any) -> str:
    """A WDL literal for a Python value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return q(value)
    return f"json({q(json.dumps(value, separators=(',', ':')))})"


@dataclass(frozen=True)
class V:
    """A constant or an expression."""
    expr: Optional[str] = None
    const: Any = None

    @property
    def is_const(self) -> bool:
        return self.expr is None

    @staticmethod
    def of(value: Any) -> "V":
        return V(None, value)

    @staticmethod
    def e(expr: str) -> "V":
        return V(expr, None)

    def wdl(self) -> str:
        """As an expression (constants become literals)."""
        return lit(self.const) if self.is_const else self.expr

    def json(self) -> Any:
        """As a JSON value in an action's inputs (expressions get the leading @)."""
        if self.is_const:
            if isinstance(self.const, str) and self.const.startswith("@"):
                return "@" + self.const          # a literal leading @ is written @@
            return self.const
        return "@" + self.expr


def prop(base: str, name: str) -> str:
    return f"{base}?[{q(name)}]"


def all_of(conditions: list[str]) -> str:
    conditions = [c for c in conditions if c]
    if not conditions:
        return "true"
    return conditions[0] if len(conditions) == 1 else f"and({', '.join(conditions)})"


def any_of(conditions: list[str]) -> str:
    return conditions[0] if len(conditions) == 1 else f"or({', '.join(conditions)})"


def missing(expr: str, input_type: str) -> str:
    """True when an input value is absent (empty text and empty lists count as absent)."""
    if input_type in ("string", "date", "datetime"):
        return f"empty(coalesce({expr}, ''))"
    if input_type == "list":
        return f"empty(coalesce({expr}, json('[]')))"
    return f"equals({expr}, null)"


def concat(parts: list[V]) -> V:
    """Text concatenation; stays constant when every part is."""
    if all(p.is_const for p in parts):
        return V.of("".join("" if p.const is None else str(p.const) for p in parts))
    merged: list[V] = []
    for p in parts:
        if p.is_const and merged and merged[-1].is_const:
            merged[-1] = V.of(str(merged[-1].const) + str(p.const))
        else:
            merged.append(p)
    return V.e(merged[0].wdl() if len(merged) == 1 else f"concat({', '.join(p.wdl() for p in merged)})")


def odata_string(v: V) -> V:
    """OData string literal ('text', quotes doubled) or null."""
    if v.is_const:
        return V.of("null" if v.const is None else "'" + str(v.const).replace("'", "''") + "'")
    e = v.expr
    return V.e(f"if(equals({e}, null), 'null', concat('''', replace(string({e}), '''', ''''''), ''''))")


def odata_literal(v: V, t: Optional[TypeRef]) -> V:
    """The OData literal for a value of a declared type (function parameters and $filter)."""
    enum = t.enum if t is not None else None
    edm = t.edm if t is not None else None
    if enum:
        if v.is_const:
            return V.of("null" if v.const is None else f"{enum}'{v.const}'")
        return V.e(f"if(equals({v.expr}, null), 'null', concat({q(enum + chr(39))}, string({v.expr}), ''''))")
    if edm == "Edm.Boolean":
        if v.is_const:
            return V.of("null" if v.const is None else ("true" if v.const in (True, "true", "TRUE") else "false"))
        return V.e(f"if(equals({v.expr}, null), 'null', toLower(string({v.expr})))")
    if edm in _RAW:
        if v.is_const:
            return V.of("null" if v.const is None else str(v.const))
        return V.e(f"if(equals({v.expr}, null), 'null', string({v.expr}))")
    return odata_string(v)


def transform(v: V, kind: Optional[str]) -> V:
    if not kind:
        return v
    if v.is_const and v.const is None:
        return v
    e = v.wdl()
    if kind == "datetime":       # a business calendar date as IFS accepted it: YYYY-MM-DDT00:00:00Z
        return V.e(f"if(equals({e}, null), null, concat(formatDateTime({e}, 'yyyy-MM-dd'), 'T00:00:00Z'))")
    if kind == "date":
        return V.e(f"if(equals({e}, null), null, formatDateTime({e}, 'yyyy-MM-dd'))")
    if kind == "local-datetime":
        return V.e(f"if(equals({e}, null), null, formatDateTime({e}, 'yyyy-MM-ddTHH:mm:ss'))")
    if kind == "string":
        return V.e(f"if(equals({e}, null), null, string({e}))")
    if kind == "number":
        return V.e(f"if(equals({e}, null), null, float({e}))")
    raise ValueError(f"unknown transform {kind!r}")
