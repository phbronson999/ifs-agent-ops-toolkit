import json
import time

from ifs_agent_ops.ifs.auth.callback import create_login_page, open_browser, start_callback_server
from ifs_agent_ops.ifs.auth.pkce import PkceAuth
from ifs_agent_ops.ifs.auth.token_store import FileTokenStore


async def handler(force: bool, auth: PkceAuth, store: FileTokenStore) -> str:
    if not force:
        session_id = store.current_session_id()
        if session_id:
            session = store.get(session_id)
            if session:
                now_ms = int(time.time() * 1000)
                if session.expires_at > now_ms:
                    return json.dumps({
                        "status": "already_authenticated",
                        "message": "You already have an active session",
                        "sessionId": session_id,
                        "expiresInMinutes": (session.expires_at - now_ms) // 60000,
                        "hint": "Use force=True to re-authenticate",
                    }, indent=2)

    auth_url, state = auth.start_auth_flow()
    start_callback_server(auth, auth.settings.callback_port)

    html_path = create_login_page(auth_url, state)
    open_browser(f"file://{html_path}")

    return json.dumps({
        "status": "login_window_opened",
        "message": "Login window opened in your default browser",
        "state": state,
        "instructions": (
            "Please complete the authentication in the browser window. "
            "After successful login, you'll be redirected back and your session "
            "will be saved automatically."
        ),
        "loginPagePath": html_path,
    }, indent=2)
