import json
import os
import stat
from pathlib import Path
from typing import Optional

from ifs_agent_ops.ifs.auth.token_store import TokenData, set as store_set, all_sessions

_SESSION_DIR = Path.home() / ".ifs-mcp"
_SESSION_FILE = _SESSION_DIR / "session.json"


def _ensure_dir() -> None:
    _SESSION_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


def _load_from_disk() -> dict[str, dict]:
    if not _SESSION_FILE.exists():
        return {}
    try:
        return json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_session(session_id: str, data: TokenData) -> None:
    try:
        _ensure_dir()
        sessions = _load_from_disk()
        sessions[session_id] = {
            "access_token": data.access_token,
            "refresh_token": data.refresh_token,
            "expires_at": data.expires_at,
            "user_id": data.user_id,
        }
        _SESSION_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
        _SESSION_FILE.chmod(0o600)
    except Exception:
        pass  # silent fail — session still works in memory


def get_current_session_id() -> Optional[str]:
    sessions = _load_from_disk()
    if not sessions:
        return None
    # Return the last added session (matches TypeScript behavior)
    return list(sessions.keys())[-1]


def initialize_token_store() -> None:
    """Load persisted sessions from disk into the in-memory store on startup."""
    for sid, data in _load_from_disk().items():
        store_set(
            sid,
            TokenData(
                access_token=data.get("access_token", ""),
                refresh_token=data.get("refresh_token"),
                expires_at=data.get("expires_at", 0),
                user_id=data.get("user_id", sid),
            ),
        )
