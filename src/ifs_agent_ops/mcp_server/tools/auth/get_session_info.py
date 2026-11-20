import json
import time

from ifs_agent_ops.ifs.auth.token_store import FileTokenStore


async def handler(store: FileTokenStore) -> str:
    session_id = store.current_session_id()

    if not session_id:
        return json.dumps({
            "authenticated": False,
            "message": "No active session found. Please use start_oauth to authenticate.",
        }, indent=2)

    session = store.get(session_id)
    if not session:
        return json.dumps({
            "authenticated": False,
            "message": "Session ID found but session data is missing. Please re-authenticate.",
        }, indent=2)

    now_ms = int(time.time() * 1000)
    is_expired = session.expires_at < now_ms
    expires_in_s = max(0, (session.expires_at - now_ms) // 1000)

    return json.dumps({
        "authenticated": True,
        "sessionId": session_id,
        "expiresIn": 0 if is_expired else expires_in_s,
        "expiresInMinutes": 0 if is_expired else expires_in_s // 60,
        "isExpired": is_expired,
        "canRefresh": session.refresh_token is not None,
        "userId": session.user_id,
        "message": (
            "Session expired but can be refreshed automatically on next API call"
            if is_expired
            else f"Session active and valid for {expires_in_s // 60} more minutes"
        ),
    }, indent=2)
