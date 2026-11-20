#!/usr/bin/env python3
"""IFS Agent Ops Toolkit MCP server — Streaming HTTP transport entry point."""
from pathlib import Path

# Load .env relative to the project root (not cwd) — mirrors index.ts behavior.
# Must run before any config imports so env vars are populated.
from dotenv import load_dotenv
# src/ifs_agent_ops/mcp_server/server.py -> repo root is parents[3].
load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

from typing import Literal, Optional

import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP

from ifs_agent_ops.config import get_mcp_server_port
from ifs_agent_ops.ifs.auth.oauth_manager import oauth_manager
from ifs_agent_ops.ifs.auth.session_manager import initialize_token_store
from ifs_agent_ops.mcp_server.resources.index import get_resources
from ifs_agent_ops.mcp_server.prompts.build_ifs_skill_guide import (
    projection_definition, projection_handler,
    har_definition, har_handler,
    openapi_definition, openapi_handler,
)

# ── Boot ─────────────────────────────────────────────────────────────────────

initialize_token_store()

# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP("ifs-agent-ops", host="127.0.0.1", port=get_mcp_server_port())

# ── Dynamic resources (hot-reload: scanned fresh on every request) ────────────
# Access the underlying low-level Server to register custom resource handlers.
_server = mcp._mcp_server  # type: ignore[attr-defined]


@_server.list_resources()
async def _list_resources() -> list[mcp_types.Resource]:
    return [r.definition for r in get_resources()]


@_server.read_resource()
async def _read_resource(uri) -> list[mcp_types.ResourceContents]:
    uri_str = str(uri)
    resources = get_resources()
    resource = next((r for r in resources if r.definition.uri == uri_str), None)
    if not resource:
        raise ValueError(f"Unknown resource: {uri_str}")
    text = Path(resource.path).read_text(encoding="utf-8")
    return [mcp_types.TextResourceContents(uri=uri_str, mimeType="text/markdown", text=text)]


# ── Prompts ───────────────────────────────────────────────────────────────────

@_server.list_prompts()
async def _list_prompts() -> list[mcp_types.Prompt]:
    return [projection_definition, har_definition, openapi_definition]


@_server.get_prompt()
async def _get_prompt(name: str, arguments: dict[str, str] | None) -> mcp_types.GetPromptResult:
    _handlers = {
        "build_ifs_skill_from_projection": projection_handler,
        "build_ifs_skill_from_har": har_handler,
        "build_ifs_skill_from_openapi": openapi_handler,
    }
    fn = _handlers.get(name)
    if not fn:
        raise ValueError(f"Unknown prompt: {name}")
    return await fn(arguments or {})


# ── Auth tools ────────────────────────────────────────────────────────────────

@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def start_oauth(force: bool = False) -> str:
    """Start OAuth 2.0 authentication flow — opens a login window in the browser. Call this automatically whenever an API tool returns an authentication_required error or session expiry. Do not ask the user to trigger this manually."""
    from ifs_agent_ops.mcp_server.tools.auth.start_oauth import handler
    return await handler(force, oauth_manager)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def get_session_info() -> str:
    """Get information about the current saved session, including session ID and expiration status."""
    from ifs_agent_ops.mcp_server.tools.auth.get_session_info import handler
    return await handler()


# ── API tools ─────────────────────────────────────────────────────────────────

@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def call_protected_api(
    endpoint: str,
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"],
    session_id: Optional[str] = None,
    body: Optional[dict] = None,
    headers: Optional[dict] = None,
) -> str:
    """Call a protected IFS Cloud API endpoint using an authenticated session. IFS Cloud projection names, endpoint paths, key fields, and OData filter syntax are non-standard and vary per customer configuration. Guessing them will produce 404 errors or silently return wrong data. You MUST call get_api_guide first to get the correct endpoint and query pattern for the workflow — do not skip this step even if you think you know the endpoint. Do NOT call $metadata, probe unknown paths, or construct endpoints from scratch. If get_api_guide returns no matching guide, stop and tell the user: 'I don't have a skill for that workflow yet. Please perform this action in IFS Cloud in your browser, then export a HAR file from DevTools (Network tab → right-click → Save all as HAR with content) and use the build_ifs_skill_guide prompt to create a skill for it.' If the response contains error 'authentication_required', immediately call start_oauth — do not relay the error to the user or ask them to authenticate manually. If no session_id is provided, uses the most recent saved session. Use headers to pass If-Match (ETag) for PATCH/DELETE/bound actions."""
    from ifs_agent_ops.mcp_server.tools.api.call_protected_api import handler
    return await handler(endpoint, method, session_id, body, headers, oauth_manager)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def get_api_guide(guide: Optional[str] = None) -> str:
    """Look up the skill guide for an IFS workflow before querying the API. IFS Cloud endpoint paths, projection names, key fields, and OData filter syntax are customer-specific and non-standard — they cannot be inferred from general OData or REST knowledge. Call this first with a keyword (e.g. 'customer', 'sales', 'parts') to get the correct endpoints and query patterns. Call without arguments to list all available guides."""
    from ifs_agent_ops.mcp_server.tools.api.get_api_guide import handler
    return await handler(guide)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
async def export_api_data(
    endpoint: str,
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"],
    filename: Optional[str] = None,
    session_id: Optional[str] = None,
    body: Optional[dict] = None,
) -> str:
    """Export large API result sets to a CSV file. Fetches data in batches of 100 records using $top/$skip pagination and saves to the path set in IFS_MCP_EXPORT_DIR env var, otherwise ~/.ifs-mcp/exports/. Use this when call_protected_api returns a 'Response too large' warning."""
    from ifs_agent_ops.mcp_server.tools.api.export_api_data import handler
    return await handler(endpoint, method, filename, session_id, body, oauth_manager)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def import_skill(source: str, filename: Optional[str] = None) -> str:
    """Import an IFS resource guide (skill) from an HTTPS URL or local file path and make it available immediately. Useful for sharing skills between users — share the .md file and others can import it with this tool. GitHub raw URLs work well: https://raw.githubusercontent.com/user/repo/main/skills/my-skill.md. The skill is available immediately after import — no restart needed."""
    from ifs_agent_ops.mcp_server.tools.api.import_skill import handler
    return await handler(source, filename)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
async def save_skill(filename: str, content: str) -> str:
    """Save an IFS resource guide (skill) to the skills library. For new skills, writes immediately. For existing skills, writes the update and returns a summary of what changed. Call this after drafting a skill with build_ifs_skill_guide. Filename must end in .md and follow the convention: ifs-[module]-[area].md."""
    from ifs_agent_ops.mcp_server.tools.api.save_skill import handler
    return await handler(filename, content)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def save_to_scratch(filename: str, content: str) -> str:
    """Save content to the project scratch directory (IFS_MCP_SCRATCH_DIR env var, otherwise ~/.ifs-mcp/scratch/). Use this to persist large API responses, downloaded OpenAPI specs, or any temp file you want in a known local location. Returns the full path of the saved file."""
    from ifs_agent_ops.mcp_server.tools.api.save_to_scratch import handler
    return await handler(filename, content)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def parse_har_file(path: str) -> str:
    """Read and summarise a browser HAR file for skill authoring. Returns a structured summary of the IFS API calls found in the recording. Provide the absolute path to the .har file exported from browser DevTools."""
    from ifs_agent_ops.mcp_server.tools.api.parse_har_file import handler
    return await handler(path)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def read_openapi_file(path: str) -> str:
    """Read and summarise a downloaded OpenAPI/Swagger JSON file for skill authoring. Returns a structured summary of the entity sets, methods, and properties found in the spec. Provide the absolute path to the downloaded JSON file."""
    from ifs_agent_ops.mcp_server.tools.api.read_openapi_file import handler
    return await handler(path)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
async def get_projection_enum_map(
    projection_name: str,
    entity_set: Optional[str] = None,
    include_reference_sets: bool = False,
) -> str:
    """Parse a projection's $metadata XML and return resolved enum definitions for its entity sets. Use this when authoring $filter expressions for enum fields — IFS Cloud requires the fully qualified enum type name (e.g. IfsApp.CustomerOrderHandling.CustomerOrderState'Released'). Pass projection_name (e.g. 'CustomerOrderHandling'). Pass entity_set to filter to a single entity set and keep the response small (recommended). Reference_ sets are excluded by default; set include_reference_sets=true to include them."""
    from ifs_agent_ops.mcp_server.tools.api.get_projection_enum_map import handler
    return await handler(projection_name, entity_set, include_reference_sets, oauth_manager)


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    port = get_mcp_server_port()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
