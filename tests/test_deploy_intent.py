"""Deployment intent (deploy.yaml): parse, validate, scaffold, CLI."""
import json

import pytest
import yaml

from ifs_agent_ops.deploy import intent as di
from ifs_agent_ops.workspace.package import init_package


@pytest.fixture
def package(tmp_path):
    p = init_package(tmp_path / "pkg", "ifs-customer-orders", display_name="Customer Orders",
                     projections=["CustomerOrderHandling", "CustomerHandling"])
    folder = p.root / "power-platform" / "connectors" / "CustomerOrderHandling"   # generated: one service used
    folder.mkdir(parents=True)
    for name in ("apiDefinition.swagger.json", "apiProperties.json", "operation-ids.yaml"):
        (folder / name).write_text("{}", encoding="utf-8")
    return p


def _intent(package, **changes):
    data = yaml.safe_load(di.scaffold(package, "ContosoAdmin", "ti"))
    pp = data["power_platform"]
    for key, value in changes.items():
        section, _, field = key.partition("__")
        target = pp[section] if section == "solution" else pp[section][0]
        target[field] = value
    return di.parse(data, package.root / di.DEPLOY_FILE)


def test_scaffold_is_valid_and_has_only_logical_names(package):
    text = di.scaffold(package, "ContosoAdmin", "ti")
    intent = di.parse(yaml.safe_load(text), package.root / di.DEPLOY_FILE)
    assert di.validate(intent, package) == ([], [])
    pp = intent.power_platform
    assert (pp.solution.unique_name, pp.solution.display_name, pp.solution.prefix) == \
        ("IfsCustomerOrders", "IFS Customer Orders", "ti")
    [connector] = pp.connectors                     # one per generated IFS service
    assert (connector.logical_name, connector.display_name, connector.source) == (
        "customer-order-handling", "IFS Customer Order Handling", "power-platform/connectors/CustomerOrderHandling")
    assert connector.host_from == "ti_IFSDomain" == pp.environment_variables[0].schema_name
    assert [(r.logical_name, r.connector) for r in pp.connection_references] == [
        ("ti_IfsCustomerOrderHandling", "customer-order-handling")]
    assert "http" not in text and "ifs.example" not in text


@pytest.mark.parametrize("change, message", [
    ({"solution__unique_name": "Ifs Customer"}, "solution.unique_name"),
    ({"solution__prefix": "TI"}, "solution.prefix"),
    ({"environment_variables__schema_name": "new_IFSDomain"}, "schema_name must be ti_"),
    ({"environment_variables__type": "secret"}, "type must be one of"),
    ({"connection_references__connector": "nope"}, "connector 'nope' is not declared"),
    ({"connection_references__logical_name": "IfsCustomerOrders"}, "logical_name must be ti_"),
    ({"connectors__host_from": "ti_Missing"}, "is not a declared environment variable"),
    ({"connectors__source": "../elsewhere"}, "must be a folder inside the package"),
    ({"connectors__display_name": "IFS Customer Order Handling Extra"}, "Power Platform allows 30"),
])
def test_validation_errors(package, change, message):
    errors, _ = di.validate(_intent(package, **change), package)
    assert any(message in e for e in errors), errors


def test_warnings_for_ungenerated_connector_and_missing_reference(package):
    (package.root / "power-platform" / "connectors" / "CustomerOrderHandling" / "apiProperties.json").unlink()
    intent = _intent(package)
    intent.power_platform.connection_references.clear()
    errors, warnings = di.validate(intent, package)
    assert errors == []
    assert any("apiProperties.json not generated" in w for w in warnings)
    assert any("no connection reference" in w for w in warnings)


def test_display_names_fit_the_power_platform_limit():
    assert di.connector_display_name("CustomerOrderHandling") == "IFS Customer Order Handling"
    assert len(di.connector_display_name("ShopFloorWorkbenchOperationHandling")) <= 30


def test_parse_rejects_unknown_keys_and_missing_fields(package):
    with pytest.raises(di.IntentError, match="unknown key"):
        di.parse({"power_platform": {"solution": {}, "flows": []}}, package.root)
    with pytest.raises(di.IntentError, match="missing: prefix"):
        di.parse({"power_platform": {"solution": {"unique_name": "A", "display_name": "A", "publisher": "P"}}},
                 package.root)
    assert di.parse({}, package.root).power_platform is None


def test_cli_init_then_check(package, monkeypatch, capsys):
    from ifs_agent_ops.cli.main import main

    monkeypatch.setenv("IFS_AGENT_OPS_PACKAGES", str(package.root))
    argv = ["deploy", "init", "--package", "ifs-customer-orders", "--publisher", "ContosoAdmin", "--prefix", "ti"]
    assert main(argv) == 0
    assert di.load_intent(package).power_platform.solution.publisher == "ContosoAdmin"
    assert main(argv) == 2                                  # never overwrites
    capsys.readouterr()
    assert main(["deploy", "check", "--package", "ifs-customer-orders", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    di.deploy_path(package).write_text("power_platform:\n  solution: {unique_name: X}\n", encoding="utf-8")
    assert main(["deploy", "check", "--package", "ifs-customer-orders"]) == 1
    assert "FAIL" in capsys.readouterr().out
