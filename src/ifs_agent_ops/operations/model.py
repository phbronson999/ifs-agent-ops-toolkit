"""Canonical operation and workflow model (v0).

An ``Operation`` is the complete IFS API contract for one projection call: everything IFS accepts,
typed from metadata (never trimmed for an agent). A ``Workflow`` here is an *observed* workflow:
the call sequence of one recording with its data flow, which is evidence and not a contract.
What an agent may supply and which steps it runs belongs to a curated capability that references
operations (see docs/operation-model.md; Phase 7b). Stored as YAML in a package
(``operations/<id>.yaml``, ``workflows/<id>.yaml``). Platform-specific concerns (Power Platform
renames, connector quirks) do not belong here; the model keeps the original IFS path template and
IFS types.

``status: approved`` on an operation means its technical description is verified. It does not
mean the operation is exposed to agents (exposure is decided by approved capabilities), and it does
not mean the operation has been executed outside the recorded context: that is ``runtime_validation``.

Identity rules: ``id`` is derived from projection + kind + path segments and never changes;
``connector_operation_id`` is assigned once when the operation is first drafted and is never
regenerated (published connector operationIds must stay stable).
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["draft", "approved", "deprecated"]
Kind = Literal["query", "read", "create", "update", "delete", "action", "function"]
Location = Literal["path", "query", "body", "header"]

SCHEMA_VERSION = 1


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TypeRef(_Model):
    edm: str = Field(description="Declared IFS/OData type, e.g. Edm.String, Edm.Decimal, IfsApp.X.SomeEnum")
    enum: Optional[str] = Field(None, description="Fully qualified enum type; $filter literals must use "
                                                  "<enum>'<Member>'")
    members: list[str] = Field(default_factory=list)
    nullable: bool = True


class Placement(_Model):
    segment: str = Field(description="Path segment (entity set, navigation property or operation) carrying the key")
    key: str = Field(description="Key or parameter name in that segment")
    quoted: bool = Field(description="True for Key='{v}' (string literal), False for Key={v}")
    alias: Optional[bool] = Field(None, description="True when sent as a parameter alias, Key=@Key, with the value "
                                                    "as JSON in the query string (@Key=<json>); IFS uses this for "
                                                    "structured (complex-type) function parameters")


class PathParameter(_Model):
    name: str = Field(description="Logical parameter; nested navigation can place the same key several times")
    placements: list[Placement]
    type: Optional[TypeRef] = None


class Parameter(_Model):
    name: str
    location: Location
    type: Optional[TypeRef] = None
    required: Optional[bool] = Field(None, description="From metadata (non-nullable action parameter); None if unknown")
    observed: list[str] = Field(default_factory=list, description="Observed value shapes, e.g. string:9999-99-99, "
                                                                  "number, boolean, string:'TRUE'")
    in_metadata: bool = True


class Header(_Model):
    name: str
    required: bool
    reason: str


class QueryOptions(_Model):
    select: list[str] = Field(default_factory=list)
    expand: list[str] = Field(default_factory=list)
    filter_patterns: list[str] = Field(default_factory=list, description="Anonymized $filter expressions observed")
    orderby: list[str] = Field(default_factory=list)
    top: Optional[int] = None
    fields: dict[str, TypeRef] = Field(default_factory=dict, description="Metadata types of the fields referenced "
                                                                         "by $select/$filter/$orderby")


class Response(_Model):
    shape: Literal["collection", "entity", "complex", "value", "empty", "unknown"]
    type: Optional[str] = Field(None, description="Entity or complex type returned")
    fields: list[str] = Field(default_factory=list)


class Evidence(_Model):
    recording: str = Field(description="Package-relative path of the sanitized recording")
    call_index: int
    part: Optional[int] = Field(None, description="Position inside a $batch call")
    status: int


RuntimeStatus = Literal["untested", "conditional", "validated", "failed"]


class RuntimeValidation(_Model):
    """Has an implementation been executed against IFS? Separate from (and never implied by) approval.

    ``status`` is set only by runtime validation (Phase 11), never by a human approval; it is excluded
    from approval hashes so a validation result cannot invalidate an approval. ``requirements`` lists
    what must be proven at runtime.
    """
    status: RuntimeStatus = "untested"
    requirements: list[str] = Field(default_factory=list, description="What runtime validation must prove")


class Approval(_Model):
    approved_at: str
    revision: int
    approved_by: Optional[str] = Field(None, description="Human reviewer (recorded from Phase 7b on)")
    content_sha256: Optional[str] = Field(None, description="Technical content hash at approval; a later change "
                                                            "to the definition invalidates the approval")


class Operation(_Model):
    schema_version: int = SCHEMA_VERSION
    id: str
    connector_operation_id: str
    status: Status = "draft"
    revision: int = 1
    title: str = ""
    description: str = ""
    projection: str
    kind: Kind
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"]
    path_template: str = Field(description="Original IFS path after <projection>.svc, keys as {Name} placeholders")
    target: str = Field(description="Entity set, navigation chain or action/function name")
    entity_type: Optional[str] = None
    path_parameters: list[PathParameter] = Field(default_factory=list)
    query: QueryOptions = Field(default_factory=QueryOptions)
    body: list[Parameter] = Field(default_factory=list)
    headers: list[Header] = Field(default_factory=list)
    response: Response = Field(default_factory=lambda: Response(shape="unknown"))
    depends_on: list[str] = Field(default_factory=list, description="Operations whose responses supplied inputs in "
                                                                    "the recordings (observed, including UI-driven "
                                                                    "calls; evidence for curation)")
    evidence: list[Evidence] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list, description="Technical model questions: doubts that the "
                                                                      "canonical representation itself is correct "
                                                                      "(human text; kept on re-draft)")
    runtime_validation: RuntimeValidation = Field(default_factory=RuntimeValidation,
                                                  description="Runtime evidence still needed; not part of approval")
    approval: Optional[Approval] = None

    def technical_hash(self) -> str:
        """SHA-256 of the technical representation (identity + everything derived from IFS evidence)."""
        data = {"id": self.id, "projection": self.projection, **self.model_dump(
            mode="json", exclude_none=True, include={"kind", "method", "path_template", "target", "entity_type",
                                                     "path_parameters", "query", "body", "headers", "response"})}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def derived(self) -> dict:
        """Everything the drafter derives (excludes identity, human text, status and evidence)."""
        return self.model_dump(include={"kind", "method", "path_template", "target", "entity_type",
                                        "path_parameters", "query", "body", "headers", "response", "depends_on"})


class Binding(_Model):
    to: str = Field(description='What the step sends, e.g. "key OrderNo", "body CustomerNo", "If-Match"')
    from_step: int
    from_field: str


class Step(_Model):
    step: int
    operation: str
    task: Optional[str] = Field(None, description="Recording-plan task the call was made in (id from the plan)")
    bindings: list[Binding] = Field(default_factory=list)


class PlanTaskRef(_Model):
    id: str
    title: str
    status: str


class RecordingPlan(_Model):
    """The demonstrator's intent and ordered tasks from the guided capture (semantic evidence)."""
    intent: str
    complete: bool
    tasks: list[PlanTaskRef] = Field(default_factory=list)


class Workflow(_Model):
    """Observed workflow: what the IFS web client did in one recording (UI lookups included).

    Evidence for curating capabilities; generators never consume it directly.
    """
    schema_version: int = SCHEMA_VERSION
    id: str
    status: Status = "draft"
    title: str = ""
    description: str = ""
    recording: str
    plan: Optional[RecordingPlan] = Field(None, description="Recording plan, when captured with one")
    steps: list[Step] = Field(default_factory=list)
    approval: Optional[Approval] = None


def json_schema() -> dict:
    return {"operation": Operation.model_json_schema(), "workflow": Workflow.model_json_schema()}
