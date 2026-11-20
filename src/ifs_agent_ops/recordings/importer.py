"""``record import``: the only path by which a recording enters an operation package.

Pipeline: raw HAR -> sanitize -> normalize hosts -> [redact values] -> verify -> write.

Verification refuses to write when the transformed recording no longer tells the same story
as the raw one: the sequence of calls (method, target, status, response shape, evidence
headers, body keys) and every data-flow link must be identical, and no real host may remain.
Raw input is never copied into the package.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional

import yaml

from ifs_agent_ops.analysis.odata import anonymise_filter
from ifs_agent_ops.analysis.sequence import build_sequence
from ifs_agent_ops.recordings.har import calls_from_har
from ifs_agent_ops.recordings.redact import business_values, normalize_hosts, redact_values
from ifs_agent_ops.recordings.sanitize import sanitize_har
from ifs_agent_ops.workspace.package import Package


class ImportRefused(RuntimeError):
    pass


@dataclass
class ImportResult:
    recording: Path
    annotations: Path
    values_redacted: bool
    hosts_normalized: dict[str, str]
    sanitize_report: dict[str, int]
    values_mapped: int
    residual_value_matches: int
    calls: int
    ifs_calls: int
    data_flow_links: int
    warnings: list[str] = field(default_factory=list)


def _fingerprint(har: dict):
    views, links = build_sequence(calls_from_har(har))
    calls = [(v.index, v.method, v.status, v.kind, v.service, v.target, v.is_action, v.response,
              tuple(v.headers), tuple(v.body_keys), tuple(v.select),
              tuple(anonymise_filter(x) for x in v.expand),  # nested $filter literals are values
              v.returns_etag, v.traced)
             for v in views]
    flow = [(ln.to_index, ln.to_place, ln.from_index, ln.from_field) for ln in links]
    return calls, flow, views


def _toolkit_version() -> str:
    try:
        return version("ifs-agent-ops-toolkit")
    except PackageNotFoundError:
        return "unknown"


def import_recording(
    har_path: Path,
    package: Package,
    workflow: str,
    task: str,
    *,
    redact: Optional[bool] = None,
    description: str = "",
    force: bool = False,
) -> ImportResult:
    har_path = Path(har_path)
    target = package.recording_path(workflow, task)
    notes = target.with_suffix(".annotations.yaml")
    if target.exists() and not force:
        raise ImportRefused(f"{target} already exists; pass force=True (--force) to replace it")
    redact = package.redact_values_default if redact is None else redact

    raw_bytes = har_path.read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8"))
    raw_calls, raw_flow, raw_views = _fingerprint(raw)

    clean, sanitize_report = sanitize_har(raw)
    clean, hosts = normalize_hosts(clean)
    mapping: dict[str, str] = {}
    if redact:
        clean, mapping = redact_values(clean, seed=raw_bytes)
    mapped = len(mapping)

    new_calls, new_flow, _ = _fingerprint(clean)
    if new_calls != raw_calls:
        diff = next(i for i, (a, b) in enumerate(zip(raw_calls, new_calls)) if a != b) \
            if len(raw_calls) == len(new_calls) else "count"
        raise ImportRefused(f"Transformed recording changed the call sequence (first difference: call {diff}); not written")
    if new_flow != raw_flow:
        raise ImportRefused(
            f"Transformed recording changed data flow ({len(raw_flow)} links before, {len(new_flow)} after); not written")

    text = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    leaked_hosts = [h for h in hosts if h in text.lower()]
    if leaked_hosts:
        raise ImportRefused(f"{len(leaked_hosts)} real host name(s) still present after normalisation; not written")

    warnings: list[str] = []
    residual = 0
    if redact:
        # Business values the redactor replaced must not survive anywhere else (e.g. inside text it
        # could not parse). Counted, never printed.
        originals = {v for v in mapping if len(v) >= 4}
        positional, free_texts = business_values(clean)
        leaked = originals & positional
        if originals and free_texts:
            pattern = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(
                re.escape(v) for v in sorted(originals, key=len, reverse=True)) + r")(?![A-Za-z0-9])")
            for t in free_texts:
                leaked.update(pattern.findall(t))
        residual = len(leaked)
        if residual:
            warnings.append(f"{residual} redacted business value(s) still occur verbatim somewhere in the "
                            "recording (inside text the redactor could not parse); review before publishing")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    ifs_calls = sum(1 for v in raw_views if v.kind != "other")
    annotations = {
        "workflow": workflow,
        "task": task,
        "description": description,
        "source": {
            "file": har_path.name,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toolkit_version": _toolkit_version(),
        },
        "evidence": {
            "sanitized": True,
            "hosts": sorted(set(hosts.values())),
            "values_redacted": bool(redact),
            "ifs_trace_calls": sum(1 for v in raw_views if v.traced),
        },
        "sanitize_report": dict(sorted(sanitize_report.items())),
        "redaction": {"values_mapped": mapped, "residual_value_matches": residual} if redact else None,
        "analysis": {
            "entries": len(raw_views),
            "ifs_calls": ifs_calls,
            "failed_calls": sum(1 for v in raw_views if v.kind != "other" and v.failed),
            "batch_calls": sum(1 for v in raw_views if v.kind == "ifs-batch"),
            "data_flow_links": len(raw_flow),
        },
    }
    notes.write_text(yaml.safe_dump(annotations, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
    return ImportResult(
        recording=target, annotations=notes, values_redacted=bool(redact), hosts_normalized=hosts,
        sanitize_report=sanitize_report, values_mapped=mapped, residual_value_matches=residual,
        calls=len(raw_views), ifs_calls=ifs_calls, data_flow_links=len(raw_flow), warnings=warnings,
    )

