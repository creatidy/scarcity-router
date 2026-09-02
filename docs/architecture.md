# Architecture

## Context

Scarcity Router is a local decision service between capacity sources, curated data
and clients. It does not sit on the model request path.

```text
provider/local telemetry ──> collectors ──> normalized capacity
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

## Components

### Collectors

Small read-only provider adapters acquire telemetry and translate it into the
normalized capacity model. They own provider discovery, subprocess/protocol
handling, response validation and provider-specific error mapping. They do not
rank models or interpret task difficulty.

Provider drift must stop at this boundary. A broken adapter yields an explicit
status such as `schema_changed` while the rest of the service remains healthy.

### Capacity store/view

Holds the most recent v1 normalized snapshots and their retrieval timestamps.
Freshness evaluation, refresh behavior and durable history are separate concerns;
the initial implementation may be in-memory and refreshed on demand. Secrets and
raw Authorization values never enter the normalized state.

### Catalog

Contains model identities, provider/account relationships, hard properties and
curated multidimensional capabilities. Profiles map stable task names to
requirements. Catalog data is versioned and human-reviewable; it does not read
runtime quota.

### Policy

Contains default scarcity behavior, reservation rules, preference modes and
explicit overrides. Runtime policy is separate from repository governance and
from the catalog, so a user can change today's preference without changing
source repositories or capability claims.

### Selector

Consumes only normalized capacity, catalog data, task requirements and policy.
It has no credential access, no provider parsing and no client-specific code.
It produces a deterministic decision object with the selected model, ranked
alternatives, exclusions, input provenance and reasons.

### Interfaces

- **CLI** is the first operational interface (`doctor`, `status`, then
  `select --explain` and `simulate`).
- **REST** becomes the canonical language-neutral machine contract. It binds to
  `127.0.0.1` by default. Target endpoints include `/healthz`, `/v1/status`,
  `/v1/providers`, `/v1/providers/{provider}`, `/v1/select` and `/v1/simulate`.
- **MCP** is a thin adapter with tools such as `get_capacity_status`,
  `select_model` and `simulate_selection`. Local stdio is preferred initially.
- **Dashboard** is a small operational view after core contracts exist, not a
  separate frontend product.

No interface owns selection or collector business logic.

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

## Domain contracts

M1 freezes `docs/capacity-model.md` as the v1 serialized capacity contract:

- `CapacitySnapshot`: schema version, provider/source identifiers, optional safe
  plan, retrieval time, status, windows, optional local runtime and safe
  diagnostics. Account identifiers and freshness fields are not in v1.
- `CapacityWindow`: validated resource and period kind, optional duration,
  complementary used/remaining percentage pair, optional reset time and an
  allowlisted opaque provider window identifier.
- `ModelProfile`: stable model identity, variants, provider, hard properties,
  capability assessments and provenance.
- `TaskRequirement`: level, capability minima, hard constraints and optional
  policy hints.
- `SelectionDecision`: selected candidate, ranked alternatives, exclusions,
  reasons, capacity/catalog versions and degraded/unknown indicators.

Schemas must distinguish omitted, unknown, unsupported, unavailable and zero. The
capacity contract uses omitted optional fields for unknown values and explicit
`unknown` enum values for unresolved window/runtime semantics.

## Configuration boundaries

Separate configuration namespaces are expected:

- static catalog and profile data distributed with the project;
- user policy and temporary preferences;
- collector discovery/configuration without copied secrets;
- service settings such as refresh interval and local bind address.

Do not place secrets in ordinary configuration. Do not make users edit
`AGENTS.md`, repository policy or the capability catalog for daily choices.

## Change isolation and reliability

Provider adapters are expected to change more frequently than the normalized
domain. Interface adapters may evolve independently. Public serialized
contracts require explicit versioning once released.

Partial failure is normal: one provider may be unknown while others remain
usable. The selector never fabricates telemetry to hide that failure and the
explanation must identify degraded inputs.

## Deferred architecture

Persistent history, runtime failure feedback, central team quota pools,
multi-user policy, signed catalog releases and commercial services are future
possibilities, not foundations to build in M1.
