"""Skill generator (Phase 8): an approved capability -> an agent skill (Markdown) in the package.

Deterministic and offline: the same approved capability and operations always produce the same
text (no timestamps, no LLM, no IFS calls). The skill has two audiences in one document:

- the agent: what to collect, what to ask, what never to supply, how the `prepare` /
  `submit` conversation goes, what to report;
- implementers and reviewers: a technical reference limited to the curated steps (never the full
  IFS field surface), the request rules the evidence established, the runtime-validation
  requirements still open, and provenance.

Only approved capabilities whose approval is current are generated. A capability that still has
runtime-validation requirements is generated as *not deployment-validated* (roadmap §6.2).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ifs_agent_ops.ifs.paths import projection_path
from ifs_agent_ops.operations.capability import Capability, Source, Step
from ifs_agent_ops.operations.capability_store import load_capabilities
from ifs_agent_ops.generators.gating import GenerationRefused, gate_capability as _gate  # noqa: F401
from ifs_agent_ops.operations.model import Operation
from ifs_agent_ops.operations.store import load_operations
from ifs_agent_ops.workspace.package import Package

GENERATOR = "ifs-agent-ops skills generator v1"
MARKER = "<!-- ifs-agent-ops:generated"
_SOURCE_WORDS = {"document": "the customer's document", "user": "ask the user", "agent": "you determine it"}
_MATCHES = {"none": "no match", "many": "more than one match", "none-or-many": "no match or more than one match"}


def is_generated(text: str) -> bool:
    return MARKER in text


def skill_path(package: Package, cap_id: str) -> Path:
    return package.skills_dir / f"{cap_id}.md"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _cell(text) -> str:
    return _one_line(str(text if text not in (None, "") else "-")).replace("|", "\\|")


def _source_text(src: Source) -> str:
    """Where a value comes from, plus the binding's note: notes carry how to send it (e.g. a contains
    search rather than an exact match), which an agent cannot infer from the source alone."""
    kind = src.kind
    if kind == "step":
        text = f"`{src.step}`.{src.field}"
    elif kind == "fixed":
        text = f"`{json.dumps(src.fixed)}`"
    elif kind == "derived":
        text = f"derived: {_one_line(src.derived)}"
    elif kind == "record":
        from ifs_agent_ops.operations.capability_validate import record_text

        text = record_text(src.record)
        if not src.record.same_as:
            text += " (" + ", ".join(src.record.keep + [m + "?" for m in src.record.keep_if_present]) + ")"
    else:
        text = f"{kind} `{getattr(src, kind)}`" + (f" ({src.transform})" if src.transform else "")
    return text + (f" (note: {_one_line(src.note)})" if src.note else "")


def _when_text(when: dict) -> str:
    if when.get("missing"):
        return "the value is missing"
    if "step" in when:
        return f"the `{when['step']}` check finds {_MATCHES.get(when.get('matches'), when.get('matches'))}"
    return ", ".join(f"{k}={v}" for k, v in when.items()) or "needed"


# ── agent-facing contract ────────────────────────────────────────────────────

def _inputs(cap: Capability) -> list[str]:
    out = ["## Information you collect", "",
           "| Input | Type | Where it comes from | Required | Notes |", "|---|---|---|---|---|"]
    for i in cap.inputs:
        notes = [_one_line(i.description)]
        if i.resolve:
            notes.append(f"checked against IFS by `{i.resolve}`")
        if i.confirm:
            notes.append("shown to the user for confirmation before submit")
        out.append(f"| `{i.name}` | {i.type} | {_SOURCE_WORDS[i.source]} | {'yes' if i.required else 'no'} | "
                   f"{_cell('; '.join(n for n in notes if n))} |")
        for item in i.items:
            notes = [_one_line(item.description)] + ([f"checked against IFS by `{item.resolve}`"] if item.resolve else [])
            out.append(f"| `{i.name}[].{item.name}` | {item.type} | {_SOURCE_WORDS[item.source]} | "
                       f"{'yes' if item.required else 'no'} | {_cell('; '.join(n for n in notes if n))} |")
    out.append("")
    if cap.config:
        out += ["## Organization settings (never ask for or change these)", ""]
        out += [f"- `{k}` = `{v.value}`: {_one_line(v.description)}" for k, v in cap.config.items()]
        out.append("")
    out += ["## Values you never supply", "",
            "Only the inputs above may come from the document, the user or you. Every other value IFS needs "
            "(defaults, customer terms, addresses, prices, totals, cost) is supplied by IFS during the actions "
            "or by the organization settings. Never ask the user for those values, never take them from the "
            "document, and never invent them.", ""]
    return out


def _questions(cap: Capability) -> list[str]:
    if not cap.questions:
        return []
    out = ["## Questions to ask", ""]
    for q in cap.questions:
        out.append(f"- When {_when_text(q.when)} (`{q.asks}`), ask: \"{_one_line(q.prompt)}\"")
    return out + [""]


def _flow(cap: Capability) -> list[str]:
    out = ["## Conversation flow", "",
           "1. Read the customer's document and collect the inputs above.",
           "2. Ask the questions above for anything missing or ambiguous; ask at most two at a time."]
    n = 3
    for a in cap.actions:
        writes = any(cap.step(s) and cap.step(s).role == "write" for s in a.steps)
        if a.requires_confirmation:
            out.append(f"{n}. Show the user the values to confirm: {', '.join(f'`{c}`' for c in a.confirm)}, "
                       "together with any warnings. Wait for an explicit yes. A change by the user means "
                       "running the earlier action again before asking again.")
            n += 1
        out.append(f"{n}. Run `{a.name}`{' (writes to IFS)' if writes else ' (writes nothing)'}: "
                   f"{_one_line(a.description)}")
        n += 1
    out.append(f"{n}. Report the results, warnings and errors described below. Never report a value IFS did "
               "not return.")
    out.append("")
    for a in cap.actions:
        out += [f"### Action `{a.name}`", "", _one_line(a.description), "",
                f"- Writes to IFS: {'yes' if any(cap.step(s) and cap.step(s).role == 'write' for s in a.steps) else 'no'}",
                f"- Requires user confirmation: {'yes' if a.requires_confirmation else 'no'}",
                f"- Returns: {', '.join(f'`{r}`' for r in a.returns) or '-'}", ""]
    return out


def _results(cap: Capability) -> list[str]:
    out = ["## Results, warnings and errors", ""]
    if cap.outputs:
        out += ["| Result | Meaning |", "|---|---|"]
        out += [f"| `{o.name}` | {_cell(o.description or f'{o.step}.{o.field}')} |" for o in cap.outputs]
        out.append("")
    for w in cap.warnings:
        where = f"step `{w.step}`" + (f" when {w.when}" if w.when else "") if w.source == "step" else "IFS"
        out.append(f"- Warning ({where}): {_one_line(w.meaning)}")
    for e in cap.errors:
        cond = ", ".join(f"{k} {v}" for k, v in e.when.items())
        out.append(f"- Error `{e.meaning}`: when `{e.step}` returns {cond}"
                   + (" (include the IFS message)" if e.include == "ifs-message" else ""))
    out.append("")
    if cap.deferred:
        out += ["## Out of scope (do not offer)", ""] + [f"- {_one_line(d)}" for d in cap.deferred] + [""]
    return out


# ── technical reference ──────────────────────────────────────────────────────

def _request_rules(cap: Capability, ops: dict[str, Operation]) -> list[str]:
    rules: list[str] = []
    for step in cap.steps:
        op = ops.get(step.operation)
        if op is None:
            continue
        for target, src in step.bindings.items():
            where, _, name = target.partition(".")
            if where == "header" and src.kind == "fixed":
                rules.append(f"`{step.id}`: send header `{name}: {src.fixed}`.")
            if src.transform == "datetime":
                observed = next((b.observed for b in op.body if b.name == name), [])
                zoned = any(o.endswith("Z") for o in observed) or where == "path"
                rules.append(f"`{step.id}`: `{name}` is a calendar date sent as "
                             f"{'`YYYY-MM-DDT00:00:00Z`' if zoned else '`YYYY-MM-DDT00:00:00`'}"
                             f"{' (format observed and accepted by IFS)' if observed else ''}.")
        for p in op.path_parameters:
            if any(pl.alias for pl in p.placements):
                rules.append(f"`{step.id}`: `{p.name}` is a parameter alias: `{p.name}=@{p.name}` in the path, "
                             f"JSON value in the query string as `@{p.name}=<json>`.")
            elif p.type and p.type.enum and not any(pl.quoted for pl in p.placements):
                rules.append(f"`{step.id}`: `{p.name}` is sent as the enum literal `{p.type.enum}'<Member>'`.")
            elif p.type and p.type.edm == "Edm.String" and step.bindings.get(f"path.{p.name}") is not None \
                    and step.bindings[f"path.{p.name}"].kind == "fixed" and step.bindings[f"path.{p.name}"].fixed is None:
                rules.append(f"`{step.id}`: `{p.name}` is sent as an unquoted `null`.")
    if any(op.method in ("POST", "PATCH") for op in (ops.get(s.operation) for s in cap.steps) if op):
        rules.append("Request bodies use `Content-Type: application/json;IEEE754Compatible=true`; decimals are "
                     "sent as strings, as observed.")
    return list(dict.fromkeys(rules))


def _technical(cap: Capability, ops: dict[str, Operation]) -> list[str]:
    out = ["## Technical reference (for implementers and reviewers)", "",
           "The agent does not call these operations itself; the actions above are executed for it. "
           "Only the curated steps are listed; the full IFS field surface stays in the canonical operations.", ""]
    for a in cap.actions:
        out += [f"### `{a.name}` steps", "",
                "| # | Step | Role | Operation (revision) | Call | Notes |", "|---|---|---|---|---|---|"]
        for n, sid in enumerate(a.steps, 1):
            s: Optional[Step] = cap.step(sid)
            if s is None:
                continue
            op = ops.get(s.operation)
            call = f"`{op.method} {op.path_template}`" if op else "?"
            notes = []
            if s.for_each:
                notes.append(f"for each `{s.for_each}`")
            if s.requires_input:
                notes.append(f"only when `{s.requires_input}` is supplied")
            if s.body_from:
                notes.append("body seeded from " + ", ".join(f"`{b}`" for b in s.body_from))
            out.append(f"| {n} | `{s.id}` | {s.role} | `{s.operation}` (r{op.revision if op else '?'}) | {call} | "
                       f"{_cell('; '.join(notes))} |")
        out.append("")
    out += ["### Values each step sends", ""]
    for s in cap.steps:
        if not s.bindings and not s.body_from:
            continue
        parts = [f"`{t}` <- {_source_text(src)}" for t, src in s.bindings.items()]
        out.append(f"- `{s.id}`: " + ("; ".join(parts) if parts else "no explicit values")
                   + (f"; everything else from {', '.join(f'`{b}`' for b in s.body_from)}" if s.body_from else ""))
    out.append("")
    rules = _request_rules(cap, ops)
    if rules:
        out += ["### Request rules established from evidence", ""] + [f"- {r}" for r in rules] + [""]
    projections = sorted({ops[s.operation].projection for s in cap.steps if s.operation in ops})
    out += ["Base path: " + ", ".join(f"`{projection_path(p)}`" for p in projections), ""]
    return out


def _runtime_and_provenance(cap: Capability, ops: dict[str, Operation], package: Package) -> list[str]:
    rv = cap.runtime_validation
    out = [f"## Runtime validation: {rv.status}", "",
           "Approval does not mean the sequence has been executed against IFS. Until runtime validation "
           "succeeds, artifacts generated from this capability must not be deployed or promoted as "
           "production-ready. Open requirements:", ""]
    out += [f"{i}. {_one_line(r)}" for i, r in enumerate(rv.requirements, 1)] or ["_None recorded._"]
    a = cap.approval
    used = sorted({s.operation for s in cap.steps})
    out += ["", "## Provenance", "",
            f"- Package: `{package.name}`",
            f"- Capability: `{cap.id}` revision {cap.revision}, content sha256 `{cap.content_hash()}`",
            f"- Capability approval: {a.approved_by} at {a.approved_at}",
            "- Operations (technical approval at revision): " + ", ".join(
                f"`{i}` r{ops[i].revision}" for i in used if i in ops),
            f"- Evidence: " + ", ".join(f"`{e}`" for e in cap.evidence),
            f"- Generator: {GENERATOR}", ""]
    return out


def render_skill(package: Package, cap: Capability, ops: dict[str, Operation]) -> str:
    _gate(cap, ops)
    validated = cap.runtime_validation.status == "validated"
    lines = [f"# {cap.title}", "", _one_line(cap.description), "",
             f"{MARKER} capability={cap.id} revision={cap.revision} sha256={cap.content_hash()[:16]}; "
             f"do not edit by hand: ifs-agent-ops skills generate {cap.id} --package {package.name} -->", "",
             f"> **Status:** generated from the approved capability `{cap.id}` (revision {cap.revision}). Runtime "
             f"validation: **{cap.runtime_validation.status}**"
             + ("." if validated else " - this skill is **not deployment-validated**."), ""]
    lines += _inputs(cap) + _questions(cap) + _flow(cap) + _results(cap)
    lines += _technical(cap, ops) + _runtime_and_provenance(cap, ops, package)
    return "\n".join(lines).rstrip() + "\n"


@dataclass
class GenerationResult:
    capability: str
    path: Path
    action: str  # created | updated | unchanged | would-create | would-update | refused
    message: str = ""


def generate(package: Package, cap_id: Optional[str] = None, check: bool = False) -> list[GenerationResult]:
    caps = load_capabilities(package)
    ops = load_operations(package)
    if cap_id and cap_id not in caps:
        raise KeyError(f"No capability {cap_id!r} in {package.name} (have: {', '.join(caps) or 'none'})")
    results = []
    for cap in ([caps[cap_id]] if cap_id else list(caps.values())):
        path = skill_path(package, cap.id)
        try:
            text = render_skill(package, cap, ops)
        except GenerationRefused as exc:
            results.append(GenerationResult(cap.id, path, "refused", str(exc)))
            continue
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing is not None and not is_generated(existing):
            results.append(GenerationResult(cap.id, path, "refused",
                                            f"{path.name} exists and was not generated; not overwriting it"))
            continue
        if existing == text:
            results.append(GenerationResult(cap.id, path, "unchanged"))
        elif check:
            results.append(GenerationResult(cap.id, path, "would-create" if existing is None else "would-update"))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            results.append(GenerationResult(cap.id, path, "created" if existing is None else "updated"))
    return results
