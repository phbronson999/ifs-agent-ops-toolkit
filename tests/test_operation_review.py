"""Technical operation review: alias parameters, content hash, review notes and the review report."""
import json

from ifs_agent_ops.ifs.metadata.edmx import parse_edmx
from ifs_agent_ops.operations import service
from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.operations.capability_review import render_review
from ifs_agent_ops.operations.draft import parse_path, resolve, templatize
from ifs_agent_ops.operations.operation_review import MEANING
from ifs_agent_ops.operations.store import approve, load_operations, save_operation
from ifs_agent_ops.operations.validate import validate_operation
from test_capabilities import BASE
from test_operations import pkg  # noqa: F401

NS = "IfsApp.Alias"
ALIAS_EDMX = f"""<?xml version="1.0"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>
<Schema Namespace="{NS}" xmlns="http://docs.oasis-open.org/odata/ns/edm">
 <ComplexType Name="Rec"><Property Name="Company" Type="Edm.String"/></ComplexType>
 <Function Name="GetStuff"><Parameter Name="Code" Type="Edm.String"/><Parameter Name="Params" Type="{NS}.Rec"/>
  <ReturnType Type="{NS}.Rec"/></Function>
 <EntityContainer Name="Main"><FunctionImport Name="GetStuff" Function="{NS}.GetStuff"/></EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>"""


def test_parameter_alias_is_kept_in_the_canonical_template():
    model = parse_edmx(ALIAS_EDMX)
    for client_alias in ("@Params", "@Alias1"):  # the alias name is the client's choice
        segs = parse_path(f"/GetStuff(Code=null,Params={client_alias})")
        template, params = templatize(model, segs, resolve(model, segs, "GET"))
        assert template == "/GetStuff(Code='{Code}',Params=@Params)"
        assert params["Params"].placements[0].alias is True and params["Params"].placements[0].quoted is False
        assert params["Code"].placements[0].alias is None


def test_technical_hash_pins_the_representation_not_the_commentary(pkg):  # noqa: F811
    service.draft(pkg)
    op_id = "OrderHandling.create.OrderSet"
    op = load_operations(pkg)[op_id]
    noted = op.model_copy(update={"description": "verified in TEST", "review_notes": ["is X proven?"]})
    assert noted.technical_hash() == op.technical_hash()
    approved = approve(pkg, op_id, "Pat")
    assert approved.approval.content_sha256 == op.technical_hash()
    assert not [f for f in validate_operation(approved) if f.rule == "changed-since-approval"]
    edited = approved.model_copy(update={"method": "PUT"})  # hand edit after approval
    assert [f.severity for f in validate_operation(edited) if f.rule == "changed-since-approval"] == ["error"]


def test_review_notes_survive_redraft_and_show_as_open_questions(pkg):  # noqa: F811
    service.draft(pkg)
    op_id = "OrderHandling.create.OrderSet"
    save_operation(pkg, load_operations(pkg)[op_id].model_copy(update={"review_notes": ["standalone POST unproven"]}))
    service.draft(pkg)
    op = load_operations(pkg)[op_id]
    assert op.review_notes == ["standalone POST unproven"]
    assert ("open-question", "info") in {(f.rule, f.severity) for f in validate_operation(op)}


def test_operations_review_artifacts_and_json(pkg, monkeypatch, capsys):  # noqa: F811
    from ifs_agent_ops.cli.main import main

    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(pkg.root))
    service.draft(pkg)
    ids = ["OrderHandling.create.OrderSet", "OrderHandling.create.OrderSet.LinesArray"]
    assert main(["operations", "review", *ids, "--package", "ifs-orders"]) == 0
    out = capsys.readouterr().out
    assert "Technical operation review" in out and "[" not in out  # redirected: no ANSI
    base = pkg.root / ".ifs-agent-ops" / "review" / "operations"
    doc = (base / "selection.md").read_text(encoding="utf-8")
    for expected in (MEANING, "## Review summary", "## Operations requiring attention", "## Operation summaries",
                     "appendix/OrderHandling.create.OrderSet.md"):
        assert expected in doc, expected
    appendix = (base / "appendix" / "OrderHandling.create.OrderSet.md").read_text(encoding="utf-8")
    for expected in ("## Request body", "## Headers, ETag and warnings", "## Response", "## Evidence and data flow",
                     "Content SHA-256", "WantedDate", "string:9999-99-99T99:99:99Z", "## Findings (every finding"):
        assert expected in appendix, expected
    for text in (doc, appendix):
        assert "ORD-1" not in text and "'C1'" not in text  # shapes, never recorded values

    assert main(["operations", "review", ids[0], "--package", "ifs-orders", "--output", "json"]) == 0
    raw = capsys.readouterr().out
    assert "" not in raw
    data = json.loads(raw)
    op = data["operations"][0]
    assert data["schema"] == "ifs-agent-ops/operation-review" and op["id"] == ids[0] and op["meaning"] == MEANING
    assert {"state", "attention", "findings", "finding_groups", "open_questions", "path", "content_sha256", "body",
            "response", "etag", "evidence", "used_by", "request_usage"} <= set(op)
    assert main(["operations", "review", ids[0], "--package", "ifs-orders", "--output", "json"]) == 0
    assert capsys.readouterr().out == raw  # deterministic


def test_capability_review_shows_deferred_scope_and_output_meaning(pkg):  # noqa: F811
    service.draft(pkg)
    data = {**BASE, "deferred": ["Charges: a later revision."],
            "outputs": [{"name": "order_no", "step": "create-header", "field": "OrderNo",
                         "description": "IFS order number"}]}
    text = render_review(Capability.model_validate(data), load_operations(pkg), {})
    assert "Deliberately out of scope" in text and "Charges: a later revision." in text
    assert "IFS order number" in text
