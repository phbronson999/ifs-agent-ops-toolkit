import io
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

from ifs_agent_ops.ifs.client import ApiCallOptions, call_protected_api
from ifs_agent_ops.ifs.auth.oauth_manager import OAuthManager

_BATCH_SIZE = 100
_DELIMITER = ";"


def _escape_csv(value: object) -> str:
    if value is None:
        return ""
    s = str(value)
    if _DELIMITER in s or '"' in s or "\n" in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def _build_csv(records: list[dict]) -> str:
    if not records:
        return ""
    headers = list(records[0].keys())
    lines = [_DELIMITER.join(_escape_csv(h) for h in headers)]
    for rec in records:
        lines.append(_DELIMITER.join(_escape_csv(rec.get(h)) for h in headers))
    return "\n".join(lines)


def _strip_pagination(endpoint: str) -> str:
    if "?" not in endpoint:
        return endpoint
    base, qs = endpoint.split("?", 1)
    params = parse_qs(qs, keep_blank_values=True)
    params.pop("$top", None)
    params.pop("$skip", None)
    remaining = "&".join(
        f"{k}={v}" for k, vs in params.items() for v in vs
    )
    return f"{base}?{remaining}" if remaining else base


def _append_param(endpoint: str, param: str) -> str:
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}{param}"


async def handler(
    endpoint: str,
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"],
    filename: Optional[str],
    session_id: Optional[str],
    body: Optional[dict],
    oauth_manager: OAuthManager,
) -> str:
    output_filename = filename or "export"

    export_dir_env = os.getenv("IFS_MCP_EXPORT_DIR")
    export_dir = Path(export_dir_env) if export_dir_env else (Path.home() / ".ifs-mcp" / "exports")
    export_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    output_path = export_dir / f"{output_filename}_{timestamp}.csv"

    base_endpoint = _strip_pagination(endpoint)
    all_records: list[dict] = []
    skip = 0
    batch_count = 0

    try:
        while True:
            batch_ep = _append_param(base_endpoint, f"$top={_BATCH_SIZE}&$skip={skip}")
            result = await call_protected_api(
                ApiCallOptions(endpoint=batch_ep, method=method, session_id=session_id, body=body),
                oauth_manager,
            )

            if not result.success:
                return json.dumps({
                    "error": "API call failed during export",
                    "batch": batch_count + 1,
                    "records_exported_so_far": len(all_records),
                    "details": result.message or result.error,
                }, indent=2)

            records = (result.data or {}).get("value") if isinstance(result.data, dict) else None
            if not isinstance(records, list) or not records:
                break

            all_records.extend(records)
            batch_count += 1
            skip += _BATCH_SIZE

            if len(records) < _BATCH_SIZE:
                break

        if not all_records:
            return json.dumps({"message": "No records found to export."}, indent=2)

        csv_content = _build_csv(all_records)
        bom = "﻿"
        output_path.write_text(bom + csv_content, encoding="utf-8")

        return json.dumps({
            "success": True,
            "total_records": len(all_records),
            "batches_fetched": batch_count,
            "file": str(output_path),
            "fields": list(all_records[0].keys()),
        }, indent=2)

    except Exception as exc:
        return json.dumps({
            "error": "Export failed",
            "message": str(exc),
            "records_exported_so_far": len(all_records),
        }, indent=2)
