from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import httpx

from ifs_agent_ops.config import get_api_base_url
from ifs_agent_ops.ifs.auth.oauth_manager import OAuthManager
from ifs_agent_ops.ifs.auth.session_manager import get_current_session_id

HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH"]


@dataclass
class ApiCallOptions:
    endpoint: str
    method: HttpMethod
    session_id: Optional[str] = None
    body: Optional[Any] = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ApiResponse:
    success: bool
    status: Optional[int] = None
    data: Optional[Any] = None
    error: Optional[str] = None
    message: Optional[str] = None
    details: Optional[Any] = None


async def call_protected_api(options: ApiCallOptions, oauth_manager: OAuthManager) -> ApiResponse:
    session_id = options.session_id or get_current_session_id()

    if not session_id:
        return ApiResponse(
            success=False,
            error="authentication_required",
            message=(
                "No active session. Call the start_oauth tool now to open the login "
                "window — do not ask the user to do this manually."
            ),
        )

    try:
        access_token = await oauth_manager.get_access_token(session_id)
    except Exception as exc:
        return ApiResponse(
            success=False,
            error="authentication_required",
            message=str(exc),
        )

    base = get_api_base_url()
    url = f"{base}{options.endpoint}"
    req_headers = {**options.headers}
    req_headers.setdefault("Accept", "application/json;odata.metadata=full;IEEE754Compatible=true")
    if options.method in ("POST", "PUT", "PATCH") and options.body:
        req_headers.setdefault("Content-Type", "application/json")
    req_headers["Authorization"] = f"Bearer {access_token}"

    try:
        async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
            resp = await client.request(
                options.method,
                url,
                headers=req_headers,
                json=options.body if options.body else None,
            )

        if resp.status_code in (401, 403):
            return ApiResponse(
                success=False,
                status=resp.status_code,
                error="authentication_required",
                message=(
                    "Session expired or unauthorised. Call the start_oauth tool now to "
                    "re-authenticate — do not ask the user to do this manually."
                ),
            )

        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            data = resp.text

        return ApiResponse(success=True, status=resp.status_code, data=data)

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            return ApiResponse(
                success=False,
                status=status,
                error="authentication_required",
                message="Session expired or unauthorised. Call start_oauth to re-authenticate.",
            )
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text
        return ApiResponse(
            success=False,
            status=status,
            error="API call failed",
            message=str(detail),
            details=detail if isinstance(detail, dict) else None,
        )
    except Exception as exc:
        raise
