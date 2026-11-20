# IFS Agent Ops Toolkit

A toolkit for discovering, modeling, and packaging IFS Cloud operations for use by AI agents, including agent skills, Power Platform custom connectors, and dispatchers.

## Overview

IFS Agent Ops Toolkit provides a development environment for turning observed IFS Cloud workflows into reusable operations that AI agents can understand and execute.

The toolkit combines observed IFS network activity with IFS API metadata to build a canonical definition of each operation. These definitions become the source for generating the artifacts needed to expose IFS functionality to agents.

The goal is to define an IFS operation once and use that same definition throughout the agent execution path.

## Architecture

The toolkit separates development tooling from the packages it produces.

### Toolkit

`ifs-agent-ops-toolkit` contains the reusable tooling used to:

- Capture IFS Cloud workflows and their network activity
- Organize captured traffic by workflow and task
- Analyze relevant IFS API operations
- Enrich observed operations using IFS OpenAPI and metadata
- Build and maintain canonical operation definitions
- Generate agent skills and instructions
- Generate Power Platform custom connector definitions
- Generate Power Automate flow dispatcher
- Validate and test generated operations

### Operation Packages

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
├── recordings/
├── operations/
├── agent/
│   ├── skills/
│   └── instructions/
├── power-platform/
│   └── connector/
└── dispatcher/
```

## Operation Model

The canonical operation definition is the source of truth for an agent-accessible IFS operation.

It combines:

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
Human review
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
- **Generators** - produce agent, connector, and flow dispatcher artifacts
- **Workspace management** - manages independently maintained IFS operation packages

## Design Principles

- Define an operation once and generate dependent artifacts from it.
- Preserve raw workflow evidence for troubleshooting and future analysis.
- Keep organization-specific operation packages separate from the toolkit itself.
- Use IFS metadata to verify what the API supports rather than relying solely on observed traffic.
- Preserve workflow context and operation ordering when analyzing network activity.
- Keep MCP as a first-class interface so the toolkit can be used directly from AI-assisted development environments.
- Require human review before generated operation definitions become authoritative.

## Status

Early development and architecture validation.

The project is evolving from my existing Python-based IFS MCP implementation that already provides IFS authentication, protected API calls, OpenAPI inspection, metadata analysis, and HAR-based workflow analysis. 
https://github.com/phbronson999/ifs-mcp-server-local/tree/python-port
