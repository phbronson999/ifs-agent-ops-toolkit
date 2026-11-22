"""Phase 13: the Microsoft 365 agent generated from approved capabilities, rendered per target."""
import json

import pytest
import yaml
from test_connector import pinned  # noqa: F401  (fixture chain)
from test_flows import flows_pkg  # noqa: F401
from test_operations import pkg  # noqa: F401
from test_skills import approved  # noqa: F401

from ifs_agent_ops.deploy import targets as tg
from ifs_agent_ops.deploy.agent import AgentError, render
from ifs_agent_ops.generators.agent import (MARKER, REGISTRATION_VAR, SPEC_FILE, agent_root, function_name,
                                            generate)
from ifs_agent_ops.generators.flow import flow_dir


@pytest.fixture
def agent_pkg(flows_pkg):  # noqa: F811
    gen = generate(flows_pkg)
    assert gen.ok, gen.errors
    return flows_pkg


def _read(package, name):
    return (agent_root(package) / name).read_text(encoding="utf-8")


def test_project_files_and_determinism(agent_pkg):
    root = agent_root(agent_pkg)
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert names == ["appPackage/ai-plugin.json", "appPackage/apiSpecificationFile/ifs-flows.yaml",
                     "appPackage/color.png", "appPackage/declarativeAgent.json", "appPackage/instruction.txt",
                     "appPackage/manifest.json", "appPackage/outline.png", "m365agents.yml", "report.md"]
    assert MARKER in _read(agent_pkg, "report.md")
    assert (root / "appPackage/color.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert {r.action for r in generate(agent_pkg).results} == {"unchanged"}
    assert function_name("create-order", "prepare") == "createOrderPrepare"


def test_plugin_spec_and_consequential_writes(agent_pkg):
    plugin = json.loads(_read(agent_pkg, "appPackage/ai-plugin.json"))
    assert [f["name"] for f in plugin["functions"]] == ["createOrderPrepare", "createOrderSubmit"]
    [runtime] = plugin["runtimes"]
    assert runtime["auth"] == {"type": "OAuthPluginVault", "reference_id": "${{" + REGISTRATION_VAR + "}}"}
    assert runtime["spec"]["url"] == "apiSpecificationFile/ifs-flows.yaml"
    spec = yaml.safe_load(_read(agent_pkg, SPEC_FILE))
    assert spec["servers"] == [{"url": "%%FLOW_SERVER%%"}]
    prepare = spec["paths"]["%%TRIGGER_PATH:CreateOrder_Prepare%%"]["post"]
    submit = spec["paths"]["%%TRIGGER_PATH:CreateOrder_Submit%%"]["post"]
    assert prepare["x-openai-isConsequential"] is False and submit["x-openai-isConsequential"] is True
    flow = json.loads((flow_dir(agent_pkg, "create-order") / "submit.json").read_text(encoding="utf-8"))
    assert submit["requestBody"]["content"]["application/json"]["schema"] == \
        flow["properties"]["definition"]["triggers"]["manual"]["inputs"]["schema"]
    scheme = spec["components"]["securitySchemes"]["ifsFlows"]["flows"]["authorizationCode"]
    assert scheme["scopes"] == {"https://service.flow.microsoft.com//.default": "Access Power Automate"}
    assert "%%TENANT_ID%%" in scheme["tokenUrl"]
    yml = yaml.safe_load(_read(agent_pkg, "m365agents.yml"))
    register = next(s for s in yml["provision"] if s["uses"] == "oauth/register")
    assert register["writeToEnvironmentFile"] == {"configurationId": REGISTRATION_VAR}


def test_instructions_require_confirmation_before_submit(agent_pkg):
    text = _read(agent_pkg, "appPackage/instruction.txt")
    assert "Call `createOrderPrepare`" in text
    assert "Only after the user explicitly confirms, call `createOrderSubmit`" in text
    assert "`confirmed: true`" in text and "Never invent" in text
    agent = json.loads(_read(agent_pkg, "appPackage/declarativeAgent.json"))
    assert agent["instructions"] == "$[file('instruction.txt')]" and agent["actions"][0]["file"] == "ai-plugin.json"
    manifest = json.loads(_read(agent_pkg, "appPackage/manifest.json"))
    assert manifest["id"] == "${{TEAMS_APP_ID}}" and manifest["developer"]["name"] == "${{DEVELOPER_NAME}}"


def test_refused_without_generated_flows(flows_pkg):  # noqa: F811
    (flow_dir(flows_pkg, "create-order") / "submit.json").unlink()
    gen = generate(flows_pkg)
    assert not gen.ok and "flow generate" in gen.errors[0]


@pytest.fixture
def target(agent_pkg, tmp_path, monkeypatch):
    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path / "home"))
    tg.write_target(agent_pkg.name, "dev", {"FriendlyName": "Dev", "EnvironmentIdentifier": {"Id": "env-1"},
                                            "EnvironmentUrl": "https://dev.example/"})
    t = tg.load_target(agent_pkg.name, "dev")
    tg.record_flow_triggers(t, {
        "CreateOrder_Prepare": "https://envhost.example:443/powerautomate/automations/direct/cu/20/workflows/a1/triggers"
                               "/manual/paths/invoke?api-version=1",
        "CreateOrder_Submit": "https://envhost.example:443/powerautomate/automations/direct/cu/12/workflows/b2/triggers"
                              "/manual/paths/invoke?api-version=1"})
    return tg.load_target(agent_pkg.name, "dev")


def test_render_fills_target_values_and_keeps_provisioned_ids(agent_pkg, target):
    out = render(agent_pkg, target, tenant="tenant-1")
    spec = yaml.safe_load((out.folder / SPEC_FILE).read_text(encoding="utf-8"))
    assert spec["servers"] == [{"url": "https://envhost.example"}]       # default port dropped
    assert "/powerautomate/automations/direct/cu/20/workflows/a1/triggers/manual/paths/invoke" in spec["paths"]
    assert "tenant-1" in spec["components"]["securitySchemes"]["ifsFlows"]["flows"]["authorizationCode"]["tokenUrl"]
    assert not (out.folder / "report.md").exists() and not out.provisioned
    env = out.env_file.read_text(encoding="utf-8")
    assert "TEAMSFX_ENV=dev" in env and "AGENT_SCOPE=personal" in env
    # values the Agents Toolkit writes during provision survive the next render
    out.env_file.write_text(env + f"TEAMS_APP_ID=app-1\n{REGISTRATION_VAR}=reg-1\n", encoding="utf-8")
    again = render(agent_pkg, target, tenant="tenant-1")
    text = again.env_file.read_text(encoding="utf-8")
    assert "TEAMS_APP_ID=app-1" in text and f"{REGISTRATION_VAR}=reg-1" in text and again.provisioned
    assert str(again.folder).startswith(str(tg.targets_dir(agent_pkg.name).parent.parent))


def test_render_refusals(agent_pkg, target):
    tg.record_flow_triggers(target, {"CreateOrder_Prepare": target.flow_triggers["CreateOrder_Prepare"]})
    with pytest.raises(AgentError, match="no trigger URL for CreateOrder_Submit"):
        render(agent_pkg, tg.load_target(agent_pkg.name, "dev"), tenant="t")
    tg.record_flow_triggers(target, {"CreateOrder_Prepare": "https://h/p?sig=abc",
                                     "CreateOrder_Submit": "https://h/q"})
    with pytest.raises(AgentError, match="SAS signature"):
        render(agent_pkg, tg.load_target(agent_pkg.name, "dev"), tenant="t")


def test_app_version_never_starts_with_zero():
    from ifs_agent_ops.generators.agent import app_version

    assert app_version("0.1.0") == "1.1.0" and app_version("1.2.3") == "2.2.3" and app_version("0.2") == "1.2.0"


def test_render_raises_the_app_version_when_the_package_changes(agent_pkg, target, monkeypatch):
    import ifs_agent_ops.deploy.agent as ag

    def version(out):
        return json.loads((out.folder / "appPackage/manifest.json").read_text(encoding="utf-8"))["version"]

    first = render(agent_pkg, target, tenant="t")
    assert version(first) == "1.1.0" and "APP_VERSION=1.1.0" in first.env_file.read_text(encoding="utf-8")
    assert version(render(agent_pkg, target, tenant="t")) == "1.1.0"          # nothing changed: same version
    path = agent_root(agent_pkg) / "appPackage/instruction.txt"           # a changed agent (the generator check is
    path.write_text(path.read_text(encoding="utf-8") + "One more rule.\n", encoding="utf-8")  # skipped here)
    monkeypatch.setattr(ag, "generate", lambda package, check=False: type("G", (), {"results": []})())
    assert version(render(agent_pkg, target, tenant="t")) == "1.1.1"          # content changed: patch + 1
    assert version(render(agent_pkg, target, tenant="t")) == "1.1.1"
