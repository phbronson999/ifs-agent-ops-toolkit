"""Parity tests: ifs_agent_ops must reproduce outputs captured from the legacy ifs_mcp_server.

Snapshots are produced by scripts/capture_legacy_snapshots.py. If a difference is an
intended behavior change, re-baseline deliberately and record why in the commit.
"""
import pytest

import snapshot_cases as cases


@pytest.mark.parametrize("case", cases.ALL_CASES, ids=lambda c: c.__name__)
def test_matches_legacy_snapshot(case):
    outputs = case("new")
    assert outputs, "case produced no outputs"
    for name, actual in outputs.items():
        expected = (cases.SNAPSHOTS / name).read_text(encoding="utf-8")
        assert actual == expected, f"{name} differs from legacy snapshot"


def test_tool_inventory_is_legacy_plus_declared_additions():
    import asyncio

    from ifs_agent_ops.mcp_server import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == cases.LEGACY_TOOL_NAMES | cases.ADDED_TOOL_NAMES
    prompts = [p.name for p in asyncio.run(server._list_prompts())]
    assert sorted(prompts) == sorted([
        "build_ifs_skill_from_projection", "build_ifs_skill_from_har", "build_ifs_skill_from_openapi",
    ])
