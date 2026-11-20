"""Typed errors raised by the IFS client.

These carry facts only. Wording aimed at an LLM or a user (for example "call start_oauth")
belongs to the interface layer (``ifs_agent_ops.mcp_server.presenters``), not here.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

AuthReason = Literal["no_session", "token_error", "unauthorized"]


class IfsError(Exception):
    """Base class for IFS client errors."""


class AuthenticationRequired(IfsError):
    """No usable credentials for the request.

    reason:
      - ``no_session``: no session id was given and none is stored.
      - ``token_error``: obtaining or refreshing the access token failed (``detail`` has why).
      - ``unauthorized``: IFS answered 401/403 (``status`` is set).
    """

    def __init__(self, reason: AuthReason, *, status: Optional[int] = None, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.status = status
        self.detail = detail


class IfsApiError(IfsError):
    """IFS answered with a non-2xx status other than 401/403.

    ``detail`` is the parsed JSON body when it is JSON, otherwise the response text.
    """

    def __init__(self, status: int, detail: Any, text: str) -> None:
        super().__init__(f"IFS API error {status}")
        self.status = status
        self.detail = detail
        self.text = text
