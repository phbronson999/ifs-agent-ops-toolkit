import os


def get_api_base_url() -> str:
    return os.environ.get("API_BASE_URL", "").rstrip("/")


def get_oauth_realm() -> str:
    return os.environ.get("OAUTH_REALM", "")


def get_oauth_client_id() -> str:
    return os.environ.get("OAUTH_CLIENT_ID", "")


def get_skills_dir() -> str | None:
    return os.environ.get("SKILLS_DIR") or None


def get_scratch_dir() -> str | None:
    return os.environ.get("IFS_MCP_SCRATCH_DIR") or None


def get_mcp_server_port() -> int:
    return int(os.environ.get("MCP_SERVER_PORT", "8000"))


def get_oauth_callback_port() -> int:
    return int(os.environ.get("OAUTH_CALLBACK_PORT", "3000"))


def get_oauth_config() -> dict:
    base = get_api_base_url()
    realm = get_oauth_realm()
    port = get_oauth_callback_port()
    return {
        "client_id": get_oauth_client_id(),
        "authorization_url": f"{base}/auth/realms/{realm}/protocol/openid-connect/auth",
        "token_url": f"{base}/auth/realms/{realm}/protocol/openid-connect/token",
        "redirect_uri": f"http://localhost:{port}/oauth/callback",
        "scope": "openid",
    }
