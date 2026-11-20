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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ifs-agent-ops")
    parser.add_argument("--version", action="store_true", help="print the toolkit version")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("profiles", help="list IFS environment profiles and their sign-in state")
    har = sub.add_parser("har", help="work with browser HAR recordings").add_subparsers(dest="har_command", required=True)
    s_san = har.add_parser("sanitize", help="remove credentials, trace infrastructure details and _initiator")
    s_san.add_argument("input")
    s_san.add_argument("output")
    s_san.add_argument("--in-place", action="store_true", help="allow output to overwrite input")
    s_an = har.add_parser("analyze", help="print the recorded call sequence and data flow")
    s_an.add_argument("input")
    s_an.add_argument("--all", action="store_true", help="include non-IFS requests")
    args = parser.parse_args(argv)
    load_env()

    if args.version:
        try:
            print(version("ifs-agent-ops-toolkit"))
        except PackageNotFoundError:
            print("unknown")
        return 0
    if args.command == "profiles":
        return cmd_profiles(args)
    if args.command == "har":
        return cmd_har_sanitize(args) if args.har_command == "sanitize" else cmd_har_analyze(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
