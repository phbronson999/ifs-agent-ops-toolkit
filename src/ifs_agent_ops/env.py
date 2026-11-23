"""Load ``.env`` the same way for every entry point (MCP server and CLI).

The file is read from the repository root (as the legacy server did), never overriding
variables already set in the process environment.

Settings that ``.env`` supplied can be read again with ``live_value``: a change to the file (for example the
TUI registering a new package) then applies to a running process, such as the toolkit server, without a restart.
"""
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# src/ifs_agent_ops/env.py -> repo root is parents[2].
REPO_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Settings this process took from .env (not from its own environment), with the value last taken. Only these
# follow later edits of the file, and only while nothing else has changed them in the process.
_FROM_FILE: dict[str, str] = {}


def load_env() -> None:
    if REPO_ENV_FILE.exists():
        for key, value in dotenv_values(REPO_ENV_FILE).items():
            if key not in os.environ and value is not None:
                _FROM_FILE[key] = value
    load_dotenv(REPO_ENV_FILE, override=False)


def from_env_file(key: str) -> bool:
    """True when the setting came from .env and nothing in this process has changed it since."""
    return key in _FROM_FILE and os.environ.get(key) == _FROM_FILE[key]


def live_value(key: str) -> str:
    """The setting's current value: re-read from .env when .env supplied it, else from the process environment."""
    if from_env_file(key) and REPO_ENV_FILE.exists():
        value = dotenv_values(REPO_ENV_FILE).get(key)
        if value is not None and value != _FROM_FILE[key]:
            _FROM_FILE[key] = os.environ[key] = value
    return os.environ.get(key, "")
