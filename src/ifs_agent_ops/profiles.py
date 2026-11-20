"""Named IFS environment profiles and the toolkit home directory.

Profiles live in ``<home>/profiles.toml`` (home = ``IFS_AGENT_OPS_HOME`` or
``~/.ifs-agent-ops``)::

    default_profile = "test"

    [profiles.test]
    base_url = "https://ifs-test.example.com"
    realm = "testrealm"
    client_id = "ifs-agent-ops"
    callback_port = 3000          # optional, default 3000
    mcp_server_port = 8000        # optional; else MCP_SERVER_PORT / 8000
    # tls_verify = false          # optional, default true (OS trust store)
    # ca_bundle = "C:/certs/corp.pem"

Selection order: explicit name (``--profile``) > ``IFS_PROFILE`` > ``default_profile`` in the
file. With none of those, an implicit ``default`` profile is built from the legacy
environment variables (``API_BASE_URL``, ``OAUTH_REALM``, ``OAUTH_CLIENT_ID``,
``OAUTH_CALLBACK_PORT``, ``IFS_TLS_VERIFY``, ``IFS_CA_BUNDLE``), so existing ``.env`` setups
keep working. Legacy connection variables are never mixed into a named file profile.

Each profile has its own session file: ``<home>/profiles/<name>/session.json``.
"""
from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from ifs_agent_ops import config
from ifs_agent_ops.ifs.settings import IfsSettings
from ifs_agent_ops.ifs.tls import VerifyType, os_trust_store_context, verify_from_env

LEGACY_SESSION_FILE = Path.home() / ".ifs-mcp" / "session.json"
IMPLICIT_PROFILE = "default"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_REQUIRED = ("base_url", "realm", "client_id")
_KNOWN = set(_REQUIRED) | {"callback_port", "mcp_server_port", "tls_verify", "ca_bundle"}


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    realm: str
    client_id: str
    callback_port: int = 3000
    mcp_server_port: Optional[int] = None
    tls_verify: bool = True
    ca_bundle: Optional[str] = None
    source: Literal["file", "env"] = "file"

    def verify(self) -> VerifyType:
        if self.source == "env":
            return verify_from_env()
        if not self.tls_verify:
            return False
        return self.ca_bundle or os_trust_store_context()

    def settings(self) -> IfsSettings:
        return IfsSettings(
            base_url=self.base_url.rstrip("/"),
            realm=self.realm,
            client_id=self.client_id,
            callback_port=self.callback_port,
            verify=self.verify(),
        )


def toolkit_home() -> Path:
    return Path(os.environ.get("IFS_AGENT_OPS_HOME") or Path.home() / ".ifs-agent-ops")


def profiles_file(home: Optional[Path] = None) -> Path:
    return (home or toolkit_home()) / "profiles.toml"


def session_file(profile_name: str, home: Optional[Path] = None) -> Path:
    return (home or toolkit_home()) / "profiles" / profile_name / "session.json"


def _from_table(name: str, table: dict) -> Profile:
    if not _NAME_RE.match(name):
        raise ProfileError(f"Invalid profile name {name!r}: use letters, digits, '-' or '_'.")
    missing = [k for k in _REQUIRED if not str(table.get(k, "")).strip()]
    if missing:
        raise ProfileError(f"Profile {name!r} is missing required setting(s): {', '.join(missing)}.")
    unknown = sorted(set(table) - _KNOWN)
    if unknown:
        raise ProfileError(f"Profile {name!r} has unknown setting(s): {', '.join(unknown)}.")
    return Profile(
        name=name,
        base_url=str(table["base_url"]),
        realm=str(table["realm"]),
        client_id=str(table["client_id"]),
        callback_port=int(table.get("callback_port", 3000)),
        mcp_server_port=int(table["mcp_server_port"]) if "mcp_server_port" in table else None,
        tls_verify=bool(table.get("tls_verify", True)),
        ca_bundle=table.get("ca_bundle") or None,
        source="file",
    )


def load_profiles(home: Optional[Path] = None) -> tuple[dict[str, Profile], Optional[str]]:
    """Return (profiles by name, default_profile) from profiles.toml (empty if no file)."""
    path = profiles_file(home)
    if not path.exists():
        return {}, None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"Could not parse {path}: {exc}") from exc
    profiles = {name: _from_table(name, table) for name, table in (data.get("profiles") or {}).items()}
    default = data.get("default_profile")
    if default is not None and default not in profiles:
        raise ProfileError(f"default_profile {default!r} in {path} is not defined under [profiles].")
    return profiles, default


def env_profile() -> Profile:
    """The implicit profile built from legacy environment variables."""
    return Profile(
        name=IMPLICIT_PROFILE,
        base_url=config.get_api_base_url(),
        realm=config.get_oauth_realm(),
        client_id=config.get_oauth_client_id(),
        callback_port=config.get_oauth_callback_port(),
        source="env",
    )


def resolve_profile(name: Optional[str] = None, home: Optional[Path] = None) -> Profile:
    profiles, default = load_profiles(home)
    chosen = name or os.environ.get("IFS_PROFILE") or default
    if not chosen:
        return env_profile()
    if chosen in profiles:
        return profiles[chosen]
    if chosen == IMPLICIT_PROFILE:
        return env_profile()
    available = ", ".join(sorted(profiles)) or "(none defined)"
    raise ProfileError(f"Unknown profile {chosen!r}. Available: {available}. Defined in {profiles_file(home)}.")


def migrate_legacy_session(target: Path, legacy: Path = LEGACY_SESSION_FILE) -> bool:
    """Copy the legacy server's session file to ``target`` once. Returns True if copied.

    A copy (not a move) so the legacy server keeps working during migration. The two files
    diverge afterwards; if the IFS realm rotates refresh tokens, a refresh in one makes the
    other's refresh token stale and that server will ask for a new login.
    """
    if target.exists() or not legacy.exists():
        return False
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(legacy, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return True
