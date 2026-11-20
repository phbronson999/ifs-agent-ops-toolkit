# Operation model: evidence, canonical operations and capabilities

The package holds three distinct things. Generators consume the third, which references the second;
the first is evidence for both.

| Concept | Answers | Lives in | Produced by | Approved means |
|---|---|---|---|---|
| **Observed workflow** | What did the IFS web client actually do? | `workflows/<recording>.yaml` | `operations draft` (from one recording) | not approved; evidence only |
| **Canonical operation** | What does IFS accept, and how does the call work? | `operations/<id>.yaml` | `operations draft` (recordings + pinned metadata) | the technical description is correct |
| **Capability** (curated) | What should an agent/application do to accomplish the business task, and what may it supply? | `capabilities/<id>.yaml` (Phase 7b) | authored by a human, assisted by drafts | the business contract is reviewed and safe to generate from |

```text
PDF / user input
  -> capability inputs (agent / document / user question)
  -> lookups and validation (capability steps over canonical operations)
  -> derived, defaulted and configured values
  -> confirmation
  -> full canonical IFS request(s)
  -> IFS
```

## 1. Observed workflows (evidence)

An observed workflow is the recorded sequence of IFS calls, with data-flow bindings. It includes
UI-only lookups, refreshes, redundant reads, and validations triggered by UI behaviour. The pilot
recording has 72 steps. It's kept as evidence and is **never** generated from directly.

## 2. Canonical operations

A canonical operation is the complete, authoritative IFS API contract: the original path template,
every parameter IFS accepts (typed from metadata), required headers, the response shape, and the
evidence of how it was called. The customer-order header create has about 55 body fields and the
line create about 91. Canonical operations are **never trimmed** to suit an agent.

- `status: approved` on an operation means *the technical description is verified*. It does **not**
  mean exposed to agents.
- `depends_on` is **observed** data flow (including UI-driven calls). It's evidence for curation,
  not the capability's execution order.

## 3. Capabilities: the curated, agent-facing contract (proposed schema, Phase 7b)

A capability references canonical operations by id and declares, for every value a step sends,
where that value comes from. It never copies an operation to hide fields. Platform-specific
concerns (Power Platform parameter renames, connector quirks) don't belong here either; they stay
in the connector generator (Phase 9).

```yaml
id: create-customer-order
status: draft                         # draft | approved | deprecated
title: Create a customer order
evidence: [workflows/create-customer-order.header-lines-charges.yaml]

config:                               # package-level business defaults (not environment-specific)
  order_type: { type: string, value: "NO" }

inputs:                               # the agent-facing contract
  - name: customer_no
    type: string                      # from the canonical type (Edm.String)
    source: document                  # agent | document | user
    required: true
    resolve: { step: find-customer }  # must resolve through a lookup
    confirm: true                     # shown to the user before submission
  - name: wanted_delivery_date
    type: date
    source: user                      # ask the user when the PDF lacks it
    required: true
  - name: lines
    type: list
    items:
      - { name: catalog_no, type: string, source: document, required: true, resolve: { step: validate-line } }
      - { name: quantity,   type: decimal, source: document, required: true }

actions:                              # what an agent/dispatcher can invoke
  - name: prepare                     # lookups + validation only; returns a proposal and warnings
    steps: [find-customer, customer-defaults, validate-line, price-line]
  - name: submit                      # writes; refuses without explicit confirmation
    requires_confirmation: true
    steps: [create-header, create-line]

steps:
  - id: find-customer
    operation: CustomerOrderHandling.query.Reference_CustOrdCust7
    role: lookup                      # lookup | validation | calculation | write
    bindings:
      filter.CustomerNo: { input: customer_no }
  - id: create-header
    operation: CustomerOrderHandling.create.CustomerOrderSet
    role: write
    bindings:
      body.CustomerNo:         { input: customer_no }
      body.Contract:           { step: customer-defaults, field: Contract }
      body.OrderId:            { config: order_type }
      body.CurrencyCode:       { step: customer-defaults, field: CurrencyCode }
      body.WantedDeliveryDate: { input: wanted_delivery_date, transform: datetime }
    # every canonical parameter not bound here is omitted
  - id: create-line
    operation: CustomerOrderHandling.create.CustomerOrderSet.OrderLinesArray
    role: write
    for_each: lines
    bindings:
      path.OrderNo:   { step: create-header, field: OrderNo }
      body.CatalogNo: { item: catalog_no }
      body.BuyQtyDue: { item: quantity }

outputs:
  - { name: order_no, step: create-header, field: OrderNo }
```

Value sources: `input` (agent, document or user), `item` (a field of a list input), `step`
(an earlier step's response field), `config` (package default), `derived` (an expression over
other values), `fixed` (a literal), or unbound, which means omitted. A value that is neither
bound nor an input is **not** agent-visible.

Every question a generator needs answered maps to a field:

| Question | Answer from |
|---|---|
| Which operations belong to the capability? | `steps[].operation` |
| Which are internal support vs. exposed? | `actions` (agent-invocable) vs. steps only reachable inside an action |
| Which operations does the connector need? | every operation referenced by an approved capability's steps |
| Which inputs may the agent supply? | `inputs` (`source: agent` or `document`) |
| Which are required from the user/PDF? | `inputs` with `required: true`, by `source` |
| From earlier outputs / defaults / derived / fixed? | step `bindings` (`step` / `config` / `derived` / `fixed`) |
| Which need lookup or validation? | `inputs[].resolve` and `role: lookup|validation` steps |
| Which need user confirmation? | `inputs[].confirm` and `actions[].requires_confirmation` |
| What the dispatcher exposes | `actions` with their inputs and outputs (not connector parameters) |

Validation rules for capabilities (Phase 7b):
- every referenced operation exists and is **approved**
- every binding targets a real canonical parameter, and every `step`/`field` reference exists
  and precedes its use
- every **required** canonical parameter of a write step is bound
- input types are compatible with the canonical types they feed
- write actions require confirmation

## 4. What each generator reads

| Generator | Reads | Never |
|---|---|---|
| Skill (Phase 8) | approved capability: purpose, inputs, questions, confirmation, actions; operations for technical detail | the full field surface of an operation |
| Connector (Phase 9) | operations referenced by approved capabilities (plus §5a compatibility transforms) | operations not used by any approved capability |
| Dispatcher (Phase 12) | capability actions: inputs, steps, bindings, outputs | a mirror of every connector parameter |
| Agent (Phase 13) | capability actions and their contract, via the dispatcher endpoint | raw IFS operations |

## 5. Evidence principles (from the Phase 6 pilot)

1. **`$batch` subrequests are first-class evidence.** Order lines were created inside a `$batch`
   changeset. Batch parts keep their order (`#58.2`), data flow, redaction and evidence
   references (`call_index` + `part`).
2. **IFS behavioural rules are evidence-driven unless metadata or documentation proves them
   universal.** "No `Z` on write datetimes" came from shop floor clocking actions; customer-order
   writes accepted `Z`. The model records the format IFS accepted per operation.
3. **Function parameter quoting follows the declared type.** `TaxCode=null` and `TaxCode='T1'`
   are one parameter of one operation.
4. **Organisation-specific business configuration in IFS metadata belongs in the organisation's
   package.** An example is a configured layout name such as `TietexsamplesDOTnlbl` as an enum
   member. Credentials, hosts, realms, internal network identifiers and secrets remain the
   things that are removed.
