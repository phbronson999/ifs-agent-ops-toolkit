import json
from typing import Literal, Optional

from ifs_agent_ops.ifs.client import ApiCallOptions, call_protected_api as _call
from ifs_agent_ops.ifs.auth.oauth_manager import OAuthManager

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
    oauth_manager: OAuthManager,
) -> str:
    result = await _call(
        ApiCallOptions(
            endpoint=endpoint,
            method=method,
            session_id=session_id,
            body=body,
            headers=headers or {},
        ),
        oauth_manager,
    )

    if not result.success:
        return json.dumps({
            "error": result.error,
            "status": result.status,
            "message": result.message,
            "details": result.details,
        }, indent=2)

    response_json = json.dumps({"status": result.status, "data": result.data}, indent=2)

    if len(response_json) > _MAX_RESPONSE_SIZE:
        summary = _build_large_response_summary(result.data, endpoint)
        if summary:
            return json.dumps(summary, indent=2)

    return response_json
