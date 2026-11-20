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
└── deploy.yaml          deployment intent: logical names, no secrets or environment IDs (planned)
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
- **Manual workflow recorder** - captures task-oriented IFS network activity
- **Analyzer** - converts captured activity into structured operation evidence
- **Operation model** - maintains canonical definitions of approved operations
- **Generators** - produce agent, connector, and flow dispatcher source artifacts
- **Workspace management** - manages independently maintained IFS capability packages
- **Deployment adapters** - reconcile package intent with Power Platform and Microsoft 365 per target environment
- **Doctor / bootstrap** - capability-based readiness checks and explicit local setup

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
