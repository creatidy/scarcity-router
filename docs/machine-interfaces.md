# Machine Interfaces (REST and MCP)

This document is the authoritative contract for the machine interfaces. It
defines the REST surface, the MCP tool surface, their shared semantics, error
behavior, versioning, security boundary and parity requirement. Nothing here
changes selection, scarcity, provider or capacity semantics; those remain
owned by their existing authoritative documents.

- **Status:** Frozen contract. REST v1, the stdio MCP adapter and their
  automated parity surface are implemented and covered by live acceptance.
- **Implementation:** The local REST adapter and thin stdio MCP adapter call the
  shared application/core directly. Their transport code owns no selection,
  scarcity, provider or capacity semantics.

## Recommendation Versus Execution

These v1 interfaces return `SelectionDecision`, not an executable resource,
provider reservation or admission token. `reasoning_effort` is independently
catalog-sourced; `variant` is opaque. Even the authenticated composed
server's `/v1/select` delegates to the recommendation application, not
resource-aware `route_request`. Do not reconstruct an `sr-pin:` from its
model-only result or claim Kernel gateway integration from a parity test.

[#174](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/174)
owns the executable recommendation/admission/pin producer-consumer proof;
[#175](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/175)
owns rich requirement mapping. Both first inspect existing contracts and
preserve this frozen recommendation surface. No new endpoint, payload,
per-request allowlist or protocol version is introduced by documentary
alignment (D-068). The local `scarcity-router-server` recommender is not an
inference server; optional execution uses the composed control server.

#175's local `kernel_requirements` interpreter is not an additional machine
operation or request envelope: frozen v1 still accepts exactly its existing
profile-XOR-explicit-requirement inputs. The interpreter consumes producer v1
declaration bytes and explicit typed markers, accounts for unsupported meaning,
and projects through the existing requirement/routing core. Its local diagnostic
version does not version REST, MCP, OpenAI or worker messages. Kernel retains
the full current draft/decision/evidence/Program handoff and owns its trusted
approval and result/scope enforcement; metadata never grants Router permission.
Producer grammar/coverage and source-versus-consumer limits are in
`capability-model.md#kernel-ordinary-meaning-local-interpretation-175` and D-075.
Kernel #51 adoption and #174's executable public admission/pin contract remain
separate reception tasks; neither is established by a library conformance test.

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

The interfaces must not create a REST selector, an MCP selector, REST-only simulation
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

MCP application input validation mirrors the logical `invalid_request` payload
through the same shared parser (see [MCP error semantics](#mcp-error-semantics)).
The official MCP SDK owns stdio framing and JSON-RPC decoding before the tool
handler. Duplicate-name evidence is not preserved by the installed SDK and
non-standard numbers may reach the handler as floating-point values; the
shared typed parser still rejects them wherever the logical field type forbids
them. Raw framing failures and decoder behavior before the handler are
protocol-level SDK behavior, not a second Scarcity Router parser (D-031).

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
  ],
  "eligibility": [
    { "...existing ExecutionEligibility v1 to_dict()... (D-039)" }
  ]
}
```

The outer envelope field `schema_version` is the machine-interface envelope
version (integer `1` in v1). The `schema_version: 3` inside each snapshot is
the capacity contract version (D-023). They are separate concepts and are
never collapsed.

`eligibility` (D-039) is an additive backwards-compatible v1 field: the
execution-eligibility reports collected during the same one observation as
the snapshots, in canonical provider order, each with its own
`schema_version` (`1`). Providers that do not produce eligibility reports
contribute none. The key is present only when at least one report exists, so
existing v1 clients that ignore it remain valid; `scarcity_status` mirrors
the same envelope. Excluded candidates in `/v1/select` decisions carry the
paired report as `execution_eligibility` with the
`execution_*` reason codes (D-039).

CLI presentation remains deliberately separate: `status --json` is the frozen
canonical snapshot **array**, not the REST/MCP envelope, and includes no policy
reports. The terminal-only human view (D-072/#182) displays paired policy reports
when present, collection/quotas/source/fetch age and safe recovery as distinct
facts; no report is unknown, not eligible. Piped text and all JSON bytes/order,
streams, exit codes and flags remain compatible. No extra collection or clock
read, execution readiness claim, scope aggregation or watch service is added.

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
| `selector_policy` | SelectorPolicy object, optional | Serialized `SelectorPolicy`; missing or `null` means the server-configured default policy (the default user configuration, D-036) and, when none is configured, the documented neutral policy. An explicit request policy always wins. |
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
| `selector_policy` | server default, else neutral | server default, else neutral |
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

`reasoning_effort` (D-057) is an additive backwards-compatible v1 member of
every serialized candidate evaluation — `selected`, `alternatives`, `excluded`,
`closest_candidates` and `recoverable_candidates`: the exact normalized
reasoning effort configured on that candidate's catalog entry, or an explicit
`null` when the entry has no configured effort. `null` is materially distinct
from the real configured effort `"none"`. The member is always present, is
carried from the same catalog entry that participated in ranking, and is never
derived from `identity.variant`, model name, display name or any other opaque
identity. The envelope `schema_version` stays `1`; existing v1 clients that
ignore the member remain valid. The same member appears unchanged in
`scarcity_simulate` results and on every surface that serializes the
`SelectionDecision` (CLI JSON, MCP, the authenticated control `/v1/select`
and the remote bridge).

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
safe message. Exception internals are never exposed. The machine-interface
error-code vocabulary is closed: exactly `invalid_request` and
`internal_error`.

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
machine-interface contract v1 (the same logical `/v1` contract).

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

`MCP → REST → application` is rejected: it would add a runtime
dependency and a local server lifecycle requirement for MCP clients, while
the exact same application/core is already available in-process and parity is
easier to test directly. No repository evidence shows a concrete benefit that
would justify the indirection.

The adapter uses the official `mcp` Python SDK stable v2 line (`mcp>=2,<3`, resolved
to 2.1.1 in `uv.lock`) and its low-level `mcp.server.lowlevel.Server` API.
The SDK's official stdio transport owns JSON-RPC framing; the adapter itself
has no HTTP, SSE, Streamable HTTP, REST subprocess or custom protocol loop.
The shared transport-neutral dependency record and logical parser live in
`selection_app.py` and `machine_api.py`; `mcp.py` only invokes them.
The low-level SDK server does not apply an advertised `inputSchema` before
`on_call_tool`, so the schemas describe obvious object/field structure while
the shared logical parser remains authoritative for all application semantics
and returns the frozen logical errors.

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

Prose is never the primary result. The implementation uses the official SDK's
`structured_content` as the primary machine result and includes one
deterministic JSON text block containing the same payload for clients that
require text content. The SDK's `is_error` flag is true for the two logical
error payloads.

### MCP error semantics

The **logical** structured error payloads are frozen now. The MCP adapter carries them in
the official SDK's `structured_content` with `is_error = true`, preserving this
logical payload and the closed error-code vocabulary:

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

- A contract-invalid input delivered to the handler (missing/`null`
  violations, the requirement-source violation, an unknown `profile_id`, an
  unknown field, a schema-invalid value or an invalid override) is a tool
  **input error** carrying the logical `invalid_request` payload. It is never
  converted into a fabricated `SelectionDecision`. Raw duplicate-key or
  non-standard-number behavior that the official SDK resolves before the
  handler follows the SDK boundary described above; REST strict parsing is
  unchanged.
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
REST envelope, and the released CLI behavior is preserved unchanged.
The deterministic parity tests cover `direct application == CLI JSON == REST ==
MCP` for representative scenarios.

## Security and lifecycle

- **Local machine interface.** The default REST bind address is `127.0.0.1`.
  There is no `0.0.0.0` default, no LAN exposure default and no remote
  multi-user deployment assumption. Any non-loopback exposure requires an
  explicit future security decision and threat analysis
  (`docs/security.md`).
- **No authentication layer.** This is acceptable only because the server
  binds to loopback by default. REST is a local machine interface, not an
  internet-facing service. OAuth, API keys, sessions, reverse-proxy auth and
  TLS termination are out of scope; non-loopback bind options require a future
  security decision.
- **Credentials are never interface data.** REST and MCP never accept
  provider credentials from clients, never return provider credentials, never
  accept arbitrary provider endpoints and never proxy model prompts. Clients
  are untrusted with respect to secrets and receive only normalized safe
  output (`docs/security.md`).
- **REST runtime scope.** A single local process; no daemon manager, no
  background cache, no database, no persistent history, no scheduler. Each
  request may collect current provider telemetry through the existing
  application path. Request caching is not part of the current service.
- **MCP runtime scope.** The stdio process lifecycle is owned by the MCP
  client. Stdio is local process IPC: it creates no network listener and has no
  Host, CORS or MCP-auth concern. No daemon and no shared cache exists between
  REST and MCP. The tools may invoke the existing provider collectors and the
  bounded provider-managed auth recovery, but never accept credentials,
  provider endpoints or catalog paths from MCP input. The MCP SDK is the
  runtime dependency `mcp>=2,<3` of the installable package (D-034); the
  development environment additionally resolves it through the dev group.
- **Side effects.** As frozen in [Side-effect semantics](#side-effect-semantics).

## Versioning

Separate contracts carry separate versions; they are never collapsed:

| Family | Current Value | Authority and Supported Reader Boundary | Executable Evidence (`tests/`) |
| --- | --- | --- | --- |
| `CapacitySnapshot.schema_version` | `3` | [capacity.py](../scarcity_router/capacity.py), `SCHEMA_VERSION`; v3 only, not a v1/v2 capacity migration | `test_capacity.py`: old/future schema, exact types, missing semantics, closed vocabulary |
| `ExecutionEligibility.schema_version` | `1` | [eligibility.py](../scarcity_router/eligibility.py), `ELIGIBILITY_SCHEMA_VERSION`; v1 only | `test_eligibility.py`: round-trip, enum/unknown-key/version refusal |
| `Machine envelope` | `1` | [machine_api.py](../scarcity_router/machine_api.py), `ENVELOPE_SCHEMA_VERSION`; REST `/v1/`, MCP, remote recommendation | `test_interfaces_guardrails.py`: direct/CLI/REST/MCP/remote parity, additive eligibility, integer-only envelope/missing document refusal |
| `catalog_version` | `5` | [model-catalog.json](../model-catalog.json), [ModelCatalog](../scarcity_router/selection_types.py); positive integer CONTENT/calibration generation, no separate schema field | `test_model_calibration.py`, `test_selection_types.py`, `test_model_contracts.py`: provenance/override round-trip; content increment is not recalibration |
| `policy_version` | `9` | [model-policy.json](../model-policy.json), [load_model_policy](../scarcity_router/selection_app.py); positive integer CONTENT/calibration generation | `test_model_policy.py`, `test_model_calibration.py`, `test_selection_app.py`: profile expansion and decision provenance |
| `Model policy schema` | `1` | [selection_app.py](../scarcity_router/selection_app.py), `MODEL_POLICY_SCHEMA_VERSION`; declared integer 1 only; bounded legacy projection below | `test_selection_app.py`: future/bool/float/string/null refusal, compatible descriptive additions |
| `Track registry schema` | `1` | [model_tracks.py](../scarcity_router/model_tracks.py), `TRACK_REGISTRY_SCHEMA_VERSION`; reviewed dated floors, no content counter | `test_model_contracts.py`, `test_execution_sources.py`: strict shape, floor provenance, restricted tracks, generation upgrades |
| `Execution surface` | `1` | [gateway_contracts.py](../scarcity_router/gateway_contracts.py), `EXECUTION_SURFACE_VERSION`; independent Chat Completions ingress | `test_gateway_contracts.py`, `test_gateway_server.py`: request/error/pin semantics, cancellation/tools |
| `RouteDecision.schema_version` | `1` | [routing_core.py](../scarcity_router/routing_core.py), `ROUTE_DECISION_SCHEMA_VERSION`; executable core decision with D-079 public projection, not the frozen recommendation-only envelope | `test_routing_core.py`, `test_kernel_requirements.py`: exact pins, authorization, no substitution/profile drift |
| `ResourceStateSnapshot.schema_version` | `1` | [resource_state.py](../scarcity_router/resource_state.py), `RESOURCE_STATE_SCHEMA_VERSION`; observation only | `test_resource_state.py`: version/enums/required fields/future observation refusal |
| `RegistrySnapshot.schema_version` | `1` | [resource_state.py](../scarcity_router/resource_state.py), `REGISTRY_SCHEMA_VERSION`; evaluated server read model | `test_resource_state.py`: current/future snapshot versions |
| `WorkerStateReport.schema_version` | `1` | [resource_state.py](../scarcity_router/resource_state.py), `WORKER_REPORT_SCHEMA_VERSION`; authenticated grant intersection | `test_resource_state.py`: atomic report rejection; reports cannot redefine registrations |
| `ModelInventoryReport.schema_version` | `1` | [model_inventory.py](../scarcity_router/model_inventory.py), `MODEL_INVENTORY_SCHEMA_VERSION`; protocol v2+ carrier, bounded closed shapes | `test_model_contracts.py`, `test_worker_sources.py`: source/auth enums, unknown keys/missing identities |
| `Worker protocol` | `4` | [worker_protocol.py](../scarcity_router/worker_protocol.py), `WORKER_PROTOCOL_VERSION`; server `(4, 3, 2, 1)`, current worker `(4,)` | `test_worker_protocol.py`, `test_worker_sources.py`, `test_reasoning_output_semantics.py`, `test_worker_bridged_adapter.py`: old/current negotiation, tool/reasoning downlevel refusal, interruption/cancellation |
| `Server configuration schema` | `2` | [server_config.py](../scarcity_router/server_config.py), `CONFIG_SCHEMA_VERSION`; read 1/2, write 2; nonempty sources require 2 | `test_server_config.py`, `test_execution_sources.py`, `test_worker_zcode_adapter.py`: v1 projection, source kinds, future/type/field refusal |
| `Server store schema` | `2` | [server_store.py](../scarcity_router/server_store.py), `STORE_SCHEMA_VERSION`, `MIGRATIONS`; ordered 0/1 to 2, no reverse migration | `test_server_store.py`, `test_program_integration.py`, `test_m09_server.py`: rollback/idempotence/concurrent open, retained state, future startup refusal |
| `Worker identity store schema` | `1` | [worker_identity_store.py](../scarcity_router/worker_identity_store.py), `WORKER_IDENTITY_STORE_SCHEMA_VERSION`; independent pairing store, v1 only | `test_worker_identity_store.py`: identity/revocation/permissions and version refusal |
| `Worker local store schema` | `1` | [worker_local_store.py](../scarcity_router/worker_local_store.py), `WORKER_LOCAL_STORE_SCHEMA_VERSION`; independent device state, v1 only | `test_worker_local_store.py`: persistence/allowlist/permissions and version refusal |
| `Worker local settings schema` | `1` | [worker_setup.py](../scarcity_router/worker_setup.py), `WORKER_LOCAL_SETTINGS_SCHEMA_VERSION`; v1 only, closed settings | `test_worker_first_run.py`: malformed/future settings refusal, shared first-run path |
| `AuditRecord.schema_version` | `1` | [gateway_audit.py](../scarcity_router/gateway_audit.py), `AUDIT_SCHEMA_VERSION`; metadata, not physical-model attestation | `test_gateway_contracts.py`, `test_e2e_acceptance.py`: round-trip/privacy, bounded retention |
| `Package release` | `0.1.0` | [get_version](../scarcity_router/__init__.py), [release engineering](release-engineering.md); SemVer distribution, not protocol/schema/calibration | `test_release_workflow.py`, `tools/package_check.py`: metadata/resources/isolated installed surfaces |

`test_model_contracts.VersionMatrixTests` checks these values against actual
constants/loaders, not a second version registry. Listing a test is not a claim
that its current run passed. The #185 evidence baseline is Router
`79795ec99960c442901f729804569138d85a902d`; delivery evidence records the exact
tested HEAD, full-gate/package receipts and independent UX/security review.
No universal version, future ABI, adapter or endpoint is introduced.

Other current separately owned formats have literal version 1:
[KernelRequirementInterpretation](../scarcity_router/kernel_requirements.py)
(`interpretation_version`, local non-authorizing projection),
[ExecutionAssurance/source_call_facts](../scarcity_router/execution_assurance.py)
(trusted in-process facts, not public worker grants),
[DiagnosticsReport](../scarcity_router/diagnostics.py) and the
[control API export](../scarcity_router/control_api.py). Owned tests are
`test_kernel_requirements.py`, `test_execution_assurance.py`, `test_diagnostics.py`
and `test_control_api.py`. Nested requirements, selector/user policies, simulation,
capability evidence/overrides, sources, pins and adapter calls have the owning
parser's grammar, not extra invented counters. Adapter implementation versions
and vendor runtime minimum/tested versions remain distinct in
`openai_http_adapter.py`, `worker_bridged_adapter.py`, `codex_worker_evidence.py`,
`zcode_worker_evidence.py` and their compatibility matrices. Runtime minimum or
adapter version is not an installed/live receipt.

The composed producer now also includes D-079/#174, integrated at Router
`c38c8ae5390e27b9bfb9ae210b76d6efa89a2d36`: executable selection request/response
documents have literal version 1, and the retained
`ExecutableRequirements.schema_version` independently has literal version 1.
Their authorities at that exact producer revision are
[executable_contract.py](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/src/commit/c38c8ae5390e27b9bfb9ae210b76d6efa89a2d36/scarcity_router/executable_contract.py),
[executable_client.py](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/src/commit/c38c8ae5390e27b9bfb9ae210b76d6efa89a2d36/scarcity_router/executable_client.py) and
`GatewayApplication.select_executable`; `test_executable_contract.py` covers the
actual closed parsers, future/type refusal, helper preservation and exact admission.
These values do not derive from the machine envelope, worker protocol or package
version. Retained context is a demand, not authorization, availability or a receipt.
Existing context-free clients remain supported. Installed Kernel/harness and live
identity acceptance are still separate from this published producer contract.
A v1 Chat Completions endpoint alone does not establish D-079 support: the new
context requires a matching producer/consumer and must never be stripped to retry
against an older endpoint or otherwise silently downleveled.

### Projection and Upgrade Authority

The local catalog, calibrated policy and reviewed tracks remain the single
working bootstrap (D-025/D-053). Source runs resolve root-authoritative files;
installed runs resolve wheel resources independent of working directory. Explicit
overrides win. Packaging maps those same files, never a second public truth.
Omitted model-policy `schema_version` keeps the concretely shipped minimal
user-artifact projection: required positive `policy_version`, `task_profiles[].id`
and complete `calibrated_requirement`, plus existing strictly pinned evaluation
profiles. An explicit unsupported declaration never selects that legacy form.
Descriptive additions are not selector authority. Catalog/track parsers remain
closed; compatibility is per parser, not permission to ignore all new fields.

Content generations can advance without changing shape. They do not authorize
altering ratings, human overrides, calibrated minima, reviewed floors or evidence.
Catalog round-trips retain base assessment/evidence and separate human override;
profile decisions retain catalog/policy generation. Cached Kernel interpretations
retain expansion/digest and refuse changed profile content/version. Reconstructing
a historical decision needs exact input artifacts, evaluation instant and policy,
not version numbers alone. Existing blackout/happy-hour expiry and unknown capacity
semantics remain. MI conflict/freshness/calibration migration is not admitted;
#176 must retain actual utilized cuts, conflicts, expiry and local override authority
before replacing bootstrap. Publication time is not freshness or an authority winner.

Server configuration reads known v1 documents without sources and writes v2 without
dropping providers, aliases, grants or limits; source kinds stay closed/validated.
SQLite migration does not itself rewrite the configuration document. The 0/1-to-2
pending migration set is one `BEGIN IMMEDIATE` transaction: failed DDL/bookkeeping
rolls back, concurrent opens recheck history under that lock, reopen is idempotent.
Future stores refuse before persistent journal-mode/permission changes, control
composition, configuration writes or listener startup. Invalid/gapped history
refuses, never repairs. D-044 private permissions/credential boundaries remain.
Worker stores retain their independent version refusal, not server migrations.

The server validates version-specific owned table definitions, including keys,
constraints and migration targets, against its actual migration producers;
unexpected triggers/views or missing/incompatible tables cannot authorize a
migration. Formatting, exact identifier quoting and ASCII keyword/name case are
not new versions; delimiter-bearing and Unicode-confusable names are not aliases.
SQLite-compiled column names/types, nullability, defaults, primary keys, indexes
(including uniqueness and collation), and foreign keys must also match the same
trusted migration reference. Quoted constraint keywords used as declared type
words do not constitute an equivalent owned schema.
The comparison enumerates the complete `sqlite_schema`: reserved-looking names
are not exempt. Only exact generated objects present in the trusted migration
reference (including owning table, NULL SQL and compiled structure) are admitted;
unknown `sqlite_*` triggers, views, tables, indexes or statistics are refused.
The preflight reads a consistent schema/history snapshot. Ordinary read-only
errors never authorize recovery. For SQLite's exact `SQLITE_READONLY_ROLLBACK`
condition, a protected short-lived candidate is recovered and validated first;
only supported candidates authorize SQLite's normal rollback on the original.
Unknown/invalid hot stores retain their original DB/journal bytes. This is bounded
single-store crash recovery, not a repair/downgrade engine or a new operator step.
The one candidate slot is durably claimed before copying and serialized by a
portable private advisory lock. Startup reclaims only attested stale state, even
when the original is now cold or refuses a future schema; it never deletes an
active inspector or guesses ownership of unknown artifacts. The persistent lock
is non-secret metadata, not another credential store.

No downgrade/reverse migration exists. v2 retires only superseded `worker_pairings`;
those hashes cannot become M05 salted identities (D-049). Unrelated configuration,
admin/client/provider state and audit survive. Retained audit/pins are historical
metadata, not live availability/authority; retirement never substitutes a target.
Audit still expires by configured age/count, so indefinite replay is not promised.
If rollback is required, retain a consistent protected backup before upgrade and
restore only with its matching program/schema and accepted security procedure.
Never edit markers or strip unknown fields to run an older executable.

Normal steps are unchanged: install/upgrade the public wheel or checkout tool,
restart the composed server first, then upgraded workers/services. Current server
accepts old v1/v2/v3 workers; v4 workers cannot connect to an old-only server.
Inventory requires v2, client-tool continuation v3+, reasoning preservation v4;
unsupported semantics fail before send/dispatch rather than dropping effort,
tools, reasoning or provenance. Ambiguous interrupted execution is not replayed.
Existing service install/restart/uninstall and first-run provisioning remain the
[README](../README.md#updates-and-uninstall) path; no private onprem checkout,
conversion utility, daemon or recurring manual step is added. `make package-check`
is the installed-package gate, not a release/deploy claim.

### Cross-Product Evidence and Gates

Exact public source cuts below are structural evidence, not vendored code/data,
dependency adoption, permanent ABI approval or installed consumer acceptance:

| Producer / Consumer | Frozen Structural Evidence | Admitted Combination / Remaining Gate |
| --- | --- | --- |
| MI publication | MI `3a241701851eeaed2fbc0cd4b8a1e9417bb0f02d`, `src` tree `1f83d2fe835aa43e7ac569e2c047606c8ce5d712`, publication document blob `ef2b83bb6342ff94c86af229047da40d4820783e`; inspected same source at `938f4160493359f9795c3bda081b065afac96b16` | `publication.py`: framing integer 1, external exact-byte digest; `contract.py`: closed working payload `/1` and `/2`; `utilization.py`: `mi.utilization-working/1`; `producer.py`: retained atomic `/1` import to `/2`, history/scope refusal. MI #13 comment15114/#15 comment15692 accept only this bounded producer. Router does NOT admit either raw payload today. |
| Kernel recommendation | Kernel `fa7fd5da97f47834e1c071f8fa1057b10430e4a4`, `src` tree `ae78704c3146dc4749b081206d6e5b9a20bb22a1`; inspected same source at `73e3af45c911aa9ae8c26b97bf0ec9f452063f10`, `adapters/scarcity_router.py` | Existing `/v1/select` consumer validates exact envelope/identity/effort and retains opaque decision provenance. Its producer pin is Router `5c48d51f1eb1f11424a5100eb2ccf20a6cba4581`. Recommendation-only, not executable route/full harness/outcome import. |
| Router local Kernel interpreter | [kernel_requirements.py](../scarcity_router/kernel_requirements.py), schema evidence Kernel `a9a65006cc8cc5b5e4468746a18ff51daf742e6b` | Declaration v1 ordinary markers, digest/profile expansion/local exact-admission tests implemented; original subject/approval stays Kernel-owned. No public operation is inferred. |

Canonical-to-staged correspondence concerns source only; newer upstream tests/docs
differ. No upstream test execution or current installed proof follows from tree
equality. Boundaries are at
[MI #13](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/13#issuecomment-15114),
[MI #15](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/15#issuecomment-15692)
and [Kernel #11](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/11).

| To-Prove Contract | Owner / Evidence Needed Before Activation or Acceptance |
| --- | --- |
| Installed executable selection/requirement-to-pin consumption | Router #174 producer D-079 and its exact positive/negative helper fixtures are integrated at `c38c8ae5390e27b9bfb9ae210b76d6efa89a2d36`; Kernel #52 and #181 still own the separately authorized installed/real-harness identity receipt. Frozen recommendation-only output is not that executable route. |
| MI consumption/bootstrap/override migration | Router #176 + MI #19 + Kernel #55: admitted framing/payload/scope/freshness mapping, retained actual-use cut/context, conflict/expiry/calibration/override reconstruction, installed upgrade/refusal receipts. Producer acceptance is not private admission. |
| Router state/events | Router #183 + Console: audience/grants, cursor/gap/reconnect/freshness semantics, exact producer-consumer proof. Admin reads are not an agreed Console stream. |
| Complete outcome/native-lane retirement | Kernel #58 + Router #177/#199: complete accepted outcome producer, equivalent installed Kernel-controlled path and explicit retirement authority. Thin export/discovery is not migration acceptance. |

These named gates implement unresolved-producer evidence discipline, not fictional
proof of future transitions. They remain required backlog work; #185 neither defines
their ABI nor deletes their requirements.

### Explicit MI Consumer (#176)

The optional `mi` extra reuses Model Intelligence's Apache-2.0 package at canonical
revision `d65891668b7e79f7b666c021f02a1b4bf8c4ca9c`, not a second decoder, catalog,
calibration system or producer. Its public contract is
[PUBLICATION_CONTRACT.md](https://forgejo.creatidy.com/Creatidy/model-intelligence/src/commit/d65891668b7e79f7b666c021f02a1b4bf8c4ca9c/PUBLICATION_CONTRACT.md).
No default CLI/REST/MCP configuration, network refresh or active-route admission
is enabled. Normal installations do not need this extra. Install an explicitly
selected checkout with `uv tool install '/path/to/checkout[mi]'` when the optional
library consumer is required; the dependency remains exact-pinned to Forgejo.

An authorized library caller constructs `mi_consumer.MIConsumer` with an independently
trusted `Applicability`, positive byte bound and explicit `ModelBinding` associations.
`load(path, expected_cut_reference)` bounds the read; `adopt(bytes, reference)` uses
the producer's frame/digest, closed `/1` or `/2` grammar, member/source coverage and
retained-history validators before atomically replacing the in-memory input slot.
The input slot is not an admission decision. Failed reads/updates leave it intact;
there is no polling, credential access or hidden fallback. A changed scope or
rewritten/lost history refuses. Rollback uses a separately retained receipt for
historical replay, not an automatic downgrade or a freshness renewal.

Pass `mi_consumer=consumer` to the existing `selection_app.select_from_inputs` seam.
It captures the immutable cut before collectors or evaluation can refresh the slot,
projects evidence through MI's `effective_at`, and feeds bounded public native-limit
annotations into the actual selector's catalog display metadata. Exact bound model,
channel, configuration and known independent effort must agree; variant is never
parsed as effort. Public input/output token allowances with explicit basis/unit
can appear as **public facts**, not executable ceilings. Unsupported semantics,
unknown/future/stale freshness, conflicts, revocation and unknown rights produce
distinct non-positive annotations. Source-provided prose is never rendered.
Retained future-only knowledge is reported as future, not absent; a future successor
does not displace a current effective fact. Unknown values and unsupported native
semantics are separate dispositions.
Restoration requires a public, non-future, explicitly fresh retraction observation;
an inadmissible retraction cannot erase the retained revocation's withholding.
Bootstrap properties, ratings, identities, quota and private policy remain unchanged.
This explanation-only data use is implemented, not a claim of active MI routing.

Before `select_model` executes, the application freezes exact requirement/profile,
bootstrap/evaluated catalog, policy, bindings, capacity/eligibility/replenishment and
full evaluation instant as local context bytes. `decision.mi_use` retains the cut's
original bytes/reference, context, exact result bytes, utilization manifest and
EvidenceRef. `machine_api.selection_envelope` emits the accepted additive
`mi_utilization` reference beside `decision`, checking the bound result/context with
MI's own `decode_decision_utilization`. Without this explicit library input the
existing envelope is byte-shape compatible. A refresh during evaluation cannot
relabel the prior decision; microsecond instants remain distinct in the manifest.
The caller must retain the complete `mi_use` artifacts with the decision in its
existing local evidence store for durable replay, not just the reference. Private
context/publication bytes are not added to the response or uploaded to MI.

Tests in `test_mi_consumer.py` exercise the actual producer codec, shared selector,
mid-collection refresh and immutable envelope binding. They are deterministic
component conformance, not MI #19 installed acceptance or Kernel #55 adoption.
MI #19 followed Router #176 and is now closed for installed explicit-input
interoperability, not real-source positive admission. Automatic calibration and
bootstrap replacement remain disabled.

### Opt-In MI Admission (#222)

D-082 was explicitly owner-approved on 2026-10-26T22:57:22Z for implementation and
independent validation, NOT production activation. Existing default calls and the
explanation-only path are unchanged. No service, catalog or calibration layer is added.

After `MIConsumer.load/adopt` has validated a publication, the caller can explicitly
capture `admission = consumer.admission(independently_trusted_reference, evaluated_at=instant)` and pass
`mi_admission=admission` to `selection_app.select_from_inputs` or `RouteRequest`.
Explanation and active inputs are mutually exclusive at the application seam.
The immutable snapshot does not follow later refreshes or auto-approve a new cut.
It is bound to that exact evaluation instant; reuse at a different instant refuses.
Capture a current snapshot for every new decision; retain an old one only for replay.
Every new evaluation rechecks reference equality, exact reviewed applicability,
freshness, conflicts and withdrawal. Retaining an old snapshot for replay is not
an exemption from freshness or evidence of the producer's latest state. Use a new
explicitly trusted snapshot for new decisions after a known revocation; do not
reuse a historical snapshot as a current producer feed. There is no default network
refresh or latest-cut discovery/production configuration in this delivery.

Supported advertised integral input/output allowances in tokens **intersect** known
bootstrap hard ceilings. They cannot raise ceilings, fill unknown hard properties,
change quality ratings or establish entitlement, quota, billing or actual execution
identity. Missing/untrusted/unsupported/non-fresh/conflicting/withdrawn required
evidence excludes the explicitly bound identity through existing `policy_blocked`
selection or `resource_blocked` authorization gates. Unbound bootstrap identities
keep their original authority. Executable resources additionally require the exact
reviewed public channel and supported effort controls; existing authorization,
compatibility, capacity and exact-pin admission still apply. Pins never re-rank.

The route/admission wrapper freezes all authoritative inputs and MI binding/policy
before the existing core runs. `RouteDecision.mi_use` and `AdmissionDecision.mi_use`
retain the original cut/context/result/manifest. Their `mi_use.envelope(result.to_dict())`
uses the accepted utilization contract and checks the complete result binding;
raw `to_dict()` remains backwards compatible, so library callers must retain/emit
the envelope rather than discard that optional receipt. Selection continues to use
`machine_api.selection_envelope`. No existing gateway wire input grants clients the
ability to configure this Router-owned policy.

**Actual source investigation, 2026-10-26T22:59:29Z:** the existing MI adapter at the
accepted pin successfully fetched all three original models.dev files at
`f014f106dd414d575de2d0160d91267e2e7cb119`, plus current official plan/campaign/API/cohort
documents. Pricing exceeded its declared 2620-byte bound; a separate bounded
diagnostic retrieval then returned HTTP 500. No incomplete generation was normalized
or published. Exact URLs/content digests and observed failures are in #222. No
synthetic payload is substituted for real-source acceptance.

Even a previously valid complete Z.ai cut has a null overall channel,
adapter-level rather than native configuration, model subjects without explicit
native configuration/effort, and observations without `fresh_until`. Its published
native meanings are not automatically input/output allowances. Thus **no current
real Z.ai/models.dev facts are positively admitted** by this conservative policy.
The MI producer/curator can independently review changed source size/schema and
produce a complete compatible generation; the relevant source/curator must then
supply evidenced exact applicability and an explicit freshness basis, with Router
review of its exact association/meaning. None requires MI to wait for more Router
code, or Router to invent freshness/effort from aliases or retrieval time. The
controlled tests prove actual selection, routing and admission behavior, not live
provider or production acceptance.

### Acceptance Evidence Map

This covers all six #185 criteria, not only the newly corrected refusal seams.
Executed commands/results belong to the exact-revision delivery receipt; full
validation, package checks and independent reviews remain mandatory gates.

| Criterion | Local Conformance Evidence | Qualified Boundary |
| --- | --- | --- |
| 1. Independent tested matrix | `test_model_contracts.VersionMatrixTests`, calibration/policy tests, interface parity | Current values come from owning producers; package/adapter/harness versions are not schema/content versions. |
| 2. Old/current/additive/negative/lossy combinations | Interface guardrails, capacity/eligibility/resource/model parsers, config/worker negotiation, reasoning-output/tool/cancellation suites | Additions are accepted only where promised; closed shapes refuse. No unsupported old capacity shape or lossy worker transport is guessed. |
| 3. Bootstrap/config/override authority and reconstruction | `test_config.py`, `test_selection_types.py` human-override round-trips, `test_model_calibration.py`, `test_execution_sources.py`, `test_resource_policy.py`, `test_kernel_requirements.py`; synthetic MI frame non-admission | Root artifacts stay authoritative; content/provenance/expiry are preserved, not automatically recalibrated. Future MI replacement remains #176's explicit gate. |
| 4. Bounded durable migration | `test_server_store.py`, `test_program_integration.MigrationFromSchemaOneTests`, `test_server_config.py`, execution-source/ZCode config suites, worker-store version tests, composed startup refusal | Atomic rollback, idempotence and future refusal preserve state. Reverse migration/unlimited audit retention are not promised. |
| 5. Public installation/lifecycle/upgrade order | `tools/package_check.py`, `test_config.py`, `test_worker_first_run.py`, `test_worker_service.py`, `test_e2e_acceptance.py`, release-workflow tests | Source/installed resource paths, synthetic first-run/service lifecycle and server-first order; no publication/live platform/private onprem claim. |
| 6. Exact evidence/full gates/UX/security | Router baseline and pinned producer tree/blob table above; negative fixtures; required `uv sync --only-dev`, `make check`, `uv run basedpyright`, `git diff --check`, `make package-check`; fresh correctness and explicit independent security acceptance at stable base/HEAD under the applicable policy and scoped exception below | Those checks/reviews must have actual delivery receipts before completion. Named unresolved-producer gates are retained, not silently waived or implemented here. |

For these exact #174/#185 deliveries only, the owner's 2026-10-24T21:55:32Z
decision supersedes the unused manual Daybreak roles with fresh native Kilo
independent security verification. Original-source provenance, exact scope and
finite allocations are preserved in [#174's admission](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/174#issuecomment-18779)
and [#185's admission](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/185#issuecomment-18776).
Actual explicit security approval remains mandatory; tests or generic COMMENT do
not replace it. This is not a global policy change or a Daybreak invocation through
Kilo. Other deliveries keep their existing security gates, and D-078's fresh
read-only conversation, frozen source and automatic native handoff stay unchanged.

### Machine-Interface Evolution

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

No compatibility serializer is currently needed; choosing between the two
options above for a concrete incompatible nested-domain evolution is a future
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
must serialize or bound them explicitly.

## Design scenarios

Representative machine-interface semantics are covered by deterministic tests
using fixtures, never live quota:

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

## Coexistence with the execution surface (D-045)

The optional execution gateway (D-040) adds an OpenAI-compatible execution
surface. It is a **separate, independently versioned contract** — "execution
surface v1" — and this document remains the authority only for
machine-interface v1:

- Machine-interface v1 is untouched: same four REST operations, same MCP
  tools, same envelopes, error vocabulary, loopback/unauthenticated boundary
  and additive versioning rules (D-028/D-030/D-031). The M08 guardrail suite
  (#93) keeps the `direct == CLI == REST == MCP` parity green throughout the
  gateway program and extends it to gateway-era additive fields.
- The execution surface (`GET /v1/models`, `POST /v1/chat/completions` with
  SSE) is served by the authenticated server component (D-044). Its `/v1/`
  prefix is the OpenAI client convention and is **not** machine-interface
  v1. The two path sets are disjoint
  (`/healthz`, `/v1/status`, `/v1/select`, `/v1/simulate` versus
  `/v1/models`, `/v1/chat/completions`), so one listener may serve both in
  server deployments without ambiguity, but contracts, versions, security
  boundaries and error vocabularies never mix: execution-surface errors
  follow OpenAI-compatible client conventions and never reuse or extend the
  closed `invalid_request`/`internal_error` vocabulary frozen here.
- The loopback REST adapter defined here is never converted into the
  execution server; the execution server is a distinct component with its
  own listener, TLS and authentication. In server deployments, control
  operations with equivalent semantics are exposed through the server's
  authenticated control API under explicit versioning (M08/M09).
- The optional remote bridge (M08) is a configured client mode against the
  authenticated server: explicit configuration, explicit failure — a
  configured remote server that fails surfaces an error and never silently
  degrades to local state or local policy.
- The worker protocol carries its own independent protocol version with
  handshake negotiation (D-043) and is not part of either interface version.

## Non-goals

Within machine-interface v1 itself, this document still provides no remote
service exposure, authentication or TLS termination, Docker/systemd/Windows
service packaging, databases, caches, persistent history, dashboards, model
execution, prompt proxying, automatic dispatch, reset-credit acquisition or
redemption, provider health integration, Artificial Analysis runtime
integration, new providers, new model ratings, new profiles or additional
ranking modes. The execution surface, its authentication and the server
component are now authorized program scope, but they live in their own
contracts outside this v1 document (D-040, D-044, D-045;
[`docs/architecture.md`](architecture.md)); any change to the local security
or runtime boundary of the surfaces defined here still requires an explicit
decision and corresponding contract review.
