"""Phase 6: metadata (actions/functions), $batch, snapshots, drafting, identity, validation, approval."""
import json
import re

import pytest
import yaml

from ifs_agent_ops.ifs.metadata.edmx import parse_edmx
from ifs_agent_ops.ifs.metadata import snapshot as snap
from ifs_agent_ops.operations import service
from ifs_agent_ops.operations.draft import parse_path, value_shape
from ifs_agent_ops.operations.store import approve, load_operations, load_workflows, save_operation
from ifs_agent_ops.operations.validate import validate_all
from ifs_agent_ops.recordings import multipart as M
from ifs_agent_ops.recordings.har import batch_parts, calls_from_har
from ifs_agent_ops.recordings.redact import redact_values
from ifs_agent_ops.workspace.package import init_package

NS = "IfsApp.OrderHandling"
EDMX = f"""<?xml version="1.0"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>
<Schema Namespace="{NS}" xmlns="http://docs.oasis-open.org/odata/ns/edm">
 <EnumType Name="OrderState"><Member Name="Planned"/><Member Name="Released"/></EnumType>
 <EntityType Name="Order"><Key><PropertyRef Name="OrderNo"/></Key>
  <Property Name="OrderNo" Type="Edm.String" Nullable="false"/><Property Name="CustomerNo" Type="Edm.String"/>
  <Property Name="Objstate" Type="{NS}.OrderState"/><Property Name="WantedDate" Type="Edm.DateTimeOffset"/>
  <NavigationProperty Name="LinesArray" Type="Collection({NS}.Line)"/></EntityType>
 <EntityType Name="Line"><Key><PropertyRef Name="OrderNo"/><PropertyRef Name="LineNo"/></Key>
  <Property Name="OrderNo" Type="Edm.String" Nullable="false"/><Property Name="LineNo" Type="Edm.String" Nullable="false"/>
  <Property Name="Qty" Type="Edm.Decimal"/></EntityType>
 <ComplexType Name="CheckResult"><Property Name="Ok" Type="Edm.String"/></ComplexType>
 <Action Name="ValidateThing" IsBound="false"><Parameter Name="Flag" Type="Edm.String"/>
  <Parameter Name="OnDate" Type="Edm.Date"/><Parameter Name="Amount" Type="Edm.Decimal"/>
  <Parameter Name="Must" Type="Edm.String" Nullable="false"/><ReturnType Type="{NS}.CheckResult"/></Action>
 <Action Name="Order_Release" IsBound="true"><Parameter Name="it" Type="{NS}.Order"/></Action>
 <Function Name="GetDefaults"><Parameter Name="CustomerNo" Type="Edm.String"/><Parameter Name="TaxCode" Type="Edm.String"/>
  <ReturnType Type="{NS}.CheckResult"/></Function>
 <EntityContainer Name="Main"><EntitySet Name="OrderSet" EntityType="{NS}.Order"/>
  <ActionImport Name="ValidateThing" Action="{NS}.ValidateThing"/>
  <FunctionImport Name="GetDefaults" Function="{NS}.GetDefaults"/></EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>"""
SVC = "https://ifs.example/main/ifsapplications/projection/v1/OrderHandling.svc"
ETAG = 'W/"etag-1"'


def entry(method, path, status=200, body=None, resp=None, headers=None, mime="application/json"):
    req = {"method": method, "url": f"{SVC}/{path}", "headers": headers or []}
    if body is not None:
        req["postData"] = {"mimeType": mime, "text": body if isinstance(body, str) else json.dumps(body)}
    content = {"mimeType": "application/json"}
    if resp is not None:
        content["text"] = resp if isinstance(resp, str) else json.dumps(resp)
    return {"request": req, "response": {"status": status, "headers": [], "content": content}}


def batch_bodies():
    nl = "\r\n"
    req = (f"--b1{nl}Content-Type: multipart/mixed;boundary=c1{nl}{nl}"
           f"--c1{nl}Content-Type: application/http{nl}Content-Transfer-Encoding:binary{nl}Content-ID: 1{nl}{nl}"
           f"POST OrderSet(OrderNo='ORD-1')/LinesArray HTTP/1.1{nl}Content-Type: application/json{nl}{nl}"
           f'{{"OrderNo":"ORD-1","LineNo":"1","Qty":"2.5"}}{nl}--c1--{nl}{nl}--b1--{nl}')
    resp = (f"--r1{nl}Content-Type: multipart/mixed;boundary=d1{nl}{nl}"
            f"--d1{nl}Content-Type: application/http{nl}Content-Transfer-Encoding:binary{nl}{nl}"
            f"HTTP/1.1 201 Created{nl}Content-Type: application/json{nl}{nl}"
            f'{{"OrderNo":"ORD-1","LineNo":"1","Qty":"2.5"}}{nl}--d1--{nl}{nl}--r1--{nl}')
    return req, resp


def sample_har():
    breq, bresp = batch_bodies()
    batch = entry("POST", "$batch", body=breq, resp=bresp, mime="multipart/mixed;boundary=b1")
    batch["response"]["content"]["mimeType"] = "multipart/mixed;boundary=r1"
    order = {"@odata.etag": ETAG, "OrderNo": "ORD-1", "CustomerNo": "C1", "Objstate": "Planned",
             "Objstate@odata.type": f"#{NS}.OrderState"}
    return {"log": {"entries": [
        entry("GET", "GetDefaults(CustomerNo='C1',TaxCode=null)", resp={"Ok": "Y"}),
        entry("GET", "GetDefaults(CustomerNo='C1',TaxCode='T1')", resp={"Ok": "Y"}),
        entry("POST", "OrderSet", 201, body={"CustomerNo": "C1", "WantedDate": "2026-05-17T10:00:00Z"}, resp=order),
        batch,
        entry("PATCH", "OrderSet(OrderNo='ORD-1')", 204, body={"CustomerNo": "C2"}, headers=[{"name": "If-Match", "value": ETAG}]),
        entry("POST", f"OrderSet(OrderNo='ORD-1')/{NS}.Order_Release", 204, headers=[{"name": "If-Match", "value": ETAG}]),
        entry("POST", "ValidateThing", body={"Flag": True, "OnDate": "2026-05-17T00:00:00Z", "Amount": "10.5"},
              resp={"Ok": "N"}),
        entry("GET", "OrderSet?$filter=Objstate%20eq%20'Planned'&$select=OrderNo,Objstate", resp={"value": [order]}),
        entry("GET", "OrderSet(OrderNo='ORD-1')", 404),
        {"request": {"method": "GET", "url": "https://other.example/x", "headers": []},
         "response": {"status": 200, "headers": [], "content": {}}},
    ]}}


@pytest.fixture
def pkg(tmp_path):
    p = init_package(tmp_path / "pkg", "ifs-orders", projections=["OrderHandling"])
    (p.recordings_dir / "create-order").mkdir(parents=True)
    (p.recordings_dir / "create-order" / "full.har").write_text(json.dumps(sample_har()), encoding="utf-8")
    snap.write_snapshot(p, "OrderHandling", metadata_xml=EDMX, openapi_json=None,
                        base_url="https://ifs.example", realm="", source="test")
    return p


# ── metadata ─────────────────────────────────────────────────────────────────

def test_edmx_actions_functions_imports_and_complex_types():
    m = parse_edmx(EDMX)
    act = m.operation_import("ValidateThing")
    assert (act.kind, act.is_bound, act.return_type) == ("action", False, f"{NS}.CheckResult")
    assert [p.name for p in act.call_parameters] == ["Flag", "OnDate", "Amount", "Must"]
    bound = m.bound_operation(f"{NS}.Order_Release", f"{NS}.Order")
    assert bound.is_bound and bound.call_parameters == []
    assert m.operation_import("GetDefaults").kind == "function"
    assert [p.name for p in m.structure(f"{NS}.CheckResult").properties] == ["Ok"]


def test_snapshot_normalizes_host_and_realm_and_refuses_leaks(tmp_path, monkeypatch):
    p = init_package(tmp_path / "p", "ifs-x")
    openapi = json.dumps({"host": "ifs-real.corp.local", "securityDefinitions": {"o": {
        "tokenUrl": "https://ifs-real.corp.local/auth/realms/corprealm/protocol/openid-connect/token"}}})
    target = snap.write_snapshot(p, "OrderHandling", metadata_xml=EDMX, openapi_json=openapi,
                                 base_url="https://IFS-REAL.corp.local", realm="corprealm", source="test")
    text = (target / snap.OPENAPI_FILE).read_text()
    assert "ifs-real" not in text and "corprealm" not in text and "/realms/example/" in text
    assert yaml.safe_load((target / snap.SNAPSHOT_FILE).read_text())["files"]["metadata.xml"]["sha256"]
    monkeypatch.setattr(snap, "normalize", lambda t, h, r: t)
    with pytest.raises(snap.SnapshotRefused, match="real host"):
        snap.write_snapshot(p, "OrderHandling", metadata_xml=EDMX, openapi_json=openapi,
                            base_url="https://ifs-real.corp.local", realm="corprealm", source="test")


# ── $batch ───────────────────────────────────────────────────────────────────

def test_multipart_round_trip_is_lossless_and_parts_pair_with_responses():
    breq, bresp = batch_bodies()
    assert M.render(M.parse(breq)) == breq and M.render(M.parse(bresp)) == bresp
    parts = batch_parts(calls_from_har(sample_har())[3])
    assert [(p.part, p.method, p.status) for p in parts] == [(1, "POST", 201)]
    assert parts[0].url == f"{SVC}/OrderSet(OrderNo='ORD-1')/LinesArray"
    assert json.loads(parts[0].response_body)["LineNo"] == "1"


def test_redaction_keeps_batch_protocol_and_redacts_inside():
    out, mapping = redact_values(sample_har(), seed=b"t")
    body = out["log"]["entries"][3]["request"]["postData"]["text"]
    assert "HTTP/1.1" in body and "Content-Transfer-Encoding:binary" in body and "application/json" in body
    assert "ORD-1" not in body and mapping["ORD-1"] in body


def test_base64_text_bodies_are_cleaned_like_any_other_and_keep_data_flow():
    """The browser extension stores $batch multipart responses base64-encoded (DevTools exports use text).
    Sanitizing, host normalization, redaction and the leak checks must all see inside them."""
    import base64
    from ifs_agent_ops.recordings.importer import _fingerprint
    from ifs_agent_ops.recordings.redact import business_values, normalize_hosts
    from ifs_agent_ops.recordings.sanitize import sanitize_har

    har = sample_har()
    for e in har["log"]["entries"]:
        e["request"]["url"] = e["request"]["url"].replace("ifs.example", "ifs-real.corp.local")
    content = har["log"]["entries"][3]["response"]["content"]
    text = content["text"].replace('{"OrderNo"', '{"@odata.context":"https://ifs-real.corp.local/x","OrderNo"')
    content.update(text=base64.b64encode(text.encode()).decode(), encoding="base64")
    image = {"mimeType": "image/png", "encoding": "base64", "text": base64.b64encode(b"\x89PNG\x00\xff").decode()}
    har["log"]["entries"][9]["response"]["content"] = dict(image)
    assert "ORD-1" in business_values(har)[0]                      # the survivor check sees inside it too

    clean, report = sanitize_har(har)
    assert report.get("binary_body_removed") == 1                   # only the image: not kept at all
    assert clean["log"]["entries"][9]["response"]["content"] == {"mimeType": "image/png", "text": "<binary body removed>"}
    clean, hosts = normalize_hosts(clean)
    out, mapping = redact_values(clean, seed=b"t")
    batch = out["log"]["entries"][3]["response"]["content"]
    assert "encoding" not in batch and "HTTP/1.1 201 Created" in batch["text"]   # stored as text now
    assert "ifs-real" not in json.dumps(out) and "ifs.example" in batch["text"]
    assert "ORD-1" not in batch["text"] and mapping["ORD-1"] in batch["text"]
    assert redact_values(har, seed=b"t")[0]["log"]["entries"][9]["response"]["content"] == image  # redaction alone: untouched
    assert _fingerprint(out)[:2] == _fingerprint(har)[:2]           # same calls and data flow as the original


# ── drafting ─────────────────────────────────────────────────────────────────

def test_parse_path_respects_quotes():
    segs = parse_path("Set(K='a/b)c',N=1)/Nav('it''s')/IfsApp.X.Act")
    assert [s.name for s in segs] == ["Set", "Nav", "IfsApp.X.Act"]
    assert segs[0].keys == [("K", "a/b)c", True), ("N", "1", False)] and segs[1].keys == [(None, "it's", True)]


def test_value_shape_keeps_format_not_value():
    assert value_shape("2026-05-17T10:00:00Z") == "string:9999-99-99T99:99:99Z"
    assert value_shape("TRUE") == "string:'TRUE'" and value_shape("ORD-48151") == "string:aaa-99999"
    assert value_shape(True) == "boolean" and value_shape(2.5) == "number"


def test_draft_covers_every_successful_call_including_batch_parts(pkg):
    report, skipped = service.draft(pkg)
    ops = load_operations(pkg)
    assert set(ops) == {
        "OrderHandling.function.GetDefaults", "OrderHandling.create.OrderSet",
        "OrderHandling.create.OrderSet.LinesArray", "OrderHandling.update.OrderSet",
        "OrderHandling.action.OrderSet.Order_Release", "OrderHandling.action.ValidateThing",
        "OrderHandling.query.OrderSet",
    }
    assert skipped == {"failed call": 1}  # the 404; the non-IFS call is not an IFS call at all
    # one template despite TaxCode=null vs TaxCode='T1' (quoting follows the declared type)
    assert ops["OrderHandling.function.GetDefaults"].path_template == "/GetDefaults(CustomerNo='{CustomerNo}',TaxCode='{TaxCode}')"
    lines = ops["OrderHandling.create.OrderSet.LinesArray"]
    assert lines.entity_type == f"{NS}.Line" and lines.evidence[0].part == 1
    assert lines.path_template == "/OrderSet(OrderNo='{OrderNo}')/LinesArray"
    assert "OrderHandling.create.OrderSet" in lines.depends_on
    assert next(p for p in lines.body if p.name == "Qty").type.edm == "Edm.Decimal"
    update = ops["OrderHandling.update.OrderSet"]
    assert any(h.name == "If-Match" and h.required for h in update.headers)
    release = ops["OrderHandling.action.OrderSet.Order_Release"]
    assert any(h.name == "If-Match" and h.required for h in release.headers)
    query = ops["OrderHandling.query.OrderSet"]
    assert query.query.fields["Objstate"].enum == f"{NS}.OrderState"
    wf = load_workflows(pkg)["create-order.full"]
    line_step = next(s for s in wf.steps if s.operation.endswith("LinesArray"))
    assert any(b.to == "key OrderNo" and b.from_field == "OrderNo" for b in line_step.bindings)


def test_identity_is_stable_and_human_edits_survive(pkg):
    service.draft(pkg)
    op = load_operations(pkg)["OrderHandling.create.OrderSet"]
    save_operation(pkg, op.model_copy(update={"title": "Create customer order header"}))
    before = {k: (v.connector_operation_id, v.revision) for k, v in load_operations(pkg).items()}
    report, _ = service.draft(pkg)
    after = load_operations(pkg)
    assert {k: (v.connector_operation_id, v.revision) for k, v in after.items()} == before
    assert after["OrderHandling.create.OrderSet"].title == "Create customer order header"
    assert not report.created and not report.updated


def test_redraft_bumps_revision_of_drafts_and_reports_drift_for_approved(pkg):
    service.draft(pkg)
    approve(pkg, "OrderHandling.update.OrderSet")
    har = sample_har()
    har["log"]["entries"][2]["request"]["postData"]["text"] = json.dumps(
        {"CustomerNo": "C1", "WantedDate": "2026-05-17T10:00:00Z", "Objstate": "Planned"})
    har["log"]["entries"][4]["request"]["postData"]["text"] = json.dumps({"CustomerNo": "C3", "WantedDate": "2026-05-18T08:00:00"})
    (pkg.recordings_dir / "create-order" / "full.har").write_text(json.dumps(har), encoding="utf-8")
    report, _ = service.draft(pkg)
    ops = load_operations(pkg)
    assert ops["OrderHandling.create.OrderSet"].revision == 2
    assert "OrderHandling.update.OrderSet" in report.drifted
    assert ops["OrderHandling.update.OrderSet"].status == "approved"
    assert len(ops["OrderHandling.update.OrderSet"].body) == 1  # approved operation left untouched

    # a human redrafts the drifted operation: new content, next revision, back to draft, human text kept
    op = ops["OrderHandling.update.OrderSet"]
    save_operation(pkg, op.model_copy(update={"title": "Update an order", "review_notes": ["kept"]}))
    with pytest.raises(ValueError, match="drafting already updates it"):
        service.redraft(pkg, "OrderHandling.create.OrderSet")          # a draft: plain drafting covers it
    redrafted = service.redraft(pkg, "OrderHandling.update.OrderSet")
    assert redrafted.status == "draft" and redrafted.approval is None and redrafted.revision == op.revision + 1
    assert len(redrafted.body) == 2 and redrafted.title == "Update an order" and redrafted.review_notes == ["kept"]
    assert redrafted.connector_operation_id == op.connector_operation_id
    approve(pkg, "OrderHandling.update.OrderSet")
    with pytest.raises(ValueError, match="has not drifted"):
        service.redraft(pkg, "OrderHandling.update.OrderSet")


# ── validation and approval ─────────────────────────────────────────────────

def test_validators_flag_ifs_traps(pkg):
    service.draft(pkg)
    rules = {(f.operation.split(".", 1)[1], f.rule, f.severity) for f in validate_all(load_operations(pkg))}
    assert ("action.ValidateThing", "string-typed-flag", "error") in rules
    assert ("action.ValidateThing", "date-only", "error") in rules
    assert ("action.ValidateThing", "numeric-as-string", "info") in rules
    assert ("action.ValidateThing", "required-not-observed", "warning") in rules
    assert ("query.OrderSet", "enum-filter-literal", "error") in rules
    assert ("create.OrderSet", "write-datetime-format", "info") in rules
    assert not any(r[1] == "if-match-required" for r in rules)


def test_approval_is_refused_while_errors_exist(pkg, monkeypatch, capsys):
    from ifs_agent_ops.cli.main import main

    from ifs_agent_ops.cli import main as cli

    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    assert main(["operations", "draft", "--package", "ifs-orders"]) == 0
    assert main(["operations", "approve", "OrderHandling.action.ValidateThing", "--package", "ifs-orders"]) == 1
    assert "refused" in capsys.readouterr().err
    # no interactive terminal (automation, agents, pipes): refused even for a valid operation
    assert main(["operations", "approve", "OrderHandling.create.OrderSet", "--package", "ifs-orders"]) == 1
    assert "interactive terminal" in capsys.readouterr().err
    answers = iter(["C0DE42"])
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirmation_code", lambda: "C0DE42")
    monkeypatch.setattr(cli, "_ask", lambda prompt: next(answers))
    assert main(["operations", "approve", "OrderHandling.create.OrderSet", "--package", "ifs-orders"]) == 0
    op = load_operations(pkg)["OrderHandling.create.OrderSet"]
    assert op.status == "approved" and op.approval.revision == op.revision
    assert op.approval.approved_by == "Pat Reviewer"
    assert main(["operations", "validate", "--package", "ifs-orders"]) == 1  # errors remain elsewhere


def test_mcp_operation_tools(pkg, monkeypatch):
    import asyncio

    from ifs_agent_ops.mcp_server.tools import operations as tools

    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    assert "7 operation(s)" in asyncio.run(tools.draft("ifs-orders"))
    assert "OrderHandling.query.OrderSet" in asyncio.run(tools.list_operations("ifs-orders"))
    shown = asyncio.run(tools.get_operation("ifs-orders", "ValidateThing"))
    assert "path_template: /ValidateThing" in shown and "# error: string-typed-flag" in shown
    assert asyncio.run(tools.get_operation("ifs-orders", "OrderSet")).startswith("error: ")  # ambiguous


def test_missing_snapshot_is_reported(tmp_path):
    p = init_package(tmp_path / "pkg", "ifs-orders", projections=["OrderHandling"])
    (p.recordings_dir / "w").mkdir(parents=True)
    (p.recordings_dir / "w" / "t.har").write_text(json.dumps(sample_har()), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=re.escape("metadata snapshot --package ifs-orders")):
        service.draft(p)


def test_json_schema_is_exported():
    from ifs_agent_ops.operations.model import json_schema

    schema = json_schema()
    assert "path_template" in schema["operation"]["properties"] and "steps" in schema["workflow"]["properties"]
