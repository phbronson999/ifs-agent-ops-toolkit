from ifs_agent_ops.analysis.har_legacy import parse_har, summarise_har  # grouped view (legacy output)


async def handler(path: str) -> str:
    try:
        parsed = parse_har(path)
        return summarise_har(parsed)
    except Exception as exc:
        return f"Failed to parse HAR file: {exc}"
