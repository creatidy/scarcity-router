# Machine Interfaces (REST and MCP)

This document is the authoritative M3 contract for the machine interfaces. It
freezes the REST surface, the MCP tool surface, their shared semantics, error
behavior, versioning, security boundary and parity requirement **before** any
transport is implemented (M3a planning gate, decision D-028). Nothing here
changes selection, scarcity, provider or capacity semantics; those remain
owned by their existing authoritative documents.

- **Status:** Frozen contract (M3a). REST v1 is implemented (M3b, D-029);
  MCP is not yet implemented (M3c pending).
- **Implementation:** M3b (minimal local REST adapter) and M3c (thin stdio
  MCP adapter + parity tests). See `docs/roadmap.md` for the frozen sequence.
- **Scope guard:** M3a froze this document only. No REST or MCP runtime, no
  framework dependency and no product source change belongs to it.

## Principles

1. **Recommend, do not proxy.** No interface accepts prompts, model traffic,
   provider credentials or arbitrary provider endpoints (D-001, D-009).
2. **One authoritative application path.** CLI, REST and MCP are transport
   adapters over the SAME application/core. They do not own selection,
   scarcity, provider parsing, simulation or policy semantics (D-007).
3. **Explicit over magical.** Every envelope, error code and version boundary
   in this document is frozen here, never chosen implicitly by an
   implementation framework.
4. **Unknown and degraded states stay data.** Provider operational states are
   normalized domain data in successful responses, not transport failures.
5. **Machine structure first.** Structured payloads are the contract;
   human-readable text is at most an additional, non-primary field.

## Shared application/core boundary

There is exactly one application/core path:

```text
CLI ───────┐
REST ──────┼─> application/core (selection_app + pure core)
MCP stdio ─┘
```

Transport adapters do only:

```text
parse transport input → call application/core → serialize existing typed result
```

M3 must not create a REST selector, an MCP selector, REST-only simulation
logic or MCP-only fallback logic. The serialized domain contracts are reused,
never forked:

- `CapacitySnapshot.to_dict()` — capacity contract v3
  (`docs/capacity-model.md`);
- `TaskRequirement`, `SelectorPolicy`, `ReplenishmentState` — selector inputs
  (`docs/selection-policy.md`, D-024/D-026/D-027);
- `SelectionDecision.to_dict()` and `SimulationResult.to_dict()` — outputs
  (D-027).

The application layer resolves artifacts (`model-catalog.json`,
`model-policy.json`) from its configured defaults, exactly as the CLI does
today. **Artifacts are process configuration, not per-request inputs:** REST
and MCP clients never supply catalog/model-policy file paths, and never
supply provider endpoints or credentials. Per-request inputs are exactly the
requirement source (profile id or explicit `TaskRequirement`), the optional
tightening, the optional `SelectorPolicy`, the optional replenishment
observations and, for simulation, the typed `SimulationOverrides`.

There is no client-supplied evaluation instant for `status`/`select`; each
invocation obtains one instant from the server clock and shares it across
blackout evaluation and capacity collection, exactly like the CLI. Only the
existing typed `SimulationOverrides.evaluated_at` may move the simulated
instant of a simulation.

## REST v1

The REST path prefix `/v1/` is the machine-interface major version. The
outer REST envelope version is **not** the capacity contract version and
**not** the catalog or policy version (see [Versioning](#versioning)).

Frozen surface — exactly four operations:

```text
GET  /healthz
GET  /v1/status
POST /v1/select
POST /v1/simulate
```

`/v1/providers` and `/v1/providers/{provider}` from earlier roadmap prose are
**deferred**: `/v1/status` already returns the full normalized snapshot set
(exactly two providers per D-017), and per-provider filtering is a trivial
client-side selection over the returned array. No current value justifies
them; do not implement them automatically.

Requests and responses are `application/json`. Unknown request keys are
rejected as invalid requests. The snapshot arrays use the same canonical
ordering as the CLI (`openai` then `zai`).

### Request parsing and strictness

REST request bodies are parsed with the same deterministic strictness the
application already applies at its artifact boundaries (duplicate keys and
non-finite constants fail there). Framework JSON defaults must not decide
this implicitly. For every REST request body, frozen:

```text
duplicate JSON object keys -> invalid_request (HTTP 400)
NaN                        -> invalid_request (HTTP 400)
Infinity                   -> invalid_request (HTTP 400)
-Infinity                  -> invalid_request (HTTP 400)
```

MCP input validation mirrors these semantics logically through the same
`invalid_request` error payload (see [MCP error semantics](#mcp-error-semantics)).

### GET /healthz

Process/service liveness only. It must not trigger provider telemetry
collection, must not imply providers are healthy, must not perform model
calls, and its response is intentionally tiny:

```json
{
  "status": "ok"
}
```

`/healthz` is never extended into a provider diagnostic endpoint.

### GET /v1/status

Uses the same normalized capacity collection/application semantics as CLI
`status` — one fresh sequential collection per request, one shared
observation timestamp, no cache. The response is the existing normalized
CapacitySnapshot v3 documents inside one small versioned envelope; there is
no REST-specific capacity representation.

```json
{
  "schema_version": 1,
  "snapshots": [
    { "...existing CapacitySnapshot v3 to_dict()..." }
  ]
}
```

The outer envelope field `schema_version` is the machine-interface envelope
version (integer `1` in v1). The `schema_version: 3` inside each snapshot is
the capacity contract version (D-023). They are separate concepts and are
never collapsed.

Provider operational states — `unavailable`, `auth_required`,
`unsupported`, `schema_changed`, `unknown`, exhausted windows — are DATA:
they normally still produce a successful HTTP 200 `/v1/status` response
containing the normalized provider state. Ordinary provider telemetry
degradation is never mapped to HTTP 500 (or any other transport error).

### POST /v1/select

One typed JSON request shape, mirroring the application inputs rather than
CLI flag names:

```json
{
  "profile_id": "deep_coding",
  "requirement": null,
  "tightening": null,
  "selector_policy": null,
  "replenishment_states": []
}
```

| Field | Type | Semantics |
| --- | --- | --- |
| `profile_id` | string, optional | Calibrated task profile id resolved through the profile catalog. |
| `requirement` | TaskRequirement object, optional | Explicit full stored `TaskRequirement` (serialized contract). |
| `tightening` | TaskRequirement object, optional | Full `TaskRequirement` that monotonically tightens the profile. |
| `selector_policy` | SelectorPolicy object, optional | Serialized `SelectorPolicy`; missing or `null` means the documented neutral policy. |
| `replenishment_states` | array of ReplenishmentState, optional | Normalized replenishment observations; missing or `[]` means none; explicit `null` is `invalid_request`. |

The same exclusivity as the CLI is enforced — there is no alternative
simplified requirement model:

```text
profile_id XOR requirement
tightening only with profile_id
```

Supplying both `profile_id` and `requirement`, or `tightening` without
`profile_id`, is an invalid request (HTTP 400).

Missing and explicit-`null` field semantics are frozen exactly — there is no
other implicit mapping:

| Field | missing | explicit `null` |
| --- | --- | --- |
| `profile_id` | absent | absent |
| `requirement` | absent | absent |
| `tightening` | absent | absent |
| `selector_policy` | neutral/default policy | neutral/default policy |
| `replenishment_states` | no observations | `invalid_request` |

`replenishment_states: null` is an `invalid_request`: the field is an array
at this boundary and ordinary select input has no baseline-replacement
tri-state to represent (that tri-state exists only in the nested simulation
`overrides`; see below).

Exactly one effective requirement source is required — effective
`profile_id` XOR effective `requirement`, after the missing/`null` mapping
above. Both absent/null and both present are `invalid_request`.

An unknown `profile_id` — one that does not resolve in the configured
profile catalog — is an `invalid_request` (HTTP 400): a client input error,
not an internal error and not a no-solution.

The response is the existing `SelectionDecision` inside the versioned
envelope:

```json
{
  "schema_version": 1,
  "decision": { "...existing SelectionDecision.to_dict()..." }
}
```

Reason codes are never translated, provenance is never removed and the result
is never reduced to a model identifier.

### POST /v1/simulate

The request reuses the exact `/v1/select` request shape plus the typed
simulation overrides:

```json
{
  "profile_id": "routine_coding",
  "overrides": {
    "capacity_percentages": [
      {
        "provider": "zai",
        "scope_id": "coding_plan",
        "resource": "tokens",
        "kind": "weekly",
        "remaining_percent": 2
      }
    ],
    "selector_policy": null,
    "replenishment_states": null,
    "evaluated_at": null
  }
}
```

`overrides` is a required key whose value is the serialized
`SimulationOverrides` contract; `{}` (all fields absent) is legal and means
"no overrides". The tri-state semantics of
`replenishment_states`/`selector_policy`/`evaluated_at` inside overrides are
the existing typed ones (D-027): absent/`null` retains the baseline input.
The nested `overrides.replenishment_states` deliberately keeps this
`SimulationOverrides` tri-state and is not conflated with the top-level
select semantics: missing/`null` retains the baseline observations, `[]`
replaces them with no observations, and a non-empty array fully replaces
them. Only `overrides.evaluated_at` can move the simulated instant; there is
no other client-supplied clock.

The response reuses the existing `SimulationResult` — baseline and simulated
decisions are both produced by the same selector core; REST never creates a
second simulation implementation:

```json
{
  "schema_version": 1,
  "result": {
    "baseline": { "...SelectionDecision..." },
    "simulated": { "...SelectionDecision..." },
    "applied_overrides": { "...SimulationOverrides..." }
  }
}
```

### Error semantics

Exactly three result classes are distinguished, and the distinction is frozen
here rather than chosen by an implementation framework:

**1. Valid domain result — HTTP 200.** A legitimate no-solution is NOT an
HTTP error. When no candidate is eligible, the response is:

```text
HTTP 200
SelectionDecision.selected = null
reason_codes includes no_eligible_candidate
```

HTTP 404, 409, 422 and 500 are never used for a valid no-solution.

**2. Invalid client request — HTTP 400.** Malformed JSON, a non-JSON content
type on a POST, duplicate JSON object keys, `NaN`/`Infinity`/`-Infinity`
constants, unknown keys, a schema-invalid `TaskRequirement`,
`SelectorPolicy`, `ReplenishmentState` or `SimulationOverrides`, the
requirement-source violation (profile and requirement both present, or both
absent/null), an unknown `profile_id`, `replenishment_states: null`,
tightening with an explicit requirement, or an invalid simulation override
all return:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "safe structural message only"
  }
}
```

HTTP 400 (not 422) is frozen: the existing CLI treats every invalid
input/config case as one failure class, and no repository evidence justifies
a second validation-specific status. The error message is a safe structural
message only — never a traceback, never a raw provider payload, never
credentials, never local credential paths.

**3. Application/internal failure — HTTP 500.** A failure of the application
itself (for example artifact loading failure or an unexpected internal
exception) returns the same envelope with the code `internal_error` and a
safe message. Exception internals are never exposed. The M3 error-code
vocabulary is closed: exactly `invalid_request` and `internal_error`.

**4. Transport routing responses.** An unknown path (404) or unsupported
method (405) is transport-level routing with no domain meaning; its body is
unspecified but must not leak internals. These statuses carry no domain
semantics and are never produced for a valid request.

**Provider telemetry degradation is not a REST error.** Provider telemetry
failures represented by existing CapacitySnapshot status remain domain data
in successful responses; they never become transport errors. In particular a
degraded provider during `select` is handled by the existing
unknown/degraded selection policy, never mapped to HTTP 503.

## Side-effect semantics

The exact wording is frozen (the operations are **not** called
side-effect-free):

> Selection issues no model prompt and does not intentionally consume
> inference quota. Capacity collection uses the existing telemetry path and
> may exercise the bounded provider-managed authentication recovery already
> accepted in D-018.

`status`/`select`/`simulate` may trigger the existing capacity collectors and
therefore the bounded D-018 recovery. They never: execute model inference,
consume reset credits, redeem replenishment, write provider configuration or
dispatch selected models.

## MCP

A minimal stdio MCP adapter exposing exactly three tools:

```text
scarcity_status
scarcity_select
scarcity_simulate
```

Not exposed: one tool per provider, internal scarcity helper functions, reset
redemption or any execution capability. Tool names carry no version suffix
(no `scarcity_select_v1`); the tool descriptions state that they expose the
M3 machine-interface contract v1 (the same logical `/v1` contract).

### Transport and architecture

Preferred and frozen transport: **stdio**. The MCP adapter calls the
application layer **directly, in-process** — the same Python
application/core the CLI uses. It does **not** require or invoke the local
REST server:

```text
CLI ───────┐
REST ──────┼─> application/core
MCP stdio ─┘
```

`MCP → REST → application` is rejected for M3: it would add a runtime
dependency and a local server lifecycle requirement for MCP clients, while
the exact same application/core is already available in-process and parity is
easier to test directly. No repository evidence shows a concrete benefit that
would justify the indirection.

### Tool inputs

- `scarcity_status` — no provider/model arguments for v1. Returns the same
  normalized status content as `GET /v1/status`.
- `scarcity_select` — input structurally mirrors the `POST /v1/select`
  request body exactly (same fields, same exclusivity rules). No
  MCP-specific shorthand is invented.
- `scarcity_simulate` — input structurally mirrors the `POST /v1/simulate`
  request body exactly (`select` input plus `overrides`).

### Tool outputs

Tools return structured machine objects corresponding to the domain
contracts — the same JSON payloads as the REST envelopes:

- `scarcity_status` → `{ "schema_version": 1, "snapshots": [...] }`
- `scarcity_select` → `{ "schema_version": 1, "decision": {...} }`
- `scarcity_simulate` → `{ "schema_version": 1, "result": {...} }`

Prose is never the primary result. A human-readable explanation may be added
as an optional additional text field only if it does not replace the
structured payload. Where the eventual MCP SDK supports typed structured
content, the same JSON payload is the contract; the SDK encoding choice
belongs to M3c.

### MCP error semantics

The **logical** structured error payloads are frozen now; SDK-specific wire
encoding is not. The eventual official SDK may carry these payloads through
its supported tool-error mechanism (`isError`, structured content or
equivalent), but M3c must preserve this logical payload and the closed
error-code vocabulary:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "safe structural message"
  }
}
```

```json
{
  "error": {
    "code": "internal_error",
    "message": "safe structural message"
  }
}
```

- A contract-invalid input (the HTTP 400 class, including strict-parsing
  failures — duplicate keys, `NaN`/`Infinity`/`-Infinity` — missing/`null`
  violations, the requirement-source violation, an unknown `profile_id`, or
  an invalid override) is a tool **input error** carrying the logical
  `invalid_request` payload. It is never converted into a fabricated
  `SelectionDecision`.
- An application failure carries the logical `internal_error` payload.
- A valid no-solution and a degraded provider remain **successful**
  structured tool results (the same payloads as their REST HTTP 200
  counterparts) and never use the error shape.
- No additional error codes are invented.

## Parity

Parity is an explicit acceptance requirement, not an aspiration. Equivalent
logical inputs must produce equivalent core results through CLI, REST and
MCP. Transport wrapping may differ; the business result must not.

For deterministic injected inputs (fixed collectors and clock), after
unwrapping each transport envelope:

```text
REST SelectionDecision == MCP SelectionDecision == direct application/core SelectionDecision
REST SimulationResult  == MCP SimulationResult  == direct application/core SimulationResult
```

Equality is typed/domain equality of the unwrapped contracts, not byte
equality of transport envelopes. CLI `--json` output remains semantically
equivalent to the same core result: the CLI emits the bare
`SelectionDecision`/`SimulationResult`/snapshot-array documents without the
REST envelope, and that released M1/M2e CLI behavior is preserved unchanged.
M3 closeout must prove `direct application == CLI JSON == REST == MCP` for
representative deterministic scenarios (`docs/roadmap.md`).

## Security and lifecycle

- **Local machine interface.** The default REST bind address is `127.0.0.1`.
  There is no `0.0.0.0` default, no LAN exposure default and no remote
  multi-user deployment assumption. Any non-loopback exposure requires an
  explicit future security decision and threat analysis
  (`docs/security.md`).
- **No authentication layer in M3.** This is acceptable only because the
  server binds to loopback by default. M3 REST is a local machine interface,
  not an internet-facing service. OAuth, API keys, sessions, reverse-proxy
  auth and TLS termination are out of scope; M3b/M3c must not add
  non-loopback bind options.
- **Credentials are never interface data.** REST and MCP never accept
  provider credentials from clients, never return provider credentials, never
  accept arbitrary provider endpoints and never proxy model prompts. Clients
  are untrusted with respect to secrets and receive only normalized safe
  output (`docs/security.md`).
- **REST runtime scope.** A single local process; no daemon manager, no
  background cache, no database, no persistent history, no scheduler. Each
  request may collect current provider telemetry through the existing
  application path. Request caching is not invented in M3.
- **MCP runtime scope.** The stdio process lifecycle is owned by the MCP
  client. No daemon and no shared cache between REST and MCP.
- **Side effects.** As frozen in [Side-effect semantics](#side-effect-semantics).

## Versioning

Separate contracts carry separate versions; they are never collapsed:

| Version | Meaning | Value |
| --- | --- | --- |
| `CapacitySnapshot.schema_version` | Capacity contract (`docs/capacity-model.md`) | `3` |
| REST path prefix `/v1/` + envelope `schema_version` | Machine-interface contract (this document) | `1` |
| `catalog_version` | Model catalog content version | `1` (current artifact) |
| `policy_version` | Model policy/profile content version | `5` (current artifact) |

The machine-interface contract includes **both its envelope and the
serialized domain documents exposed inside it**. `CapacitySnapshot`,
`TaskRequirement`, `SelectorPolicy`, `ReplenishmentState`,
`SelectionDecision`, `SimulationResult` and `SimulationOverrides` are all
part of the v1 wire contract. Within machine-interface v1:

- additive backwards-compatible domain fields may flow through v1 as long as
  existing v1 clients remain valid;
- an incompatible removal, rename, type change or semantic change in **any**
  exposed nested domain contract is an incompatible machine-interface
  change, and requires either:
  1. a compatibility serializer preserving the v1 wire contract, or
  2. a new machine-interface major version;
- changing only a domain's internal version number does not by itself
  require a new machine-interface version when its serialized v1-visible
  shape remains backwards compatible (a future capacity v4 that adds only
  optional fields would flow through v1; one that removes or retypes a v1
  field would not);
- existing domain serialization is reused rather than forked.

M3a creates no compatibility serializers; choosing between the two options
above for a concrete incompatible nested-domain evolution is a future
explicit decision, never an implementation accident.

MCP exposes the same v1 logical contract under simple, unversioned tool
names, with the version stated in the tool documentation.

## Concurrency

The collectors and application are deliberately synchronous and
deterministic. The frozen semantic is:

```text
one request → one application invocation → existing deterministic synchronous provider collection
```

No parallel provider collection semantics are introduced through REST/MCP.
A transport framework may technically admit concurrent requests later, but
core/provider concurrency must never be invented implicitly: if concurrent
service requests create lifecycle or resource concerns, the implementation
must serialize or bound them explicitly. This is an M3b/M3c implementation
concern, not a redesign of M2.

## Design scenarios

Expected machine-interface semantics for the representative scenarios
(contract expectations for M3b/M3c tests; deterministic fixtures, never live
quota):

1. **Status with one degraded provider.** One provider `status = unknown`,
   the other `status = ok`. REST: HTTP 200 with both snapshots in the
   envelope. MCP: a successful structured tool result containing both
   snapshots. Degradation is data, not an error.
2. **No eligible model.** REST: HTTP 200 with
   `decision.selected = null` and `no_eligible_candidate` in
   `reason_codes`. MCP: a successful tool result with the same
   `SelectionDecision`.
3. **Invalid TaskRequirement.** REST: HTTP 400 with the safe
   `invalid_request` envelope. MCP: a structured protocol-level invalid-input
   tool result — never a fabricated `SelectionDecision`.
4. **Provider unavailable during select.** The provider state remains
   normalized domain data; the selector handles it with the existing
   unknown/degraded policy. It is never mapped directly to HTTP 503.
5. **Simulation 98/2.** The weekly-critical override
   (`remaining_percent = 2` on the Z.ai weekly window): REST and MCP both
   return the same `SimulationResult` as a direct core call with the same
   injected inputs.
6. **Scientific-review no-solution.** If Sol is unavailable and no
   sufficient candidate remains, the HTTP call and the MCP call still
   succeed with a valid no-solution `SelectionDecision`.
7. **Duplicate JSON object key in a request.** REST: HTTP 400
   `invalid_request` — strict parsing is frozen, never framework-default
   lenient parsing (`NaN`/`Infinity`/`-Infinity` constants are the same
   class). MCP: the logical `invalid_request` input error.
8. **Both or neither requirement source.** `profile_id` and `requirement`
   both present, or both absent/null, is HTTP 400 `invalid_request` — never
   a guessed default requirement.
9. **Unknown profile id.** A `profile_id` that does not resolve in the
   configured profile catalog is HTTP 400 `invalid_request` — a client
   input error, not an internal error and not a no-solution.
10. **MCP invalid input.** Carries the logical `invalid_request` error
    payload through the eventual SDK tool-error mechanism — never a
    fabricated `SelectionDecision`.
11. **MCP internal failure.** Carries the logical `internal_error` error
    payload.

## Non-goals

M3a freezes contracts only. The following are explicitly out of scope for M3a
and, unless a document says otherwise, for M3 as a whole: FastAPI or any
specific framework commitment, HTTP listener runtime, Uvicorn, MCP server
runtime, MCP SDK dependency, network sockets in tests, authentication, TLS,
Docker, systemd, Windows services, databases, caches, history, dashboards,
model execution, prompt proxying, automatic dispatch, reset-credit
acquisition or redemption, provider health integration, Artificial Analysis
runtime integration, new providers, new model ratings, new profiles, new
ranking modes and M4. Framework and SDK choices are made — and their
dependencies justified — in the M3b/M3c implementation issues, never in
advance here.
