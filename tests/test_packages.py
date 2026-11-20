"""Phase 5: packages, discovery, host normalisation, value redaction and the import boundary."""
import json
import re

import pytest

from ifs_agent_ops.analysis.sequence import build_sequence
from ifs_agent_ops.recordings import importer as imp
from ifs_agent_ops.recordings.har import body_json, calls_from_har
from ifs_agent_ops.recordings.redact import normalize_hosts, redact_values
from ifs_agent_ops.workspace import discovery
from ifs_agent_ops.workspace.package import PackageError, init_package, load_package

HOST = "ifs-internal.corp.local"
SVC = f"https://{HOST}/main/ifsapplications/projection/v1/CustomerOrderHandling.svc"
ORDER, CUST, LINE = "ORD-48151", "C7734", 4711
ETAG = 'W/"Vy8iQUFBVkJ1QUFBQUFBQUFBOjEi"'
DT_Z, DT_LOCAL = "2026-05-17T10:00:00Z", "2026-05-18T08:30:00"


def e(method, url, status=200, body=None, resp=None, headers=None):
    req = {"method": method, "url": url, "headers": headers or [], "queryString": [], "cookies": []}
    if body is not None:
        req["postData"] = {"mimeType": "application/json", "text": json.dumps(body)}
    content = {"mimeType": "application/json"}
    if resp is not None:
        content["text"] = resp if isinstance(resp, str) else json.dumps(resp)
    return {"request": req, "response": {"status": status, "headers": [], "content": content, "cookies": []},
            "serverIPAddress": "10.20.30.40"}


def envelope(payload, location=None):
    headers = {"Content-Type": "application/json"}
    if location:
        headers["Location"] = location
        headers["OData-EntityID"] = location
    return {"request": {"method": "POST", "uri": f"/CustomerOrderSet?$filter=CustomerNo eq '{CUST}'", "protocol": "HTTP/1.1"},
            "response": {"status": {"code": "201", "info": "Created"}, "headers": headers, "body": json.dumps(payload)},
            "server": {"version": "23.1.4.0", "environment": {"authType": "BEARER", "serverName": HOST}},
            "ifs-trace": {"authStatus": "authorized", "trace": [
                {"type": "Application", "text": f"Customer_Order_API.New__ order {ORDER} for {CUST}"}]}}


def sample_har():
    order = {"@odata.context": f"https://{HOST}/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/$metadata#CustomerOrderSet(OrderNo='{ORDER}')",
             "@odata.etag": ETAG, "OrderNo": ORDER, "CustomerNo": CUST, "Objstate": "Planned",
             "Objstate@odata.type": "#IfsApp.CustomerOrderHandling.CustomerOrderState",
             "WantedDeliveryDate": DT_Z, "luname": "CustomerOrder",
             "keyref": f"ORDER_NO={ORDER}^",
             "LinesArray@odata.navigationLink": f"CustomerOrderSet(OrderNo='{ORDER}')/LinesArray",
             "Qty": 12.5, "Lines": LINE}
    return {"log": {"version": "1.2", "pages": [{"id": "p", "title": f"Order {ORDER}"}], "entries": [
        e("GET", f"{SVC}/Reference_CustOrdCust7?$filter=CustomerNo%20eq%20%27{CUST}%27",
          resp={"value": [{"CustomerNo": CUST, "Name": "Acme Widgets Ltd", "Email": "buyer@acme.example"}]},
          headers=[{"name": "Referer", "value": f"https://{HOST}/main/ifsapplications/web/page/CustomerOrder/Form;$filter=CustomerNo%20eq%20%27{CUST}%27"}]),
        e("POST", f"{SVC}/CustomerOrderSet", 201, body={"CustomerNo": CUST, "WantedDeliveryDate": DT_LOCAL},
          resp=envelope(order, location=f"{SVC}/CustomerOrderSet(OrderNo='{ORDER}')"),
          headers=[{"name": ":path", "value": "/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet"}]),
        e("PATCH", f"{SVC}/CustomerOrderSet(OrderNo='{ORDER}')", 204,
          body={"Objstate": "Planned", "Lines": LINE, "Qty": 12.5},
          headers=[{"name": "If-Match", "value": ETAG}]),
        e("GET", f"{SVC}/CustomerOrderSet(OrderNo='{ORDER}')?$expand=LinesArray($filter=PartNo%20eq%20%27P-100%27)&$select=OrderNo",
          resp={"OrderNo": ORDER}),
        e("POST", f"{SVC}/CustomerOrderSet(KeyRef='ORDER_NO={ORDER}^')/IfsApp.CustomerOrderHandling.CustomerOrder_Release",
          resp={"value": "OK"}),
    ]}}


SENSITIVE = [HOST, ORDER, CUST, "Acme Widgets Ltd", "buyer@acme.example", "P-100", "10.20.30.40"]


def links(har):
    return [(l.to_index, l.to_place, l.from_index, l.from_field) for l in build_sequence(calls_from_har(har))[1]]


# ── packages ─────────────────────────────────────────────────────────────────

def test_init_creates_layout_and_refuses_to_overwrite(tmp_path):
    p = init_package(tmp_path / "pkg", "ifs-demo", description="Demo", projections=["DemoHandling"])
    assert (p.root / "package.yaml").exists() and p.skills_dir.is_dir() and p.recordings_dir.is_dir()
    assert "*.raw.har" in (p.root / ".gitignore").read_text()
    assert ".ifs-agent-ops/" in (p.root / ".gitignore").read_text().splitlines()
    assert p.display_name == "ifs-demo"  # defaults to the permanent name
    assert (p.name, p.version, p.projections, p.redact_values_default) == ("ifs-demo", "0.1.0", ["DemoHandling"], True)
    with pytest.raises(PackageError, match="already contains"):
        init_package(tmp_path / "pkg", "ifs-demo")


@pytest.mark.parametrize("manifest,match", [
    ("name: Bad Name\nversion: 1\n", "'name' must be"),
    ("name: ok\n", "'version' is required"),
    ("name: ok\nversion: 1\nprojection: X\n", "unknown key"),
    ("name: ok\nversion: 1\nprojections: X\n", "must be a list"),
])
def test_manifest_validation(tmp_path, manifest, match):
    (tmp_path / "package.yaml").write_text(manifest, encoding="utf-8")
    with pytest.raises(PackageError, match=match):
        load_package(tmp_path)


def test_display_name_is_renameable_while_name_stays(tmp_path):
    p = init_package(tmp_path / "pkg", "ifs-orders", display_name="Customer Orders")
    assert (p.name, p.display_name) == ("ifs-orders", "Customer Orders")
    manifest = p.root / "package.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("Customer Orders", "Customer Order Handling"),
                        encoding="utf-8")
    renamed = load_package(p.root)
    assert (renamed.name, renamed.display_name) == ("ifs-orders", "Customer Order Handling")


def test_discovery_from_parent_and_direct_paths(tmp_path, monkeypatch):
    a = init_package(tmp_path / "all" / "a", "pkg-a")
    init_package(tmp_path / "all" / "b", "pkg-b")
    dup = init_package(tmp_path / "other" / "a-copy", "pkg-a")
    monkeypatch.setenv(discovery.ENV_VAR, ";".join([str(tmp_path / "all"), str(dup.root), str(tmp_path / "missing")]))
    monkeypatch.setattr(discovery.os, "pathsep", ";")
    packages, problems = discovery.discover_packages()
    assert [p.name for p in packages] == ["pkg-a", "pkg-b"]
    assert packages[0].root == a.root
    assert any("Duplicate package name 'pkg-a'" in x for x in problems)
    assert any("does not exist" in x for x in problems)
    assert discovery.find_package("pkg-b").name == "pkg-b"
    with pytest.raises(PackageError, match="not found"):
        discovery.find_package("pkg-z")


def test_skills_dir_overrides_package_skill_with_same_name(tmp_path, monkeypatch):
    from ifs_agent_ops.mcp_server.resources.index import get_resources

    p = init_package(tmp_path / "p", "pkg")
    (p.skills_dir / "ifs-x.md").write_text("# From package\n\nPackage version.", encoding="utf-8")
    (p.skills_dir / "ifs-y.md").write_text("# Y\n\nOnly in package.", encoding="utf-8")
    override = tmp_path / "skills"
    override.mkdir()
    (override / "ifs-x.md").write_text("# From SKILLS_DIR\n\nOverride.", encoding="utf-8")
    monkeypatch.setenv(discovery.ENV_VAR, str(p.root))
    monkeypatch.setenv("SKILLS_DIR", str(override))
    names = {str(r.definition.uri): r.definition.name for r in get_resources()}
    assert names["ifs://ifs-x/guide"] == "From SKILLS_DIR" and names["ifs://ifs-y/guide"] == "Y"
    assert list(names)[-2:] == ["ifs://ifs-common-odata-reference/guide", "ifs://ifs-skill-authoring/guide"]


# ── host normalisation ───────────────────────────────────────────────────────

def test_hosts_become_placeholders_everywhere():
    har = sample_har()
    har["log"]["entries"].append(e("GET", "https://cdn.vendor.example/app.js"))
    out, mapping = normalize_hosts(har)
    text = json.dumps(out)
    assert mapping == {HOST: "ifs.example", "cdn.vendor.example": "external-1.example"}
    assert HOST not in text and HOST.upper() not in text and "ifs.example" in text
    assert har["log"]["entries"][0]["request"]["url"].startswith(f"https://{HOST}")  # input untouched


# ── value redaction ──────────────────────────────────────────────────────────

def redacted():
    clean, _ = normalize_hosts(sample_har())
    return redact_values(clean, seed=b"test")


def test_redaction_removes_business_values_everywhere():
    out, mapping = redacted()
    text = json.dumps(out, ensure_ascii=False)
    for value in [ORDER, CUST, "Acme Widgets Ltd", "buyer@acme.example", "P-100"]:
        assert value not in text, f"{value!r} survived"
        assert value in mapping


def test_redaction_keeps_identity_shape_types_and_structure():
    out, m = redacted()
    assert re.fullmatch(r"[A-Z]{3}-\d{5}", m[ORDER]) and re.fullmatch(r"[A-Z]\d{4}", m[CUST])
    assert set(m.values()).isdisjoint(m.keys())  # no pseudo-value equals any original
    calls = calls_from_har(out)
    order = json.loads(json.loads(calls[1].response_body)["response"]["body"])
    assert order["OrderNo"] == m[ORDER] and order["CustomerNo"] == m[CUST]
    assert order["Objstate"] == "Planned" and order["luname"] == "CustomerOrder"        # enum / LU kept
    assert order["@odata.etag"] == ETAG                                                    # ETag kept
    assert order["WantedDeliveryDate"].endswith("Z") and "T" in order["WantedDeliveryDate"]  # format kept
    assert order["keyref"] == f"ORDER_NO={m[ORDER]}^"                                       # column kept
    assert isinstance(order["Qty"], float) and isinstance(order["Lines"], int)              # JSON types kept
    assert m[ORDER] in order["@odata.context"] and m[ORDER] in order["LinesArray@odata.navigationLink"]
    patch_body = json.loads(calls[2].request_body)
    assert patch_body["Objstate"] == "Planned"  # enum echoed in a request body stays linkable
    post_body = json.loads(calls[1].request_body)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", post_body["WantedDeliveryDate"])  # no Z, as sent
    assert "IfsApp.CustomerOrderHandling.CustomerOrder_Release" in calls[4].url


def test_redaction_covers_urls_headers_fragments_and_trace():
    out, m = redacted()
    entries = out["log"]["entries"]
    referer = entries[0]["request"]["headers"][0]["value"]
    assert m[CUST] in referer and CUST not in referer
    env = json.loads(entries[1]["response"]["content"]["text"])
    assert m[ORDER] in env["response"]["headers"]["Location"] and ORDER not in json.dumps(env)
    assert f"order {m[ORDER]} for {m[CUST]}" in env["ifs-trace"]["trace"][0]["text"]
    assert "$expand=LinesArray($filter=PartNo" in entries[3]["request"]["url"].replace("%20", " ")
    assert out["log"]["pages"][0]["title"] == f"Order {m[ORDER]}"


def test_redaction_preserves_every_data_flow_link():
    raw = sample_har()
    out, _ = redacted()
    assert links(out) == links(raw)
    assert (2, "If-Match", 1, "@odata.etag") in links(raw)
    assert (4, "key KeyRef", 1, "keyref") in links(raw)


# ── import boundary ──────────────────────────────────────────────────────────

@pytest.fixture
def raw_file(tmp_path):
    path = tmp_path / "raw" / "order.har"
    path.parent.mkdir()
    path.write_text(json.dumps(sample_har()), encoding="utf-8")
    return path


def test_import_writes_repository_safe_evidence(tmp_path, raw_file):
    pkg = init_package(tmp_path / "pkg", "ifs-orders")
    r = imp.import_recording(raw_file, pkg, "create-order", "header", description="Create header")
    text = r.recording.read_text(encoding="utf-8")
    assert not [s for s in SENSITIVE if s in text]
    assert r.residual_value_matches == 0 and r.values_redacted and r.data_flow_links == len(links(sample_har()))
    notes = r.annotations.read_text(encoding="utf-8")
    assert "values_redacted: true" in notes and "ifs.example" in notes and HOST not in notes
    assert str(raw_file.parent) not in notes  # only the file name of the source is recorded
    assert not list(pkg.root.rglob("order.har"))  # raw input never copied in


def test_import_keep_values_still_sanitizes_and_normalizes(tmp_path, raw_file):
    pkg = init_package(tmp_path / "pkg", "ifs-orders")
    r = imp.import_recording(raw_file, pkg, "wf", "t", redact=False)
    text = r.recording.read_text(encoding="utf-8")
    assert ORDER in text and HOST not in text and "10.20.30.40" not in text and not r.values_redacted


def test_import_refuses_overwrite_and_unfaithful_output(tmp_path, raw_file, monkeypatch):
    pkg = init_package(tmp_path / "pkg", "ifs-orders")
    imp.import_recording(raw_file, pkg, "wf", "t")
    with pytest.raises(imp.ImportRefused, match="already exists"):
        imp.import_recording(raw_file, pkg, "wf", "t")

    def breaks_identity(har, seed):  # a redactor that maps the same value differently each time
        out, mapping = redact_values(har, seed)
        body = out["log"]["entries"][2]["request"]["postData"]
        body["text"] = body["text"].replace(str(mapping.get(str(LINE), LINE)), "9999")
        out["log"]["entries"][2]["request"]["url"] = out["log"]["entries"][2]["request"]["url"].replace("'", "'X", 1)
        return out, mapping

    monkeypatch.setattr(imp, "redact_values", breaks_identity)
    with pytest.raises(imp.ImportRefused, match="changed data flow|call sequence"):
        imp.import_recording(raw_file, pkg, "wf", "t2")
    assert not (pkg.recordings_dir / "wf" / "t2.har").exists()


def test_import_refuses_when_a_real_host_survives(tmp_path, raw_file, monkeypatch):
    pkg = init_package(tmp_path / "pkg", "ifs-orders")
    monkeypatch.setattr(imp, "normalize_hosts", lambda har: (har, {HOST: "ifs.example"}))
    with pytest.raises(imp.ImportRefused, match="real host"):
        imp.import_recording(raw_file, pkg, "wf", "t", redact=False)


def test_cli_record_import_and_package_list(tmp_path, raw_file, monkeypatch, capsys):
    from ifs_agent_ops.cli.main import main

    assert main(["package", "init", str(tmp_path / "pkg"), "--name", "ifs-orders", "--projection", "CustomerOrderHandling"]) == 0
    monkeypatch.setenv(discovery.ENV_VAR, str(tmp_path / "pkg"))
    assert main(["record", "import", str(raw_file), "--package", "ifs-orders", "--workflow", "create-order",
                 "--task", "header"]) == 0
    out = capsys.readouterr().out
    assert "data-flow links" in out and "verified unchanged" in out and "values redacted: yes" in out
    assert main(["package", "list"]) == 0
    assert "recordings=1" in capsys.readouterr().out
    assert main(["record", "import", str(raw_file), "--package", "nope", "--workflow", "w", "--task", "t"]) == 2
