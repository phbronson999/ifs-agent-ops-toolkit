# IFS Agent Ops Toolkit

A toolkit for discovering, modeling, and packaging IFS Cloud operations for use by AI agents, including agent skills, Power Platform custom connectors, and dispatchers.

## Overview

IFS Agent Ops Toolkit provides a development environment for turning observed IFS Cloud workflows into reusable operations that AI agents can understand and execute.

The toolkit combines observed IFS network activity with IFS API metadata to build a canonical definition of each operation. These definitions become the source for generating the artifacts needed to expose IFS functionality to agents.

The goal is to define an IFS operation once and use that same definition throughout the agent execution path.

## Architecture

The toolkit separates development tooling from the packages it produces, and generation from deployment.

```text
1. Development toolchain   Git, Python, Node, PAC, Agents Toolkit, VS Code, Claude Code  (doctor / bootstrap)
2. Toolkit                 capture, sanitize, analyze, enrich, model, validate, generate, deployment adapters
3. Capability package      evidence, metadata, operations, workflows, generated artifacts, deployment intent
4. Deployment / ALM        targets, authentication, Power Platform solutions, agent provisioning, validation, promotion
5. Experiences             Copilot / Teams / SPFx / SharePoint consumers
```

A capability package describes *what should exist*, using logical names only. Deployment adapters
reconcile that intent with a target environment through Microsoft's current tooling (`pac`,
Microsoft 365 Agents Toolkit). Secrets, connection IDs and environment URLs never live in a package.
See [docs/roadmap.md](docs/roadmap.md) for the lifecycle model, Microsoft tooling matrix and phases.

### Toolkit

`ifs-agent-ops-toolkit` contains the reusable tooling used to:

- Capture IFS Cloud workflows and their network activity
- Sanitize recordings and import them into packages as repository-safe evidence
- Analyze relevant IFS API operations, including call order and data flow
- Enrich observed operations using IFS OpenAPI and metadata
- Build and maintain canonical operation and workflow definitions
- Generate agent skills, Power Platform connector and dispatcher source artifacts, and agent artifacts
- Deploy, validate and promote those artifacts per environment (deployment adapters)
- Check and prepare a developer machine (`doctor`, `bootstrap`)

### Capability Packages

IFS business functionality is maintained outside the toolkit in independently versioned packages.

For example:

```text
ifs-agent-ops-toolkit/

ifs-customer-orders/
ifs-shop-floor-clockings/
ifs-inventory/
```

A package can contain:

```text
ifs-customer-orders/
├── package.yaml
├── recordings/          sanitized evidence (record import)
├── metadata/            pinned $metadata / $openapi snapshots
├── operations/          canonical operations: the complete IFS API contracts (draft | approved)
├── workflows/           observed workflows: what the IFS web client did (evidence)
├── capabilities/        curated capabilities: agent-facing inputs, actions and bindings
├── agent/               skills, instructions, agent/plugin source
├── power-platform/      connector and flow source artifacts
└── deploy.yaml          deployment intent: logical names, no secrets, hosts or environment IDs
```

## Operation Model

The canonical operation definition is the source of truth for an IFS API operation: what IFS accepts
and how the call works. What an agent may supply is a separate, curated **capability** that
references canonical operations and declares where every value comes from (the agent, a document,
a user question, an earlier step, a default, a derivation or a fixed value). Observed workflows from
recordings are evidence for both, never a contract. See [docs/operation-model.md](docs/operation-model.md).

A canonical operation combines:

- Observed behavior from an IFS workflow
- IFS projection and endpoint information
- Request parameters and payloads
- Relevant headers
- Response structures
- IFS metadata and enumeration information
- Workflow sequencing and dependencies

Generated artifacts derive from this model rather than being maintained independently.

```text
IFS workflow
     ↓
Recorded network activity
     ↓
IFS API analysis
     ↓
Canonical operation definition
     ↓
Human review (operation: technically correct)
     ↓
Curated capability (agent-facing inputs, actions, bindings)
     ↓
Human review (capability: business contract approved)
     ↓
Agent skill
Custom connector
Dispatcher
```

This keeps the agent instructions and execution path aligned with the same underlying operation definition.

## Components

The project is expected to include:

- **MCP server** - exposes toolkit functionality to AI-assisted development environments
- **IFS client** - authentication, API access, OpenAPI, and metadata inspection
- **Guided capture** - the normal start of capability development: the user's intent and an ordered recording
  plan, recorded task by task in the IFS web UI by the browser extension (`browser-extension/`, Rust/WASM core)
- **Analyzer** - converts captured activity into structured operation evidence
- **Operation model** - maintains canonical definitions of approved operations
- **Generators** - produce agent, connector, and flow dispatcher source artifacts
- **Workspace management** - manages independently maintained IFS capability packages
- **Deployment adapters** - reconcile package intent with Power Platform and Microsoft 365 per target environment
- **Doctor / bootstrap** - capability-based readiness checks and explicit local setup

## Development workflow

Capability development starts with intent and a planned recording ([roadmap §1.1](docs/roadmap.md)):

```text
intent -> recording plan -> task-bucketed browser capture -> analysis -> draft capability + skill
       -> local Claude/MCP testing against TEST -> more planned recordings as needed -> local validation
       -> human approval -> deployment artifacts -> deployed-path validation
```

```powershell
ifs-agent-ops capture new "I want to teach the builder how to create a customer order" `
    --task "Create order header" --task "Add order line" --task "Add charge" --task "Finish order" `
    --workflow create-customer-order
# record in Edge/Chrome with the IFS Agent Ops side panel, then:
ifs-agent-ops capture import <session> --package ifs-customer-orders
```

Or tell Claude your intent: the MCP server (`capture_*` tools) proposes and tracks the plan.

### Test log

When Claude tests against IFS through the MCP server, every `call_protected_api` call is logged locally: the
request, the response and its headers (ETag, IFS warnings), the status and the time it took. Each tool result
carries a `test_log_ref` (for example `20261121-0007`) so findings can point at the exact call.

```powershell
ifs-agent-ops testlog list               # the latest day's calls (--day 2026-11-21, --all, --json)
ifs-agent-ops testlog show 20261121-0007 # one call in full
```

The log is kept per profile in `~/.ifs-agent-ops/profiles/<profile>/test-log/`, one file per day. It holds real
business data and the IFS host, so it stays on your machine and never goes into a package. Tokens and cookies
are never written. Set `IFS_AGENT_OPS_TEST_LOG=off` to turn it off.

### Validation findings

After testing a capability against IFS, Claude records what each test showed against the capability's numbered
runtime-validation requirements: passed, failed or inconclusive, a plain-language summary, and the test log
references of the calls that show it. Findings are saved in the package as
`validation/<capability>.findings.yaml`, so commit them with the package.

```powershell
ifs-agent-ops validation status create-customer-order --package ifs-customer-orders
ifs-agent-ops validation report create-customer-order --package ifs-customer-orders
```

`validation report` writes the evidence report to `<package>/.ifs-agent-ops/review/<capability>.validation.md`:
each requirement with its findings and the logged calls they cite. It quotes your local test log, so it isn't
committed. The TUI's **Testing** tab (key 5) shows the same thing as a tree: requirement, then findings, then
calls. Expand a call to see its request and response with their headers.

Each requirement shows its latest finding, or "not tested". If a requirement's wording changes in a later
revision, its earlier findings are marked outdated and stop counting. Findings are a report only: they never
change the capability's runtime-validation status or its approval. The execution engine sets that status.

## Reviewing and approving

A recording becomes usable in three reviews. The first is a look at the evidence. The other two are the
approval gates described in the [operation model](docs/operation-model.md).

### 1. Review the imported recording, then commit it

`capture import` writes the recording into the package but doesn't commit it. Check the following before
you commit:

- **Steps:** each plan step holds the calls you expect. For example, the order is created in "Create order
  header", and each line is posted in its own step.
- **No real data:** order and customer numbers are pseudo-values, the host is `ifs.example`, and binary
  bodies such as images read `<binary body removed>`.
- **Nothing you don't want kept:** if something shouldn't be there, re-record or re-import instead of
  editing the file by hand.

```powershell
# the analysis: calls grouped by plan step, the writes, and the data flow between calls
ifs-agent-ops har analyze <package>\recordings\<workflow>\<session>.har
# the plan as saved with the recording: step statuses and the calls that belong to each
code <package>\recordings\<workflow>\<session>.annotations.yaml
```

When the recording looks right, commit it in the package repository.

### 2. Operations: technically correct

```powershell
ifs-agent-ops operations draft --package ifs-customer-orders   # draft operations and workflows from every recording
ifs-agent-ops operations list  --package ifs-customer-orders   # operation ids and their status (draft, approved)
```

Review and approve work on a chosen set of operations. Either pick every operation a capability uses with
`--capability`, or name the operations; a unique part of an ID is enough:

```powershell
# report in <package>/.ifs-agent-ops/review/operations/
ifs-agent-ops operations review --package ifs-customer-orders --capability create-customer-order
ifs-agent-ops operations review --package ifs-customer-orders CreateChangeRequest MandatoryPrePosting

# you approve, in an interactive terminal: every operation of a capability at once, or one operation
ifs-agent-ops operations approve --package ifs-customer-orders --capability create-customer-order
ifs-agent-ops operations approve --package ifs-customer-orders CreateChangeRequest
```

The review report lists every finding. An **UNRESOLVED** note blocks approval until it's resolved. Add
`--details` or `--full` to `operations review` for more.

### 3. Capability: the business contract

A capability is curated, not drafted. You write it, with Claude's help, in
`capabilities/<id>.yaml`. It references the approved operations and states where every value comes from.

```powershell
ifs-agent-ops capabilities validate --package ifs-customer-orders               # check it against the operations
ifs-agent-ops capabilities review   --package ifs-customer-orders <capability>  # report in <package>/.ifs-agent-ops/review/
ifs-agent-ops capabilities approve  --package ifs-customer-orders <capability>  # you approve, in an interactive terminal
```

### Then generate

Only approved capabilities generate artifacts:

```powershell
ifs-agent-ops skills generate    --package ifs-customer-orders   # agent skills
ifs-agent-ops connector generate --package ifs-customer-orders   # Power Platform custom connector
ifs-agent-ops flow generate      --package ifs-customer-orders   # one Power Automate flow per capability action
```

`--check` reports what would change without writing anything.

There is one connector per IFS service (`power-platform/connectors/<Projection>/`), using **Basic
authentication through the on-premises data gateway**. The IFS username, password and gateway are chosen when
someone creates the connection; they are never stored in the package.

There is one flow per capability action (`power-platform/flows/<capability>/prepare.json` and `submit.json`,
with a `report.md`). A flow is built from the action's inputs, steps, bindings and outputs, not from the
connector's parameters:

- It is called through an HTTP trigger that only the users listed in the deployment target may call.
- It runs the steps in order. Per-line steps run one line at a time.
- Every IFS error is mapped to the capability's error meanings.
- It answers with JSON: `ok`, `error`, outputs or the proposal, questions and warnings.
- `submit` writes nothing unless the request says `confirmed: true`. If it fails partway, it stops and returns
  what it already created.

Flows need the capability's lookups and warnings in structured form (`match` on filter bindings, warning
conditions as `{field, equals | not_equals}`); prose rules are refused.

In the TUI you approve from a confirmation dialog: review it and press Enter. The CLI approve commands only
run in an interactive terminal and ask you to type back a one-time code shown on screen, because an agent could
run them. Your git `user.name` (or your Windows login) is recorded as the reviewer. They refuse to run from
a script or from Claude, because a person must approve. Claude can run the drafts and reviews and walk you through the reports.

## Deploying to Power Platform

`deploy.yaml` (committed with the package) declares what should exist, by logical name only: the solution and
its publisher, one connector per IFS service, the environment variable that holds the IFS host (the deployed
connector's host becomes `@environmentVariables("<name>")`), and a connection reference per connector.
A **target** (machine-local, in `~/.ifs-agent-ops/targets/<package>/`, never committed) names the environment.
Sign in to Power Platform first with `pac auth create`.

```powershell
ifs-agent-ops deploy init    --package ifs-customer-orders --publisher ContosoAdmin --prefix ti
ifs-agent-ops deploy check   --package ifs-customer-orders
ifs-agent-ops targets init   --package ifs-customer-orders --target dev      # pick from pac env list
ifs-agent-ops deploy plan    --package ifs-customer-orders --target dev      # read-only: what would change
ifs-agent-ops deploy apply   --package ifs-customer-orders --target dev      # asks before changing anything
ifs-agent-ops deploy status  --package ifs-customer-orders --target dev      # read-only, plus manual steps
ifs-agent-ops doctor --for power-platform
```

`apply` imports one unmanaged solution file with only what differs, adds existing components (such as a shared
environment variable) to the solution, then reads the environment back; a second `apply` changes nothing.
Connectors are always created inside the solution by import, which keeps the gateway setting (`pac connector
create` loses it). A person still creates each connection (gateway, IFS username and password) and picks it on
the connection reference; `status` lists what is left. The toolkit deploys to the development environment only:
promote with Power Platform Pipelines. `apply` also writes the deployment settings file next to the target.

Generated flows deploy into the same solution when `deploy.yaml` lists them:

```yaml
  flows:
  - source: power-platform/flows/create-customer-order
```

The target file names who may call their triggers:

```yaml
flow_trigger_users:
- maker@contoso.example
```

`apply` fills in the deployed connector's API name and that user list, imports the flows turned on, and
records each flow's trigger URL under `flow_triggers` in the target file. The user list and the trigger URLs
stay machine-local and never go into the package. Reading flows back and getting trigger URLs uses the Azure
CLI sign-in (`az login --allow-no-subscriptions`).

### Validating the deployed path

```powershell
ifs-agent-ops deploy validate --package ifs-customer-orders --target dev
```

`validate` checks three layers. **Static**: `deploy.yaml` and the generated connectors and skills. **Platform**:
the environment matches `deploy.yaml` with no manual steps left, the solution exports (Pipelines needs that),
and the Power Apps checker reports no critical or high issues (it uploads the solution file to Microsoft's
checker service; `--skip-solution-check` skips it, but then nothing is marked validated). **Runtime**: the
capability's runtime cases (`validation/<capability>.runtime.yaml` in the package) are called through the
deployed flows' HTTP triggers with your Azure CLI sign-in and checked against their expectations: the questions
and warnings returned, how many rows each lookup found, or that a candidate is among them, and the outputs. Cases
of an action that writes to IFS (`submit`) run only with `--allow-writes`, which asks first. Otherwise they are
reported as not run and don't block.

```yaml
cases:
  - name: known customer, part and coordinator
    action: prepare
    inputs: {customer_no: "C1", order_coordinator: "18", lines: [{catalog_no: "P1", quantity: 10}]}
    expect: {ok: true, questions: [], matches: {find-customer: 1}, lines: [{check-part: 1}]}
```

Learned on the development environment: through the connector and the on-premises gateway, a **space anywhere
in `$filter`** breaks the request (IFS answers `TYPES_NOT_COMPATIBLE`, or finds nothing for a quoted value). The
generated flows separate filter operators with tabs, and a multi-word `contains` search becomes one `contains()`
per word. Two more:

- **Functions with a parameter alias** (`P=@P` with `?@P=<json>`, e.g. GetCustomerDefaults) are called inside an
  OData `$batch`. A flow can't set a parameter named `@P`, IFS rejects the JSON inline in the path, and connector
  policies don't take effect through the gateway. The batch body carries the request line unchanged; IFS wants `:`
  in date-times written as `%3A` there.
- **Error responses have no schema in the connector**, so IFS's error message reaches the flow. IFS error bodies
  don't match the pinned spec, and a schema check would replace the message with a validation failure.

It then sets each deployed capability's `runtime_validation.status`: **validated** when every requirement's
latest finding passed (testing against IFS) and the deployed-path checks passed, **failed** when a finding
failed, otherwise **conditional** (or **untested**). The reason is recorded under `runtime_validation.evaluation`;
the change is a few lines in the capability file, for you to review and commit. Validation never changes an
approval. Each run is also saved locally under `.ifs-agent-ops/runs/`.

## The Microsoft 365 agent

One declarative agent per package calls the capabilities through their flows. This is the path proven by hand:
declarative agent, API plugin (OpenAPI, `OAuthPluginVault` with Entra sign-in), the flows' HTTP triggers,
connector, on-premises gateway, IFS. Each capability action is one plugin function. `prepare` runs without
asking. `submit` writes, so Copilot asks before calling it, and the generated instructions only call it after
the user confirmed the proposal.

```powershell
ifs-agent-ops agent generate --package ifs-customer-orders           # agent/m365-agent/ (Agents Toolkit project)
ifs-agent-ops agent render   --package ifs-customer-orders --target dev
cd "$HOME\.ifs-agent-ops\agents\ifs-customer-orders\dev"
atk provision --env dev                                               # signs in; asks for the Entra app's client id and secret
ifs-agent-ops doctor --for agents
```

The package holds the project with placeholders. `render` fills in the target's values in a machine-local copy:
the flows' server and trigger paths (recorded by `deploy apply`) and the sign-in tenant. Optional settings go
under `agent:` in the target file: `tenant_id`, `scope` (`personal` or `shared`), `app_name_suffix` and
`developer`. The Agents Toolkit keeps the app id and the OAuth registration id in that copy's env file, and
later renders keep them.

The app version comes from the package version with the major number raised by one, because Microsoft rejects
versions that start with 0. `render` also raises the patch number whenever the rendered app package changed, so
a published update always has a higher version. Install the CLI with
`npm install -g @microsoft/m365agentstoolkit-cli` (Node 20.19+ or 22.12+).

## Design Principles

- Define an operation once and generate dependent artifacts from it.
- Preserve raw workflow evidence for troubleshooting and future analysis.
- Keep organization-specific operation packages separate from the toolkit itself.
- Use IFS metadata to verify what the API supports rather than relying solely on observed traffic.
- Preserve workflow context and operation ordering when analyzing network activity.
- Keep MCP as a first-class interface so the toolkit can be used directly from AI-assisted development environments.
- Require human review before generated operation definitions become authoritative.
- Keep generation and deployment separate; keep Microsoft deployment tooling out of the canonical model.
- Keep secrets and environment-specific identifiers out of packages; give generated resources stable logical identities.
- Inspect (`doctor`), set up locally (`bootstrap`) and change external systems (`deploy`) only as separate, explicit actions.

## Status

Early development, used against a real IFS Cloud TEST environment and a Power Platform development
environment. Phases 0–13 of the [roadmap](docs/roadmap.md) are merged:

- **Capture → operations → capability:** guided browser capture, sanitized recordings, canonical operations and
  curated capabilities, each approved by a person.
- **Testing with Claude:** a local test log of IFS calls and validation findings per requirement. The pilot
  capability (`create-customer-order`) has all 13 requirements passed.
- **Generators:** agent skills, Power Platform custom connectors (one per IFS service, Basic authentication
  through the on-premises data gateway), Power Automate flows (one per capability action) and a Microsoft
  365 declarative agent (one per package).
- **Deployment:** `deploy.yaml` plus machine-local targets; `deploy plan / apply / status` reconcile a solution
  in a development environment (idempotent); `deploy validate` checks the deployed path and records the
  capability's runtime-validation status (the pilot is **validated** on the development target).

The pilot runs end to end on the development environment, from Microsoft 365 Copilot through the agent, the
flows, the connector and the gateway to IFS and back. `deploy validate` runs the pilot's runtime cases through
the flows.

**Next:** Phase 14, promotion and CI readiness: promoting the solution from development to test through Power
Platform Pipelines, and the agent with it, from the CLI. Only capabilities whose runtime status is validated
(or conditional, within its recorded scope) are promoted.

The project evolved from my Python-based IFS MCP implementation, which provides IFS authentication,
protected API calls, OpenAPI inspection, metadata analysis and HAR-based workflow analysis:
https://github.com/phbronson999/ifs-mcp-server-local/tree/python-port
