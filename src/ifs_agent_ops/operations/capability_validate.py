"""Validation of curated capabilities against the canonical operations they reference."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from ifs_agent_ops.operations.capability import Capability, Source, Step
from ifs_agent_ops.operations.model import Operation

Severity = Literal["error", "warning", "info"]

# Capability input type -> compatible canonical EDM types (transforms widen this, see below).
_COMPATIBLE = {
    "string": {"Edm.String", "Edm.Guid"},
    "decimal": {"Edm.Decimal", "Edm.Double", "Edm.Single"},
    "integer": {"Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte", "Edm.Decimal"},
    "boolean": {"Edm.Boolean"},
    "date": {"Edm.Date"},
    "datetime": {"Edm.DateTimeOffset"},
}
_TRANSFORM_TARGET = {"date": {"Edm.Date"}, "datetime": {"Edm.DateTimeOffset"}, "local-datetime": {"Edm.DateTimeOffset"},
                     "string": {"Edm.String"}, "number": {"Edm.Decimal", "Edm.Double", "Edm.Int32", "Edm.Int64"}}


@dataclass
class Finding:
    capability: str
    where: str
    rule: str
    severity: Severity
    message: str


@dataclass
class FieldSource:
    """How one canonical body field of a write step is supplied (for review and generators)."""
    field: str
    kind: str          # input | item | step | config | derived | fixed | seeded | omitted
    detail: str
    canonical_type: Optional[str]
    required: Optional[bool]


def _target(op: Operation, target: str):
    loc, _, name = target.partition(".")
    if loc == "body":
        return next((p for p in op.body if p.name == name), None)
    if loc == "path":
        return next((p for p in op.path_parameters if p.name == name), None)
    if loc == "filter":
        return op.query.fields.get(name) or (name in op.response.fields or None)
    if loc == "header":
        return next((h for h in op.headers if h.name.lower() == name.lower()), None)
    return None


def _edm(target_obj) -> Optional[str]:
    t = getattr(target_obj, "type", None)
    if t is not None and hasattr(t, "edm"):
        return t.edm
    if hasattr(target_obj, "edm"):
        return target_obj.edm
    return None


def field_sources(cap: Capability, step: Step, op: Operation, ops: dict[str, Operation]) -> list[FieldSource]:
    """Classify every canonical body field of ``step`` (the review table)."""
    out = []
    seeded: dict[str, str] = {}
    for sid in step.body_from:
        src = cap.step(sid)
        src_op = ops.get(src.operation) if src else None
        for f in (src_op.response.fields if src_op else []):
            seeded[f] = sid
    for p in op.body:
        edm = p.type.edm if p.type else None
        b = step.bindings.get(f"body.{p.name}")
        if b is not None:
            detail = {"input": f"input {b.input}", "item": f"item {b.item}", "step": f"{b.step}.{b.field}",
                      "config": f"config {b.config}", "derived": f"derived: {b.derived}",
                      "fixed": f"fixed {b.fixed!r}"}[b.kind]
            if b.transform:
                detail += f" ({b.transform})"
            out.append(FieldSource(p.name, b.kind, detail, edm, p.required))
        elif p.name in step.omit:
            out.append(FieldSource(p.name, "omitted", "explicitly omitted", edm, p.required))
        elif p.name in seeded:
            out.append(FieldSource(p.name, "seeded", f"from {seeded[p.name]} record", edm, p.required))
        else:
            out.append(FieldSource(p.name, "omitted", "not supplied", edm, p.required))
    return out


def validate_capability(cap: Capability, ops: dict[str, Operation]) -> list[Finding]:
    out: list[Finding] = []

    def add(where, rule, severity, message):
        out.append(Finding(cap.id, where, rule, severity, message))

    inputs = cap.all_inputs()
    step_ids = [s.id for s in cap.steps]
    if len(set(step_ids)) != len(step_ids):
        add("steps", "duplicate-step-id", "error", "step ids must be unique")
    position = {sid: i for i, sid in enumerate(step_ids)}

    for name, cv in cap.config.items():
        if cv.value is None:
            add(f"config.{name}", "config-unset", "error", "configuration value not set (organization decision)")

    for i, step in enumerate(cap.steps):
        where = f"step {step.id}"
        op = ops.get(step.operation)
        if op is None:
            add(where, "unknown-operation", "error", f"operation {step.operation} does not exist")
            continue
        if op.status != "approved":
            add(where, "operation-not-approved", "error",
                f"{step.operation} is not approved (technical review comes before capability approval)")
        if step.role == "write" and op.kind not in ("create", "update", "delete", "action"):
            add(where, "role-mismatch", "warning", f"role write but operation kind is {op.kind}")
        if step.for_each and (step.for_each not in inputs or inputs[step.for_each].type != "list"):
            add(where, "bad-for-each", "error", f"for_each {step.for_each!r} is not a list input")
        if step.requires_input and step.requires_input not in inputs:
            add(where, "unknown-input", "error", f"requires_input {step.requires_input!r} is not an input")

        for sid in step.body_from:
            if sid not in position or position[sid] >= i:
                add(where, "bad-body-from", "error", f"body_from {sid!r} must name an earlier step")

        for target, src in step.bindings.items():
            tobj = _target(op, target)
            if not tobj:
                add(where, "unknown-target", "error", f"{target} is not a parameter of {op.id}")
                continue
            _check_source(cap, step, i, target, src, tobj, inputs, position, ops, add)

        if step.role == "write":
            for fs in field_sources(cap, step, op, ops):
                if fs.required and fs.kind == "omitted":
                    add(where, "required-unbound", "error", f"required body field {fs.field} is not supplied")
            for p in op.path_parameters:
                if f"path.{p.name}" not in step.bindings:
                    add(where, "path-unbound", "error", f"path parameter {p.name} is not bound")
        for name in step.omit:
            if not any(p.name == name for p in op.body):
                add(where, "unknown-omit", "warning", f"omit {name}: not a body field of {op.id}")

    referenced = {s for a in cap.actions for s in a.steps}
    for a in cap.actions:
        where = f"action {a.name}"
        for sid in a.steps:
            if sid not in position:
                add(where, "unknown-step", "error", f"step {sid!r} does not exist")
        writes = [sid for sid in a.steps if sid in position and cap.steps[position[sid]].role == "write"]
        if writes and not a.requires_confirmation:
            add(where, "write-without-confirmation", "error", "actions that write must require confirmation")
        for c in a.confirm:
            if c not in inputs and c not in {o.name for o in cap.outputs}:
                add(where, "unknown-confirm", "error", f"confirm {c!r} is neither an input nor an output")
    for sid in step_ids:
        if sid not in referenced:
            add(f"step {sid}", "unreachable-step", "warning", "step is not part of any action")

    for q in cap.questions:
        if q.asks not in inputs:
            add(f"question {q.id}", "unknown-input", "error", f"asks {q.asks!r}, which is not an input")
    asked = {q.asks for q in cap.questions}
    for name, inp in inputs.items():
        if inp.source == "user" and inp.required and name not in asked:
            add(f"input {name}", "user-input-without-question", "error", "required user input has no question")
        if inp.resolve and inp.resolve not in position:
            add(f"input {name}", "unknown-resolve-step", "error", f"resolve step {inp.resolve!r} does not exist")
        if not _input_used(cap, name):
            add(f"input {name}", "unused-input", "warning", "input is never bound to any step")

    for o in cap.outputs:
        if o.step not in position:
            add(f"output {o.name}", "unknown-step", "error", f"step {o.step!r} does not exist")
            continue
        op = ops.get(cap.steps[position[o.step]].operation)
        if op and o.field not in op.response.fields and not (op.response.shape == "value" and o.field == "value"):
            add(f"output {o.name}", "unobserved-field", "warning",
                f"{o.field} was not observed in {op.id} responses")

    if cap.status == "approved":
        _check_approval(cap, ops, add)
    for note in cap.review_notes:
        add("review", "open-question", "info", note)
    for req in cap.runtime_validation.requirements:
        add("runtime", "runtime-validation", "info", req)
    for x in cap.excluded_calls:
        if x.call in {s.operation.split(".")[-1] for s in cap.steps}:
            add(f"excluded {x.call}", "excluded-but-used", "error", f"{x.call} is listed as excluded but a step uses it")
    return out


def _input_used(cap: Capability, name: str) -> bool:
    top, _, item = name.partition(".")
    for s in cap.steps:
        for b in s.bindings.values():
            if b.input == name or (item and b.item == item and s.for_each == top) or (not item and s.for_each == name):
                return True
    return any(i.name == name and i.resolve for i in cap.inputs)


def _check_source(cap, step, i, target, src: Source, tobj, inputs, position, ops, add):
    where = f"step {step.id}"
    canonical = _edm(tobj)
    if src.kind == "input":
        if src.input not in inputs:
            add(where, "unknown-input", "error", f"{target}: input {src.input!r} does not exist")
            return
        _type_check(where, target, inputs[src.input].type, src.transform, canonical, add)
    elif src.kind == "item":
        key = f"{step.for_each}.{src.item}" if step.for_each else None
        if key is None or key not in inputs:
            add(where, "unknown-item", "error", f"{target}: item {src.item!r} needs for_each over a list input")
            return
        _type_check(where, target, inputs[key].type, src.transform, canonical, add)
    elif src.kind == "step":
        if src.step not in position or position[src.step] >= i:
            add(where, "bad-step-reference", "error", f"{target}: step {src.step!r} must be an earlier step")
            return
        src_op = ops.get(cap.steps[position[src.step]].operation)
        if src_op and src.field not in src_op.response.fields \
                and not (src_op.response.shape == "value" and src.field == "value"):
            add(where, "unobserved-field", "warning", f"{target}: {src.field} was not observed in {src_op.id} responses")
    elif src.kind == "config" and src.config not in cap.config:
        add(where, "unknown-config", "error", f"{target}: config {src.config!r} does not exist")


def _type_check(where, target, input_type, transform, canonical, add):
    if canonical is None or input_type == "list":
        return
    allowed = set(_COMPATIBLE.get(input_type, set()))
    if transform:
        allowed |= _TRANSFORM_TARGET.get(transform, set())
    base = canonical.split("(")[-1].rstrip(")")
    if base.startswith("IfsApp."):
        allowed_enum = input_type == "string"
        if not allowed_enum:
            add(where, "type-mismatch", "error", f"{target}: {input_type} input feeds enum {base}")
        return
    if base not in allowed:
        add(where, "type-mismatch", "error", f"{target}: {input_type} input feeds {base}" +
            ("" if transform else " (add a transform if intended)"))


def _check_approval(cap: Capability, ops: dict[str, Operation], add) -> None:
    ap = cap.approval
    if ap is None:
        add("approval", "approval-missing", "error", "status is approved but no approval record exists")
        return
    if ap.content_sha256 != cap.content_hash():
        add("approval", "changed-since-approval", "error", "content changed after approval; review and approve again")
    for op_id, rev in ap.operations.items():
        op = ops.get(op_id)
        if op is None or op.revision != rev or op.status != "approved":
            add("approval", "operation-changed-since-approval", "error",
                f"{op_id} changed (or lost approval) after this capability was approved")
