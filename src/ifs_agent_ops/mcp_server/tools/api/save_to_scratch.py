import json
from pathlib import Path

from ifs_agent_ops.config import get_scratch_dir


async def handler(filename: str, content: str) -> str:
    scratch_env = get_scratch_dir()
    scratch_dir = Path(scratch_env) if scratch_env else (Path.home() / ".ifs-mcp" / "scratch")
    scratch_dir.mkdir(parents=True, exist_ok=True)

    dest = (scratch_dir / filename).resolve()
    if not str(dest).startswith(str(scratch_dir.resolve())):
        return json.dumps({"error": "Invalid filename — path traversal not allowed."})

    dest.write_text(content, encoding="utf-8")
    return json.dumps({"success": True, "file": str(dest), "bytes": len(content.encode("utf-8"))})
