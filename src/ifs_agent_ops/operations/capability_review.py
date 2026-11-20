"""Human review report for a capability (Markdown).

The report is what a reviewer reads before approving: the agent-visible contract, the questions
and confirmations, every canonical field of every write step with its source and evidence, the
open questions from the author, and validation results. It is written to the package's reserved
local folder (``.ifs-agent-ops/review/``), never into committed source.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.operations.capability_validate import field_sources, validate_capability
from ifs_agent_ops.operations.model import Operation, Workflow
from ifs_agent_ops.workspace.package import LOCAL_STATE_DIR, Package


def observed_sources(workflows: dict[str, Workflow], op_id: str) -> dict[str, list[str]]:
    """field -> recorded sources ("operation.field") from observed workflow data flow."""
    out: dict[str, Counter] = defaultdict(Counter)
    for wf in workflows.values():
        step_op = {s.step: s.operation for s in wf.steps}
        for s in wf.steps:
            if s.operation != op_id:
                continue
            for b in s.bindings:
                if b.to.startswith("body "):
                    out[b.to[5:]][f"{step_op[b.from_step].split('.', 2)[2]}.{b.from_field}"] += 1
    return {k: [x for x, _ in v.most_common(2)] for k, v in out.items()}


def _md_escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_review(cap: Capability, ops: dict[str, Operation], workflows: dict[str, Workflow]) -> str:
    findings = validate_capability(cap, ops)
    counts = Counter(f.severity for f in findings)
    L = [f"# Capability review: {cap.title}",
         "",
         f"`{cap.id}` · revision {cap.revision} · status **{cap.status}**"
         + (f" · approved by {cap.approval.approved_by} at {cap.approval.approved_at}" if cap.approval else ""),
         "",
         cap.description or "_No description._",
         "",
         "> **What approval means:** this is the intentional and safe business contract that downstream "
         "agents and applications may use. Operation approval (technical correctness) is a separate, "
         "earlier gate.",
         ""]

    L += ["## 1. Decisions for the reviewer", ""]
    L += [f"{i}. {_md_escape(n)}" for i, n in enumerate(cap.review_notes, 1)] or ["_None recorded._"]
    if cap.deferred:
        L += ["", "**Deliberately out of scope for this revision** (agents must not offer it):", ""]
        L += [f"- {_md_escape(d)}" for d in cap.deferred]
    L += ["", f"## 2. Validation: {counts.get('error', 0)} error(s), {counts.get('warning', 0)} warning(s)", ""]
    blocking = [f for f in findings if f.severity in ("error", "warning")]
    if blocking:
        L += ["| severity | where | rule | message |", "|---|---|---|---|"]
        L += [f"| {f.severity} | {f.where} | {f.rule} | {_md_escape(f.message)} |" for f in blocking]
    else:
        L += ["_No errors or warnings._"]

    L += ["", "## 3. Actions (the only externally exposed units)", "",
          "| action | steps (in order) | requires confirmation | user confirms | returns |", "|---|---|---|---|---|"]
    for a in cap.actions:
        L.append(f"| **{a.name}** — {_md_escape(a.description)} | {' → '.join(a.steps)} | "
                 f"{'yes' if a.requires_confirmation else 'no'} | {', '.join(a.confirm) or '-'} | {', '.join(a.returns) or '-'} |")

    L += ["", "## 4. Agent-facing inputs (everything an agent, document or user supplies)", "",
          "| input | type | source | required | resolved by | confirmed | description |", "|---|---|---|---|---|---|---|"]
    for name, i in cap.all_inputs().items():
        L.append(f"| `{name}` | {i.type} | {i.source} | {'yes' if i.required else 'no'} | {i.resolve or '-'} | "
                 f"{'yes' if i.confirm else 'no'} | {_md_escape(i.description)} |")

    L += ["", "## 5. Questions the agent asks the user", ""]
    L += [f"- **{q.id}** fills `{q.asks}` when {q.when}: {_md_escape(q.prompt)}" for q in cap.questions] or ["_None._"]

    L += ["", "## 6. Organisation configuration", ""]
    L += [f"- `{k}` ({v.type}) = **{v.value!r}** — {_md_escape(v.description)}" for k, v in cap.config.items()] or ["_None._"]

    L += ["", "## 7. Steps", ""]
    for s in cap.steps:
        op = ops.get(s.operation)
        state = f"{op.status}, rev {op.revision}" if op else "MISSING"
        L += [f"### `{s.id}` — {s.role}{' (for each ' + s.for_each + ')' if s.for_each else ''}", "",
              f"Operation `{s.operation}` ({state}) — `{op.method + ' ' + op.path_template if op else '?'}`", ""]
        if s.description:
            L += [_md_escape(s.description), ""]
        non_body = {k: v for k, v in s.bindings.items() if not k.startswith("body.")}
        if non_body:
            L += ["Path/filter/header bindings: " + ", ".join(
                f"`{k}` ← {v.kind} {getattr(v, v.kind) if v.kind != 'step' else v.step + '.' + v.field}"
                for k, v in non_body.items()), ""]
        if op is None or s.role != "write":
            if op is not None and any(k.startswith("body.") for k in s.bindings):
                L += ["Body bindings: " + ", ".join(f"`{k[5:]}` ← {v.kind}" for k, v in s.bindings.items()
                                                    if k.startswith("body.")), ""]
            continue
        fsrc = field_sources(cap, s, op, ops)
        observed = observed_sources(workflows, op.id)
        shapes = {p.name: ", ".join(p.observed[:2]) for p in op.body}
        by_kind = Counter(f.kind for f in fsrc)
        L += [f"**{len(fsrc)} canonical body fields:** " + ", ".join(f"{n} {k}" for k, n in by_kind.most_common()), ""]
        explicit = [f for f in fsrc if f.kind not in ("seeded",)]
        L += ["| field | IFS type | required | supplied by | observed value shape | recorded source |",
              "|---|---|---|---|---|---|"]
        for f in explicit:
            L.append(f"| `{f.field}` | {f.canonical_type or '?'} | {'yes' if f.required else ('-' if f.required is None else 'no')} | "
                     f"**{f.kind}**: {_md_escape(f.detail)} | {_md_escape(shapes.get(f.field, ''))} | "
                     f"{_md_escape(', '.join(observed.get(f.field, [])) or '(user-entered or UI constant)')} |")
        seeded = [f for f in fsrc if f.kind == "seeded"]
        if seeded:
            groups: dict[str, list[str]] = defaultdict(list)
            for f in seeded:
                groups[f.detail].append(f.field)
            L += ["", "Seeded from earlier records (IFS defaults/validations; not agent-visible):", ""]
            L += [f"- {src}: {', '.join(sorted(names))}" for src, names in groups.items()]
        L.append("")

    L += ["## 8. Outputs, warnings and errors returned to the agent", ""]
    L += [f"- output `{o.name}` ← {o.step}.{o.field}{' (' + o.aggregate + ')' if o.aggregate else ''}"
          + (f": {_md_escape(o.description)}" if o.description else "") for o in cap.outputs]
    L += [f"- warning ({w.source}{': ' + w.header if w.header else ''}{': ' + w.step if w.step else ''}) — {_md_escape(w.meaning)}"
          for w in cap.warnings]
    L += [f"- error `{e.meaning}` when {e.step} {e.when}" for e in cap.errors]

    L += ["", "## 9. Operations used and their technical approval", "",
          "| operation | kind | status | revision |", "|---|---|---|---|"]
    for op_id in sorted({s.operation for s in cap.steps}):
        op = ops.get(op_id)
        L.append(f"| `{op_id}` | {op.kind if op else '?'} | {op.status if op else 'MISSING'} | {op.revision if op else '-'} |")

    L += ["", "## 10. Evidence", ""] + [f"- `{e}`" for e in cap.evidence]
    L += ["", "---", "",
          "To approve, a human runs, in an interactive terminal:", "",
          f"```\nifs-agent-ops capabilities approve {cap.id} --package <package>\n```", "",
          "Approval records your name, the content hash and each operation's revision; any later change "
          "invalidates it."]
    return "\n".join(L) + "\n"


def write_review(package: Package, cap: Capability, ops, workflows) -> Path:
    folder = package.root / LOCAL_STATE_DIR / "review"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{cap.id}.md"
    path.write_text(render_review(cap, ops, workflows), encoding="utf-8", newline="\n")
    return path
