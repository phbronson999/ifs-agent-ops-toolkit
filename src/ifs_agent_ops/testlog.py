"""Local test log: every IFS call made through ``call_protected_api``, kept as evidence for validation.

When Claude tests a capability against IFS (Phase 9b), each call is appended here with its request,
response, response headers (ETag, IFS warnings, ...) and timing, and the tool result carries the entry's
reference (``test_log_ref``) so findings can point at exact calls.

The log holds raw business data and the real IFS host, so it stays local (evidence boundary): it lives
beside the profile's session, ``<home>/profiles/<profile>/test-log/<YYYY-MM-DD>.jsonl``, never in a
package. Credentials are never written: ``Authorization`` and cookies are dropped from the headers.
Set ``IFS_AGENT_OPS_TEST_LOG=off`` to turn logging off.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_DIRNAME = "test-log"
MAX_BODY_CHARS = 1_000_000  # larger bodies are cut; the entry says so
_DROP_REQUEST_HEADERS = {"authorization", "cookie", "proxy-authorization"}
_DROP_RESPONSE_HEADERS = {"set-cookie"}


def logging_enabled() -> bool:
    return os.environ.get("IFS_AGENT_OPS_TEST_LOG", "").strip().lower() not in ("0", "off", "false", "no")


def _headers(headers: dict | None, drop: set[str]) -> dict[str, str]:
    return {str(k): str(v) for k, v in (headers or {}).items() if str(k).lower() not in drop}


def _body(value: Any) -> Any:
    """The body as logged: unchanged unless it is too large, then a cut preview with its size."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= MAX_BODY_CHARS:
        return value
    return {"_truncated": True, "_chars": len(text), "_preview": text[:MAX_BODY_CHARS]}


class CallLog:
    """Append-only JSONL log of IFS calls for one profile (one file per local day)."""

    def __init__(self, folder: Path, profile: str) -> None:
        self.folder = Path(folder)
        self.profile = profile
        self._lock = threading.Lock()

    @classmethod
    def beside_session(cls, session_file: Path, profile: str) -> CallLog:
        return cls(Path(session_file).parent / LOG_DIRNAME, profile)

    def _file(self, day: str) -> Path:
        return self.folder / f"{day}.jsonl"

    def record(self, *, method: str, endpoint: str, request_headers: dict | None, request_body: Any,
               status: int | None, ok: bool, response_headers: dict | None = None,
               response_body: Any = None, error: str | None = None, message: str | None = None,
               duration_ms: int | None = None, now: datetime | None = None) -> str:
        """Append one call and return its reference (``YYYYMMDD-NNNN``, numbered per day)."""
        now = (now or datetime.now()).astimezone()
        day = now.strftime("%Y-%m-%d")
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self._file(day)
            count = sum(1 for line in path.open(encoding="utf-8") if line.strip()) if path.exists() else 0
            ref = f"{now:%Y%m%d}-{count + 1:04d}"
            entry = {
                "ref": ref, "at": now.isoformat(timespec="seconds"), "profile": self.profile,
                "method": method, "endpoint": endpoint,
                "request_headers": _headers(request_headers, _DROP_REQUEST_HEADERS),
                "request_body": _body(request_body),
                "status": status, "ok": ok, "error": error, "message": message,
                "response_headers": _headers(response_headers, _DROP_RESPONSE_HEADERS),
                "response_body": _body(response_body), "duration_ms": duration_ms,
            }
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return ref

    def days(self) -> list[str]:
        """Days that have a log, newest first."""
        if not self.folder.is_dir():
            return []
        return sorted((p.stem for p in self.folder.glob("*.jsonl")), reverse=True)

    def entries(self, day: str | None = None) -> list[dict]:
        """Entries of one day (``YYYY-MM-DD``), or of all days, in the order they were made."""
        out: list[dict] = []
        for d in ([day] if day else sorted(self.days())):
            path = self._file(d)
            if path.exists():
                out += [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
        return out

    def get(self, ref: str) -> dict | None:
        ymd = ref.split("-", 1)[0]
        if len(ymd) != 8 or not ymd.isdigit():
            return None
        return next((e for e in self.entries(f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}") if e["ref"] == ref), None)
