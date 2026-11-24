"""Send writes that IFS only accepts together as one $batch change set (test tool for Claude)."""
import json
import time
from typing import Optional

from ifs_agent_ops.ifs.batch import BatchRequest
from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.ifs.errors import AuthenticationRequired, IfsApiError
from ifs_agent_ops.ifs.paths import projection_path
from ifs_agent_ops.mcp_server.presenters import auth_message
from ifs_agent_ops.testlog import CallLog


def _requests(raw: list[dict]) -> list[BatchRequest]:
    out = []
    for n, r in enumerate(raw, 1):
        if not isinstance(r, dict) or not r.get("method") or not r.get("url"):
            raise ValueError(f"request {n} needs 'method' and 'url' (relative to the projection, e.g. 'PrePostSave')")
        out.append(BatchRequest(method=str(r["method"]).upper(), url=str(r["url"]), body=r.get("body"),
                                headers={str(k): str(v) for k, v in (r.get("headers") or {}).items()}))
    return out


async def handler(projection: str, requests: list[dict], session_id: Optional[str], headers: Optional[dict],
                  client: IfsClient, test_log: CallLog | None = None) -> str:
    try:
        reqs = _requests(requests)
    except ValueError as exc:
        return json.dumps({"error": "invalid_request", "message": str(exc)}, indent=2)
    endpoint = f"{projection_path(projection)}/$batch"
    logged_request = [{"method": r.method, "url": r.url, "headers": r.headers or None, "body": r.body} for r in reqs]
    started = time.monotonic()

    def log(**kw) -> str | None:
        if test_log is None:
            return None
        return test_log.record(method="POST", endpoint=endpoint, request_headers=headers,
                               request_body={"changeset": logged_request},
                               duration_ms=int((time.monotonic() - started) * 1000), **kw)

    try:
        resp, parts = await client.send_changeset(projection, reqs, headers=headers, session_id=session_id)
    except AuthenticationRequired as err:
        ref = log(status=err.status, ok=False, error="authentication_required", message=auth_message(err))
        return json.dumps({"error": "authentication_required", "status": err.status, "message": auth_message(err),
                           **({"test_log_ref": ref} if ref else {})}, indent=2)
    except IfsApiError as err:
        ref = log(status=err.status, ok=False, error="API call failed", message=str(err.detail),
                  response_body=err.detail)
        return json.dumps({"error": "API call failed", "status": err.status, "message": str(err.detail),
                           **({"test_log_ref": ref} if ref else {})}, indent=2)
    except Exception as exc:
        log(status=None, ok=False, error=type(exc).__name__, message=str(exc))
        raise

    shown = [{"content_id": p.content_id, "status": p.status, "etag": p.headers.get("etag"), "data": p.data}
             for p in parts]
    ok = bool(parts) and len(parts) == len(reqs) and all(p.ok for p in parts)
    ref = log(status=resp.status, ok=ok, response_headers=resp.headers, response_body={"parts": shown},
              error=None if ok else "a part of the change set failed")
    result = {"status": resp.status, "ok": ok, "parts": shown}
    if not ok:
        result["note"] = ("IFS rolled back the whole change set: nothing in it was saved. A failed change set is "
                          "answered with the failing part only.")
    if ref:
        result["test_log_ref"] = ref
    return json.dumps(result, indent=2)
