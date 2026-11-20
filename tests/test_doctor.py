"""Phase 7: capability-based readiness checks."""
import json
import time

import pytest

from ifs_agent_ops import doctor
from ifs_agent_ops import profiles as prof
from ifs_agent_ops.workspace.package import init_package

IFS_ENV = {"API_BASE_URL": "https://ifs.example", "OAUTH_REALM": "r", "OAUTH_CLIENT_ID": "c",
           "OAUTH_CALLBACK_PORT": "3999"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path / "home"))
    for k in ("IFS_PROFILE", "IFS_AGENT_OPS_PACKAGES", "IFS_TLS_VERIFY", "IFS_CA_BUNDLE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in IFS_ENV.items():
        monkeypatch.setenv(k, v)
    return tmp_path


def by_name(results, capability):
    return {r.name: r for r in results if r.capability == capability}


def test_optional_capabilities_are_not_required_by_default(env):
    results = doctor.run(online=False)
    for cap in doctor.OPTIONAL:
        assert by_name(results, cap)["-"].status == doctor.NOT_REQUIRED
    assert {r.capability for r in results if r.status != doctor.NOT_REQUIRED} <= set(doctor.CORE)


def test_fresh_clone_is_actionable(env, monkeypatch):
    for k in ("API_BASE_URL", "OAUTH_REALM", "OAUTH_CLIENT_ID"):
        monkeypatch.setenv(k, "")
    results = doctor.run(online=False)
    ifs = by_name(results, "ifs")
    assert ifs["profile settings"].status == doctor.FAIL
    assert ".env.example" in ifs["profile settings"].fix
    assert by_name(results, "packages")["configuration"].status == doctor.WARN
    assert doctor.exit_code(results) == 1
    rendered = doctor.render(results)
    assert "fix: copy .env.example" in rendered and "failed" in rendered


def test_sign_in_states(env):
    from ifs_agent_ops.ifs.auth.token_store import FileTokenStore, TokenData

    def state():
        return by_name(doctor.run(["ifs"], online=False), "ifs")["sign-in"]

    assert state().status == doctor.WARN and "start_oauth" in state().fix
    store = FileTokenStore(prof.session_file("default"))
    now = int(time.time() * 1000)
    store.save("s", TokenData(access_token="a", expires_at=now - 1000, user_id="s", refresh_token="r"))
    assert state().status == doctor.PASS and "refreshes" in state().detail
    store.save("s", TokenData(access_token="a", expires_at=now - 1000, user_id="s"))
    assert state().status == doctor.WARN
    store.save("s", TokenData(access_token="a", expires_at=now + 600_000, user_id="s"))
    assert state().status == doctor.PASS and "min left" in state().detail


def test_tls_disabled_is_a_warning(env, monkeypatch):
    monkeypatch.setenv("IFS_TLS_VERIFY", "false")
    assert by_name(doctor.run(["ifs"], online=False), "ifs")["tls setting"].status == doctor.WARN


def test_unknown_profile_fails_with_fix(env, monkeypatch):
    monkeypatch.setenv("IFS_PROFILE", "prod")
    check = by_name(doctor.run(["ifs"], online=False), "ifs")["profile"]
    assert check.status == doctor.FAIL and "Unknown profile 'prod'" in check.detail


def test_package_hygiene(env, monkeypatch):
    pkg = init_package(env / "pkg", "ifs-orders", projections=["OrderHandling"])
    (pkg.root / ".gitignore").write_text("*.raw.har\n", encoding="utf-8")         # missing .ifs-agent-ops/
    rec = pkg.recordings_dir / "wf"
    rec.mkdir(parents=True)
    (rec / "t.har").write_text('{"log":{"entries":[{"request":{"url":"https://ifs.example/main/ifsapplications/'
                               'projection/v1/OrderHandling.svc/OrderSet"}}]}}', encoding="utf-8")  # no annotations
    (pkg.root / "stray.har").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    checks = by_name(doctor.run(["packages"], online=False), "packages")
    assert checks["ifs-orders: .gitignore"].status == doctor.WARN
    assert ".ifs-agent-ops/" in checks["ifs-orders: .gitignore"].detail
    assert checks["ifs-orders: git"].status == doctor.WARN               # not a git repository
    rec_check = checks["ifs-orders: recordings"]
    assert rec_check.status == doctor.FAIL and "stray.har" in rec_check.detail and "record import" in rec_check.fix
    snap = checks["ifs-orders: metadata snapshots"]
    assert snap.status == doctor.WARN and "OrderHandling" in snap.detail


def test_missing_package_path_is_a_failure(env, monkeypatch):
    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(env / "nope"))
    results = doctor.run(["packages"], online=False)
    assert any(r.name == "discovery" and r.status == doctor.FAIL for r in results)


def test_mcp_sdk_2_is_flagged(env, monkeypatch):
    real = doctor.version
    monkeypatch.setattr(doctor, "version", lambda d: "2.0.1" if d == "mcp" else real(d))
    check = by_name(doctor.run(["toolkit"], online=False), "toolkit")["dependency mcp"]
    assert check.status == doctor.FAIL and "FastMCP" in check.fix


def test_external_tools_missing_or_too_old(env, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    results = doctor.run(["power-platform", "agents"], online=False)
    pp, agents = by_name(results, "power-platform"), by_name(results, "agents")
    assert pp["pac"].status == doctor.FAIL and "Microsoft.PowerApps.CLI.Tool" in pp["pac"].fix
    assert agents["node"].status == doctor.FAIL and agents["atk"].status == doctor.FAIL
    assert "@microsoft/m365agentstoolkit-cli" in agents["atk"].fix

    monkeypatch.setattr(doctor, "_run", lambda cmd, timeout=20.0: "v20.11.0" if cmd[0] == "node" else None)
    node = by_name(doctor.run(["agents"], online=False), "agents")["node"]
    assert node.status == doctor.FAIL and "20.19+" in node.fix
    monkeypatch.setattr(doctor, "_run", lambda cmd, timeout=20.0: "v22.12.0" if cmd[0] == "node" else None)
    assert by_name(doctor.run(["agents"], online=False), "agents")["node"].status == doctor.PASS


def test_a_crashing_check_does_not_hide_the_others(env, monkeypatch):
    def boom():
        raise RuntimeError("bug")
    monkeypatch.setitem(doctor.CHECKS, "packages", boom)
    results = doctor.run(online=False)
    assert by_name(results, "packages")["doctor"].status == doctor.FAIL
    assert by_name(results, "toolkit")["python"].status == doctor.PASS


def test_cli_json_and_exit_code(env, monkeypatch, capsys):
    from ifs_agent_ops.cli.main import main

    code = main(["doctor", "--offline", "--json", "--for", "toolkit"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and {d["status"] for d in data if d["capability"] == "toolkit"} <= {"PASS", "WARN"}
    for k in ("API_BASE_URL", "OAUTH_REALM", "OAUTH_CLIENT_ID"):
        monkeypatch.setenv(k, "")
    assert main(["doctor", "--offline", "--for", "ifs"]) == 1


def test_mcp_doctor_tool(env):
    import asyncio

    from ifs_agent_ops.mcp_server import server

    text = asyncio.run(server.doctor(["toolkit"], offline=True))
    assert "[toolkit]" in text and "NOT REQUIRED" in text
