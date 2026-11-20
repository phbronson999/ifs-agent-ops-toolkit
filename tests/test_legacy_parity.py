"""Parity tests: ifs_agent_ops must reproduce outputs captured from the legacy ifs_mcp_server.

Snapshots are produced by scripts/capture_legacy_snapshots.py. If a difference is an
intended behaviour change, re-baseline deliberately and record why in the commit.
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


def test_tool_inventory_unchanged():
    import json

    surface = json.loads(cases.mcp_surface("new")["mcp_surface.json"])
    assert [t["name"] for t in surface["tools"]] == sorted([
        "start_oauth", "get_session_info", "call_protected_api", "get_api_guide",
        "export_api_data", "import_skill", "save_skill", "save_to_scratch",
        "parse_har_file", "read_openapi_file", "get_projection_enum_map",
    ])
    assert [p["name"] for p in surface["prompts"]] == sorted([
        "build_ifs_skill_from_projection", "build_ifs_skill_from_har", "build_ifs_skill_from_openapi",
    ])
