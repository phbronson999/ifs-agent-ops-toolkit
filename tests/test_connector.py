"""Phase 9: the Power Platform connector generated from approved capabilities (plan §5a C1–C11)."""
import copy
import json

import pytest
import yaml

from ifs_agent_ops.generators.connector import (DEFINITION_FILE, MAP_FILE, REPORT_FILE, compat, connector_dir,
                                                generate, validate)
from ifs_agent_ops.generators.connector.select import match_spec
from ifs_agent_ops.generators.gating import GenerationRefused
from ifs_agent_ops.ifs.metadata import snapshot as snap
from ifs_agent_ops.ifs.metadata.edmx import parse_edmx
from ifs_agent_ops.ifs.paths import PROJECTION_ROOT
from ifs_agent_ops.operations.capability_store import caps_dir
from ifs_agent_ops.operations.model import Operation, PathParameter, Placement, TypeRef
from ifs_agent_ops.operations.store import load_operations, save_operation
from test_capabilities import USED
from test_operations import EDMX, NS, pkg  # noqa: F401
from test_skills import approved  # noqa: F401

LINK = ", see [OData Filtering](http://docs.oasis-open.org/odata)"


def spec() -> dict:
    ref = lambda kind, name: {"$ref": f"#/{kind}/{name}"}  # noqa: E731
    return {
        "swagger": "2.0", "info": {"title": "OrderHandling", "description": "IFS UI text", "version": "1"},
        "host": "ifs.example", "basePath": f"{PROJECTION_ROOT}/OrderHandling.svc",
        "tags": [{"name": "OrderSet"}, {"name": "Functions"}, {"name": "Unused"}],
        "paths": {
            "/OrderSet": {
                "get": {"tags": ["OrderSet"], "operationId": "GetOrderSet", "parameters": [
                    ref("parameters", "top"),
                    {"name": "$filter", "in": "query", "type": "string", "description": "Filter items" + LINK},
                    {"name": "$select", "in": "query", "type": "array", "collectionFormat": "csv",
                     "items": {"type": "string", "enum": ["luname", "keyref", "OrderNo", "Objstate"]}},
                    {"name": "$orderby", "in": "query", "type": "array",
                     "items": {"type": "string", "enum": ["OrderNo", "OrderNo desc"]}}],
                    "responses": {"200": ref("responses", "OrderarrayResponse")}},
                "post": {"tags": ["OrderSet"], "operationId": "CreateOrderSet", "parameters": [
                    {"in": "body", "name": "Order-InsertBodyParam", "required": True,
                     "schema": ref("definitions", "Order-Insert")}],
                    "responses": {"201": ref("responses", "OrderResponse")}}},
            "/OrderSet(OrderNo='{OrderNo}')/LinesArray": {
                "post": {"tags": ["OrderSet"], "operationId": "CreateLine", "parameters": [
                    {"name": "OrderNo", "in": "path", "required": True, "type": "string"},
                    {"in": "body", "name": "Line-InsertBodyParam", "required": True, "schema": ref("definitions", "Line")}],
                    "responses": {"201": ref("responses", "LineResponse")}}},
            "/GetDefaults(CustomerNo='{CustomerNo}',TaxCode='{TaxCode}')": {
                "get": {"tags": ["Functions"], "operationId": "OrderHandling.GetDefaults_CustomerNo_TaxCode",
                        "parameters": [{"name": "CustomerNo", "in": "path", "required": True, "type": "string"},
                                       {"name": "TaxCode", "in": "path", "required": True, "type": "string"}],
                        "responses": {"200": ref("responses", "CheckResultResponse")}}},
            "/Unused": {"get": {"tags": ["Unused"], "operationId": "U",
                                "responses": {"200": ref("responses", "UnusedResponse")}}},
        },
        "definitions": {
            "Order": {"properties": {"OrderNo": {"type": "string"},
                                     "LinesArray": {"type": "array", "items": ref("definitions", "Line")}}},
            "Order-Insert": {"properties": {"OrderNo": {"type": "string"}}},
            "Line": {"properties": {"LineNo": {"type": "string"}}},
            "CheckResult": {"properties": {"Ok": {"type": "string"}}},
            "UnusedDef": {"properties": {"X": {"type": "string"}}},
        },
        "parameters": {
            "top": {"name": "$top", "in": "query", "type": "integer", "description": "Show the first n" + LINK},
            "skip": {"name": "$skip", "in": "query", "type": "integer"},
        },
        "responses": {
            "OrderarrayResponse": {"description": "r", "schema": {"properties": {
                "value": {"type": "array", "items": ref("definitions", "Order")}}}},
            "OrderResponse": {"description": "r", "schema": ref("definitions", "Order")},
            "LineResponse": {"description": "r", "schema": ref("definitions", "Line")},
            "CheckResultResponse": {"description": "r", "schema": ref("definitions", "CheckResult")},
            "UnusedResponse": {"description": "r", "schema": ref("definitions", "UnusedDef")},
        },
    }


def _pin(pkg, document):  # noqa: F811
    snap.write_snapshot(pkg, "OrderHandling", metadata_xml=EDMX, openapi_json=json.dumps(document),
                        base_url="https://ifs-test.local", realm="", source="test")


@pytest.fixture
def pinned(approved):  # noqa: F811
    _pin(approved, spec())
    return approved


def _definition(pkg):  # noqa: F811
    return json.loads((connector_dir(pkg) / DEFINITION_FILE).read_text(encoding="utf-8"))


def test_connector_contains_only_capability_operations_with_their_dependencies(pinned):
    gen = generate(pinned)
    assert gen.ok and [r.action for r in gen.results] == ["created"] * 4
    doc = _definition(pinned)
    ops = load_operations(pinned)
    assert sorted(o["operationId"] for item in doc["paths"].values() for o in item.values()) == \
        sorted(ops[i].connector_operation_id for i in USED)
    assert (doc["host"], doc["basePath"]) == ("ifs.example", PROJECTION_ROOT)
    assert all(p.startswith("/OrderHandling.svc/") for p in doc["paths"])
    # C7: closure kept, everything else dropped
    assert sorted(doc["definitions"]) == ["CheckResult", "Line", "Order", "Order-Insert"]
    assert sorted(doc["parameters"]) == ["top"] and "UnusedResponse" not in doc["responses"]
    assert [t["name"] for t in doc["tags"]] == ["OrderSet", "Functions"]
    assert "securityDefinitions" not in doc  # connection auth is deployment state (Phase 10)
    # C4 from the manifest, C11 compact UTF-8
    assert 30 <= len(doc["info"]["description"]) <= 500 and "create-order" in doc["info"]["description"]
    raw = (connector_dir(pinned) / DEFINITION_FILE).read_text(encoding="utf-8")
    assert raw == validate.compact(doc) + "\n"


def test_function_parameters_are_literals_and_reads_are_annotated(pinned):
    generate(pinned)
    doc = _definition(pinned)
    fn_path = "/OrderHandling.svc/GetDefaults(CustomerNo={CustomerNo},TaxCode={TaxCode})"
    params = {p["name"]: p for p in doc["paths"][fn_path]["get"]["parameters"]}
    assert "or null" in params["CustomerNo"]["description"] and params["CustomerNo"]["x-ms-summary"] == "CustomerNo"
    # entity keys keep the quotes; the caller sends the bare value
    assert "/OrderHandling.svc/OrderSet(OrderNo='{OrderNo}')/LinesArray" in doc["paths"]
    query = {p.get("name", p.get("$ref")): p for p in doc["paths"]["/OrderHandling.svc/OrderSet"]["get"]["parameters"]}
    assert all(p.get("type") != "array" for p in query.values())                        # C3
    assert "AVAILABLE FIELDS: OrderNo, CustomerNo, Objstate" in query["$select"]["description"]   # C9
    assert "luname" not in query["$select"]["description"]
    assert f"Objstate eq {NS}.OrderState'<Member>'; members: Planned, Released" in query["$filter"]["description"]  # C8
    assert "OData Filtering" not in json.dumps(doc) and "Values include: OrderNo, OrderNo desc" in \
        query["$orderby"]["description"]
    create = doc["paths"]["/OrderHandling.svc/OrderSet(OrderNo='{OrderNo}')/LinesArray"]["post"]
    assert [p["in"] for p in create["parameters"]] == ["path", "body"]


def test_generation_is_deterministic_and_checkable(pinned):
    generate(pinned)
    assert [r.action for r in generate(pinned).results] == ["unchanged"] * 4
    assert [r.action for r in generate(pinned, check=True).results] == ["unchanged"] * 4
    path = connector_dir(pinned) / DEFINITION_FILE
    path.write_text("{}", encoding="utf-8")
    assert "would-update" in [r.action for r in generate(pinned, check=True).results]
    assert path.read_text(encoding="utf-8") == "{}"


def test_report_and_operation_id_map(pinned):
    generate(pinned)
    report = (connector_dir(pinned) / REPORT_FILE).read_text(encoding="utf-8")
    for expected in ("not deployment-validated", "## Size (C10)", "create-order",
                     "placeholder `ifs.example`", "## Provenance", "openapi.v2.json sha256"):
        assert expected in report, expected
    data = yaml.safe_load((connector_dir(pinned) / MAP_FILE).read_text(encoding="utf-8"))
    ops = load_operations(pinned)
    assert {k: v["operation_id"] for k, v in data["operations"].items()} == \
        {i: ops[i].connector_operation_id for i in USED}
    assert data["retired"] == []


def test_operation_ids_never_change_and_are_retired_not_reused(pinned):
    generate(pinned)
    map_path = connector_dir(pinned) / MAP_FILE
    text = map_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    data["operations"]["OrderHandling.query.Gone"] = {"operation_id": "query_Gone", "method": "GET", "path": "/x",
                                                      "revision": 1}
    map_path.write_text(text.split("package:")[0] + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    gen = generate(pinned)
    assert gen.ok and yaml.safe_load(map_path.read_text(encoding="utf-8"))["retired"] == ["query_Gone"]

    op = load_operations(pinned)[USED[1]]
    save_operation(pinned, op.model_copy(update={"connector_operation_id": "renamed_GetDefaults"}))
    gen = generate(pinned)
    assert not gen.ok and "operationId would change" in gen.errors[0]
    save_operation(pinned, op.model_copy(update={"connector_operation_id": "query_Gone"}))
    assert "retired and cannot be reused" in generate(pinned).errors[0]


def test_only_current_approvals_and_pinned_metadata_are_used(pinned):
    path = caps_dir(pinned) / "create-order.yaml"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("status: approved", "status: draft", 1), encoding="utf-8")
    assert "no approved capability" in generate(pinned).errors[0]
    path.write_text(original, encoding="utf-8")

    op = load_operations(pinned)[USED[0]]
    save_operation(pinned, op.model_copy(update={"query": op.query.model_copy(update={"top": 7})}))
    assert not generate(pinned).ok
    save_operation(pinned, op)

    openapi = snap.snapshot_dir(pinned, "OrderHandling") / snap.OPENAPI_FILE
    openapi.write_text(openapi.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert "does not match its snapshot.yaml hash" in generate(pinned).errors[0]


def test_operation_missing_from_spec_is_refused(approved):  # noqa: F811
    document = spec()
    del document["paths"]["/GetDefaults(CustomerNo='{CustomerNo}',TaxCode='{TaxCode}')"]
    _pin(approved, document)
    gen = generate(approved)
    assert not gen.ok and "is not in the pinned $openapi" in gen.errors[0]
    assert not (connector_dir(approved) / DEFINITION_FILE).exists()


def test_hand_written_connector_is_not_overwritten(pinned):
    folder = connector_dir(pinned)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / DEFINITION_FILE).write_text('{"hand": "made"}', encoding="utf-8")
    gen = generate(pinned)
    assert not gen.ok and "was not generated" in gen.errors[0]
    assert (folder / DEFINITION_FILE).read_text(encoding="utf-8") == '{"hand": "made"}'


def test_size_limit_fails_with_diagnostic(pinned):
    gen = generate(pinned, max_bytes=500)
    assert not gen.ok and "C10" in gen.errors[0] and "over the 500-byte limit" in gen.errors[0]
    assert "largest paths" in gen.errors[0] and not (connector_dir(pinned) / DEFINITION_FILE).exists()


def test_cli_connector_generate(pinned, capsys):
    from ifs_agent_ops.cli.main import main

    assert main(["connector", "generate", "--package", "ifs-orders", "--check"]) == 1
    assert "would-create" in capsys.readouterr().out
    assert main(["connector", "generate", "--package", "ifs-orders", "--output", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and len(data["operations"]) == 4 and data["size"]["limit"] == 1_000_000
    assert main(["connector", "generate", "--package", "ifs-orders", "--check"]) == 0


# ── units ────────────────────────────────────────────────────────────────────

def _op(path: str, kind: str = "function", params=(), **extra) -> Operation:
    return Operation(id=f"P.{kind}.X", connector_operation_id=f"{kind}_X", projection="P", kind=kind,
                     method="GET", path_template=path, target="X", path_parameters=list(params), **extra)


def test_c1_repeated_keys_are_renamed_in_path_order():
    key = PathParameter(name="OrderNo", type=TypeRef(edm="Edm.String"), placements=[
        Placement(segment="OrderSet", key="OrderNo", quoted=True),
        Placement(segment="LinesArray", key="OrderNo", quoted=True)])
    line = PathParameter(name="LineItemNo", type=TypeRef(edm="Edm.Int32"),
                         placements=[Placement(segment="LinesArray", key="LineItemNo", quoted=False)])
    op = _op("/OrderSet(OrderNo='{OrderNo}')/LinesArray(OrderNo='{OrderNo}',LineItemNo={LineItemNo})",
             kind="read", params=[key, line])
    rendered = compat.render_path(op)
    assert rendered.path == "/OrderSet(OrderNo='{OrderNo}')/LinesArray(OrderNo='{OrderNo_2}',LineItemNo={LineItemNo})"
    assert rendered.renamed == ["OrderNo -> OrderNo_2"]
    params = [compat.path_parameter(p, {"OrderNo": key.type, "LineItemNo": line.type}[p.logical])
              for p in rendered.params]
    assert params[1]["x-ms-summary"] == "OrderNo" and "Same value as OrderNo" in params[1]["description"]
    assert params[2]["type"] == "integer"  # C1 fix: unquoted integer keys keep their type


def test_function_literals_aliases_and_enum_descriptions():
    enum = TypeRef(edm=f"{NS}.Country", enum=f"{NS}.Country", members=[f"C{i}" for i in range(25)])
    op = _op("/Fn(A='{A}',B={B},C=@C)", params=[
        PathParameter(name="A", placements=[Placement(segment="Fn", key="A", quoted=True)]),
        PathParameter(name="B", type=enum, placements=[Placement(segment="Fn", key="B", quoted=False)])])
    rendered = compat.render_path(op)
    assert rendered.path == "/Fn(A={A},B={B},C=@C)" and rendered.aliases == ["C"]
    desc = compat.path_parameter(rendered.params[1], enum)["description"]
    assert f"{NS}.Country'<Member>', or null" in desc and "(and 5 more" in desc


def test_c5_operation_ids_are_shortened_deterministically():
    op = _op("/X").model_copy(update={"connector_operation_id": "a" * 400})
    oid = compat.operation_id(op)
    assert len(oid) == compat.MAX_OPERATION_ID and oid == compat.operation_id(op) and oid[-9] == "_"


def test_c4_description_is_bounded_without_cutting_words():
    desc = compat.info_description("word " * 150, ["c"], ["P"])
    assert len(desc) <= 500 and desc.endswith("word...")


def test_validator_reports_each_problem():
    doc = {"info": {"description": "short"}, "paths": {
        "/A({K})": {"get": {"operationId": "a", "parameters": [
            {"name": "Q", "in": "query", "type": "array"}, {"$ref": "#/parameters/missing"}]}},
        "/B": {"get": {"operationId": "a", "parameters": [{"name": "Z", "in": "path", "required": True}],
                       "responses": {"200": {"$ref": "#/definitions/Nope"}}}}}}
    errors, size = validate.check(doc, limit=50)
    text = "\n".join(errors)
    for rule in ("C4 info.description", "C2 GET /A({K}): placeholder {K}", "C2 GET /B: path parameter Z",
                 "C3 GET /A({K})", "C5 operationId a is used", "C7 dangling $ref #/definitions/Nope",
                 "C7 dangling $ref #/parameters/missing", "C10"):
        assert rule in text, rule
    assert size.bytes > 50


def test_bound_function_matches_equivalent_route():
    edmx = parse_edmx(EDMX.replace('<EntitySet Name="OrderSet" EntityType="{NS}.Order"/>'.replace("{NS}", NS),
                                   f'<EntitySet Name="OrderSet" EntityType="{NS}.Order"/>'
                                   f'<EntitySet Name="LineSet" EntityType="{NS}.Line"/>'))
    document = copy.deepcopy(spec())
    document["paths"][f"/LineSet/{NS}.Line_Default()"] = {"get": {"operationId": "LD", "responses": {}}}
    op = _op(f"/OrderSet(OrderNo='{{OrderNo}}')/LinesArray/{NS}.Line_Default()")
    path, _, how = match_spec(op, document, edmx)
    assert (path, how) == (f"/LineSet/{NS}.Line_Default()", "bound-equivalent")
    with pytest.raises(GenerationRefused, match="not in the pinned"):
        match_spec(_op(f"/OrderSet/{NS}.Other_Default()"), document, edmx)
