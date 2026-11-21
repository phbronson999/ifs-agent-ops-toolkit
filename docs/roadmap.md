# IFS Agent Ops Toolkit — Architecture and Roadmap

This is the source of truth for the lifecycle architecture and the phased migration from
`ifs-mcp-server-local-python`. `docs/migration-from-ifs-mcp.md` records what each completed
phase changed; this document says where the project is going and in what order.

Status at time of writing: Phases 0–8 merged. The `create-customer-order` capability (revision 4) and its 12 operations are approved by a human; runtime validation is untested. Phase 9 (connector generator) merged. Phase 9a (guided capture: intent, recording plan and task-bucketed browser capture) is in review; it makes browser capture the normal start of capability development (§1.1). Deployment (Phase 10 onwards) waits for the development loop (Phase 9b). The decisions recorded on 2026-11-20 are folded
in: the customer-order pilot, permanent package `name`, the reserved `.ifs-agent-ops/` folder,
and the proven agent path as the Phase 13 baseline.

---

## 1. Lifecycle

The goal is not only to generate files, but to create, provision, validate, update, promote and
reproduce IFS capabilities for agents without depending on remembered portal steps. The proven
runtime path (Microsoft 365 → Power Platform → on-premises data gateway → IFS Cloud) is not
being redesigned; the toolkit makes building and operating it reproducible.

```text
IFS workflow evidence (recordings, $metadata, $openapi)
  -> analysis / enrichment                       toolkit
  -> canonical operations + workflows            package (source of truth)
  -> human approval                              package (status: approved)
  -> generated source artifacts                  generators -> package
  -> environment-specific deployment             deployment adapters -> platforms
  -> validation                                  validation -> target
  -> promotion through environments              ALM (solutions, pipelines)
  -> runtime capability                          consumed by experiences
```

### 1.1 Development workflow (Skill Builder)

Capability development **starts with the user's intent and a planned browser recording**. Importing a
DevTools HAR (`record import`) remains available for evidence that already exists. It is not the normal
path.

```text
intent                         "I want to teach the builder how to create a customer order."
-> recording plan              ordered tasks agreed before capture: [ ] Create order header [ ] Add order line ...
-> task-bucketed capture       browser extension + toolkit capture session; one current task at a time
-> analysis / enrichment       recorded order, data flow, grouped by plan task
-> draft capability + skill    curated from task-bucketed evidence
-> local Claude/MCP testing    against IFS TEST
-> another planned recording   whenever more evidence is needed (a new plan; same package)
-> refine and repeat
-> local validation            the capability's actions executed directly against TEST
-> human approval              operations (technical) and capability (business)
-> deployment artifacts        connector, dispatcher, agent (Phases 9, 12, 13)
-> deployed-path validation    through the deployed connector / flow / agent (Phase 11)
```

**The recording plan is semantic evidence from the human demonstrator, not UI state.** The plan
travels with the recording, from the capture session to the sanitized package recording. It is
recorded in the recording's annotations, in analysis (calls grouped by task) and in the observed
workflows the drafter writes (steps carry their task), so capability curation knows which calls
belong to which business action.

```text
session -> intent
        -> ordered task plan
            -> task -> status (pending | active | complete | skipped), added during recording or not
                    -> ordered network events
```

Recording rules:
- exactly one task is current while recording
- the user performs that task normally in the IFS web UI
- events stay in recorded order
- completing the task closes its interval, and the next task becomes current
- tasks can be skipped, or added during recording
- stopping keeps the session, the task boundaries and every event
- a plan is **complete** when every task is complete or skipped, and **incomplete** otherwise; an
  incomplete plan can still be imported, and is marked as such

Capture architecture:
- **The toolkit is the authority.** Sessions, plans, boundaries and raw events live locally in
  toolkit home (`~/.ifs-agent-ops/captures/<session>/`), never in a repository.
- **The browser extension is a capture and control client.** It is a Manifest V3 extension for Edge or
  Chrome, with a Rust/WASM core and a thin JS layer. It records the IFS tab through `chrome.debugger`
  (in-order requests and responses, with bodies). It strips credential headers before anything
  leaves the browser, and streams events to the toolkit's local capture API. Its side panel is the
  recording UI.
- **Capture surfaces:**
  - the CLI (`ifs-agent-ops capture ...`)
  - MCP tools, so Claude can turn an intent into a plan and track progress
  - the extension's side panel
- **Into the package:** `capture import` hands the stopped session to the existing
  `record import` boundary (sanitize, normalize host, redact values, verify). The plan goes with it.

## 2. Layers and boundaries

| # | Layer | Owns | Must not own |
|---|---|---|---|
| 1 | **Development toolchain** | Git, Python, Node, PAC, Agents Toolkit, VS Code, Claude Code, PowerShell; checked by `doctor`, installed by `bootstrap` | anything tenant- or environment-specific |
| 2 | **Toolkit** (`ifs-agent-ops-toolkit`) | capture, sanitize, analyze, enrich, model, validate, **generate**, and the deployment **adapters** (code) | any customer's operations, recordings or environment values |
| 3 | **Capability package** (`ifs-<capability>` repo) | evidence, metadata snapshots, canonical operations/workflows, generated source artifacts, **deployment intent** (logical names) | secrets, tokens, connection IDs, environment URLs, resolved resource IDs |
| 4 | **Deployment / ALM** | targets (environment configuration), authentication, reconciliation of package intent with platforms, validation against a target, promotion | the canonical IFS model; IFS business/API logic |
| 5 | **Experiences** | Copilot/Teams, SPFx/React (e.g. the customer-order review UI), future consumers | IFS business/API logic (they consume capabilities) |

Responsibility boundaries inside the lifecycle:

| Concern | Input | Output | Owner | Notes |
|---|---|---|---|---|
| **Generation** | approved operations/workflows + pinned metadata | source artifacts in the package (connector `apiDefinition`/`apiProperties`, dispatcher flow definition, agent manifest/plugin, skills) | generators (toolkit) | deterministic, offline, no credentials |
| **Environment configuration** | target name | environment URL, solution managed/unmanaged, non-secret variable values, secret *references* | targets file in toolkit home (or CI variables) | never in the package |
| **Authentication** | target | `pac`/`atk`/IFS sessions | the respective CLIs; IFS profiles | interactive locally, service principal in CI |
| **Deployment** | package intent + source artifacts + target | reconciled platform resources | deployment adapters | idempotent: plan → apply → status |
| **Validation** | target | pass/fail evidence | validation commands | static (artifacts), platform (solution check), runtime (call through the deployed path) |
| **Promotion** | a validated solution version | managed solution in the next environment | Power Platform Pipelines / `pac` | release version recorded per target |

Generation and deployment are separate so Microsoft tooling can change without touching the
canonical model:

```text
operations/workflows -> connector generator  -> power-platform/connector/*   -> PP adapter -> deployed connector
operations/workflows -> dispatcher generator -> power-platform/flows/*       -> PP adapter -> deployed flow
operations/workflows -> agent generator      -> agent/<agent>/*              -> agent adapter -> deployed M365 agent
```

## 3. Deployment model (desired state)

### 3.1 Package: deployment intent (environment-independent)

A future `deploy.yaml` in the package declares *what should exist*, by logical name only:

```yaml
power_platform:
  solution:
    unique_name: IfsCustomerOrders        # stable; derived from the package name once, then fixed
    publisher_prefix: ifsops
  connectors:
    - logical_name: ifs-customer-orders   # maps to power-platform/connector/
      operation_ids: operation-ids.yaml   # stable operationId map (see 3.4)
  flows:
    - logical_name: dispatch-customer-order
      source: power-platform/flows/dispatch-customer-order.json
  connection_references:
    - logical_name: ifsops_IfsCustomerOrders   # connector -> resolved per target
      connector: ifs-customer-orders
  environment_variables:
    - schema_name: ifsops_IfsBaseUrl
      type: string
      description: IFS projection base URL as seen through the gateway
agents:
  - logical_name: customer-order-assistant
    source: agent/customer-order-assistant/   # Agents Toolkit / WIQD project
    actions:
      - api_plugin: dispatch-customer-order   # OpenAPI action -> the flow's authenticated HTTP trigger
        endpoint_from: flows.dispatch-customer-order.trigger_url   # resolved per target at deploy
        auth: oauth-plugin-vault              # registration reference held in the target, not here
```

### 3.2 Toolkit home: targets (environment-specific, never committed to a package)

```text
~/.ifs-agent-ops/targets/<package>/<target>.yaml   (or CI variables)
  power_platform.environment_url, managed: true|false
  environment variable values (non-secret)
  connection reference -> connection id (resolved or created by a human once)
  secret references (Key Vault name / "prompt"), never secret values
  agent environment name (Agents Toolkit .env.<target>)
  OAuthPluginVault registration reference and Entra app (client) id for the agent plugin
  (resolved deployment state: the flow HTTP trigger URL the agent plugin points at)
```

Deployment state (resolved resource IDs, last deployed versions) is **read back from the
platform** on every `status`/`deploy` (e.g. `pac solution list`, `pac solution online-version`,
`pac connector list`), optionally cached locally. Caches live under `~/.ifs-agent-ops/state/`
or in the package's reserved **`.ifs-agent-ops/`** folder, which every package `.gitignore`
excludes and which never becomes canonical source. The platform is authoritative, never the cache.

### 3.3 Commands (conceptual; not implemented)

| Command | Changes anything? | Responsibility |
|---|---|---|
| `doctor [--for <capability>]` | no | inspect toolchain, config, auth, packages; PASS/WARN/FAIL/NOT REQUIRED |
| `bootstrap [--for <capability>]` | local only, explicit | install/configure deterministic local prerequisites, each step confirmed |
| `generate --package` | package files only | run generators for approved operations |
| `validate --package [--target]` | no | static artifact checks; with `--target`, platform and runtime checks |
| `plan --package --target` | no | compute the diff between intent and deployed state |
| `deploy --package --target` | external, explicit | reconcile (create or update, never duplicate), then report |
| `status --package --target` | no | what is deployed, which versions, drift from source |
| `promote --package --from --to` | external, explicit | move a validated managed solution version (Pipelines/`pac pipeline deploy`) |

`doctor` inspects, `bootstrap` changes only the local machine on request, and
`deploy`/`promote` change external systems on request. None of them silently installs software,
authenticates as the user, creates tenant resources or credentials, or touches IAM, gateways or
production.

### 3.4 Identity, idempotency and versioning

- **Stable logical identities**, recorded in the package and never regenerated:
  package `name` (**permanent** once the package is established; the renameable human-facing
  title is `display_name`); solution unique name; connector logical name; connector **operationId map**
  (operation → operationId, so regeneration and the 300-char shortening never change published
  IDs); flow logical names; agent logical name; environment variable schema names;
  connection reference logical names.
- **Idempotency:** adapters look resources up by logical identity (solution unique name,
  connector name in the solution, flow name, schema name) and update in place; `plan` shows
  create/update/no-op before `deploy`.
- **Versioning ownership:**

| Level | Version | Owned by |
|---|---|---|
| Capability package | `package.yaml` `version` (semver) | humans, on approved model changes |
| Canonical operation | revision + approval status | package |
| Generated artifacts | derived (package version + generator version), recorded in artifact headers | generators |
| Power Platform solution | `major.minor.build.revision` | deployment adapter, stamped from the package version; build/revision per deploy |
| Agent app package | manifest `version` | agent adapter, from the package version |
| Deployment release | target + solution version + agent version + timestamp | deployment log/state |

  Rollback = redeploy (or promote) the previous managed solution version and agent package
  version; source rollback = revert the package commit and regenerate.

## 4. Current Microsoft tooling (verified Nov 2026; recheck before implementing)

Power Platform CLI (`pac`):

| Operation | Mechanism | Automation |
|---|---|---|
| Auth profiles | `pac auth create` (interactive user, or service principal) | interactive locally; full in CI with a service principal |
| Environment discovery/selection | `pac env list` / `pac env select` | full |
| Solution project / source control | `pac solution init`, `clone`, `sync`, `pack`, `unpack` | full |
| Native Git binding | Dataverse Git integration (Azure DevOps; **managed environments**; system admin to bind) | setup is portal/admin; afterwards automatic |
| Custom connector create/update **in a solution** | `pac connector create|update --solution-unique-name` with `apiDefinition`/`apiProperties` | full |
| Connector OAuth client secret | blanked on export; set in the UI or via environment variables (Key Vault recommended) | manual or partial |
| Connector import order | connector must exist before flows/connection references import (separate solution or import first) | adapter must sequence it |
| Flow create/update | solution import of the unpacked flow definition, or the Dataverse Web API `workflow` table (`clientdata`) | full via solution import; flows arrive **off**, so turning them on is a separate step |
| Connection references and environment variable values | `pac solution create-settings` → deployment settings file → import with settings | full once connections exist |
| Connections (OAuth consent, gateway selection) | maker portal; `pac connection create` covers only service-principal Dataverse connections | **manual**, once per target |
| Solution checker | `pac solution check` | full |
| Versioning | `pac solution version` (local), `pac solution online-version` (deployed) | full |
| Export/import (managed or unmanaged) | `pac solution export` / `import` | full |
| Promotion | Power Platform Pipelines; `pac pipeline list|deploy` (targets must be **managed environments**; managed solutions only) | pipeline setup is portal/admin; runs are automatable |
| Deployed state / drift | `pac solution list`, `online-version`, `pac connector list`; `clone`/`sync` + diff against package | full |
| Install | `dotnet tool install --global Microsoft.PowerApps.CLI.Tool` (.NET 10), MSI, or VS Code extension; some commands are Windows-MSI only | full |

Microsoft 365 agents:

| Operation | Mechanism | Automation |
|---|---|---|
| Source structure | Agents Toolkit project: `appPackage/` (manifest, declarative agent, plugin, OpenAPI), `m365agents.yml`, `env/.env.<target>` | — |
| Provision / deploy / package / validate / publish | `atk provision|deploy|package|validate|publish --env <target>` (`npm i -g @microsoft/m365agentstoolkit-cli`, Node 20.19+/22.12+) | interactive sign-in locally; CI templates exist |
| Multi-environment | one `.env.<target>` per target, `${{VAR}}` placeholders in manifests | full |
| OAuth for API plugins | `oauth/register` during provision (prompts for client ID/secret) or Teams Developer Portal | partial; secrets via environment variables in CI |
| Org-wide publish | `atk publish` submits; **admin approval** in the Microsoft 365 admin center | manual approval |
| Work IQ Dev Tools (WIQD) | create/validate/provision/package/publish/monitor/evaluate declarative agents; **preview** (Node 24+) | evaluate; don't build on it until 1.0 |

Still manual or portal-dependent with current tooling:
- creating connections (OAuth consent, on-premises data gateway selection)
- entering the connector client secret, unless it's sourced from Key Vault
- one-time Pipelines host setup and managed-environment enablement
- Dataverse Git binding
- IFS IAM client registrations and redirect URIs
- gateway installation and configuration
- tenant admin approval of agent publishing
- custom app upload policy

## 5. Developer toolchain and bootstrap

Readiness is **capability-based**: a machine that only records and analyses IFS workflows
doesn't fail because SPFx tooling is absent.

| Capability | Required | Checked by `doctor` |
|---|---|---|
| `toolkit` | Python ≥ 3.11, venv, Git | Python version, venv, package install, toolkit home writable |
| `ifs` | toolkit + an IFS profile | profiles valid, IFS session state, TLS trust to the IFS host |
| `packages` | toolkit | `IFS_AGENT_OPS_PACKAGES` resolves, manifests valid, raw HARs not tracked |
| `power-platform` | PAC (version floor), .NET runtime when using the .NET tool | `pac` present and version, auth profile present, target environment reachable |
| `agents` | Node (20.19+/22.12+), Agents Toolkit CLI | `atk` present and version, sign-in state, target `.env` present |
| `spfx` | Node per SPFx version, SPFx toolchain | only when `--for spfx`; consumer projects own this |
| optional | VS Code, Claude Code, PowerShell 7, WIQD | reported as info |

Separate problems, separate owners:
1. software installation (`bootstrap`, explicit and per step)
2. local toolkit configuration (toolkit home, profiles, targets)
3. authentication (each CLI's own sign-in)
4. tenant/environment provisioning (`deploy`)
5. secrets (Key Vault or CI secret stores, never files in repos)
6. organization prerequisites: IFS IAM clients, gateway, managed environments, Pipelines host (documented checklists that `doctor` verifies but never performs)

Results: PASS, WARN, FAIL, NOT REQUIRED (for the capability asked about).

## 6. Phased roadmap

| # | Phase | Scope | Exit check |
|---|---|---|---|
| 0–4 | done | baseline, parity tests, client decoupling, profiles, recordings/analysis | merged |
| 5 | **Workspace & packages** (merged) | packages, discovery, redacting import boundary | parity + import verification |
| 6 | **Operation model v0** (merged) | pydantic model and JSON Schema; drafts from recordings plus metadata; IFS rule validators; approval status. **Pilot: customer-order create** (decided; the primary end-to-end proving scenario, and its real recordings are already imported); shop floor clockings becomes the second package. Adds stable logical IDs (operation id, `operationId` reservation) | drafts cover the recorded create-order flow; ids stable across re-drafts |
| 7 | **Toolchain `doctor` v1** (small, independent) | capability-based checks for `toolkit`, `ifs` and `packages`; PP/agent checks report NOT REQUIRED until their phases | doctor output on a fresh clone is actionable |
| 7b | **Capability curation: customer order** (human review gate before any generator; no runtime execution) | a new `Capability` model (separate from the observed `Workflow`), schema and validator ([operation-model.md](operation-model.md) §3); `create-customer-order` capability curated from the observed workflows: agent-facing inputs (document/user/agent), lookups and validation, defaults/derived/fixed values, user questions, `prepare` and `submit` actions, confirmation, meaningful outputs/warnings/errors; the canonical operations it uses reviewed and approved; observed calls not included are classified; runtime-validation requirements recorded; the capability itself **approved by a human** | capability validates (every required canonical parameter of each write is bound; every referenced operation is approved); human approval recorded; runtime status stays `untested` |
| 8 | **Skill generator** | from **approved capabilities** (curated workflow, agent-visible inputs, questions, confirmation), with canonical operations for technical detail; a capability may still carry runtime-validation requirements, and generated artifacts are then marked *not deployment-validated*; writes into the package; replaces legacy `save_skill` defaults | generated skill describes the curated workflow, not the full IFS field surface; datetime contradiction gone |
| 9 | **Connector generator** (merged) | as planned (§5a of the plan: select → prune → compatibility transforms → annotate → validate), selecting **only operations referenced by approved capabilities**; emits `power-platform/connector/*` and the **stable operationId map** | all checks pass; size report |
| 9a | **Guided capture: intent, recording plan, task-bucketed browser capture** (§1.1) | capture session and recording-plan model (toolkit home); local capture API (served by the MCP server or `capture serve`); CLI and MCP surfaces; MV3 browser extension (Rust/WASM core, `chrome.debugger`, side panel); `capture import` through the existing `record import` boundary with the plan in the annotations; analysis and observed workflows grouped by task | an intent -> plan -> recording -> import run produces a package recording whose analysis and drafted workflow show the plan's tasks and boundaries |
| 9b | **Development loop** (before any deployment) | task-aware drafting of a capability and skill from planned recordings; local Claude/MCP testing against TEST; repeated planned recordings into the same package; **local validation**: the direct-to-IFS execution of curated capability actions (design notes §6.3, moved here from Phase 11), recording `runtime_validation` before human approval | a new capability goes from intent to locally validated and human-approved without hand-written skills or DevTools exports |
| 10 | **Deployment foundations + Power Platform adapter v1** (after 9b) | `deploy.yaml` schema; targets in toolkit home; adapter interface (`plan`/`apply`/`status`); PP adapter for **solution + connector + environment variables + connection-reference placeholders** via `pac`; deployment settings file generation; manual-step checklist; `doctor --for power-platform` | deploy is idempotent to a DEV target (second run is a no-op); status shows drift |
| 11 | **Deployed-path validation** | target-based `validate`: static, `pac solution check`, runtime calls through the deployed connector/flow (read-only by default). The direct-to-IFS mode moved to Phase 9b as local validation | green on DEV; capability and operation `runtime_validation` status recorded from runs |
| 12 | **Dispatcher generator + flow deployment** | dispatcher definition from **capability actions** (their inputs, steps, bindings, outputs), not a mirror of connector parameters; deployed as a solution component; turned on after import | flow runs on DEV through the gateway |
| 13 | **Agent artifacts + agent adapter** | Baseline is the **proven path** (§6.1): a declarative agent with an API-plugin/OpenAPI action whose endpoint is the authenticated Power Automate HTTP trigger of the Phase 12 flow, using OAuthPluginVault with Entra delegated auth. Generated into `agent/<name>/` (Agents Toolkit layout); `atk` adapter with `--env <target>`; the plugin's server URL resolved per target from the deployed flow's trigger URL; `doctor --for agents` | agent provisioned to DEV reaches IFS through the generated capability, with the same request/response as the proven hand-built agent |
| 14 | **Promotion and CI readiness** | only artifacts whose capability is runtime `validated` (or `conditional`, within its recorded scope) may be promoted; managed solution versions, `promote` via Pipelines/`pac pipeline deploy`, agent version promotion, service-principal auth paths, release records | DEV → TEST promotion reproducible from CLI |

### 6.2 Approval is not runtime validation

Three states are tracked separately and never imply each other:

| State | Question | Set by | Recorded in |
|---|---|---|---|
| Technical operation approval | Is this a technically correct canonical representation of the observed IFS operation and its metadata contract? | a human (`operations approve`) | `operation.approval` |
| Capability approval | Is this the intentional and safe business contract downstream agents/applications may use? | a human (`capabilities approve`) | `capability.approval` |
| Runtime validation | Has an implementation of it been executed against IFS (or a deployed target) successfully? | runtime validation runs (Phase 11) | `runtime_validation.status`: untested / conditional / validated / failed, plus `requirements` |

Lifecycle with the existing phases:

```text
intent -> recording plan -> task-bucketed capture (9a) -> evidence (5)
       -> canonical operations (6) + curated capability (7b) + skill (8)    drafts
       -> local Claude/MCP testing; more planned recordings as needed     (9b loop)
       -> local validation against TEST (9b)       sets runtime_validation from runs
       -> technical approval + capability approval (human)
       -> deployment artifacts (9, 12, 13)         draft, not deployment-validated
       -> deployed-path validation (10, 11) -> promotion (14)
```

The `create-customer-order` pilot was built before this workflow existed (DevTools HAR imports,
approval before local validation). New capabilities follow the sequence above. Local validation now
comes before approval, so a reviewer sees runtime evidence. It still never grants or revokes an
approval.

Rules:
- Approval never sets runtime status, and runtime status is excluded from approval hashes, so a
  validation result can neither grant nor invalidate an approval.
- Generator development may consume an approved capability that still carries runtime-validation
  requirements. Its outputs are drafts that must not be promoted or deployed as production-ready
  until runtime validation succeeds.
- Uncertainty that belongs to runtime validation is recorded as a requirement. It doesn't block
  technical approval, and it doesn't justify inventing an alternative call sequence ahead of evidence.
**Approval direction:**

- **Capability approval is the primary human business gate.** It is the decision that a curated
  contract may be used downstream.
- **Capability-scoped operation approval is the normal technical workflow** when a group of operations
  was reviewed together: `operations approve --capability <id>` runs one human ceremony. Each operation
  still records its own approval, so a later change invalidates only that operation.
- **Single-operation approval remains available** (`operations approve <id>`) for exceptions and manual
  review.
- **Operation review keeps moving toward exception-driven attention** instead of repetitive ceremony.
  Clean operations reviewed as part of a capability need no individual attention. Attention goes to:
  - validation errors
  - technical-model ambiguity
  - conflicting evidence
  - unusual or destructive behavior (e.g. deletes, state transitions)
  - incomplete metadata or evidence
  - other explicit review findings

  This is a direction, not yet implemented. There is no automatic approval.
- **Approval and runtime validation stay separate.** Runtime validation never grants or revokes approval.
  It focuses later (Phase 11) on actual execution: requests sent, errors, warnings and results against
  a target.

### 6.3 Phase 11 design notes (learned in Phase 7b)

The toolkit can't execute a canonical operation today. `IfsClient.request` and the MCP
`call_protected_api` send a hand-built endpoint, body and headers, and discard response headers.
For the direct-to-IFS mode, Phase 11 will likely need the following.

Rendering a canonical operation into a request:
- path keys: quoting by declared type, unquoted `null`, enum literals `Type'Member'`
- parameter aliases (`Name=@Name` with the JSON value in the query string)
- the IEEE754Compatible content type

Executing a capability action step by step:
- source resolution: input, item, config, fixed and earlier-step fields
- `body_from` seeding and `omit`
- `for_each`
- executable composite records: a structured source kind, replacing descriptive `derived` text

Responses:
- headers preserved: ETag, Location, OData-EntityID, `X-IFS-*` warnings
- response-field extraction, including bare-value responses

Runs:
- dry-run planning that shows every request, compared with the recorded requests
- controlled execution, with writes enabled explicitly per run
- target/environment awareness
- run evidence stored locally (`.ifs-agent-ops/runs/`)
- recording the outcome into `runtime_validation`

This was proposed during Phase 7b and deliberately deferred to Phase 11.

**Proving scenario: customer-order PDF → conversational validation → confirmation → IFS
submission.** It runs on Phase 6's customer-order pilot, the Phase 7b curated
`create-customer-order` capability, Phase 8 (skill), Phase 9 (connector),
Phases 10–11 (deploy and validate), Phase 12 (dispatcher) and Phase 13 (agent). It is the first
end-to-end acceptance test, at the end of Phase 13. The SPFx/React experience consumes the
deployed agent/capability and stays a separate project that this roadmap doesn't build. Its
needs that feed back into the model are: a `prepare` action (lookups and validation only, which
returns a proposal and warnings), `submit` with explicit confirmation, user questions for missing
required inputs, and warning surfacing (`X-IFS-Accept-Warnings`).

**Observed vs. curated.** Observed workflows (what the IFS web client did) and canonical operations
(what IFS accepts) are evidence and technical truth. The curated capability (what the agent should
do and may supply) is what generators consume. A writable IFS field doesn't become agent-visible
just because metadata declares it. See [operation-model.md](operation-model.md).

### 6.1 Proven runtime path (baseline for Phases 12–13)

```text
Declarative agent
  -> API plugin / OpenAPI action         (auth: OAuthPluginVault, Entra delegated)
  -> authenticated Power Automate HTTP trigger (accepts the user's Entra token)
  -> Power Automate flow                 (the dispatcher, Phase 12)
  -> IFS custom connector                (Phase 9 artifacts, Phase 10 deployment)
  -> on-premises data gateway
  -> IFS Cloud
```

The flow is **not** added as a native declarative-agent action. Microsoft's known issue about
"Power Automate flows as actions in declarative agents" concerns that native mechanism, not an
API plugin calling an HTTPS endpoint. This path was proven end to end, and the generators
reproduce it rather than replace it. Re-check current documentation at Phase 13, and change course
only if a limitation is shown to apply to this API-plugin → authenticated HTTP trigger pattern.

What this means for the deployment model:
- **The flow's HTTP trigger URL is deployment state.** It's created per environment when the
  solution imports the flow. The plugin's OpenAPI `servers` URL is resolved per target at deploy
  time, never committed to the package.
- **OAuth registration is per environment.** The OAuthPluginVault reference (from `atk`
  `oauth/register` or the Teams Developer Portal) and the Entra app registration whose token the
  trigger accepts are organization prerequisites, recorded in targets as references only.
- **The trigger's authentication setting** ("who can trigger") is part of the flow source and is
  validated in Phase 11.
- **Order:** Phase 12 deploys the flow and records its trigger URL. Phase 13 provisions the agent
  against it.

## 7. Assumptions to validate before Phase 10

1. **Resolved (2026-11-20): how the agent reaches Power Automate.** It goes through the proven
   path in §6.1: an API plugin to the authenticated HTTP trigger. Still to confirm at Phase 12:
   whether the trigger URL stays stable across solution upgrades in the same environment (if not,
   the agent must be re-provisioned on each flow deployment).
2. **Gateway:** the custom connector's connection uses the on-premises data gateway, so creating
   connections stays manual per target.
3. **Managed environments and licensing** for TEST/PROD (needed for Pipelines and Git integration).
4. **Solution split:** whether the connector needs its own solution, imported before the solution
   containing flows and connection references (Microsoft's guidance for first-time imports).
5. **Connector auth:** which auth the connector uses toward IFS, and where its secret lives
   (Key Vault-backed environment variable vs manual entry).
6. **CI identity:** whether a service principal (application user) is allowed in each environment.
7. **Agent tooling choice:** Agents Toolkit (GA) now, WIQD (preview) re-evaluated at 1.0.
8. **PAC install path** on developer machines (.NET tool vs MSI; MSI-only commands).
