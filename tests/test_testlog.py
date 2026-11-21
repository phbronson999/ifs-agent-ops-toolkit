"""Phase 9b: the local test log of call_protected_api calls."""
import asyncio
import json
from datetime import datetime

import httpx
import pytest
import respx

from ifs_agent_ops import profiles as prof
from ifs_agent_ops import testlog
from ifs_agent_ops.cli import main as cli
from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.mcp_server.tools.api.call_protected_api import handler
from ifs_agent_ops.testlog import CallLog

BASE = "https://ifs-test.local"
EP = "/main/ifsapplications/projection/v1/Demo.svc/ThingSet"


class _Tokens:
    def current_session_id(self):
        return "s"

    async def get_access_token(self, session_id):
        return "secret-token"


def call(log, route, method="GET", body=None, headers=None):
    client = IfsClient(BASE, _Tokens())

    async def run():
        with respx.mock() as router:
            route(router)
            try:
                return await handler(EP, method, None, body, headers, client, log)
            finally:
                await client.aclose()
    return asyncio.run(run())


# ── the log ──────────────────────────────────────────────────────────────────

def test_refs_are_numbered_per_day_and_entries_read_back(tmp_path):
    log = CallLog(tmp_path, "test")
    day1 = datetime(2026, 11, 21, 9, 30)
    a = log.record(method="GET", endpoint=EP, request_headers=None, request_body=None, status=200, ok=True, now=day1)
    b = log.record(method="POST", endpoint=EP, request_headers=None, request_body={"x": 1}, status=201, ok=True,
                   now=day1)
    c = log.record(method="GET", endpoint=EP, request_headers=None, request_body=None, status=200, ok=True,
                   now=datetime(2026, 11, 22, 8, 0))
    assert (a, b, c) == ("20261121-0001", "20261121-0002", "20261122-0001")
    assert log.days() == ["2026-11-22", "2026-11-21"]
    assert [e["ref"] for e in log.entries("2026-11-21")] == [a, b]
    assert [e["ref"] for e in log.entries()] == [a, b, c]
    assert log.get(b)["request_body"] == {"x": 1} and log.get(b)["profile"] == "test"
    assert log.get("20261121-0099") is None and log.get("nonsense") is None


def test_credentials_are_never_written_and_large_bodies_are_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(testlog, "MAX_BODY_CHARS", 50)
    log = CallLog(tmp_path, "test")
    ref = log.record(method="GET", endpoint=EP, request_headers={"Authorization": "Bearer x", "Cookie": "c=1",
                                                                 "If-Match": 'W/"1"'},
                     request_body=None, status=200, ok=True,
                     response_headers={"Set-Cookie": "s=1", "ETag": 'W/"2"'}, response_body={"v": "y" * 100})
    e = log.get(ref)
    assert e["request_headers"] == {"If-Match": 'W/"1"'} and e["response_headers"] == {"ETag": 'W/"2"'}
    assert e["response_body"]["_truncated"] is True and len(e["response_body"]["_preview"]) == 50
    raw = (tmp_path / "{}.jsonl".format(e["at"][:10])).read_text(encoding="utf-8")
    assert "Bearer" not in raw and "c=1" not in raw and "s=1" not in raw


def test_logging_can_be_turned_off(monkeypatch):
    monkeypatch.delenv("IFS_AGENT_OPS_TEST_LOG", raising=False)
    assert testlog.logging_enabled()
    monkeypatch.setenv("IFS_AGENT_OPS_TEST_LOG", "off")
    assert not testlog.logging_enabled()


# ── call_protected_api ───────────────────────────────────────────────────────

def test_success_is_logged_with_response_headers_and_the_result_carries_the_ref(tmp_path):
    log = CallLog(tmp_path, "test")
    out = json.loads(call(log, lambda r: r.post(BASE + EP).mock(return_value=httpx.Response(
        201, json={"OrderNo": "C1"}, headers={"ETag": 'W/"abc"', "X-IFS-Warnings": "w1"})),
        method="POST", body={"CustomerNo": "3822"}, headers={"X-IFS-Accept-Warnings": "true"}))
    assert out["status"] == 201 and out["data"] == {"OrderNo": "C1"}
    e = log.get(out["test_log_ref"])
    assert e["method"] == "POST" and e["endpoint"] == EP and e["status"] == 201 and e["ok"] is True
    assert e["request_body"] == {"CustomerNo": "3822"}
    assert e["request_headers"] == {"X-IFS-Accept-Warnings": "true"}
    assert e["response_headers"]["etag"] == 'W/"abc"' and e["response_headers"]["x-ifs-warnings"] == "w1"
    assert e["response_body"] == {"OrderNo": "C1"} and e["duration_ms"] >= 0
    assert "secret-token" not in json.dumps(e)


def test_ifs_errors_are_logged_with_the_ifs_message(tmp_path):
    log = CallLog(tmp_path, "test")
    err = {"error": {"code": "DATABASE_ERROR", "message": "ORA-20124: Field [AuthorizeCode] is mandatory"}}
    out = json.loads(call(log, lambda r: r.post(BASE + EP).mock(return_value=httpx.Response(
        500, json=err, headers={"X-Request-Id": "r1"})), method="POST", body={}))
    assert out["error"] == "API call failed" and out["status"] == 500
    e = log.get(out["test_log_ref"])
    assert e["ok"] is False and e["status"] == 500 and e["response_body"] == err
    assert e["response_headers"]["x-request-id"] == "r1" and "ORA-20124" in e["message"]


def test_transport_failures_are_logged_and_still_raised(tmp_path):
    log = CallLog(tmp_path, "test")
    with pytest.raises(httpx.ConnectError):
        call(log, lambda r: r.get(BASE + EP).mock(side_effect=httpx.ConnectError("down")))
    [e] = log.entries()
    assert e["ok"] is False and e["status"] is None and e["error"] == "ConnectError"


def test_without_a_log_the_result_is_unchanged(tmp_path):
    out = json.loads(call(None, lambda r: r.get(BASE + EP).mock(return_value=httpx.Response(200, json={"a": 1}))))
    assert out == {"status": 200, "data": {"a": 1}}


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_lists_and_shows_the_profile_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path))
    name = prof.resolve_profile(None).name
    log = CallLog.beside_session(prof.session_file(name), name)
    assert cli.main(["testlog", "list"]) == 0 and "No test log yet" in capsys.readouterr().out
    ref = log.record(method="GET", endpoint=EP, request_headers=None, request_body=None, status=200, ok=True,
                     response_headers={"ETag": "e"}, duration_ms=12)
    assert cli.main(["testlog", "list"]) == 0
    out = capsys.readouterr().out
    assert ref in out and "/Demo.svc/ThingSet" in out and "[etag]" in out and "1 call(s)" in out
    assert cli.main(["testlog", "show", ref]) == 0 and json.loads(capsys.readouterr().out)["ref"] == ref
    assert cli.main(["testlog", "show", "20990101-0001"]) == 2
