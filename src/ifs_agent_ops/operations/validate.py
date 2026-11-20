"""IFS rules checked against canonical operations.

Each rule encodes a trap documented in the skill authoring guide or seen in real IFS behaviour,
so it is caught once, in the model, instead of in every generated artifact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from ifs_agent_ops.operations.model import Operation

Severity = Literal["error", "warning", "info"]
_DATETIME_WITH_ZONE = re.compile(r"^string:9{4}-99-99T[9:.]+(Z|[+-]99:?99)$")
_DATETIME_ANY = re.compile(r"^string:9{4}-99-99T")
_NUMERIC = {"Edm.Decimal", "Edm.Double", "Edm.Single", "Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte"}


@dataclass
class Finding:
    operation: str
    rule: str
    severity: Severity
    message: str
    subject: Optional[str] = None  # the field/parameter the finding is about, when there is one


def validate_operation(op: Operation) -> list[Finding]:
    out: list[Finding] = []

    def add(rule, severity, message, subject=None):
        out.append(Finding(op.id, rule, severity, message, subject))

    if op.entity_type is None and op.kind not in ("action", "function"):
        add("metadata-unresolved", "error", f"target {op.target!r} could not be resolved in the metadata snapshot")

    # Enum fields in $filter must use the fully qualified literal: Field eq IfsApp.X.Enum'Member'
    for pattern in op.query.filter_patterns:
        for field in re.findall(r"\b(\w+)\s+(?:eq|ne)\s+'\{value\}'", pattern):
            ftype = op.query.fields.get(field)
            if ftype is not None and ftype.enum:
                add("enum-filter-literal", "error",
                    f"$filter compares enum field {field} with a plain string; use {ftype.enum}'<Member>'", field)

    for p in op.body:
        edm = p.type.edm if p.type else None
        if not p.in_metadata:
            add("unknown-parameter", "error", f"body field {p.name} is not declared in the metadata snapshot", subject=p.name)
            continue
        if edm == "Edm.String" and "boolean" in p.observed:
            add("string-typed-flag", "error", f"{p.name} is Edm.String but was sent as a JSON boolean; send \"TRUE\"/\"FALSE\"", subject=p.name)
        if edm == "Edm.Date" and any(_DATETIME_ANY.match(o) for o in p.observed):
            add("date-only", "error", f"{p.name} is Edm.Date; send a date without time (e.g. \"2026-05-17\")", subject=p.name)
        if op.kind in ("create", "update", "action") and edm == "Edm.DateTimeOffset":
            # Evidence comes only from successful calls, so an observed format is one IFS accepted.
            # Some IFS writes (e.g. shop floor clocking actions) reject zoned times; others accept
            # them. Record what this operation accepted instead of assuming a global rule.
            zoned = [o for o in p.observed if _DATETIME_WITH_ZONE.match(o)]
            local = [o for o in p.observed if _DATETIME_ANY.match(o) and not _DATETIME_WITH_ZONE.match(o)]
            if zoned and local:
                add("write-datetime-mixed", "warning",
                    f"{p.name}: recordings sent both zoned and local times; confirm which IFS expects", subject=p.name)
            elif zoned or local:
                add("write-datetime-format", "info",
                    f"{p.name}: IFS accepted {'UTC/offset' if zoned else 'local (no suffix)'} times in recordings; "
                    "keep that format", subject=p.name)
        if edm in _NUMERIC and any(o.startswith("string:") for o in p.observed):
            add("numeric-as-string", "info",
                f"{p.name} is {edm} but was sent as a string (IEEE754Compatible); callers must use the same "
                "Content-Type convention or send a JSON number", subject=p.name)
        if p.required and not p.observed:
            add("required-not-observed", "warning", f"required parameter {p.name} was never observed in a recording", subject=p.name)

    needs = op.method in ("PATCH", "PUT", "DELETE")
    if needs and not any(h.name == "If-Match" and h.required for h in op.headers):
        add("if-match-required", "error", "write to an existing entity requires If-Match (ETag)")
    for h in op.headers:
        if h.name == "If-Match" and "not seen in the recording" in h.reason:
            add("if-match-unobserved", "warning", "If-Match is required but the recording did not send it")

    if not op.evidence:
        add("no-evidence", "warning", "operation has no recorded evidence")
    if op.status == "approved" and op.approval and op.approval.content_sha256             and op.approval.content_sha256 != op.technical_hash():
        add("changed-since-approval", "error", "the definition changed after approval; review and approve again")
    for note in op.review_notes:
        add("open-question", "info", note)
    for req in op.runtime_validation.requirements:
        add("runtime-validation", "info", req)
    return out


def validate_all(ops: dict[str, Operation]) -> list[Finding]:
    findings: list[Finding] = []
    ids = {}
    for op in ops.values():
        findings.extend(validate_operation(op))
        if op.connector_operation_id in ids:
            findings.append(Finding(op.id, "connector-operation-id-unique", "error",
                                    f"connector_operation_id duplicates {ids[op.connector_operation_id]}"))
        ids[op.connector_operation_id] = op.id
        for dep in op.depends_on:
            if dep not in ops:
                findings.append(Finding(op.id, "dangling-dependency", "error", f"depends_on {dep} does not exist"))
    return findings


def approval_blockers(op: Operation) -> tuple[list[Finding], list[str]]:
    """What prevents (re-)approval: validation errors of the definition as a draft, and open technical
    model questions. Approval bookkeeping itself (e.g. changed-since-approval) is not a blocker for
    re-approving the changed definition."""
    draft = op.model_copy(update={"status": "draft", "approval": None})
    return [f for f in validate_operation(draft) if f.severity == "error"], list(op.review_notes)
