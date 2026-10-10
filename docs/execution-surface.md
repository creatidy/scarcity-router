# Execution Surface v1 (OpenAI-compatible ingress)

This document is the authoritative contract for the execution gateway's
OpenAI-compatible ingress, implemented by M03 (#88). It defines the
surface, its authentication and limits, the request/response shapes, the
streaming lifecycle, error semantics and the audit trail. Nothing here
changes selection, routing, capacity or provider semantics; those remain
owned by their existing authoritative documents
([`docs/selection-policy.md`](selection-policy.md),
[`docs/capacity-model.md`](capacity-model.md),
[`docs/architecture.md`](architecture.md)).

- **Status:** Versioned contract, implemented ("execution surface v1").
  The surface is optional, explicitly deployed server functionality
  (D-040); recommendation-only mode is unchanged and remains the default.
- **Implementation:** `scarcity_router/gateway_server.py` (authenticated
  ingress), `scarcity_router/gateway_coordinator.py` (execution
  lifecycle), `scarcity_router/gateway_openai.py` (wire mapping),
  `scarcity_router/gateway_adapters.py` (dispatch seam),
  `scarcity_router/gateway_audit.py` (D-043 audit trail),
  `scarcity_router/gateway_contracts.py` (limits, errors, client
  identity). The loopback REST v1 adapter (`scarcity_router/server.py`)
  is untouched and never becomes the execution server (D-030/D-045).
  The `server_direct_http` channel's production adapter is the generic
  OpenAI-compatible HTTP adapter with evidence-based presets
  (`scarcity_router/providers/openai_http_adapter.py`, M04 #89); since
  the M04/M05/M09 integration the `worker_bridged` channel is served by
  the M05 adapter when administrator configuration binds a worker
  resource (`scarcity_router/server_composition.py`), and M06's Codex
  execution adapter (issue #91 Stage 2) is delivered through that same
  `worker_bridged` channel as a worker-local adapter
  (`scarcity_router/worker_codex_adapter.py`, allowlist id `codex`,
  composed via the resource's `local_adapter_id` per D-049) — no second
  execution channel exists for it, and the `local_app_adapter` channel
  itself remains unregistered. API-only operation works without a worker.

## Shared-System Scope and Current Limits

D-068 assigns task/workspace/harness/accepted-result lifecycle to Kernel;
Router owns inventory/selection/admission/provider calls. A harness remains
the tool-loop executor; client tools return to it. The integrated D-063
workspace-editing ZCode lane is a preserved, explicit ownership conflict
(U-014/#180), not target inference-only conformance.

This implemented ingress is Chat Completions. Responses is accepted future
direction under D-056, with actual subset/compatibility work registered in
#181; Anthropic Messages is not an implemented ingress. Protocol naming
alone proves neither compatibility nor a complete tool loop.

The public `sr-pin:<resource_id>/<provider>/<model>/<variant>` and optional
`@<decision_id>` go through exact `admit_pinned_target`, which rechecks
current authorization/state without competitive reranking. Pins are not
credentials, quota reservations or physical-model attestations for a
plan-managed lane. Another authorized role/Attempt may obtain a new decision;
no substitute occurs within a pinned Attempt. Non-pinned generic clients
are not silently migrated into this rule.

The #178 pure-core exact-pin correction preserves the gateway's exact admission
path. #179/D-071 separates catalog effort from opaque variant in discovery,
logical resolution and pin dispatch. The plan lane's `plan` variant is not a
reasoning effort. Effort-less plain HTTP pins retain omitted wire control:
their evidenced preset mapping, not the variant name, governs provider effort.
Ordinary logical HTTP requests also preserve omission: choosing a sole configured
variant does not promise that its effort was sent on a preset that cannot map it.
Evidenced native worker defaults carry catalog effort; unsupported worker-loopback
controls preserve omission. Codex independently checks
its runtime's supported effort, never equates opaque variant with effort. Only an
inventory-owned native binding can supply a default for an otherwise raw null call.

`executed_target` records dispatch provenance, not independent physical-model
verification. #179/#181 require evidence for actual harness/protocol/adapter/
source versions. Configured subscription entitlement alone cannot establish
observed access mode or free/eligible promotional usage (#184). Request
limits and `SpendingLimit` rate ceiling are not an accepted-task spend cap;
unknown usage/hidden calls stay unknown and uncontrollable output cannot
promise hard cost. Kernel aggregates full-path budget; Router supplies and
enforces only supported source/call facts. No additional API or payload is
introduced by this qualification; #174 owns missing public consumer proof.

### Identity Evidence Boundary

The following distinctions apply to every supported channel and its actual
adapter/protocol version; existing response/audit fields are not attestations:

| Fact | Existing evidence | Limit |
| --- | --- | --- |
| Requested | Parsed `model` and normalized request `reasoning_effort`; response `model` echoes the request | An echo is not execution observation |
| Resolved | Exact admitted resource/provider/model/opaque variant and catalog configured effort | A catalog or source registration is configuration, not physical-model proof |
| Dispatched | `executed_target`, adapter name/version, and the adapter call's carried effort | The target is assigned when dispatch starts, not when a model is independently observed |
| Observed | No independent physical-model/effort attestation in the frozen response/audit contract | UNKNOWN/unattested on `server_direct_http`, `worker_bridged` and `client_direct`; never inferred from dispatch, provider-reported tokens or model self-report |

`plan-managed` names only the configured lane. Its catalog effort is null and
discovery advertises no effort; even literal `none` is an unsupported configured
control there. Exact physical-model requests cannot resolve onto that lane. A
consumer requiring attested physical identity must refuse an unattested route,
not treat this metadata as RUNTIME_VERIFIED. No new physical-attestation request
flag or guarantee is introduced; that consumer contract remains under #174/#175.

Native Codex checks its configured model/effort arguments; ZCode 0.16.9 has no
supported physical-model observation or effort control. Neither fact attests
the physical model executed. Output, continuation, cancellation, usage and
helper-call limitations stay channel/version-specific matrix and audit facts;
missing usage or hidden-call evidence is unknown, not zero calls or free access.
**REAL_HARNESS_IDENTITY_ACCEPTANCE** remains OPEN under #174/#181 for separately
authorized version-pinned real-harness receipts. #180 still governs affected
workspace-editing lanes; synthetic #179 acceptance does not resolve it or grant
live inference, deployment, workspace or credential access.

## Harness compatibility contract (D-056)

### Executable Recommendation (D-077, #174)

Contract-first producer freeze under #174; installed consumer acceptance remains
open. The optional gateway
adds authenticated `POST /v1/route`, not a recommendation v1 mode or another service.
It uses the same inference-client bearer key and current administrator/client
grants as execution. Request version, route-decision schema, recommendation schema,
catalog content and policy versions are independent.

The closed request has exactly `schema_version: 1`, `model` (existing alias/logical
model/pin spelling), `requirement` (complete existing TaskRequirement) and `binding`
(existing RequestBinding structural/identity demands). Ordinary task meaning can
be prepared with #175's local interpreter; Router does not authenticate Kernel
approval or invent minima from text. Missing quality, unsupported/contradictory
constraints, booleans as versions and unknown fields refuse without inference.
The operation reuses current configured artifacts, policy, compatibility and
quota-free readiness observation. No model-call probe, quota reservation, dispatch,
client-supplied grant or caller-selected provider origin is permitted.
Selection captures one current authority tuple (registry, administrator rules,
client grant and adapters), not a mixture with an older application's authority.
The same snapshot determines target eligibility and public disclosure. Unavailable
authority refuses with `state_unavailable`; a removed adapter is an availability
refusal (`adapter_unavailable`) on this new opt-in surface. Legacy route inputs
have no adapter-channel intersection and their existing behavior remains unchanged.
Exact admission of retained context uses that same current-authority mechanism,
including the current entry's health/capability/effective-limit facts, not an older
application's cached entry. After task/profile/structural merging, the retained
known input-context demand must also fit the gateway allowance at selection and
execution; checking only the small outgoing message estimate is insufficient.
The composed publisher supplies the complete current application, including
catalog/calibration, profiles/version, aliases, compatibility, policy and gateway
limits. An operation takes a transient read-only view sharing that publication's
runtime handles, not a second inventory or service. Admission uses its coherent
authority inputs; final dispatch still performs the existing live revocation check.
Failure to obtain the publication refuses without an old-application fallback.
The authenticated HTTP ingress also rejects its transient bearer appearing inside
reflected demand strings, including embedded tags. Credentials are not retained,
logged or placed in refusal messages; no durable plaintext credential store is added.

The version-1 executable envelope contains `route`, the actual existing RouteDecision,
and `execution`, null on no solution, otherwise a ready-to-use exact `model` pin,
catalog `reasoning_effort` (including distinct null/none/plan semantics), and complete
`execution_requirements`. It is not a new ranking DTO. Typed route refusals preserve
the layer; unsupported physical observation cannot become a verified assertion.
Resource disclosure is checked independently against the same effective core
authorization, even when binding failed before the authorization gate. Denied
identities become `restricted`, private detail and denied promotion provenance
are omitted, and the actual refusal layer/reason codes remain.
This is the authenticated public projection of the actual core decision, not an
additional ranking or a way to inventory resources denied to the client.
The pin is produced/verified through existing serialization/parser semantics,
including optional `@decision_id` provenance, not a newly invented spelling.

`execution_requirements` is an opt-in closed version-1 context containing the
complete resolved `requirement`, structural `binding`, `profile_policy_version`
and `profile_expansion` (the latter two explicitly null when no profile is used).
It is an additional demand, not an approval, grant, publisher attestation or
reservation. It must accompany an exact `sr-pin:` model on Chat Completions;
alias/logical substitution refuses. The pinned identity must agree with retained
binding. Current profile version AND content, actual message/tool/stream/output
controls, current grants/resource state/capabilities and hard quality requirements
are rechecked; no ranking or fallback occurs. A numeric output requirement is a
capability floor, not a generation cap; submitted output ceilings remain actual
execution controls and cannot be silently lost. Context is never forwarded as
prompt, provider metadata or a tool instruction.
The retained quality includes genuine task/profile output floors, not the extra
capacity demand derived from a generation ceiling. A smaller execution ceiling is
permitted only when it does not contradict a genuine floor. Selection checks the
controls needed for each candidate's promised catalog effort before competition;
an effort-less configuration is not forced to acquire reasoning controls.
An incoming pin's old/absent decision reference is audit provenance, not target
identity. A newly issued route decision may update that reference while preserving
all resource/provider/model/variant dimensions; the prepared response/context/final
outbound pin then agree exactly.
For a suspended native turn using executable context, the actual initial output
control is retained separately from its maximum permitted ceiling. Continuation
must resend that actual control unchanged: the existing result-delivery protocol
cannot apply a newly narrowed cap to the running turn. Such changes refuse before
tool-result delivery, including a formerly non-binding cap narrowed to a binding
one. Legacy context-free continuation behavior is unchanged; no turn is silently
restarted or relabelled to apply a new control.

A narrow consumer helper consumes the actual serialized envelope and its retained
expected request, checks identity/effort/decision consistency and prepares an ordinary
Chat Completions payload with the pin/context. It rejects missing/malformed/lost/
changed pins or requirements on that path, without requiring manual copying or
new recurring configuration. Any promised non-null effort is explicitly carried;
an adapter lacking its evidenced mapping refuses rather than drops it. Existing
generic clients and frozen recommendation CLI/REST/MCP are unchanged.

**Proof limit:** a stateless server cannot identify the original Attempt if a
harness strips its context, even if the historically valid pin remains, or strips
both context and pin and sends an otherwise valid ordinary request.
That harness path is unsupported/unaccepted for a pinned Attempt; it is not an
automatic new Attempt or universal gateway loss-detection guarantee. The exact
inspected Kernel #51 recommendation adapter is not an installed executable consumer.
Kernel #52 owns that binding; no modifications to Kernel are part of this delivery.
Synthetic dispatch preservation cannot satisfy REAL_HARNESS_IDENTITY_ACCEPTANCE.

Consumer entry point is
`scarcity_router.executable_client.prepare_executable_completion(response,
expected_request=request, completion=chat_body)`. Retain the original request
and producer response in Kernel's immutable Attempt evidence. It performs
no semantic approval: retain #175's original approved declaration, its digest,
interpretation version, inspected producer revision and used profile expansion
in Kernel's evidence before transmission. The demand/context is not that receipt.
The helper performs
no network calls or execution and returns the ordinary outgoing body; it validates
the complete received frame against the retained request before adding the exact
pin, effort and context. A non-null output ceiling is copied into the actual
generation control when absent; conflicting/larger controls refuse. A supported
consumer checks the resulting body at its dispatch boundary, rather than treating
generic metadata or a model echo as preservation proof.
The same helper's `require_bound=True` validates the final serialized outbound
body against the retained artifacts: lost pin/context/effort or output control
refuses without insertion, repair, ranking or relabelling as a new Attempt. Default
preparation accepts an unbound chat body; an already-bound partial body refuses.
This validation belongs in the supported consumer adapter, not a manual operator
step. An arbitrary unvalidated harness is not thereby supported or accepted.

Separately authorized end-to-end recipe: record exact Router/Kernel/harness versions
and scoped authorization; prepare an approved supported requirement; consume the
serialized executable envelope automatically; persist its pin, complete context,
decision and original approved meaning before Attempt dispatch; demonstrate the
actual outbound request retains them and admission rechecks current authority;
compare exact dispatch evidence with independently observed identity where available.
Exercise lost pin/context, changed effort/profile, revoked grant, stale resource,
unavailable model, incompatible tools/protocol, restart and ambiguous dispatch
without retry/substitution. Record UNKNOWN physical identity/usage honestly.
Inference, workspace/tool effects, service deployment and financial acceptance
remain separately authorized; no such receipt is delivered by an offline fixture.

This surface is the first protocol adapter over Scarcity Router's
harness-independent semantic execution contract. Compatibility is defined
SEMANTICALLY — what any conforming harness may rely on — never as a list of
client-specific payload quirks. ZCode, Kilo, Cline, other coding-agent
harnesses, OpenAI-compatible SDK clients, scripts and first-party
algorithms consume the same semantics; the routing core never branches on
client identity, and a client's harmless additional syntax is normalized at
this protocol edge only when its semantics are unambiguous and bounded (the
reasoning-dialect layer below is the precedent).

| Semantic area | Contract | Status on this surface |
| --- | --- | --- |
| Model discovery | adoption-aware `GET /v1/models` with `x_scarcity_router` metadata (D-055) | Implemented |
| Logical model selection | bare logical ids resolve to the exact `(provider, model)` identity; no cross-model substitution (D-055) | Implemented |
| Reasoning controls | exact `reasoning_effort`; bounded dialect normalization (#135); D-054 max-only intact | Implemented |
| Role history | `system`/`developer`/`user`/`assistant`/`tool`, admission-gated per matrix cell | Implemented (text-only in v1) |
| Streaming | SSE `chat.completion.chunk` frames, optional usage chunk, `[DONE]` | Implemented |
| Client-owned tool declarations | `tools[]` validated at ingress; capability-gated before inference | Implemented |
| Tool calls returned to the client | `tool_calls` always return to the CLIENT; the router/worker never executes them (D-043); admitted per source only where the matrix evidences it | Implemented on evidenced server-direct channels (`tool_calls` PASS/PARTIAL cells); Codex worker source PARTIAL (D-062: the protocol-v3 availability-gated dynamic-tool bridge; live signed-in acceptance pending) |
| Client tool-result continuation | `role: "tool"` results with `tool_call_id` transported back into the backend's continuation | Implemented on evidenced server-direct channels (`tool_results` PASS/PARTIAL cells); Codex worker source PARTIAL (D-062: the suspended-turn continuation with the owner-accepted lossy `success` mapping; live signed-in acceptance pending) |
| Structured output | `response_format` text/`json_object`/`json_schema`, matrix-gated | Implemented |
| Max output semantics | effective output ceiling = model ∩ channel ∩ administrator allowance; honest, visible, rejection-based; a channel without an output-limit control normalizes away only a non-binding requested limit (audited) and rejects a binding one (`output_limit_unenforceable`) | Implemented (#136/D-058) |
| Context capability | effective context = model ∩ channel ∩ administrator allowance; UNKNOWN never guessed | Implemented (#136/D-058; D-055 metadata) |
| Cancellation | propagates to the backend where the channel supports it; client disconnects detected | Implemented |
| Usage | provider-reported vs estimated kept distinct (`usage_source`) | Implemented |
| Typed errors | closed OpenAI-compatible vocabulary; fail-closed semantics | Implemented |
| Authentication | one bearer client key per inference identity (D-044) | Implemented |
| Transport security | loopback plain HTTP supported (incl. host-network containers); same-host container tier accepted, implemented under #139, applies to the whole composed listener; TLS required otherwise (D-056) | Partially implemented |

The client-owned tool lifecycle (D-056):

```text
harness advertises tools
  -> Scarcity Router transports tool definitions
  -> backend/model requests a client tool
  -> Scarcity Router returns a tool_call to the harness
  -> harness executes it under the HARNESS permission model
  -> harness sends the tool result
  -> Scarcity Router transports the continuation
  -> backend/model continues
```

Scarcity Router and its worker never execute a client-owned tool.
Backend-native tools (for example a backend's own command-execution or
file-change facilities) are a different capability domain and are never
presented to the client as if they were client tool calls. The lifecycle
is a per-source capability: evidence-backed server-direct presets already
evidence `tool_calls`/`tool_results` (PASS/PARTIAL cells with dated
evidence; the evidence-free generic OpenAI-compatible preset defaults
every cell to UNKNOWN and stays fail-closed), so tool-requiring requests
execute there today. The Codex worker source implements the lifecycle
through the D-062 client-tool bridge (#137): a PARTIAL, protocol-v3-gated
suspended-turn continuation whose evidence and remaining live gate are
recorded in `docs/codex-adapter-stage1-evidence.md` and D-062.

### The Codex client-tool continuation (D-062, #137)

On the Codex source, the lifecycle is a SUSPENDED TURN (Family A): the
backend thread/turn stays alive while the harness executes its tool, and
the harness's ordinary `role: "tool"` request resumes the SAME turn —
one provider call, one usage observation, one decision identity across
both HTTP legs. One mapping rule is an explicit OWNER DECISION
(D-062 pt 6): upstream (openai/codex @ 36650394) defines the
dynamic-tool answer's `success` as "Whether the tool call succeeded" —
a required bool the generic text-only `role: "tool"` message does not
carry — so Scarcity Router applies an owner-accepted LOSSY
compatibility rule: a valid `role: "tool"` result is represented as
`success: true` with the verbatim content as one inputText item. That
answer does NOT natively mean "the client returned a result" and is
NOT proof the external operation succeeded; semantic failures ride in
the verbatim content, which Scarcity Router never inspects, parses or
reinterprets, and `success: false` is never inferred. The frozen rules
of the implemented mechanism:

- **Ordinary shapes are the only carrier.** The initial response is a
  normal `assistant.tool_calls` + `finish_reason: "tool_calls"`
  completion; the continuation is a normal request echoing those
  `tool_calls` and carrying `role: "tool"` with `tool_call_id`. No
  Scarcity-Router header, field, endpoint or internal id exists. The
  visible `tool_call_id` is an opaque server-issued token (`srct-…`);
  worker, thread and process identities never cross the wire.
- **Exactly-once, single-subscriber.** The continuation resolves at
  most once: replay or double delivery is `409
  continuation_already_resolved`; a result from another client is
  indistinguishably `404 continuation_not_found`; an unknown id is
  `404 continuation_not_found`. A continuation past its deadline is
  `404 continuation_expired`. Any drift in the echoed request — model,
  reasoning effort, tool declarations, `tool_choice`, conversation
  prefix, the assistant `tool_calls` echo, or an over-bound (4 MiB)
  result text — is `400 continuation_mismatch`. Fingerprints are
  bounded digests; no prompt or tool content is stored.
- **One absolute lifetime.** The whole logical turn — initial
  inference, harness tool time, resumed inference, further rounds —
  shares the original attempt's admission deadline
  (`execution_time_limit_seconds`). There is no per-leg reset; expiry
  interrupts the backend turn, cleans the record, and later results
  receive the typed expired/not-found response. Pending continuations
  are bounded (server-side structural bound, never client-expandable).
- **Sticky for ranking, never above authority.** The continuation
  path performs no scarcity/campaign/policy ranking: D-059 changes
  cannot move or terminate a suspended turn. Two hard gates recheck
  CURRENT state before delivery: the client's live authorization grant
  is evaluated against the EXACT original target through the same M02
  authorization stage admission used (a revoked/narrowed grant yields
  typed `403 unauthorized_target` and the suspended turn is cancelled),
  and the exact target's registration must still exist. Worker/source
  loss and gateway restart fail the continuation closed (the state is
  in-memory by architecture; a lost suspension says
  `continuation_not_found`, never pretends durability).
- **Live v3 eligibility, pre-ranking (review round 2).** A
  tool-bearing request is availability-gated per `worker_bridged`
  resource on the owning worker's LIVE protocol-v3 negotiation
  (`worker_continuation_unavailable`): with two otherwise equal routes,
  the v3 route is selected and a v2 route is never chosen into a
  backend continuation failure; with no live v3 fact, tool requests
  fail closed; ordinary non-tool requests are unaffected; the static
  matrix is not mutated.
- **Registration before exposure.** A `tool_call` id only ever reaches
  a client already registered as a live continuation: registration
  happens inside the dispatch before the streamed `tool_call` frame or
  the response is produced. A bounded-table failure cancels the
  suspended backend and returns a typed failure that exposes no token.
- **Streaming.** A streamed suspension leg renders the complete
  `tool_call` delta and the `finish_reason: "tool_calls"` frame before
  `[DONE]`; the resumed leg streams the turn's continuation normally.
- **Audit.** The initial leg audits `completed` with an
  `unknown`-status usage-free call observation (`suspended_for_client_tool`);
  the terminal leg audits `completed` with the turn's single
  usage-bearing observation (`continuation_resumed`) and repeats the
  original decision/target identity — one provider call is never
  reported as two.

### Effective capability (D-056; implemented by #136, D-058)

A route's executable capability is the intersection of four distinct
layers: logical model hard capabilities (catalog, provenance-bearing);
execution-source/channel capabilities — what the route evidences it can
carry (context ceiling, output ceiling, output-limit control, tool round
trip, streaming, cancellation); administrator policy/limits —
authoritative ceilings that only narrow; and client-request requirements,
which may only narrow, never expand (D-042). In short:

```text
effective capability = model capability
                     ∩ execution-channel capability
                     ∩ administrator allowance
```

The implemented per-route rule (#136/D-058): the route's effective
ceiling in a limits dimension is `min(known of: the EXACT calibrated
variant's hard property, channel ceiling, administrator allowance)` —
never a sibling variant's calibration, never a minimum across variants —
and it stays UNKNOWN when neither the variant nor the channel evidences a
ceiling — an administrator allowance alone never certifies a capability
(UNKNOWN → `null` in metadata, fail-closed in admission, never a guessed
number). Output capability gates routes BEFORE competitive ranking
(`output_limit_unknown` / `output_limit_insufficient` /
`output_limit_unenforceable` reason codes, one shared rule with
post-admission enforcement): a request never routes to a weaker route
when another route serving the exact same identity can satisfy it; a
pinned route is evaluated exactly and rejected with the mapped typed 400s.
Admission then enforces the SELECTED route's effective ceilings with
typed rejections (`context_length_exceeded`, `output_limit_exceeded`) —
never silent clipping — and the administrator's global pre-check stays
authoritative.

**Model-level advertising (the #136 aggregation rule).** Multiple routes
serving the same logical model may carry different effective ceilings;
admission stays route-specific. `GET /v1/models` advertises as the
model-level headline (`effective_context_limit_tokens`,
`max_output_tokens`) the STRONGEST bound route's effective ceiling — a
request within it is executable on this gateway through that route
(pre-ranking output eligibility routes each request to a satisfying
route) — and carries the honest per-route detail beside it: an
`x_scarcity_router.routes` array with every bound resource's own
effective ceilings (`null` when unknown for that route), each computed
per exact calibrated variant the resource binds and reporting the
weakest such variant (the detail never over-advertises any variant it
serves). The headline is never presented as if every
route supports it (the detail exposes the spread), a request is never
constrained to the weakest route's ceiling, and UNKNOWN never becomes a
number. A request whose output semantics no route can satisfy — above
every route's evidenced output, or a binding explicit limit on channels
without an output-limit control — finds no eligible target and is
rejected with the TYPED limits 400 (`output_limit_insufficient` /
`output_limit_unenforceable` / `output_limit_unknown`; mapped for
pinned and unpinned decisions alike), never a generic
retry-suggesting error; an administrator- or channel-shortfall request
on the selected route takes the same typed rejections.

**Channels without an output-limit control.** A channel that evidences
`output_limit_control: false` (today the Codex execution surface,
re-evidenced 2026-09-28: the app-server turn contract carries no
output-token control) cannot honor an explicit client output limit. A
requested limit on such a channel is normalized away ONLY when it is
non-binding — at or above the model's proven hard maximum
(`hard_properties.output_tokens` of the exact selected model) — and the
normalization is audited (`output_limit_normalized`); a smaller, binding
request is rejected `output_limit_unenforceable` rather than silently
ignored; an UNKNOWN hard maximum is never normalized. Arbitrary
`generation_params` stay refuse-not-drop everywhere.

**Honest administrator defaults.** The shipped `GatewayLimits` defaults
(16 MiB body, 2^21 input-context tokens, 131072 output tokens, 1200 s
execution) are bounded operational guards that only narrow —
ADMINISTRATOR POLICY, deliberately separate from execution-channel
capability. No default is derived from a single channel's registration
fact: the body bound carries headroom above the largest evidenced
context (tokenization-independent), and the input guard sits above the
strongest evidenced model hard context in the current catalog so policy
never silently narrows an evidenced route; reachability is enforced per
route from the intersection. An operator may knowingly lower any of
them, and lowered (non-default) limits are exactly what the
configuration export represents.

## Versioning and coexistence (D-045)

- The execution surface is its own contract family starting at version 1
  (`EXECUTION_SURFACE_VERSION = 1`). Its `/v1/` path prefix is the
  OpenAI **client convention**, not machine-interface v1; the two
  contracts never share a version, an error vocabulary or a security
  boundary.
- Path sets are disjoint: machine-interface v1 is exactly
  `/healthz`, `/v1/status`, `/v1/select`, `/v1/simulate` on the loopback
  REST adapter; the execution surface is exactly `GET /v1/models` and
  `POST /v1/chat/completions` on the authenticated execution server.
  Because the path sets are disjoint, one listener process MAY serve both
  in server deployments (D-045); what never mixes is the contracts'
  semantics -- versions, error vocabularies and security boundaries stay
  separate documents and separate boundaries in one process.
- Execution-surface errors follow OpenAI-compatible client conventions.
  They never reuse or extend the closed machine-interface
  `invalid_request`/`internal_error` vocabulary.
- The surface evolves additively within v1; any incompatible change
  requires a new major version and an explicit decision.

## Security boundary (D-044)

- **Authentication.** Every request must carry exactly one
  `Authorization: Bearer <key>` header. Keys are verified against the
  administrator-provided client-key directory, which stores only SHA-256
  key hashes and compares them in constant time. There is no default
  key, no anonymous access and no administration endpoint: client keys
  authorize inference and nothing else.
- **No bearer secrets in URLs.** Credentials are read from headers only;
  query strings and fragments are never interpreted.
- **Listener defaults.** Default bind is `127.0.0.1:8787`. Plain-HTTP
  loopback is a SUPPORTED LOCAL TRANSPORT, not an insecure-debug escape
  hatch (D-056): no certificates are required for a locally running
  harness or client against a locally running router, including a
  container sharing the host network namespace (native Linux
  `--network host` shares the host loopback). A standard Docker bridge
  publish of a loopback port (`-p 127.0.0.1:8787:8787`) is NOT currently
  supported in plaintext — published traffic reaches the container on a
  non-loopback interface, where a plaintext bind is refused — and
  belongs to the accepted container-transport tier, implemented under
  #139. A non-loopback bind requires explicit TLS (`--tls-certfile`/
  `--tls-keyfile`) and is refused otherwise; there is no plaintext
  non-loopback mode and no `verify=false` anywhere. When the container
  tier lands it will be an explicitly bounded deployment mode applying
  to the entire composed HTTP listener (execution, machine-interface,
  control and administration surfaces ride one listener, D-041), never a
  global non-loopback plaintext listener; until it lands, non-loopback
  plaintext is refused exactly as today.
- **Host discipline.** Exactly one `Host` header is required; on a
  loopback bind its value must identify the loopback listener
  (DNS-rebinding guard). No CORS headers are ever emitted.
- **Router-loop protection.** Gateway-originated traffic identifies
  itself on ingress with the `X-Scarcity-Router-Gateway` marker header
  (stamped by this program's outbound adapters, M04+). Requests carrying
  it fail loudly with `router_loop_detected` instead of silently serving
  as another router's backend.
- **Admission limits before dispatch** — request-body size, estimated
  input context, output ceiling, global and per-client concurrency,
  execution time and spending ceilings (the routing core's
  `SpendingLimit`, D-042) — all administrator-configurable with safe
  defaults (`GatewayLimits`). No client input can expand any limit.
- **No prompt/response logging by default, no secret logging.** The
  handler is quiet by design; every error message is a safe structural
  message. Diagnostics are redacted and allowlisted.

## Model-field resolution (D-042, amended by D-055)

The `model` field of every request resolves in exactly one of three
ways; anything else is `model_not_found`. The resolution order is:
pinned reference, then alias, then logical model.

1. **Pinned executable-target reference** — the exact reference syntax
   `sr-pin:<resource_id>/<provider>/<model>/<variant>`, optionally
   carrying `@<decision_id>` audit provenance from a prior
   recommendation. The reference is EXACT: all four identifier components
   are required and admission approves exactly that resource and variant.
   A no-longer-bound identity is the explicit `pinned_model_not_bound`
   rejection — never a substitution, never a re-ranking.
2. **Routing-profile alias** — an administrator-configured name bound to
   an EXISTING profile id in the task-profile catalog (for example
   `deep-coding`). Aliases are bindings into the existing
   requirement/policy model, never a second scoring system. Aliases take
   precedence over logical model ids (see below); an alias shadowing a
   logical model is visible in `GET /v1/models`
   (`logical_model_shadowed`), never accidental.
3. **Logical model (D-055)** — an adopted source model requested by its
   bare physical-model id (`gpt-5.6-luna`). Resolution binds the exact
   `(provider, model)` identity and constrains routing to exactly that
   identity; normal capacity/scarcity/authorization policy chooses among
   the resources providing it. No cross-model substitution exists; no
   exact match fails explicitly. The reasoning effort is exact:
   `unsupported_reasoning_effort` for a configured effort the identity does
   not offer (`compatibility_unknown` when reasoning support is unknown),
   `reasoning_effort_required` when no effort is requested and several distinct
   configured efforts exist (null and literal `none` remain distinct).
   Several opaque configurations with the same effort compete normally.
   A max-only family's single legal effort `max` is used, per D-054.
   A slug carried by two providers is `ambiguous_logical_model`.

`GET /v1/models` lists the configured aliases first, then the exposed
logical models: a calibrated model identity is exposed only when at
least one registered resource binds it. An empty deployment exposes
nothing; a retired model disappears; a registered-but-unavailable model
still appears and yields `no_eligible_target` on request (never a
misleading `model_not_found`). Restricted and unclassified models are
never exposed or resolvable. Every entry carries the additive
`x_scarcity_router` metadata block: `kind`
(`routing_alias`/`logical_model`), and for logical models `provider`,
`reasoning_efforts` (configured catalog efforts with matching bound routes,
never opaque variant names; null is omitted, literal `none` retained),
`effective_context_limit_tokens` (model hard
context intersected with the bound channels' known context ceilings;
`null` = unknown, never guessed) and `max_output_tokens`. A
recommendation is not a reservation: pinned admission re-checks current
state and may reject explicitly.

## Requests

`POST /v1/chat/completions` bodies are parsed with the same strict JSON
rules as machine-interface v1 (duplicate object keys and non-finite
constants are rejected before parsing). The top-level key set is a closed
allowlist of the fields execution surface v1 defines:

```text
model, messages, stream, stream_options, tools, tool_choice,
response_format, reasoning_effort, reasoning, thinking, enable_thinking,
max_completion_tokens, max_tokens, temperature, top_p, stop, seed,
frequency_penalty, presence_penalty, parallel_tool_calls, user, metadata
```

Anything else is rejected as `unknown_parameter` — the surface never
silently ignores semantics it does not implement. Additional frozen
strictness:

- `messages` roles: `system`, `developer`, `user`, `assistant`, `tool`.
  Non-assistant messages require string `content`; `tool` messages
  require `tool_call_id`; only `assistant` messages may carry
  `tool_calls`. Multimodal content parts are rejected in v1 (text only).
- `n` is not accepted (per-choice fan-out is a later explicit sub-scope);
  `logprobs`, `top_logprobs`, `logit_bias`, `service_tier` and other
  unimplemented parameters are rejected the same way.
- `max_tokens` and `max_completion_tokens` are mutually exclusive.
- `stream_options` is accepted only with `stream: true` and supports only
  `include_usage`.
- `tools` accepts only `{"type": "function", ...}` definitions;
  `tool_choice` accepts `"none"`, `"auto"`, `"required"` or a forced
  `{"type": "function", "function": {"name": ...}}`.
- `response_format` accepts `"text"`, `"json_object"` and `"json_schema"`.
- `reasoning_effort` accepts `none`, `minimal`, `low`, `medium`, `high`,
  `xhigh`, `max`, `ultra` (the runtime-reported GPT-6-generation efforts
  entered additively with D-053).

### Reasoning-dialect normalization (#135)

Three additional forms are accepted ONLY through a bounded
normalization layer observed from real representative clients; they are
consumed at parse time and never forwarded, stored or re-interpreted:

- `reasoning: {"effort": "<effort>"}` — closed object (`effort` only);
  an effort-bearing form that must agree with `reasoning_effort`.
- `thinking: {"type": "enabled"|"disabled"}` — closed object (`type`
  only); a consistency assertion, never an effort selector.
- `enable_thinking: true|false` — a consistency assertion.

Semantics: the two effort-bearing forms must agree (`conflicting_reasoning_parameters`
on contradiction); a disabled flag alongside any explicit effort is a
conflict; contradictory flags conflict; enabled assertions with no
effort anywhere fail explicitly (`reasoning_effort_required`) — no
effort is ever guessed; disabled-only forms mean no reasoning requested;
unknown keys inside the objects are rejected as `unknown_parameter`.
Routing and adapters see exactly one canonical reasoning intent
(`reasoning_effort`).

### Capability validation before inference (D-043)

Request STRUCTURE yields compatibility requirements — never ranking
inputs and never an LLM request classifier:

| Request structure              | Matrix feature required   |
| ------------------------------ | ------------------------- |
| `tools` present / forced choice| `tool_calls`              |
| `role: "tool"` messages        | `tool_results`            |
| more than one message          | `roles_history`           |
| structured `response_format`   | `structured_output`       |
| `stream: true`                 | `streaming`               |
| `reasoning_effort` present     | `reasoning_controls`      |

`tool_calls`, `structured_output`, `streaming` and
`reasoning_controls` gate through the routing core's compatibility gates
(M02, `CompatibilityCell`); `roles_history` and `tool_results` are
admission-gated by the coordinator against the same matrix cells with the
same lookup semantics. A required feature with a missing, `UNKNOWN` or
`UNSUPPORTED` cell fails closed with an explicit 400 before any
inference; for non-pinned requests the routing core simply selects only
compatible targets and a no-solution result is an explicit 503.

The input context floor is a coarse conservative estimate
(`ceil(characters / 4)` across message content, tool-call arguments and
tool definitions). It is a requirement FLOOR for admission, never an
exact count and never a ranking signal.

## Execution lifecycle (D-043)

```text
admission -> bounded concurrency reservation -> dispatch -> streaming
lifecycle -> completion / cancellation -> usage accounting -> audit
```

Frozen rules, implemented by the coordinator:

- **Routing happens once.** Non-pinned requests call `route_request`
  exactly once and dispatch the selected target; pinned requests call
  `admit_pinned_target` (admission only, never competitive re-ranking).
  The coordinator never re-ranks, never re-routes and never substitutes a
  target — dispatch failure fails closed with an explicit `backend_failure`
  error and automatic cross-target failover does not exist.
- **The prompt-destination invariant holds from admission**, not from the
  first stream byte.
- **Concurrency is bounded.** Reservations are acquired after admission
  and before dispatch; exhaustion is an immediate 429
  (`concurrency_limit_reached`), never an unbounded queue.
- **No blind retries.** A dispatch with ambiguous outcome state produces
  `ambiguous_execution_state` (HTTP 500) and a `failed_ambiguous` audit
  record; nothing is re-dispatched. Exactly-once execution is not
  promised — one external request may cause several internal provider
  calls, and usage/accounting represents that honestly.
- **Cancellation propagates** to the selected backend where the channel
  supports it (a compatibility-matrix fact, reported per adapter). The
  server detects client disconnects during streaming (EOF on the request
  connection), sets the cancellation event and audits the request as
  `cancelled` with `client_disconnected`.
- **Bounded execution time.** Every dispatch runs under the admission
  deadline (`execution_time_limit_seconds`); exceeding it is a 408
  `execution_time_limit_exceeded`, audited as `timed_out`. A synchronous
  HTTP request is never silently converted into undefined-duration
  background work; there is no task scheduler.
- **Client tools stay client-side.** Requested tools return to the CLIENT
  as `tool_calls`; the router never executes them locally in any mode.

## Streaming

`stream: true` switches the response to `text/event-stream` with
`chat.completion.chunk` frames:

- headers and the first frame (assistant role delta) go out with the
  first adapter chunk, so pre-dispatch failures remain clean HTTP errors;
- normalized adapter chunks render as content deltas, reasoning deltas
  (`delta.reasoning_content`, D-064), complete tool-call deltas, a
  terminal finish chunk and — only when `stream_options.include_usage`
  is true — a final usage chunk with an empty `choices` array;
- the stream always ends with the `data: [DONE]` sentinel;
- a dispatch that completes without emitting chunks is rendered as a
  synthesized chunk sequence (well-formed framing, never silent
  non-streaming delivery);
- a failure after the stream has started is delivered as an in-band error
  frame followed by `[DONE]`.

## Responses

Non-streaming responses are `chat.completion` objects with the standard
`id`, `object`, `created`, `model`, `choices`, `usage` fields plus an
additive `x_scarcity_router` extension:

```json
{
  "x_scarcity_router": {
    "request_id": "chatcmpl-...",
    "decision_id": "rd-...",
    "usage_source": "provider_reported",
    "estimated_usage": null
  }
}
```

`usage` carries the best available token counts; `usage_source` says
which kind they are — `provider_reported`, `estimated`, `mixed` (a
multi-call fan-out with both kinds) or `unavailable` (numbers are then
structural zeros, never a fabricated "provider reported zero").
`estimated_usage` repeats the estimate alongside the reported numbers so
the two stay distinguishable (D-043).

### Reasoning output (D-064, #158)

When the selected backend's preset evidences a reasoning-output field
(DeepSeek/Z.ai `reasoning_content`, OpenRouter `reasoning`), the
response carries ONE additive client-facing member —
`choices[0].message.reasoning_content` (non-streaming) or
`choices[0].delta.reasoning_content` (streaming) — holding the
backend's opaque reasoning text verbatim. The member is present only
when reasoning was preserved; ordinary responses are byte-identical to
the pre-D-064 shape. Reasoning output is response-only: request
history keeps its closed message key set, so a client echoing
`reasoning_content` back receives the standard `unknown_parameter`
400. A reasoning shape a preset does not evidence (including OpenAI-,
Ollama- or generic-preset backends, and structured `reasoning_details`
everywhere) fails the execution closed with a typed translation error
instead of silently discarding the text.

## Error semantics (OpenAI-compatible vocabulary)

Errors use the OpenAI envelope
`{"error": {"message", "type", "param", "code"}}` with a closed
`type` vocabulary — never the machine-interface v1 codes:

| HTTP | type                  | typical codes                                      |
| ---- | --------------------- | -------------------------------------------------- |
| 400  | `invalid_request_error` | `unknown_parameter`, `invalid_json`, `invalid_pin_reference`, `pinned_model_not_bound`, `compatibility_unsupported`, `compatibility_unknown`, `context_length_exceeded`, `output_limit_exceeded`, `router_loop_detected`, `invalid_host`, `unsupported_reasoning_effort`, `reasoning_effort_required`, `ambiguous_logical_model`, `conflicting_reasoning_parameters`, `continuation_mismatch`, `tool_result_too_large` |
| 401  | `authentication_error`  | (no code)                                          |
| 403  | `permission_error`      | `unauthorized_target`, `spend_limit_exceeded`      |
| 404  | `not_found_error`       | `model_not_found`, `pin_target_not_found`, `continuation_not_found`, `continuation_expired` |
| 408  | `timeout_error`         | `execution_time_limit_exceeded`                    |
| 409  | `conflict_error`        | `continuation_already_resolved` (D-062 replay/double delivery) |
| 413  | `invalid_request_error` | `request_too_large`                                |
| 429  | `rate_limit_error`      | `concurrency_limit_reached`                        |
| 5xx  | `api_error`             | `no_eligible_target`, `adapter_unavailable`, `backend_failure`, `ambiguous_execution_state` |

Messages are safe structural text: never a traceback, never a provider
payload, never request content values.

## Audit trail (D-043)

Every request — executed or rejected — produces one audit record with the
frozen field set: request id, decision id, client id, routing profile,
routing policy version, registry revision and generation instant (state
snapshot identity/version), selected target, actually-executed target,
adapter name and version, start/end time, result status
(`completed`/`cancelled`/`failed`/`failed_ambiguous`/`timed_out`/
`rejected`), reason codes, provider-reported usage, estimated usage and
the internal call count. The default trail contains NO prompt or response
content — free text is not representable in the record. Retention is
bounded (in-memory, count- and age-bounded, frozen non-zero defaults);
the injectable `AuditSink` seam receives any later durable
implementation (D-041 leaves the embedded store to M09 if needed).

## Explicitly deferred sub-scopes

- `/v1/responses` (Responses API): a later explicit sub-scope with a
  documented supported subset; a fake surface that silently drops
  unsupported semantics is forbidden (D-043).
- `n > 1` per-choice sampling; `logprobs`/`top_logprobs`/`logit_bias`;
  multimodal content parts; provider-passthrough parameters beyond the
  allowlist above.
- Client-key issuance/rotation UX and the durable D-044 store (M09)
  have since landed, as have the real execution adapters for
  `server_direct_http` (M04), `worker_bridged` (M05) and the M06 Codex
  worker-local adapter (reached through `worker_bridged`), composed only
  from administrator configuration; the `local_app_adapter` channel
  itself remains unregistered. The default deployment still starts
  empty and answers honestly (empty model list, explicit no-target
  errors) until that configuration lands.
- Representative-client (OpenAI SDK, IDE) acceptance evidence and
  backend capability-matrix evidence are tracked under issue #88's
  evidence requirements and M10 (#95) end-to-end acceptance.
