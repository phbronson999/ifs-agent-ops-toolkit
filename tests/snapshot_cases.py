"""Characterization cases shared by the legacy capture script and the test suite.

Each case runs against a "flavor": either the legacy ``ifs_mcp_server`` package
(used once, by ``scripts/capture_legacy_snapshots.py``, to record expected output)
or the migrated ``ifs_agent_ops`` package (used by the tests). Outputs must match.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = Path(__file__).parent / "snapshots"

HAR_FIXTURE = FIXTURES / "customer_order_flow.har"
OPENAPI_FIXTURE = FIXTURES / "ShopFloorClockingsHandling.openapi.json"
EDMX_FIXTURE = FIXTURES / "CustomerOrderHandling.metadata.xml"

# Module paths per flavor.
MODULES = {
    "legacy": {
        "har": "ifs_mcp_server.lib.har_parser",
        "openapi": "ifs_mcp_server.lib.openapi_parser",
        "enum_map": "ifs_mcp_server.tools.api.get_projection_enum_map",
        "server": "ifs_mcp_server.server",
    },
    "new": {
        "har": "ifs_agent_ops.analysis.har_legacy",
        "openapi": "ifs_agent_ops.ifs.metadata.openapi",
        "enum_map": "ifs_agent_ops.mcp_server.tools.api.get_projection_enum_map",
        "server": "ifs_agent_ops.mcp_server.server",
    },
}

ENUM_MAP_CASES = {
    "enum_map_all": dict(entity_set=None, include_reference_sets=False),
    "enum_map_with_refs": dict(entity_set=None, include_reference_sets=True),
    "enum_map_entity_set": dict(entity_set="CustomerOrderSet", include_reference_sets=False),
    "enum_map_missing_set": dict(entity_set="NoSuchSet", include_reference_sets=False),
}


def _mod(flavor: str, key: str):
    return importlib.import_module(MODULES[flavor][key])


def _to_json(obj) -> str:
    return json.dumps(dataclasses.asdict(obj), indent=2, sort_keys=True)


def har_outputs(flavor: str) -> dict[str, str]:
    m = _mod(flavor, "har")
    parsed = m.parse_har(str(HAR_FIXTURE))
    return {"har_parsed.json": _to_json(parsed), "har_summary.md": m.summarise_har(parsed)}


def openapi_outputs(flavor: str) -> dict[str, str]:
    m = _mod(flavor, "openapi")
    parsed = m.parse_openapi(str(OPENAPI_FIXTURE))
    return {"openapi_parsed.json": _to_json(parsed), "openapi_summary.md": m.summarise_openapi(parsed)}


class _FakeOAuth:
    async def get_access_token(self, session_id: str) -> str:
        return "test-token"


def enum_map_outputs(flavor: str) -> dict[str, str]:
    """Run the enum-map tool handler with HTTP and session lookup stubbed out."""
    import respx
    import httpx

    m = _mod(flavor, "enum_map")
    edmx = EDMX_FIXTURE.read_text(encoding="utf-8")
    original_session = m.get_current_session_id
    original_base = m.get_api_base_url
    m.get_current_session_id = lambda: "test-session"
    m.get_api_base_url = lambda: "https://example.ifs.cloud"
    out: dict[str, str] = {}
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(
                "https://example.ifs.cloud/main/ifsapplications/projection/v1/"
                "CustomerOrderHandling.svc/$metadata"
            ).mock(return_value=httpx.Response(200, text=edmx))
            for name, kwargs in ENUM_MAP_CASES.items():
                out[f"{name}.json"] = asyncio.run(
                    m.handler("CustomerOrderHandling", oauth_manager=_FakeOAuth(), **kwargs)
                )
    finally:
        m.get_current_session_id = original_session
        m.get_api_base_url = original_base
    return out


def mcp_surface(flavor: str) -> dict[str, str]:
    """Tool and prompt definitions exposed by the server (name excluded: it changed by design)."""
    server = _mod(flavor, "server")

    async def collect():
        tools = await server.mcp.list_tools()
        prompts = await server._list_prompts()
        return tools, prompts

    tools, prompts = asyncio.run(collect())
    data = {
        "tools": sorted(
            (
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                    "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else None,
                }
                for t in tools
            ),
            key=lambda t: t["name"],
        ),
        "prompts": sorted(
            (p.model_dump(exclude_none=True) for p in prompts),
            key=lambda p: p["name"],
        ),
    }
    return {"mcp_surface.json": json.dumps(data, indent=2, sort_keys=True)}


ALL_CASES = (har_outputs, openapi_outputs, enum_map_outputs, mcp_surface)
