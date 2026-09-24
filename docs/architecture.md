# Architecture

## Context

Scarcity Router is a local decision service between capacity sources, curated
data and clients. In its default **recommendation-only mode** it does not sit
on the model request path:

```text
provider telemetry ────────> collectors ──> normalized capacity
model catalog ────────────────────────────> capability data
task/profile ─────────────────────────────> requirements
user policy ──────────────────────────────> reservations/preferences
                                             |
                                             v
                                          selector
                                             |
                              selected + fallbacks + explanation
                                             |
                                   CLI / REST / MCP / UI
```

The optional **execution gateway** (D-040, program map A0 / issues #85–#95)
adds a second mode: an authenticated server component exposes one
OpenAI-compatible endpoint so standard clients can have requests served from
the best available authorized resource — API providers, Ollama/local
inference, or the approved local Codex adapter — under the same
least-scarce-capable discipline. The gateway reuses this recommendation core;
it does not replace it. The full module map, contracts and security
architecture of the gateway are in
[Execution-gateway architecture](#execution-gateway-architecture-a0-program)
below and in [`docs/security.md`](security.md).

## Components

### Collectors

Small provider adapters acquire telemetry and translate it into the normalized
capacity model. Acquisition is read-only except for the single bounded
provider-managed OpenAI auth-recovery exception of `docs/decisions.md` D-018. They own provider discovery, subprocess/protocol
handling, response validation and provider-specific error mapping. They do not
rank models or interpret task difficulty.

The supported collector set is exactly OpenAI/Codex and Z.ai Coding Plan.
Adding a later provider requires separate scope and does not change the
selector's provider-independent boundary. The execution-gateway program adds
execution *adapters* (generic HTTP, Ollama, Codex) under the same
provider-edge discipline — see
[Execution-gateway architecture](#execution-gateway-architecture-a0-program)
and [`docs/providers.md`](providers.md); it does not add recommendation
collectors on its own.

Provider drift must stop at this boundary. A broken adapter yields an explicit
status such as `schema_changed` while the rest of the service remains healthy.

### Capacity store/view

Holds the most recent v3 normalized snapshots and their retrieval timestamps.
Freshness evaluation, refresh behavior and durable history are separate concerns;
the initial implementation may be in-memory and refreshed on demand. Secrets and
raw Authorization values never enter the normalized state.

### Catalog

Contains model identities, provider/account relationships, hard properties and
curated multidimensional capabilities. Profiles map stable task names to
requirements. Catalog data is versioned and human-reviewable; it does not read
runtime quota.

Catalog v2 (D-032) adds explicit normalized `reasoning_effort` per calibrated
invocation configuration. Model capability, reasoning intensity and subscription
scarcity are separate inputs: configurations do not inherit vectors from sibling
efforts or invent separate quota buckets. `ModelIdentity` remains the same
provider/model/opaque-variant triple. Selector ranking reads catalog effort
directly after scarcity and capability margin, before preference and identity;
no interface parses variant to infer effort. Null is unconfigured, distinct from
the real `"none"` effort setting. Machine-interface decision/envelope schemas
remain v1 unchanged.

### Policy

Contains default scarcity behavior, reservation rules, preference modes and
explicit overrides. Runtime policy is separate from repository governance and
from the catalog, so a user can change today's preference without changing
source repositories or capability claims. The pure resource-state and
preservation primitives live in `scarcity_router/scarcity.py` (continuous
scarcity penalty, explanatory labels, binding applicability, most-restrictive
aggregation, known/unknown/unavailable assessments) and
`scarcity_router/policy.py` (unknown-capacity modes, scope-targeted
reservations, timezone-aware blackouts, `ReplenishmentState` visibility modes,
the `UserPolicy` container); ranking modes remain the selector's concern.

### Selector

Consumes only normalized capacity, catalog data, task requirements and policy.
It has no credential access, no provider parsing and no client-specific code.
It produces a deterministic decision object with the selected model, ranked
alternatives, exclusions, input provenance and reasons.

The pure modules `scarcity_router/selector.py` (the `balanced` candidate
pipeline, the exact ranking order, `SelectorPolicy` and the monotone
`tighten_requirement` merge) and `scarcity_router/simulation.py` (typed
overrides applied to copies of the inputs, re-running the same `select_model`
core for the baseline and simulated decisions) provide the selection behavior.
The application layer
`scarcity_router/selection_app.py` loads artifacts strictly, resolves the
requirement, obtains one shared evaluation instant through
`collect_status` and renders deterministic output; `scarcity_router/config.py`
resolves and provisions the default user configuration
(`~/.config/scarcity-router`, D-036); `scarcity_router/cli.py`
dispatches `status`, `select`, `simulate` and `install-config`.

### Interfaces

- **CLI** is the primary local operational interface. The module dispatcher
  provides read-only `status`, `select` (with `--explain` and `--json`) and
  `simulate` through one application path; a separate `doctor` command remains
  deferred.
- **REST** is the language-neutral machine contract. It binds to `127.0.0.1`
  by default and exposes exactly `/healthz`, `/v1/status`, `/v1/select` and
  `/v1/simulate`. `/v1/providers` and `/v1/providers/{provider}` remain
  deferred because `/v1/status` already returns the full snapshot set. The
  loopback-only standard-library adapter in `scarcity_router/server.py`
  (`python -m scarcity_router.server`, default port 8765, serialized
  requests, no runtime dependency) calls the same typed application seam as
  the CLI.
- **MCP** is a thin adapter over local stdio. It exposes
  `scarcity_status`, `scarcity_select` and `scarcity_simulate`, calls the
  application layer directly in-process and never requires the REST server.
  `scarcity_router/mcp.py` uses the official MCP SDK v2 low-level `Server` API,
  structured tool results and the client-owned stdio lifecycle. It shares the
  transport-neutral dependency record in `selection_app.py`, the logical
  parser/envelopes in `scarcity_router/machine_api.py` and the same typed
  application seam; it has no REST runtime dependency.
- **Dashboard** is a small operational view after core contracts exist, not a
  separate frontend product. In the execution-gateway program its realization
  is the M09 web UX served by the server component.

The authoritative machine-interface contract — envelopes, error semantics,
side-effect wording, security/lifecycle boundary, versioning and the
CLI/REST/MCP parity requirement — is [`docs/machine-interfaces.md`](machine-interfaces.md)
(D-028). No interface owns selection or collector business logic. The
OpenAI-compatible execution surface is a separately versioned contract
outside this v1 document (D-045; see
[Execution-gateway architecture](#execution-gateway-architecture-a0-program)).

## Dependency direction

Dependencies point inward:

```text
provider implementations ---> collector contract ---> normalized domain
CLI / REST / MCP -----------> application service ---> selector/domain
data files -----------------> catalog/policy loaders -> selector/domain
```

The domain must not import a provider, web framework, MCP SDK, CLI framework or
Kilo-specific module. Provider adapters may depend on small protocol helpers but
not on the selector.

The MCP SDK is isolated at the `scarcity_router/mcp.py` transport edge. The
shared `ApplicationDependencies` record carries only process-configured
artifact paths plus injectable collectors and clock; clients cannot supply
paths, endpoints or credentials. `machine_api.py` contains logical request
parsing and v1 envelopes, not selection or scarcity logic.

## Domain contracts

The current normalized capacity contract is v3, documented in
`docs/capacity-model.md`:

- `CapacitySnapshot`: schema version, provider/source identifiers, optional safe
  plan, retrieval time, status, windows and safe diagnostics. Account identifiers
  and freshness fields are not in the contract.
- `CapacityWindow`: validated resource and period kind, optional semantic
  capacity-scope identity (`scope_id`; `(provider, scope_id)` identifies one
  scope, `None`/omitted means unknown applicability, opaque exact-match only),
  optional duration, complementary used/remaining percentage pair, optional
  reset time and an allowlisted opaque provider window identifier. Both
  `scope_id` and `window_id` are never parsed by consumers; `window_id` is
  diagnostic only (D-020, D-023).
- `ModelProfile`: stable model identity, variants, provider, hard properties,
  capability assessments and provenance. `ModelCatalogEntry` plus
  `ModelIdentity`, `ModelHardProperties`, `EvidenceRef`, `HumanOverride` and
  the `CapabilityAssessment` vector in `scarcity_router/selection_types.py`
  represent these values: unknown capability is the explicit
  `{"rating": null}` state, a known rating requires complete provenance,
  overrides preserve the source assessment, and `capacity_bindings`
  distinguishes unknown applicability (`None`) from a known non-empty set of
  exact `(provider, scope_id)` references.
- `TaskRequirement`: task level, per-dimension capability minima on the frozen
  `1..5` scale with explicit unknown representation, typed hard constraints and
  single-place profile expansion. Its stored shape is the three-part
  (`task_level`, `capability_minima`, `hard_constraints`) form; profile
  expansion is a construction pathway, not a serialized fourth field.
- `SelectionDecision`: selected candidate, ranked alternatives, exclusions,
  reasons, capacity/catalog versions and degraded/unknown indicators.
  `scarcity_router/selector.py` also provides the per-candidate
  `CandidateEvaluation` record: one primary
  exclusion stage from the closed vocabulary (`policy_blackout`,
  `hard_constraint`, `capability`, `capacity`, `reservation`), structured
  hard/capability failures, normalized reason codes, deterministic
  closest-candidate and recoverable-candidate lists for no-solution
  results, and serialize-only `to_dict()` output.
- `ScarcityAssessment`: the per-candidate scarcity result over explicit
  capacity bindings — continuous integer penalty, explanatory label,
  explicit `known`/`unknown`/`unavailable` state and normalized reason
  codes. `scarcity_router/scarcity.py` applies the most restrictive applicable
  window, ignores unrelated scopes, gives explicit exhaustion
  (`remaining_percent == 0`) precedence over incomplete telemetry, and assigns
  no numeric penalty to unknown capacity.

The domain also defines these supporting or forward-looking contracts:

- `CapacityScope`: `(provider, scope_id)` identity for one normalized capacity
  scope. The identity exists in the contract through
  `CapacityWindow.scope_id`, and `CapacityScopeRef` binds catalog entries to
  exact scopes.
- `ReplenishmentState`: minimal safe reset-credit facts — replenishment
  opportunities, never current capacity. `scarcity_router/policy.py` supports
  the `ignore`, `advisory` and `recoverable` visibility modes.
- `ExecutionBudget`: a finite bounded envelope for any future compound
  recommendation. It remains a conceptual product boundary; the service does
  not execute the recommended workflow.

Schemas must distinguish omitted, unknown, unsupported, unavailable and zero. The
capacity contract uses omitted optional fields for unknown values and explicit
`unknown` enum values for unresolved window semantics.

## Configuration boundaries

Separate configuration namespaces are expected:

- static catalog and profile data distributed with the project;
- user policy and temporary preferences;
- collector discovery/configuration without copied secrets;
- service settings such as refresh interval and local bind address.

The root `model-policy.json` currently carries both selector-facing
task-profile data and descriptive development/model-role metadata. Separating
those concerns before a public configuration API is a deferred design choice;
this cleanup does not split or redesign the artifact.

Do not place secrets in ordinary configuration. Do not make users edit
`AGENTS.md`, repository policy or the capability catalog for daily choices.

## Change isolation and reliability

Provider adapters are expected to change more frequently than the normalized
domain. Interface adapters may evolve independently. Public serialized
contracts require explicit versioning once released.

Partial failure is normal: one provider may be unknown while others remain
usable. The selector never fabricates telemetry to hide that failure and the
explanation must identify degraded inputs.

## Execution-gateway architecture (A0 program)

This section is the A0 architecture specification for the optional execution
gateway (issue #85; decisions D-040 through D-045). It defines module
boundaries, responsibilities and contracts precisely enough that each module
issue can proceed independently without inventing incompatible assumptions.
A0 itself implements nothing; every runtime behavior below lands in a module
issue.

### Program map

| Planning id | Forgejo issue | Scope |
| --- | --- | --- |
| A0 | #85 | Architecture, decisions, contracts (this section) |
| M01 | #86 | Resource registry, state, and collectors for executable resources |
| M02 | #87 | Routing core, policy, and client-requirement binding for executable targets |
| M03 | #88 | OpenAI-compatible gateway and execution coordinator |
| M04 | #89 | Generic OpenAI-compatible HTTP adapter and Ollama integration |
| M05 | #90 | Native worker, pairing, and execution transport |
| M06 | #91 | Codex adapter: CLI/App Server with Desktop and VS Code installations |
| M07 | #92 | ZCode adapter feasibility — Stage 1 complete; Stage 2 cancelled (D-047) |
| M08 | #93 | MCP, REST, and CLI compatibility; optional remote mode |
| M09 | #94 | Configuration, web UX, and diagnostics |
| M10 | #95 | Distribution, installation, update, and end-to-end acceptance |

Dependency direction (blocked-by; cycle-free): M01→A0; M02→A0; M03→A0,M01,M02;
M04→A0,M03; M05→A0,M03; M06→A0,M05 (Stage 2 only; Stage 1 evidence is exempt);
M07→A0,M05 (Stage 2 only; Stage 1 evidence is exempt; Stage 2 cancelled by
D-047); M08→A0; M09→A0,M03;
M10→A0,M01,M02,M03,M04,M05. The first useful execution vertical slice is
M01/M02 → M03/M04 with the minimum M09/M10 support each slice needs; a user
must be able to use real API/Ollama resources through one OpenAI-compatible
endpoint before all local CLI adapters are complete. M07 was an independent
research track that never blocked the program: Stage 1 produced
[`docs/zcode-adapter-stage1-evidence.md`](zcode-adapter-stage1-evidence.md)
and Stage 2 was cancelled by owner decision (D-047).

### Operating modes and deployment topologies

1. **Recommendation-only (default, unchanged).** CLI, loopback REST v1 and
   stdio MCP over the existing core; no server component, worker or Docker.
   This is what exists today and remains installable and supported exactly as
   is (D-040, guarded by M08/M10).
2. **Server-only execution.** The server component serves
   `/v1/models` + `/v1/chat/completions` (plus control API and UI) and
   executes through server-direct HTTP providers (including a
   network-accessible Ollama). No worker is required; API-only operation
   must work without one (M04).
3. **Server plus workers.** One server, one or more native workers on the
   user's other machines (LAN/VPN). Workers bridge localhost-only Ollama
   instances and local Codex entitlements to the server over outbound
   TLS/WSS; no inbound worker port, firewall rule or manual IP configuration
   (M05).
4. **Remote recommendation bridge (optional, M08).** An MCP/control client
   explicitly configured against the server instead of local operation:
   explicit configuration, explicit failure, never a silent local fallback.

Not provided: internet-facing execution without a separate explicit owner
decision, multi-tenant SaaS, or any topology that requires a deployed
database, cache or message-queue service (D-041).

### Module map and responsibility boundaries

The boundaries below are **logical modules inside one Python package, not
microservices** (D-041). One server component and one native worker component
exist at runtime; no module boundary introduces a new container or deployed
service.

| Module | Issue | Owns | Main inputs | Main outputs | Never does |
| --- | --- | --- | --- | --- | --- |
| Resource state, registry and collectors | M01 (#86) | Identity/health/freshness/cost/pool observation of every resource; versioned state snapshots; bounded polling/cache (resolves U-003 for the server state store) | Provider telemetry, worker state reports, admin resource configuration | Versioned resource-state snapshots; eligibility reports (D-039) | Rank, route, execute, or let telemetry change capability ratings |
| Routing core | M02 (#87) | Pure deterministic selection of an authorized executable target; policy; requirement binding; decision provenance | State snapshots, catalog/policy artifacts, request requirements, client identity/profile, admin constraints | Route decisions (with target, provenance, exclusions) | I/O, provider parsing, credential access, execution |
| Execution coordinator + gateway | M03 (#88) | Ingress request validation, admission, concurrency reservation, dispatch, streaming lifecycle, cancellation, usage/accounting, audit writing | Route decisions (or pinned targets), execution adapters, limits | Streams/responses to clients; usage and audit records | Routing decisions, provider parsing, executing client tools |
| HTTP execution adapters | M04 (#89) | One generic OpenAI-compatible HTTP adapter with evidence-based provider presets; Ollama (direct + worker-bridged transport) | Coordinator dispatch, admin-configured origins/credentials | Provider requests/responses, capability reports | Per-provider gateways; sourcing configuration from client requests |
| Native worker + transport | M05 (#90) | Outbound TLS/WSS connection, pairing identity, heartbeat, state reporting, local adapter invocation within allowlists, local diagnostics | Server worker-protocol messages, local resources | State reports, streams, usage reports | Routing decisions; generic shell/ssh; expanding its allowlist on request |
| Codex adapter | M06 (#91) | Codex execution through the official CLI/App Server mechanisms on Desktop/CLI/VS Code installations (worker-side or server-reachable) | Stage-1 evidence, D-039 eligibility, worker isolation | Compatibility matrix entries, execution | GUI automation; token extraction; adopting user projects/plugins |
| ZCode adapter | M07 (#92) | Closed (D-047, 2026-09-20): Stage 1 produced dated feasibility evidence; Stage 2 is cancelled — no execution adapter is planned | Stage-1 evidence, vendor terms | Dated feasibility evidence only; no execution | Assuming official APIs, promotional eligibility or redistribution rights |
| Interface compatibility | M08 (#93) | Frozen v1 guardrail suite; parity; optional remote bridge | Every module's changes | Green parity suite | Semantic drift in frozen surfaces |
| Configuration, web UX, diagnostics | M09 (#94) | Admin onboarding, provider configuration, pairing UI, client keys, routing profiles, diagnostics/doctor | Admin actions, server state | Control API + UI; copyable client configuration | A second selector; exporting secrets; author-private defaults |
| Distribution and acceptance | M10 (#95) | Server container, worker packaging, update path, E2E and security acceptance | All modules | Installable artifacts; honest acceptance matrix | Fictional artifacts; depending on private infrastructure |

### Responsibility-separation invariants

- **Resource State observes; it never decides.** Registry/state/collectors
  answer what exists, whether it is reachable, how fresh the observation is,
  what it costs and which quota pools apply. Quota never changes a capability
  rating (D-002), and telemetry never reorders ranking.
- **The Routing Core decides; it never touches the world.** It consumes only
  normalized state, catalog/policy artifacts, requirements, identity and
  constraints, and produces a deterministic route decision with provenance. It
  performs no network, subprocess or credential operation — the existing
  import-cleanliness of `selector.py`/`scarcity.py`/`policy.py` extends to
  every execution-era input.
- **Execution coordinates; it never chooses and never parses.** The
  coordinator runs a request's lifecycle against a decision the core produced
  (or a pinned target), enforces limits, dispatches through adapters, streams,
  cancels and accounts. Provider wire formats stop at adapter edges (D-003);
  the coordinator never parses provider payloads and never re-ranks
  candidates.
- **Adapters translate; they never policy.** Execution adapters own provider
  differences exactly like collectors do: validate schema, map errors, report
  capabilities honestly (compatibility matrix), fail closed on drift.

### Server and native-worker responsibilities

**Server component** (one process; may be containerized per M10):

- terminates all public surfaces: the OpenAI-compatible execution surface,
  the authenticated control API, the lightweight web UI (M09) and the worker
  protocol endpoint;
- holds server-side credentials under the D-044 storage decision
  (administrator-configured provider endpoints/keys, client API keys, worker
  identities);
- aggregates resource state (its own collectors plus worker reports through
  one shared normalization — never two collector implementations for the same
  provider/account, M01/M05), runs the routing core, coordinates execution,
  enforces limits and writes the bounded audit trail.

**Native worker** (one package per platform; installed only where needed):

- originates one outbound TLS/WSS connection to the configured server URL;
  no inbound listening port for normal operation;
- holds its per-device pairing credential and local adapter allowlist; local
  application credentials stay on the worker host whenever possible (Codex
  auth remains provider-managed, D-018 unchanged);
- reports safe normalized resource state, executes dispatched requests
  through allowlisted local adapters (localhost Ollama, Codex),
  streams results and usage, and enforces its allowlist even if the server
  requests more (M05);
- performs no routing decisions.

### Contract map

Every contract below is versioned, additive-first and owned by exactly one
module; the field-level serialized shapes are defined by the owning module
issue under these frozen semantic rules. Contracts never share versions
across families (D-045).

**Request contract (execution ingress — M03).** `GET /v1/models` and
`POST /v1/chat/completions` with SSE streaming, authenticated as an inference
client (D-044). Requests are validated for capability before any inference:
a request feature (tools, structured output, reasoning controls, context
size) is a compatibility requirement; unsupported capabilities are rejected
before inference or routed only to a backend that actually supports and is
authorized for them. The `model` field carries either an
administrator-defined routing-profile alias (which resolves to the existing
task/profile requirement model — never a second scoring system, D-042) or a
pinned executable-target reference. The Responses API is a later explicit
sub-scope with a documented supported subset; a fake `/v1/responses` that
silently drops semantics is forbidden.

**Route-decision contract (M02, semantics frozen by D-042).** A gateway-era
decision separates: the physical model/variant (`ModelIdentity`); the
execution channel/surface (server-direct HTTP adapter, worker-bridged
adapter, local CLI/app adapter); the entitlement in use
(subscription-included, promotional, PAYG metered, prepaid credits,
local/ungated); the confirmed quota pool(s) the entitlement draws from; and
the client routing profile under which the decision was made. The decision
carries `decision_id`, provenance, alternatives and exclusions exactly like
the existing `SelectionDecision` discipline, and its serialized extension of
selection output flows only under the machine-interface additive rules
(D-028) on the control side and under execution-surface versioning (D-045)
on the gateway side. Implemented by #87 as
`scarcity_router/routing_core.py` (`schema_version = 1`): the pure
`route_request` core layers requirement binding, authorization and
execution-surface gates on top of the unmodified balanced selector, and
`admit_pinned_target` performs the recommendation-to-execution admission
without re-ranking. The pinned executable-target reference is EXACT:
`PinnedTarget` carries the selected target's `resource_id` plus its exact
`ModelIdentity` (provider, model, variant), converted from the decision's
selected target with `PinnedTarget.from_route_target` (or
`from_route_target_dict` over the serialized decision), so no target
dimension is ever reconstructed from outside the decision. Admission
verifies the pinned identity is one of the identities the named resource
currently binds and approves exactly that resource and variant; a
no-longer-bound identity is an explicit typed rejection
(`pinned_model_not_bound`), never a substitution of another bound variant
or a canonically-first fallback.

**Resource-state contract (M01).** A new versioned contract family,
sibling to capacity v3 (which is preserved; extensions only through explicit
versioning per the D-023/D-028-compatible discipline; implemented by #86 as
`scarcity_router/resource_state.py`, `schema_version = 1`), split into
three explicit records:

- `ResourceStateSnapshot` is a **pure observation** of one executable
  resource: identity, `observed_at`, health (the capacity v3 status
  vocabulary plus its diagnostics), every quota fact as an unchanged
  `CapacityWindow` paired with its observation class
  (`direct_observation`, `provider_telemetry`, `estimate`, `local_limit`,
  `unknown` — see [`docs/capacity-model.md`](capacity-model.md)), and
  promotions as separate observations (source, observation time,
  execution-channel scope, model scope, plan scope, validity period,
  timezone). It deliberately carries no freshness/polling policy, no
  capability fields and no cost fields.
- `ResourceRegistration` is the **administrator-owned canonical
  configuration and policy**: the freshness TTL, the polling cadence, the
  configured execution-capability facts and the configured cost facts. It
  is authoritative on the server; observations — including worker reports
  — can neither redefine it nor silently override it.
- `ResourceRegistryEntry` is the **evaluated, self-contained server read
  model**: the authoritative registration state combined with the latest
  observation and the derived freshness/refresh-due state (the U-003
  resolution scope, evaluated against an explicit injectable instant with
  future-dated observations failing closed).

Across the three records, M01 as a whole owns identity, health,
freshness, cost, quota-pool and bounded polling state for every executable
resource — the ownership is split across the family, not duplicated into
the observation document. No record of this family ever contains secrets,
account identifiers or raw provider payloads.

**Execution contract (M03, semantics frozen by D-043).** Admission →
bounded concurrency reservation → dispatch → stream → completion/cancellation
→ usage accounting. Frozen rules: cancellation propagates to the selected
backend where supported; the selected target is never silently replaced,
before or after a response stream has started — a dispatch failure fails
closed with an explicit error and automatic cross-target failover does not
exist (the prompt-destination invariant of [`docs/security.md`](security.md)
holds from admission, not from the first stream byte); retries after
ambiguous execution state must not blindly duplicate inference consumption
(exactly-once is not promised);
one external request may cause multiple internal provider calls and
usage/accounting represents this honestly; client-supplied tools return to
the client as `tool_calls` and the router never executes them locally; an
execution mode with undefined or unbounded start time never silently
replaces a synchronous HTTP request.

**Worker protocol contract (M05, semantics frozen by D-043/D-044).** One
versioned message protocol over a single outbound TLS/WSS connection;
handshake performs protocol-version negotiation (incompatible versions fail
safely), per-device authentication and heartbeat; message classes cover
state reports, execute, stream chunks, cancellation and usage; reconnect is
bounded with backoff and network loss never automatically duplicates an
already-started request. No generic shell/ssh/arbitrary-command message
exists in the protocol vocabulary. Implemented as worker protocol v1 —
transport, framing, message vocabulary, pairing and reconnect semantics are
specified in [`docs/worker-protocol.md`](worker-protocol.md).

**Entitlement and quota-pool model (M01/M02, frozen by D-042).** The same
model name never implies the same resource, entitlement, quota pool, cost
model or promotional eligibility. Resources sharing one confirmed quota
pool are not independent capacity; unconfirmed sharing is never assumed in
either direction (one subscription discovered through Desktop, CLI, Windows
and WSL is not four pools, nor is pool sharing presumed without
verification). A promotion-based routing preference is distinct from proof
that an execution qualifies (D-039 remains the proof path). The router never
assumes it observes account usage happening outside it.

**OpenAI compatibility matrix (M03 with M04/M06 evidence).** Keyed by
(adapter, adapter version, model/backend) across: roles and conversation
history, streaming, `tool_calls`, tool results, structured output, reasoning
controls, context limits, error semantics, usage reporting, cancellation.
Cell values are `PASS`, `PARTIAL`, `UNSUPPORTED` or `UNKNOWN`, each with
dated evidence and tested version. Admission requires the needed
capabilities to be `PASS` or `PARTIAL` with a documented semantic mapping;
`UNKNOWN` and `UNSUPPORTED` fail closed. An agentic CLI backend is not
automatically an OpenAI-compatible backend; concatenating messages into a
text prompt is not sufficient compatibility.

**Audit-metadata contract (frozen by D-043).** Per executed request: request
id, decision id, client/profile identity, routing-policy version, state
snapshot identity/version, selected target, actually-executed target,
adapter version, start/end time, result status, provider-reported usage,
estimated usage where applicable. No prompt or response contents in the
default audit trail; bounded retention; redacted diagnostics everywhere.

### Authorization precedence

Frozen by D-042 and restated here as the routing-core's evaluation order:

```text
administrator constraints
  > client authorization
    > request requirements
      > configured routing profile
        > explicitly selected target/model
          > optimization preferences
```

Each layer may only narrow the space allowed by stronger layers. A client
override may narrow permissions and must never expand authorization,
provider access or spending limits. An explicitly requested target is pinned
and never silently replaced: if it violates a stronger layer (unauthorized,
incompatible, blocked), the request fails with an explicit error rather than
being re-routed. Administrator-configured identities, profiles/aliases and
limits are never sourced from client request content.

### Recommendation-to-execution binding

A client that first uses `select` (MCP, REST or CLI) receives a decision
carrying `decision_id` and, for executable resources, an executable-target
reference. To execute that choice through the gateway, the client pins the
target reference in the execution request (carrying the decision id for
audit provenance). The gateway then performs **admission only** —
authorization, limits, availability, compatibility — and dispatches; it does
not re-run competitive ranking, so no unexpected second routing decision
occurs. Frozen alongside (D-042): a recommendation is not automatically a
reservation, a capacity guarantee or an execution guarantee; nothing is
reserved between select and execution, and admission may still reject the
pinned target explicitly.

### Durable state

Per D-041: the server keeps one embedded durable store (SQLite-class
single-file store in its data directory) for configuration state,
identities, usage accounting and the bounded audit trail; its schema is
server-internal, migrations are explicit and tested, and no external
database, cache or message-queue service is introduced. Slices that need no
durable state may run in memory. The worker keeps only its identity file and
local configuration. Recommendation-only mode keeps today's zero-durable-
state behavior.

### Coexistence with frozen interfaces

Machine-interface v1 (loopback REST `/healthz`, `/v1/status`, `/v1/select`,
`/v1/simulate`; stdio MCP tools; CLI) is preserved with frozen semantics and
boundaries. The OpenAI-compatible execution surface is a separately
versioned contract whose `/v1/` prefix is the OpenAI client convention, not
machine-interface v1; path sets are disjoint so one listener may serve both
in server deployments. The loopback REST adapter is never converted into the
execution server (D-044/D-045). Versioning rules, error-vocabulary
separation and the extended parity requirement are
[`docs/machine-interfaces.md`](machine-interfaces.md).

### Remote bridge client (M08, #93)

The remote recommendation bridge (topology 4 above) is a configured CLIENT
mode. M08 ships the client, `scarcity_router/remote.py`; the server-side
authenticated control API it calls is owned by M03/M09 and did not exist
when the client landed. The smallest interface-side expectation is
therefore frozen in the client module and exercised against a synthetic
in-process server in the M08 guardrail suite (`make guardrails`):

- **Endpoints:** the machine-interface v1 logical contract on the
  configured server origin — `GET /v1/status`, `POST /v1/select`,
  `POST /v1/simulate`. No `/healthz`: liveness is a local-surface concern.
- **Envelopes:** exactly the machine-interface v1 envelopes (outer
  `schema_version` integer `1` plus `snapshots` / `decision` / `result`),
  so equivalent state and policy produce responses semantically equal to
  the local surfaces (the D-045 parity rule). Responses are parsed with
  the shared strict JSON parser.
- **Authentication:** `Authorization: Bearer <client API key>` — the D-044
  client identity class. The key is transient client input: header-only,
  never in a URL, never logged, redacted from the configuration repr.
- **Transport:** verified TLS for every non-loopback origin; no
  verification bypass option exists. Plain HTTP is accepted only for
  explicit loopback origins (the bounded D-044 exception). Redirects are
  never followed.
- **Failure:** every transport, authentication, status, envelope or
  validation failure raises the typed `RemoteBridgeError`. The client
  holds no collectors, artifacts or policy, so a silent fallback to local
  state or local policy is structurally impossible. Structural request
  violations are rejected client-side through the same shared
  `machine_api` parsers the frozen adapters use; catalog-dependent
  validation (for example an unknown profile id) belongs to the remote
  server and surfaces as an explicit error.

If M03/M09 land different control-API paths or envelope versioning, the
reconciliation is confined to `scarcity_router/remote.py` (endpoint
constants plus envelope validation) and the synthetic-server tests.

### Execution sources, model inventory and dynamic resources (D-053, #116)

The execution-source layer sits between administrator configuration and the
existing routing core; it changes nothing about how a decision is made, only
where executable resources come from:

```text
ExecutionSource (administrator configuration, per-source identity)
      |  worker-run discovery on the source's own controlled runtime
      v
ModelInventory (typed, bounded, versioned worker-protocol state)
      |  classification against the reviewed track registry
      v
adoption policy (DISCOVERED -> CLASSIFIED -> ROUTABLE, conservative floors)
      v
derived exact resources (deterministic per-source materialization)
      v
existing Routing Core (unchanged) -> exact pinned execution target
```

Contracts:

- **ExecutionSource.** One configured, independently authenticated source of
  executable model capacity — `source_id`, `kind` (`codex_subscription`
  first), `label`, owning `worker_id`, `entitlement`, optional explicit
  `quota_pool_id`, adoption policy. Lives in the additive `sources` domain of
  the administrator configuration document (config schema version 2; a v1
  document remains valid). The user configures the source; the source
  discovers models; physical models remain the exact execution targets.
- **ModelInventory.** The worker-protocol v2 state report carries an
  optional bounded `inventories` section: per source — `source_id`,
  `adapter_id` (`codex:<source_id>`), `observed_at`, closed-vocabulary auth
  state, runtime name/version, and per-model slug plus runtime-reported
  reasoning efforts. Bounded entries, no credentials, no account metadata,
  no raw provider payloads. A v1 peer pair behaves exactly as before.
- **Adapter instances.** Adapter KIND (`codex`) is separate from adapter
  INSTANCE (`codex:<source_id>`); one worker process serves any number of
  sources, each with an isolated controlled CODEX home
  (`state_dir/codex-sources/<source_id>/`), auth state, inventory and
  execution identity. No credential or home is shared between sources.
- **Tracks and adoption.** `model-tracks.json` (reviewed, versioned) maps
  slug structure to stable capability families (`openai/luna`,
  `openai/sol`, `openai/astra`, restricted `daybreak`). A discovered model
  becomes routable only through the conservative floor policy: known track,
  safely parsed naming, approved track capability floor, runtime-advertised
  effort, source/auth/health gates, compatibility evidence. Unclassified
  models stay discovered/not-routable; restricted tracks (Daybreak Blue) are
  never materialized as routable resources.
- **Derived resources.** Deterministic per-source materialization
  (`resource_id = <source_id>:<slug>`), exact slug and efforts,
  source-owned lifecycle: absence from an authenticated inventory →
  `unavailable`; absent for three consecutive authenticated inventories →
  retired (audit history and pins remain interpretable; reappearance
  re-materializes). Derived registrations are in-memory derived state — the
  source, not a second durable store, is their origin. D-049's ownership
  clause is amended at source granularity: the administrator grants
  ownership by configuring the source; inventory expands what that grant
  covers, subject to adoption policy.

### Migration plan

- No rewrite: the existing package, language and module structure are
  preserved; gateway and worker code are new modules beside the application
  layer (see the file map below).
- Machine-interface v1 changes are additive-only (D-028); the execution
  surface starts at its own v1 and evolves additively within major versions
  (D-045); the worker protocol negotiates versions independently (D-043).
- Capacity contract v3 is preserved; executable-resource state extends
  through the new resource-state contract (M01), not through a forced v4.
- Catalog/policy artifacts evolve additively per the D-032 discipline.
- Recommendation-only users migrate nothing: zero-configuration continuity
  is an M08/M10 acceptance criterion, not an aspiration.

### Existing files and modules

Mapping of the current tree to module ownership (no new directory structure
is imposed by A0; new modules land beside these as their issues require):

| Existing file/module | Primary owner going forward |
| --- | --- |
| `capacity.py`, `eligibility.py`, `status.py`, `resource_state.py` (M01 resource-state contract, added by #86), `providers/*` (collectors) | M01 (+#86); execution adapters in `providers/` per M04/M06 |
| `selector.py`, `policy.py`, `scarcity.py`, `simulation.py`, `selection_types.py`, `selection_app.py`, `routing_core.py` (M02 route-decision contract, added by #87) | M02 (+#87) |
| `server.py` | Stays the frozen loopback REST v1 adapter (M08 guard); never becomes the execution server |
| `machine_api.py` | M08 (+#93); gateway request parsing is new M03-owned code, not a v1 change |
| `mcp.py`, `cli.py` | M08 (+#93); `doctor` realization and config UX extend via M09 |
| `config.py` | M09 (+#94) |
| `model-catalog.json`, `model-policy.json`, `examples/*` | M01/M02/M09 additive evolution |
| New: gateway/coordinator modules, worker package, server control API/UI | M03, M05, M09 respectively |

## Deferred architecture

Runtime failure feedback, central team quota pools, multi-user policy,
signed catalog releases and commercial services remain future possibilities,
not foundations of the current service. Persistent server-side state beyond
D-041's minimal store, and any multi-user authorization model, require new
explicit decisions.
