import base64
import json
import os
import platform
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ifs_agent_ops.ifs.auth.oauth_manager import OAuthManager
from ifs_agent_ops.ifs.auth.session_manager import get_current_session_id, save_session
from ifs_agent_ops.ifs.auth.token_store import get as store_get

_callback_started = False
_callback_lock = threading.Lock()

def _get_icon_base64() -> str:
    try:
        from importlib.resources import files
        data = files("ifs_agent_ops.assets").joinpath("icon.png").read_bytes()
        return base64.b64encode(data).decode()
    except Exception:
        return ""


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _create_login_page(auth_url: str, state: str) -> str:
    tmp_dir = Path(tempfile.gettempdir()) / "ifs-mcp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    html_path = tmp_dir / f"login-{state}.html"

    icon_b64 = _get_icon_base64()
    logo_html = (
        f'<img src="data:image/png;base64,{icon_b64}" width="80" height="80" '
        'alt="MCP Server" style="border-radius:50%;display:block;margin:0 auto 24px;" />'
        if icon_b64
        else '<div class="logo">🔐</div>'
    )

    escaped_url = json.dumps(auth_url)  # produces a JSON-safe string literal
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>IFS Cloud Login</title>
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
            background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
            min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; }}
    .container {{ background:white; border-radius:16px; box-shadow:0 20px 60px rgba(0,0,0,.3);
                  max-width:480px; width:100%; padding:48px 40px; text-align:center;
                  animation:fadeIn .5s ease-out; }}
    .logo {{ width:80px; height:80px; margin:0 auto 24px;
             background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
             border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:32px; }}
    h1 {{ font-size:28px; color:#1a202c; margin-bottom:12px; font-weight:700; }}
    .subtitle {{ font-size:16px; color:#718096; margin-bottom:32px; line-height:1.5; }}
    .info-box {{ background:#f7fafc; border-left:4px solid #667eea; padding:16px;
                 margin-bottom:32px; text-align:left; border-radius:4px; }}
    .info-box p {{ font-size:14px; color:#4a5568; line-height:1.6; margin-bottom:8px; }}
    .info-box p:last-child {{ margin-bottom:0; }}
    .login-button {{ background:linear-gradient(135deg,#667eea 0%,#764ba2 100%); color:white;
                     border:none; padding:16px 48px; font-size:18px; font-weight:600;
                     border-radius:8px; cursor:pointer; width:100%;
                     box-shadow:0 4px 12px rgba(102,126,234,.4); }}
    .login-button:hover {{ transform:translateY(-2px); box-shadow:0 6px 20px rgba(102,126,234,.6); }}
    .security-note {{ margin-top:24px; font-size:12px; color:#a0aec0;
                      display:flex; align-items:center; justify-content:center; gap:6px; }}
    @keyframes fadeIn {{ from {{ opacity:0; transform:translateY(20px); }}
                         to   {{ opacity:1; transform:translateY(0); }} }}
  </style>
</head>
<body>
  <div class="container">
    {logo_html}
    <h1>Connect to your IFS Cloud instance</h1>
    <p class="subtitle">Sign in to access your protected resources through the MCP server</p>
    <div class="info-box">
      <p><strong>What happens next:</strong></p>
      <p>1. Click the button below to open the IFS login page</p>
      <p>2. Enter your credentials on the IFS Cloud portal</p>
      <p>3. You'll be redirected back automatically</p>
      <p>4. Your session will be saved securely</p>
    </div>
    <button class="login-button" onclick="handleLogin()">🔐 Sign in to IFS Cloud</button>
    <div class="security-note"><span>🔒</span><span>Secured with OAuth 2.0 + PKCE</span></div>
  </div>
  <script>
    function handleLogin() {{ window.location.href = {escaped_url}; }}
  </script>
</body>
</html>"""

    html_path.write_text(html, encoding="utf-8")
    return str(html_path)


def _open_browser(url: str) -> None:
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["cmd.exe", "/c", "start", "", url], shell=False)
        elif system == "Darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception as exc:
        print(f"Failed to open browser: {exc}")


def _make_callback_handler(oauth_manager: OAuthManager):
    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/oauth/callback":
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            code = (params.get("code") or [None])[0]
            state = (params.get("state") or [None])[0]
            error = (params.get("error") or [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()

            if error:
                body = f"<html><body><h1>Authentication Failed</h1><p>Error: {_html_escape(error)}</p></body></html>"
                self.wfile.write(body.encode())
                return

            try:
                session_id = oauth_manager.exchange_code_sync(code, state)
                body = (
                    "<html><body>"
                    "<h1>Authentication Successful!</h1>"
                    "<p>Session saved. You can close this window and start using the MCP tools.</p>"
                    "</body></html>"
                )
            except Exception as exc:
                body = (
                    f"<html><body><h1>Authentication Failed</h1>"
                    f"<p>Error: {_html_escape(str(exc))}</p></body></html>"
                )

            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass  # suppress request logs

    return OAuthCallbackHandler


def _start_callback_server(oauth_manager: OAuthManager, port: int) -> None:
    global _callback_started
    with _callback_lock:
        if _callback_started:
            return
        _callback_started = True

    handler = _make_callback_handler(oauth_manager)
    try:
        server = HTTPServer(("127.0.0.1", port), handler)
    except OSError:
        # Port already in use — callback server already running
        return

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()


async def handler(force: bool, oauth_manager: OAuthManager) -> str:
    from ifs_agent_ops.config import get_oauth_callback_port

    if not force:
        session_id = get_current_session_id()
        if session_id:
            session = store_get(session_id)
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

    auth_url, state = oauth_manager.start_auth_flow()
    port = get_oauth_callback_port()
    _start_callback_server(oauth_manager, port)

    html_path = _create_login_page(auth_url, state)
    _open_browser(f"file://{html_path}")

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
