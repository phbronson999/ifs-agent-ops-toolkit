from ifs_agent_ops.ifs.metadata.openapi import parse_openapi, summarise_openapi


async def handler(path: str) -> str:
    try:
        parsed = parse_openapi(path)
        return summarise_openapi(parsed)
    except Exception as exc:
        return f"Failed to read OpenAPI file: {exc}"
