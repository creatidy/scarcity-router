# Worker Protocol v1 (native worker transport)

This document is the authoritative contract for the Scarcity Router
worker protocol, implemented by M05 (#90). It defines the transport, the
versioning and negotiation rules, the closed message vocabulary, the
pairing/trust bootstrap (D-044), the attempt-identity and
reconnect-safety semantics (D-043), and the state-report path. Nothing
here changes selection, routing, capacity or provider semantics; those
remain owned by their existing authoritative documents.

- **Status:** Versioned contract, implemented ("worker protocol v1").
  The protocol negotiates its own version independently of every other
  contract family (D-043/D-045).
- **Implementation:** `scarcity_router/worker_protocol.py` (framing,
  messages, negotiation — shared by both ends),
  `scarcity_router/worker_endpoint.py` (server-side endpoint; composition
  seam + standalone entrypoint `python -m scarcity_router.worker_endpoint`),
  `scarcity_router/worker_bridged_adapter.py` (the M03 `worker_bridged`
  `ExecutionAdapter`), `scarcity_router/worker_client.py` (the native
  worker runtime; entrypoints `python -m scarcity_router.worker_client
  pair|run`), `scarcity_router/worker_identity_store.py` (server-side
  pairing/identity store and M09 administration seam),
  `scarcity_router/worker_local_store.py` (worker-side bounded state),
  `scarcity_router/worker_local_adapters.py` (worker-side allowlist and
  local adapter seam), `scarcity_router/worker_local_translation.py`
  (provisional loopback translation; the M04 shared translation core
  replaces it at the construction site).

## Topology and transport

```text
Scarcity Router Server  <── outbound TLS ──  Native Worker  ──  local resources
(listens; already a          one long-lived              (localhost-only
 network surface)            worker-initiated             Ollama, local
                             connection                   entitlements)
```

- **Worker-initiated outbound connection only.** The worker requires no
  inbound listening port, no firewall opening, no manual worker-IP
  configuration and no SSH path for normal operation (D-043). SSH is a
  way a user reaches a machine; it is never the Scarcity Router worker
  protocol. The worker source contains no `bind`/`listen` calls, and
  tests assert this structurally and behaviorally.
- **Transport decision (issue #90 evidence).** A0 permits "a simple
  versioned protocol over TLS/WSS"; the issue names both minimal RFC 6455
  WebSocket framing and plain length-prefixed TLS framing as acceptable.
  This implementation uses **length-prefixed JSON frames over verified
  TLS**. Rejected alternatives: WebSocket framing (HTTP upgrade, masking
  and fragmentation machinery a dedicated machine-to-machine channel
  never benefits from); gRPC/HTTP/2 (a new runtime dependency, unjustified
  under D-013). The protocol layer is deliberately transport-agnostic
  (any `recv_exact`/`send_all`/`close` triple) so tests exercise it over
  in-memory transports deterministically.
- **Verified TLS; `verify=false` does not exist.** The worker builds its
  TLS context with `ssl.create_default_context()` (certificate AND
  hostname verification; `srws://` origins). Plaintext (`srw://`) is
  refused for every non-loopback host — the bounded localhost exception
  for dev/tests only, mirroring the execution surface's rule (D-044).
  The server endpoint requires explicit TLS for any non-loopback bind.
- **Framing.** One frame is a 4-byte big-endian unsigned length followed
  by exactly that many bytes of UTF-8 JSON, at most
  `MAX_FRAME_BYTES = 16 MiB`. Payloads are parsed strictly (duplicate
  object keys and non-finite constants are rejected) and must be JSON
  objects with an exact key set per message type. Oversized, malformed
  or non-object frames are a fatal protocol error: the connection is
  closed, never resynchronized mid-stream.
- **No arbitrary-command channel exists.** The vocabulary below is
  CLOSED: there is no shell, SSH, exec, file-transfer or
  arbitrary-command message, and no extension path without a protocol
  version bump. Unknown `type` values, unknown keys and unsafe
  identifiers are fatal `malformed_message`/`unknown_message_type`
  errors. The strict parser leaves no place for a server to smuggle an
  executable path, shell command, environment variables, a filesystem
  root or subprocess flags into a worker.

## Versioning and negotiation

- `WORKER_PROTOCOL_VERSION = 1`. The worker's first frame (`hello` or
  `pair_request`) carries `supported_versions` (1..8 entries). The server
  selects the highest mutually supported version and echoes it in
  `hello_ack`/`pair_result` (`negotiated_version`).
- Disjoint version sets are an explicit, fatal
  `protocol_version_unsupported` on both ends: the worker stops (fail
  closed, no retry storm), the server closes the connection. Incompatible
  versions never fall back to guessing.

## Pairing, identity, rotation, revocation (D-044)

- **Bootstrap.** The administrator initiates pairing server-side
  (`WorkerIdentityStore.begin_pairing`, the seam M09's UI will drive) and
  receives a short-lived ONE-TIME code (default TTL 600 s, bounded
  backlog). The worker presents the code plus the server URL over a
  verified TLS connection (`pair_request`); on success it receives its
  PER-DEVICE identity — `worker_id` + credential — in `pair_result`, and
  persists it locally. No shared fleet secret, no manual PKI, no
  worker-IP configuration.
- **Codes are single-use and expiring.** Redemption is an atomic
  conditional update: a replayed or raced code is rejected with
  `pairing_code_used`; an old code with `pairing_code_expired`; an
  unknown code with `pairing_code_invalid`. Each is a distinct, fatal
  protocol error on the pairing connection.
- **Authentication.** Every subsequent connection authenticates with the
  per-device credential in `hello`. Unknown identity → `unauthorized`;
  revoked identity → `credential_revoked`; wrong credential →
  `unauthorized`. Failures are fatal and the worker stops instead of
  retrying (revocation and invalid-credential rejection therefore take
  effect WITHOUT redeployment).
- **Storage.** The server store (`worker_identity_store`) keeps only
  salted SHA-256 hashes of codes and credentials, in one SQLite database
  created inside a `0o700` directory with the file forced to `0o600`
  (the recorded D-044 permissioned-file fallback; SQLite chosen for
  atomic single-use code redemption under concurrent connections).
  The worker store (`worker_local_store`) keeps the device credential,
  server origin and allowlist in one SQLite database with the same
  permission posture. **Windows and WSL are different devices** and
  resolve different state directories by construction
  (`%LOCALAPPDATA%\scarcity-router\worker` vs the XDG data home
  `~/.local/share/scarcity-router/worker`); no credential or
  configuration directory is ever shared between them. Provider
  application credentials are never copied to the server or into these
  stores (Codex auth remains provider-managed, D-018).
- **Rotation.** An authenticated worker may send `rotate_credential`;
  the server issues a new per-device credential (`credential_rotated`),
  invalidates the old one immediately, and the worker persists it. The
  administrator seam can also rotate out of band
  (`WorkerAdminService.rotate_worker_credential`).
- **Revocation.** `WorkerAdminService.revoke_worker` (M09 seam) revokes
  the identity and closes its live sessions immediately; the next
  connection is rejected with `credential_revoked`.

## Message vocabulary (closed)

Worker → Server: `hello`, `pair_request`, `heartbeat`,
`state_report`, `execute_chunk`, `execute_result`,
`attempt_interrupted`, `rotate_credential`.

Server → Worker: `hello_ack`, `pair_result`, `heartbeat_ack`,
`state_report_ack`, `execute`, `cancel`, `credential_rotated`,
`error`.

- `hello`/`pair_request` carry the version list; `hello` additionally
  carries the per-device identity (over TLS-encrypted frames — credentials
  never appear in URLs or logs).
- `heartbeat{seq}` / `heartbeat_ack{seq}` implement liveness. The server
  advertises its expected interval in the handshake ack and closes
  sessions silent for more than 3 intervals; the worker adopts the
  advertised cadence. **Composition note:** the endpoint's automatic
  `enforce_liveness()` wiring awaits M09/M10 server composition — the
  capability exists and is tested deterministically (including the
  frozen-monotonic session-reaping path), but the current standalone
  entrypoint does not yet run a liveness monitor loop.
- `state_report{report}` carries ONE complete M01
  `WorkerStateReport` document (validated by the M01 contract, applied
  through `ResourceRegistry.apply_worker_report` — the single shared
  normalization; there is no second collector for worker-observed
  resources). The server answers `state_report_ack` on success or a
  non-fatal `error` naming the rejection reason; a rejected report
  changes nothing (M01 atomicity).
- `execute{request_id, attempt_id, adapter_id, deadline, call}` is the
  only execution-carrying message. `call` is the M03 `AdapterCall`
  vocabulary serialized exactly (identities, messages, tools, output
  ceiling, generation parameters) and strictly revalidated worker-side.
  `adapter_id` names a worker-local adapter; the worker enforces its own
  allowlist against it regardless of what the server asks.
- `execute_chunk{attempt_id, chunk}` carries one normalized M03
  `AdapterStreamChunk` (`text_delta`, `tool_call`, `finish`, `usage`).
- `execute_result{attempt_id, status, calls, message?, finish_reason?,
  note?}` is sent exactly once per attempt; `calls` carries the M03
  `CallObservation` documents so worker-reported usage stays honest and
  distinguishable end to end.
- `cancel{attempt_id}` propagates coordinator/client cancellation to one
  in-flight attempt.
- `attempt_interrupted{attempt_id}` is sent by a reconnecting worker for
  every attempt that was in flight (or whose result could not be
  delivered) when the connection died.
- `error{code, message, fatal}` carries the closed error vocabulary:
  `protocol_version_unsupported`, `unauthorized`, `credential_revoked`,
  `pairing_code_invalid`, `pairing_code_expired`, `pairing_code_used`,
  `pairing_unavailable`, `unknown_message_type`, `malformed_message`,
  `frame_too_large`, `attempt_unknown`, `internal_error`. Fatal errors
  end the session; non-fatal errors (a rejected report, a late result)
  leave it up.

## Attempt identity and reconnect safety (D-043)

- **Identity.** Every execution is identified by the pair
  (`request_id`, `attempt_id`); the attempt id is server-issued, unique
  per dispatch, and bounded by the safe-id grammar.
- **Exactly-once dispatch.** The server sends `execute` exactly once per
  attempt, and the M03 coordinator never retries a dispatch — so no
  reconnect, no failover and no worker restart can ever produce a
  duplicate execution of an already-started request. A network loss
  NEVER becomes an automatic re-execution.
- **Disconnect = ambiguity.** When a session dies, the endpoint resolves
  every in-flight attempt as `interrupted`; the adapter surface
  translates that into `AdapterAmbiguousError` (the coordinator reports
  `ambiguous_execution_state` and audits `failed_ambiguous`). If the
  execute frame could not be delivered deterministically, the outcome is
  likewise ambiguous. Nothing is re-dispatched.
- **Reconnect honesty.** On its next successful connection the worker
  reports `attempt_interrupted` for every lost attempt BEFORE anything
  else; the server records it (bounded log) and resolves any still-pending
  tracker as ambiguous. A late `execute_result` for an attempt the
  session no longer tracks is answered with a non-fatal
  `attempt_unknown` error and never attributed to another request.
- **Cancellation.** Coordinator/client cancellation reaches the worker
  as `cancel{attempt_id}`; the worker cancels its local adapter
  best-effort and reports the terminal state honestly (`cancelled`, or
  ambiguity if the connection died first). The worker also enforces the
  admission deadline locally (cancel at expiry) in addition to the
  server-side bound.
- **Reconnect is bounded.** The worker reconnects with capped
  exponential backoff and a bounded attempt budget; exhausting it stops
  the worker with an explicit diagnostic. Authentication, version and
  revocation failures stop the worker immediately.

## Local adapter isolation (D-044)

- **The allowlist is worker-side and unconditional.** Only adapters
  registered in the worker's own `LocalAdapterRegistry` can ever run;
  there is no runtime/protocol registration path. An `execute` naming an
  unregistered `adapter_id` is rejected with a typed
  `adapter_not_allowed` failure and the local adapter is never invoked —
  even if the server is compromised or misconfigured.
- **Typed messages only.** Local adapters consume and produce the M03
  normalized vocabulary; they receive no filesystem root, no environment
  variables, no shell, no client-supplied flags. No root/Administrator
  execution, no Docker socket, no arbitrary repository mounts.
- **Loopback only.** The production `LoopbackOllamaAdapter` reaches
  localhost-only OpenAI-compatible endpoints over plain loopback HTTP
  (the bounded D-044 localhost exception; no credentials attached, no
  TLS bypass — remote origins are refused at construction). Its
  OpenAI-compatible translation is behind the replaceable
  `LoopbackTranslation` seam: the M04 shared translation core slots in
  at the construction site (see the cross-workstream note in
  `worker_local_translation.py`). M05 ships only the thin transport
  invocation and a clearly-labelled provisional default.
- **Diagnostics hygiene.** Worker diagnostics are bounded, structured
  and redacted: exception TYPE names, safe reason codes and counts —
  never credential values, prompts, provider payloads or local paths.
  Local runtime logs are accounted for by the same rules (verified by
  M10 acceptance).

## Server composition

The endpoint is one composed surface of the single server component
(D-041): `WorkerEndpoint` binds the identity store, the M01 registry,
the session table and (optionally) the TCP/TLS listener;
`WorkerBridgedAdapter` registers into the M03 `AdapterRegistry` for the
`worker_bridged` channel and dispatches admitted calls through the
endpoint. A standalone/dev entrypoint
(`python -m scarcity_router.worker_endpoint --store ... [--tls-certfile
... --tls-keyfile ...]`) runs the endpoint alone; M09 provides the
administration UX over the typed seams
(`WorkerIdentityStore`, `WorkerAdminService`,
`WorkerEndpoint.revoke_worker`).
