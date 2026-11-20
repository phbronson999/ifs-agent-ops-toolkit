"""Phase 4: evidence loading, sanitization, sequence/data-flow analysis."""
import copy
import json
from pathlib import Path

import pytest

from ifs_agent_ops.analysis.sequence import build_sequence, render_sequence
from ifs_agent_ops.recordings.har import calls_from_har, load_har
from ifs_agent_ops.recordings.sanitize import REDACTED, sanitize_har

FIXTURE = Path(__file__).parent / "fixtures" / "customer_order_flow.har"
SVC = "https://example.ifs.cloud/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9.c2lnbmF0dXJl"


def entry(method, url, status=200, req_headers=None, body=None, resp=None, resp_headers=None,
          cookies=None, query=None, mime="application/json"):
    req = {"method": method, "url": url, "headers": req_headers or [], "cookies": cookies or [],
           "queryString": query or []}
    if body is not None:
        req["postData"] = {"mimeType": mime, "text": body if isinstance(body, str) else json.dumps(body)}
    content = {"mimeType": "application/json"}
    if resp is not None:
        content["text"] = resp if isinstance(resp, str) else json.dumps(resp)
    return {"request": req, "response": {"status": status, "headers": resp_headers or [], "content": content,
                                         "cookies": []}, "_initiator": {"stack": "x" * 50},
            "serverIPAddress": "10.9.8.7"}


def trace_envelope(payload, status="200"):
    return {
        "request": {"method": "GET", "uri": "/x", "protocol": "HTTP/1.1"},
        "response": {"status": {"code": status, "info": "OK"}, "headers": {}, "body": json.dumps(payload)},
        "server": {"version": "1", "environment": {"authType": "BEARER", "remoteUser": "PHILL",
                                                   "remoteAddr": "10.1.2.3", "serverName": "ifs-int-01"}},
        "ifs-trace": {"authStatus": "authorized", "trace": [
            {"type": "Application", "category": "APPLICATION", "text": "Shop_Oper_Clocking_API.New__"},
            {"type": "sql", "origin": "db", "text": "select * from x where id = :1",
             "textWithBinds": "select * from x where id = 'SECRET-BIND'", "bindParams": {"1": "SECRET-BIND"}},
        ]},
    }


def sensitive_har():
    return {"log": {"version": "1.2", "pages": [{"id": "p1", "title": f"https://idp/cb?code=AUTHCODE&state=ST"}],
                    "entries": [
        entry("GET", "https://idp.example/cb?code=AUTHCODE&state=XYZSTATE&session_state=SS1",
              query=[{"name": "code", "value": "AUTHCODE"}, {"name": "state", "value": "XYZSTATE"}],
              req_headers=[{"name": "Referer", "value": f"https://app/#access_token={JWT}"}]),
        entry("POST", "https://idp.example/token", body="grant_type=refresh_token&refresh_token=RT-SECRET&client_id=c",
              mime="application/x-www-form-urlencoded",
              resp={"access_token": "AT-SECRET", "refresh_token": "RT2-SECRET", "expires_in": 300}),
        entry("GET", f"{SVC}/ShopOperClockingUtilSet?$filter=Contract%20eq%20'100'",
              req_headers=[{"name": "Authorization", "value": "Bearer AT-SECRET"},
                           {"name": "Cookie", "value": "SESSION=COOKIE-SECRET"},
                           {"name": "X-XSRF-TOKEN", "value": "XSRF-SECRET"},
                           {"name": "X-IFS-Accept-Warnings", "value": "true"}],
              cookies=[{"name": "SESSION", "value": "COOKIE-SECRET"}],
              resp_headers=[{"name": "Set-Cookie", "value": "SESSION=COOKIE-SECRET2"},
                            {"name": "X-IFS-Request-Id", "value": "req-123"}],
              resp=trace_envelope({"value": [{"ClockingSeq": 4711, "Code": "LABOR", "State": "Active",
                                              "@odata.etag": "W/\"etag-1\""}]})),
        entry("PATCH", f"{SVC}/ShopOperClockingSet(ClockingSeq=4711)", status=204,
              req_headers=[{"name": "If-Match", "value": "W/\"etag-1\""}], body={"NoteText": "fixed"}),
    ]}}


SECRETS = ["AUTHCODE", "XYZSTATE", "SS1", JWT, "RT-SECRET", "AT-SECRET", "RT2-SECRET", "COOKIE-SECRET",
           "XSRF-SECRET", "SECRET-BIND", "PHILL", "10.1.2.3", "ifs-int-01", "10.9.8.7"]


# ── evidence loading ─────────────────────────────────────────────────────────

def test_load_keeps_every_entry_in_order_with_headers_and_bodies():
    calls = load_har(FIXTURE)
    assert [c.index for c in calls] == list(range(13))
    assert calls[11].url.endswith("$batch") and calls[12].status == 404  # kept, not filtered
    assert calls[9].request_header("if-match") == 'W/"etag"'
    assert json.loads(calls[7].request_body)["CustomerNo"] == "CUST-001"


def test_trace_envelope_payload_is_unwrapped():
    har = sensitive_har()
    call = calls_from_har(har)[2]
    assert call.ifs_traced
    status, body = call.payload()
    assert status == 200 and json.loads(body)["value"][0]["ClockingSeq"] == 4711
    plain = calls_from_har(har)[3]
    assert not plain.ifs_traced and plain.payload()[0] == 204


# ── sanitizer ────────────────────────────────────────────────────────────────

def test_sanitizer_removes_every_secret_and_keeps_business_data():
    har = sensitive_har()
    original = copy.deepcopy(har)
    clean, report = sanitize_har(har)
    text = json.dumps(clean)
    leaked = [s for s in SECRETS if s in text]
    assert not leaked, f"secrets survived sanitization: {leaked}"
    assert har == original, "input must not be modified"
    # business evidence survives: IFS fields named Code/State, enum-ish values, keys, etag, trace text
    envelope = json.loads(clean["log"]["entries"][2]["response"]["content"]["text"])
    body = json.loads(envelope["response"]["body"])
    assert body["value"][0] == {"ClockingSeq": 4711, "Code": "LABOR", "State": "Active", "@odata.etag": 'W/"etag-1"'}
    assert "Shop_Oper_Clocking_API.New__" in text and "select * from x where id = :1" in text
    environment = envelope["server"]["environment"]
    assert environment["authType"] == "BEARER" and environment["remoteUser"] == REDACTED
    assert envelope["ifs-trace"]["trace"][1]["bindParams"] == {"1": REDACTED}
    assert "X-IFS-Accept-Warnings" in text and "req-123" in text
    assert all("_initiator" not in e for e in clean["log"]["entries"])
    for key in ("header", "cookie", "url_param", "jwt", "form_param", "body_key", "trace_environment",
                "trace_bind_values", "trace_sql_with_binds", "_initiator", "server_ip"):
        assert report.get(key), f"expected report to count {key}"


def test_sanitizer_keeps_credential_header_names_with_redacted_values():
    clean, _ = sanitize_har(sensitive_har())
    headers = {h["name"]: h["value"] for h in clean["log"]["entries"][2]["request"]["headers"]}
    assert headers["Authorization"] == headers["Cookie"] == headers["X-XSRF-TOKEN"] == REDACTED
    assert headers["X-IFS-Accept-Warnings"] == "true"


def test_sanitizing_does_not_change_the_analysis():
    har = sensitive_har()
    before = build_sequence(calls_from_har(har))
    after = build_sequence(calls_from_har(sanitize_har(har)[0]))
    assert render_sequence(*before) == render_sequence(*after)


def test_unchanged_bodies_are_not_reserialized():
    har = {"log": {"entries": [entry("GET", f"{SVC}/X", resp='{"value": [ {"A": 1} ]}')]}}
    clean, _ = sanitize_har(har)
    assert clean["log"]["entries"][0]["response"]["content"]["text"] == '{"value": [ {"A": 1} ]}'


# ── sequence + data flow ─────────────────────────────────────────────────────

def test_sequence_keeps_failures_batch_and_metadata_in_order():
    views, _ = build_sequence(load_har(FIXTURE))
    kinds = [(v.index, v.kind) for v in views if v.kind != "ifs"]
    assert kinds == [(0, "ifs-metadata"), (1, "other"), (11, "ifs-batch")]
    assert views[12].failed and views[12].response == "error"
    assert views[9].headers == ["if-match"]


def test_data_flow_links_keys_body_values_and_etags_without_values():
    _, links = build_sequence(load_har(FIXTURE))
    pairs = {(ln.to_index, ln.to_place, ln.from_index, ln.from_field) for ln in links}
    assert (8, "key OrderNo", 7, "OrderNo") in pairs   # action on the order the POST created
    assert (7, "body CustomerNo", 4, "CustomerNo") in pairs

    views, links = build_sequence(calls_from_har(sensitive_har()))
    assert (3, "If-Match", 2, "@odata.etag") in {(l.to_index, l.to_place, l.from_index, l.from_field) for l in links}
    assert (3, "key ClockingSeq", 2, "ClockingSeq") in {(l.to_index, l.to_place, l.from_index, l.from_field) for l in links}
    rendered = render_sequence(views, links)
    assert "4711" not in rendered and "etag-1" not in rendered  # names and positions only
    assert "IFS server tracing was on for 1 call(s)" in rendered


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_sanitize_refuses_to_overwrite_input_without_flag(tmp_path, capsys):
    from ifs_agent_ops.cli.main import main

    src = tmp_path / "rec.har"
    src.write_text(json.dumps(sensitive_har()), encoding="utf-8")
    assert main(["har", "sanitize", str(src), str(src)]) == 2
    out = tmp_path / "clean" / "rec.har"
    assert main(["har", "sanitize", str(src), str(out)]) == 0
    assert "XSRF-SECRET" not in out.read_text(encoding="utf-8")
    assert "business data values are kept" in capsys.readouterr().out


def test_mcp_analyze_recording_tool_reports_failures(tmp_path):
    import asyncio

    from ifs_agent_ops.mcp_server.tools.api.analyze_recording import handler

    out = asyncio.run(handler(str(FIXTURE)))
    assert "#12 GET CustomerOrderHandling/CustomerOrderSet -> 404 FAILED error" in out
    assert asyncio.run(handler(str(tmp_path / "missing.har"))).startswith("Failed to analyse recording:")
