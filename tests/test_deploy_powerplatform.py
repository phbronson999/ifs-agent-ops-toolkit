"""Power Platform adapter: plan / apply / status against a fake environment (no pac, no network)."""
import json
import re
import zipfile

import pytest
import yaml
from test_connector import pinned  # noqa: F401  (pytest fixture chain: pinned -> approved -> pkg)
from test_operations import pkg  # noqa: F401
from test_skills import approved  # noqa: F401

from ifs_agent_ops.deploy import powerplatform as ppa
from ifs_agent_ops.deploy import solution_zip as sz
from ifs_agent_ops.deploy import targets as tg
from ifs_agent_ops.deploy.intent import parse, scaffold
from ifs_agent_ops.deploy.pac import parse_table

REF_TYPE = 10150


class FakeEnvironment:
    """Just enough of an environment: solutions, publisher, connectors, variables, references, membership.
    Importing a solution file applies it the way Dataverse does (adds/updates, never removes)."""

    def __init__(self):
        self.solutions = {}
        self.publisher = {"uniquename": "TietexAdmin", "friendlyname": "TietexAdmin", "customizationprefix": "ti",
                          "customizationoptionvalueprefix": "12,677", "publisherid": "p-1"}
        self.connectors = {}        # name -> row (+ definition, params)
        self.variables = {}         # schema -> row
        self.values = {}            # definition id -> value
        self.references = {}        # logical -> row
        self.members = {}           # solution id -> set(object ids)
        self.imports = []           # parsed zips
        self.added = []

    # Pac interface ---------------------------------------------------------
    def solutions_list(self):
        return [{"Id": s["Id"], "SolutionUniqueName": n, "VersionNumber": s["Version"], "IsManaged": s["Managed"]}
                for n, s in self.solutions.items()]

    def fetch(self, entity, attributes, conditions=None, like=False):
        (_, value), = (conditions or {}).items()
        if entity == "publisher":
            return [dict(self.publisher)] if value == self.publisher["uniquename"] else []
        if entity == "solutioncomponent":
            return [{"objectid": o} for o in self.members.get(value, set())]
        if entity == "connector":
            row = self.connectors.get(value)
            return [{k: v for k, v in row.items() if k not in ("definition", "params")}] if row else []
        if entity == "environmentvariabledefinition":
            return [dict(self.variables[value])] if value in self.variables else []
        if entity == "environmentvariablevalue":
            return [{"value": self.values[value]}] if value in self.values else []
        if entity == "connectionreference":
            return [dict(self.references[value])] if value in self.references else []
        if entity == "solutioncomponentdefinition":
            return [{"name": "connectionreference", "solutioncomponenttype": "10,150"}]
        raise AssertionError(entity)

    def download_connector(self, connector_id):
        row = next(r for r in self.connectors.values() if r["connectorid"] == connector_id)
        return json.loads(json.dumps(row["definition"])), {"connectionParameters": row["params"]}

    def import_solution(self, path):
        with zipfile.ZipFile(path) as z:
            files = {n: z.read(n).decode("utf-8") for n in z.namelist()}
        self.imports.append(files)
        sol = files["solution.xml"]
        name = re.search(r"<UniqueName>(.*?)</UniqueName>", sol).group(1)
        version = re.search(r"<Version>(.*?)</Version>", sol).group(1)
        entry = self.solutions.setdefault(name, {"Id": f"sol-{name}", "Managed": False})
        entry["Version"] = version
        members = self.members.setdefault(entry["Id"], set())
        cust = files["customizations.xml"]
        for block in re.findall(r"<Connector>(.*?)</Connector>", cust, re.DOTALL):
            def get(tag, block=block):
                return re.search(f"<{tag}>(.*?)</{tag}>", block, re.DOTALL).group(1)

            cname = get("name")
            self.connectors[cname] = {
                "connectorid": get("connectorid"), "name": cname, "displayname": get("displayname"),
                "connectorinternalid": f"shared_{cname}-5fabc", "capabilities":
                    "gateway" if get("capabilities") == sz.GATEWAY_CAPABILITY else "",
                "definition": json.loads(files[f"Connector/{cname}_openapidefinition.json"]),
                "params": json.loads(files[f"Connector/{cname}_connectionparameters.json"])}
            members.add(get("connectorid"))       # root component: lands in the solution
        for logical, block in re.findall(r'connectionreferencelogicalname="(.*?)">(.*?)</connectionreference>',
                                         cust, re.DOTALL):
            connector = re.search(r"<connectorid>(.*?)</connectorid>", block).group(1)
            row = self.references.setdefault(logical, {"connectionreferencelogicalname": logical,
                                                       "connectionreferenceid": f"ref-{logical}", "connectionid": ""})
            row["connectorid"] = connector       # not a root component: must be added to the solution
        for path_name, text in files.items():
            m = re.match(r"environmentvariabledefinitions/(\w+)/environmentvariabledefinition.xml", path_name)
            if m:
                self.variables[m.group(1)] = {"schemaname": m.group(1), "type": "String", "defaultvalue": "",
                                              "environmentvariabledefinitionid": f"var-{m.group(1)}"}
                members.add(f"var-{m.group(1)}")   # root component: lands in the solution
            m = re.match(r"environmentvariabledefinitions/(\w+)/environmentvariablevalues.json", path_name)
            if m:
                self.values[f"var-{m.group(1)}"] = json.loads(text)["environmentvariablevalues"][
                    "environmentvariablevalue"]["value"]
        return "Solution Imported successfully."

    def add_component(self, solution, component_id, component_type):
        self.added.append((component_id, component_type))
        self.members[self.solutions[solution]["Id"]].add(component_id)
        return "added"


class FakePac:
    def __init__(self, env: FakeEnvironment):
        self.e = env

    def solutions(self):
        return self.e.solutions_list()

    def fetch(self, *a, **k):
        return self.e.fetch(*a, **k)

    def download_connector(self, cid):
        return self.e.download_connector(cid)

    def import_solution(self, path):
        return self.e.import_solution(path)

    def add_component(self, *a):
        return self.e.add_component(*a)


@pytest.fixture
def setup(pinned, tmp_path, monkeypatch):  # noqa: F811
    from ifs_agent_ops.generators.connector import generate

    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path / "home"))
    assert generate(pinned).ok
    intent = parse(yaml.safe_load(scaffold(pinned, "TietexAdmin", "ti")), pinned.root / "deploy.yaml")
    tg.write_target(pinned.name, "dev", {"FriendlyName": "Dev", "EnvironmentIdentifier": {"Id": "env-1"},
                                         "EnvironmentUrl": "https://dev.example/"}, {"ti_IFSDomain": "ifs.example"})
    target = tg.load_target(pinned.name, "dev")
    env = FakeEnvironment()
    return pinned, intent, target, env, FakePac(env)


def test_first_deploy_creates_everything_then_second_is_a_no_op(setup):
    package, intent, target, env, pac = setup
    before = ppa.plan(intent, package, target, pac)
    assert [(i.kind, i.action) for i in before.changes] == [
        ("solution", "create"), ("connector", "create"), ("environment variable", "create"),
        ("connection reference", "create")]

    result = ppa.apply(intent, package, target, pac)
    assert result.imported == ["1.0.0.0", "1.0.0.1"]       # connector first; reference once its API id exists
    assert result.after.changes == [] and env.added == [("ref-ti_IfsOrderHandling", REF_TYPE)]
    connector = env.connectors["ti_ifsorderhandling"]
    assert connector["capabilities"] == "gateway" and connector["displayname"] == "IFS Order Handling"
    assert connector["definition"]["host"] == '@environmentVariables("ti_IFSDomain")'
    assert connector["definition"]["basePath"].endswith("/OrderHandling.svc")
    assert env.references["ti_IfsOrderHandling"]["connectorid"] == \
        "/providers/Microsoft.PowerApps/apis/shared_ti_ifsorderhandling-5fabc"
    assert env.values["var-ti_IFSDomain"] == "ifs.example"
    # only the connection is left for a person
    assert [i.kind for i in result.after.manual] == ["connection"]

    settings = json.loads(result.settings_path.read_text(encoding="utf-8"))
    assert settings["EnvironmentVariables"] == [{"SchemaName": "ti_IFSDomain", "Value": "ifs.example"}]
    assert settings["ConnectionReferences"][0]["LogicalName"] == "ti_IfsOrderHandling"
    assert result.settings_path.parent == tg.targets_dir(package.name)

    again = ppa.apply(intent, package, target, pac)
    assert again.imported == [] and again.added == [] and len(env.imports) == 2


def test_changed_connector_is_updated_in_place(setup):
    package, intent, target, env, pac = setup
    ppa.apply(intent, package, target, pac)
    connector_id = env.connectors["ti_ifsorderhandling"]["connectorid"]
    env.connectors["ti_ifsorderhandling"]["definition"]["paths"].pop(next(iter(
        env.connectors["ti_ifsorderhandling"]["definition"]["paths"])))
    p = ppa.plan(intent, package, target, pac)
    assert [(i.kind, i.action, i.detail) for i in p.changes] == [
        ("connector", "update", "definition differs (paths)")]
    ppa.apply(intent, package, target, pac)
    assert env.connectors["ti_ifsorderhandling"]["connectorid"] == connector_id   # same connector, updated
    assert ppa.plan(intent, package, target, pac).changes == []


def test_existing_variable_is_added_not_redefined(setup):
    package, intent, target, env, pac = setup
    env.variables["ti_IFSDomain"] = {"schemaname": "ti_IFSDomain", "type": "String", "defaultvalue": "",
                                     "environmentvariabledefinitionid": "var-existing"}
    env.values["var-existing"] = "already.set"
    p = ppa.plan(intent, package, target, pac)
    assert ("environment variable", "add to solution") in [(i.kind, i.action) for i in p.changes]
    ppa.apply(intent, package, target, pac)
    assert all("environmentvariabledefinitions/" not in n for f in env.imports for n in f)
    assert ("var-existing", sz.ENVIRONMENT_VARIABLE_COMPONENT) in env.added
    assert env.values["var-existing"] == "already.set"


def test_connector_without_gateway_needs_a_person(setup):
    package, intent, target, env, pac = setup
    ppa.apply(intent, package, target, pac)
    env.connectors["ti_ifsorderhandling"]["capabilities"] = ""
    p = ppa.plan(intent, package, target, pac)
    [item] = [i for i in p.manual if i.kind == "connector"]
    assert "cannot be turned on after creation" in item.detail


def test_refusals(setup):
    package, intent, target, env, pac = setup
    env.solutions["IfsOrders"] = {"Id": "s", "Version": "1.0.0.0", "Managed": True}
    with pytest.raises(ppa.DeployError, match="is managed"):
        ppa.plan(intent, package, target, pac)
    env.solutions.clear()
    env.publisher["customizationprefix"] = "xx"
    with pytest.raises(ppa.DeployError, match="has prefix 'xx'"):
        ppa.plan(intent, package, target, pac)
    with pytest.raises(ppa.DeployError, match="no deploy.yaml"):
        ppa.plan(None, package, target, pac)


def test_stale_generated_connector_is_refused(setup):
    package, intent, target, _, pac = setup
    from ifs_agent_ops.generators.connector import DEFINITION_FILE, connector_dir

    (connector_dir(package, "OrderHandling") / DEFINITION_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(ppa.DeployError, match="not current"):
        ppa.plan(intent, package, target, pac)


def test_connector_names_are_stable():
    assert ppa.connector_name("ti", "customer-order-handling") == "ti_ifscustomerorderhandling"


def test_parse_pac_fetch_table():
    raw = ("Connected as someone@example.com\nConnected to... Dev\n\n"
           "name                        displayname                 capabilities connectorid\n"
           "ti_ifscustomerorderhandling IFS Customer Order Handling gateway      f1ae99e0\n"
           "ti_other                    Other                                    1234\n\n")
    assert parse_table(raw) == [
        {"name": "ti_ifscustomerorderhandling", "displayname": "IFS Customer Order Handling",
         "capabilities": "gateway", "connectorid": "f1ae99e0"},
        {"name": "ti_other", "displayname": "Other", "capabilities": "", "connectorid": "1234"}]
    assert parse_table("Connected as x\n\n") == []


def test_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path))
    envs = [{"FriendlyName": "ContosoTest", "UniqueName": "unq1", "EnvironmentIdentifier": {"Id": "env-7"},
             "EnvironmentUrl": "https://test.example/"}]
    assert tg.match_environment(envs, "contosotest")["UniqueName"] == "unq1"
    assert tg.match_environment(envs, "https://test.example")["UniqueName"] == "unq1"
    with pytest.raises(tg.TargetError, match="available: ContosoTest"):
        tg.match_environment(envs, "Prod")
    path = tg.write_target("ifs-orders", "dev", envs[0], {"ti_IFSDomain": "ifs.example"})
    t = tg.load_target("ifs-orders", "dev")
    assert (t.environment, t.environment_url, t.variables) == \
        ("ContosoTest", "https://test.example", {"ti_IFSDomain": "ifs.example"})
    assert path.parent == tmp_path / "targets" / "ifs-orders" and tg.list_targets("ifs-orders") == ["dev"]
    with pytest.raises(tg.TargetError, match="already exists"):
        tg.write_target("ifs-orders", "dev", envs[0])
    with pytest.raises(tg.TargetError, match="no target 'prod'"):
        tg.load_target("ifs-orders", "prod")


def test_solution_file_contents():
    sol = sz.SolutionFile("IfsOrders", "IFS Orders", "1.0.0.4", sz.Publisher("Pub", "Pub", "ti", 12677),
                          [sz.ConnectorPart("c-1", "ti_ifsx", "IFS X", "d & e", {"swagger": "2.0"}, {"u": {}})],
                          [sz.VariablePart("ti_V", "V", "desc", "string")],
                          [sz.ReferencePart("ti_IfsX", "IFS X", "/providers/Microsoft.PowerApps/apis/shared_x")])
    solution, cust = sz.solution_xml(sol), sz.customizations_xml(sol)
    assert '<RootComponent type="372" id="{c-1}" schemaName="ti_ifsx"' in solution
    assert "<CustomizationPrefix>ti</CustomizationPrefix>" in solution and "<Managed>0</Managed>" in solution
    assert "<capabilities>[-1,118690003,-1]</capabilities>" in cust and "d &amp; e" in cust
    assert 'connectionreferencelogicalname="ti_IfsX"' in cust
    assert "<type>100000000</type>" in sz.variable_definition_xml(sol.variables[0])


def test_doctor_checks_targets(setup, monkeypatch):
    from ifs_agent_ops import doctor

    package, _, target, _, _ = setup
    (package.root / "deploy.yaml").write_text(scaffold(package, "TietexAdmin", "ti"), encoding="utf-8")
    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(package.root))
    listing = json.dumps([{"FriendlyName": "Dev", "EnvironmentIdentifier": {"Id": "env-1"}}])
    monkeypatch.setattr(doctor, "_run", lambda cmd, timeout=20.0: listing if cmd[:3] == ["pac", "env", "list"] else None)
    [check] = doctor._deployment_targets("power-platform", True)
    assert (check.name, check.status, check.detail) == ("ifs-orders target dev", doctor.PASS, "Dev reachable")
    monkeypatch.setattr(doctor, "_run", lambda cmd, timeout=20.0: "[]")
    assert doctor._deployment_targets("power-platform", True)[0].status == doctor.FAIL
    target.path.unlink()
    [check] = doctor._deployment_targets("power-platform", True)
    assert check.status == doctor.WARN and "targets init" in check.fix
