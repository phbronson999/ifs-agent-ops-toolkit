"""Local storage of capture sessions: ``<toolkit home>/captures/<session>/``.

- ``session.yaml``: the intent, plan, task boundaries and status
- ``events.jsonl``: the ordered network events streamed by the browser extension, one JSON object
  per line; credential headers are removed at capture and again here
- ``capture.raw.har``: written by ``capture import``; still raw, so it stays local like any raw capture

Nothing here enters a repository. A package receives only the sanitized recording, produced by
``record import`` with the plan in its annotations.

The MCP server, ``capture serve`` and CLI commands may touch the same session from different
processes, so every change runs under a lock file and rewrites ``session.yaml`` atomically.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import yaml

from ifs_agent_ops.capture.session import CaptureError, CaptureSession
from ifs_agent_ops.profiles import toolkit_home

SESSION_FILE = "session.yaml"
EVENTS_FILE = "events.jsonl"
INDEX_FILE = "requests.idx"  # "<ts>\t<request_id>" per recorded request (cheap progress counts)
RAW_HAR_FILE = "capture.raw.har"
CREDENTIAL_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
MAX_BODY_CHARS = 8_000_000
_EVENT_TYPES = {"request", "response", "failed"}


def captures_dir(home: Optional[Path] = None) -> Path:
    return (home or toolkit_home()) / "captures"


class CaptureStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else captures_dir()

    def dir(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or "\\" in session_id or session_id.startswith("."):
            raise CaptureError(f"invalid session id {session_id!r}")
        return self.root / session_id

    # ── sessions ─────────────────────────────────────────────────────────────

    def create(self, session: CaptureSession) -> CaptureSession:
        folder = self.dir(session.id)
        if folder.exists():
            raise CaptureError(f"session {session.id} already exists")
        folder.mkdir(parents=True)
        self._write(session)
        (folder / EVENTS_FILE).touch()
        return session

    def load(self, session_id: str) -> CaptureSession:
        path = self.dir(session_id) / SESSION_FILE
        if not path.exists():
            raise CaptureError(f"no capture session {session_id!r} (see `ifs-agent-ops capture list`)")
        return CaptureSession.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def list(self) -> list[CaptureSession]:
        if not self.root.exists():
            return []
        out = []
        for folder in sorted(self.root.iterdir()):
            if (folder / SESSION_FILE).exists():
                try:
                    out.append(self.load(folder.name))
                except Exception:  # a damaged session is skipped, not fatal for the listing
                    continue
        return out

    def latest_open(self) -> Optional[CaptureSession]:
        """The most recent session that is recording, else the most recent one being planned."""
        sessions = self.list()
        for status in ("recording", "planning"):
            match = [s for s in sessions if s.status == status]
            if match:
                return match[-1]
        return None

    def _write(self, session: CaptureSession) -> None:
        path = self.dir(session.id) / SESSION_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(session.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
                       encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    @contextmanager
    def lock(self, session_id: str, timeout: float = 10.0) -> Iterator[None]:
        lock = self.dir(session_id) / ".lock"
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 30:  # left behind by a crashed process
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise CaptureError(f"session {session_id} is locked by another process")
                time.sleep(0.02)
        try:
            yield
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    @contextmanager
    def edit(self, session_id: str) -> Iterator[CaptureSession]:
        """Load, change and save a session under the lock (nothing is saved if the change raises)."""
        with self.lock(session_id):
            session = self.load(session_id)
            yield session
            self._write(session)

    # ── events ───────────────────────────────────────────────────────────────

    def append_events(self, session_id: str, events: list[dict]) -> int:
        """Validate, strip credentials and append events (caller holds the lock). Returns the count stored."""
        clean = [clean_event(e) for e in events]
        with (self.dir(session_id) / EVENTS_FILE).open("a", encoding="utf-8", newline="\n") as fh:
            for e in clean:
                fh.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
        requests = [e for e in clean if e["type"] == "request"]
        if requests:
            with (self.dir(session_id) / INDEX_FILE).open("a", encoding="utf-8", newline="\n") as fh:
                for e in requests:
                    fh.write(f"{e['ts']!r}\t{e['request_id']}\n")
        return len(clean)

    def requests(self, session_id: str) -> list[tuple[float, str]]:
        """(send time, request id) of every recorded request, without reading bodies."""
        path = self.dir(session_id) / INDEX_FILE
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            ts, _, rid = line.partition("\t")
            out.append((float(ts), rid))
        return out

    def events(self, session_id: str) -> list[dict]:
        path = self.dir(session_id) / EVENTS_FILE
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def clean_event(e: dict) -> dict:
    if not isinstance(e, dict) or e.get("type") not in _EVENT_TYPES:
        raise CaptureError(f"invalid event type {e.get('type') if isinstance(e, dict) else e!r}")
    for key, kind in (("seq", int), ("request_id", str), ("ts", (int, float))):
        if not isinstance(e.get(key), kind) or isinstance(e.get(key), bool):
            raise CaptureError(f"event is missing a valid {key!r}")
    out = dict(e)
    if "headers" in out:
        if not isinstance(out["headers"], dict):
            raise CaptureError("event headers must be an object")
        out["headers"] = {str(k): str(v) for k, v in out["headers"].items() if str(k).lower() not in CREDENTIAL_HEADERS}
    for key in ("body", "post_data"):
        value = out.get(key)
        if value is not None and not isinstance(value, str):
            raise CaptureError(f"event {key} must be a string")
        if value and len(value) > MAX_BODY_CHARS:
            out[key], out[f"{key}_truncated"] = value[:MAX_BODY_CHARS], True
    return out
