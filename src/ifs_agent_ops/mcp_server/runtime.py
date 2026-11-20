"""Process-wide objects the MCP tools share, built once at server start."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ifs_agent_ops.ifs.auth.pkce import PkceAuth
from ifs_agent_ops.ifs.auth.token_store import DEFAULT_SESSION_FILE, FileTokenStore
from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.ifs.settings import IfsSettings


@dataclass
class Runtime:
    settings: IfsSettings
    store: FileTokenStore
    auth: PkceAuth
    client: IfsClient

    @classmethod
    def build(cls, settings: IfsSettings, session_file: Path = DEFAULT_SESSION_FILE) -> "Runtime":
        store = FileTokenStore(session_file)
        store.load()
        auth = PkceAuth(settings, store)
        client = IfsClient(settings.base_url, auth, verify=settings.verify)
        return cls(settings=settings, store=store, auth=auth, client=client)

    @classmethod
    def from_env(cls) -> "Runtime":
        return cls.build(IfsSettings.from_env())
