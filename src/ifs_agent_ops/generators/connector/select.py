"""Selection: the operations referenced by approved capabilities, matched to the pinned ``$openapi``.

Selection is driven by the curated capabilities only (roadmap Phase 9). An observed call, a
``depends_on`` link or a writable entity set never puts an operation in the connector by itself.

Each selected operation keeps its canonical path (the recorded request IFS accepted). The pinned
OpenAPI document supplies the operation *object* (body schema, query options, responses), found by:

- ``exact``: the same path template and method;
- ``normalized``: the same path once key values are ignored (e.g. the spec writes an enum key as
  ``Key=Ns.Enum'{Key}'`` while the recording sent ``Key=null``);
- ``bound-equivalent``: no spec path has this binding route, but the spec declares the same bound
  function/action on the same entity type through another route (IFS binds a function to the
  entity type, whichever path reaches it). Reported so a reviewer sees it.

Anything else refuses generation: the generator never invents an operation object.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ifs_agent_ops.generators.gating import GenerationRefused, gate_capability, gate_operation
from ifs_agent_ops.ifs.metadata.edmx import EdmxModel
from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.operations.model import Operation

_KEY_VALUE = re.compile(r"(\w+)=[^,()]+")
_SEGMENT = re.compile(r"^(?P<name>[^(]+)(?:\((?P<args>.*)\))?$")


@dataclass
class Selected:
    op: Operation
    capabilities: list[str] = field(default_factory=list)
    spec_path: str = ""
    spec_op: dict = field(default_factory=dict)
    match: str = ""  # exact | normalized | bound-equivalent


def shape(path: str) -> str:
    """The path with key/parameter values removed: ``/A(K='{K}')/B`` -> ``/A(K)/B``."""
    return _KEY_VALUE.sub(r"\1", path)


def segments(path: str) -> list[tuple[str, Optional[str]]]:
    out = []
    for seg in [s for s in path.split("/") if s]:
        m = _SEGMENT.match(seg)
        out.append((m.group("name"), m.group("args")) if m else (seg, None))
    return out


def selected_operations(caps: dict[str, Capability], ops: dict[str, Operation]) -> tuple[list[Selected], list[str]]:
    """Operations used by approved capabilities (gated), and the capabilities left out (with reason)."""
    chosen: dict[str, Selected] = {}
    skipped: list[str] = []
    for cap in sorted(caps.values(), key=lambda c: c.id):
        if cap.status != "approved":
            skipped.append(f"{cap.id} ({cap.status})")
            continue
        gate_capability(cap, ops)
        for step in cap.steps:
            sel = chosen.setdefault(step.operation, Selected(op=ops[step.operation]))
            if cap.id not in sel.capabilities:
                sel.capabilities.append(cap.id)
    if not chosen:
        raise GenerationRefused("no approved capability references any operation; nothing to generate"
                                + (f" (not approved: {', '.join(skipped)})" if skipped else ""))
    for sel in chosen.values():
        gate_operation(sel.op)
    return list(chosen.values()), skipped


def _entity_type_at(edmx: EdmxModel, segs: list[tuple[str, Optional[str]]]) -> Optional[str]:
    """Entity type reached by an entity-set/navigation route (None if the route is not navigable)."""
    if not segs:
        return None
    es = edmx.entity_sets.get(segs[0][0])
    current = es.entity_type if es else None
    for name, _ in segs[1:]:
        et = edmx.entity_types.get(current or "")
        nav = next((n for n in (et.navigation if et else []) if n.name == name), None)
        if nav is None:
            return None
        current = nav.target
    return current


def match_spec(op: Operation, spec: dict, edmx: EdmxModel) -> tuple[str, dict, str]:
    method = op.method.lower()
    paths = spec.get("paths", {})
    if method in paths.get(op.path_template, {}):
        return op.path_template, paths[op.path_template][method], "exact"
    target = shape(op.path_template)
    hits = [p for p in paths if method in paths[p] and shape(p) == target]
    if len(hits) == 1:
        return hits[0], paths[hits[0]][method], "normalized"
    if len(hits) > 1:
        raise GenerationRefused(f"{op.id}: {len(hits)} spec paths match {op.path_template}; ambiguous")

    segs = segments(op.path_template)
    last = shape("/" + "/".join(f"{n}({a})" if a is not None else n for n, a in segs[-1:]))
    if op.kind in ("function", "action") and len(segs) > 1 and "." in segs[-1][0]:
        bound_to = _entity_type_at(edmx, segs[:-1])
        candidates = []
        for p, item in paths.items():
            psegs = segments(p)
            if method in item and len(psegs) > 1 and shape("/" + p.rsplit("/", 1)[-1]) == last \
                    and bound_to and _entity_type_at(edmx, psegs[:-1]) == bound_to:
                candidates.append(p)
        if len(candidates) == 1:
            return candidates[0], paths[candidates[0]][method], "bound-equivalent"
        if len(candidates) > 1:
            raise GenerationRefused(f"{op.id}: {len(candidates)} spec routes declare {segs[-1][0]} on "
                                    f"{bound_to}; ambiguous")
    raise GenerationRefused(f"{op.id}: {op.method} {op.path_template} is not in the pinned $openapi of "
                            f"{op.projection}; re-snapshot the metadata or review the operation")
