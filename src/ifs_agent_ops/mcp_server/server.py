#!/usr/bin/env python3
"""IFS Agent Ops Toolkit MCP server — Streaming HTTP transport entry point."""
from pathlib import Path

# Load the repo-root .env before any config is read (shared with the CLI).
from ifs_agent_ops.env import load_env
load_env()

from typing import Literal, Optional

import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP

from ifs_agent_ops.config import get_mcp_server_port
from ifs_agent_ops.mcp_server.runtime import Runtime
from ifs_agent_ops.profiles import ProfileError
from ifs_agent_ops.mcp_server.resources.index import get_resources
from ifs_agent_ops.mcp_server.prompts.build_ifs_skill_guide import (
    projection_definition, projection_handler,
    har_definition, har_handler,
    openapi_definition, openapi_handler,
)

# ── Boot ─────────────────────────────────────────────────────────────────────

# One server process serves one IFS profile. main() builds the runtime for --profile /
# IFS_PROFILE before serving; rt() builds it lazily when the module is used without main().
_runtime: Optional[Runtime] = None


def rt() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime.for_profile()
    return _runtime

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
    return await handler(force, rt().auth, rt().store)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def get_session_info() -> str:
    """Get information about the current saved session, including session ID and expiration status."""
    from ifs_agent_ops.mcp_server.tools.auth.get_session_info import handler
    return await handler(rt().store)


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
    return await handler(endpoint, method, session_id, body, headers, rt().client, rt().test_log)


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
    return await handler(endpoint, method, filename, session_id, body, rt().client)


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
    """Read and summarize a browser HAR file for skill authoring. Returns a structured summary of the IFS API calls found in the recording. Provide the absolute path to the .har file exported from browser DevTools."""
    from ifs_agent_ops.mcp_server.tools.api.parse_har_file import handler
    return await handler(path)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def analyze_recording(path: str, include_non_ifs: bool = False) -> str:
    """Analyze a browser HAR recording of an IFS workflow in recorded order. Unlike parse_har_file (which groups and deduplicates), this keeps every call in sequence, including failed calls, $batch and $metadata, unwraps IFS server-trace responses, lists IFS-relevant request headers (If-Match, Prefer, X-IFS-Accept-Warnings), and shows data flow: which earlier response field supplied each key, body value or If-Match ETag a later call sent. Reports field names and call numbers only, never data values. Provide the absolute path to the .har file."""
    from ifs_agent_ops.mcp_server.tools.api.analyze_recording import handler
    return await handler(path, include_non_ifs)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def draft_operations(package: str) -> str:
    """Draft canonical IFS operations and workflows for an operation package from its sanitized recordings and pinned metadata snapshots. Writes operations/*.yaml and workflows/*.yaml in the package. Identities are stable: re-drafting updates draft operations in place, keeps connector operation ids, and never rewrites approved operations (differences are reported as drift). Pass the package name (from IFS_AGENT_OPS_PACKAGES) or its path."""
    from ifs_agent_ops.mcp_server.tools.operations import draft
    return await draft(package)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def list_operations(package: str) -> str:
    """List the canonical operations (status, kind, method, id) and workflows of an operation package."""
    from ifs_agent_ops.mcp_server.tools.operations import list_operations as handler
    return await handler(package)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def get_operation(package: str, operation_id: str) -> str:
    """Show one canonical operation as YAML: IFS path template, path/body parameters with IFS types and enums, headers (e.g. If-Match), response shape, dependencies, evidence, plus its validation findings. operation_id may be a unique part of the id."""
    from ifs_agent_ops.mcp_server.tools.operations import get_operation as handler
    return await handler(package, operation_id)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def get_validation_status(package: str, capability: str) -> str:
    """Show what testing against IFS has shown so far for a capability: its numbered runtime-validation requirements, each PASSED / FAILED / INCONCLUSIVE / NOT TESTED with the latest finding and the test_log_ref of the calls behind it. Call this before testing a capability to see what still needs testing, and to get the requirement numbers for record_validation_finding. Pass the package name (from IFS_AGENT_OPS_PACKAGES) or its path."""
    from ifs_agent_ops.mcp_server.tools.validation import get_status
    return await get_status(package, capability)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def record_validation_finding(package: str, capability: str, requirement: int, result: Literal["passed", "failed", "inconclusive"], summary: str, test_log_refs: list[str] | None = None) -> str:
    """Record what a test against IFS showed for one runtime-validation requirement of a capability (numbered as in get_validation_status). Record right after testing, one finding per requirement you tested: 'passed' when the calls show the requirement holds, 'failed' when they show it does not (say what IFS did instead), 'inconclusive' when the test could not decide it. test_log_refs are the test_log_ref values call_protected_api returned for the calls that show it (required for passed and failed). The summary is committed with the package, so write it for a reviewer in plain language and name only the test records needed (for example the order number); never paste raw responses, tokens or personal data. Findings are a report: they never change the capability's runtime_validation status or its approval."""
    from ifs_agent_ops.mcp_server.tools.validation import record
    try:
        log = getattr(rt(), "test_log", None)
    except ProfileError:  # no profile configured: refs cannot be checked against a log
        log = None
    return await record(package, capability, requirement, result, summary, test_log_refs, log)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
async def doctor(capabilities: Optional[list[str]] = None, offline: bool = False) -> str:
    """Check this machine's readiness for toolkit work, per capability: toolkit, ifs, packages (default), power-platform, agents, spfx. Read-only: reports PASS / WARN / FAIL / NOT REQUIRED with a concrete fix for each problem; never installs, signs in or changes anything."""
    import asyncio

    from ifs_agent_ops import doctor as doc
    results = await asyncio.to_thread(doc.run, capabilities or None, online=not offline)
    return doc.render(results)


@mcp.tool(annotations=mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def read_openapi_file(path: str) -> str:
    """Read and summarize a downloaded OpenAPI/Swagger JSON file for skill authoring. Returns a structured summary of the entity sets, methods, and properties found in the spec. Provide the absolute path to the downloaded JSON file."""
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
    return await handler(projection_name, entity_set, include_reference_sets, rt().client)


# ── Guided capture (intent -> recording plan -> task-bucketed browser recording) ──

from ifs_agent_ops.capture import api as _capture_api  # noqa: E402
from ifs_agent_ops.mcp_server.tools import capture as _capture  # noqa: E402

# The browser extension streams events to /capture/v1/... on this server (same host and port as /mcp).
mcp.custom_route(_capture_api.ROUTE, methods=_capture_api.METHODS, include_in_schema=False)(_capture_api.handle)

_LOCAL_WRITE = mcp_types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_LOCAL_READ = mcp_types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


@mcp.tool(annotations=_LOCAL_WRITE)
async def capture_create_session(intent: str, tasks: list[str], workflow: str = "", package: str = "",
                                 optional_tasks: Optional[list[str]] = None) -> str:
    """Start teaching the builder a capability: create a guided-capture session from the user's intent (e.g. 'I want to teach the builder how to create a customer order') and an ordered recording plan of business tasks the user will demonstrate in the IFS web UI (e.g. ['Create order header', 'Add order line', 'Add charge', 'Finish order']). This is the normal beginning of capability development; prefer it to asking for a DevTools HAR export. Agree the plan with the user BEFORE calling this: propose the tasks from the intent and let the user change, add, remove or reorder them. optional_tasks lists the titles of tasks the user says the business action can do without (e.g. ['Add charge']); optional tasks never make the plan incomplete. workflow is the package recordings folder (e.g. 'create-customer-order'); package is optional. Nothing is recorded yet: the user starts recording from the IFS Agent Ops browser extension's side panel. Sessions are local (toolkit home) and never committed."""
    return await _capture.create_session(intent, tasks, workflow, package, optional_tasks)


@mcp.tool(annotations=_LOCAL_WRITE)
async def capture_set_plan(session_id: str, tasks: list[str], intent: str = "",
                           optional_tasks: Optional[list[str]] = None) -> str:
    """Replace the ordered recording plan of a session that has not started recording (tasks keep their ids when the title is unchanged), optionally changing the intent. Use it when the user edits or reorders the plan before recording. optional_tasks, when given, is the complete list of task titles to mark optional (the rest become required); omit it to keep each task's current optional flag. While recording, use capture_add_task or capture_skip_task instead."""
    return await _capture.set_plan(session_id, tasks, intent, optional_tasks)


@mcp.tool(annotations=_LOCAL_READ)
async def capture_status(session_id: str = "") -> str:
    """Show a guided-capture session: intent, ordered plan with each task's status (pending/active/complete/skipped/incomplete), the current task, progress, recorded IFS requests per task, whether the plan is complete, and the next step. Without session_id, the session currently recording (or being planned) is shown."""
    return await _capture.status(session_id)


@mcp.tool(annotations=_LOCAL_READ)
async def capture_list_sessions() -> str:
    """List local guided-capture sessions with their intent, status, progress and the package recordings imported from them."""
    return await _capture.list_sessions()


@mcp.tool(annotations=_LOCAL_WRITE)
async def capture_complete_task(session_id: str = "", task_id: str = "") -> str:
    """Mark the current recording-plan task as complete when the user says they finished demonstrating it. The calls recorded since the task became current are attributed to it, and the next pending task becomes current. Pass task_id to guard against completing a different task than the one the user means."""
    return await _capture.complete_task(session_id, task_id)


@mcp.tool(annotations=_LOCAL_WRITE)
async def capture_skip_task(session_id: str = "", task_id: str = "") -> str:
    """Skip the current recording-plan task (e.g. the order needs no charge). Calls already recorded during it are kept and attributed to it; the next pending task becomes current."""
    return await _capture.skip_task(session_id, task_id)


@mcp.tool(annotations=_LOCAL_WRITE)
async def capture_add_task(title: str, session_id: str = "", description: str = "", position: str = "",
                           optional: bool = False) -> str:
    """Add a task to the recording plan, also during recording when the user realizes another business action needs to be demonstrated. While recording it goes right after the current task by default; position may be 'end' or a 1-based number. Set optional when the business action can be done without this step. Tasks added during recording are marked as such."""
    return await _capture.add_task(title, session_id, description, position, optional)


@mcp.tool(annotations=_LOCAL_WRITE)
async def capture_stop(session_id: str = "") -> str:
    """Stop a guided recording. The session, task boundaries and every recorded event are kept; a task still current becomes 'incomplete', and the plan is reported as complete or incomplete. The browser extension detaches from the IFS tab when it sees the stop."""
    return await _capture.stop(session_id)


@mcp.tool(annotations=_LOCAL_WRITE)
async def capture_import(session_id: str, package: str, workflow: str = "", name: str = "",
                         redact_values: bool = True) -> str:
    """Import a stopped guided recording into an operation package through the record-import boundary (sanitize, host normalization, value redaction, verification). The recording plan (intent, tasks, statuses, call ranges) is written into the recording's annotations, so analyze_recording groups calls by task and draft_operations tags observed workflow steps with their task. Only the sanitized recording enters the package; the raw capture stays local."""
    return await _capture.import_session(session_id, package, workflow, name, redact_values)


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="ifs-agent-ops-mcp", description="IFS Agent Ops MCP server")
    parser.add_argument("--profile", help="IFS profile from profiles.toml (default: IFS_PROFILE, "
                                          "default_profile, or the .env-based 'default' profile)")
    args = parser.parse_args(argv)

    global _runtime
    try:
        _runtime = Runtime.for_profile(args.profile)
    except ProfileError as exc:
        parser.exit(2, f"ifs-agent-ops-mcp: {exc}\n")

    mcp.settings.port = _runtime.mcp_server_port or get_mcp_server_port()
    print(
        f"ifs-agent-ops-mcp: profile '{_runtime.profile}' -> {_runtime.settings.base_url or '(no base URL)'} "
        f"| session {_runtime.store.path} | http://{mcp.settings.host}:{mcp.settings.port}/mcp",
        file=sys.stderr,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
