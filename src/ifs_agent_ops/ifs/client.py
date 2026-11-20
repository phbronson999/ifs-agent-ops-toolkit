"""Authenticated HTTP access to IFS Cloud projections.

``IfsClient`` returns data on success and raises typed errors (``ifs_agent_ops.ifs.errors``)
otherwise. Transport failures (``httpx.TransportError``) propagate unchanged.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import httpx

from ifs_agent_ops.ifs.auth.base import TokenProvider
from ifs_agent_ops.ifs.errors import AuthenticationRequired, IfsApiError
from ifs_agent_ops.ifs.paths import append_param, strip_pagination
from ifs_agent_ops.ifs.tls import VerifyType

DEFAULT_ACCEPT = "application/json;odata.metadata=full;IEEE754Compatible=true"
_BODY_METHODS = ("POST", "PUT", "PATCH")


@dataclass
class IfsResponse:
    status: int
    data: Any  # parsed JSON, or the response text when it is not JSON
    text: str


class IfsClient:
    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        *,
        verify: VerifyType = True,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = token_provider
        self.verify = verify
        self.timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None
        self._http_loop: Optional[asyncio.AbstractEventLoop] = None

    # ── connection reuse ────────────────────────────────────────────────────
    def _client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._http is None or self._http_loop is not loop or self._http.is_closed:
            # A client is bound to the loop it was first used on; make a new one per loop.
            self._http = httpx.AsyncClient(timeout=self.timeout, verify=self.verify)
            self._http_loop = loop
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    # ── requests ────────────────────────────────────────────────────────────
    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        body: Any = None,
        headers: Optional[dict[str, str]] = None,
        session_id: Optional[str] = None,
    ) -> IfsResponse:
        session_id = session_id or self.tokens.current_session_id()
        if not session_id:
            raise AuthenticationRequired("no_session")
        try:
            access_token = await self.tokens.get_access_token(session_id)
        except Exception as exc:
            raise AuthenticationRequired("token_error", detail=str(exc)) from exc

        req_headers = {**(headers or {})}
        req_headers.setdefault("Accept", DEFAULT_ACCEPT)
        if method in _BODY_METHODS and body:
            req_headers.setdefault("Content-Type", "application/json")
        req_headers["Authorization"] = f"Bearer {access_token}"

        resp = await self._client().request(
            method,
            f"{self.base_url}{endpoint}",
            headers=req_headers,
            json=body if body else None,
        )

        if resp.status_code in (401, 403):
            raise AuthenticationRequired("unauthorized", status=resp.status_code)
        if not resp.is_success:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise IfsApiError(resp.status_code, detail, resp.text)

        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return IfsResponse(status=resp.status_code, data=data, text=resp.text)

    async def get_text(self, endpoint: str, *, accept: str, session_id: Optional[str] = None) -> str:
        resp = await self.request("GET", endpoint, headers={"Accept": accept}, session_id=session_id)
        return resp.text

    async def paginate(
        self,
        endpoint: str,
        method: str = "GET",
        *,
        body: Any = None,
        session_id: Optional[str] = None,
        page_size: int = 100,
    ) -> AsyncIterator[list]:
        """Yield successive non-empty ``value`` pages using $top/$skip.

        Any existing $top/$skip on the endpoint is replaced. Stops on an empty page, a
        short page, or a response without a ``value`` list.
        """
        base = strip_pagination(endpoint)
        skip = 0
        while True:
            page_ep = append_param(base, f"$top={page_size}&$skip={skip}")
            resp = await self.request(method, page_ep, body=body, session_id=session_id)
            records = resp.data.get("value") if isinstance(resp.data, dict) else None
            if not isinstance(records, list) or not records:
                return
            yield records
            if len(records) < page_size:
                return
            skip += page_size
