"""Build one Power Automate cloud flow (Workflow Definition Language) for one capability action.

Shape of every generated flow::

    manual (HTTP request; only the users listed in the deployment target may call it)
    Initialize_* variables: error, warnings, questions, one per step result, outputs
    Try (scope)
      [submit] confirmation and required-input gates
      one gated item per step, in the action's order ("If_<Step>": runs only while no error is recorded,
      and for lookups only when the values they search with are supplied); consecutive per-line steps share
      one loop ("For_Each_<List>", one line at a time)
    Outcome (compose)    ok / error, also when the Try scope failed unexpectedly
    Respond (response)   JSON: ok, error, outputs or proposal, questions, warnings

Every connector call records its result in a variable (later steps and the response read the variables, so
a skipped step reads as an empty result), and has a failure handler that records the capability's error
meaning, the HTTP status and the IFS response. After a recorded error the remaining steps are skipped and
the flow responds with what it has: a failed submit returns the order and lines already created.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ifs_agent_ops.generators.connector.compat import render_path
from ifs_agent_ops.generators.flow.expr import (
    V,
    all_of,
    any_of,
    concat,
    lit,
    missing,
    odata_literal,
    odata_string,
    prop,
    q,
    transform,
)
from ifs_agent_ops.generators.gating import GenerationRefused
from ifs_agent_ops.operations.capability import (
    Action,
    Capability,
    Input,
    RecordSource,
    Source,
    Step,
    WarningCondition,
)
from ifs_agent_ops.operations.model import Operation, TypeRef

ALL = ["Succeeded", "Failed", "Skipped", "TimedOut"]
FAILED = ["Failed", "TimedOut"]
DEFAULT_TOP = 50
# A space anywhere in $filter breaks the request through the connector and the on-premises gateway (IFS answers
# TYPES_NOT_COMPATIBLE, or finds nothing for a quoted value); a tab works. So operators are separated by tabs, and
# a contains search on several words becomes one contains() per word (tested on DEV 2026-11-22).
TAB = "\t"
AND, OR = f"{TAB}and{TAB}", f"{TAB}or{TAB}"
CALL_TIMEOUT = "PT60S"    # per IFS call; the HTTP trigger must answer within 120 seconds
READ_RETRIES = {"type": "fixed", "count": 3, "interval": "PT5S"}
USERS_TOKEN = "%%TRIGGER_USERS%%"
API_PREFIX = "/providers/Microsoft.PowerApps/apis/"
_SCHEMA_TYPES = {"string": "string", "decimal": "number", "integer": "integer", "boolean": "boolean",
                 "date": "string", "datetime": "string", "list": "array"}
_MATCH_COUNT = {"none": "equals({n}, 0)", "many": "greater({n}, 1)", "none-or-many": "not(equals({n}, 1))",
                "one": "equals({n}, 1)"}


def api_token(connector: str) -> str:
    """Placeholder for a connector's environment-specific API name (filled in by deploy)."""
    return f"%%CONNECTOR_API:{connector}%%"


@dataclass
class ConnectorUse:
    """How a flow reaches one IFS service: the connector and the connection reference (from deploy.yaml)."""
    projection: str
    connector: str            # deploy.yaml logical name, e.g. customer-order-handling
    schema_name: str          # connector schema name, e.g. ti_ifscustomerorderhandling
    reference: str            # connection reference logical name
    swagger: dict             # the generated connector definition
    operation_ids: dict       # canonical operation id -> connector operationId

    @property
    def key(self) -> str:
        return f"shared_{self.schema_name}"


MAX_DESCRIPTION = 250   # Power Automate refuses to turn a flow on with longer action descriptions


def short_description(text: str) -> str:
    """The first sentence, cut at a word boundary to fit an action description."""
    text = " ".join((text or "").split())
    first = re.split(r"(?<=[.;])\s", text, maxsplit=1)[0]
    if len(first) <= MAX_DESCRIPTION:
        return first
    cut = first[:MAX_DESCRIPTION - 3].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "..."


@dataclass
class Seq:
    """Actions that run one after another."""
    statuses: list[str] = field(default_factory=lambda: ["Succeeded"])
    actions: dict[str, dict] = field(default_factory=dict)
    last: Optional[str] = None

    def add(self, name: str, action: dict) -> str:
        self.actions[name] = {"runAfter": {self.last: list(self.statuses)} if self.last else {}, **action}
        self.last = name
        return name

    def side(self, name: str, action: dict, run_after: dict) -> None:
        """An action that runs beside the chain (failure handlers)."""
        self.actions[name] = {"runAfter": run_after, **action}


def pascal(text: str) -> str:
    return "_".join(w[:1].upper() + w[1:] for w in re.split(r"[^A-Za-z0-9]+", text) if w)


def var_name(step_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", step_id).strip("_")


class FlowBuilder:
    def __init__(self, cap: Capability, action: Action, ops: dict[str, Operation],
                 connectors: dict[str, ConnectorUse]) -> None:
        self.cap, self.action, self.ops, self.connectors = cap, action, ops, connectors
        self.names: set[str] = set()
        self.steps = [self._step(sid) for sid in action.steps]
        self.confirming = action.requires_confirmation
        self.variables: list[tuple[str, str, Any]] = []
        self.loop_of: dict[str, Optional[str]] = {}
        self.inputs = cap.all_inputs()
        self.outputs = [o for o in cap.outputs if o.name in action.returns]
        self.ifs_warnings = any(w.source == "ifs" for w in cap.warnings)
        self.used: dict[str, ConnectorUse] = {}
        self.notes: list[str] = []
        self.call_name: dict[str, str] = {}
        self.names.update({"try", "outcome", "respond"})

    # ── helpers ──────────────────────────────────────────────────────────────

    def _step(self, sid: str) -> Step:
        step = self.cap.step(sid)
        if step is None:
            raise GenerationRefused(f"{self.cap.id}: action {self.action.name} names unknown step {sid!r}")
        return step

    def unique(self, name: str) -> str:
        name = name[:80]
        out, n = name, 2
        while out.lower() in self.names:
            out, n = f"{name[:76]}_{n}", n + 1
        self.names.add(out.lower())
        return out

    def variable(self, name: str, vtype: str, value: Any) -> str:
        if all(v[0] != name for v in self.variables):
            self.variables.append((name, vtype, value))
        return name

    def refuse(self, step: Optional[Step], message: str):
        where = f"{self.cap.id} {self.action.name}" + (f" step {step.id}" if step else "")
        raise GenerationRefused(f"{where}: {message}")

    def step_var(self, step_id: str) -> str:
        return f"variables({q(var_name(step_id))})"

    def record_of(self, sid: str) -> str:
        """The step's record: the step variable, or its first row when the step returns a list (list steps keep
        {value: [...]}, so a field such as ReleaseNo or @odata.etag is read from the first row)."""
        step = self.cap.step(sid)
        op = self.ops.get(step.operation) if step else None
        if op is not None and op.response.shape == "collection":
            return f"first(coalesce({self.step_var(sid)}?['value'], json('[]')))"
        return self.step_var(sid)

    def item(self, loop: Optional[str], name: str) -> str:
        return prop(f"items({q(loop)})", name)

    # ── sources ──────────────────────────────────────────────────────────────

    def source(self, src: Source, step: Step, loop: Optional[str]) -> V:
        kind = src.kind
        if kind == "input":
            if src.input not in self.inputs or "." in src.input:
                self.refuse(step, f"input {src.input!r} cannot be read here")
            v = V.e(prop("triggerBody()", src.input))
        elif kind == "item":
            if loop is None:
                self.refuse(step, f"item {src.item!r} outside a per-line step")
            v = V.e(self.item(loop, src.item))
        elif kind == "step":
            v = V.e(self.field_of(src.step, src.field, step, loop))
        elif kind == "config":
            v = V.of(self.cap.config[src.config].value)
        elif kind == "fixed":
            v = V.of(src.fixed)
        elif kind == "derived":
            self.refuse(step, "a prose (derived) value cannot be executed; describe it as a record")
        else:
            self.refuse(step, f"a {kind} source is not supported here")
        return transform(v, src.transform)

    def field_of(self, sid: str, fld: str, step: Step, loop: Optional[str]) -> str:
        if sid not in self.loop_of:
            self.refuse(step, f"reads step {sid!r}, which does not run earlier in this action")
        if self.loop_of[sid] is not None and self.loop_of[sid] != loop:
            self.refuse(step, f"reads per-line step {sid!r} from outside its loop")
        return prop(self.record_of(sid), fld)

    def overlay(self, steps: list[str], member: str, step: Step, loop: Optional[str]) -> V:
        """A member of the overlaid response records: the latest step with a non-null value wins."""
        parts = [self.field_of(s, member, step, loop) for s in reversed(steps)]
        return V.e(parts[0] if len(parts) == 1 else f"coalesce({', '.join(parts)})")

    def record(self, seq: Seq, step: Step, target: str, rec: RecordSource, loop: Optional[str]) -> str:
        """Compose actions building a record; returns the name of the action whose output is the record."""
        while rec.same_as:                       # the same record, built again from the same results
            other = self.cap.step(rec.same_as)
            rec = other.bindings[target].record
        member = lambda m: (self.source(rec.set[m], step, loop) if m in rec.set
                            else self.overlay(rec.steps, m, step, loop))
        obj = {m: member(m).json() for m in rec.keep}
        for m in rec.set:
            if m not in rec.keep and m not in rec.keep_if_present:
                obj[m] = self.source(rec.set[m], step, loop).json()
        name = seq.add(self.unique(f"Record_{pascal(step.id)}_{target.split('.', 1)[1]}"),
                       {"type": "Compose", "inputs": obj,
                        "description": f"{len(rec.keep)} members" + (f" from {', '.join(rec.steps)}" if rec.steps
                                                                      else "")})
        base = name
        for m in rec.keep_if_present:            # carried only when present (not null)
            v = member(m).wdl()
            name = seq.add(self.unique(f"{base}_If_{m}"), {
                "type": "Compose",
                "inputs": f"@if(equals({v}, null), outputs({q(name)}), setProperty(outputs({q(name)}), {q(m)}, {v}))"})
        return name

    # ── connector parameters ─────────────────────────────────────────────────

    def use(self, step: Step, op: Operation) -> tuple[ConnectorUse, str, dict]:
        cu = self.connectors.get(op.projection)
        if cu is None:
            self.refuse(step, f"no connector for {op.projection} in deploy.yaml")
        op_id = cu.operation_ids.get(op.id)
        swagger_op = next((o for ops in cu.swagger.get("paths", {}).values() for o in ops.values()
                           if isinstance(o, dict) and o.get("operationId") == op_id), None)
        if op_id is None or swagger_op is None:
            self.refuse(step, f"{op.id} is not in the generated {op.projection} connector (connector generate)")
        self.used[op.projection] = cu
        params = {}
        for p in swagger_op.get("parameters", []):
            if "$ref" in p:
                p = cu.swagger.get("parameters", {}).get(p["$ref"].split("/")[-1], {})
            params[p.get("name")] = p
        return cu, op_id, params

    def parameters(self, seq: Seq, step: Step, op: Operation, loop: Optional[str], swagger_params: dict,
                   filter_value: Optional[V] = None, top: Optional[int] = None) -> dict:
        out: dict[str, Any] = {}
        rendered = render_path(op)
        types = {p.name: p.type for p in op.path_parameters}
        for pp in rendered.params:
            b = step.bindings.get(f"path.{pp.logical}")
            if b is None:
                self.refuse(step, f"path parameter {pp.logical} is not bound")
            v = self.source(b, step, loop)
            out[pp.name] = (odata_literal(v, types.get(pp.logical)) if pp.literal else v).json()
        for alias in rendered.aliases:
            b = step.bindings.get(f"path.{alias}")
            if b is None:
                self.refuse(step, f"parameter {alias} is not bound")
            if b.kind == "record":
                out[alias] = f"@string(outputs({q(self.record(seq, step, f'path.{alias}', b.record, loop))}))"
            else:
                v = self.source(b, step, loop)
                out[alias] = V.e(f"string({v.wdl()})").json()
        for target, b in step.bindings.items():
            if target.startswith("header."):
                out[target.split(".", 1)[1]] = self.source(b, step, loop).json()
        if filter_value is not None:
            out["$filter"] = filter_value.json()
        if step.expand:
            out["$expand"] = ",".join(step.expand)
        if top is not None:
            out["$top"] = top
        body = self.body(seq, step, op, loop)
        if body is not None:
            body_param = next((n for n, p in swagger_params.items() if p.get("in") == "body"), None)
            if body_param is None:
                self.refuse(step, "the connector operation takes no body")
            out[body_param] = f"@outputs({q(body)})"
        unknown = sorted(k for k in out if k not in swagger_params)
        if unknown:
            self.refuse(step, f"connector operation has no parameter(s) {', '.join(unknown)} (regenerate the "
                              "connector)")
        return out

    def body(self, seq: Seq, step: Step, op: Operation, loop: Optional[str]) -> Optional[str]:
        if not op.body:
            return None
        fields: dict[str, Any] = {}
        seeds = {sid: set(self.ops[self.cap.step(sid).operation].response.fields) for sid in step.body_from}
        for p in op.body:
            b = step.bindings.get(f"body.{p.name}")
            if b is not None:
                if b.kind == "record":
                    fields[p.name] = f"@outputs({q(self.record(seq, step, f'body.{p.name}', b.record, loop))})"
                else:
                    fields[p.name] = self.source(b, step, loop).json()
            elif p.name in step.omit:
                continue
            else:
                from_steps = [sid for sid in step.body_from if p.name in seeds[sid]]
                if from_steps:
                    fields[p.name] = self.overlay(from_steps, p.name, step, loop).json()
        if not fields:
            return None
        return seq.add(self.unique(f"Body_{pascal(step.id)}"), {"type": "Compose", "inputs": fields})

    def filter_parts(self, step: Step, op: Operation, loop: Optional[str]) -> tuple[list[V], Optional[tuple]]:
        parts, words = [], None
        for target, b in step.bindings.items():
            if not target.startswith("filter."):
                continue
            name = target.split(".", 1)[1]
            t: Optional[TypeRef] = op.query.fields.get(name)
            match = b.match or "eq"
            v = self.source(b, step, loop)
            if match == "words":
                words = (name, v)
            elif match in ("eq", "ne"):
                parts.append(concat([V.of(f"{name}{TAB}{match}{TAB}"), odata_literal(v, t)]))
            else:   # contains, case-insensitive: one contains() per word (no spaces inside quoted values)
                parts.append(self.contains_words(name, v))
        return parts, words

    @staticmethod
    def contains_words(name: str, v: V) -> V:
        head = f"contains(tolower({name}),'"
        if v.is_const:
            words = str(v.const or "").lower().split() or [""]
            return V.of(AND.join(head + w.replace("'", "''") + "')" for w in words))
        text = f"replace(toLower(trim(coalesce({v.expr}, ''))), '''', '''''')"
        between = "')" + AND + head          # each space becomes: end this contains(), start the next
        return V.e(f"concat({q(head)}, replace({text}, ' ', {q(between)}), {q(chr(39) + ')')})")

    @staticmethod
    def joined(parts: list[V], sep: str = AND) -> Optional[V]:
        if not parts:
            return None
        out = [parts[0]]
        for p in parts[1:]:
            out += [V.of(sep), p]
        return concat(out)

    # ── connector actions ────────────────────────────────────────────────────

    def call(self, seq: Seq, step: Step, op: Operation, loop: Optional[str], name: str, params: dict,
             operation_id: Optional[str] = None) -> str:
        cu = self.connectors[op.projection]
        seq.add(name, {"type": "OpenApiConnection", "inputs": {
            "host": {"apiId": API_PREFIX + api_token(cu.connector), "connectionName": cu.key,
                     "operationId": operation_id or cu.operation_ids[op.id]},
            "parameters": params, "authentication": "@parameters('$authentication')",
            # reads retry a few times; writes never (a retried write could create a duplicate). Bounded, so the
            # flow answers within the HTTP trigger's synchronous response limit
            "retryPolicy": READ_RETRIES if step.role != "write" else {"type": "none"}},
            "limit": {"timeout": CALL_TIMEOUT}})
        seq.side(self.unique(f"{name}_Failed"), self.set_error(self.failure(step, name, loop)), {name: FAILED})
        return name

    def failure(self, step: Step, action: str, loop: Optional[str]) -> dict:
        status = f"outputs({q(action)})?['statusCode']"
        by_class = {}
        for e in self.cap.errors:
            if e.step == step.id and "status" in e.when:
                by_class[str(e.when["status"]).lower()] = e.meaning
        meaning = lit("step-failed")
        if "4xx" in by_class:
            meaning = f"if(greaterOrEquals(int(coalesce({status}, 0)), 400), {q(by_class['4xx'])}, {meaning})"
        if "5xx" in by_class:
            meaning = f"if(greaterOrEquals(int(coalesce({status}, 0)), 500), {q(by_class['5xx'])}, {meaning})"
        err = {"meaning": V.e(meaning).json() if by_class else "step-failed", "step": step.id,
               "status": "@" + status, "ifs_response": f"@body({q(action)})"}
        if loop:
            err["line"] = f"@items({q(loop)})"
        return err

    def set_error(self, value: dict) -> dict:
        return {"type": "SetVariable", "inputs": {"name": "error", "value": value}}

    # ── one step ─────────────────────────────────────────────────────────────

    def run_condition(self, step: Step, loop: Optional[str]) -> str:
        conds = ["empty(variables('error'))"]
        needed: list[tuple[str, str]] = []
        if step.requires_input:
            needed.append(self.input_ref(step.requires_input, loop, step))
        if step.role != "write":     # lookups and checks run only with the values they use
            for b in step.bindings.values():
                if b.kind == "input":
                    needed.append((prop("triggerBody()", b.input), self.inputs[b.input].type))
                elif b.kind == "item" and loop:
                    needed.append((self.item(loop, b.item), self.inputs[f"{step.for_each}.{b.item}"].type))
        seen = set()
        for expr, itype in needed:
            if expr not in seen:
                seen.add(expr)
                conds.append(f"not({missing(expr, itype)})")
        return "@" + all_of(conds)

    def input_ref(self, name: str, loop: Optional[str], step: Optional[Step]) -> tuple[str, str]:
        _, _, sub = name.partition(".")
        if sub:
            if loop is None:
                self.refuse(step, f"{name} is a per-line value")
            return self.item(loop, sub), self.inputs[name].type
        return prop("triggerBody()", name), self.inputs[name].type

    def step_items(self, step: Step, loop: Optional[str]) -> list[tuple[str, dict]]:
        op = self.ops[step.operation]
        _, _, swagger_params = self.use(step, op)
        inner = Seq()
        sv = var_name(step.id)
        parts, words = self.filter_parts(step, op, loop)
        top = DEFAULT_TOP if op.kind == "query" else None
        before: list[tuple[str, dict]] = []
        if render_path(op).aliases:
            self.batch_step(inner, step, op, loop)
        elif words is None:
            params = self.parameters(inner, step, op, loop, swagger_params, self.joined(parts), top)
            name = self.call(inner, step, op, loop, self.unique(pascal(step.id)), params)
            self.call_name[step.id] = name
            inner.add(self.unique(f"Set_{sv}"), {"type": "SetVariable", "inputs": {
                "name": sv, "value": f"@coalesce(body({q(name)}), json('{{}}'))"}})
        else:
            before = self.words_reset(step)
            self.words_search(inner, step, op, loop, swagger_params, parts, words)
        self.after_step(inner, step, op, loop)
        if_name = self.unique(f"If_{pascal(step.id)}")
        cond = {"type": "If", "expression": self.run_condition(step, loop), "actions": inner.actions,
                "else": {"actions": {}}}
        if step.description:
            cond["description"] = short_description(step.description)
        return before + [(if_name, cond)] + self.checks_after(step, op, loop)

    # ── calls inside $batch (functions with parameter aliases) ───────────────

    @staticmethod
    def url_safe(v: V, star: bool = False) -> V:
        """Escape what IFS's batch request-line parser rejects (':' in date-times) and what URLs cannot carry.
        ``star`` also encodes '*' (key values such as ReleaseNo '*'), as the IFS web client does in change sets."""
        pairs = [("%", "%25"), (" ", "%20"), (":", "%3A"), ("#", "%23"), ("&", "%26")] + ([("*", "%2A")] if star else [])
        if v.is_const:
            text = "null" if v.const is None else str(v.const)
            for a, b in pairs:
                text = text.replace(a, b)
            return V.of(text)
        expr = f"string({v.expr})"
        for a, b in pairs:
            expr = f"replace({expr}, {q(a)}, {q(b)})"
        return V.e(expr)

    def batch_step(self, seq: Seq, step: Step, op: Operation, loop: Optional[str]) -> None:
        """Call a function that takes a parameter alias (P=@P, ?@P=<json>) inside $batch: flows cannot set a parameter
        named @P, and the batch body carries the request line unchanged through the connector and gateway."""
        from ifs_agent_ops.generators.connector import BATCH_BOUNDARY, BATCH_OPERATION_ID

        cu = self.connectors[op.projection]
        if not any(isinstance(o, dict) and o.get("operationId") == BATCH_OPERATION_ID
                   for ops in cu.swagger.get("paths", {}).values() for o in ops.values()):
            self.refuse(step, "the connector has no $batch operation (regenerate the connector)")
        if op.method != "GET":
            self.refuse(step, "only functions (GET) with parameter aliases can be called inside $batch")
        rendered = render_path(op)
        types = {p.name: p.type for p in op.path_parameters}
        by_name = {pp.name: pp for pp in rendered.params}
        url: list[V] = []
        for piece in re.split(r"(\{\w+\})", rendered.path.lstrip("/")):
            m = re.fullmatch(r"\{(\w+)\}", piece)
            if not m:
                url.append(V.of(piece))
                continue
            pp = by_name[m.group(1)]
            b = step.bindings.get(f"path.{pp.logical}")
            if b is None:
                self.refuse(step, f"path parameter {pp.logical} is not bound")
            v = self.source(b, step, loop)
            url.append(self.url_safe(odata_literal(v, types.get(pp.logical)) if pp.literal else v))
        for n, alias in enumerate(rendered.aliases):
            b = step.bindings.get(f"path.{alias}")
            if b is None:
                self.refuse(step, f"parameter {alias} is not bound")
            if b.kind == "record":
                value = f"outputs({q(self.record(seq, step, f'path.{alias}', b.record, loop))})"
            else:
                value = self.source(b, step, loop).wdl()
            url += [V.of(("?" if n == 0 else "&") + f"@{alias}="), V.e(f"uriComponent(string({value}))")]
        line = concat(url)
        target = line.const if line.is_const else "@{" + line.expr + "}"
        crlf = "\r\n"
        body = crlf.join([f"--{BATCH_BOUNDARY}", "Content-Type: application/http", "Content-Transfer-Encoding: binary",
                          "", f"GET {target} HTTP/1.1", "Accept: application/json", "", "", f"--{BATCH_BOUNDARY}--",
                          ""])
        P = pascal(step.id)
        request = seq.add(self.unique(f"Batch_{P}"), {"type": "Compose", "inputs": body,
                                                      "description": f"{op.id} inside $batch (parameter alias)"})
        self.used[op.projection] = cu
        name = self.call(seq, step, op, loop, self.unique(P), {
            "Content-Type": f"multipart/mixed;boundary={BATCH_BOUNDARY}", "batch": f"@outputs({q(request)})"},
            operation_id=BATCH_OPERATION_ID)
        self.call_name[step.id] = name
        text = seq.add(self.unique(f"{P}_Response"), {
            "type": "Compose", "inputs": f"@base64ToString(body({q(name)})?['$content'])"})
        t = f"outputs({q(text)})"
        status = f"int(substring({t}, add(indexOf({t}, 'HTTP/1.1 '), 9), 3))"
        json_part = f"json(substring({t}, indexOf({t}, '{{'), add(sub(lastIndexOf({t}, '}}'), indexOf({t}, '{{')), 1)))"
        err = self.failure(step, name, loop)
        err["status"] = "@" + status
        err["ifs_response"] = "@" + t
        err["meaning"] = err["meaning"].replace(f"outputs({q(name)})?['statusCode']", status) \
            if isinstance(err["meaning"], str) else err["meaning"]
        ok = Seq()
        ok.add(self.unique(f"Set_{var_name(step.id)}"), {"type": "SetVariable", "inputs": {
            "name": var_name(step.id), "value": "@" + json_part}})
        failed = Seq()
        failed.add(self.unique(f"{P}_Failed_In_Batch"), self.set_error(err))
        seq.add(self.unique(f"{P}_Succeeded"), {"type": "If", "expression": f"@less({status}, 400)",
                                                "actions": ok.actions, "else": {"actions": failed.actions}})

    # ── writes in one $batch change set ─────────────────────────────────────

    @staticmethod
    def inline(v: V) -> str:
        """A value inside a text action: the constant itself, or an @{...} interpolation."""
        if v.is_const:
            return "" if v.const is None else str(v.const)
        return "@{" + v.expr + "}"

    def groups(self, steps: list[Step]) -> list[list[Step]]:
        """Consecutive steps with the same batch id form one group; every other step is a group of its own."""
        out: list[list[Step]] = []
        for s in steps:
            if s.batch and out and out[-1][0].batch == s.batch:
                out[-1].append(s)
            else:
                out.append([s])
        return out

    def group_items(self, group: list[Step], loop: Optional[str]) -> list[tuple[str, dict]]:
        if len(group) == 1 and not group[0].batch:
            return self.step_items(group[0], loop)
        return self.changeset_items(group, loop)

    def changeset_items(self, group: list[Step], loop: Optional[str]) -> list[tuple[str, dict]]:
        """Send the group's writes as one $batch change set (IFS saves all or none), then read each part back:
        its status, and its JSON into the step's variable. IFS answers a failed change set with the failing part
        only, so fewer parts than requests also means failure."""
        from ifs_agent_ops.generators.connector import BATCH_BOUNDARY, BATCH_OPERATION_ID

        first = group[0]
        op0 = self.ops[first.operation]
        cu = self.connectors.get(op0.projection)
        if cu is None:
            self.refuse(first, f"no connector for {op0.projection} in deploy.yaml")
        if not any(isinstance(o, dict) and o.get("operationId") == BATCH_OPERATION_ID
                   for ops in cu.swagger.get("paths", {}).values() for o in ops.values()):
            self.refuse(first, "the connector has no $batch operation (regenerate the connector)")
        self.used[op0.projection] = cu
        inner = Seq()
        crlf, cs = "\r\n", "changeset_ifs_agent_ops"
        text: list[str] = [f"--{BATCH_BOUNDARY}", f"Content-Type: multipart/mixed;boundary={cs}", ""]
        for n, step in enumerate(group, 1):
            op = self.ops[step.operation]
            if op.method == "GET":
                self.refuse(step, "a change set holds writes only")
            rendered = render_path(op)
            if rendered.aliases:
                self.refuse(step, "an operation with parameter aliases cannot be part of a change set")
            types = {p.name: p.type for p in op.path_parameters}
            by_name = {pp.name: pp for pp in rendered.params}
            url = ""
            for piece in re.split(r"(\{\w+\})", rendered.path.lstrip("/")):
                m = re.fullmatch(r"\{(\w+)\}", piece)
                if not m:
                    url += piece
                    continue
                pp = by_name[m.group(1)]
                b = step.bindings.get(f"path.{pp.logical}")
                if b is None:
                    self.refuse(step, f"path parameter {pp.logical} is not bound")
                v = self.source(b, step, loop)
                url += self.inline(self.url_safe(odata_literal(v, types.get(pp.logical)) if pp.literal else v,
                                                 star=True))
            text += [f"--{cs}", "Content-Type: application/http", "Content-Transfer-Encoding: binary",
                     f"Content-ID: {n}", "", f"{op.method} {url} HTTP/1.1",
                     "Accept: application/json;odata.metadata=full;IEEE754Compatible=true"]
            for target, b in step.bindings.items():
                if target.startswith("header."):
                    text.append(f"{target.split('.', 1)[1]}: {self.inline(self.source(b, step, loop))}")
            body = self.body(inner, step, op, loop)
            if body is not None:
                text += ["Content-Type: application/json;IEEE754Compatible=true", "",
                         f"@{{string(outputs({q(body)}))}}"]
            else:
                text += ["", ""]
        text += [f"--{cs}--", f"--{BATCH_BOUNDARY}--", ""]
        P = pascal("_".join(s.id for s in group)) if len(group) <= 3 else pascal(first.batch)
        request = inner.add(self.unique(f"Batch_{P}"), {
            "type": "Compose", "inputs": crlf.join(text),
            "description": f"Change set {first.batch}: " + ", ".join(s.id for s in group)})
        name = self.call(inner, first, op0, loop, self.unique(P), {
            "Content-Type": f"multipart/mixed;boundary={BATCH_BOUNDARY}", "batch": f"@outputs({q(request)})"},
            operation_id=BATCH_OPERATION_ID)
        for s in group:
            self.call_name[s.id] = name
        resp = inner.add(self.unique(f"{P}_Response"), {
            "type": "Compose", "inputs": f"@base64ToString(body({q(name)})?['$content'])"})
        parts = inner.add(self.unique(f"{P}_Parts"), {
            "type": "Compose", "inputs": f"@skip(split(outputs({q(resp)}), 'HTTP/1.1 '), 1)"})
        pv = f"outputs({q(parts)})"

        def status(i: int) -> str:
            return f"int(substring({pv}[{i}], 0, 3))"

        def part_json(i: int) -> str:
            p = f"{pv}[{i}]"
            return (f"if(contains({p}, '{{'), json(substring({p}, indexOf({p}, '{{'), "
                    f"add(sub(lastIndexOf({p}, '}}'), indexOf({p}, '{{')), 1))), json('{{}}'))")

        ok = all_of([f"equals(length({pv}), {len(group)})"] + [f"less({status(i)}, 400)" for i in range(len(group))])
        main = next((s for s in group if any(e.step == s.id for e in self.cap.errors)), group[-1])
        err = self.failure(main, name, loop)
        err["status"] = f"@if(greater(length({pv}), 0), {status(0)}, null)"
        err["ifs_response"] = f"@outputs({q(resp)})"
        err["meaning"] = err["meaning"].replace(f"outputs({q(name)})?['statusCode']",
                                                f"if(greater(length({pv}), 0), {status(0)}, 0)") \
            if isinstance(err["meaning"], str) else err["meaning"]
        err["changeset"] = [s.id for s in group]
        good = Seq()
        for i, s in enumerate(group):
            good.add(self.unique(f"Set_{var_name(s.id)}"), {"type": "SetVariable", "inputs": {
                "name": var_name(s.id), "value": "@" + part_json(i)}})
            self.after_step(good, s, self.ops[s.operation], loop)
        bad = Seq()
        bad.add(self.unique(f"{P}_Failed_In_Batch"), self.set_error(err))
        inner.add(self.unique(f"{P}_Succeeded"), {"type": "If", "expression": "@" + ok,
                                                   "actions": good.actions, "else": {"actions": bad.actions}})
        cond = {"type": "If", "expression": self.run_condition(first, loop), "actions": inner.actions,
                "else": {"actions": {}}, "description": short_description(
                    f"One change set (all saved or none): {', '.join(s.id for s in group)}")}
        return [(self.unique(f"If_{P}"), cond)]

    def after_step(self, seq: Seq, step: Step, op: Operation, loop: Optional[str]) -> None:
        sv = self.record_of(step.id)
        for o in self.outputs:
            if o.step != step.id or loop is None:
                continue
            value = prop(sv, o.field)
            vn = f"out_{var_name(o.name)}"
            if o.aggregate == "sum":
                self.variable(vn, "float", 0)
                seq.add(self.unique(f"Add_{vn}"), {"type": "IncrementVariable", "inputs": {
                    "name": vn, "value": f"@float(coalesce({value}, 0))"}})
            elif o.aggregate == "count":
                self.variable(vn, "integer", 0)
                seq.add(self.unique(f"Count_{vn}"), {"type": "IncrementVariable", "inputs": {"name": vn, "value": 1}})
            else:
                self.variable(vn, "array", [])
                seq.add(self.unique(f"Append_{vn}"), {"type": "AppendToArrayVariable", "inputs": {
                    "name": vn, "value": "@" + value}})
        for w in self.cap.warnings:
            if w.source == "step" and w.step == step.id:
                if not isinstance(w.when, WarningCondition):
                    self.refuse(step, "warning condition is prose; write it as {field, equals | not_equals}")
                value = prop(sv, w.when.field)
                if "equals" in w.when.model_fields_set:
                    test = f"equals(string({value}), {q(str(w.when.equals))})"
                else:
                    test = f"not(equals(string({value}), {q(str(w.when.not_equals))}))"
                entry = {"source": "step", "step": step.id, "meaning": w.meaning, "value": "@" + value}
                if loop:
                    entry["line"] = f"@items({q(loop)})"
                seq.add(self.unique(f"Warn_{pascal(step.id)}"), {
                    "type": "If", "expression": "@" + test, "else": {"actions": {}},
                    "actions": {self.unique(f"Append_Warning_{pascal(step.id)}"): {
                        "runAfter": {}, "type": "AppendToArrayVariable",
                        "inputs": {"name": "warnings", "value": entry}}}})
        sends_accept = any(t.lower() == "header.x-ifs-accept-warnings" for t in step.bindings)
        if self.ifs_warnings and step.role == "write" and sends_accept:
            action = self.call_name[step.id]
            header = (f"coalesce(outputs({q(action)})?['headers']?['X-IFS-Info'], "
                      f"outputs({q(action)})?['headers']?['x-ifs-info'])")
            entry = {"source": "ifs", "step": step.id,
                     "messages": f"@json(base64ToString({header}))?['X-IFS-Info']"}
            if loop:
                entry["line"] = f"@items({q(loop)})"
            seq.add(self.unique(f"IFS_Messages_{action}"), {
                "type": "If", "expression": f"@not(empty(coalesce({header}, '')))", "else": {"actions": {}},
                "actions": {self.unique(f"Append_IFS_Messages_{action}"): {
                    "runAfter": {}, "type": "AppendToArrayVariable", "inputs": {"name": "warnings", "value": entry}}}})

    def checks_after(self, step: Step, op: Operation, loop: Optional[str]) -> list[tuple[str, dict]]:
        """Questions (actions without confirmation) and refusals (with confirmation) on a lookup's matches."""
        out = []
        rows = f"coalesce({self.step_var(step.id)}?['value'], json('[]'))"
        n = f"length({rows})"
        rules = []
        if self.confirming:
            rules = [(e.when.get("matches"), {"meaning": e.meaning, "step": step.id}) for e in self.cap.errors
                     if e.step == step.id and "matches" in e.when]
        else:
            for qn in self.cap.questions:
                if qn.when.get("step") == step.id and "matches" in qn.when:
                    rules.append((qn.when["matches"], {"id": qn.id, "asks": qn.asks, "prompt": qn.prompt}))
        for matches, value in rules:
            if op.response.shape != "collection":
                self.refuse(step, f"'matches' needs a lookup that returns a list ({op.id} returns {op.response.shape})")
            if matches not in _MATCH_COUNT:
                self.refuse(step, f"unknown matches value {matches!r}")
            if loop:
                value = {**value, "line": f"@items({q(loop)})"}
            test = _MATCH_COUNT[matches].format(n=n)
            if self.confirming:
                act = self.set_error(value)
                label = f"Refuse_{pascal(step.id)}"
            else:
                act = {"type": "AppendToArrayVariable", "inputs": {"name": "questions", "value": value}}
                label = f"Ask_{pascal(value['id'])}"
            out.append((self.unique(label), {
                "type": "If", "expression": "@" + all_of(["empty(variables('error'))", test]),
                "else": {"actions": {}}, "actions": {self.unique(f"{label}_Record"): {"runAfter": {}, **act}}}))
        return out

    # ── word search ──────────────────────────────────────────────────────────

    def words_reset(self, step: Step) -> list[tuple[str, dict]]:
        sv = var_name(step.id)
        self.variable(f"{sv}_rows", "array", [])
        self.variable(f"{sv}_kept", "array", [])
        return [(self.unique(f"Reset_{sv}_rows"), {"type": "SetVariable", "inputs": {"name": f"{sv}_rows",
                                                                                     "value": []}}),
                (self.unique(f"Reset_{sv}_kept"), {"type": "SetVariable", "inputs": {"name": f"{sv}_kept",
                                                                                     "value": []}})]

    def words_search(self, seq: Seq, step: Step, op: Operation, loop: Optional[str], swagger_params: dict,
                     parts: list[V], words: tuple[str, V]) -> None:
        """Every word contained; if nothing matches, drop the words that match nothing on their own and search
        again with the rest; if still nothing, any of the remaining words."""
        field_name, text = words
        sv = var_name(step.id)
        base = self.joined(parts)
        prefix = f"concat({base.wdl()}, {q(AND)}, " if base is not None else "concat("
        P = pascal(step.id)

        def term(x: str) -> str:
            return f"concat('contains(tolower({field_name}),', {odata_string(V.e(x)).expr}, ')')"

        def search(target: Seq, name: str, terms_expr: str, sep: str, top: int) -> str:
            joined = f"join({terms_expr}, {q(sep)})"
            if sep == OR:
                joined = f"concat('(', {joined}, ')')"
            params = self.parameters(target, step, op, loop, swagger_params, V.e(f"{prefix}{joined})"), top)
            return self.call(target, step, op, loop, self.unique(name), params)

        def keep_rows(target: Seq, name: str) -> None:
            target.add(self.unique(f"Set_{sv}_rows_{name}"), {"type": "SetVariable", "inputs": {
                "name": f"{sv}_rows", "value": f"@coalesce(body({q(name)})?['value'], json('[]'))"}})

        word_list = seq.add(self.unique(f"{P}_Words"), {"type": "Query", "inputs": {
            "from": f"@split(toLower(trim(coalesce({text.wdl()}, ''))), ' ')", "where": "@not(empty(item()))"}})
        terms = seq.add(self.unique(f"{P}_Terms"), {"type": "Select", "inputs": {
            "from": f"@body({q(word_list)})", "select": "@" + term("item()")}})
        first = search(seq, P, f"body({q(terms)})", AND, DEFAULT_TOP)
        keep_rows(seq, first)

        refine = Seq()
        probe_loop = self.unique(f"{P}_Probe_Words")
        probe = Seq()
        probe_name = self.unique(f"{P}_Probe")
        params = self.parameters(probe, step, op, loop, swagger_params,
                                 V.e(f"{prefix}{term(f'items({q(probe_loop)})')})"), 1)
        self.call(probe, step, op, loop, probe_name, params)
        probe.add(self.unique(f"{P}_Keep_Word"), {
            "type": "If", "expression": f"@not(empty(body({q(probe_name)})?['value']))", "else": {"actions": {}},
            "actions": {self.unique(f"{P}_Append_Word"): {"runAfter": {}, "type": "AppendToArrayVariable",
                                                         "inputs": {"name": f"{sv}_kept",
                                                                    "value": f"@items({q(probe_loop)})"}}}})
        refine.add(probe_loop, {"type": "Foreach", "foreach": f"@body({q(word_list)})", "actions": probe.actions,
                                "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
                                "description": "Words that match nothing on their own (typos) are dropped"})
        kept = Seq()
        kept_terms = kept.add(self.unique(f"{P}_Kept_Terms"), {"type": "Select", "inputs": {
            "from": f"@variables({q(sv + '_kept')})", "select": "@" + term("item()")}})
        every = search(kept, f"{P}_Kept_Words", f"body({q(kept_terms)})", AND, DEFAULT_TOP)
        keep_rows(kept, every)
        anyw = Seq()
        some = search(anyw, f"{P}_Any_Word", f"body({q(kept_terms)})", OR, DEFAULT_TOP)
        keep_rows(anyw, some)
        kept.add(self.unique(f"{P}_If_Still_None"), {
            "type": "If", "expression": f"@empty(variables({q(sv + '_rows')}))", "actions": anyw.actions,
            "else": {"actions": {}}})
        refine.add(self.unique(f"{P}_If_Words_Kept"), {
            "type": "If", "expression": f"@not(empty(variables({q(sv + '_kept')})))", "actions": kept.actions,
            "else": {"actions": {}}})
        seq.add(self.unique(f"{P}_If_None"), {"type": "If", "expression": f"@empty(variables({q(sv + '_rows')}))",
                                              "actions": refine.actions, "else": {"actions": {}}})
        seq.add(self.unique(f"Set_{sv}"), {"type": "SetVariable", "inputs": {
            "name": sv, "value": f"@setProperty(json('{{}}'), 'value', variables({q(sv + '_rows')}))"}})
        self.notes.append(f"step {step.id}: word search on {field_name} (every word; then without words that "
                          "match nothing alone; then any word). Candidates are not ranked by how many words match.")

    # ── gates ────────────────────────────────────────────────────────────────

    def gates(self) -> list[tuple[str, dict]]:
        out = []
        if self.confirming:
            out.append((self.unique("Check_Confirmation"), {
                "type": "If", "expression": "@not(equals(triggerBody()?['confirmed'], true))",
                "description": "Writes only after the user confirmed the proposal returned by prepare",
                "else": {"actions": {}}, "actions": {self.unique("Refuse_Unconfirmed"): {
                    "runAfter": {}, **self.set_error({"meaning": "confirmation-required",
                                                      "detail": "send confirmed: true after the user confirmed"})}}}))
            required = [i for i in self.cap.inputs if i.required]
            if required:
                conds = [missing(prop("triggerBody()", i.name), i.type) for i in required]
                out.append((self.unique("Check_Required_Inputs"), {
                    "type": "If", "expression": "@" + all_of(["empty(variables('error'))", any_of(conds)]),
                    "else": {"actions": {}}, "actions": {self.unique("Refuse_Missing_Inputs"): {
                        "runAfter": {}, **self.set_error({"meaning": "missing-input", "detail": "required: " +
                                                          ", ".join(i.name for i in required)})}}}))
                for lst in [i for i in self.cap.inputs if i.type == "list" and i.items]:
                    req = [it for it in lst.items if it.required]
                    if not req:
                        continue
                    flt = self.unique(f"Find_Incomplete_{pascal(lst.name)}")
                    where = any_of([missing(prop("item()", it.name), it.type) for it in req])
                    out.append((flt, {"type": "Query", "inputs": {
                        "from": f"@coalesce(triggerBody()?[{q(lst.name)}], json('[]'))", "where": "@" + where}}))
                    out.append((self.unique(f"Check_{pascal(lst.name)}_Complete"), {
                        "type": "If", "expression": f"@and(empty(variables('error')), not(empty(body({q(flt)}))))",
                        "else": {"actions": {}}, "actions": {self.unique(f"Refuse_Incomplete_{pascal(lst.name)}"): {
                            "runAfter": {}, **self.set_error({
                                "meaning": "missing-input", "detail": f"every {lst.name} entry needs " +
                                ", ".join(it.name for it in req), "lines": f"@body({q(flt)})"})}}}))
        else:
            for qn in self.cap.questions:
                if qn.when.get("missing") and "." not in qn.asks and qn.asks in self.inputs:
                    expr, itype = self.input_ref(qn.asks, None, None)
                    out.append((self.unique(f"Ask_{pascal(qn.id)}"), {
                        "type": "If", "expression": "@" + missing(expr, itype), "else": {"actions": {}},
                        "actions": {self.unique(f"Ask_{pascal(qn.id)}_Record"): {
                            "runAfter": {}, "type": "AppendToArrayVariable",
                            "inputs": {"name": "questions", "value": {"id": qn.id, "asks": qn.asks,
                                                                      "prompt": qn.prompt}}}}}))
        return out

    # ── the flow ─────────────────────────────────────────────────────────────

    def segments(self) -> list[tuple[Optional[str], list[Step]]]:
        segs: list[tuple[Optional[str], list[Step]]] = []
        for s in self.steps:
            if s.for_each and segs and segs[-1][0] == s.for_each:
                segs[-1][1].append(s)
            else:
                segs.append((s.for_each, [s]))
        return segs

    def build(self) -> dict:
        self.variable("error", "object", {})
        self.variable("warnings", "array", [])
        if "questions" in self.action.returns:
            self.variable("questions", "array", [])
        proposal = "proposal" in self.action.returns
        if proposal:
            self.variable("line_results", "array", [])
        for s in self.steps:
            self.variable(var_name(s.id), "object", {})
        try_seq = Seq(statuses=ALL)
        for name, act in self.gates():
            try_seq.add(name, act)
        top_steps = []
        for list_name, steps in self.segments():
            if list_name is None:
                for s in steps:
                    self.loop_of[s.id] = None
                    top_steps.append(s)
                for group in self.groups(steps):
                    for name, act in self.group_items(group, None):
                        try_seq.add(name, act)
                continue
            if list_name not in self.inputs or self.inputs[list_name].type != "list":
                self.refuse(steps[0], f"for_each {list_name!r} is not a list input")
            loop = self.unique(f"For_Each_{pascal(list_name)}")
            body = Seq(statuses=ALL)
            for s in steps:
                self.loop_of[s.id] = loop
                body.add(self.unique(f"Reset_{var_name(s.id)}"), {"type": "SetVariable", "inputs": {
                    "name": var_name(s.id), "value": {}}})
            for group in self.groups(steps):
                for name, act in self.group_items(group, loop):
                    body.add(name, act)
            if proposal:
                body.add(self.unique(f"Keep_{pascal(list_name)}_Results"), {
                    "type": "AppendToArrayVariable", "inputs": {"name": "line_results", "value": {
                        "line": f"@items({q(loop)})",
                        "results": {s.id: f"@variables({q(var_name(s.id))})" for s in steps}}}})
            try_seq.add(loop, {"type": "Foreach", "foreach": f"@coalesce(triggerBody()?[{q(list_name)}], json('[]'))",
                               "actions": body.actions, "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
                               "description": f"One {list_name} entry at a time"})
        for o in self.outputs:
            if o.step not in self.loop_of:
                self.refuse(None, f"output {o.name} reads step {o.step!r}, which this action does not run")
            if self.loop_of[o.step] is not None and o.aggregate is None:
                self.refuse(None, f"output {o.name} reads a per-line step; it needs an aggregate (list, sum, count)")

        actions = Seq()
        for name, vtype, value in self.variables:
            actions.add(self.unique(f"Initialize_{name}"), {"type": "InitializeVariable", "inputs": {
                "variables": [{"name": name, "type": vtype, "value": value}]}})
        actions.add("Try", {"type": "Scope", "actions": try_seq.actions,
                            "description": f"{self.cap.title}: {self.action.name}"})
        succeeded = "equals(actions('Try')?['status'], 'Succeeded')"
        actions.statuses = ALL
        actions.add("Outcome", {"type": "Compose", "inputs": {
            "ok": f"@and(empty(variables('error')), {succeeded})",
            "error": f"@if(empty(variables('error')), if({succeeded}, null, json('{{\"meaning\":\"flow-failed\"}}')), "
                     "variables('error'))"}})
        actions.statuses = ["Succeeded"]
        actions.add("Respond", {"type": "Response", "kind": "Http", "inputs": {
            "statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": self.response(top_steps)}})
        return self.workflow(actions.actions)

    def response(self, top_steps: list[Step]) -> dict:
        body: dict[str, Any] = {"ok": "@outputs('Outcome')?['ok']", "capability": self.cap.id,
                                "revision": self.cap.revision, "action": self.action.name,
                                "error": "@outputs('Outcome')?['error']"}
        if self.outputs:
            outs = {}
            for o in self.outputs:
                if self.loop_of[o.step] is None:
                    outs[o.name] = "@" + prop(self.record_of(o.step), o.field)
                else:
                    outs[o.name] = f"@variables({q('out_' + var_name(o.name))})"
            body["outputs"] = outs
        if "proposal" in self.action.returns:
            body["proposal"] = {"inputs": "@triggerBody()",
                                "results": {s.id: f"@variables({q(var_name(s.id))})" for s in top_steps},
                                "lines": "@variables('line_results')"}
        if "questions" in self.action.returns:
            body["questions"] = "@variables('questions')"
        body["warnings"] = "@variables('warnings')"
        return body

    def trigger_schema(self) -> dict:
        def schema(i: Input) -> dict:
            s: dict[str, Any] = {"type": _SCHEMA_TYPES[i.type]}
            if i.description:
                s["description"] = i.description[:300]
            if i.type == "list":
                s["items"] = {"type": "object", "properties": {it.name: schema(it) for it in i.items}}
            if i.type == "date":
                s["format"] = "date"
            return s

        props = {i.name: schema(i) for i in self.cap.inputs}
        if self.confirming:
            props["confirmed"] = {"type": "boolean",
                                  "description": "true once the user confirmed the proposal returned by prepare"}
        return {"type": "object", "properties": props}

    def workflow(self, actions: dict) -> dict:
        references = {cu.key: {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": cu.reference},
                               "api": {"name": api_token(cu.connector), "logicalName": cu.schema_name}}
                      for cu in self.used.values()}
        return {"properties": {
            "connectionReferences": references,
            "definition": {
                "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/"
                           "workflowdefinition.json#",
                "contentVersion": "1.0.0.0",
                "parameters": {"$connections": {"defaultValue": {}, "type": "Object"},
                               "$authentication": {"defaultValue": {}, "type": "SecureObject"}},
                "triggers": {"manual": {"type": "Request", "kind": "Http", "inputs": {
                    "triggerAuthenticationType": "User", "triggerAllowedUsers": USERS_TOKEN,
                    "schema": self.trigger_schema(), "method": "POST"},
                    "description": short_description(self.action.description or self.cap.title)}},
                "actions": actions},
            "templateName": None},
            "schemaVersion": "1.0.0.0"}
