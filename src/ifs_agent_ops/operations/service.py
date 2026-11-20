"""Package-level operations used by both the CLI and the MCP tools."""
from __future__ import annotations

from collections import Counter

import yaml

from ifs_agent_ops.ifs.metadata.snapshot import load_metadata, snapshot_dir
from ifs_agent_ops.operations.draft import draft_package
from ifs_agent_ops.operations.store import MergeReport, load_operations, load_workflows, merge_draft
from ifs_agent_ops.operations.validate import validate_all
from ifs_agent_ops.recordings.har import load_har
from ifs_agent_ops.analysis.odata import parse_projection_url
from ifs_agent_ops.workspace.package import Package


def projections_with_snapshots(package: Package) -> dict:
    """Metadata for every package projection that appears in a recording (snapshot required)."""
    used: set[str] = set()
    for rec in package.recordings_dir.rglob("*.har") if package.recordings_dir.is_dir() else []:
        for call in load_har(rec):
            target = parse_projection_url(call.url)
            if target and target.service in package.projections:
                used.add(target.service)
    missing = sorted(p for p in used if not (snapshot_dir(package, p) / "metadata.xml").exists())
    if missing:
        raise FileNotFoundError(
            "Missing metadata snapshot(s): " + ", ".join(missing)
            + f". Run: ifs-agent-ops metadata snapshot --package {package.name} --projection <name>")
    return {p: load_metadata(package, p) for p in sorted(used)}


def draft(package: Package) -> tuple[MergeReport, dict[str, int]]:
    d = draft_package(package, projections_with_snapshots(package))
    return merge_draft(package, d.operations, d.workflows), d.skipped


def draft_summary(package: Package, report: MergeReport, skipped: dict[str, int]) -> str:
    ops = load_operations(package)
    lines = [f"Package {package.name}: {len(ops)} operation(s), {len(load_workflows(package))} workflow(s)."]
    for label in ("created", "updated", "unchanged", "drifted", "not_in_recordings"):
        items = getattr(report, label)
        if items:
            lines.append(f"- {label.replace('_', ' ')}: {len(items)}")
    if report.drifted:
        lines.append("  approved operations whose recordings now differ (review, re-approve or deprecate):")
        lines += [f"    - {x}" for x in report.drifted]
    if skipped:
        lines.append("- skipped calls: " + ", ".join(f"{k} ({v})" for k, v in sorted(skipped.items())))
    findings = validate_all(ops)
    counts = Counter(f.severity for f in findings)
    lines.append(f"- validation: {counts.get('error', 0)} error(s), {counts.get('warning', 0)} warning(s), "
                 f"{counts.get('info', 0)} info")
    return "\n".join(lines)


def list_summary(package: Package) -> str:
    ops = load_operations(package)
    if not ops:
        return f"Package {package.name} has no operations yet; run the draft first."
    lines = [f"{'status':<9} {'kind':<9} {'method':<6} id"]
    for op in sorted(ops.values(), key=lambda o: (o.kind, o.id)):
        lines.append(f"{op.status:<9} {op.kind:<9} {op.method:<6} {op.id}")
    wfs = load_workflows(package)
    if wfs:
        lines.append("")
        lines += [f"workflow {wf.status:<9} {wf.id} ({len(wf.steps)} steps)" for wf in wfs.values()]
    return "\n".join(lines)


def show(package: Package, op_id: str) -> str:
    ops = load_operations(package)
    if op_id not in ops:
        matches = [i for i in ops if op_id.lower() in i.lower()]
        if len(matches) != 1:
            raise KeyError(f"No unique operation matches {op_id!r}" + (f": {', '.join(matches[:10])}" if matches else ""))
        op_id = matches[0]
    op = ops[op_id]
    text = yaml.safe_dump(op.model_dump(mode="json", exclude_none=True), sort_keys=False, width=120)
    findings = [f for f in validate_all(ops) if f.operation == op_id]
    if findings:
        text += "\n# validation\n" + "\n".join(f"# {f.severity}: {f.rule}: {f.message}" for f in findings)
    return text


def validation_report(package: Package, op_id: str | None = None, include_info: bool = False) -> tuple[str, int]:
    ops = load_operations(package)
    findings = [f for f in validate_all(ops) if (op_id is None or f.operation == op_id)
                and (include_info or f.severity != "info")]
    errors = sum(1 for f in findings if f.severity == "error")
    if not findings:
        return "no findings", 0
    return "\n".join(f"{f.severity:<8} {f.rule:<26} {f.operation}: {f.message}" for f in findings), errors


def resolve_operation_id(ops: dict, op_id: str) -> str:
    if op_id in ops:
        return op_id
    matches = [i for i in ops if op_id.lower() in i.lower()]
    if len(matches) != 1:
        raise KeyError(f"No unique operation matches {op_id!r}" + (f": {', '.join(matches[:10])}" if matches else ""))
    return matches[0]


def operation_reviews(package: Package, op_ids: list[str] | None = None,
                      capability: str | None = None) -> tuple[dict, str]:
    """The structured technical review for operations chosen by id or by the capability using them.

    Returns (review set, report name). Every presentation renders from the review set."""
    from ifs_agent_ops.operations.capability_store import load_capabilities
    from ifs_agent_ops.operations.operation_review import build_operation_review, build_review_set

    ops = load_operations(package)
    caps = load_capabilities(package)
    if capability:
        if capability not in caps:
            raise KeyError(f"No capability {capability!r} in {package.name}")
        chosen = list(dict.fromkeys(s.operation for s in caps[capability].steps if s.operation in ops))
        name = f"capability-{capability}"
    else:
        chosen = [resolve_operation_id(ops, i) for i in (op_ids or [])]
        if not chosen:
            raise KeyError("name at least one operation, or use --capability")
        name = chosen[0] if len(chosen) == 1 else "selection"
    models: dict = {}
    for proj in {ops[i].projection for i in chosen}:
        try:
            models[proj] = load_metadata(package, proj)
        except (FileNotFoundError, OSError):
            models[proj] = None
    cache: dict = {}
    reviews = [build_operation_review(package, ops[i], ops, models.get(ops[i].projection), caps, cache)
               for i in chosen]
    return build_review_set(reviews, caps[capability] if capability else None), name
