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
  form an implicit `default` profile (unchanged behavior for existing setups).
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
| Analysis | `analysis/odata.py` | projection URL + OData query parsing, `$filter` anonymization |
| Analysis | `analysis/grouping.py`, `summary.py` | legacy grouped view (`parse_har_file` output, unchanged) |
| Analysis | `analysis/sequence.py` | ordered view + data flow (keys, body values, ETag -> If-Match) |

- New MCP tool `analyze_recording`; new CLI `ifs-agent-ops har sanitize IN OUT` and `har analyze IN`.
- Recordings made with **IFS server tracing** on wrap each response as
  `{"request","response","server","ifs-trace"}`; the OData payload is the string `response.body`.
  The sequence view unwraps it. The legacy grouped view does not (its unprinted `response_sample`
  was always the envelope skeleton for such recordings) and is kept as-is for parity.
- Sanitizing keeps business data values and host names; review before sharing outside the organization.
- Optional local parity check on real recordings:
  `IFS_LEGACY_SRC=<legacy src> IFS_REAL_HAR_DIR=<har folder> pytest tests/test_real_har_parity.py`

## Phase 5: workspace, packages and the evidence boundary

- **Packages** (`workspace/package.py`): `package.yaml` (name, version, description,
  projections, ifs_release, `evidence.redact_values`) plus the layout `recordings/`, `metadata/`,
  `operations/`, `workflows/`, `agent/skills|instructions/`, `power-platform/connector/`,
  `dispatcher/`. `ifs-agent-ops package init|list`.
- **Discovery** (`workspace/discovery.py`): `IFS_AGENT_OPS_PACKAGES`. Skills are served from
  packages; `SKILLS_DIR` remains as a higher-precedence legacy override. With only `SKILLS_DIR`
  set the resource order and `get_api_guide` results are exactly the legacy ones (snapshotted).
- **Guidance** moved to `ifs_agent_ops/guidance/`; the bundled skills moved to the
  `ifs-customer-orders` and `ifs-shop-floor-clockings` package repos (copies kept as test fixtures).
- **Import boundary** (`ifs-agent-ops record import`): raw HAR -> `sanitize` -> host
  normalization (`ifs.example`) -> `redact-values` (package default; `--keep-values` opts out) ->
  verification -> `recordings/<workflow>/<task>.har` + `.annotations.yaml`. The import is refused
  if the call sequence or any data-flow link differs from the raw recording, or a real host remains.
  Raw files are never copied into a package.
- **Value redaction** (`recordings/redact.py`) keeps identity (same value -> same pseudo-value,
  never equal to any original), shape (digit/letter/punctuation; ISO date/time `T`/`Z`/offset kept)
  and JSON types. Structural values are kept: field names, enum members (`<field>@odata.type =
  #IfsApp...`, propagated to every occurrence), `luname`, `IfsApp...'Member'` literals, ETags.
  Key values inside `@odata.context`/`@odata.id`/`...Link`, URL paths and fragments (IFS web routes),
  `$expand` nested filters, URL-bearing headers and keyrefs are redacted consistently.
- Known limitation: short all-digit business values can coincide with HTTP sizes or trace
  timings (e.g. a 4-digit customer number equal to a `content-length`); these are not leaks.

## Phase 6: canonical operation model v0 (pilot: customer-order create)

- **Metadata:** `edmx.py` now parses actions, functions, action/function imports and complex
  types. `ifs-agent-ops metadata snapshot` pins `$metadata` + `$openapi?V2` into
  `metadata/<Projection>/`, with host and realm normalized (`ifs.example`, `example`) and a
  `snapshot.yaml` holding hashes. Drafting reads only pinned snapshots.
- **Model** (`operations/model.py`, pydantic, JSON Schema via `operations schema`):
  - `Operation` keeps the original IFS path template (`{Key}` placeholders). Path parameters are
    logical, with placements for repeated parent keys.
  - Body parameters carry IFS types and enums plus observed value *shapes*. `query.fields` holds
    the metadata types of referenced fields.
  - It also records headers (`If-Match` required for writes to existing entities), response
    shape, `depends_on` (from data flow), evidence and approval.
  - `Workflow` is the ordered steps of a recording, with bindings (`to` ← step/field).
- **Identity:** `id` = projection.kind.target (stable). `connector_operation_id` is assigned
  once and preserved. Re-drafting updates drafts in place (revision bump only on change), never
  rewrites approved operations, and reports their drift.
- **Validation** (`operations/validate.py`):
  - errors: enum `$filter` literal, string-typed flags, date-only types, unknown parameters,
    unresolved metadata, missing `If-Match`, duplicate connector ids, dangling dependencies
  - warnings: required-but-unobserved parameters, mixed datetime formats
  - Datetime format is evidence-based: the recordings show which format IFS *accepted* (customer
    orders accept UTC `Z`; shop floor clocking actions do not).
  - `operations approve` is refused while errors exist.
- **`$batch`:** `recordings/multipart.py` (lossless parse/render). Batch parts are calls of their
  own in the sequence view (`#58.2`), the import verification and drafting. The IFS web client
  creates order lines inside a `$batch` changeset, so without this the pilot's line create was
  invisible.
- **Phase 5 fix:** value redaction now processes multipart bodies structurally. It previously
  fell back to free-text substitution, which could rewrite protocol text (`HTTP/1.1`,
  `application/json`) when a business value matched it. The pilot recordings were re-imported.
- **Interfaces:** CLI `operations draft|list|show|validate|approve|schema`, `metadata snapshot`.
  MCP tools `draft_operations`, `list_operations`, `get_operation`.
- **Pilot result** (`ifs-customer-orders`): 52 operations (header, lines via `$batch`, charges,
  7 actions, 16 functions, 26 lookups/reads) and 2 workflows. A re-draft is a no-op. Validation:
  0 errors, 0 warnings.

## Phase 7: `doctor` v1 (capability-based readiness)

`ifs-agent-ops doctor [--for <capability>]... [--all] [--offline] [--json]` (also the read-only MCP
tool `doctor`) inspects the machine and reports PASS / WARN / FAIL / NOT REQUIRED per check, with a
concrete fix for every problem. It never installs, signs in or changes anything. Exit code 1 means a
requested capability has a FAIL.

| Capability | Default | Checks |
|---|---|---|
| `toolkit` | yes | Python ≥ 3.11, virtual environment, toolkit install, pinned dependencies (`mcp < 2`, pydantic 2, PyYAML, truststore, httpx), Git, writable toolkit home |
| `ifs` | yes | profile resolves and is complete, https, TLS setting, reachability with TLS verification (`--offline` skips it), sign-in state (refreshable sessions pass), OAuth callback port free |
| `packages` | yes | `IFS_AGENT_OPS_PACKAGES`, discovery problems, package `.gitignore` (`*.raw.har`, `.ifs-agent-ops/`), git repository, every HAR file is a sanitized `record import`, metadata snapshots for recorded projections, operation validation errors |
| `power-platform` | no | `pac` present and version, `pac auth` profile, `dotnet`; target checks arrive in Phase 10 |
| `agents` | no | Node (20.19+ or 22.12+), Agents Toolkit CLI (`atk`); sign-in and environment checks arrive in Phase 13 |
| `spfx` | no | Node presence; the SPFx toolchain itself is owned by consuming projects |

On a fresh clone (no `.env`, no packages) the result is one FAIL ("copy .env.example to .env and
set …") and one WARN (set `IFS_AGENT_OPS_PACKAGES` or `package init`), with exit code 1.

## Phase 7b: curated capabilities and the human approval gate

A **capability** (`<package>/capabilities/<id>.yaml`) is the intentional, safe business contract that
downstream agents and applications may use. It is a separate model from the observed `Workflow`
(evidence) and the canonical `Operation` (technical truth). It declares:

- organization `config` values (unset values block approval)
- agent-facing `inputs` with their source (`document`, `user`, `agent`), `resolve` lookups and `confirm` flags
- user `questions`, `actions` (e.g. `prepare` writes nothing; `submit` requires confirmation)
- `steps` over approved operations with a role (lookup, validation, defaults, calculation, write),
  `for_each`, `body_from` seeding (IFS `*_Default` records overlaid by validation results) and explicit
  `bindings`, each with exactly one source (input, item, step field, config, derived, fixed)
- meaningful `outputs`, `warnings` and `errors`, and `review_notes` (open questions for the reviewer)

| Command | Purpose |
|---|---|
| `capabilities list` / `validate` | status and findings (unknown or unapproved operations, type mismatches, unbound required or path parameters on writes, writes without confirmation, unreachable steps, user inputs without a question, unset config, changes since approval) |
| `capabilities review <id>` | writes a local Markdown review report to `<package>/.ifs-agent-ops/review/<id>.md`: reviewer decisions, actions, inputs, questions, config, a field-by-field table per write step showing who supplies each canonical field, operations and their approval, evidence |
| `capabilities approve <id>` | human-only (see below) |
| `capabilities schema` | JSON Schema |

**Approval is human-only.** `operations approve` and `capabilities approve` refuse unless stdin and
stdout are an interactive terminal, then require the one-time code shown on screen to be typed back
(the reviewer's name is taken from git `user.name`, or the OS login); EOF or an interrupt refuses. There is no MCP approval tool (a test enforces
it). Capability approval also refuses while any validation error exists, and records the reviewer,
time, a SHA-256 of the capability content and the revision of every operation it uses: editing the
capability or re-drafting a referenced operation afterwards invalidates the approval.
`Approval.approved_by` is now recorded for operation approvals too.

The first capability, `create-customer-order` in the `ifs-customer-orders` package, is a draft for
review. It validates with only human blockers: its 9 operations are not yet technically approved and
3 config values (site, order type, catalog source constant) are undecided.

### Technical operation review (Phase 7b follow-up)

One structured review result (`operations/operation_review.py`) feeds four presentations, none of
which computes facts of its own:

| Presentation | How to get it |
|---|---|
| Capability overview (terminal) | `operations review --capability <id> --package <p>` |
| One operation: summary, then deeper | `operations review <op-id> --package <p> [--details \| --full]` |
| Markdown | `<package>/.ifs-agent-ops/review/operations/<name>.md`, the concise review document, plus `appendix/<op-id>.md`, the exhaustive technical detail per operation |
| JSON | `--output json`, the structured result itself: deterministic, no ANSI, no files written |

**Findings.** Every finding has a level:

- `blocking`: validation error
- `open`: a recorded open question
- `check`: something the reviewer must confirm
- `info`
- `known`: classified web-client behavior, such as the Prefer wait header, excluded from the contract

Equivalent findings are grouped (for example, "24 decimal fields ... IEEE754Compatible") and keep their
field lists. The operation **state** is derived from the levels and approval data alone:

- BLOCKED: there is a blocking finding
- APPROVED: the approval is current and nothing is blocking
- UNRESOLVED: there is an open question
- CHECK: there are checks to confirm
- READY: otherwise

The terminal views use Rich. In an interactive terminal they use color for state only. When output is
redirected, they print ASCII, no color and a fixed width, so the output is deterministic. `NO_COLOR` is
honored.

`operations approve` shows the operation summary, a YOU ARE APPROVING / YOU ARE NOT APPROVING panel, and
any unresolved questions and checks before the unchanged human gate. Approval also pins the technical
content hash, so editing the definition afterwards raises `changed-since-approval`. `--output json` is
also available on `operations validate` and `capabilities validate`.

Operations can carry `review_notes` (open technical questions) and a `description` (verified facts). Both
are human text, kept on re-draft and excluded from the hash. The drafter now keeps IFS parameter aliases
(`Name=@Name`, with the value as JSON in the query string); previously it flattened them into inline
placeholders.

Capabilities gained `deferred` (scope deliberately left out of a revision) and `Output.description`.

### Approval is not runtime validation (Phase 7b boundary)

Operations and capabilities carry `runtime_validation`: a `status` (untested, conditional, validated or
failed) and the `requirements` that runtime validation must prove. Approval never sets the status. The
status is excluded from approval hashes, and runtime requirements never make an operation UNRESOLVED.
`operation.review_notes` now means *technical model questions* only. `capability.review_notes` means
*curation decisions*, and `capability.excluded_calls` classifies every observed web-client call the
capability leaves out as `excluded`, `deferred` or `validate-at-runtime`.

Executing capabilities against IFS (request rendering, step execution, composite records, response
headers) is Phase 11 work, recorded in roadmap §6.3. It was not built in Phase 7b.

### Approval ergonomics (Phase 7c follow-up)

- **Capability-scoped approval:** `operations approve --capability <id> --package <p>` runs one review
  and one human ceremony for every operation a capability references.
  - The screen shows the capability overview, the exact operation ids, revisions and content hashes, and
    the CHECK items being confirmed, plus what is and is not approved. Runtime requirements appear
    separately.
  - The ceremony needs an interactive terminal and the one-time code typed back; the reviewer name is
    taken from git `user.name` (or the OS login).
  - It is refused if any referenced operation has validation errors or open technical model questions,
    or if an operation changes between the review and the confirmation.
  - Each operation records its own approval (`approved_at`, `approved_by`, `revision`,
    `content_sha256`). `runtime_validation` is untouched. `operations approve <id>` remains for
    individual review.
- **Re-approval:** approval checks validate the definition as a draft. An operation whose approval
  became stale (`changed-since-approval`) can be reviewed and approved again.
- **Small approval diffs:** operation and capability approval now replace only the top-level `status`
  and `approval` entries in the YAML file (`operations/yaml_persist.py`). The rest of a hand-authored
  file (comments, flow mappings, folded text) is untouched. The patched text is re-parsed and compared
  with the intended model before it is written; if they differ, the full serializer is used instead.
  Content hashes are computed on the parsed model, so formatting never changes identity.

## Phase 8: skill generator

`ifs-agent-ops skills generate [<capability>] --package <p> [--check] [--output json]` writes
`<package>/agent/skills/<capability>.md` from an **approved** capability whose approval is current. Drafts
and stale approvals are refused. Generation is deterministic and offline (`generators/skills.py`): no
timestamps, no LLM and no IFS calls. `--check` writes nothing and exits 1 when a skill is missing or
stale, for CI.

The skill is built from the capability. It reads operations only for technical detail and never lists
their full field surface.

For the agent:
- what to collect, and where each input comes from
- organization settings, which are never asked for
- values never supplied (defaults, terms, prices, totals and cost come from IFS)
- the questions to ask
- the `prepare` / confirmation / `submit` flow
- results, warnings and errors
- out-of-scope items

For implementers and reviewers:
- the curated steps per action, with operation revisions and calls
- the values each step sends
- request rules established from evidence: headers, date formats, aliases, enum literals, unquoted nulls
- runtime-validation status and requirements (*not deployment-validated* while untested)
- provenance: capability revision and hash, approval, operation revisions, evidence

Generated skills carry a marker. `skills generate` never overwrites a hand-written file, and the MCP
`save_skill` tool refuses to overwrite a generated one. Generated skills are served like any package
skill (`ifs://<capability>/guide`).

**Intentional difference (re-baselined):** the authoring guide's universal "no `Z` on write datetimes"
rule contradicted the customer-order evidence. It now says the format is operation-specific, with the
recorded examples for shop floor clocking (no suffix) and customer order (`Z`, with the date
preserved). Only the `get_api_guide_skill-authoring` snapshot changed.

## Phase 9: connector generator

`ifs-agent-ops connector generate --package <p> [--check] [--max-bytes N] [--output json]` writes
`<package>/power-platform/connector/` from the operations referenced by **approved** capabilities
(`generators/connector/`). Each capability and operation must have a current approval; draft
capabilities are left out and listed. Generation is deterministic and offline. It reads only the
pinned `$openapi?V2` and `$metadata`, and refuses when they don't match their `snapshot.yaml` hashes.
`--check` writes nothing and exits 1 when a file is missing or stale.

| File | Content |
|---|---|
| `apiDefinition.swagger.json` | Swagger 2.0, compact UTF-8; the bytes measured are the bytes written |
| `apiProperties.json` | environment-independent properties only |
| `operation-ids.yaml` | the stable operationId map; an id never changes, and a retired id is never reused |
| `report.md` | selection, how values reach IFS, transforms, size report, deployment items, provenance |

**Pipeline** (plan §5a):
- select and match to the spec: `exact`, `normalized` (key values ignored) or `bound-equivalent`
  (the spec declares the same bound function on the same entity type through another route);
  anything else refuses generation
- compatibility transforms: C1 unique placeholders, C3 arrays become strings, C4 description from
  the manifest, C5 operationId length
- annotations from metadata: C8 enum `$filter` syntax, C9 field lists on GETs, specification
  links removed
- pruning: C7 `$ref` closure; a dangling reference is an error
- validation: C2 placeholders, C5 uniqueness, C7, C10 size against a 1 MB default, with the
  largest paths and definitions on failure

**Decisions to review:**
- **Canonical path, spec operation object.** The connector path is the canonical (recorded) path.
  The spec supplies the body schema, query options and responses. Path and header parameters are
  rebuilt from the canonical operation, so types and repeated keys come from the model.
- **Function parameters are OData literals.** The recordings send `OrderNo=null` to
  `CheckCustPoExist` and `GetCustomerDefaults`, which a quoted placeholder (`OrderNo='{OrderNo}'`)
  cannot express. Function-call parameters are therefore placed unquoted, and the caller sends
  `'text'`, `null`, `Ns.Enum'Member'`, a number or a date-time. Entity keys keep their quotes.
  Parameter aliases stay in the path (`P=@P`), with an `@P` JSON query parameter.
- **`bound-equivalent` match.** The recorded route
  `CustomerOrderSet(OrderNo=...)/OrderLinesArray/...CustomerOrderLine_Default()` is not in the IFS
  spec, which declares the function only on `/CustomerOrderLineSet`. The generator keeps the
  recorded route and reuses that operation object. The report names the spec route.
- **Selection.** Only operations that approved capabilities reference are included. Operations that
  appear only in `depends_on` links or observed workflows are left out.
- **Not generated (deployment state, Phase 10).**
  - The host stays the `ifs.example` placeholder.
  - Connection authentication (roadmap §7 assumption 5) and the on-premises data gateway
    (assumption 2) are not generated: there are no `securityDefinitions`, and `connectionParameters`
    is empty.
- **Not deployment-validated** until Phase 11. To confirm on import:
  - `@` alias query parameter names
  - literal path parameters

Approval gates are shared with the skill generator (`generators/gating.py`).

## Phase 9a: guided capture (intent, recording plan, task-bucketed browser capture)

The roadmap now starts capability development with the user's intent and a planned recording
(roadmap §1.1). Two new phases follow:
- **9a (this change):** guided capture.
- **9b:** the development loop. It covers local Claude/MCP testing against TEST and local validation
  before approval, through the direct-to-IFS mode moved from Phase 11.

Phase 11 is now deployed-path validation only. Phase 10 (deployment) waits for 9b. Importing a
DevTools HAR (`record import`) stays available for existing evidence.

**Model** (`capture/session.py`):
- A session holds an intent, an ordered plan of tasks, and the ordered network events.
- Task status is one of pending, active, complete, skipped or incomplete. A task can also be marked
  as added during recording.
- While recording, exactly one task is active. Completing or skipping it makes the next pending task
  active.
- Each task has an interval `[started_at, ended_at)`. A request belongs to the task whose interval
  contains its send time.
- Stopping keeps everything. A task still active at stop becomes `incomplete`.
- A plan is *complete* when every task is complete or skipped.

**Storage:**
- Sessions are local, in `<toolkit home>/captures/<session>/`: `session.yaml`, `events.jsonl`,
  `requests.idx` and, on import, `capture.raw.har`.
- Changes run under a lock file, so the MCP server, `capture serve` and the CLI can share a session.

**Surfaces** (one service: `capture/service.py`):

| Surface | Entry points |
|---|---|
| CLI | `ifs-agent-ops capture new / list / show / plan / complete / skip / add / stop / import / serve / extension` |
| MCP | `capture_create_session`, `capture_set_plan`, `capture_status`, `capture_list_sessions`, `capture_complete_task`, `capture_skip_task`, `capture_add_task`, `capture_stop`, `capture_import` (tool inventory updated deliberately) |
| Local API (for the extension) | `/capture/v1/...` on the MCP server's port, or `capture serve`; extension origins only, so web pages are refused |

**Browser extension** (`browser-extension/`):
- Manifest V3, for Edge or Chrome.
- The **Rust/WASM core** (`core/`, crate `ifs-capture-core`) turns `chrome.debugger` Network events
  into ordered capture events:
  - IFS origin, XHR and fetch only
  - credential headers removed before anything leaves the browser
  - CDP monotonic times converted to wall-clock
  - post data and bodies fetched on demand, redirects kept
  - size-bounded batches
- The **side panel** (thin JS) is the recording UI:
  - define or edit the intent
  - create, edit, reorder and remove tasks
  - start recording
  - show the current task and progress
  - complete or skip the task, add a task while recording
  - stop
  - show whether the plan was complete or incomplete
- Events and commands go through one ordered queue. A task boundary is the moment of the click.
- The panel polls the session, so changes made in Claude or the CLI appear in it.

**Into the package:**
- `capture import` exports the stopped session as a local raw HAR and runs the existing
  `record import` (sanitize, host normalization, value redaction, verification).
- The recording annotations gain a `plan` block: intent, completeness, tasks with status and
  0-based entry ranges, and unassigned ranges. Business values the redactor replaced are also
  replaced in the plan text.
- `analyze_recording` / `har analyze` show the plan and group the sequence by task.
- `draft_operations` writes `Workflow.plan` and `Step.task`.

**Tests:**
- `tests/test_capture.py` (10 tests):
  - plan rules
  - attribution
  - ingest rules
  - export ranges
  - import, analysis and drafting
  - API (including the origin refusal)
  - CLI
  - MCP
- `cargo test` in `browser-extension/core` (5 tests).
- `browser-extension/tests/wasm_smoke.mjs` (the built bindings; run by `build.ps1`).
- `browser-extension/tests/e2e_edge.mjs`: the real extension in headless Edge records a planned
  demonstration against a stand-in IFS (`fake_ifs.py`) and checks the per-task attribution.
