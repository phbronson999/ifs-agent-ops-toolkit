"""ifs-agent-ops command line interface."""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from typing import Optional
from urllib.parse import urlparse

from ifs_agent_ops import profiles as prof
from ifs_agent_ops.env import load_env
from ifs_agent_ops.ifs.auth.token_store import FileTokenStore


def _session_status(profile_name: str) -> str:
    path = prof.session_file(profile_name)
    store = FileTokenStore(path)
    sid = store.current_session_id()
    if not sid:
        return "not signed in"
    store.load()
    data = store.get(sid)
    if not data:
        return "unreadable session"
    remaining_ms = data.expires_at - int(time.time() * 1000)
    if remaining_ms > 0:
        return f"signed in ({remaining_ms // 60000} min left)"
    return "expired (refreshable)" if data.refresh_token else "expired"


def cmd_profiles(args: argparse.Namespace) -> int:
    try:
        file_profiles, default = prof.load_profiles()
    except prof.ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rows = list(file_profiles.values())
    if prof.IMPLICIT_PROFILE not in file_profiles:
        rows.append(prof.env_profile())
    try:
        active = prof.resolve_profile().name
    except prof.ProfileError as exc:
        active = None
        print(f"warning: {exc}", file=sys.stderr)

    print(f"Profiles file: {prof.profiles_file()}{'' if prof.profiles_file().exists() else ' (not created)'}")
    for p in rows:
        marker = "*" if p.name == active else " "
        host = urlparse(p.base_url).hostname or "(no base URL)"
        source = ".env/environment" if p.source == "env" else "profiles.toml"
        print(f"{marker} {p.name:<12} {host:<32} realm={p.realm or '-':<12} callback={p.callback_port:<5} "
              f"[{source}] {_session_status(p.name)}")

    shared = [port for port, n in Counter(p.callback_port for p in rows).items() if n > 1]
    if shared:
        print(f"\nnote: callback port(s) {', '.join(map(str, shared))} are shared by several profiles. "
              "Those profiles cannot sign in at the same time from separate servers; give each a "
              "distinct callback_port (and register http://localhost:<port>/oauth/callback in IFS IAM) "
              "if you run them concurrently.")
    print("\n* = active profile (--profile, IFS_PROFILE, or default_profile)")
    return 0


def cmd_har_sanitize(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from ifs_agent_ops.recordings.sanitize import sanitize_har

    src, dst = Path(args.input), Path(args.output)
    if dst.resolve() == src.resolve() and not args.in_place:
        print("error: output is the input file; pass --in-place to overwrite it", file=sys.stderr)
        return 2
    har = json.loads(src.read_text(encoding="utf-8"))
    clean, report = sanitize_har(har)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
    print(f"{src} ({src.stat().st_size // 1024} KB) -> {dst} ({dst.stat().st_size // 1024} KB)")
    for k, v in sorted(report.items()):
        print(f"  {k}: {v}")
    print("note: business data values are kept; review before sharing outside your organization.")
    return 0


def cmd_har_analyze(args: argparse.Namespace) -> int:
    from ifs_agent_ops.analysis.sequence import build_sequence, render_sequence
    from ifs_agent_ops.recordings.annotations import load_plan
    from ifs_agent_ops.recordings.har import load_har

    views, links = build_sequence(load_har(args.input))
    print(render_sequence(views, links, include_other=args.all, plan=load_plan(args.input)))
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    from pathlib import Path

    from ifs_agent_ops.workspace.discovery import ENV_VAR, discover_packages
    from ifs_agent_ops.workspace.package import PackageError, init_package

    if args.package_command == "init":
        try:
            p = init_package(Path(args.path), args.name, description=args.description, projections=args.projection,
                             display_name=args.display_name)
        except PackageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"created package '{p.name}' at {p.root}")
        print(f"add it to {ENV_VAR} so the toolkit discovers its skills and recordings")
        return 0
    packages, problems = discover_packages()
    for p in packages:
        skills = len(list(p.skills_dir.glob("*.md"))) if p.skills_dir.is_dir() else 0
        recordings = len(list(p.recordings_dir.rglob("*.har"))) if p.recordings_dir.is_dir() else 0
        print(f"{p.name:<28} {p.version:<8} skills={skills:<3} recordings={recordings:<3} {p.root}")
    if not packages:
        print(f"no packages found; set {ENV_VAR}")
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    return 0


def cmd_record_import(args: argparse.Namespace) -> int:
    from pathlib import Path

    from ifs_agent_ops.recordings.importer import ImportRefused, import_recording
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        package = find_package(args.package)
        redact = True if args.redact_values else False if args.keep_values else None
        r = import_recording(Path(args.har), package, args.workflow, args.task, redact=redact,
                             description=args.description, force=args.force)
    except (PackageError, ImportRefused, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"imported into {package.name}: {r.recording.relative_to(package.root)}")
    print(f"  calls: {r.calls} ({r.ifs_calls} IFS), data-flow links: {r.data_flow_links} (verified unchanged)")
    print(f"  hosts normalized: {len(r.hosts_normalized)} -> {', '.join(sorted(set(r.hosts_normalized.values()))) or '-'}")
    print(f"  values redacted: {'yes' if r.values_redacted else 'no (--keep-values)'}"
          + (f", {r.values_mapped} distinct values mapped" if r.values_redacted else ""))
    print("  sanitized: " + ", ".join(f"{k}={v}" for k, v in sorted(r.sanitize_report.items())))
    for w in r.warnings:
        print(f"  warning: {w}")
    print(f"  annotations: {r.annotations.relative_to(package.root)}")
    return 0


def cmd_metadata(args: argparse.Namespace) -> int:
    import asyncio

    from ifs_agent_ops.ifs.metadata.snapshot import SnapshotRefused, fetch_snapshot
    from ifs_agent_ops.ifs.errors import AuthenticationRequired, IfsApiError
    from ifs_agent_ops.mcp_server.runtime import Runtime
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        package = find_package(args.package)
        rt = Runtime.for_profile(args.profile)
    except (PackageError, prof.ProfileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    async def run() -> int:
        for projection in args.projection:
            try:
                target = await fetch_snapshot(rt.client, package, projection, realm=rt.settings.realm,
                                              include_openapi=not args.no_openapi)
            except AuthenticationRequired:
                print("error: not signed in to IFS for this profile; use the start_oauth MCP tool "
                      "(or the IFS login) and retry", file=sys.stderr)
                return 2
            except (IfsApiError, SnapshotRefused) as exc:
                print(f"error: {projection}: {exc}", file=sys.stderr)
                return 2
            sizes = ", ".join(f"{p.name} {p.stat().st_size // 1024} KB" for p in sorted(target.iterdir()))
            print(f"{projection}: {target.relative_to(package.root)} ({sizes})")
        await rt.client.aclose()
        return 0

    return asyncio.run(run())


def cmd_testlog(args: argparse.Namespace) -> int:
    """The local test log of call_protected_api calls (see ``ifs_agent_ops.testlog``)."""
    import json

    from ifs_agent_ops.ifs.paths import PROJECTION_ROOT
    from ifs_agent_ops.testlog import CallLog

    try:
        name = prof.resolve_profile(args.profile).name
    except prof.ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    log = CallLog.beside_session(prof.session_file(name), name)

    if args.testlog_command == "show":
        entry = log.get(args.ref)
        if entry is None:
            print(f"error: no test log entry {args.ref} for profile {name} ({log.folder})", file=sys.stderr)
            return 2
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return 0

    days = log.days()
    if not days:
        print(f"No test log yet for profile {name} ({log.folder}).")
        return 0
    entries = log.entries(None if args.all else (args.day or days[0]))
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0
    print(f"Test log for profile {name}: {log.folder}")
    for e in entries:
        endpoint = e["endpoint"].removeprefix(PROJECTION_ROOT)
        flags = " ".join(f for f, on in (("etag", any(k.lower() == "etag" for k in e["response_headers"])),
                                          ("warnings", any("warning" in k.lower() for k in e["response_headers"])))
                         if on)
        status = e["status"] if e["status"] is not None else e.get("error") or "-"
        print(f"{e['ref']}  {e['at'][11:19]}  {e['method']:<6} {status!s:<4} "
              f"{e.get('duration_ms') or '-'!s:>6} ms  {endpoint}" + (f"  [{flags}]" if flags else ""))
    print(f"\n{len(entries)} call(s). Details: ifs-agent-ops testlog show <ref>")
    return 0


def cmd_validation(args: argparse.Namespace) -> int:
    """What testing against IFS has shown per runtime-validation requirement (findings; report only)."""
    import json

    from ifs_agent_ops.operations.capability_store import load_capabilities
    from ifs_agent_ops.operations.findings import (
        FindingError,
        load_findings,
        requirement_status,
        status_summary,
    )
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        package = find_package(args.package)
        caps = load_capabilities(package)
        if args.capability not in caps:
            raise FindingError(f"no capability {args.capability!r} in {package.name}")
        cap = caps[args.capability]
        data = load_findings(package, cap.id)
    except (PackageError, KeyError, FindingError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.validation_command == "report":
        from ifs_agent_ops.operations.evidence import (
            active_log,
            build_evidence,
            write_report,
        )

        ev = build_evidence(package, cap, active_log())
        path = write_report(package, ev)
        missing = sorted({c.ref for r in ev.requirements for c in r.calls.values() if not c.found})
        print(f"{cap.id} revision {cap.revision}: {ev.counts_text()}.\nEvidence report (local, not committed): {path}")
        if missing:
            print(f"{len(missing)} cited call(s) are not in this machine's test log: {', '.join(missing)}")
        return 0
    if args.json:
        print(json.dumps([{"requirement": r.number, "text": r.text, "status": r.status,
                           "latest": r.latest.model_dump() if r.latest else None,
                           "outdated_findings": len(r.outdated)} for r in requirement_status(cap, data)], indent=2))
    else:
        print(status_summary(cap, data))
    return 0


def cmd_operations(args: argparse.Namespace) -> int:
    import json

    from ifs_agent_ops.operations import service
    from ifs_agent_ops.operations.model import json_schema
    from ifs_agent_ops.operations.store import approve
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    if args.operations_command == "schema":
        print(json.dumps(json_schema(), indent=2))
        return 0
    try:
        package = find_package(args.package)
        if args.operations_command == "draft":
            report, skipped = service.draft(package)
            print(service.draft_summary(package, report, skipped))
        elif args.operations_command == "list":
            print(service.list_summary(package))
        elif args.operations_command == "show":
            print(service.show(package, args.operation))
        elif args.operations_command == "redraft":
            try:
                op = service.redraft(package, args.operation)
            except ValueError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
            print(f"redrafted {op.id}: revision {op.revision}, back to draft; review and approve it again "
                  "(capabilities that use it need re-approval too)")
        elif args.operations_command == "validate":
            if args.output == "json":
                from dataclasses import asdict

                from ifs_agent_ops.operations.store import load_operations
                from ifs_agent_ops.operations.validate import validate_all

                findings = [asdict(f) for f in validate_all(load_operations(package))
                            if (args.operation is None or f.operation == args.operation)
                            and (args.all or f.severity != "info")]
                print(json.dumps({"schema": "ifs-agent-ops/operation-validation", "schema_version": 1,
                                  "package": package.name, "findings": findings}, indent=2))
                return 1 if any(f["severity"] == "error" for f in findings) else 0
            text, errors = service.validation_report(package, args.operation, include_info=args.all)
            print(text)
            return 1 if errors else 0
        elif args.operations_command == "review":
            from ifs_agent_ops.cli.review_view import make_console, render_operation, render_review_set
            from ifs_agent_ops.operations.operation_review import to_json
            from ifs_agent_ops.operations.review_markdown import write_review_artifacts

            if args.capability and args.operation:
                raise KeyError("give operation ids or --capability, not both")
            review_set, name = service.operation_reviews(package, args.operation, args.capability)
            if args.output == "json":
                print(to_json(review_set))  # the structured result itself; no files written
                return 0
            detail = "full" if args.full else "details" if args.details else "summary"
            console = make_console()
            if len(review_set["operations"]) == 1 and not args.capability:
                render_operation(console, review_set["operations"][0], package.name, detail)
            else:
                render_review_set(console, review_set, package.name, detail)
            doc, appendices = write_review_artifacts(package, review_set, name)
            console.print()
            console.print(f"review document: {doc}", markup=False, highlight=False)
            console.print(f"technical appendix: {doc.parent / 'appendix'} ({len(appendices)} file(s))",
                          markup=False, highlight=False)
        elif args.operations_command == "approve" and args.capability:
            if args.operation:
                raise KeyError("give one operation id or --capability, not both")
            return _approve_operation_set(package, args.capability)
        elif args.operations_command == "approve":
            from ifs_agent_ops.cli.review_view import make_console, render_approval
            from ifs_agent_ops.operations.review_markdown import appendix_name, write_review_artifacts
            from ifs_agent_ops.operations.store import load_operations
            from ifs_agent_ops.operations.validate import approval_blockers

            if not args.operation:
                raise KeyError("give an operation id, or --capability <id> to approve a capability's operation set")
            op_id = service.resolve_operation_id(load_operations(package), args.operation)
            errors, _ = approval_blockers(load_operations(package)[op_id])
            if errors:
                print(f"refused: {op_id} has validation errors\n" + "\n".join(
                    f"  {f.rule}: {f.message}" for f in errors), file=sys.stderr)
                return 1
            review_set, name = service.operation_reviews(package, [op_id])
            r = review_set["operations"][0]
            doc, _ = write_review_artifacts(package, review_set, name)
            render_approval(make_console(), r, package.name, str(doc.parent / appendix_name(r["id"])))
            reviewer = human_confirmation("operation", f"Technical approval of {r['id']}.", package.root)
            if reviewer is None:
                return 1
            op = approve(package, r["id"], reviewer)
            print(f"approved {op.id} (revision {op.revision}) by {reviewer}")
    except (PackageError, FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(prompt: str) -> str:
    return input(prompt)


def _confirmation_code() -> str:
    import secrets

    return secrets.token_hex(3).upper()


def _approve_operation_set(package, cap_id: str) -> int:
    """One human approval ceremony for the operations a capability references (technical approval).

    Each operation still records its own approval (reviewer, time, revision, content hash). Refused
    when any referenced operation has validation errors or open technical model questions."""
    from ifs_agent_ops.cli.review_view import make_console, render_operation_set_approval, render_review_set
    from ifs_agent_ops.operations.approval import ApprovalChanged, apply_approvals, plan_operation_set
    from ifs_agent_ops.operations.review_markdown import write_review_artifacts

    plan = plan_operation_set(package, cap_id)
    reviews = plan.review_set["operations"]
    console = make_console()
    render_review_set(console, plan.review_set, package.name, "summary")
    write_review_artifacts(package, plan.review_set, plan.report_name)

    if plan.blocked:
        print(f"\nrefused: {len(plan.blocked)} operation(s) in {cap_id} have validation errors or open technical "
              "model questions; resolve them (or review them individually) first:", file=sys.stderr)
        for op_id, reasons in plan.blocked:
            print(f"  {op_id}", file=sys.stderr)
            for reason in reasons:
                print(f"    - {reason}", file=sys.stderr)
        return 1
    if not plan.pending:
        console.print(f"\nAll {len(reviews)} operation(s) used by {cap_id} already have a current technical approval.",
                      markup=False)
        return 0
    render_operation_set_approval(console, cap_id, plan.pending, plan.current)
    reviewer = human_confirmation(
        "operation set",
        f"Technical approval of {len(plan.pending)} operation(s) used by {cap_id}, each recorded separately.",
        package.root)
    if reviewer is None:
        return 1
    try:
        approved = apply_approvals(package, plan.pending, reviewer)
    except ApprovalChanged as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    for op in approved:
        console.print(f"approved {op.id} (revision {op.revision}, sha256 {op.approval.content_sha256[:12]})",
                      markup=False)
    console.print(f"{len(approved)} operation(s) approved by {reviewer}; runtime validation unchanged.", markup=False)
    return 0


def human_confirmation(kind: str, meaning: str, root=None) -> Optional[str]:
    """Approval gate: interactive terminal and a one-time code typed back.

    Returns the reviewer name (git user.name, else the OS login), or None when refused. Automated
    callers (scripts, agents, CI, pipes, or a null stdin, which Windows reports as a terminal) are
    refused: they either have no terminal, hit end-of-input, or cannot know the one-time code
    printed only now.
    """
    from ifs_agent_ops.operations.approval import reviewer_name

    if not _is_interactive():
        print(f"refused: approving this {kind} requires a human at an interactive terminal "
              "(no automation, agents or pipes)", file=sys.stderr)
        return None
    code = _confirmation_code()
    reviewer = reviewer_name(root)
    print(f"\n{meaning}\n\nApproving as: {reviewer}\nOne-time confirmation code: {code}\n")
    try:
        if _ask("Type the confirmation code to approve: ").strip().upper() != code:
            print("refused: the confirmation code did not match", file=sys.stderr)
            return None
    except (EOFError, KeyboardInterrupt):
        print(f"\nrefused: approving this {kind} needs interactive input from a human", file=sys.stderr)
        return None
    return reviewer


def cmd_capabilities(args: argparse.Namespace) -> int:
    import json

    from collections import Counter

    from ifs_agent_ops.operations.capability import json_schema
    from ifs_agent_ops.operations.capability_review import write_review
    from ifs_agent_ops.operations.capability_store import ApprovalRefused, approve_capability, load_capabilities
    from ifs_agent_ops.operations.capability_validate import validate_capability
    from ifs_agent_ops.operations.store import load_operations, load_workflows
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    if args.capabilities_command == "schema":
        print(json.dumps(json_schema(), indent=2))
        return 0
    try:
        package = find_package(args.package)
        caps = load_capabilities(package)
        ops = load_operations(package)
        cap_id = getattr(args, "capability", None)
        if cap_id and cap_id not in caps:
            raise KeyError(f"No capability {cap_id!r} in {package.name} (have: {', '.join(caps) or 'none'})")
        if args.capabilities_command == "list":
            if not caps:
                print(f"{package.name} has no capabilities yet (capabilities/<id>.yaml)")
            for cap in caps.values():
                c = Counter(f.severity for f in validate_capability(cap, ops))
                print(f"{cap.status:<9} rev {cap.revision:<3} {cap.id:<32} {c.get('error', 0)} error(s), "
                      f"{c.get('warning', 0)} warning(s), {len(cap.review_notes)} open question(s)")
            return 0
        if args.capabilities_command == "validate":
            from dataclasses import asdict

            selected = [caps[cap_id]] if cap_id else list(caps.values())
            results = [(cap, validate_capability(cap, ops)) for cap in selected]
            errors = sum(f.severity == "error" for _, fs in results for f in fs)
            if args.output == "json":
                print(json.dumps({"schema": "ifs-agent-ops/capability-validation", "schema_version": 1,
                                  "package": package.name,
                                  "capabilities": [{"id": cap.id, "status": cap.status, "revision": cap.revision,
                                                    "findings": [asdict(f) for f in fs]} for cap, fs in results]},
                                 indent=2))
                return 1 if errors else 0
            for cap, fs in results:
                for f in fs:
                    print(f"{f.severity:<8} {cap.id}: {f.where}: {f.rule}: {f.message}")
            return 1 if errors else 0
        if args.capabilities_command == "review":
            path = write_review(package, caps[cap_id], ops, load_workflows(package))
            print(f"review report: {path}")
            return 0
        if args.capabilities_command == "approve":
            cap = caps[cap_id]
            path = write_review(package, cap, ops, load_workflows(package))
            print(f"Review report: {path}")
            if cap.review_notes:
                print(f"\n{len(cap.review_notes)} open question(s) recorded by the author:")
                for n in cap.review_notes:
                    print(f"  - {n}")
            reviewer = human_confirmation(
                "capability",
                "Capability approval: this is the intentional and safe business contract that downstream "
                "agents and applications may use. Approval pins this exact content and the revisions of "
                "the operations it uses. It does NOT claim runtime validation: runtime status stays "
                f"'{cap.runtime_validation.status}' with {len(cap.runtime_validation.requirements)} "
                "requirement(s) carried forward.", package.root)
            if reviewer is None:
                return 1
            approved = approve_capability(package, cap_id, reviewer)
            print(f"approved {approved.id} (revision {approved.revision}) by {reviewer}")
            return 0
    except ApprovalRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (PackageError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    """Generate agent skills from approved capabilities (deterministic; writes into the package)."""
    import json

    from ifs_agent_ops.generators.skills import generate
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        package = find_package(args.package)
        results = generate(package, args.capability, check=args.check)
    except (PackageError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.output == "json":
        print(json.dumps({"schema": "ifs-agent-ops/skill-generation", "schema_version": 1, "package": package.name,
                          "check": args.check, "results": [{"capability": r.capability, "path": str(r.path),
                                                            "action": r.action, "message": r.message}
                                                           for r in results]}, indent=2))
    else:
        for r in results:
            print(f"{r.action:<12} {r.capability:<32} {r.path}" + (f"\n             {r.message}" if r.message else ""))
        if not results:
            print(f"{package.name} has no capabilities")
    failed = any(r.action == "refused" for r in results)
    stale = args.check and any(r.action.startswith("would-") for r in results)
    return 1 if failed or stale else 0


def cmd_connector(args: argparse.Namespace) -> int:
    """Generate the Power Platform connector from approved capabilities (deterministic; writes into the package)."""
    import json

    from ifs_agent_ops.generators.connector import generate
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        package = find_package(args.package)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    gen = generate(package, check=args.check, max_bytes=args.max_bytes)
    if args.output == "json":
        print(json.dumps({"schema": "ifs-agent-ops/connector-generation", "schema_version": 2, "package": package.name,
                          "check": args.check, "ok": gen.ok, "errors": gen.errors,
                          "connectors": [{"projection": p, "size": {"bytes": b.size.bytes, "limit": b.size.limit},
                                          "operations": [{"operation": e.sel.op.id, "operation_id": e.operation_id,
                                                          "method": e.method, "path": e.path, "match": e.sel.match}
                                                         for e in b.entries]}
                                         for p, b in gen.built.items()],
                          "results": [{"path": str(r.path), "action": r.action, "message": r.message}
                                      for r in gen.results]}, indent=2))
    else:
        for r in gen.results:
            print(f"{r.action:<12} {r.path}" + (f"\n             {r.message}" if r.message else ""))
        for p, b in gen.built.items():
            print(f"{p}: {len(b.entries)} operations; apiDefinition {b.size.bytes:,} of {b.size.limit:,} bytes "
                  f"({100 * b.size.bytes / b.size.limit:.1f}%)")
        if gen.built:
            print("Check the deployed path with: ifs-agent-ops deploy validate --package "
                  f"{package.name} --target <target>")
    stale = args.check and any(r.action.startswith("would-") for r in gen.results)
    return 1 if not gen.ok or stale else 0


def cmd_flow(args: argparse.Namespace) -> int:
    """Generate Power Automate flows (one per capability action) from approved capabilities."""
    import json

    from ifs_agent_ops.generators.flow import flow_name, generate
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        package = find_package(args.package)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    gen = generate(package, check=args.check)
    if args.output == "json":
        print(json.dumps({"schema": "ifs-agent-ops/flow-generation", "schema_version": 1, "package": package.name,
                          "check": args.check, "ok": gen.ok, "errors": gen.errors,
                          "flows": [{"capability": c, "action": a, "name": flow_name(c, a)}
                                    for c, acts in gen.flows.items() for a in acts],
                          "results": [{"path": str(r.path), "action": r.action, "message": r.message}
                                      for r in gen.results]}, indent=2))
    else:
        for r in gen.results:
            print(f"{r.action:<12} {r.path}" + (f"\n             {r.message}" if r.message else ""))
        names = [flow_name(c, a) for c, acts in gen.flows.items() for a in acts]
        if names:
            print(f"Flows: {', '.join(names)}. Deploy them with: ifs-agent-ops deploy apply --package "
                  f"{package.name} --target <target> (deploy.yaml lists them under flows)")
        elif gen.ok:
            print("No approved capabilities: nothing to generate.")
    stale = args.check and any(r.action.startswith("would-") for r in gen.results)
    return 1 if not gen.ok or stale else 0


def cmd_agent(args: argparse.Namespace) -> int:
    """Microsoft 365 agent: generate the Agents Toolkit project; render it for a target (machine-local)."""
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        package = find_package(args.package)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.agent_command == "generate":
        from ifs_agent_ops.generators.agent import generate

        gen = generate(package, check=args.check)
        for r in gen.results:
            print(f"{r.action:<12} {r.path}" + (f"\n             {r.message}" if r.message else ""))
        if gen.functions:
            print(f"Functions: {', '.join(gen.functions)}. Render for a target with: ifs-agent-ops agent render "
                  f"--package {package.name} --target <target>")
        stale = args.check and any(r.action.startswith("would-") for r in gen.results)
        return 1 if not gen.ok or stale else 0
    from ifs_agent_ops.deploy.agent import AgentError, render
    from ifs_agent_ops.deploy.targets import TargetError, load_target

    try:
        target = load_target(package.name, args.target)
        out = render(package, target)
    except (TargetError, AgentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Rendered the agent for {target.name} ({target.environment}) in {out.folder}")
    print(f"Environment file: {out.env_file}")
    print("Next, in a terminal (signs in to Microsoft 365" + ("" if out.provisioned else
          "; the OAuth registration asks for the Entra app's client id and secret") + "):")
    print(f'  cd "{out.folder}"')
    print(f"  atk provision --env {target.name}")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    """Deployment intent (deploy.yaml): scaffold it, check it."""
    import json

    from ifs_agent_ops.deploy import intent as di
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        package = find_package(args.package)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    path = di.deploy_path(package)
    if args.deploy_command == "init":
        if path.exists():
            print(f"error: {path} already exists; edit it, then run: ifs-agent-ops deploy check "
                  f"--package {package.name}", file=sys.stderr)
            return 2
        text = di.scaffold(package, args.publisher, args.prefix, args.host_variable)
        import yaml
        errors, _ = di.validate(di.parse(yaml.safe_load(text), path), package)
        if errors:
            print("error: " + "\n       ".join(errors), file=sys.stderr)
            return 2
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"created {path}\nReview it, then run: ifs-agent-ops deploy check --package {package.name}")
        return 0
    if args.deploy_command in ("plan", "apply", "status"):
        return _deploy_target(args, package)
    if args.deploy_command == "validate":
        return _deploy_validate(args, package)
    # check
    try:
        intent = di.load_intent(package)
    except di.IntentError as exc:
        errors, warnings = [str(exc)], []
    else:
        if intent is None:
            errors, warnings = [(f"no {di.DEPLOY_FILE} in {package.root} (create one with: ifs-agent-ops deploy init "
                                 f"--package {package.name} --publisher <name> --prefix <prefix>)")], []
        else:
            errors, warnings = di.validate(intent, package)
    if args.json:
        print(json.dumps({"package": package.name, "path": str(path), "ok": not errors, "errors": errors,
                          "warnings": warnings}, indent=2))
    else:
        for e in errors:
            print(f"FAIL  {e}")
        for w in warnings:
            print(f"WARN  {w}")
        if not errors:
            print(f"OK    {path}" + (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 1 if errors else 0


def _deploy_target(args: argparse.Namespace, package) -> int:
    """deploy plan | apply | status against a target environment (Power Platform)."""
    import json
    from dataclasses import asdict

    from ifs_agent_ops.deploy import powerplatform as ppa
    from ifs_agent_ops.deploy.intent import IntentError, load_intent
    from ifs_agent_ops.deploy.targets import TargetError, load_target

    try:
        target = load_target(package.name, args.target)
        intent = load_intent(package)
        if args.deploy_command == "apply":
            current = ppa.plan(intent, package, target)
            print("\n".join(ppa.describe(current)))
            if not current.changes:
                result = ppa.apply(intent, package, target)
            else:
                if not args.yes:
                    if not _is_interactive():
                        print("error: deploy apply changes the target environment; rerun in a terminal to confirm, "
                              "or pass --yes", file=sys.stderr)
                        return 2
                    if _ask(f"Deploy {len(current.changes)} change(s) to {target.environment}? [y/N] ").strip().lower() \
                            not in ("y", "yes"):
                        print("Canceled; nothing changed.")
                        return 1
                result = ppa.apply(intent, package, target)
                print(f"Imported solution version(s): {', '.join(result.imported) or '-'}"
                      + (f"; added to the solution: {', '.join(result.added)}" if result.added else ""))
                print("Verified: " + ppa.describe(result.after)[-1])
            if result.turned_on:
                print(f"Turned on: {', '.join(result.turned_on)}")
            if result.triggers_recorded:
                print(f"Trigger URLs recorded in {target.path} (flow_triggers)")
            if result.settings_path:
                print(f"Deployment settings file: {result.settings_path}")
            steps = ppa.checklist(result.after)
        else:
            current = ppa.plan(intent, package, target)
            if args.json:
                print(json.dumps({**{k: v for k, v in asdict(current).items() if k != "items"},
                                  "items": [asdict(i) for i in current.items],
                                  "changes": len(current.changes)}, indent=2))
                return 0 if args.deploy_command == "status" or not current.changes else 1
            print("\n".join(ppa.describe(current)))
            steps = ppa.checklist(current)
    except (TargetError, IntentError, ppa.DeployError, ppa.PacError, ppa.FlowApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if steps:
        print("\nManual steps (a person does these in the environment):")
        for n, step in enumerate(steps, 1):
            print(f"  {n}. {step}")
    return 0


def _deploy_validate(args: argparse.Namespace, package) -> int:
    """Deployed-path validation, then the runtime-validation status of the deployed capabilities."""
    import json
    from dataclasses import asdict

    from ifs_agent_ops.deploy import validate as dv
    from ifs_agent_ops.deploy.intent import IntentError, load_intent
    from ifs_agent_ops.deploy.targets import TargetError, load_target

    try:
        target = load_target(package.name, args.target)
        intent = load_intent(package)
        if args.allow_writes and not args.yes:
            if not _is_interactive():
                print("error: --allow-writes runs the cases that write to IFS (for example, creating an order); "
                      "rerun in a terminal to confirm, or pass --yes", file=sys.stderr)
                return 2
            if _ask(f"Run the runtime cases that WRITE to IFS through {target.environment}'s flows? [y/N] "
                    ).strip().lower() not in ("y", "yes"):
                print("Canceled; nothing ran.")
                return 1
        run = dv.validate(intent, package, target, solution_check=not args.skip_solution_check,
                          record=not args.no_record, allow_writes=args.allow_writes)
    except (TargetError, IntentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        data = asdict(run)
        data["path"] = str(run.path)
        data["deployed_ok"] = run.deployed_ok
        print(json.dumps(data, indent=2))
    else:
        print("\n".join(dv.describe(run)))
        print(f"\nRun record (local): {run.path}")
        if any(c.recorded and c.status != c.previous for c in run.capabilities):
            print("The capability files changed (runtime_validation): review and commit them with the package; "
                  f"regenerate skills and connectors to show the status (ifs-agent-ops skills generate --package "
                  f"{package.name}; ifs-agent-ops connector generate --package {package.name}).")
    return 0 if run.deployed_ok else 1


def cmd_targets(args: argparse.Namespace) -> int:
    """Deployment targets: machine-local files naming the environment a package deploys to."""
    from ifs_agent_ops.deploy import targets as tg
    from ifs_agent_ops.deploy.pac import Pac, PacError
    from ifs_agent_ops.workspace.discovery import find_package
    from ifs_agent_ops.workspace.package import PackageError

    try:
        package = find_package(args.package)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.targets_command == "list":
            names = tg.list_targets(package.name)
            print("\n".join(names) if names else f"no targets for {package.name} (ifs-agent-ops targets init "
                                                 f"--package {package.name} --target dev)")
            return 0
        if args.targets_command == "show":
            path = tg.load_target(package.name, args.target).path
            print(f"# {path}\n" + path.read_text(encoding="utf-8"))
            return 0
        variables = {}
        for item in args.var:
            name, sep, value = item.partition("=")
            if not sep or not name:
                raise tg.TargetError(f"--var {item!r}: use NAME=VALUE")
            variables[name.strip()] = value
        environments = Pac().environments()
        if args.environment:
            env = tg.match_environment(environments, args.environment)
        else:
            if not _is_interactive():
                names = ", ".join(e.get("FriendlyName", "?") for e in environments)
                raise tg.TargetError(f"pass --environment (one of: {names})")
            for n, e in enumerate(environments, 1):
                print(f"  {n}. {e.get('FriendlyName')}  {e.get('EnvironmentUrl')}")
            choice = _ask("Environment number: ").strip()
            if not choice.isdigit() or not 1 <= int(choice) <= len(environments):
                raise tg.TargetError("no environment chosen")
            env = environments[int(choice) - 1]
        path = tg.write_target(package.name, args.target, env, variables)
    except (tg.TargetError, PacError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"created {path} ({env.get('FriendlyName')})\n"
          f"Next: ifs-agent-ops deploy plan --package {package.name} --target {args.target}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """Guided capture: intent -> recording plan -> task-bucketed browser recording -> package import."""
    import json

    from ifs_agent_ops.capture import service
    from ifs_agent_ops.capture.render import extension_dir, render
    from ifs_agent_ops.capture.session import CaptureError
    from ifs_agent_ops.capture.store import CaptureStore
    from ifs_agent_ops.workspace.package import PackageError

    store = CaptureStore()
    c = args.capture_command
    try:
        if c == "serve":
            import uvicorn

            from ifs_agent_ops.capture.api import app

            print(f"capture API on http://127.0.0.1:{args.port}/capture/v1 (sessions in {store.root}); Ctrl+C stops",
                  file=sys.stderr)
            uvicorn.run(app(), host="127.0.0.1", port=args.port, log_level="warning")
            return 0
        if c == "extension":
            folder = extension_dir()
            if folder is None:
                print("the browser extension is not in this installation (it ships with the toolkit source)")
                return 1
            built = (folder / "pkg" / "ifs_capture_core_bg.wasm").exists()
            print(folder)
            print("built" if built else "not built yet: run browser-extension/build.ps1")
            return 0 if built else 1
        if c == "list":
            sessions = store.list()
            for s in sessions:
                p = s.progress()
                print(f"{s.id:<40} {s.status:<10} {p['done']}/{p['total']}  {s.intent}")
            if not sessions:
                print(f"no capture sessions in {store.root}")
            return 0
        if c == "import":
            from ifs_agent_ops.recordings.importer import ImportRefused
            from ifs_agent_ops.workspace.discovery import find_package

            try:
                package = find_package(args.package)
                result, plan = service.import_session(store, args.session, package, args.workflow, args.name,
                                                      redact=False if args.keep_values else None, force=args.force)
            except (ImportRefused, PackageError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"imported    {result.recording}")
            print(f"annotations {result.annotations}")
            print(f"plan {'complete' if plan['complete'] else 'INCOMPLETE'}: "
                  + ", ".join(f"{t['title']} ({t['status']}, {t['calls']} calls)" for t in plan["tasks"]))
            for w in result.warnings:
                print(f"warning: {w}")
            return 0
        if c == "new":
            data = service.create(store, args.intent, args.task or [], args.workflow, args.package, args.optional)
        elif c == "show":
            data = service.view(store, store.load(service.resolve(store, args.session)))
        elif c == "plan":
            sid = args.session
            data = service.view(store, store.load(sid))
            if args.intent:
                data = service.apply(store, sid, "intent", intent=args.intent)
            if args.add:
                data = service.apply(store, sid, "add", title=args.add, position=args.at)
            if args.edit:
                data = service.apply(store, sid, "edit", task_id=args.edit[0], title=args.edit[1])
            if args.remove:
                data = service.apply(store, sid, "remove", task_id=args.remove)
            if args.move:
                data = service.apply(store, sid, "move", task_id=args.move[0], position=int(args.move[1]))
            for task_id in args.optional or []:
                data = service.apply(store, sid, "edit", task_id=task_id, optional=True)
            for task_id in args.required or []:
                data = service.apply(store, sid, "edit", task_id=task_id, optional=False)
        elif c == "add":
            pos = int(args.position) if args.position and args.position.isdigit() else args.position
            data = service.apply(store, args.session, "add", title=args.title, position=pos, optional=args.optional)
        else:  # complete | skip | stop
            data = service.apply(store, args.session, c, task_id=getattr(args, "task_id", None))
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.output == "json" else render(data))
    return 0


def _add_output(parser: argparse.ArgumentParser) -> None:
    """Output contract: human text by default; --output json is the structured result for automation."""
    parser.add_argument("--output", choices=["text", "json"], default="text",
                        help="text (human, default) or json (structured, no ANSI, deterministic)")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ifs-agent-ops")
    parser.add_argument("--version", action="store_true", help="print the toolkit version")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("tui", help="guided review and approval (also: run ifs-agent-ops with no arguments)")
    sub.add_parser("profiles", help="list IFS environment profiles and their sign-in state")
    doc = sub.add_parser("doctor", help="check machine readiness per capability (read-only)")
    doc.add_argument("--for", dest="capabilities", action="append", default=[],
                     choices=["toolkit", "ifs", "packages", "power-platform", "agents", "spfx"],
                     help="capability to check (repeatable; default: toolkit, ifs, packages)")
    doc.add_argument("--all", action="store_true", help="check every capability")
    doc.add_argument("--offline", action="store_true", help="skip network checks")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    boot = sub.add_parser("bootstrap", help="install missing local tools per capability (asks before each install)")
    boot.add_argument("--for", dest="capabilities", action="append", default=[],
                      choices=["toolkit", "ifs", "packages", "power-platform", "agents", "spfx"],
                      help="capability to prepare (repeatable; default: toolkit, ifs, packages)")
    boot.add_argument("--all", action="store_true", help="every capability")
    boot.add_argument("--dry-run", action="store_true", help="show what would be installed; change nothing")
    boot.add_argument("--yes", action="store_true", help="install without asking for each tool")
    har = sub.add_parser("har", help="work with browser HAR recordings").add_subparsers(dest="har_command", required=True)
    s_san = har.add_parser("sanitize", help="remove credentials, trace infrastructure details and _initiator")
    s_san.add_argument("input")
    s_san.add_argument("output")
    s_san.add_argument("--in-place", action="store_true", help="allow output to overwrite input")
    s_an = har.add_parser("analyze", help="print the recorded call sequence and data flow")
    s_an.add_argument("input")
    s_an.add_argument("--all", action="store_true", help="include non-IFS requests")

    pkg = sub.add_parser("package", help="operation packages").add_subparsers(dest="package_command", required=True)
    p_init = pkg.add_parser("init", help="create a package skeleton")
    p_init.add_argument("path")
    p_init.add_argument("--name", required=True, help="package name, e.g. ifs-customer-orders")
    p_init.add_argument("--display-name", default="", help="renameable human-facing title; --name is permanent")
    p_init.add_argument("--description", default="")
    p_init.add_argument("--projection", action="append", default=[], help="IFS projection (repeatable)")
    pkg.add_parser("list", help="list packages found via IFS_AGENT_OPS_PACKAGES")

    rec = sub.add_parser("record", help="recordings in packages").add_subparsers(dest="record_command", required=True)
    r_imp = rec.add_parser("import", help="sanitize, normalize hosts, redact values, verify and add a HAR to a package")
    r_imp.add_argument("har")
    r_imp.add_argument("--package", required=True, help="package name (from IFS_AGENT_OPS_PACKAGES) or path")
    r_imp.add_argument("--workflow", required=True)
    r_imp.add_argument("--task", required=True)
    r_imp.add_argument("--description", default="")
    group = r_imp.add_mutually_exclusive_group()
    group.add_argument("--keep-values", action="store_true",
                       help="keep business values (only sanitize + host normalization)")
    group.add_argument("--redact-values", action="store_true", help="redact business values (package default)")
    r_imp.add_argument("--force", action="store_true", help="replace an existing recording")

    cap = sub.add_parser("capture", help="guided capture: intent, recording plan, task-bucketed browser recording"
                         ).add_subparsers(dest="capture_command", required=True)
    c_new = cap.add_parser("new", help="create a session from an intent and an ordered recording plan")
    c_new.add_argument("intent", help='e.g. "I want to teach the builder how to create a customer order"')
    c_new.add_argument("--task", action="append", help="plan task, in order (repeatable)")
    c_new.add_argument("--optional", action="append", metavar="TITLE",
                       help="mark the --task with this title optional (repeatable)")
    c_new.add_argument("--workflow", help="package recordings folder, e.g. create-customer-order")
    c_new.add_argument("--package", help="target package (optional)")
    _add_output(c_new)
    cap.add_parser("list", help="local capture sessions")
    c_show = cap.add_parser("show", help="plan, current task and progress")
    c_show.add_argument("session", nargs="?", help="session id (default: the session recording or being planned)")
    _add_output(c_show)
    c_plan = cap.add_parser("plan", help="edit the intent or plan (tasks that started keep their place)")
    c_plan.add_argument("session")
    c_plan.add_argument("--intent")
    c_plan.add_argument("--add", metavar="TITLE")
    c_plan.add_argument("--at", type=int, metavar="N", help="1-based position for --add (default: end)")
    c_plan.add_argument("--edit", nargs=2, metavar=("TASK", "TITLE"))
    c_plan.add_argument("--remove", metavar="TASK")
    c_plan.add_argument("--move", nargs=2, metavar=("TASK", "N"))
    c_plan.add_argument("--optional", action="append", metavar="TASK", help="mark a task optional (repeatable)")
    c_plan.add_argument("--required", action="append", metavar="TASK", help="mark a task required (repeatable)")
    _add_output(c_plan)
    for name, text in (("complete", "complete the current task"), ("skip", "skip the current task"),
                       ("stop", "stop recording (keeps the session, boundaries and events)")):
        c_x = cap.add_parser(name, help=text)
        c_x.add_argument("session", nargs="?")
        if name != "stop":
            c_x.add_argument("--task-id", help="refuse unless this is the current task")
        _add_output(c_x)
    c_add = cap.add_parser("add", help="add a task (while recording: right after the current one)")
    c_add.add_argument("title")
    c_add.add_argument("--session")
    c_add.add_argument("--position", help="'next', 'end' or a 1-based number")
    c_add.add_argument("--optional", action="store_true", help="the business action can be done without this task")
    _add_output(c_add)
    c_imp = cap.add_parser("import", help="import a stopped session into a package through record import")
    c_imp.add_argument("session")
    c_imp.add_argument("--package", required=True)
    c_imp.add_argument("--workflow", help="default: the session's workflow")
    c_imp.add_argument("--name", help="recording name (default: the session id)")
    c_imp.add_argument("--keep-values", action="store_true", help="do not redact business values")
    c_imp.add_argument("--force", action="store_true", help="replace an existing recording")
    c_srv = cap.add_parser("serve", help="run the local capture API without the MCP server")
    c_srv.add_argument("--port", type=int, default=8000)
    cap.add_parser("extension", help="where the browser extension is, and whether it is built")

    meta = sub.add_parser("metadata", help="projection metadata snapshots").add_subparsers(
        dest="metadata_command", required=True)
    m_snap = meta.add_parser("snapshot", help="pin $metadata and $openapi?V2 into a package (host/realm normalized)")
    m_snap.add_argument("--package", required=True)
    m_snap.add_argument("--projection", required=True, action="append", help="projection name (repeatable)")
    m_snap.add_argument("--profile", help="IFS profile to fetch from (default: active profile)")
    m_snap.add_argument("--no-openapi", action="store_true", help="snapshot $metadata only")

    ops = sub.add_parser("operations", help="canonical operations and workflows").add_subparsers(
        dest="operations_command", required=True)
    for name, helptext in (("draft", "draft operations/workflows from recordings + metadata snapshots"),
                           ("list", "list operations and workflows"),
                           ("validate", "check operations against IFS rules")):
        sp = ops.add_parser(name, help=helptext)
        sp.add_argument("--package", required=True)
        if name == "validate":
            sp.add_argument("--operation")
            sp.add_argument("--all", action="store_true", help="include info findings")
            _add_output(sp)
    o_show = ops.add_parser("show", help="print one operation with its findings")
    o_show.add_argument("operation", help="operation id (or a unique part of it)")
    o_show.add_argument("--package", required=True)
    o_rev = ops.add_parser("review", help="technical review report for operations (written to "
                                          "<package>/.ifs-agent-ops/review/operations/)")
    o_rev.add_argument("operation", nargs="*", help="operation id(s) (or unique parts)")
    o_rev.add_argument("--capability", help="review every operation the capability uses")
    o_rev.add_argument("--package", required=True)
    depth = o_rev.add_mutually_exclusive_group()
    depth.add_argument("--details", action="store_true", help="add request fields, headers, evidence and grouped "
                                                              "finding members")
    depth.add_argument("--full", action="store_true", help="add every response field, composite members and every "
                                                           "finding ungrouped")
    _add_output(o_rev)
    o_redraft = ops.add_parser("redraft", help="re-derive a DRIFTED approved operation from the current recordings "
                                               "(back to draft: review and approve it again)")
    o_redraft.add_argument("operation")
    o_redraft.add_argument("--package", required=True)
    o_appr = ops.add_parser("approve", help="technical approval by a human (interactive terminal only)")
    o_appr.add_argument("operation", nargs="?", help="one operation id (individual review)")
    o_appr.add_argument("--capability", help="approve every operation the capability uses, in one ceremony")
    o_appr.add_argument("--package", required=True)
    ops.add_parser("schema", help="print the JSON Schema of operations and workflows")

    caps = sub.add_parser("capabilities", help="curated capabilities (agent-facing business contracts)").add_subparsers(
        dest="capabilities_command", required=True)
    c_list = caps.add_parser("list", help="list capabilities with status and validation counts")
    c_list.add_argument("--package", required=True)
    for name, helptext in (("validate", "validate a capability (or all) against its canonical operations"),
                           ("review", "write the human review report to <package>/.ifs-agent-ops/review/"),
                           ("approve", "human approval (interactive terminal only)")):
        sp = caps.add_parser(name, help=helptext)
        sp.add_argument("capability", nargs="?" if name == "validate" else None)
        sp.add_argument("--package", required=True)
        if name == "validate":
            _add_output(sp)
    caps.add_parser("schema", help="print the JSON Schema of capabilities")

    con = sub.add_parser("connector", help="Power Platform custom connector generated from approved capabilities"
                         ).add_subparsers(dest="connector_command", required=True)
    c_gen = con.add_parser("generate", help="generate <package>/power-platform/connectors/<Projection>/ "
                                            "(one connector per IFS service) from approved capabilities")
    c_gen.add_argument("--package", required=True)
    c_gen.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file is missing or stale")
    c_gen.add_argument("--max-bytes", type=int, default=1_000_000, help="apiDefinition size limit (default 1000000)")
    _add_output(c_gen)

    flw = sub.add_parser("flow", help="Power Automate flows (one per capability action) generated from approved "
                                      "capabilities").add_subparsers(dest="flow_command", required=True)
    f_gen = flw.add_parser("generate", help="generate <package>/power-platform/flows/<capability>/<action>.json")
    f_gen.add_argument("--package", required=True)
    f_gen.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file is missing or stale")
    _add_output(f_gen)

    agt = sub.add_parser("agent", help="Microsoft 365 agent generated from approved capabilities (calls the flows)"
                         ).add_subparsers(dest="agent_command", required=True)
    a_gen = agt.add_parser("generate", help="generate <package>/agent/m365-agent/ (Agents Toolkit project)")
    a_gen.add_argument("--package", required=True)
    a_gen.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file is missing or stale")
    a_ren = agt.add_parser("render", help="render the agent for a target into ~/.ifs-agent-ops/agents/ "
                                          "(then: atk provision --env <target>)")
    a_ren.add_argument("--package", required=True)
    a_ren.add_argument("--target", required=True)

    skl = sub.add_parser("skills", help="agent skills generated from approved capabilities").add_subparsers(
        dest="skills_command", required=True)
    s_gen = skl.add_parser("generate", help="generate <package>/agent/skills/<capability>.md from approved capabilities")
    s_gen.add_argument("capability", nargs="?", help="capability id (default: every capability in the package)")
    s_gen.add_argument("--package", required=True)
    s_gen.add_argument("--check", action="store_true", help="write nothing; exit 1 if a skill is missing or stale")
    _add_output(s_gen)

    tl = sub.add_parser("testlog", help="local log of IFS calls made while testing with Claude (call_protected_api)"
                        ).add_subparsers(dest="testlog_command", required=True)
    t_list = tl.add_parser("list", help="calls of the latest day with a log (or --day / --all)")
    t_list.add_argument("--day", help="YYYY-MM-DD")
    t_list.add_argument("--all", action="store_true", help="every day")
    t_list.add_argument("--json", action="store_true")
    t_list.add_argument("--profile", help="IFS profile (default: active profile)")
    t_show = tl.add_parser("show", help="one call in full: request, response, response headers")
    t_show.add_argument("ref", help="reference such as 20261121-0007 (the tool result's test_log_ref)")
    t_show.add_argument("--profile", help="IFS profile (default: active profile)")

    val = sub.add_parser("validation", help="findings from testing against IFS, per runtime-validation requirement"
                         ).add_subparsers(dest="validation_command", required=True)
    v_status = val.add_parser("status", help="each requirement: passed / failed / inconclusive / not tested")
    v_status.add_argument("capability")
    v_status.add_argument("--package", required=True)
    v_status.add_argument("--json", action="store_true")
    v_report = val.add_parser("report", help="write the evidence report: findings with the logged calls behind them "
                                             "(<package>/.ifs-agent-ops/review/, local)")
    v_report.add_argument("capability")
    v_report.add_argument("--package", required=True)

    dep = sub.add_parser("deploy", help="deployment intent (deploy.yaml): what should exist, by logical name"
                         ).add_subparsers(dest="deploy_command", required=True)
    d_init = dep.add_parser("init", help="write a first deploy.yaml for the package")
    d_init.add_argument("--package", required=True)
    d_init.add_argument("--publisher", required=True, help="existing publisher unique name (e.g. ContosoAdmin)")
    d_init.add_argument("--prefix", required=True, help="that publisher's customization prefix (e.g. ti)")
    d_init.add_argument("--host-variable", help="environment variable holding the IFS host (default <prefix>_IFSDomain)")
    d_check = dep.add_parser("check", help="validate deploy.yaml against itself and the package (read-only)")
    d_check.add_argument("--package", required=True)
    d_check.add_argument("--json", action="store_true")
    for name, text in (("plan", "what deploying would change in the target environment (read-only)"),
                       ("apply", "deploy: import the solution with what differs, then verify (changes the target)"),
                       ("status", "what is deployed, what differs, and the manual steps left (read-only)")):
        sp = dep.add_parser(name, help=text)
        sp.add_argument("--package", required=True)
        sp.add_argument("--target", required=True, help="target name (see: ifs-agent-ops targets list)")
        if name == "apply":
            sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        else:
            sp.add_argument("--json", action="store_true")
    d_val = dep.add_parser("validate", help="deployed-path validation (static, platform incl. solution checker; "
                                            "runtime via the Phase 12 flow), then record runtime_validation status")
    d_val.add_argument("--package", required=True)
    d_val.add_argument("--target", required=True)
    d_val.add_argument("--skip-solution-check", action="store_true",
                       help="do not upload the solution to the Power Apps checker")
    d_val.add_argument("--no-record", action="store_true", help="report the status without writing it")
    d_val.add_argument("--allow-writes", action="store_true",
                       help="also run runtime cases whose action writes to IFS (asks first)")
    d_val.add_argument("--yes", action="store_true", help="with --allow-writes: do not ask")
    d_val.add_argument("--json", action="store_true")

    tgt = sub.add_parser("targets", help="deployment targets (machine-local, never committed)"
                         ).add_subparsers(dest="targets_command", required=True)
    t_init = tgt.add_parser("init", help="create a target: pick a Power Platform environment from `pac env list`")
    t_init.add_argument("--package", required=True)
    t_init.add_argument("--target", required=True, help="target name, e.g. dev")
    t_init.add_argument("--environment", help="environment display name, id or URL (default: pick from a list)")
    t_init.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                        help="non-secret environment variable value for this target (repeatable)")
    t_list = tgt.add_parser("list", help="targets of a package")
    t_list.add_argument("--package", required=True)
    t_show = tgt.add_parser("show", help="one target")
    t_show.add_argument("--package", required=True)
    t_show.add_argument("--target", required=True)

    args = parser.parse_args(argv)
    load_env()

    if args.command == "testlog":
        return cmd_testlog(args)
    if args.command == "validation":
        return cmd_validation(args)
    if args.command == "deploy":
        return cmd_deploy(args)
    if args.command == "targets":
        return cmd_targets(args)

    if args.command == "package":
        return cmd_package(args)
    if args.command == "record":
        return cmd_record_import(args)
    if args.command == "metadata":
        return cmd_metadata(args)
    if args.command == "operations":
        return cmd_operations(args)
    if args.command == "capabilities":
        return cmd_capabilities(args)
    if args.command == "skills":
        return cmd_skills(args)
    if args.command == "connector":
        return cmd_connector(args)
    if args.command == "flow":
        return cmd_flow(args)
    if args.command == "agent":
        return cmd_agent(args)
    if args.command == "capture":
        return cmd_capture(args)

    if args.version:
        try:
            print(version("ifs-agent-ops-toolkit"))
        except PackageNotFoundError:
            print("unknown")
        return 0
    if args.command == "doctor":
        import json as _json

        from ifs_agent_ops import doctor

        wanted = list(doctor.CAPABILITIES) if args.all else (args.capabilities or None)
        results = doctor.run(wanted, online=not args.offline)
        print(_json.dumps(doctor.as_dicts(results), indent=2) if args.json else doctor.render(results))
        return doctor.exit_code(results)
    if args.command == "bootstrap":
        from ifs_agent_ops import bootstrap

        wanted = bootstrap.resolve(args.capabilities, args.all)
        outcomes = bootstrap.run(wanted, yes=args.yes, dry_run=args.dry_run)
        print("\n" + bootstrap.render(wanted, outcomes))
        return bootstrap.exit_code(outcomes)
    if args.command == "profiles":
        return cmd_profiles(args)
    if args.command == "har":
        return cmd_har_sanitize(args) if args.har_command == "sanitize" else cmd_har_analyze(args)
    from ifs_agent_ops import tui

    if args.command == "tui":
        return tui.run()
    if tui.interactive():  # no command, a human at a terminal: the guided review TUI
        return tui.run()
    parser.print_help()  # no command from a script, agent or pipe: unchanged
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
