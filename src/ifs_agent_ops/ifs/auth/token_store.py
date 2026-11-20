"""Session token storage: in-memory map plus a JSON file on disk.

Replaces the legacy module-level ``token_store`` + ``session_manager`` pair with one object
that takes its file path as a parameter. Behaviour is deliberately unchanged:

- ``load()`` copies the file into memory (legacy ``initialize_token_store()`` at startup).
- ``get()`` reads memory only; a session written to the file by another process after
  ``load()`` is visible to ``current_session_id()`` but not to ``get()``.
- ``current_session_id()`` is the *last* key in the file.
- ``save()`` rewrites the file (indent=2, mode 0600) and never raises.

The file format is shared with the legacy server (``~/.ifs-mcp/session.json``), so both can
run against the same sessions during migration.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_SESSION_FILE = Path.home() / ".ifs-mcp" / "session.json"


@dataclass
class TokenData:
    access_token: str
    expires_at: int  # milliseconds since epoch
    user_id: str
    refresh_token: Optional[str] = None


class FileTokenStore:
    def __init__(self, path: Path = DEFAULT_SESSION_FILE) -> None:
        self.path = Path(path)
        self._memory: dict[str, TokenData] = {}

    # ── memory ──────────────────────────────────────────────────────────────
    def get(self, session_id: str) -> Optional[TokenData]:
        return self._memory.get(session_id)

    def set(self, session_id: str, data: TokenData) -> None:
        self._memory[session_id] = data

    def delete(self, session_id: str) -> None:
        self._memory.pop(session_id, None)

    # ── disk ────────────────────────────────────────────────────────────────
    def _read_file(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def load(self) -> None:
        for sid, data in self._read_file().items():
            self.set(sid, TokenData(
                access_token=data.get("access_token", ""),
                refresh_token=data.get("refresh_token"),
                expires_at=data.get("expires_at", 0),
                user_id=data.get("user_id", sid),
            ))

    def save(self, session_id: str, data: TokenData) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            sessions = self._read_file()
            sessions[session_id] = {
                "access_token": data.access_token,
                "refresh_token": data.refresh_token,
                "expires_at": data.expires_at,
                "user_id": data.user_id,
            }
            self.path.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
            self.path.chmod(0o600)
        except Exception:
            # Session still works in memory; record why persistence failed.
            log.warning("Could not persist session to %s", self.path, exc_info=True)

    def current_session_id(self) -> Optional[str]:
        sessions = self._read_file()
        return list(sessions.keys())[-1] if sessions else None
