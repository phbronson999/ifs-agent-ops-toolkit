"""Phase 3: profiles, toolkit home, legacy session migration, per-profile independence."""
import asyncio
import json
import ssl
import time

import httpx
import pytest
import respx

from ifs_agent_ops import profiles as prof
from ifs_agent_ops.mcp_server.runtime import Runtime

TOML = """
default_profile = "test"

[profiles.test]
base_url = "https://ifs-test.example.com/"
realm = "testrealm"
client_id = "agent-ops"
callback_port = 3001
mcp_server_port = 8101

[profiles.prod]
base_url = "https://ifs-prod.example.com"
realm = "prodrealm"
client_id = "agent-ops"
callback_port = 3002
tls_verify = false
"""

LEGACY_ENV = {"API_BASE_URL": "https://legacy.example.com", "OAUTH_REALM": "legacyrealm",
              "OAUTH_CLIENT_ID": "legacy-client", "OAUTH_CALLBACK_PORT": "3000"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path))
    monkeypatch.delenv("IFS_PROFILE", raising=False)
    for k, v in LEGACY_ENV.items():
        monkeypatch.setenv(k, v)
    return tmp_path


def write_toml(home, text=TOML):
    (home / "profiles.toml").write_text(text, encoding="utf-8")


# ── selection ────────────────────────────────────────────────────────────────

def test_no_file_means_implicit_env_profile(home):
    p = prof.resolve_profile()
    assert (p.name, p.source, p.base_url, p.realm) == ("default", "env", "https://legacy.example.com", "legacyrealm")


def test_default_profile_from_file(home):
    write_toml(home)
    p = prof.resolve_profile()
    assert (p.name, p.source, p.realm, p.mcp_server_port) == ("test", "file", "testrealm", 8101)


def test_env_var_beats_file_default_and_explicit_beats_env(home, monkeypatch):
    write_toml(home)
    monkeypatch.setenv("IFS_PROFILE", "prod")
    assert prof.resolve_profile().name == "prod"
    assert prof.resolve_profile("test").name == "test"


def test_named_profile_ignores_legacy_env_connection_vars(home):
    write_toml(home)
    s = prof.resolve_profile("prod").settings()
    assert s.base_url == "https://ifs-prod.example.com" and s.realm == "prodrealm"


def test_explicit_default_falls_back_to_env_when_not_in_file(home):
    write_toml(home)
    assert prof.resolve_profile("default").source == "env"


def test_unknown_profile_lists_available(home):
    write_toml(home)
    with pytest.raises(prof.ProfileError, match=r"Unknown profile 'stage'.*prod, test"):
        prof.resolve_profile("stage")


@pytest.mark.parametrize("text,match", [
    ('[profiles.x]\nbase_url="u"\nrealm="r"\n', "missing required setting.*client_id"),
    ('[profiles.x]\nbase_url="u"\nrealm="r"\nclient_id="c"\nbase_uri="typo"\n', "unknown setting.*base_uri"),
    ('[profiles."a b"]\nbase_url="u"\nrealm="r"\nclient_id="c"\n', "Invalid profile name"),
    ('default_profile="nope"\n[profiles.x]\nbase_url="u"\nrealm="r"\nclient_id="c"\n', "default_profile 'nope'"),
    ('[profiles.x\n', "Could not parse"),
])
def test_invalid_files_fail_with_clear_errors(home, text, match):
    write_toml(home, text)
    with pytest.raises(prof.ProfileError, match=match):
        prof.resolve_profile("x")


def test_trailing_slash_stripped_and_tls_per_profile(home):
    write_toml(home)
    assert prof.resolve_profile("test").settings().base_url == "https://ifs-test.example.com"
    assert isinstance(prof.resolve_profile("test").settings().verify, ssl.SSLContext)
    assert prof.resolve_profile("prod").settings().verify is False


# ── session files + migration ────────────────────────────────────────────────

def test_session_files_are_per_profile(home):
    assert prof.session_file("test") != prof.session_file("prod")
    assert prof.session_file("test") == home / "profiles" / "test" / "session.json"


def test_legacy_session_copied_once_and_not_overwritten(home, tmp_path_factory):
    legacy = tmp_path_factory.mktemp("legacy") / "session.json"
    legacy.write_text('{"s": {"access_token": "old"}}', encoding="utf-8")
    target = prof.session_file("default")
    assert prof.migrate_legacy_session(target, legacy) is True
    assert json.loads(target.read_text())["s"]["access_token"] == "old"
    assert legacy.exists()  # copied, not moved
    legacy.write_text('{"s": {"access_token": "newer"}}', encoding="utf-8")
    assert prof.migrate_legacy_session(target, legacy) is False
    assert json.loads(target.read_text())["s"]["access_token"] == "old"


def test_named_profiles_do_not_inherit_legacy_session(home, monkeypatch, tmp_path_factory):
    legacy = tmp_path_factory.mktemp("legacy") / "session.json"
    legacy.write_text('{"s": {"access_token": "old"}}', encoding="utf-8")
    monkeypatch.setattr(prof, "LEGACY_SESSION_FILE", legacy)
    write_toml(home)
    rt = Runtime.for_profile("test")
    assert rt.store.current_session_id() is None
    assert not prof.session_file("default").exists()


# ── exit criterion: two profiles authenticate independently ────────────────

def _expired_session(token: str, refresh: str) -> dict:
    return {"s1": {"access_token": token, "refresh_token": refresh,
                   "expires_at": int((time.time() - 60) * 1000), "user_id": "s1"}}


def test_two_profiles_refresh_and_call_independently(home):
    write_toml(home)
    for name, tok, ref in (("test", "t-old", "t-refresh"), ("prod", "p-old", "p-refresh")):
        f = prof.session_file(name)
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps(_expired_session(tok, ref)), encoding="utf-8")

    test_rt, prod_rt = Runtime.for_profile("test"), Runtime.for_profile("prod")

    async def run():
        with respx.mock(assert_all_called=True) as r:
            r.post("https://ifs-test.example.com/auth/realms/testrealm/protocol/openid-connect/token").mock(
                return_value=httpx.Response(200, json={"access_token": "t-new", "expires_in": 600}))
            r.post("https://ifs-prod.example.com/auth/realms/prodrealm/protocol/openid-connect/token").mock(
                return_value=httpx.Response(200, json={"access_token": "p-new", "expires_in": 600}))
            t_api = r.get("https://ifs-test.example.com/x").mock(return_value=httpx.Response(200, json={"env": "test"}))
            p_api = r.get("https://ifs-prod.example.com/x").mock(return_value=httpx.Response(200, json={"env": "prod"}))
            t = await test_rt.client.request("GET", "/x")
            p = await prod_rt.client.request("GET", "/x")
            forms = {str(c.request.url.host): c.request.content.decode()
                     for c in r.calls if c.request.url.path.endswith("/token")}
            return t, p, t_api.calls.last.request, p_api.calls.last.request, forms

    t, p, t_req, p_req, forms = asyncio.run(run())
    assert (t.data, p.data) == ({"env": "test"}, {"env": "prod"})
    assert t_req.headers["authorization"] == "Bearer t-new"
    assert p_req.headers["authorization"] == "Bearer p-new"
    assert "refresh_token=t-refresh" in forms["ifs-test.example.com"]
    assert "refresh_token=p-refresh" in forms["ifs-prod.example.com"]

    t_file = json.loads(prof.session_file("test").read_text())
    p_file = json.loads(prof.session_file("prod").read_text())
    assert t_file["s1"]["access_token"] == "t-new" and p_file["s1"]["access_token"] == "p-new"


def test_signing_out_of_one_profile_leaves_the_other(home):
    write_toml(home)
    f = prof.session_file("prod")
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(_expired_session("p", "pr")), encoding="utf-8")
    assert Runtime.for_profile("test").store.current_session_id() is None
    assert Runtime.for_profile("prod").store.current_session_id() == "s1"


# ── server / CLI entry points ────────────────────────────────────────────────

def test_server_main_rejects_unknown_profile(home, capsys):
    write_toml(home)
    from ifs_agent_ops.mcp_server import server

    with pytest.raises(SystemExit) as exc:
        server.main(["--profile", "stage"])
    assert exc.value.code == 2
    assert "Unknown profile 'stage'" in capsys.readouterr().err


def test_cli_profiles_lists_profiles_and_flags_shared_ports(home, capsys):
    write_toml(home, TOML.replace("callback_port = 3002", "callback_port = 3001"))
    from ifs_agent_ops.cli.main import main

    assert main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert "* test" in out and "prod" in out and "default" in out
    assert "not signed in" in out
    assert "callback port(s) 3001 are shared" in out
