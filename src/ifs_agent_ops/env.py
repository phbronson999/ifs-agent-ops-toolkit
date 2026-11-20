"""Load ``.env`` the same way for every entry point (MCP server and CLI).

The file is read from the repository root (as the legacy server did), never overriding
variables already set in the process environment.
"""
from pathlib import Path

from dotenv import load_dotenv

# src/ifs_agent_ops/env.py -> repo root is parents[2].
REPO_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load_env() -> None:
    load_dotenv(REPO_ENV_FILE, override=False)
