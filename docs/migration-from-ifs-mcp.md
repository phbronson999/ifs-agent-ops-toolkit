# Migration from ifs-mcp-server-local-python

The initial source of this toolkit was copied from
`mcp-starter/ifs-mcp/ifs-mcp-server-local-python` at commit `8f37c7b`
(fork: https://github.com/phbronson999/ifs-mcp-server-local, branch `python-port`).
History for that code remains in those repositories.

## Phase 0 module map (import renames only)

| Old (`ifs_mcp_server`) | New (`ifs_agent_ops`) |
|---|---|
| `config.py` | `config.py` |
| `lib/api_client.py` | `ifs/client.py` |
| `lib/auth/*` | `ifs/auth/*` |
| `lib/openapi_parser.py` | `ifs/metadata/openapi.py` |
| `lib/har_parser.py` | `analysis/har_legacy.py` |
| `server.py` | `mcp_server/server.py` |
| `tools/*`, `prompts/*`, `resources/*` | `mcp_server/tools/*`, `mcp_server/prompts/*`, `mcp_server/resources/*` |
| `skills/*.md` | `skills/*.md` (moves to operation packages in Phase 5) |
| `../../icon.png` | `assets/icon.png` (loaded via `importlib.resources`) |

## Intentional differences in Phase 0

- MCP server name is `ifs-agent-ops` (was `ifs-mcp-server`). Tool, prompt and resource names are unchanged.
- Entry point is `ifs-agent-ops-mcp` (or `python -m ifs_agent_ops.mcp_server.server`).
- `.env` is still loaded from the repository root.
- `SKILLS_DIR` must be repointed to `src/ifs_agent_ops/skills` in this repo.
- Session/export/scratch locations under `~/.ifs-mcp/` are unchanged, so existing sessions keep working.

## Phase 2: IFS client decoupled

| Before | After |
|---|---|
| `lib/api_client.py` function + `ApiResponse` with LLM wording | `ifs/client.py` `IfsClient` (raises `ifs/errors.py` types); wording in `mcp_server/presenters.py` |
| `lib/auth/oauth_manager.py` singleton reading env per call | `ifs/auth/pkce.py` `PkceAuth(settings, store)` |
| `lib/auth/session_manager.py` + `token_store.py` module globals | `ifs/auth/token_store.py` `FileTokenStore(path)` (same file format and semantics) |
| Callback server / login page inside the `start_oauth` tool | `ifs/auth/callback.py` |
| EDMX fetch + parse inside `get_projection_enum_map` | `client.get_text()` + `ifs/metadata/edmx.py` neutral `EdmxModel` |
| Pagination loop inside `export_api_data` | `IfsClient.paginate()` |
| `verify=False` everywhere | OS trust store by default (`IFS_TLS_VERIFY`, `IFS_CA_BUNDLE`) |
| `initialize_token_store()` at import | `mcp_server/runtime.py` `Runtime.from_env()` |

Tool names, inputs and outputs are unchanged; 42 tool-level snapshots captured from the
legacy server (`tests/snapshots/tools/`) verify this, including auth failure, refresh,
pagination and session-file cases.

## Phase 3: profiles and toolkit home

One MCP server process serves one IFS profile. Run one server per environment (e.g. TEST on
port 8000, PROD on 8001) and register each in your MCP client config.

`~/.ifs-agent-ops/profiles.toml` (or `$IFS_AGENT_OPS_HOME/profiles.toml`):

```toml
default_profile = "test"

[profiles.test]
base_url = "https://ifs-test.example.com"
realm = "testrealm"
client_id = "ifs-agent-ops"
callback_port = 3000        # must be registered as http://localhost:3000/oauth/callback in IFS IAM
mcp_server_port = 8000

[profiles.prod]
base_url = "https://ifs-prod.example.com"
realm = "prodrealm"
client_id = "ifs-agent-ops"
callback_port = 3001
mcp_server_port = 8001
# tls_verify = false / ca_bundle = "C:/certs/corp.pem"
```

- Selection: `--profile` > `IFS_PROFILE` > `default_profile`; with none, the `.env` variables
  form an implicit `default` profile (unchanged behaviour for existing setups).
- Named profiles never read the legacy `API_BASE_URL`/`OAUTH_*` variables, so a `.env` pointing
  at TEST cannot leak into a PROD profile.
- Sessions: `~/.ifs-agent-ops/profiles/<name>/session.json`. The `default` profile copies
  `~/.ifs-mcp/session.json` once on first use (copy, not move: the legacy server keeps working;
  the two then diverge).
- Profiles that run at the same time need distinct `callback_port`s; `ifs-agent-ops profiles`
  flags shared ports and shows each profile's sign-in state.
- Unchanged for now: export/scratch defaults stay under `~/.ifs-mcp/` (they are named in tool
  descriptions covered by the parity snapshot).

## Phase 4: recordings and analysis split

| Layer | Module | Purpose |
|---|---|---|
| Evidence | `recordings/har.py` | `load_har()` -> ordered `ObservedCall` list: every entry, headers, bodies, status. `payload()` unwraps IFS server-trace envelopes |
| Evidence | `recordings/sanitize.py` | `sanitize_har()` -> copy without credentials, `_initiator`, `serverIPAddress`, trace infrastructure details or SQL bind values |
| Analysis | `analysis/odata.py` | projection URL + OData query parsing, `$filter` anonymisation |
| Analysis | `analysis/grouping.py`, `summary.py` | legacy grouped view (`parse_har_file` output, unchanged) |
| Analysis | `analysis/sequence.py` | ordered view + data flow (keys, body values, ETag -> If-Match) |

- New MCP tool `analyze_recording`; new CLI `ifs-agent-ops har sanitize IN OUT` and `har analyze IN`.
- Recordings made with **IFS server tracing** on wrap each response as
  `{"request","response","server","ifs-trace"}`; the OData payload is the string `response.body`.
  The sequence view unwraps it. The legacy grouped view does not (its unprinted `response_sample`
  was always the envelope skeleton for such recordings) and is kept as-is for parity.
- Sanitizing keeps business data values and host names; review before sharing outside the organisation.
- Optional local parity check on real recordings:
  `IFS_LEGACY_SRC=<legacy src> IFS_REAL_HAR_DIR=<har folder> pytest tests/test_real_har_parity.py`
