from dataclasses import dataclass
from typing import Optional


@dataclass
class TokenData:
    access_token: str
    expires_at: int  # milliseconds epoch
    user_id: str
    refresh_token: Optional[str] = None


# In-memory token store: session_id -> TokenData
_store: dict[str, TokenData] = {}


def get(session_id: str) -> Optional[TokenData]:
    return _store.get(session_id)


def set(session_id: str, data: TokenData) -> None:
    _store[session_id] = data


def delete(session_id: str) -> None:
    _store.pop(session_id, None)


def all_sessions() -> dict[str, TokenData]:
    return dict(_store)
