"""bootstrap: per-capability install of missing local tools, each step confirmed."""
import os

import pytest

from ifs_agent_ops import bootstrap
from ifs_agent_ops.cli.main import main


class FakeMachine:
    """Two PATHs: the current terminal's ("CUR") and the registry's, which a new terminal would get ("REG")."""

    def __init__(self, monkeypatch, current=(), registry=(), fail=(), winget=True):
        self.current = set(current) | ({"winget"} if winget else set())
        self.registry = set(registry) | self.current
        self.fail = set(fail)
        self.calls: list[list[str]] = []
        monkeypatch.setenv("PATH", "CUR")
        monkeypatch.setattr(bootstrap, "_which", self.which)
        monkeypatch.setattr(bootstrap, "_registry_path", lambda: "REG")
        monkeypatch.setattr(bootstrap, "_call", self.call)
        monkeypatch.setattr(bootstrap, "_quiet", lambda cmd, path=None: self.which(cmd[0], path) is not None)

    def which(self, cmd, path=None):
        dirs = (path or "").split(os.pathsep)
        found = ("CUR" in dirs and cmd in self.current) or ("REG" in dirs and cmd in self.registry)
        return f"/bin/{cmd}" if found else None

    def call(self, cmd, path=None):
        self.calls.append(cmd)
        if cmd[0] == "winget":
            tool = next(t for t in bootstrap.TOOLS if t.winget_id == cmd[cmd.index("--id") + 1])
        else:
            tool = next(t for t in bootstrap.TOOLS if t.npm and t.npm[-1] == cmd[-1])
        if tool.key in self.fail:
            return 1
        self.registry.update(tool.probe)
        if cmd[0] == "npm" and "npm" in self.current:  # npm installs land next to node
            self.current.update(tool.probe)
        return 0


def status(outcomes):
    return {o.tool.key: o.status for o in outcomes}


def test_capability_selects_tools():
    assert [t.key for t in bootstrap.tools_for(["toolkit"])] == ["git"]
    assert [t.key for t in bootstrap.tools_for(["power-platform"])] == ["pac", "az"]
    assert [t.key for t in bootstrap.tools_for(["agents"])] == ["node", "atk"]
    assert bootstrap.tools_for(["ifs"]) == []
    assert bootstrap.resolve([], False) == ["toolkit", "ifs", "packages"]
    assert bootstrap.resolve(["agents", "toolkit"], False) == ["toolkit", "agents"]


def test_present_tools_are_left_alone(monkeypatch):
    m = FakeMachine(monkeypatch, current={"git", "pac", "az"})
    out = bootstrap.run(["toolkit", "power-platform"], yes=True)
    assert status(out) == {"git": "present", "pac": "present", "az": "present"}
    assert m.calls == []
    assert bootstrap.exit_code(out) == 0


def test_dry_run_lists_installs_and_runs_nothing(monkeypatch):
    m = FakeMachine(monkeypatch, current={"git"})
    out = bootstrap.run(["power-platform"], dry_run=True)
    assert status(out) == {"pac": "planned", "az": "planned"}
    assert "winget install --id Microsoft.PowerAppsCLI --exact" in out[0].detail
    assert m.calls == []


def test_each_install_is_confirmed(monkeypatch):
    m = FakeMachine(monkeypatch)
    asked = []
    out = bootstrap.run(["power-platform"], confirm=lambda q: asked.append(q) or "Azure" in q, out=lambda s: None)
    assert asked == ["Install Power Platform CLI?", "Install Azure CLI?"]
    assert status(out) == {"pac": "declined", "az": "installed-new-terminal"}
    assert [c[3] for c in m.calls] == ["Microsoft.AzureCLI"]
    assert bootstrap.exit_code(out) == 1


def test_installed_but_not_on_this_path_is_reported_not_reinstalled(monkeypatch):
    m = FakeMachine(monkeypatch, registry={"pac", "az"})
    out = bootstrap.run(["power-platform"], yes=True)
    assert status(out) == {"pac": "not-on-path", "az": "not-on-path"}
    assert m.calls == []
    text = bootstrap.render(["power-platform"], out)
    assert "NOT ON PATH" in text and "Open a new terminal" in text
    assert "pac auth create" in text and "az login --allow-no-subscriptions" in text


def test_npm_tools_install_after_node_using_the_refreshed_path(monkeypatch):
    m = FakeMachine(monkeypatch)
    out = bootstrap.run(["agents"], yes=True, out=lambda s: None)
    assert status(out) == {"node": "installed-new-terminal", "atk": "installed-new-terminal"}
    assert m.calls[1] == ["npm", "install", "--global", "@microsoft/m365agentstoolkit-cli"]


def test_npm_tool_skipped_when_node_was_declined(monkeypatch):
    m = FakeMachine(monkeypatch)
    out = bootstrap.run(["agents"], confirm=lambda q: False, out=lambda s: None)
    assert status(out) == {"node": "declined", "atk": "skipped"}
    assert "needs node" in out[1].detail
    assert m.calls == []


def test_failed_install_is_reported(monkeypatch):
    FakeMachine(monkeypatch, fail={"az"})
    out = bootstrap.run(["power-platform"], yes=True, out=lambda s: None)
    assert status(out)["az"] == "failed"
    assert bootstrap.exit_code(out) == 1


def test_without_winget_points_to_manual_install(monkeypatch):
    m = FakeMachine(monkeypatch, winget=False)
    out = bootstrap.run(["toolkit"], yes=True)
    assert status(out) == {"git": "manual"}
    assert "git-scm.com" in out[0].detail
    assert m.calls == []


def test_spfx_generator_needs_the_npm_package_not_just_yo(monkeypatch):
    m = FakeMachine(monkeypatch, current={"node", "npm", "yo"})
    monkeypatch.setattr(bootstrap, "_quiet", lambda cmd, path=None: False)  # generator not installed
    out = bootstrap.run(["spfx"], yes=True, out=lambda s: None)
    assert status(out)["spfx"] in ("installed", "failed")
    assert m.calls == [["npm", "install", "--global", "yo", "@microsoft/generator-sharepoint"]]


def test_cli_dry_run(monkeypatch, capsys):
    FakeMachine(monkeypatch, current={"git"})
    assert main(["bootstrap", "--for", "power-platform", "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "TO INSTALL" in text and "Power Platform CLI" in text
    assert "ifs-agent-ops doctor --for power-platform" in text


def test_render_all_points_to_doctor_all(monkeypatch):
    FakeMachine(monkeypatch, current={"git", "node", "npm", "pac", "az", "atk", "yo"})
    caps = bootstrap.resolve([], True)
    text = bootstrap.render(caps, bootstrap.run(caps))
    assert text.endswith("Then check: ifs-agent-ops doctor --all")
    assert "doctor checks them" in text


@pytest.mark.parametrize("cap", ["ifs"])
def test_capability_without_tools(monkeypatch, cap):
    FakeMachine(monkeypatch)
    out = bootstrap.run([cap])
    assert out == [] and bootstrap.exit_code(out) == 0
    assert "nothing to install" in bootstrap.render([cap], out)
