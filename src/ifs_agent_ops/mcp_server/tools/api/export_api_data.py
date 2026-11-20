import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.ifs.errors import AuthenticationRequired, IfsApiError
from ifs_agent_ops.mcp_server.presenters import auth_message

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



async def handler(
    endpoint: str,
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"],
    filename: Optional[str],
    session_id: Optional[str],
    body: Optional[dict],
    client: IfsClient,
) -> str:
    output_filename = filename or "export"

    export_dir_env = os.getenv("IFS_MCP_EXPORT_DIR")
    export_dir = Path(export_dir_env) if export_dir_env else (Path.home() / ".ifs-mcp" / "exports")
    export_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    output_path = export_dir / f"{output_filename}_{timestamp}.csv"

    all_records: list[dict] = []
    batch_count = 0

    try:
        try:
            async for records in client.paginate(
                endpoint, method, body=body, session_id=session_id, page_size=_BATCH_SIZE
            ):
                all_records.extend(records)
                batch_count += 1
        except (AuthenticationRequired, IfsApiError) as err:
            if isinstance(err, AuthenticationRequired):
                details = auth_message(err) or "authentication_required"
            else:
                details = str(err.detail) or "API call failed"
            return json.dumps({
                "error": "API call failed during export",
                "batch": batch_count + 1,
                "records_exported_so_far": len(all_records),
                "details": details,
            }, indent=2)

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
