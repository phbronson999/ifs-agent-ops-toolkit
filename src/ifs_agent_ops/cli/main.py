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
    print("note: business data values are kept; review before sharing outside your organisation.")
    return 0


def cmd_har_analyze(args: argparse.Namespace) -> int:
    from ifs_agent_ops.analysis.sequence import build_sequence, render_sequence
    from ifs_agent_ops.recordings.har import load_har

    views, links = build_sequence(load_har(args.input))
    print(render_sequence(views, links, include_other=args.all))
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
    print(f"  hosts normalised: {len(r.hosts_normalized)} -> {', '.join(sorted(set(r.hosts_normalized.values()))) or '-'}")
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
            reviewer = human_confirmation("operation", r["id"], f"Technical approval of {r['id']}.")
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
    from ifs_agent_ops.operations import service
    from ifs_agent_ops.operations.review_markdown import write_review_artifacts
    from ifs_agent_ops.operations.store import approve, load_operations
    from ifs_agent_ops.operations.validate import approval_blockers

    review_set, name = service.operation_reviews(package, None, cap_id)
    ops = load_operations(package)
    reviews = review_set["operations"]
    console = make_console()
    render_review_set(console, review_set, package.name, "summary")
    write_review_artifacts(package, review_set, name)

    blocked = []
    for r in reviews:
        errors, questions = approval_blockers(ops[r["id"]])
        if errors or questions:
            blocked.append((r["id"], [f"{f.rule}: {f.message}" for f in errors] +
                            [f"open technical model question: {q}" for q in questions]))
    if blocked:
        print(f"\nrefused: {len(blocked)} operation(s) in {cap_id} have validation errors or open technical "
              "model questions; resolve them (or review them individually) first:", file=sys.stderr)
        for op_id, reasons in blocked:
            print(f"  {op_id}", file=sys.stderr)
            for reason in reasons:
                print(f"    - {reason}", file=sys.stderr)
        return 1
    pending = [r for r in reviews if not (r["status"] == "approved" and r["approval_current"])]
    current = [r for r in reviews if r not in pending]
    if not pending:
        console.print(f"\nAll {len(reviews)} operation(s) used by {cap_id} already have a current technical approval.",
                      markup=False)
        return 0
    render_operation_set_approval(console, cap_id, pending, current)
    acknowledgement = f"approve {len(pending)} operations for {cap_id}"
    reviewer = human_confirmation(
        "operation set", acknowledgement,
        f"Technical approval of {len(pending)} operation(s) used by {cap_id}, each recorded separately.",
        prompt=f"Type exactly '{acknowledgement}' to approve this operation set: ")
    if reviewer is None:
        return 1
    latest = load_operations(package)  # refuse if anything changed since the review was shown
    changed = [r["id"] for r in pending if r["id"] not in latest or latest[r["id"]].revision != r["revision"]
               or latest[r["id"]].technical_hash() != r["content_sha256"]]
    if changed:
        print("refused: changed since the review was shown: " + ", ".join(changed), file=sys.stderr)
        return 1
    for r in pending:
        op = approve(package, r["id"], reviewer)
        console.print(f"approved {op.id} (revision {op.revision}, sha256 {op.approval.content_sha256[:12]})",
                      markup=False)
    console.print(f"{len(pending)} operation(s) approved by {reviewer}; runtime validation unchanged.", markup=False)
    return 0


def human_confirmation(kind: str, target_id: str, meaning: str, prompt: Optional[str] = None) -> Optional[str]:
    """Approval gate: interactive terminal, a one-time code and the id typed back, and a name.

    Returns the reviewer name, or None when refused. Automated callers (scripts, agents, CI, pipes,
    or a null stdin, which Windows reports as a terminal) are refused: they either have no
    terminal, hit end-of-input, or cannot know the one-time code printed only now.
    """
    if not _is_interactive():
        print(f"refused: approving this {kind} requires a human at an interactive terminal "
              "(no automation, agents or pipes)", file=sys.stderr)
        return None
    code = _confirmation_code()
    print(f"\n{meaning}\n\nOne-time confirmation code: {code}\n")
    try:
        if _ask("Type the confirmation code: ").strip().upper() != code:
            print("refused: the confirmation code did not match", file=sys.stderr)
            return None
        if _ask(prompt or f"Type the {kind} id to approve it: ").strip() != target_id:
            print("refused: the id did not match", file=sys.stderr)
            return None
        reviewer = _ask("Your name (recorded with the approval): ").strip()
    except (EOFError, KeyboardInterrupt):
        print(f"\nrefused: approving this {kind} needs interactive input from a human", file=sys.stderr)
        return None
    if not reviewer:
        print("refused: a reviewer name is required", file=sys.stderr)
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
                "capability", cap_id,
                "Capability approval: this is the intentional and safe business contract that downstream "
                "agents and applications may use. Approval pins this exact content and the revisions of "
                "the operations it uses. It does NOT claim runtime validation: runtime status stays "
                f"'{cap.runtime_validation.status}' with {len(cap.runtime_validation.requirements)} "
                "requirement(s) carried forward.")
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


def _add_output(parser: argparse.ArgumentParser) -> None:
    """Output contract: human text by default; --output json is the structured result for automation."""
    parser.add_argument("--output", choices=["text", "json"], default="text",
                        help="text (human, default) or json (structured, no ANSI, deterministic)")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ifs-agent-ops")
    parser.add_argument("--version", action="store_true", help="print the toolkit version")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("profiles", help="list IFS environment profiles and their sign-in state")
    doc = sub.add_parser("doctor", help="check machine readiness per capability (read-only)")
    doc.add_argument("--for", dest="capabilities", action="append", default=[],
                     choices=["toolkit", "ifs", "packages", "power-platform", "agents", "spfx"],
                     help="capability to check (repeatable; default: toolkit, ifs, packages)")
    doc.add_argument("--all", action="store_true", help="check every capability")
    doc.add_argument("--offline", action="store_true", help="skip network checks")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
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
    r_imp = rec.add_parser("import", help="sanitize, normalise hosts, redact values, verify and add a HAR to a package")
    r_imp.add_argument("har")
    r_imp.add_argument("--package", required=True, help="package name (from IFS_AGENT_OPS_PACKAGES) or path")
    r_imp.add_argument("--workflow", required=True)
    r_imp.add_argument("--task", required=True)
    r_imp.add_argument("--description", default="")
    group = r_imp.add_mutually_exclusive_group()
    group.add_argument("--keep-values", action="store_true",
                       help="keep business values (only sanitize + host normalisation)")
    group.add_argument("--redact-values", action="store_true", help="redact business values (package default)")
    r_imp.add_argument("--force", action="store_true", help="replace an existing recording")

    meta = sub.add_parser("metadata", help="projection metadata snapshots").add_subparsers(
        dest="metadata_command", required=True)
    m_snap = meta.add_parser("snapshot", help="pin $metadata and $openapi?V2 into a package (host/realm normalised)")
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

    args = parser.parse_args(argv)
    load_env()

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
    if args.command == "profiles":
        return cmd_profiles(args)
    if args.command == "har":
        return cmd_har_sanitize(args) if args.har_command == "sanitize" else cmd_har_analyze(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
