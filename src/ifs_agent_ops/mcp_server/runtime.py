"""Process-wide objects the MCP tools share, built once at server start for one profile."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ifs_agent_ops import profiles as prof
from ifs_agent_ops.ifs.auth.pkce import PkceAuth
from ifs_agent_ops.ifs.auth.token_store import FileTokenStore
from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.ifs.settings import IfsSettings
from ifs_agent_ops.testlog import CallLog, logging_enabled


@dataclass
class Runtime:
    profile: str
    settings: IfsSettings
    store: FileTokenStore
    auth: PkceAuth
    client: IfsClient
    mcp_server_port: Optional[int] = None
    test_log: CallLog | None = None  # local log of call_protected_api calls (None when turned off)

    @classmethod
    def build(cls, settings: IfsSettings, session_file: Path, profile: str = prof.IMPLICIT_PROFILE,
              mcp_server_port: Optional[int] = None) -> "Runtime":
        store = FileTokenStore(session_file)
        store.load()
        auth = PkceAuth(settings, store)
        client = IfsClient(settings.base_url, auth, verify=settings.verify)
        test_log = CallLog.beside_session(session_file, profile) if logging_enabled() else None
        return cls(profile=profile, settings=settings, store=store, auth=auth, client=client,
                   mcp_server_port=mcp_server_port, test_log=test_log)

    @classmethod
    def for_profile(cls, name: Optional[str] = None, home: Optional[Path] = None) -> "Runtime":
        profile = prof.resolve_profile(name, home)
        session = prof.session_file(profile.name, home)
        if profile.source == "env":
            prof.migrate_legacy_session(session)
        return cls.build(profile.settings(), session, profile.name, profile.mcp_server_port)
