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
├── capabilities/        curated capabilities: agent-facing inputs, actions and bindings (planned, Phase 7b)
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
```

`--check` reports what would change without writing anything.

The connector uses **Basic authentication through the on-premises data gateway**. The IFS username,
password and gateway are chosen when someone creates the connection; they are never stored in the package.

### Deployment intent

`deploy.yaml` declares what should exist in Power Platform, by logical name only: the solution and its
publisher, the connector, the environment variable that holds the IFS host (the deployed connector's host
becomes `@environmentVariables("<name>")`), and the connection reference.

```powershell
ifs-agent-ops deploy init  --package ifs-customer-orders --publisher TietexAdmin --prefix ti
ifs-agent-ops deploy check --package ifs-customer-orders
```

In the TUI you approve from a confirmation dialog: review it and press Enter. The CLI approve commands only
run in an interactive terminal and ask you to type back a one-time code shown on screen, because an agent could
run them. Your git `user.name` (or your Windows login) is recorded as the reviewer. They refuse to run from
a script or from Claude, because a person must approve. Claude can run the drafts and reviews and walk you through the reports.

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

Early development and architecture validation.

The project is evolving from my existing Python-based IFS MCP implementation that already provides IFS authentication, protected API calls, OpenAPI inspection, metadata analysis, and HAR-based workflow analysis. 
https://github.com/phbronson999/ifs-mcp-server-local/tree/python-port
