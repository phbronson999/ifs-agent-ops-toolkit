"""Connection settings for one IFS Cloud environment."""
from __future__ import annotations

from dataclasses import dataclass, field

from ifs_agent_ops import config
from ifs_agent_ops.ifs.tls import VerifyType, verify_from_env


@dataclass(frozen=True)
class IfsSettings:
    base_url: str
    realm: str
    client_id: str
    callback_port: int = 3000
    verify: VerifyType = field(default=True, compare=False)
    scope: str = "openid"

    @classmethod
    def from_env(cls) -> "IfsSettings":
        return cls(
            base_url=config.get_api_base_url(),
            realm=config.get_oauth_realm(),
            client_id=config.get_oauth_client_id(),
            callback_port=config.get_oauth_callback_port(),
            verify=verify_from_env(),
        )

    @property
    def authorization_url(self) -> str:
        return f"{self.base_url}/auth/realms/{self.realm}/protocol/openid-connect/auth"

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/auth/realms/{self.realm}/protocol/openid-connect/token"

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.callback_port}/oauth/callback"
