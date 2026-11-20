"""Curated capability: the agent-facing business contract over canonical operations.

A ``Capability`` references canonical operations by id (it never copies or trims them) and
declares, for every value a step sends, where the value comes from. It is authored and approved
by a human. Approval means: *this is the intentional and safe business contract that downstream
agents/applications may use.* That is distinct from operation approval (technical correctness).

See docs/operation-model.md §3. Stored as ``capabilities/<id>.yaml`` in a package.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ifs_agent_ops.operations.model import SCHEMA_VERSION, Status

InputType = Literal["string", "decimal", "integer", "boolean", "date", "datetime", "list"]
InputSource = Literal["agent", "document", "user"]
Role = Literal["lookup", "validation", "defaults", "calculation", "write"]
_SOURCE_KINDS = ("input", "item", "step", "config", "derived", "fixed")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_M):
    """Where one value comes from. Exactly one of input/item/step/config/derived/fixed."""
    input: Optional[str] = None
    item: Optional[str] = None
    step: Optional[str] = None
    field: Optional[str] = Field(None, description="Response field of `step`")
    config: Optional[str] = None
    derived: Optional[str] = Field(None, description="Expression over other values (documented, not evaluated yet)")
    fixed: Any = None
    transform: Optional[Literal["date", "datetime", "string", "number", "local-datetime"]] = None
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


class Output(_M):
    name: str
    step: str
    field: str
    aggregate: Optional[Literal["sum", "count", "list"]] = None
    description: str = ""


class WarningRule(_M):
    source: Literal["ifs", "step"]
    header: Optional[str] = None
    step: Optional[str] = None
    when: Optional[str] = None
    meaning: str = ""


class ErrorRule(_M):
    step: str
    when: dict[str, Any] = Field(default_factory=dict)
    meaning: str
    include: Optional[str] = None


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
    review_notes: list[str] = Field(default_factory=list, description="Open questions for the human reviewer")
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
    approval: Optional[CapabilityApproval] = None

    def content_hash(self) -> str:
        """Hash of everything a reviewer approves (status/approval bookkeeping excluded)."""
        data = self.model_dump(mode="json", exclude={"status", "approval"})
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()

    def all_inputs(self) -> dict[str, Input]:
        out = {i.name: i for i in self.inputs}
        for i in self.inputs:
            for item in i.items:
                out[f"{i.name}.{item.name}"] = item
        return out

    def step(self, step_id: str) -> Optional[Step]:
        return next((s for s in self.steps if s.id == step_id), None)


def json_schema() -> dict:
    return Capability.model_json_schema()
