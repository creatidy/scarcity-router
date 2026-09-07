# Architecture

## Context

Scarcity Router is a local decision service between capacity sources, curated data
and clients. It does not sit on the model request path.

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

## Components

### Collectors

Small provider adapters acquire telemetry and translate it into the normalized
capacity model. Acquisition is read-only except for the single bounded
provider-managed OpenAI auth-recovery exception of `docs/decisions.md` D-018. They own provider discovery, subprocess/protocol
handling, response validation and provider-specific error mapping. They do not
rank models or interpret task difficulty.

The M1 collector set is exactly OpenAI/Codex and Z.ai Coding Plan. Adding a
later provider requires separate scope and does not change the selector's
provider-independent boundary.

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

### Policy

Contains default scarcity behavior, reservation rules, preference modes and
explicit overrides. Runtime policy is separate from repository governance and
from the catalog, so a user can change today's preference without changing
source repositories or capability claims. The resource-state and preservation
primitives are implemented in M2d (D-026) as the pure modules
`scarcity_router/scarcity.py` (continuous scarcity penalty, explanatory
labels, binding applicability, most-restrictive aggregation,
known/unknown/unavailable assessments) and `scarcity_router/policy.py`
(unknown-capacity modes, scope-targeted reservations, timezone-aware
blackouts, `ReplenishmentState` visibility modes, the `UserPolicy`
container); ranking modes remain the M2e selector's concern.

### Selector

Consumes only normalized capacity, catalog data, task requirements and policy.
It has no credential access, no provider parsing and no client-specific code.
It produces a deterministic decision object with the selected model, ranked
alternatives, exclusions, input provenance and reasons.

**Implemented in M2e (D-027)** as the pure modules
`scarcity_router/selector.py` (the `balanced` candidate pipeline, the exact
ranking order, `SelectorPolicy` and the monotone `tighten_requirement`
merge) and `scarcity_router/simulation.py` (typed overrides applied to
copies of the inputs, re-running the SAME `select_model` core for the
baseline and simulated decisions). The application layer
`scarcity_router/selection_app.py` loads artifacts strictly, resolves the
requirement, obtains one shared evaluation instant through
`collect_status` and renders deterministic output; `scarcity_router/cli.py`
dispatches `status`, `select` and `simulate`.

### Interfaces

- **CLI** is the first operational interface. The provisional read-only
  `status` surface is implemented, and M2e adds `select` (with `--explain`
  and `--json`) and `simulate` through the same dispatcher; a separate
  `doctor` command remains deferred.
- **REST** becomes the canonical language-neutral machine contract. It binds
  to `127.0.0.1` by default. The M3a planning gate (D-028) froze the surface
  to exactly `/healthz`, `/v1/status`, `/v1/select` and `/v1/simulate`;
  `/v1/providers` and `/v1/providers/{provider}` are deferred because
  `/v1/status` already returns the full snapshot set. **Implemented in M3b**
  (D-030) as the loopback-only standard-library adapter
  `scarcity_router/server.py` (`python -m scarcity_router.server`,
  default port 8765, single-threaded serialized requests, no runtime
  dependency), calling the same typed application seam
  (`select_from_inputs` / `simulate_from_inputs`) as the CLI runners.
- **MCP** is a thin adapter. M3a froze three tools — `scarcity_status`,
  `scarcity_select` and `scarcity_simulate` — over local stdio, calling the
  application layer directly in-process and never requiring the REST server.
  **Implemented in M3c (D-031)** in `scarcity_router/mcp.py` with the official
  MCP SDK v2 low-level `Server` API, structured tool results and the client-owned
  stdio lifecycle. It shares the transport-neutral dependency record in
  `selection_app.py`, the logical parser/envelopes in
  `scarcity_router/machine_api.py`,
  and the same typed application seam; it has no REST runtime dependency.
- **Dashboard** is a small operational view after core contracts exist, not a
  separate frontend product.

The authoritative machine-interface contract — envelopes, error semantics,
side-effect wording, security/lifecycle boundary, versioning and the
CLI/REST/MCP parity requirement — is [`docs/machine-interfaces.md`](machine-interfaces.md)
(D-028). No interface owns selection or collector business logic.

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

The current normalized capacity contract is v3, frozen in
`docs/capacity-model.md` (M2a, D-023):

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
  capability assessments and provenance. M2 planning (D-020) adds explicit
  capacity bindings to one or more capacity scopes and freezes the
  provenance/human-override record shape. **Implemented in M2b** as
  `ModelCatalogEntry` plus `ModelIdentity`, `ModelHardProperties`,
  `EvidenceRef`, `HumanOverride` and the `CapabilityAssessment` vector in
  `scarcity_router/selection_types.py` (D-024): unknown capability is the
  explicit `{"rating": null}` state, a known rating requires complete
  provenance, overrides preserve the source assessment, and
  `capacity_bindings` distinguishes unknown applicability (`None`) from a
  known non-empty set of exact `(provider, scope_id)` references.
- `TaskRequirement`: task level, per-dimension capability minima on the frozen
  `1..5` scale with explicit unknown representation, typed hard constraints and
  single-place profile expansion (D-020). **Implemented in M2b** with the
  stored three-part shape (`task_level`, `capability_minima`,
  `hard_constraints`); profile expansion is a construction pathway owned by
  M2c, not a serialized fourth field (D-024).
- `SelectionDecision`: selected candidate, ranked alternatives, exclusions,
  reasons, capacity/catalog versions and degraded/unknown indicators.
  **Implemented in M2e** in `scarcity_router/selector.py` (D-027) together
  with the per-candidate `CandidateEvaluation` record: one primary
  exclusion stage from the closed vocabulary (`policy_blackout`,
  `hard_constraint`, `capability`, `capacity`, `reservation`), structured
  hard/capability failures, normalized reason codes, deterministic
  closest-candidate and recoverable-candidate lists for no-solution
  results, and serialize-only `to_dict()` output.
- `ScarcityAssessment`: the per-candidate scarcity result over explicit
  capacity bindings — continuous integer penalty, explanatory label,
  explicit `known`/`unknown`/`unavailable` state and normalized reason
  codes. **Implemented in M2d** in `scarcity_router/scarcity.py` (D-026):
  the most restrictive applicable window governs, unrelated scopes are
  ignored, explicit exhaustion (`remaining_percent == 0`) wins over
  incomplete telemetry, and unknown carries no numeric penalty.

M2 planning additionally freezes these not-yet-implemented conceptual
contracts:

- `CapacityScope`: `(provider, scope_id)` identity for one normalized capacity
  scope. The identity now exists in the contract via
  `CapacityWindow.scope_id` (D-023), and M2b adds the core `CapacityScopeRef`
  binding type; binding real catalog models to real scopes remains M2c work.
- `ReplenishmentState`: minimal safe reset-credit facts — replenishment
  opportunities, never current capacity (D-021). **Implemented in M2d** in
  `scarcity_router/policy.py` (D-026) with the `ignore`/`advisory`/
  `recoverable` visibility modes; provider-side wiring into selector input
  assembly belongs to M2e.
- `ExecutionBudget`: the finite bounded envelope that must accompany any
  compound recommendation (D-022).

Schemas must distinguish omitted, unknown, unsupported, unavailable and zero. The
capacity contract uses omitted optional fields for unknown values and explicit
`unknown` enum values for unresolved window semantics.

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
