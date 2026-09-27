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

## Harness compatibility contract (D-056)

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
| Tool calls returned to the client | `tool_calls` always return to the CLIENT; the router/worker never executes them (D-043); admitted per source only where the matrix evidences it | Implemented on evidenced server-direct channels (`tool_calls` PASS/PARTIAL cells); Codex worker source UNSUPPORTED — #137 |
| Client tool-result continuation | `role: "tool"` results with `tool_call_id` transported back into the backend's continuation | Implemented on evidenced server-direct channels (`tool_results` PASS/PARTIAL cells); Codex worker source UNSUPPORTED — #137 |
| Structured output | `response_format` text/`json_object`/`json_schema`, matrix-gated | Implemented |
| Max output semantics | effective output ceiling = model ∩ channel ∩ administrator allowance; honest, visible, rejection-based | Architecture accepted (D-056); implementation #136 |
| Context capability | effective context = model ∩ channel; UNKNOWN never guessed | Metadata implemented (D-055); #136 completes enforcement |
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
is a per-source capability: server-direct OpenAI-compatible presets
already evidence `tool_calls`/`tool_results` (PASS/PARTIAL cells with
dated evidence), so tool-requiring requests execute there today. An
execution source that cannot implement the lifecycle — currently the
Codex worker source, whose evidence records both cells UNSUPPORTED — is
ineligible for tool-requiring requests, a limitation of that source
recorded in the compatibility matrix, never a limitation of the
architecture (#137).

### Effective capability (D-056; architecture for #136)

A route's executable capability is the intersection of four distinct
layers: logical model hard capabilities (catalog, provenance-bearing);
execution-source/channel capabilities — what the route evidences it can
carry (context ceiling, output ceiling, tool round trip, streaming,
cancellation); administrator policy/limits — authoritative ceilings that
only narrow; and client-request requirements, which may only narrow, never
expand (D-042). In short:

```text
effective capability = model capability
                     ∩ execution-channel capability
                     ∩ administrator allowance
```

A powerful model reached through a weaker execution channel exposes the
weaker effective capability for that route. UNKNOWN stays UNKNOWN: a
model-catalog maximum is never evidence that every execution source
provides it, and an unknown input yields an unknown effective value —
`null` in metadata, fail-closed in admission — never a guessed number. A
request routes only to a source whose evidenced capability satisfies the
full semantic request. D-055's `effective_context_limit_tokens` metadata
is the context-dimension precedent. How multiple routes with differing
per-route ceilings aggregate into one advertised model-level number is
explicitly #136 scope; this section fixes only the per-route intersection
rule, which #136 implements end to end.

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
   `unsupported_reasoning_effort` for a variant the identity does not
   offer, `reasoning_effort_required` when no effort is requested and
   several calibrated variants exist (a max-only family's single legal
   effort — `max` — is used, per D-054). A slug carried by two providers
   is `ambiguous_logical_model`.

`GET /v1/models` lists the configured aliases first, then the exposed
logical models: a calibrated model identity is exposed only when at
least one registered resource binds it. An empty deployment exposes
nothing; a retired model disappears; a registered-but-unavailable model
still appears and yields `no_eligible_target` on request (never a
misleading `model_not_found`). Restricted and unclassified models are
never exposed or resolvable. Every entry carries the additive
`x_scarcity_router` metadata block: `kind`
(`routing_alias`/`logical_model`), and for logical models `provider`,
`reasoning_efforts`, `effective_context_limit_tokens` (model hard
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
- `reasoning_effort` accepts `minimal`, `low`, `medium`, `high`,
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
- normalized adapter chunks render as content deltas, complete tool-call
  deltas, a terminal finish chunk and — only when
  `stream_options.include_usage` is true — a final usage chunk with an
  empty `choices` array;
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

## Error semantics (OpenAI-compatible vocabulary)

Errors use the OpenAI envelope
`{"error": {"message", "type", "param", "code"}}` with a closed
`type` vocabulary — never the machine-interface v1 codes:

| HTTP | type                  | typical codes                                      |
| ---- | --------------------- | -------------------------------------------------- |
| 400  | `invalid_request_error` | `unknown_parameter`, `invalid_json`, `invalid_pin_reference`, `pinned_model_not_bound`, `compatibility_unsupported`, `compatibility_unknown`, `context_length_exceeded`, `output_limit_exceeded`, `router_loop_detected`, `invalid_host`, `unsupported_reasoning_effort`, `reasoning_effort_required`, `ambiguous_logical_model`, `conflicting_reasoning_parameters` |
| 401  | `authentication_error`  | (no code)                                          |
| 403  | `permission_error`      | `unauthorized_target`, `spend_limit_exceeded`      |
| 404  | `not_found_error`       | `model_not_found`, `pin_target_not_found`          |
| 408  | `timeout_error`         | `execution_time_limit_exceeded`                    |
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
