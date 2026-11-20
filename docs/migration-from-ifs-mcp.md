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
