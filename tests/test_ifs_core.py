"""Unit tests for Phase 2 additions that legacy parity cannot cover."""
import asyncio
import ssl

import httpx
import respx

from ifs_agent_ops.ifs import tls
from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.ifs.metadata.edmx import parse_edmx
from ifs_agent_ops.ifs.paths import metadata_path, openapi_v2_path


# ── TLS ──────────────────────────────────────────────────────────────────────

def test_tls_default_uses_os_trust_store(monkeypatch):
    monkeypatch.delenv("IFS_TLS_VERIFY", raising=False)
    monkeypatch.delenv("IFS_CA_BUNDLE", raising=False)
    assert isinstance(tls.verify_from_env(), ssl.SSLContext)


def test_tls_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("IFS_TLS_VERIFY", "false")
    assert tls.verify_from_env() is False


def test_tls_ca_bundle_overrides_trust_store(monkeypatch):
    monkeypatch.delenv("IFS_TLS_VERIFY", raising=False)
    monkeypatch.setenv("IFS_CA_BUNDLE", "/certs/corp.pem")
    assert tls.verify_from_env() == "/certs/corp.pem"


# ── IfsClient connection reuse ───────────────────────────────────────────────

class _Tokens:
    def current_session_id(self):
        return "s"

    async def get_access_token(self, session_id):
        return "t"


def test_client_reuses_http_connection_within_a_loop():
    client = IfsClient("https://example.ifs.cloud", _Tokens())

    async def two_calls():
        with respx.mock() as router:
            router.get("https://example.ifs.cloud/x").mock(return_value=httpx.Response(200, json={}))
            await client.request("GET", "/x")
            first = client._http
            await client.request("GET", "/x")
            assert client._http is first
        await client.aclose()

    asyncio.run(two_calls())


def test_client_gets_fresh_http_client_on_new_loop():
    client = IfsClient("https://example.ifs.cloud", _Tokens())

    async def one_call():
        with respx.mock() as router:
            router.get("https://example.ifs.cloud/x").mock(return_value=httpx.Response(200, json={}))
            await client.request("GET", "/x")
        return client._http

    first = asyncio.run(one_call())
    second = asyncio.run(one_call())
    assert first is not second


# ── EDMX model ───────────────────────────────────────────────────────────────

EDMX = """<?xml version="1.0"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
 <edmx:DataServices>
  <Schema Namespace="IfsApp.Shop" xmlns="http://docs.oasis-open.org/odata/ns/edm">
   <EnumType Name="OperStatus"><Member Name="Released"/><Member Name="Closed"/></EnumType>
   <EntityType Name="ShopOrd">
    <Key><PropertyRef Name="OrderNo"/><PropertyRef Name="ReleaseNo"/></Key>
    <Property Name="OrderNo" Type="Edm.String" Nullable="false"/>
    <Property Name="ReleaseNo" Type="Edm.String" Nullable="false"/>
    <NavigationProperty Name="OperationArray" Type="Collection(IfsApp.Shop.ShopOrderOperation)"/>
    <NavigationProperty Name="PartRef" Type="IfsApp.Shop.Part"/>
   </EntityType>
   <EntityType Name="ShopOrderOperation">
    <Key><PropertyRef Name="OperationNo"/></Key>
    <Property Name="OperationNo" Type="Edm.Double"/>
    <Property Name="Status" Type="IfsApp.Shop.OperStatus"/>
   </EntityType>
   <EntityContainer Name="Main">
    <EntitySet Name="ShopOrds" EntityType="IfsApp.Shop.ShopOrd"/>
   </EntityContainer>
  </Schema>
 </edmx:DataServices>
</edmx:Edmx>"""


def test_edmx_keys_properties_and_navigation():
    m = parse_edmx(EDMX)
    shop = m.entity_type_of_set("ShopOrds")
    assert shop.key == ["OrderNo", "ReleaseNo"]
    assert [p.nullable for p in shop.properties] == [False, False]
    nav = {n.name: n for n in shop.navigation}
    assert nav["OperationArray"].target == "IfsApp.Shop.ShopOrderOperation" and nav["OperationArray"].is_collection
    assert nav["PartRef"].target == "IfsApp.Shop.Part" and not nav["PartRef"].is_collection


def test_edmx_enum_properties_resolve_through_navigation_target():
    m = parse_edmx(EDMX)
    target = m.entity_type_of_set("ShopOrds").navigation[0].target
    enums = m.enum_properties(target)
    assert list(enums) == ["Status"]
    assert enums["Status"].fqn == "IfsApp.Shop.OperStatus"
    assert enums["Status"].members == ["Released", "Closed"]


def test_projection_paths():
    assert metadata_path("Demo") == "/main/ifsapplications/projection/v1/Demo.svc/$metadata"
    assert openapi_v2_path("Demo") == "/main/ifsapplications/projection/v1/Demo.svc/$openapi?V2"
