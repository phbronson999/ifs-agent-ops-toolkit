"""Phase 8: skills generated from approved capabilities."""
import asyncio
import json

import pytest
import yaml

from ifs_agent_ops.generators.skills import MARKER, generate, render_skill, skill_path
from ifs_agent_ops.operations import service
from ifs_agent_ops.operations.capability_store import approve_capability, caps_dir, load_capabilities
from ifs_agent_ops.operations.model import Header, RuntimeValidation
from ifs_agent_ops.operations.store import approve, load_operations, save_operation
from test_capabilities import BASE, USED
from test_operations import pkg  # noqa: F401


def _cap_file(pkg, data):  # noqa: F811
    caps_dir(pkg).mkdir(parents=True, exist_ok=True)
    (caps_dir(pkg) / "create-order.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def approved(pkg, monkeypatch):  # noqa: F811
    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    service.draft(pkg)
    query = load_operations(pkg)[USED[0]]
    save_operation(pkg, query.model_copy(update={"query": query.query.model_copy(
        update={"filter_patterns": ["Objstate eq IfsApp.OrderHandling.OrderState'{value}'"]})}))
    create = load_operations(pkg)[USED[2]]  # the real recording observed this header on the write
    save_operation(pkg, create.model_copy(update={"headers": create.headers + [
        Header(name="X-IFS-Accept-Warnings", required=False, reason="observed in recording")]}))
    for op_id in USED:
        approve(pkg, op_id, "Pat")
    data = dict(BASE, description="Create an order from a document.", deferred=["Charges"],
                runtime_validation={"status": "untested", "requirements": ["standalone line insert"]})
    data["steps"] = [dict(s) for s in BASE["steps"]]
    data["steps"][2] = dict(data["steps"][2], bindings=dict(data["steps"][2]["bindings"],
                                                            **{"header.X-IFS-Accept-Warnings": {
                                                                "fixed": "true", "note": "recorded web-client  behavior"}}))
    _cap_file(pkg, data)
    approve_capability(pkg, "create-order", "Pat")
    return pkg


def test_only_current_approved_capabilities_are_generated(pkg, monkeypatch):  # noqa: F811
    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    service.draft(pkg)
    _cap_file(pkg, BASE)
    [r] = generate(pkg, "create-order")
    assert r.action == "refused" and "only approved" in r.message and not r.path.exists()


def test_stale_approval_is_refused(approved):
    path = caps_dir(approved) / "create-order.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("Create an order from a document.", "Changed."),
                    encoding="utf-8")
    [r] = generate(approved, "create-order")
    assert r.action == "refused" and "not current" in r.message


def test_generated_skill_contract(approved):
    [r] = generate(approved, "create-order")
    assert r.action == "created" and r.path == approved.skills_dir / "create-order.md"
    text = r.path.read_text(encoding="utf-8")
    assert text.startswith("# Create an order\n\nCreate an order from a document.\n\n" + MARKER)
    for expected in ("## Information you collect", "`customer_no`", "`lines[].qty`", "## Organization settings",
                     "`site` = `S1`", "## Values you never supply", "never invent them", "## Questions to ask",
                     "## Conversation flow", "Wait for an explicit yes", "### Action `submit`",
                     "- Writes to IFS: yes", "## Out of scope (do not offer)", "- Charges",
                     "## Technical reference", "Only the curated steps", "| `create-header` | write |",
                     "## Runtime validation: untested", "standalone line insert", "not deployment-validated",
                     "## Provenance", "`OrderHandling.create.OrderSet` r1"):
        assert expected in text, expected
    # request rules come from capability bindings and recorded evidence
    assert "`create-header`: send header `X-IFS-Accept-Warnings: true`." in text
    # binding notes (how to send a value) reach the agent, on one line
    assert '`header.X-IFS-Accept-Warnings` <- `"true"` (note: recorded web-client behavior)' in text
    assert "`WantedDate` is a calendar date sent as `YYYY-MM-DDT00:00:00Z` (format observed" in text
    assert "`customer-defaults`" not in text  # nothing beyond the capability's own steps


def test_generation_is_deterministic_and_checkable(approved):
    assert generate(approved, "create-order")[0].action == "created"
    assert generate(approved, "create-order")[0].action == "unchanged"
    assert generate(approved, "create-order", check=True)[0].action == "unchanged"
    path = skill_path(approved, "create-order")
    path.write_text(path.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
    assert generate(approved, "create-order", check=True)[0].action == "would-update"
    assert "hand edit" in path.read_text(encoding="utf-8")  # --check writes nothing
    assert generate(approved, "create-order")[0].action == "updated"


def test_hand_written_skills_are_never_overwritten(approved):
    path = skill_path(approved, "create-order")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Hand written\n\nLegacy skill.\n", encoding="utf-8")
    [r] = generate(approved, "create-order")
    assert r.action == "refused" and "not generated" in r.message
    assert path.read_text(encoding="utf-8") == "# Hand written\n\nLegacy skill.\n"


def test_runtime_validated_capability_drops_the_warning(approved):
    path = caps_dir(approved) / "create-order.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["runtime_validation"]["status"] = "validated"   # set by Phase 11; outside the approval hash
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    cap = load_capabilities(approved)["create-order"]
    text = render_skill(approved, cap, load_operations(approved))
    assert "Runtime validation: **validated**." in text and "not deployment-validated" not in text


def test_cli_generate_check_and_json(approved, capsys):
    from ifs_agent_ops.cli.main import main

    assert main(["skills", "generate", "--package", "ifs-orders", "--check"]) == 1   # missing
    assert "would-create" in capsys.readouterr().out
    assert main(["skills", "generate", "create-order", "--package", "ifs-orders", "--output", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["results"][0]["action"] == "created"
    assert main(["skills", "generate", "--package", "ifs-orders", "--check"]) == 0


def test_generated_skill_is_served_and_protected(approved, monkeypatch):
    from ifs_agent_ops.mcp_server.resources.index import get_resources
    from ifs_agent_ops.mcp_server.tools.api import save_skill

    generate(approved, "create-order")
    res = {str(r.definition.uri): r.definition for r in get_resources()}
    assert res["ifs://create-order/guide"].description == "Create an order from a document."
    monkeypatch.setenv("SKILLS_DIR", str(approved.skills_dir))
    out = json.loads(asyncio.run(save_skill.handler("create-order.md", "# overwritten\n")))
    assert "generated from an approved capability" in out["error"]
    assert MARKER in skill_path(approved, "create-order").read_text(encoding="utf-8")


def test_runtime_validation_requirements_do_not_block_generation(approved):
    op = load_operations(approved)[USED[2]]
    save_operation(approved, op.model_copy(update={"runtime_validation": RuntimeValidation(requirements=["x"])}))
    assert generate(approved, "create-order")[0].action == "created"
