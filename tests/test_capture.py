"""Phase 9a: guided capture (intent -> recording plan -> task-bucketed recording -> package import)."""
import asyncio
import json

import pytest
import yaml
from starlette.testclient import TestClient

from ifs_agent_ops.capture import api, service
from ifs_agent_ops.capture.har_export import export
from ifs_agent_ops.capture.session import CaptureError, new_session
from ifs_agent_ops.capture.store import CaptureStore
from ifs_agent_ops.recordings.annotations import load_plan
from test_operations import EDMX  # noqa: F401
from test_operations import pkg  # noqa: F401

ORIGIN = "https://ifs-test.local"
SVC = f"{ORIGIN}/main/ifsapplications/projection/v1/OrderHandling.svc"
EXT = {"Origin": "chrome-extension://abcdefghijklmnop"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path / "home"))
    s = CaptureStore(tmp_path / "home" / "captures")
    api.set_store(s)
    yield s
    api.set_store(None)


def call(rid, ts, method, path, body=None, resp=None, status=200, headers=None):
    """A request event and its response, as the extension's Rust core emits them."""
    req = {"type": "request", "seq": int(ts * 10), "request_id": rid, "ts": ts, "method": method, "url": SVC + path,
           "headers": {"Content-Type": "application/json", "Authorization": "Bearer t0ken", **(headers or {})},
           "resource_type": "XHR"}
    if body is not None:
        req["post_data"] = json.dumps(body)
    res = {"type": "response", "seq": int(ts * 10), "request_id": rid, "ts": ts + 0.2, "status": status,
           "status_text": "", "headers": {"Content-Type": "application/json", "Set-Cookie": "s=1"},
           "mime_type": "application/json", "body": json.dumps(resp) if resp is not None else "", "base64": False}
    return [req, res]


ORDER = {"@odata.etag": 'W/"1"', "OrderNo": "ORD-77", "CustomerNo": "5832"}


def demonstrate(store):
    """Plan three tasks, add one while recording, skip one, and stop: a complete plan."""
    sid = service.create(store, "I want to teach the builder how to create a customer order",
                         ["Create order header", "Add order line for customer 5832", "Add charge"],
                         workflow="create-order")["id"]
    service.apply(store, sid, "start", ifs_origin=ORIGIN, ts=100.0)
    service.ingest(store, sid, call("1", 101.0, "GET", "/GetDefaults(CustomerNo='5832',TaxCode=null)", resp={"Ok": "Y"})
                   + call("2", 102.0, "POST", "/OrderSet", body={"CustomerNo": "5832"}, resp=ORDER, status=201))
    service.apply(store, sid, "complete", ts=103.0, task_id="t1")
    service.ingest(store, sid, call("3", 104.0, "POST", "/OrderSet(OrderNo='ORD-77')/LinesArray",
                                    body={"OrderNo": "ORD-77", "LineNo": "1"}, resp={"OrderNo": "ORD-77"}, status=201))
    service.apply(store, sid, "add", title="Release order", ts=104.5)   # realised during the recording
    service.apply(store, sid, "complete", ts=105.0)
    service.ingest(store, sid, call("4", 106.0, "POST", "/OrderSet(OrderNo='ORD-77')/IfsApp.OrderHandling.Order_Release",
                                    status=204, headers={"If-Match": 'W/"1"'}))
    service.apply(store, sid, "complete", ts=107.0)
    service.apply(store, sid, "skip", ts=108.0)                          # no charge on this order
    service.ingest(store, sid, call("5", 108.5, "GET", "/OrderSet(OrderNo='ORD-77')", resp=ORDER))
    return sid


# ── plan and recording rules ─────────────────────────────────────────────────

def test_plan_is_edited_before_recording_and_one_task_is_current_while_recording():
    s = new_session("Teach order creation", ["Header", "Lines", "Finish"])
    s.add_task("Charge", position=3)
    s.move_task("t3", 4)
    s.edit_task("t2", title="Add order line")
    s.remove_task("t4")
    assert [t.title for t in s.tasks] == ["Header", "Add order line", "Finish"] and s.current() is None
    with pytest.raises(CaptureError, match="empty"):
        new_session("x").start(ORIGIN)
    s.start(ORIGIN, ts=10.0)
    assert [t.status for t in s.tasks] == ["active", "pending", "pending"]
    with pytest.raises(CaptureError, match="not the current task"):
        s.complete_current(ts=11.0, task_id="t2")
    s.complete_current(ts=11.0)
    with pytest.raises(CaptureError, match="keep their place"):
        s.move_task("t3", 1)
    added = s.add_task("Add charge", ts=11.5)                 # during recording: right after the current task
    assert added.added_during_recording and [t.id for t in s.tasks] == ["t1", "t2", "t5", "t3"]
    s.skip_current(ts=12.0)
    assert sum(t.status == "active" for t in s.tasks) == 1 and s.current().id == "t5"
    assert (s.task_at(10.5), s.task_at(11.0), s.task_at(11.99), s.task_at(9.0)) == ("t1", "t2", "t2", None)
    s.stop(ts=13.0)
    assert [t.status for t in s.tasks] == ["complete", "skipped", "incomplete", "pending"]
    assert not s.plan_complete and s.progress()["done"] == 2
    with pytest.raises(CaptureError, match="stopped"):
        s.add_task("late")


def test_adding_a_task_after_the_plan_is_done_makes_it_current():
    s = new_session("x", ["only"])
    s.start(ORIGIN, ts=1.0)
    s.complete_current(ts=2.0)
    assert s.current() is None and s.plan_complete
    s.add_task("one more", ts=3.0)
    assert s.current().title == "one more" and s.current().started_at == 3.0 and not s.plan_complete


def test_events_are_stored_locally_without_credentials_and_attributed_by_send_time(store):
    sid = demonstrate(store)
    events = store.events(sid)
    assert not any("Authorization" in e.get("headers", {}) or "Set-Cookie" in e.get("headers", {}) for e in events)
    view = service.view(store, store.load(sid))
    assert view["requests"]["by_task"] == {"t1": 2, "t2": 1, "t4": 1, "t3": 0}
    assert view["requests"]["unassigned"] == 1 and view["progress"]["current"] is None
    with pytest.raises(CaptureError, match="event"):
        service.ingest(store, sid, [{"type": "bogus"}])


def test_ingest_rules(store):
    sid = service.create(store, "x", ["a"])["id"]
    with pytest.raises(CaptureError, match="not recording"):
        service.ingest(store, sid, call("1", 1.0, "GET", "/A"))
    service.apply(store, sid, "start", ifs_origin=ORIGIN, ts=1.0)
    other = dict(call("9", 2.0, "GET", "/A")[0], url="https://elsewhere.example/A")
    assert service.ingest(store, sid, [other])["accepted"] == 0
    req, resp = call("1", 2.0, "GET", "/A", resp={"v": 1})
    service.ingest(store, sid, [req])
    service.apply(store, sid, "stop", ts=3.0)
    late_before_stop = call("2", 2.5, "GET", "/B")
    after_stop = call("3", 4.0, "GET", "/C")
    out = service.ingest(store, sid, [resp] + late_before_stop + after_stop)
    assert out["accepted"] == 3 and out["ignored"] == 2  # request 3 (sent after stop) and its response


def test_export_orders_entries_and_keeps_task_ranges(store):
    sid = demonstrate(store)
    service.apply(store, sid, "stop", ts=109.0)
    har, plan = export(store.load(sid), store.events(sid))
    urls = [e["request"]["url"].split(".svc")[1] for e in har["log"]["entries"]]
    assert urls[0].startswith("/GetDefaults") and urls[-1] == "/OrderSet(OrderNo='ORD-77')"
    assert plan["complete"] is True and plan["intent"].startswith("I want to teach")
    assert [(t["id"], t["status"], t["entries"]) for t in plan["tasks"]] == [
        ("t1", "complete", [0, 1]), ("t2", "complete", [2, 2]), ("t4", "complete", [3, 3]), ("t3", "skipped", None)]
    assert plan["tasks"][2]["added_during_recording"] and plan["unassigned_entries"] == [[4, 4]]


# ── into the package: annotations, analysis, observed workflows ─────────────

def test_import_carries_the_plan_into_the_package_analysis_and_workflows(store, pkg):  # noqa: F811
    sid = demonstrate(store)
    service.apply(store, sid, "stop", ts=109.0)
    result, plan = service.import_session(store, sid, pkg)
    assert result.recording == pkg.recordings_dir / "create-order" / f"{sid}.har"
    text = result.recording.read_text(encoding="utf-8")
    assert "ifs-test.local" not in text and "t0ken" not in text          # sanitized, host normalised
    notes = yaml.safe_load(result.annotations.read_text(encoding="utf-8"))
    assert notes["plan"]["complete"] is True and notes["plan"]["session"] == sid
    assert "5832" not in result.annotations.read_text(encoding="utf-8")  # plan text redacted like the recording
    assert notes["plan"]["tasks"][1]["title"].startswith("Add order line for customer ")
    assert store.load(sid).imports == [f"recordings/create-order/{sid}.har"]

    from ifs_agent_ops.mcp_server.tools.api.analyze_recording import handler
    report = asyncio.run(handler(str(result.recording)))
    assert "### Recording plan" in report and "Plan: complete" in report
    assert report.index("#### Task t1: Create order header (complete)") < report.index("#### Task t2:") \
        < report.index("#### Task t4: Release order (complete)") < report.index("#### Outside any task")
    assert "added during recording" in report and "[t3] Add charge - skipped" in report

    from ifs_agent_ops.operations import service as ops
    ops.draft(pkg)
    from ifs_agent_ops.operations.store import load_workflows
    wf = next(w for w in load_workflows(pkg).values() if w.recording.endswith(f"{sid}.har"))
    assert wf.plan.intent.startswith("I want to teach") and [t.id for t in wf.plan.tasks] == ["t1", "t2", "t4", "t3"]
    assert [s.task for s in wf.steps] == ["t1", "t1", "t2", "t4", None]
    assert load_plan(result.recording)["tasks"][0]["calls"] == 2


def test_only_stopped_sessions_with_calls_import(store, pkg):  # noqa: F811
    sid = service.create(store, "x", ["a"], workflow="wf")["id"]
    with pytest.raises(CaptureError, match="stop the recording"):
        service.import_session(store, sid, pkg)
    service.apply(store, sid, "start", ifs_origin=ORIGIN, ts=1.0)
    service.apply(store, sid, "stop", ts=2.0)
    with pytest.raises(CaptureError, match="no IFS calls"):
        service.import_session(store, sid, pkg)
    nowf = service.create(store, "y", ["a"])["id"]
    service.apply(store, nowf, "start", ifs_origin=ORIGIN, ts=1.0)
    service.apply(store, nowf, "stop", ts=2.0)
    with pytest.raises(CaptureError, match="--workflow"):
        service.import_session(store, nowf, pkg)


# ── surfaces: local API (extension), CLI, MCP ───────────────────────────────

def test_capture_api_drives_a_recording_and_refuses_web_pages(store):
    client = TestClient(api.app())
    assert client.get("/capture/v1/health", headers=EXT).json()["protocol"] == 1
    assert client.post("/capture/v1/sessions", json={"intent": "x", "tasks": ["a"]},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    s = client.post("/capture/v1/sessions", headers=EXT, json={
        "intent": "Teach order creation", "tasks": ["Header", "Lines"], "workflow": "create-order"}).json()
    sid = s["id"]
    base = f"/capture/v1/sessions/{sid}"
    client.post(f"{base}/tasks", headers=EXT, json={"title": "Charge"})
    client.post(f"{base}/tasks/t3/move", headers=EXT, json={"position": 2})
    client.patch(f"{base}/tasks/t2", headers=EXT, json={"title": "Add order line"})
    assert [t["title"] for t in client.get(base, headers=EXT).json()["tasks"]] == ["Header", "Charge", "Add order line"]
    client.post(f"{base}/start", headers=EXT, json={"ifs_origin": ORIGIN, "ts": 10.0})
    r = client.post(f"{base}/events", headers=EXT, json={"events": call("a", 11.0, "GET", "/X", resp={})})
    assert r.json() == {"accepted": 2, "ignored": 0, "status": "recording", "current_task": "t1"}
    assert client.post(f"{base}/complete", headers=EXT, json={"ts": 12.0, "task_id": "t2"}).status_code == 409
    client.post(f"{base}/complete", headers=EXT, json={"ts": 12.0, "task_id": "t1"})
    client.post(f"{base}/skip", headers=EXT, json={"ts": 13.0})
    view = client.post(f"{base}/stop", headers=EXT, json={"ts": 14.0}).json()
    assert view["status"] == "stopped" and view["progress"]["plan_complete"] is False
    assert [t["status"] for t in view["tasks"]] == ["complete", "skipped", "incomplete"]
    assert view["requests"]["by_task"]["t1"] == 1
    assert client.get("/capture/v1/sessions/nope", headers=EXT).status_code == 404
    assert client.post(f"{base}/events", headers=EXT, content=b"not json").status_code == 400


def test_cli_plan_record_and_import(store, pkg, capsys):  # noqa: F811
    from ifs_agent_ops.cli.main import main

    assert main(["capture", "new", "I want to teach the builder how to create a customer order",
                 "--task", "Create order header", "--task", "Finish order", "--workflow", "create-order"]) == 0
    out = capsys.readouterr().out
    sid = out.split()[1]
    assert "[ ] 1. Create order header" in out and "Start recording" in out
    assert main(["capture", "plan", sid, "--add", "Add order line", "--at", "2"]) == 0
    assert "[ ] 2. Add order line" in capsys.readouterr().out
    service.apply(store, sid, "start", ifs_origin=ORIGIN, ts=1.0)        # the extension starts recordings
    service.ingest(store, sid, call("1", 2.0, "POST", "/OrderSet", body={"CustomerNo": "5832"}, resp=ORDER))
    assert main(["capture", "complete"]) == 0                              # default: the session recording
    assert "[>] 2. Add order line" in capsys.readouterr().out
    assert main(["capture", "add", "Add charge", "--position", "end"]) == 0
    assert main(["capture", "stop", sid]) == 0
    assert "plan INCOMPLETE" in capsys.readouterr().out
    assert main(["capture", "import", sid, "--package", str(pkg.root)]) == 0
    assert "plan INCOMPLETE" in capsys.readouterr().out
    assert main(["capture", "show", sid, "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["imports"] == [f"recordings/create-order/{sid}.har"]
    assert main(["capture", "complete", sid]) == 1


def test_mcp_tools_plan_and_track_a_recording(store):
    from ifs_agent_ops.mcp_server import server

    created = json.loads(asyncio.run(server.capture_create_session(
        "I want to teach the builder how to create a customer order",
        ["Create order header", "Add order line", "Add charge", "Finish order"], "create-customer-order")))
    sid = created["id"]
    assert "side panel" in created["next_step"]
    plan = json.loads(asyncio.run(server.capture_set_plan(sid, ["Create order header", "Add order line", "Finish order"])))
    assert [t["id"] for t in plan["tasks"]] == ["t1", "t2", "t4"]
    service.apply(store, sid, "start", ifs_origin=ORIGIN, ts=1.0)
    status = json.loads(asyncio.run(server.capture_status()))
    assert status["current_task"] == {"id": "t1", "title": "Create order header"}
    asyncio.run(server.capture_complete_task(sid, "t1"))
    asyncio.run(server.capture_add_task("Add charge", sid))
    assert json.loads(asyncio.run(server.capture_skip_task(sid)))["current_task"]["id"] == "t5"  # added after t2
    stopped = json.loads(asyncio.run(server.capture_stop(sid)))
    assert stopped["status"] == "stopped" and not stopped["progress"]["plan_complete"]
    assert "error" in json.loads(asyncio.run(server.capture_complete_task(sid)))
    listed = json.loads(asyncio.run(server.capture_list_sessions()))
    assert listed["sessions"][0]["id"] == sid
