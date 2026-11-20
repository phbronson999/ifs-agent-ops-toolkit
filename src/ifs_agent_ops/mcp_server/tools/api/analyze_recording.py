from ifs_agent_ops.analysis.sequence import build_sequence, render_sequence
from ifs_agent_ops.recordings.har import load_har


async def handler(path: str, include_non_ifs: bool = False) -> str:
    try:
        views, links = build_sequence(load_har(path))
    except Exception as exc:
        return f"Failed to analyse recording: {exc}"
    return render_sequence(views, links, include_other=include_non_ifs)
