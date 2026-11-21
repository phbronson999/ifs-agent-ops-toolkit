"""LLM-facing wording for MCP tool results.

The IFS client raises typed errors carrying facts; this module turns them into the exact
messages the MCP tools have always returned (including the instructions to the model to
call start_oauth itself). Keeping the text here keeps it out of the reusable core.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.ifs.errors import AuthenticationRequired, IfsApiError

NO_SESSION = (
    "No active session. Call the start_oauth tool now to open the login "
    "window — do not ask the user to do this manually."
)
UNAUTHORIZED = (
    "Session expired or unauthorized. Call the start_oauth tool now to "
    "re-authenticate — do not ask the user to do this manually."
)
UNAUTHORIZED_SHORT = "Session expired or unauthorized. Call start_oauth to re-authenticate."

HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH"]


def auth_message(err: AuthenticationRequired, *, unauthorized: str = UNAUTHORIZED) -> str:
    if err.reason == "no_session":
        return NO_SESSION
    if err.reason == "token_error":
        return err.detail
    return unauthorized


@dataclass
class ApiResponse:
    """Result shape used by call_protected_api / export_api_data (unchanged from legacy)."""
    success: bool
    status: Optional[int] = None
    data: Optional[Any] = None
    error: Optional[str] = None
    message: Optional[str] = None
    details: Optional[Any] = None


async def call_api(
    client: IfsClient,
    endpoint: str,
    method: HttpMethod,
    *,
    session_id: Optional[str] = None,
    body: Any = None,
    headers: Optional[dict] = None,
) -> ApiResponse:
    """Call IFS and fold typed errors into an ApiResponse. Transport errors propagate."""
    try:
        resp = await client.request(method, endpoint, body=body, headers=headers, session_id=session_id)
    except AuthenticationRequired as err:
        return ApiResponse(success=False, status=err.status, error="authentication_required",
                           message=auth_message(err))
    except IfsApiError as err:
        return ApiResponse(success=False, status=err.status, error="API call failed",
                           message=str(err.detail),
                           details=err.detail if isinstance(err.detail, dict) else None)
    return ApiResponse(success=True, status=resp.status, data=resp.data)
