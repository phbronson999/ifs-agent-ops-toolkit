import hashlib
import hmac
import secrets
import time
import base64
from typing import Optional

import httpx

from ifs_agent_ops.config import get_oauth_config
from ifs_agent_ops.ifs.auth.token_store import TokenData, get as store_get, set as store_set
from ifs_agent_ops.ifs.auth.session_manager import save_session

_PENDING_AUTH_TTL_S = 10 * 60  # 10 minutes


def _generate_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class OAuthManager:
    def __init__(self) -> None:
        self._pending: dict[str, dict] = {}

    def start_auth_flow(self) -> tuple[str, str]:
        """Generate PKCE params, build the authorization URL, return (auth_url, state)."""
        cfg = get_oauth_config()
        state = secrets.token_hex(16)
        verifier = _generate_code_verifier()
        challenge = _generate_code_challenge(verifier)

        self._pending[state] = {
            "code_verifier": verifier,
            "expires_at": time.time() + _PENDING_AUTH_TTL_S,
        }

        from urllib.parse import urlencode
        params = urlencode({
            "client_id": cfg["client_id"],
            "redirect_uri": cfg["redirect_uri"],
            "response_type": "code",
            "scope": cfg["scope"],
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        auth_url = f"{cfg['authorization_url']}?{params}"
        return auth_url, state

    def exchange_code_sync(self, code: str, state: str) -> str:
        """Exchange authorization code for tokens (sync). Returns new session_id."""
        pending = self._pending.pop(state, None)
        if not pending:
            raise ValueError("Invalid or expired state parameter")
        if time.time() > pending["expires_at"]:
            raise ValueError("OAuth flow expired — please start again")

        cfg = get_oauth_config()
        with httpx.Client(timeout=30.0, verify=False) as client:
            resp = client.post(
                cfg["token_url"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": cfg["redirect_uri"],
                    "client_id": cfg["client_id"],
                    "code_verifier": pending["code_verifier"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            tokens = resp.json()

        session_id = secrets.token_hex(16)
        data = TokenData(
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_at=int((time.time() + tokens.get("expires_in", 3600)) * 1000),
            user_id=session_id,
        )
        store_set(session_id, data)
        save_session(session_id, data)
        return session_id

    def _refresh_sync(self, session_id: str) -> None:
        """Synchronously refresh the access token for a session."""
        session = store_get(session_id)
        if not session or not session.refresh_token:
            raise ValueError("No refresh token available")

        cfg = get_oauth_config()
        with httpx.Client(timeout=30.0, verify=False) as client:
            resp = client.post(
                cfg["token_url"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": session.refresh_token,
                    "client_id": cfg["client_id"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            tokens = resp.json()

        updated = TokenData(
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token") or session.refresh_token,
            expires_at=int((time.time() + tokens.get("expires_in", 3600)) * 1000),
            user_id=session.user_id,
        )
        store_set(session_id, updated)
        save_session(session_id, updated)

    async def get_access_token(self, session_id: str) -> str:
        """Return a valid access token, refreshing synchronously if needed."""
        import asyncio

        session = store_get(session_id)
        if not session:
            raise ValueError("No session found. Please authenticate first.")

        # Refresh if within 5-minute buffer
        if session.expires_at < (time.time() + 300) * 1000:
            await asyncio.to_thread(self._refresh_sync, session_id)
            session = store_get(session_id)

        return session.access_token  # type: ignore[union-attr]


# Module-level singleton shared by all tools
oauth_manager = OAuthManager()
