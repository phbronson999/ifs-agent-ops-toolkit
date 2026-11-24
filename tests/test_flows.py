"""Phase 12b: flows generated from approved capability actions, and deployed with the solution."""
import copy
import json
import re
import zipfile

import pytest
import yaml
from test_capabilities import BASE, USED, cap, rules
from test_connector import pinned  # noqa: F401  (fixture chain: pinned -> approved -> pkg)
from test_deploy_powerplatform import FakeEnvironment, FakePac
from test_operations import pkg  # noqa: F401
from test_skills import _cap_file, approved  # noqa: F401

from ifs_agent_ops.deploy import powerplatform as ppa
from ifs_agent_ops.deploy import targets as tg
from ifs_agent_ops.deploy.intent import load_intent, scaffold
from ifs_agent_ops.generators.flow import MARKER, flow_dir, flow_name, generate
from ifs_agent_ops.generators.flow.expr import V, concat, odata_literal, odata_string
from ifs_agent_ops.operations.capability import Capability
from ifs_agent_ops.operations.capability_store import approve_capability
from ifs_agent_ops.operations.model import TypeRef
from ifs_agent_ops.operations.store import load_operations


def _write_deploy(package, flows=True):
    text = scaffold(package, "ContosoAdmin", "ti")
    if flows:
        text += "  flows:\n  - source: power-platform/flows/create-order\n"
    (package.root / "deploy.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def flows_pkg(pinned):  # noqa: F811
    from ifs_agent_ops.generators.connector import generate as generate_connector

    assert generate_connector(pinned).ok
    _write_deploy(pinned)
    gen = generate(pinned)
    assert gen.ok, gen.errors
    return pinned


def _flow(package, action):
    return json.loads((flow_dir(package, "create-order") / f"{action}.json").read_text(encoding="utf-8"))


def _all(actions, out=None):
    out = {} if out is None else out
    for name, a in actions.items():
        out[name] = a
        _all(a.get("actions") or {}, out)
        _all((a.get("else") or {}).get("actions") or {}, out)
    return out


def _actions(package, action):
    return _all(_flow(package, action)["properties"]["definition"]["actions"])


# ── generation ───────────────────────────────────────────────────────────────

def test_one_flow_per_action_deterministic_and_checkable(flows_pkg):
    folder = flow_dir(flows_pkg, "create-order")
    assert sorted(p.name for p in folder.iterdir()) == ["prepare.json", "report.md", "submit.json"]
    assert MARKER in (folder / "report.md").read_text(encoding="utf-8")
    again = generate(flows_pkg)
    assert {r.action for r in again.results} == {"unchanged"}
    check = generate(flows_pkg, check=True)
    assert check.ok and {r.action for r in check.results} == {"unchanged"}
    assert flow_name("create-order", "submit") == "CreateOrder_Submit"


def test_descriptions_fit_the_power_automate_limit(flows_pkg):
    from ifs_agent_ops.generators.flow.build import MAX_DESCRIPTION, short_description

    long = "Word " * 100 + "end. Second sentence."
    assert len(short_description(long)) <= MAX_DESCRIPTION and short_description(long).endswith("...")
    assert short_description("One sentence. Two.") == "One sentence."
    for action in ("prepare", "submit"):
        flow = _flow(flows_pkg, action)["properties"]["definition"]
        nodes = list(_all(flow["actions"]).values()) + list(flow["triggers"].values())
        assert all(len(a.get("description", "")) <= MAX_DESCRIPTION for a in nodes)


def test_trigger_is_restricted_and_environment_values_are_placeholders(flows_pkg):
    flow = _flow(flows_pkg, "submit")
    trigger = flow["properties"]["definition"]["triggers"]["manual"]
    assert trigger["kind"] == "Http" and trigger["inputs"]["triggerAuthenticationType"] == "User"
    assert trigger["inputs"]["triggerAllowedUsers"] == "%%TRIGGER_USERS%%"
    props = trigger["inputs"]["schema"]["properties"]
    assert props["lines"]["items"]["properties"]["qty"] == {"type": "number"}
    assert props["confirmed"]["type"] == "boolean"
    assert "confirmed" not in _flow(flows_pkg, "prepare")["properties"]["definition"]["triggers"]["manual"][
        "inputs"]["schema"]["properties"]
    [ref] = flow["properties"]["connectionReferences"].values()
    assert ref["connection"]["connectionReferenceLogicalName"] == "ti_IfsOrderHandling"
    assert ref["api"] == {"name": "%%CONNECTOR_API:order-handling%%", "logicalName": "ti_ifsorderhandling"}
    text = json.dumps(flow)
    assert "ifs.example" not in text and "@" not in json.dumps(trigger["inputs"]["triggerAllowedUsers"])


def test_submit_steps_bindings_and_response(flows_pkg):
    _variant(flows_pkg, lambda d: d["actions"][1].update(returns=["order_no", "warnings"]))
    assert generate(flows_pkg).ok
    acts = _actions(flows_pkg, "submit")
    ids = yaml.safe_load((flows_pkg.root / "power-platform/connectors/OrderHandling/operation-ids.yaml")
                         .read_text(encoding="utf-8"))["operations"]
    assert acts["Check_Confirmation"]["expression"] == "@not(equals(triggerBody()?['confirmed'], true))"
    assert "Check_Required_Inputs" in acts and "Check_Lines_Complete" in acts
    loop = acts["For_Each_Lines"]
    assert loop["type"] == "Foreach" and loop["runtimeConfiguration"] == {"concurrency": {"repetitions": 1}}
    line = acts["Create_Line"]
    assert line["inputs"]["host"]["operationId"] == ids[USED[3]]["operation_id"]
    assert line["inputs"]["retryPolicy"] == {"type": "none"} and line["limit"] == {"timeout": "PT60S"}
    assert acts["Find"]["inputs"]["retryPolicy"] == {"type": "fixed", "count": 3, "interval": "PT5S"}
    assert line["inputs"]["parameters"]["OrderNo"] == "@variables('create_header')?['OrderNo']"
    body = acts["Body_Create_Line"]["inputs"]
    assert body["LineNo"] == "@items('For_Each_Lines')?['line_no']" and body["Qty"] == "@items('For_Each_Lines')?['qty']"
    # a failure records the step, status and IFS response, plus the line it belongs to
    failed = acts["Create_Line_Failed"]
    assert failed["runAfter"] == {"Create_Line": ["Failed", "TimedOut"]}
    assert failed["inputs"]["value"]["step"] == "create-line" and failed["inputs"]["value"]["line"]
    # function parameters are OData literals; null stays null
    defaults = acts["Defaults"]["inputs"]["parameters"]
    assert defaults["TaxCode"] == "null"
    assert defaults["CustomerNo"].startswith("@if(equals(triggerBody()?['customer_no'], null), 'null', concat(''''")
    # body seeded from the defaults step, bound values on top, transform applied
    header = acts["Body_Create_Header"]["inputs"]
    assert header["CustomerNo"] == "@triggerBody()?['customer_no']"
    assert "T00:00:00Z" in header["WantedDate"]
    assert set(header) == {"CustomerNo", "WantedDate"}      # the defaults step returned no fields to seed
    respond = acts["Respond"]["inputs"]["body"]
    assert respond["outputs"] == {"order_no": "@variables('create_header')?['OrderNo']"}
    assert respond["ok"] == "@outputs('Outcome')?['ok']" and respond["warnings"] == "@variables('warnings')"
    assert acts["Respond"]["runAfter"] == {"Outcome": ["Succeeded"]}
    assert acts["Outcome"]["runAfter"] == {"Try": ["Succeeded", "Failed", "Skipped", "TimedOut"]}


def test_prepare_asks_questions_and_writes_nothing(flows_pkg):
    acts = _actions(flows_pkg, "prepare")
    assert acts["Ask_Ask_Date"]["expression"] == "@empty(coalesce(triggerBody()?['wanted'], ''))"
    assert not any(a.get("type") == "OpenApiConnection" and "create" in a["inputs"]["host"]["operationId"]
                   for a in acts.values())
    find = acts["Find"]["inputs"]["parameters"]
    # tabs, not spaces: a space in $filter breaks requests through the gateway
    assert find["$filter"] == "Objstate\teq\tIfsApp.OrderHandling.OrderState'Planned'" and find["$top"] == 50
    assert set(acts["Respond"]["inputs"]["body"]) >= {"warnings", "ok", "error"}
    assert "proposal" not in acts["Respond"]["inputs"]["body"]      # BASE's prepare returns nothing else


def _variant(package, mutate):
    data = copy.deepcopy(BASE)
    data["steps"] = [dict(s, bindings=dict(s.get("bindings", {}))) for s in data["steps"]]
    data["steps"][2]["bindings"]["header.X-IFS-Accept-Warnings"] = {"fixed": "true"}
    mutate(data)
    _cap_file(package, data)
    approve_capability(package, "create-order", "Pat")


def test_match_modes_and_structured_warnings(flows_pkg):
    def mutate(data):
        find = data["steps"][0]
        find["bindings"]["filter.Objstate"] = {"fixed": "Planned", "match": "ne"}
        find["bindings"]["filter.CustomerNo"] = {"input": "customer_no", "match": "contains"}
        data["warnings"] = [{"source": "step", "step": "defaults", "when": {"field": "CustomerNo", "not_equals": 0},
                             "meaning": "check"},
                            {"source": "ifs", "header": "X-IFS-Accept-Warnings", "meaning": "IFS messages"}]

    _variant(flows_pkg, mutate)
    assert generate(flows_pkg).ok
    prepare = _actions(flows_pkg, "prepare")
    flt = prepare["Find"]["inputs"]["parameters"]["$filter"]
    assert flt.startswith("@concat('Objstate\tne\tIfsApp.OrderHandling.OrderState''Planned''\tand\t', ")
    assert "contains(tolower(CustomerNo)," in flt and "toLower(trim(coalesce(triggerBody()?['customer_no'], '')))" in flt
    # the lookup runs only when its search value is supplied
    assert "not(empty(coalesce(triggerBody()?['customer_no'], '')))" in prepare["If_Find"]["expression"]
    submit = _actions(flows_pkg, "submit")
    assert submit["Warn_Defaults"]["expression"] == "@not(equals(string(variables('defaults')?['CustomerNo']), '0'))"
    messages = submit["Append_IFS_Messages_Create_Header"]["inputs"]["value"]["messages"]
    assert messages.startswith("@json(base64ToString(coalesce(outputs('Create_Header')?['headers']?['X-IFS-Info']")


def test_word_search(flows_pkg):
    def mutate(data):
        data["steps"][0]["bindings"]["filter.CustomerNo"] = {"input": "customer_no", "match": "words"}

    _variant(flows_pkg, mutate)
    assert generate(flows_pkg).ok
    acts = _actions(flows_pkg, "prepare")
    for name in ("Find_Words", "Find_Terms", "Find", "Find_Probe_Words", "Find_Probe", "Find_Kept_Words",
                 "Find_Any_Word", "Reset_find_rows", "Reset_find_kept"):
        assert name in acts, name
    assert acts["Find_Probe"]["inputs"]["parameters"]["$top"] == 1
    assert "'\tor\t'" in acts["Find_Any_Word"]["inputs"]["parameters"]["$filter"]
    assert acts["Set_find"]["inputs"]["value"] == "@setProperty(json('{}'), 'value', variables('find_rows'))"
    assert "word search" in (flow_dir(flows_pkg, "create-order") / "report.md").read_text(encoding="utf-8")


def test_refusals(flows_pkg):
    def prose(data):
        data["warnings"] = [{"source": "step", "step": "find", "when": "Result is not 0", "meaning": "x"}]

    _variant(flows_pkg, prose)
    gen = generate(flows_pkg)
    assert not gen.ok and "warning condition is prose" in gen.errors[0]

    def derived(data):
        data["steps"][2]["bindings"]["body.CustomerNo"] = {"derived": "the customer, somehow"}

    _variant(flows_pkg, derived)
    gen = generate(flows_pkg)
    assert not gen.ok and "derived" in gen.errors[0]
    (flows_pkg.root / "deploy.yaml").unlink()
    gen = generate(flows_pkg)
    assert not gen.ok and "no deploy.yaml" in gen.errors[0]


def test_hand_written_flows_are_not_overwritten(flows_pkg):
    folder = flow_dir(flows_pkg, "create-order")
    (folder / "report.md").write_text("mine", encoding="utf-8")
    gen = generate(flows_pkg)
    assert not gen.ok and "not generated" in gen.errors[0]


def test_cli_flow_generate(flows_pkg, capsys):
    from ifs_agent_ops.cli.main import main

    assert main(["flow", "generate", "--package", flows_pkg.name, "--check"]) == 0
    out = capsys.readouterr().out
    assert "CreateOrder_Prepare, CreateOrder_Submit" in out


# ── expressions and validation ───────────────────────────────────────────────

def test_expression_helpers():
    assert odata_string(V.of("O'Brien")).const == "'O''Brien'"
    assert odata_string(V.of(None)).const == "null"
    enum = TypeRef(edm="IfsApp.P.State", enum="IfsApp.P.State", members=["A"])
    assert odata_literal(V.of("A"), enum).const == "IfsApp.P.State'A'"
    assert odata_literal(V.of("2026-12-04T00:00:00Z"), TypeRef(edm="Edm.DateTimeOffset")).const == \
        "2026-12-04T00:00:00Z"
    assert concat([V.of("a "), V.of("b")]).const == "a b"
    assert concat([V.of("x eq "), V.e("y")]).expr == "concat('x eq ', y)"
    assert V.of("@literal").json() == "@@literal" and V.e("f()").json() == "@f()"


def test_validator_rules_for_match_and_warnings(approved):  # noqa: F811
    ops = load_operations(approved)
    bad = copy.deepcopy(BASE)
    bad["steps"][1]["bindings"]["path.CustomerNo"] = {"input": "customer_no", "match": "contains"}
    bad["warnings"] = [{"source": "step", "step": "find", "when": "prose", "meaning": "m"},
                       {"source": "step", "step": "nope", "when": {"field": "X", "equals": 1}, "meaning": "m"}]
    found = rules(Capability.model_validate(bad), ops)
    assert ("step defaults", "bad-match", "error") in found
    assert ("warning 1", "not-executable", "warning") in found
    assert ("warning 2", "unknown-step", "error") in found
    # an unset match does not change approval hashes
    assert cap().content_hash() == Capability.model_validate(
        {**copy.deepcopy(BASE), "steps": [dict(s, bindings={k: dict(v, match=None) for k, v in s["bindings"].items()})
                                          for s in BASE["steps"]]}).content_hash()


# ── deployment ───────────────────────────────────────────────────────────────

class FlowEnvironment(FakeEnvironment):
    """The fake environment plus workflows: import stores the flow's clientdata and state."""

    def __init__(self):
        super().__init__()
        self.workflows = {}

    def import_solution(self, path):
        out = super().import_solution(path)
        with zipfile.ZipFile(path) as z:
            files = {n: z.read(n).decode("utf-8") for n in z.namelist()}
        sol_id = self.solutions[re.search(r"<UniqueName>(.*?)</UniqueName>", files["solution.xml"]).group(1)]["Id"]
        for wid, name, block in re.findall(r'<Workflow WorkflowId="\{(.*?)\}" Name="(.*?)">(.*?)</Workflow>',
                                           files["customizations.xml"], re.DOTALL):
            json_file = re.search(r"<JsonFileName>/(.*?)</JsonFileName>", block).group(1)
            state = int(re.search(r"<StateCode>(\d)</StateCode>", block).group(1))
            self.workflows[wid] = {"workflowid": wid, "name": name, "clientdata": files[json_file],
                                   "statecode": state, "resourceid": f"res-{name}"}
            self.members[sol_id].add(wid)
        return out


class FakeFlowApi:
    def __init__(self, env):
        self.e = env
        self.calls = []

    def workflow(self, workflow_id):
        row = self.e.workflows.get(workflow_id)
        return dict(row) if row else None

    def set_state(self, workflow_id, on):
        self.calls.append(("set_state", workflow_id, on))
        self.e.workflows[workflow_id]["statecode"] = 1 if on else 0

    def callback_url(self, resource_id, trigger="manual"):
        return f"https://flow.example/{resource_id}/{trigger}"


@pytest.fixture
def deploy_setup(flows_pkg, tmp_path, monkeypatch):
    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path / "home"))
    tg.write_target(flows_pkg.name, "dev", {"FriendlyName": "Dev", "EnvironmentIdentifier": {"Id": "env-1"},
                                            "EnvironmentUrl": "https://dev.example/"}, {"ti_IFSDomain": "ifs.example"})
    env = FlowEnvironment()
    return flows_pkg, load_intent(flows_pkg), env, FakePac(env), FakeFlowApi(env)


def _users(package, users):
    path = tg.target_path(package.name, "dev")
    text = path.read_text(encoding="utf-8")
    header = "".join(ln for ln in text.splitlines(keepends=True) if ln.startswith("#"))
    data = yaml.safe_load(text)
    data["flow_trigger_users"] = users
    path.write_text(header + yaml.safe_dump(data), encoding="utf-8")
    return tg.load_target(package.name, "dev")


def test_flows_wait_for_trigger_users(deploy_setup):
    package, intent, env, pac, api = deploy_setup
    target = tg.load_target(package.name, "dev")
    plan = ppa.plan(intent, package, target, pac, api)
    assert [(i.kind, i.action) for i in plan.items if i.kind == "flow"] == [("flow", "manual")] * 2
    assert "flow_trigger_users" in plan.manual[-1].detail


def test_flows_deploy_with_environment_values_then_no_op(deploy_setup):
    package, intent, env, pac, api = deploy_setup
    target = _users(package, ["maker@contoso.example", "agent@contoso.example"])
    before = ppa.plan(intent, package, target, pac, api)
    assert [(i.name, i.action, i.detail) for i in before.items if i.kind == "flow"] == [
        ("CreateOrder_Prepare", "create", "after the connector is created"),
        ("CreateOrder_Submit", "create", "after the connector is created")]
    result = ppa.apply(intent, package, target, pac, api)
    assert result.imported == ["1.0.0.0", "1.0.0.1"]       # connector first; references and flows with its API name
    assert result.after.changes == []
    [prepare] = [w for w in env.workflows.values() if w["name"] == "CreateOrder_Prepare"]
    deployed = json.loads(prepare["clientdata"])
    text = json.dumps(deployed)
    assert "%%" not in text and "shared_ti_ifsorderhandling-5fabc" in text
    assert deployed["properties"]["definition"]["triggers"]["manual"]["inputs"]["triggerAllowedUsers"] == \
        "maker@contoso.example; agent@contoso.example"
    assert prepare["statecode"] == 1
    # trigger URLs are recorded in the target file (machine-local), not in the package
    assert result.triggers_recorded
    reloaded = tg.load_target(package.name, "dev")
    assert reloaded.flow_triggers == {"CreateOrder_Prepare": "https://flow.example/res-CreateOrder_Prepare/manual",
                                      "CreateOrder_Submit": "https://flow.example/res-CreateOrder_Submit/manual"}
    assert tg.target_path(package.name, "dev").read_text(encoding="utf-8").startswith("# Deployment target")
    again = ppa.apply(intent, package, reloaded, pac, api)
    assert again.imported == [] and not again.triggers_recorded and len(env.imports) == 2


def test_changed_or_turned_off_flows(deploy_setup):
    package, intent, env, pac, api = deploy_setup
    target = _users(package, ["maker@contoso.example"])
    ppa.apply(intent, package, target, pac, api)
    [wid] = [w["workflowid"] for w in env.workflows.values() if w["name"] == "CreateOrder_Submit"]
    env.workflows[wid]["statecode"] = 0
    plan = ppa.plan(intent, package, target, pac, api)
    assert [(i.name, i.detail) for i in plan.changes] == [("CreateOrder_Submit", "turned off: turn on")]
    result = ppa.apply(intent, package, target, pac, api)
    assert result.turned_on == ["CreateOrder_Submit"] and ("set_state", wid, True) in api.calls
    target = _users(package, ["someone.else@contoso.example"])     # a different user list is a new definition
    plan = ppa.plan(intent, package, target, pac, api)
    assert {(i.kind, i.action, i.detail) for i in plan.changes} == {("flow", "update", "definition differs")}


def test_stale_generated_flows_are_refused(deploy_setup):
    package, intent, env, pac, api = deploy_setup
    target = _users(package, ["maker@contoso.example"])
    path = flow_dir(package, "create-order") / "submit.json"
    path.write_text(path.read_text(encoding="utf-8").replace("CreateOrder", "Changed", 1) + " ", encoding="utf-8")
    with pytest.raises(ppa.DeployError, match="flows are not current"):
        ppa.plan(intent, package, target, pac, api)


def test_targets_validate_trigger_users(deploy_setup):
    package, *_ = deploy_setup
    with pytest.raises(tg.TargetError, match="flow_trigger_users"):
        _users(package, ["not-an-email"])


def test_filters_never_contain_spaces():
    """A space in $filter breaks requests through the connector and gateway; tabs work (DEV 2026-11-22)."""
    from ifs_agent_ops.generators.flow.build import FlowBuilder
    from ifs_agent_ops.generators.flow.expr import V as Val

    assert FlowBuilder.contains_words("Name", Val.of("Harco Americas")).const == \
        "contains(tolower(Name),'harco')\tand\tcontains(tolower(Name),'americas')"
    expr = FlowBuilder.contains_words("Name", Val.e("x")).expr
    assert "replace(" in expr and "' ', ''')\tand\tcontains(tolower(Name),'''" in expr


# ── runtime cases (12c) ──────────────────────────────────────────────────────

PREPARE_ANSWER = {
    "ok": True, "error": None, "questions": [], "warnings": [{"source": "step", "step": "find", "meaning": "m"}],
    "proposal": {"results": {"find": {"value": [{"OrderNo": "A1"}]}},
                 "lines": [{"line": {}, "results": {"check": {"value": [{"CatalogNo": "900664"}, {"CatalogNo": "2"}]}}}]}}


def test_runtime_expectations():
    from ifs_agent_ops.deploy.runtime import check

    ok = {"ok": True, "questions": [], "warnings": ["find"], "matches": {"find": 1},
          "lines": [{"check": {"includes": {"CatalogNo": "900664"}}}]}
    assert check(ok, PREPARE_ANSWER) == []
    bad = {"ok": True, "questions": ["q"], "warnings": [], "matches": {"find": "none"},
           "lines": [{"check": 1}, {"check": ">0"}]}
    problems = check(bad, PREPARE_ANSWER)
    assert len(problems) == 5 and "find: 1 row(s), expected none" in problems
    assert check({"outputs": {"order_no": "present", "line_numbers": {"length": 2}}},
                 {"outputs": {"order_no": "C1", "line_numbers": [1]}}) == ["output line_numbers has 1 entries, expected 2"]


def test_runtime_cases_run_reads_and_hold_writes(flows_pkg):
    from ifs_agent_ops.deploy.runtime import cases_path, load_cases, run_cases

    path = cases_path(flows_pkg, "create-order")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"capability": "create-order", "cases": [
        {"name": "lookups", "action": "prepare", "inputs": {"customer_no": "1"}, "expect": {"ok": True,
                                                                                          "matches": {"find": 1}}},
        {"name": "create", "action": "submit", "inputs": {"customer_no": "1"}, "expect": {"ok": True}}]}),
        encoding="utf-8")
    calls = []

    def caller(url, body):
        calls.append((url, body))
        return PREPARE_ANSWER if "prepare" in url else {"ok": True}

    triggers = {"CreateOrder_Prepare": "https://flow.example/prepare", "CreateOrder_Submit": "https://flow.example/submit"}
    cases = load_cases(flows_pkg, "create-order")
    held = run_cases("create-order", cases, triggers, {"submit"}, allow_writes=False, caller=caller)
    assert [(r.case, r.status) for r in held] == [("lookups", "PASS"), ("create", "NOT RUN")]
    assert calls == [("https://flow.example/prepare", {"customer_no": "1"})]
    allowed = run_cases("create-order", cases, triggers, {"submit"}, allow_writes=True, caller=caller)
    assert [r.status for r in allowed] == ["PASS", "PASS"]
    assert calls[-1] == ("https://flow.example/submit", {"customer_no": "1", "confirmed": True})
    missing = run_cases("create-order", cases, {}, {"submit"}, allow_writes=False, caller=caller)
    assert missing[0].status == "FAIL" and "no trigger URL" in missing[0].detail


def test_batch_url_escaping():
    """IFS's batch request-line parser rejects ':' (date-times); spaces cannot travel in a URL."""
    from ifs_agent_ops.generators.flow.build import FlowBuilder
    from ifs_agent_ops.generators.flow.expr import V as Val

    assert FlowBuilder.url_safe(Val.of("2026-12-15T00:00:00Z")).const == "2026-12-15T00%3A00%3A00Z"
    assert FlowBuilder.url_safe(Val.of("'a b%'")).const == "'a%20b%25'"
    assert FlowBuilder.url_safe(Val.e("x")).expr.startswith("replace(replace(replace(replace(replace(string(x)")


# ── writes in one $batch change set ──────────────────────────────────────────

def test_batched_write_is_sent_as_one_changeset(flows_pkg):
    from ifs_agent_ops.generators.connector import generate as generate_connector

    _variant(flows_pkg, lambda d: d["steps"][3].update(batch="line-save"))
    assert generate_connector(flows_pkg).ok      # the connector gains the $batch operation
    swagger = json.loads((flows_pkg.root / "power-platform/connectors/OrderHandling/apiDefinition.swagger.json")
                         .read_text(encoding="utf-8"))
    assert "/$batch" in swagger["paths"]
    gen = generate(flows_pkg)
    assert gen.ok, gen.errors
    acts = _actions(flows_pkg, "submit")
    assert "Create_Line" in acts and acts["Create_Line"]["inputs"]["host"]["operationId"] == "batch"
    text = acts["Batch_Create_Line"]["inputs"]
    assert "Content-Type: multipart/mixed;boundary=changeset_ifs_agent_ops" in text
    assert "Content-ID: 1" in text and "\r\nPOST " in text and " HTTP/1.1\r\n" in text
    assert "POST OrderSet(OrderNo='@{replace(" in text and "string(variables('create_header')?['OrderNo'])" in text
    assert "'*', '%2A')}')/LinesArray HTTP/1.1" in text                  # key escaped inline, '*' as %2A
    assert "@{string(outputs('Body_Create_Line'))}" in text               # JSON body of the part
    assert text.rstrip().endswith("--batch_ifs_agent_ops--")
    ok = acts["Create_Line_Succeeded"]["expression"]
    assert "equals(length(outputs('Create_Line_Parts')), 1)" in ok and "less(int(substring(" in ok
    assert acts["Set_create_line"]["inputs"]["value"].startswith("@if(contains(outputs('Create_Line_Parts')[0], '{')")
    failed = acts["Create_Line_Failed_In_Batch"]["inputs"]["value"]
    assert failed["changeset"] == ["create-line"] and failed["line"] == "@items('For_Each_Lines')"
    assert acts["If_Create_Line"]["description"].startswith("One change set")


def test_validator_rules_for_batches(approved):  # noqa: F811
    ops = load_operations(approved)
    bad = copy.deepcopy(BASE)
    bad["steps"][0]["batch"] = "x"                      # a lookup
    bad["steps"][2]["batch"] = "save"                   # header (no for_each) ...
    bad["steps"][3]["batch"] = "save"                   # ... and line (for_each lines, reads the header's OrderNo)
    found = rules(Capability.model_validate(bad), ops)
    assert ("batch x", "batch-not-write", "error") in found
    assert ("batch save", "batch-for-each", "error") in found
    assert ("step create-line", "batch-self-reference", "error") in found
    split = copy.deepcopy(BASE)
    split["steps"][2]["batch"] = split["steps"][3]["batch"] = "save"
    split["actions"][1]["steps"] = ["find", "create-header", "defaults", "create-line"]
    assert ("action submit", "batch-split", "error") in rules(Capability.model_validate(split), ops)
    # an unset batch does not change approval hashes
    assert cap().content_hash() == Capability.model_validate(
        {**copy.deepcopy(BASE), "steps": [dict(s, batch=None) for s in BASE["steps"]]}).content_hash()
