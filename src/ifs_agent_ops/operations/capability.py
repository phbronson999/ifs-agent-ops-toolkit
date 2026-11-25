"""Curated capability: the agent-facing business contract over canonical operations.

A ``Capability`` references canonical operations by id (it never copies or trims them) and
declares, for every value a step sends, where the value comes from. It is authored and approved
by a human. Approval means: *this is the intentional and safe business contract that downstream
agents/applications may use.* That is distinct from operation approval (technical correctness)
and from runtime validation (``runtime_validation``: has it been executed against IFS).

See docs/operation-model.md §3. Stored as ``capabilities/<id>.yaml`` in a package.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ifs_agent_ops.operations.model import RUNTIME_RESULT_FIELDS, SCHEMA_VERSION, RuntimeValidation, Status

InputType = Literal["string", "decimal", "integer", "boolean", "date", "datetime", "list"]
InputSource = Literal["agent", "document", "user"]
Role = Literal["lookup", "validation", "defaults", "calculation", "write"]
_SOURCE_KINDS = ("input", "item", "step", "config", "record", "derived", "fixed")
Match = Literal["eq", "ne", "contains", "words"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecordSource(_M):
    """A composite record built from earlier steps (executable, unlike a prose ``derived`` value).

    Either ``same_as`` (the record another step sent for the same target), or: the response records of
    ``steps`` overlaid in order (a later null does not overwrite an earlier value), restricted to ``keep``
    (plus ``keep_if_present`` members when present), then ``set`` members (any source, including null)."""
    steps: list[str] = Field(default_factory=list, description="Earlier steps whose response records are overlaid")
    keep: list[str] = Field(default_factory=list, description="Members the record carries")
    keep_if_present: list[str] = Field(default_factory=list, description="Members carried only when present")
    set: dict[str, "Source"] = Field(default_factory=dict, description="Members set after the overlay")
    same_as: Optional[str] = Field(None, description="Send the record that step sent for the same target")

    @model_validator(mode="after")
    def _shape(self):
        if self.same_as is not None:
            if self.steps or self.keep or self.keep_if_present or self.set:
                raise ValueError("a record with same_as takes nothing else")
        elif not self.keep:
            raise ValueError("a record needs keep (the members it carries) or same_as")
        return self


class Source(_M):
    """Where one value comes from. Exactly one of input/item/step/config/record/derived/fixed."""
    input: Optional[str] = None
    item: Optional[str] = None
    step: Optional[str] = None
    field: Optional[str] = Field(None, description="Response field of `step`")
    config: Optional[str] = None
    record: Optional[RecordSource] = Field(None, description="Composite record built from earlier steps")
    derived: Optional[str] = Field(None, description="Prose description of a value; not executable (use record)")
    fixed: Any = None
    transform: Optional[Literal["date", "datetime", "string", "number", "local-datetime"]] = None
    match: Optional[Match] = Field(None, description="filter.X targets only: eq (default), ne, contains "
                                   "(case-insensitive substring) or words (every word contained; words that match "
                                   "nothing on their own are dropped; then any word)")
    or_fields: Optional[list[str]] = Field(None, description="match contains/words only: more fields searched "
                                 "with the same text (a row matches when any of the fields matches, e.g. the code "
                                 "as well as the description); rows where a searched field equals the text come first")
    note: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one(self):
        set_kinds = [k for k in _SOURCE_KINDS if k in self.model_fields_set]
        if len(set_kinds) != 1:
            raise ValueError(f"a source needs exactly one of {', '.join(_SOURCE_KINDS)} (got {set_kinds or 'none'})")
        if self.step is not None and not self.field:
            raise ValueError("a step source needs `field`")
        return self

    @property
    def kind(self) -> str:
        return next(k for k in _SOURCE_KINDS if k in self.model_fields_set)


RecordSource.model_rebuild()


class ConfigValue(_M):
    type: InputType = "string"
    value: Any = None
    description: str = ""


class Input(_M):
    name: str
    type: InputType
    source: InputSource
    required: bool = False
    description: str = ""
    resolve: Optional[str] = Field(None, description="Step that must resolve/validate this input")
    confirm: bool = False
    items: list["Input"] = Field(default_factory=list, description="Fields of each item when type is list")


class Question(_M):
    id: str
    asks: str = Field(description="Input the answer fills")
    when: dict[str, Any] = Field(default_factory=dict, description='e.g. {"missing": true} or {"step": "find-customer", "matches": "many"}')
    prompt: str = ""


class Action(_M):
    name: str
    description: str = ""
    steps: list[str]
    requires_confirmation: bool = False
    confirm: list[str] = Field(default_factory=list, description="Inputs/outputs shown to the user before the write")
    returns: list[str] = Field(default_factory=list)


class Step(_M):
    id: str
    operation: str
    role: Role
    description: str = ""
    for_each: Optional[str] = Field(None, description="List input to iterate (enables `item` sources)")
    body_from: list[str] = Field(default_factory=list, description="Steps whose response records seed the body, "
                                                                   "in order (later overlays earlier)")
    bindings: dict[str, Source] = Field(default_factory=dict, description='Target "body.X", "path.X", "filter.X" '
                                                                          'or "header.X" -> source')
    omit: list[str] = Field(default_factory=list, description="body fields removed from the seeded record")
    requires_input: Optional[str] = Field(None, description="Run this step only when this (optional) input is "
                                                            "supplied")
    expand: Optional[list[str]] = Field(None, description="Query steps: related records to return with each row "
                                                         "($expand), for example PartNoRef($select=Description)")
    batch: Optional[str] = Field(None, description="Writes IFS only accepts together: consecutive steps with the same "
                                                   "batch id are sent as one $batch change set, in order (all saved "
                                                   "or none)")


class Output(_M):
    name: str
    step: str
    field: str
    aggregate: Optional[Literal["sum", "count", "list"]] = None
    description: str = ""


class WarningCondition(_M):
    """An executable warning condition on a step's response field (compared as text)."""
    field: str
    equals: Any = None
    not_equals: Any = None

    @model_validator(mode="after")
    def _one(self):
        if ("equals" in self.model_fields_set) == ("not_equals" in self.model_fields_set):
            raise ValueError("a warning condition needs exactly one of equals, not_equals")
        return self


class WarningRule(_M):
    source: Literal["ifs", "step"]
    header: Optional[str] = None
    step: Optional[str] = None
    when: Optional[str | WarningCondition] = Field(None, description="{field, equals | not_equals}; prose is "
                                                                     "not executable by a generated flow")
    meaning: str = ""


class ErrorRule(_M):
    step: str
    when: dict[str, Any] = Field(default_factory=dict)
    meaning: str
    include: Optional[str] = None


class ExcludedCall(_M):
    """A call the IFS web client made in the evidence that this capability does not include."""
    call: str = Field(description="Operation or call name as observed")
    classification: Literal["excluded", "deferred", "validate-at-runtime"] = Field(
        description="excluded: evidence shows it is not needed; deferred: out of scope for this revision; "
                    "validate-at-runtime: believed unnecessary, must be proven by runtime validation")
    reason: str


class CapabilityApproval(_M):
    approved_at: str
    approved_by: str
    revision: int
    content_sha256: str = Field(description="Hash of the reviewed content; any later change invalidates approval")
    operations: dict[str, int] = Field(description="Operation id -> revision reviewed with this capability")


class Capability(_M):
    schema_version: int = SCHEMA_VERSION
    id: str
    status: Status = "draft"
    revision: int = 1
    title: str
    description: str = ""
    evidence: list[str] = Field(default_factory=list, description="Observed workflows this was curated from")
    review_notes: list[str] = Field(default_factory=list, description="Curation questions: business/design "
                                                                      "decisions needed before approval")
    deferred: list[str] = Field(default_factory=list, description="Scope deliberately left out of this revision "
                                                                  "(agents must not offer it)")
    config: dict[str, ConfigValue] = Field(default_factory=dict)
    inputs: list[Input] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list)
    warnings: list[WarningRule] = Field(default_factory=list)
    errors: list[ErrorRule] = Field(default_factory=list)
    excluded_calls: list[ExcludedCall] = Field(default_factory=list)
    runtime_validation: RuntimeValidation = Field(default_factory=RuntimeValidation,
                                                  description="Runtime evidence still needed; approval does not "
                                                              "claim it")
    approval: Optional[CapabilityApproval] = None

    def content_hash(self) -> str:
        """Hash of everything a reviewer approves (status, approval and runtime results excluded)."""
        data = _drop_unset_later_fields(self.model_dump(mode="json", exclude={
            "status": True, "approval": True, "runtime_validation": RUNTIME_RESULT_FIELDS}))
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()

    def all_inputs(self) -> dict[str, Input]:
        out = {i.name: i for i in self.inputs}
        for i in self.inputs:
            for item in i.items:
                out[f"{i.name}.{item.name}"] = item
        return out

    def step(self, step_id: str) -> Optional[Step]:
        return next((s for s in self.steps if s.id == step_id), None)


_LATER_FIELDS = {"match", "batch", "expand", "or_fields"}   # added after approvals existed: left out of the hash while unset


def _drop_unset_later_fields(data):
    if isinstance(data, dict):
        return {k: _drop_unset_later_fields(v) for k, v in data.items()
                if not (k in _LATER_FIELDS and v is None)}
    if isinstance(data, list):
        return [_drop_unset_later_fields(v) for v in data]
    return data


def json_schema() -> dict:
    return Capability.model_json_schema()
