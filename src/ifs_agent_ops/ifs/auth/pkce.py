"""OAuth 2.0 authorization-code + PKCE flow against IFS Cloud (Keycloak).

Refactored from the legacy ``OAuthManager``: configuration and token storage are injected
instead of read from module globals/env at call time. Error messages are unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from ifs_agent_ops.ifs.auth.token_store import FileTokenStore, TokenData
from ifs_agent_ops.ifs.settings import IfsSettings

_PENDING_AUTH_TTL_S = 10 * 60  # 10 minutes
_REFRESH_BUFFER_S = 300  # refresh when within 5 minutes of expiry


def _generate_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class PkceAuth:
    """Implements ``TokenProvider`` using a stored PKCE session."""

    def __init__(self, settings: IfsSettings, store: FileTokenStore) -> None:
        self.settings = settings
        self.store = store
        self._pending: dict[str, dict] = {}

    # ── TokenProvider ───────────────────────────────────────────────────────
    def current_session_id(self) -> Optional[str]:
        return self.store.current_session_id()

    async def get_access_token(self, session_id: str) -> str:
        session = self.store.get(session_id)
        if not session:
            raise ValueError("No session found. Please authenticate first.")
        if session.expires_at < (time.time() + _REFRESH_BUFFER_S) * 1000:
            await asyncio.to_thread(self._refresh_sync, session_id)
            session = self.store.get(session_id)
        return session.access_token  # type: ignore[union-attr]

    # ── Interactive login ───────────────────────────────────────────────────
    def start_auth_flow(self) -> tuple[str, str]:
        """Generate PKCE params and the authorization URL. Returns (auth_url, state)."""
        state = secrets.token_hex(16)
        verifier = _generate_code_verifier()
        self._pending[state] = {"code_verifier": verifier, "expires_at": time.time() + _PENDING_AUTH_TTL_S}
        params = urlencode({
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
            "scope": self.settings.scope,
            "state": state,
            "code_challenge": _generate_code_challenge(verifier),
            "code_challenge_method": "S256",
        })
        return f"{self.settings.authorization_url}?{params}", state

    def exchange_code_sync(self, code: str, state: str) -> str:
        """Exchange an authorization code for tokens. Returns the new session id."""
        pending = self._pending.pop(state, None)
        if not pending:
            raise ValueError("Invalid or expired state parameter")
        if time.time() > pending["expires_at"]:
            raise ValueError("OAuth flow expired — please start again")

        tokens = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "client_id": self.settings.client_id,
            "code_verifier": pending["code_verifier"],
        })
        session_id = secrets.token_hex(16)
        data = TokenData(
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_at=int((time.time() + tokens.get("expires_in", 3600)) * 1000),
            user_id=session_id,
        )
        self.store.set(session_id, data)
        self.store.save(session_id, data)
        return session_id

    # ── internals ───────────────────────────────────────────────────────────
    def _refresh_sync(self, session_id: str) -> None:
        session = self.store.get(session_id)
        if not session or not session.refresh_token:
            raise ValueError("No refresh token available")
        tokens = self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": session.refresh_token,
            "client_id": self.settings.client_id,
        })
        updated = TokenData(
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token") or session.refresh_token,
            expires_at=int((time.time() + tokens.get("expires_in", 3600)) * 1000),
            user_id=session.user_id,
        )
        self.store.set(session_id, updated)
        self.store.save(session_id, updated)

    def _token_request(self, form: dict) -> dict:
        with httpx.Client(timeout=30.0, verify=self.settings.verify) as client:
            resp = client.post(
                self.settings.token_url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()
