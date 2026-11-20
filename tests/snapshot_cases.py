"""Characterization cases shared by the legacy capture script and the test suite.

Each case runs against a "flavor": the legacy ``ifs_mcp_server`` package (used once, by
``scripts/capture_legacy_snapshots.py``, to record expected output) or the migrated
``ifs_agent_ops`` package (used by the tests). Outputs must match byte for byte.

How a flavor is driven (which seams are patched or injected) is the job of an *adapter*,
so the cases and snapshots stay identical while internals are refactored.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import importlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import httpx
import respx

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = Path(__file__).parent / "snapshots"

HAR_FIXTURE = FIXTURES / "customer_order_flow.har"
OPENAPI_FIXTURE = FIXTURES / "ShopFloorClockingsHandling.openapi.json"
EDMX_FIXTURE = FIXTURES / "CustomerOrderHandling.metadata.xml"

BASE = "https://example.ifs.cloud"
PROJ = BASE + "/main/ifsapplications/projection/v1"
TOKEN_URL = BASE + "/auth/realms/example/protocol/openid-connect/token"
NOW = 1_800_000_000.0  # frozen clock for session/expiry cases
NOW_MS = int(NOW * 1000)

# Module paths per flavor.
MODULES = {
    "legacy": {
        "har": "ifs_mcp_server.lib.har_parser",
        "openapi": "ifs_mcp_server.lib.openapi_parser",
        "enum_map": "ifs_mcp_server.tools.api.get_projection_enum_map",
        "server": "ifs_mcp_server.server",
        "api_client": "ifs_mcp_server.lib.api_client",
        "call_api": "ifs_mcp_server.tools.api.call_protected_api",
        "export": "ifs_mcp_server.tools.api.export_api_data",
        "session_info": "ifs_mcp_server.tools.auth.get_session_info",
        "start_oauth": "ifs_mcp_server.tools.auth.start_oauth",
        "session_manager": "ifs_mcp_server.lib.auth.session_manager",
        "token_store": "ifs_mcp_server.lib.auth.token_store",
        "oauth_manager": "ifs_mcp_server.lib.auth.oauth_manager",
    },
    "new": {
        "har": "ifs_agent_ops.analysis.har_legacy",
        "openapi": "ifs_agent_ops.ifs.metadata.openapi",
        "enum_map": "ifs_agent_ops.mcp_server.tools.api.get_projection_enum_map",
        "server": "ifs_agent_ops.mcp_server.server",
        "call_api": "ifs_agent_ops.mcp_server.tools.api.call_protected_api",
        "export": "ifs_agent_ops.mcp_server.tools.api.export_api_data",
        "session_info": "ifs_agent_ops.mcp_server.tools.auth.get_session_info",
        "start_oauth": "ifs_agent_ops.mcp_server.tools.auth.start_oauth",
    },
}


def _mod(flavor: str, key: str):
    return importlib.import_module(MODULES[flavor][key])


def _to_json(obj) -> str:
    return json.dumps(dataclasses.asdict(obj), indent=2, sort_keys=True)


# ─────────────────────────────────────────────────────────────────────────────
# Pure parsers
# ─────────────────────────────────────────────────────────────────────────────

def har_outputs(flavor: str) -> dict[str, str]:
    m = _mod(flavor, "har")
    parsed = m.parse_har(str(HAR_FIXTURE))
    return {"har_parsed.json": _to_json(parsed), "har_summary.md": m.summarise_har(parsed)}


def openapi_outputs(flavor: str) -> dict[str, str]:
    m = _mod(flavor, "openapi")
    parsed = m.parse_openapi(str(OPENAPI_FIXTURE))
    return {"openapi_parsed.json": _to_json(parsed), "openapi_summary.md": m.summarise_openapi(parsed)}


# ─────────────────────────────────────────────────────────────────────────────
# Scenario + adapters
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Scenario:
    """State of the world for one tool call."""
    current_session: str | None = "test-session"
    token_error: str | None = None          # fake auth: raise ValueError(token_error)
    real_auth: bool = False                 # use the real OAuth manager + session file
    sessions: dict | None = None            # session.json contents (real_auth / session cases)
    load_sessions: bool = True              # load session.json into memory at "startup"


class _FakeOAuth:
    def __init__(self, token_error: str | None = None):
        self.token_error = token_error

    async def get_access_token(self, session_id: str) -> str:
        if self.token_error:
            raise ValueError(self.token_error)
        return "test-token"


@contextlib.contextmanager
def _patched(*triples):
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in triples]
    try:
        for obj, name, value in triples:
            setattr(obj, name, value)
        yield
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


@contextlib.contextmanager
def _env(**values):
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update({k: v for k, v in values.items()})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class PatchAdapter:
    """Drives the legacy module layout by patching module-level seams."""

    def __init__(self, flavor: str):
        self.flavor = flavor
        self.m = lambda key: _mod(flavor, key)

    @contextlib.contextmanager
    def world(self, sc: Scenario, tmp: Path):
        sm, ts = self.m("session_manager"), self.m("token_store")
        session_file = tmp / "session.json"
        if sc.sessions is not None:
            session_file.write_text(json.dumps(sc.sessions, indent=2), encoding="utf-8")
        triples = [
            (sm, "_SESSION_DIR", tmp),
            (sm, "_SESSION_FILE", session_file),
            (ts, "_store", {}),
            (time, "time", lambda: NOW),
        ]
        if not sc.real_auth:
            current = lambda: sc.current_session  # noqa: E731
            for key in ("api_client", "enum_map"):
                triples.append((self.m(key), "get_current_session_id", current))
        for key in ("api_client", "enum_map"):
            triples.append((self.m(key), "get_api_base_url", lambda: BASE))
        with _patched(*triples), _env(
            IFS_MCP_EXPORT_DIR=str(tmp / "exports"),
            API_BASE_URL=BASE, OAUTH_REALM="example", OAUTH_CLIENT_ID="test-client",
        ):
            if sc.load_sessions:
                sm.initialize_token_store()
            self._session_file = session_file
            yield

    def auth(self, sc: Scenario):
        return self.m("oauth_manager").OAuthManager() if sc.real_auth else _FakeOAuth(sc.token_error)

    def call_api(self, sc, **kw):
        return asyncio.run(self.m("call_api").handler(oauth_manager=self.auth(sc), **kw))

    def export(self, sc, **kw):
        return asyncio.run(self.m("export").handler(oauth_manager=self.auth(sc), **kw))

    def enum_map(self, sc, **kw):
        return asyncio.run(self.m("enum_map").handler(oauth_manager=self.auth(sc), **kw))

    def session_info(self, sc):
        return asyncio.run(self.m("session_info").handler())

    def start_oauth_existing(self, sc):
        return asyncio.run(self.m("start_oauth").handler(False, self.m("oauth_manager").OAuthManager()))

    def save_session(self, sc, session_id: str, token: dict):
        ts = self.m("token_store")
        self.m("session_manager").save_session(session_id, ts.TokenData(**token))

    def session_file_text(self) -> str:
        return self._session_file.read_text(encoding="utf-8") if self._session_file.exists() else ""


class _FakeProvider:
    """TokenProvider double for the injected client (mirrors _FakeOAuth + patched session id)."""

    def __init__(self, current_session: str | None, token_error: str | None):
        self._current = current_session
        self._token_error = token_error

    def current_session_id(self):
        return self._current

    async def get_access_token(self, session_id: str) -> str:
        if self._token_error:
            raise ValueError(self._token_error)
        return "test-token"


class InjectAdapter:
    """Drives the refactored package: every seam is a constructor argument."""

    def __init__(self, flavor: str):
        self.m = lambda key: _mod(flavor, key)

    @contextlib.contextmanager
    def world(self, sc: Scenario, tmp: Path):
        from ifs_agent_ops.ifs.auth.pkce import PkceAuth
        from ifs_agent_ops.ifs.auth.token_store import FileTokenStore
        from ifs_agent_ops.ifs.client import IfsClient
        from ifs_agent_ops.ifs.settings import IfsSettings

        self._session_file = tmp / "session.json"
        if sc.sessions is not None:
            self._session_file.write_text(json.dumps(sc.sessions, indent=2), encoding="utf-8")
        settings = IfsSettings(base_url=BASE, realm="example", client_id="test-client", verify=True)
        with _patched((time, "time", lambda: NOW)), _env(IFS_MCP_EXPORT_DIR=str(tmp / "exports")):
            self.store = FileTokenStore(self._session_file)
            if sc.load_sessions:
                self.store.load()
            self.pkce = PkceAuth(settings, self.store)
            provider = self.pkce if sc.real_auth else _FakeProvider(sc.current_session, sc.token_error)
            self.client = IfsClient(BASE, provider, verify=True)
            yield

    def call_api(self, sc, **kw):
        return asyncio.run(self.m("call_api").handler(client=self.client, **kw))

    def export(self, sc, **kw):
        return asyncio.run(self.m("export").handler(client=self.client, **kw))

    def enum_map(self, sc, **kw):
        return asyncio.run(self.m("enum_map").handler(client=self.client, **kw))

    def session_info(self, sc):
        return asyncio.run(self.m("session_info").handler(self.store))

    def start_oauth_existing(self, sc):
        return asyncio.run(self.m("start_oauth").handler(False, self.pkce, self.store))

    def save_session(self, sc, session_id: str, token: dict):
        from ifs_agent_ops.ifs.auth.token_store import TokenData

        self.store.save(session_id, TokenData(**token))

    def session_file_text(self) -> str:
        return self._session_file.read_text(encoding="utf-8") if self._session_file.exists() else ""


ADAPTERS = {"legacy": PatchAdapter, "new": InjectAdapter}


# ─────────────────────────────────────────────────────────────────────────────
# Tool cases
# ─────────────────────────────────────────────────────────────────────────────

def _calls(router) -> list[dict]:
    out = []
    for call in router.calls:
        req = call.request
        hdrs = {k: req.headers[k] for k in ("accept", "content-type", "authorization", "if-match") if k in req.headers}
        out.append({"method": req.method, "url": str(req.url), "headers": hdrs,
                    "body": req.content.decode("utf-8", "replace")})
    return out


def _normalize(text: str, tmp: Path) -> str:
    text = text.replace(str(tmp).replace("\\", "\\\\"), "<TMP>").replace(str(tmp), "<TMP>")
    return re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}", "<TS>", text)


def _big_records(n: int) -> list[dict]:
    return [{f"Field{j}": f"value-{i:05d}-{j}" for j in range(8)} for i in range(n)]


SESSION_VALID = {"s1": {"access_token": "a1", "refresh_token": "r1", "expires_at": NOW_MS + 3_600_000, "user_id": "s1"}}
SESSION_EXPIRED = {"s1": {"access_token": "a1", "refresh_token": "r1", "expires_at": NOW_MS - 1_000, "user_id": "s1"}}
SESSION_EXPIRED_NO_REFRESH = {"s1": {"access_token": "a1", "refresh_token": None, "expires_at": NOW_MS - 1_000, "user_id": "s1"}}

EP = "/main/ifsapplications/projection/v1/Demo.svc/ThingSet"


def _cases():
    """name -> (scenario, routes(router), action(adapter, sc) -> str)."""
    j = lambda status, body: httpx.Response(status, json=body)  # noqa: E731
    cases = {}

    def add(name, sc, routes, action):
        cases[name] = (sc, routes, action)

    cpa = lambda **kw: (lambda a, sc: a.call_api(sc, **{"session_id": None, "body": None, "headers": None, **kw}))  # noqa: E731
    get_ok = lambda r: r.get(PROJ + "/Demo.svc/ThingSet").mock(return_value=j(200, {"value": [{"A": 1}]}))  # noqa: E731

    add("cpa_success_json", Scenario(), get_ok, cpa(endpoint=EP, method="GET"))
    add("cpa_success_text", Scenario(), lambda r: r.get(PROJ + "/Demo.svc/ThingSet").mock(
        return_value=httpx.Response(200, text="plain text")), cpa(endpoint=EP, method="GET"))
    add("cpa_post_body_headers", Scenario(), lambda r: r.post(PROJ + "/Demo.svc/ThingSet").mock(
        return_value=j(201, {"Id": 7})), cpa(endpoint=EP, method="POST", body={"Name": "x"}, headers={"If-Match": 'W/"1"'}))
    add("cpa_patch_empty_body", Scenario(), lambda r: r.patch(PROJ + "/Demo.svc/ThingSet").mock(
        return_value=httpx.Response(204)), cpa(endpoint=EP, method="PATCH", body={}))
    add("cpa_401", Scenario(), lambda r: r.get(PROJ + "/Demo.svc/ThingSet").mock(
        return_value=j(401, {"error": "x"})), cpa(endpoint=EP, method="GET"))
    add("cpa_403", Scenario(), lambda r: r.get(PROJ + "/Demo.svc/ThingSet").mock(
        return_value=httpx.Response(403, text="forbidden")), cpa(endpoint=EP, method="GET"))
    add("cpa_500_json", Scenario(), lambda r: r.get(PROJ + "/Demo.svc/ThingSet").mock(
        return_value=j(500, {"error": {"code": "ODP_X", "message": "boom"}})), cpa(endpoint=EP, method="GET"))
    add("cpa_404_text", Scenario(), lambda r: r.get(PROJ + "/Demo.svc/ThingSet").mock(
        return_value=httpx.Response(404, text="not found")), cpa(endpoint=EP, method="GET"))
    add("cpa_302", Scenario(), lambda r: r.get(PROJ + "/Demo.svc/ThingSet").mock(
        return_value=httpx.Response(302, headers={"location": "/elsewhere"}, text="moved")), cpa(endpoint=EP, method="GET"))
    add("cpa_large", Scenario(), lambda r: r.get(PROJ + "/Demo.svc/ThingSet").mock(
        return_value=j(200, {"value": _big_records(1500)})), cpa(endpoint=EP, method="GET"))
    add("cpa_no_session", Scenario(current_session=None), get_ok, cpa(endpoint=EP, method="GET"))
    add("cpa_explicit_session", Scenario(current_session=None), get_ok, cpa(endpoint=EP, method="GET", session_id="explicit"))
    add("cpa_token_error", Scenario(token_error="No refresh token available"), get_ok, cpa(endpoint=EP, method="GET"))

    def connect_error(a, sc):
        try:
            return cpa(endpoint=EP, method="GET")(a, sc)
        except Exception as exc:  # legacy re-raises transport errors
            return f"raised {type(exc).__name__}: {exc}"
    add("cpa_connect_error", Scenario(), lambda r: r.get(PROJ + "/Demo.svc/ThingSet").mock(
        side_effect=httpx.ConnectError("connection refused")), connect_error)

    # Real OAuth manager: refresh paths (expired access token).
    def refresh_ok(r):
        r.post(TOKEN_URL).mock(return_value=j(200, {"access_token": "a2", "expires_in": 1800}))
        get_ok(r)
    add("cpa_refresh_ok", Scenario(real_auth=True, sessions=SESSION_EXPIRED), refresh_ok, cpa(endpoint=EP, method="GET"))

    def refresh_fail(r):
        r.post(TOKEN_URL).mock(return_value=j(400, {"error": "invalid_grant"}))
        get_ok(r)
    add("cpa_refresh_fail", Scenario(real_auth=True, sessions=SESSION_EXPIRED), refresh_fail, cpa(endpoint=EP, method="GET"))
    add("cpa_expired_no_refresh", Scenario(real_auth=True, sessions=SESSION_EXPIRED_NO_REFRESH), get_ok, cpa(endpoint=EP, method="GET"))
    add("cpa_valid_session_file", Scenario(real_auth=True, sessions=SESSION_VALID), get_ok, cpa(endpoint=EP, method="GET"))
    add("cpa_session_not_loaded", Scenario(real_auth=True, sessions=SESSION_VALID, load_sessions=False), get_ok,
        cpa(endpoint=EP, method="GET"))

    # Export.
    exp = lambda **kw: (lambda a, sc: a.export(sc, **{"filename": "things", "session_id": None, "body": None, **kw}))  # noqa: E731

    def pages(r, total=250, fail_on_skip=None):
        recs = [{"Id": i, "Name": f"n{i}", "Note": 'semi;colon "q"' if i == 3 else None} for i in range(total)]

        def handler(request):
            skip = int(request.url.params.get("$skip", "0"))
            if fail_on_skip is not None and skip == fail_on_skip:
                return httpx.Response(500, json={"error": "page failed"})
            top = int(request.url.params.get("$top", "100"))
            return httpx.Response(200, json={"value": recs[skip:skip + top]})
        r.get(url__startswith=PROJ + "/Demo.svc/ThingSet").mock(side_effect=handler)
    add("exp_paged", Scenario(), pages, exp(endpoint=EP, method="GET"))
    add("exp_strip_pagination", Scenario(), pages,
        exp(endpoint=EP + "?$filter=Name eq 'n1'&$top=5&$skip=10&$select=Id,Name", method="GET"))
    add("exp_empty", Scenario(), lambda r: pages(r, total=0), exp(endpoint=EP, method="GET"))
    add("exp_fail_page2", Scenario(), lambda r: pages(r, fail_on_skip=100), exp(endpoint=EP, method="GET"))
    add("exp_401", Scenario(), lambda r: r.get(url__startswith=PROJ + "/Demo.svc/ThingSet").mock(
        return_value=j(401, {})), exp(endpoint=EP, method="GET"))
    add("exp_no_session", Scenario(current_session=None), pages, exp(endpoint=EP, method="GET"))
    add("exp_connect_error", Scenario(), lambda r: r.get(url__startswith=PROJ + "/Demo.svc/ThingSet").mock(
        side_effect=httpx.ConnectError("connection refused")), exp(endpoint=EP, method="GET"))

    # Enum map (success cases + error paths).
    meta_url = PROJ + "/CustomerOrderHandling.svc/$metadata"
    edmx = EDMX_FIXTURE.read_text(encoding="utf-8")
    meta_ok = lambda r: r.get(meta_url).mock(return_value=httpx.Response(200, text=edmx))  # noqa: E731
    em = lambda **kw: (lambda a, sc: a.enum_map(sc, **{"projection_name": "CustomerOrderHandling",  # noqa: E731
                                                       "entity_set": None, "include_reference_sets": False, **kw}))
    add("enum_map_all", Scenario(), meta_ok, em())
    add("enum_map_with_refs", Scenario(), meta_ok, em(include_reference_sets=True))
    add("enum_map_entity_set", Scenario(), meta_ok, em(entity_set="CustomerOrderSet"))
    add("enum_map_missing_set", Scenario(), meta_ok, em(entity_set="NoSuchSet"))
    add("enum_401", Scenario(), lambda r: r.get(meta_url).mock(return_value=httpx.Response(401)), em())
    add("enum_500", Scenario(), lambda r: r.get(meta_url).mock(return_value=httpx.Response(500, text="x" * 600)), em())
    add("enum_bad_xml", Scenario(), lambda r: r.get(meta_url).mock(return_value=httpx.Response(200, text="<not-xml")), em())
    add("enum_no_session", Scenario(current_session=None), meta_ok, em())
    add("enum_token_error", Scenario(token_error="No refresh token available"), meta_ok, em())
    add("enum_connect_error", Scenario(), lambda r: r.get(meta_url).mock(side_effect=httpx.ConnectError("connection refused")), em())

    # Session info / start_oauth / session file format.
    si = lambda a, sc: a.session_info(sc)  # noqa: E731
    add("session_info_none", Scenario(real_auth=True, sessions={}), None, si)
    add("session_info_valid", Scenario(real_auth=True, sessions=SESSION_VALID), None, si)
    add("session_info_expired_no_refresh", Scenario(real_auth=True, sessions=SESSION_EXPIRED_NO_REFRESH), None, si)
    add("session_info_not_loaded", Scenario(real_auth=True, sessions=SESSION_VALID, load_sessions=False), None, si)
    add("start_oauth_already_authenticated", Scenario(real_auth=True, sessions=SESSION_VALID), None,
        lambda a, sc: a.start_oauth_existing(sc))

    def save_and_read(a, sc):
        a.save_session(sc, "s2", {"access_token": "a2", "refresh_token": None, "expires_at": NOW_MS, "user_id": "s2"})
        return a.session_file_text()
    add("session_file_append", Scenario(real_auth=True, sessions=SESSION_VALID), None, save_and_read)
    return cases


# Cases whose snapshot names predate the "tools/" folder keep their original file names.
_LEGACY_NAMES = {"enum_map_all", "enum_map_with_refs", "enum_map_entity_set", "enum_map_missing_set"}


def tool_outputs(flavor: str) -> dict[str, str]:
    adapter = ADAPTERS[flavor](flavor)
    out: dict[str, str] = {}
    for name, (sc, routes, action) in _cases().items():
        tmp = Path(tempfile.mkdtemp(prefix="ifsops-"))
        try:
            with adapter.world(sc, tmp), respx.mock(assert_all_called=False) as router:
                if routes:
                    routes(router)
                result = action(adapter, sc)
                if name in _LEGACY_NAMES:
                    out[f"{name}.json"] = result
                    continue
                record = {"output": _normalize(result, tmp), "requests": _calls(router)}
                exports = tmp / "exports"
                if exports.exists():
                    record["export_files"] = {
                        _normalize(p.name, tmp): p.read_text(encoding="utf-8") for p in sorted(exports.iterdir())
                    }
                if sc.real_auth and name.startswith("cpa_"):
                    record["session_file_after"] = adapter.session_file_text()
                out[f"tools/{name}.json"] = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def mcp_surface(flavor: str) -> dict[str, str]:
    """Tool and prompt definitions exposed by the server (name excluded: it changed by design)."""
    server = _mod(flavor, "server")

    async def collect():
        tools = await server.mcp.list_tools()
        prompts = await server._list_prompts()
        return tools, prompts

    tools, prompts = asyncio.run(collect())
    data = {
        "tools": sorted(
            (
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                    "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else None,
                }
                for t in tools
            ),
            key=lambda t: t["name"],
        ),
        "prompts": sorted(
            (p.model_dump(exclude_none=True) for p in prompts),
            key=lambda p: p["name"],
        ),
    }
    return {"mcp_surface.json": json.dumps(data, indent=2, sort_keys=True)}


ALL_CASES = (har_outputs, openapi_outputs, tool_outputs, mcp_surface)
