import json
import time
from typing import Literal, Optional

from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.mcp_server.presenters import call_api
from ifs_agent_ops.testlog import CallLog

_MAX_RESPONSE_SIZE = 100 * 1024  # 100 KB


def _build_large_response_summary(data: object, endpoint: str) -> Optional[dict]:
    if not isinstance(data, dict):
        return None
    records = data.get("value")
    if not isinstance(records, list) or not records:
        return None

    total = len(records)
    sample = records[0]
    fields = list(sample.keys()) if isinstance(sample, dict) else []

    return {
        "_instruction": (
            f"The query returned {total} records which is too large to display. "
            "Present the options below to the user in a friendly, conversational way. "
            "Don't list them robotically — weave them into a natural response. "
            "Mention the record count and a couple of interesting fields from the "
            "sample record to give context."
        ),
        "total_records": total,
        "available_fields": fields,
        "sample_record": sample,
        "options": {
            "preview": f"Show a preview: re-run with $top=N (e.g., $top=10) to see the first few records.",
            "filter": f"Narrow it down: add $filter on fields like {', '.join(fields[:3])} to find specific records.",
            "select_fields": f"Slim the columns: use $select to return only the fields they care about (e.g., $select={','.join(fields[:3])}).",
            "export": f"Save it all: use export_api_data to download all {total} records as a CSV file to their Downloads folder.",
        },
        "endpoint_used": endpoint,
    }


async def handler(
    endpoint: str,
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"],
    session_id: Optional[str],
    body: Optional[dict],
    headers: Optional[dict],
    client: IfsClient,
    test_log: CallLog | None = None,
) -> str:
    started = time.monotonic()
    try:
        result = await call_api(client, endpoint, method, session_id=session_id, body=body, headers=headers or {})
    except Exception as exc:  # transport failures propagate unchanged, but are logged first
        if test_log is not None:
            test_log.record(method=method, endpoint=endpoint, request_headers=headers, request_body=body,
                            status=None, ok=False, error=type(exc).__name__, message=str(exc),
                            duration_ms=int((time.monotonic() - started) * 1000))
        raise

    # With a test log, every result carries the entry's reference; without one the output is unchanged.
    ref = None
    if test_log is not None:
        ref = test_log.record(
            method=method, endpoint=endpoint, request_headers=headers, request_body=body,
            status=result.status, ok=result.success, response_headers=result.headers,
            response_body=result.data if result.success else (result.details or result.message),
            error=result.error, message=result.message if not result.success else None,
            duration_ms=int((time.monotonic() - started) * 1000))
    tail = {"test_log_ref": ref} if ref else {}

    if not result.success:
        return json.dumps({
            "error": result.error,
            "status": result.status,
            "message": result.message,
            "details": result.details,
            **tail,
        }, indent=2)

    response_json = json.dumps({"status": result.status, "data": result.data, **tail}, indent=2)

    if len(response_json) > _MAX_RESPONSE_SIZE:
        summary = _build_large_response_summary(result.data, endpoint)
        if summary:
            return json.dumps({**summary, **tail}, indent=2)

    return response_json
