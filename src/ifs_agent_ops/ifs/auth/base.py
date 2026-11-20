"""The seam between the IFS client and whatever supplies access tokens."""
from __future__ import annotations

from typing import Optional, Protocol


class TokenProvider(Protocol):
    def current_session_id(self) -> Optional[str]:
        """Session to use when the caller does not name one (None if there is none)."""

    async def get_access_token(self, session_id: str) -> str:
        """Return a valid access token for the session, refreshing it if needed. Raises on failure."""
