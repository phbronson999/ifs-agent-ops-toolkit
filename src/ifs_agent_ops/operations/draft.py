"""Draft canonical operations and workflows from a package's recordings and metadata snapshots.

Each successful IFS call in a recording (restricted to the package's projections) maps to an
operation keyed by its normalized path template: key values become ``{Key}`` placeholders and
the original IFS structure (nested navigation, repeated parent keys, bound actions) is kept.
Calls with the same template and method merge into one operation. Types, enums and parameter
requirements come from the pinned ``$metadata``; observed values contribute only their shape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ifs_agent_ops.analysis.odata import anonymize_filter, parse_projection_url, parse_query
from ifs_agent_ops.analysis.sequence import build_sequence
from ifs_agent_ops.ifs.metadata.edmx import EdmxModel, Property, _strip_collection
from ifs_agent_ops.operations.model import (
    Binding, Evidence, Header, Operation, Parameter, PathParameter, Placement, QueryOptions, Response, Step,
    TypeRef, Workflow, PlanTaskRef, RecordingPlan,
)
from ifs_agent_ops.recordings.annotations import entry_tasks, load_plan
from ifs_agent_ops.recordings.har import body_json, expand_batches, load_har
from ifs_agent_ops.workspace.package import Package

_LITERALS = {"TRUE", "FALSE", "True", "False", "Y", "N"}
_WRITE_KINDS = {"create", "update", "delete", "action"}


class DraftError(RuntimeError):
    pass


# ── path parsing ─────────────────────────────────────────────────────────────

@dataclass
class Segment:
    name: str
    args: Optional[str] = None  # raw text inside (...), None when there are no parentheses
    keys: list[tuple[Optional[str], str, bool]] = field(default_factory=list)  # (name|None, value, quoted)


def _split_outside_quotes(text: str, sep: str) -> list[str]:
    parts, buf, in_q, i = [], [], False, 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            if in_q and i + 1 < len(text) and text[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_q = not in_q
        if ch == sep and not in_q:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def parse_path(path: str) -> list[Segment]:
    """Split ``Set(K='a/b')/Nav(K2=1)/IfsApp.X.Action`` into segments, respecting quoted literals."""
    segments: list[Segment] = []
    for raw in _split_outside_quotes(path.strip("/"), "/"):
        if not raw:
            continue
        m = re.match(r"^([^()]+)(?:\((.*)\))?$", raw, re.S)
        if not m:
            raise DraftError(f"Unrecognized path segment {raw!r}")
        seg = Segment(name=m.group(1), args=m.group(2))
        if seg.args:
            for item in _split_outside_quotes(seg.args, ","):
                key, _, value = item.partition("=") if re.match(r"^\w+=", item) else ("", "", item)
                value = value.strip()
                quoted = value.startswith("'") and value.endswith("'") and len(value) >= 2
                seg.keys.append((key or None, value[1:-1].replace("''", "'") if quoted else value, quoted))
        segments.append(seg)
    return segments


# ── metadata resolution ──────────────────────────────────────────────────────

def _typeref(model: EdmxModel, type_name: str, nullable: bool = True) -> TypeRef:
    base, _ = _strip_collection(type_name)
    enum = model.enum_types.get(base)
    return TypeRef(edm=type_name, enum=enum.fqn if enum else None, members=list(enum.members) if enum else [],
                   nullable=nullable)


@dataclass
class Resolved:
    kind: str
    target: str
    entity_type: Optional[str]
    segment_types: list[Optional[str]]          # entity type in effect at each segment
    operation: Any = None                        # edmx Operation for actions/functions
    keyed_last: bool = False


def resolve(model: EdmxModel, segs: list[Segment], method: str) -> Resolved:
    first, last = segs[0], segs[-1]
    names = [s.name.split(".")[-1] if s.name.startswith("IfsApp.") else s.name for s in segs]
    target = ".".join(names)
    if len(segs) == 1 and (first.name in model.action_imports or first.name in model.function_imports):
        op = model.operation_import(first.name)
        return Resolved("action" if first.name in model.action_imports else "function", target,
                        None, [None], op)
    types: list[Optional[str]] = []
    current: Optional[str] = None
    for i, seg in enumerate(segs):
        if i == 0:
            es = model.entity_sets.get(seg.name)
            current = es.entity_type if es else None
        elif seg.name.startswith("IfsApp."):
            types.append(current)
            op = model.bound_operation(seg.name, current)
            kind = (op.kind if op else ("action" if method == "POST" else "function"))
            return Resolved(kind, target, current, types, op, keyed_last=bool(segs[i - 1].keys))
        elif seg.name.startswith("$"):
            pass  # $count / $value keep the previous type
        else:
            et = model.entity_types.get(current or "")
            nav = next((n for n in (et.navigation if et else []) if n.name == seg.name), None)
            current = nav.target if nav else None
        types.append(current)
    keyed_last = bool(last.keys)
    kind = {"GET": "read" if keyed_last else "query", "POST": "create", "PATCH": "update",
            "PUT": "update", "DELETE": "delete"}.get(method, "query")
    return Resolved(kind, target, current, types, None, keyed_last)


def _key_name(model: EdmxModel, entity_type: Optional[str], position: int) -> str:
    et = model.entity_types.get(entity_type or "")
    return et.key[position] if et and position < len(et.key) else f"Key{position + 1}"


def templatize(model: EdmxModel, segs: list[Segment], res: Resolved) -> tuple[str, dict[str, PathParameter]]:
    params: dict[str, PathParameter] = {}
    parts = []
    for i, seg in enumerate(segs):
        if seg.args is None:
            parts.append(seg.name)
            continue
        seg_type = res.segment_types[i] if i < len(res.segment_types) else None
        is_op_call = res.operation is not None and i == len(segs) - 1 and (len(segs) == 1 or seg.name.startswith("IfsApp."))
        rendered = []
        for pos, (key, value, observed_quoted) in enumerate(seg.keys):
            name = key or _key_name(model, seg_type, pos)
            if is_op_call:
                prop = next((x for x in res.operation.call_parameters if x.name == name), None)
            else:
                et = model.entity_types.get(seg_type or "")
                prop = next((x for x in (et.properties if et else []) if x.name == name), None)
            # Quoting follows the declared type: a null value is sent unquoted (Key=null) even for
            # strings, so observed quoting alone would split one operation into several templates.
            # A parameter alias (Key=@Alias, value as JSON in the query string) is how IFS sends
            # structured parameters; the alias name is the client's choice, so it is canonicalized.
            alias = not observed_quoted and value.startswith("@")
            quoted = False if alias else (prop.type == "Edm.String") if prop else observed_quoted
            ph = f"@{name}" if alias else f"'{{{name}}}'" if quoted else f"{{{name}}}"
            rendered.append(f"{name}={ph}" if key else ph)
            p = params.setdefault(name, PathParameter(name=name, placements=[]))
            placement = Placement(segment=seg.name.split(".")[-1], key=name, quoted=quoted, alias=alias or None)
            if placement not in p.placements:
                p.placements.append(placement)
            if p.type is None and prop:
                p.type = _typeref(model, prop.type, prop.nullable)
        parts.append(f"{seg.name}({','.join(rendered)})")
    return "/" + "/".join(parts), params


# ── observation helpers ──────────────────────────────────────────────────────

def value_shape(value: Any, members: list[str] = ()) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        if value in _LITERALS or value in members:
            return f"string:'{value}'"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(T[\d:.]+(Z|[+-]\d{2}:?\d{2})?)?", value):
            return "string:" + re.sub(r"\d", "9", value)
        return "string:" + re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", value))[:40]
    return type(value).__name__


def _response(model: EdmxModel, res: Resolved, payload: Any) -> Response:
    if payload is None:
        return Response(shape="empty")
    rtype = res.operation.return_type if res.operation is not None else res.entity_type
    rbase = _strip_collection(rtype)[0] if rtype else None
    if isinstance(payload, dict) and isinstance(payload.get("value"), list):
        recs = [r for r in payload["value"] if isinstance(r, dict)]
        shape = "collection" if recs or not payload["value"] else "value"
    elif isinstance(payload, dict) and "value" in payload and not isinstance(payload["value"], (dict, list)):
        return Response(shape="value", type=rtype)
    elif isinstance(payload, dict):
        recs = [payload]
        shape = "complex" if rbase in model.complex_types else "entity"
    else:
        return Response(shape="value", type=rtype)
    seen: list[str] = []
    for r in recs[:50]:
        for k in r:
            if not k.startswith("@odata.") and "@odata." not in k and k not in seen:
                seen.append(k)
    return Response(shape=shape, type=rbase, fields=seen)


# ── drafting ─────────────────────────────────────────────────────────────────

@dataclass
class Draft:
    operations: dict[str, Operation] = field(default_factory=dict)
    workflows: dict[str, Workflow] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count


def _op_id(projection: str, kind: str, target: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.]", "_", f"{projection}.{kind}.{target}")


def _merge_unique(target: list, items) -> None:
    for item in items:
        if item and item not in target:
            target.append(item)


def draft_package(package: Package, metadata: dict[str, EdmxModel]) -> Draft:
    draft = Draft()
    projections = set(package.projections)
    recordings = sorted(package.recordings_dir.rglob("*.har")) if package.recordings_dir.is_dir() else []
    for rec_path in recordings:
        rel = rec_path.relative_to(package.root).as_posix()
        calls = expand_batches(load_har(rec_path))  # $batch parts are calls in their own right
        views, links = build_sequence(calls)
        call_op: dict[tuple[int, int], str] = {}  # (index, part or 0) -> operation
        for call, view in zip(calls, views):
            if view.kind != "ifs":
                continue
            if view.service not in projections:
                draft.skipped["outside package projections"] = draft.skipped.get("outside package projections", 0) + 1
                continue
            if view.failed:
                draft.skipped["failed call"] = draft.skipped.get("failed call", 0) + 1
                continue
            model = metadata.get(view.service)
            if model is None:
                raise DraftError(f"No metadata snapshot for projection {view.service}")
            tgt = parse_projection_url(call.url)
            segs = parse_path(tgt.path)
            res = resolve(model, segs, call.method)
            template, path_params = templatize(model, segs, res)
            op_id = _op_id(view.service, res.kind, res.target)
            op = draft.operations.get(op_id)
            if op is None:
                op = draft.operations[op_id] = Operation(
                    id=op_id, connector_operation_id="", projection=view.service, kind=res.kind,
                    method=call.method, path_template=template, target=res.target, entity_type=res.entity_type,
                )
            elif op.path_template != template:
                raise DraftError(f"{op_id}: two templates map to one id ({op.path_template} vs {template})")
            for name, p in path_params.items():
                existing = next((x for x in op.path_parameters if x.name == name), None)
                if existing is None:
                    op.path_parameters.append(p)
                else:
                    _merge_unique(existing.placements, p.placements)

            q = parse_query(call.url)
            _merge_unique(op.query.select, q.select)
            _merge_unique(op.query.expand, [anonymize_filter(x) for x in q.expand])
            if q.filter:
                _merge_unique(op.query.filter_patterns, [anonymize_filter(q.filter)])
            _merge_unique(op.query.orderby, [q.orderby])
            if q.top is not None:
                op.query.top = q.top
            entity = model.entity_types.get(res.entity_type or "")
            if entity is not None:
                referenced = set(q.select) | set(re.findall(r"\b([A-Z]\w*)\s+(?:eq|ne|gt|ge|lt|le)\b", q.filter or "")) \
                    | set(re.findall(r"\b([A-Z]\w*)\b", q.orderby or ""))
                for prop in entity.properties:
                    if prop.name in referenced and prop.name not in op.query.fields:
                        op.query.fields[prop.name] = _typeref(model, prop.type, prop.nullable)

            body = body_json(call.request_body)
            declared: dict[str, Property] = {}
            if res.operation is not None:
                declared = {p.name: p for p in res.operation.call_parameters}
            elif res.entity_type in model.entity_types:
                declared = {p.name: p for p in model.entity_types[res.entity_type].properties}
            if isinstance(body, dict):
                for key, value in body.items():
                    param = next((x for x in op.body if x.name == key), None)
                    if param is None:
                        prop = declared.get(key)
                        param = Parameter(name=key, location="body", in_metadata=prop is not None,
                                          type=_typeref(model, prop.type, prop.nullable) if prop else None,
                                          required=(not prop.nullable) if (prop and res.operation is not None) else None)
                        op.body.append(param)
                    _merge_unique(param.observed, [value_shape(value, param.type.members if param.type else [])])
            if res.operation is not None and res.kind == "action":
                for prop in res.operation.call_parameters:  # declared but never observed
                    if not any(x.name == prop.name for x in op.body):
                        op.body.append(Parameter(name=prop.name, location="body", required=not prop.nullable,
                                                 type=_typeref(model, prop.type, prop.nullable)))

            needs_if_match = call.method in ("PATCH", "PUT", "DELETE") or (
                res.kind == "action" and res.keyed_last and res.operation is not None and res.operation.is_bound)
            if needs_if_match and not any(h.name == "If-Match" for h in op.headers):
                sent = call.request_header("if-match") is not None
                op.headers.append(Header(name="If-Match", required=True,
                                         reason="ETag of the entity being changed" + ("" if sent else
                                                                                      " (not seen in the recording)")))
            for h in view.headers:
                canonical = {"if-match": "If-Match", "prefer": "Prefer",
                             "x-ifs-accept-warnings": "X-IFS-Accept-Warnings"}.get(h, h)
                if not any(x.name == canonical for x in op.headers):
                    op.headers.append(Header(name=canonical, required=False, reason="observed in recording"))

            status, payload_text = call.payload()
            if op.response.shape == "unknown" or (op.response.shape == "empty" and payload_text):
                op.response = _response(model, res, body_json(payload_text))
            else:
                extra = _response(model, res, body_json(payload_text))
                _merge_unique(op.response.fields, extra.fields)
            op.evidence.append(Evidence(recording=rel, call_index=call.index, part=call.part, status=status))
            call_op[(call.index, call.part or 0)] = op_id

        for ln in links:
            to_key, from_key = (ln.to_index, ln.to_part or 0), (ln.from_index, ln.from_part or 0)
            if to_key in call_op and from_key in call_op and call_op[to_key] != call_op[from_key]:
                _merge_unique(draft.operations[call_op[to_key]].depends_on, [call_op[from_key]])

        wf_id = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(rel).relative_to("recordings").with_suffix("").as_posix().replace("/", "."))
        plan = load_plan(rec_path)
        task_of = entry_tasks(plan)
        workflow = Workflow(id=wf_id, recording=rel, plan=RecordingPlan(
            intent=plan.get("intent", ""), complete=bool(plan.get("complete")),
            tasks=[PlanTaskRef(id=t["id"], title=t["title"], status=t["status"]) for t in plan.get("tasks", [])],
        ) if plan else None)
        step_of: dict[tuple[int, int], int] = {}
        for idx in sorted(call_op):
            bindings = [Binding(to=ln.to_place, from_step=step_of[(ln.from_index, ln.from_part or 0)],
                                from_field=ln.from_field)
                        for ln in links if (ln.to_index, ln.to_part or 0) == idx
                        and (ln.from_index, ln.from_part or 0) in step_of]
            prev = workflow.steps[-1] if workflow.steps else None
            if prev and prev.operation == call_op[idx] and not bindings and prev.task == task_of.get(idx[0]):
                step_of[idx] = prev.step  # collapse consecutive repeats of the same call
                continue
            workflow.steps.append(Step(step=len(workflow.steps) + 1, operation=call_op[idx],
                                       task=task_of.get(idx[0]), bindings=bindings))
            step_of[idx] = workflow.steps[-1].step
        draft.workflows[wf_id] = workflow
    return draft
