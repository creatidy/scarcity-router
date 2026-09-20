# Control surface, web UX and diagnostics (server component)

This document is the authoritative contract for the M09 administration
surface of the execution-gateway server component (issue #94). It covers
the authenticated control API, the lightweight web UI, the durable store,
the shared diagnostics logic and the `doctor` command. Nothing here
changes selection, routing, capacity or provider semantics; the frozen
machine-interface v1 surfaces ([`docs/machine-interfaces.md`](machine-interfaces.md))
and the OpenAI-compatible execution surface v1
([`docs/execution-surface.md`](execution-surface.md)) are untouched
contracts served by the same one server process (D-041).

- **Status:** Versioned contract, implemented ("control surface v1").
  Server-internal shapes (store schema, admin error payloads, UI HTML)
  evolve additively; the machine-interface control endpoints reuse the
  frozen machine-interface v1 envelopes exactly (D-045 parity rule).
- **Implementation:** `scarcity_router/control_api.py` (control plane),
  `scarcity_router/control_errors.py` (shared error/vocabulary module),
  `scarcity_router/server_ui.py` (web UI), `scarcity_router/server_store.py`
  (durable store), `scarcity_router/server_config.py` (configuration
  model), `scarcity_router/server_composition.py` (configuration →
  execution-adapter composition), `scarcity_router/diagnostics.py`
  (shared diagnostics), `scarcity_router/control_server.py` (composition
  entry point: `python -m scarcity_router.control_server`). The M03
  execution server (`scarcity_router/gateway_server.py`) accepts an
  optional control-plane attachment and dispatches the paths this
  surface owns; the M05 worker-protocol endpoint is owned by the same
  control plane (one pairing system, `worker_identity_store.py` +
  `worker_endpoint.py`), with its listener enabled by the administrator
  through `--worker-listen-port` (off by default).

## One server, four surfaces (D-041)

One server process terminates all public surfaces; there is no separate
administration service:

| Surface | Paths | Identity class |
| --- | --- | --- |
| OpenAI-compatible execution (M03) | `GET /v1/models`, `POST /v1/chat/completions` | inference client (bearer key) |
| Machine-interface control (M08 parity) | `GET /v1/status`, `POST /v1/select`, `POST /v1/simulate` | inference client (bearer key) |
| Liveness (M10; D-028 semantics) | `GET /healthz` | none (liveness only, no data) |
| Control API (this document) | `/control/**` | administrator session (+ CSRF on mutations) |
| Web UI (this document) | `/`, `/admin/**` | administrator session (cookie) |
| Worker endpoint (M05, optional listener) | worker transport (own protocol, not HTTP) | worker (per-device credential) |

The identity classes are separate and never collapsed (D-044): an
inference-client key authorizes nothing under `/control/**` or `/admin/**`
(the UI redirects unauthenticated browsers to the login page; the JSON
API answers `401`), and an administrator session authorizes nothing under
`/v1/**`. Worker pairing redemption is not an HTTP endpoint at all: the
administrator ISSUES the one-time code here, and the code is redeemed
inside the M05 worker protocol's verified-TLS handshake (the D-044 trust
bootstrap is the protocol handshake itself).

The one unauthenticated path is `GET /healthz` (M10, issue #95): the
machine-interface v1 liveness path served with exactly its frozen D-028
semantics — `{"status": "ok"}`, no collector, no store read, no auth
data — so container orchestrators and service managers have an honest
liveness probe. D-045 records that the machine-interface v1 and
execution-surface path sets are disjoint and may share one listener;
liveness reveals nothing beyond "the process is serving".

## First-run state and administrator onboarding

A fresh deployment has NO administrator credential and NO client keys:
every inference request is explicitly unauthenticated (fail closed) and
every web path redirects to forced onboarding. Onboarding
(`POST /control/bootstrap/admin`, or the `/admin/onboarding` page) is
refused with `409` once an administrator exists. There is no default
password; the password (minimum 12 characters, explicit confirmation) is
stored only as a PBKDF2-HMAC-SHA256 verifier with a fresh per-instance
salt, and setting it revokes every existing session. Login issues a
random session token (stored only as a SHA-256 hash, fixed-lifetime) in
an `HttpOnly; SameSite=Strict` cookie (`Secure` on TLS); logout deletes
it. Mutations require the per-session CSRF token (derived from the
session token, stored only as a hash) via the `X-Scarcity-CSRF` header or
the `csrf` form field.

## Control API

Machine-interface control endpoints return exactly the frozen
machine-interface v1 envelopes (`{"schema_version": 1, ...}`) and the
closed `invalid_request`/`internal_error` error vocabulary; requests are
parsed with the shared `machine_api` parsers and executed through
`select_from_inputs`/`simulate_from_inputs`. They are therefore
wire-compatible with the M08 remote bridge client
(`scarcity_router.remote`) by construction, and remote operation remains
explicit-failure with no silent local fallback.

Administration endpoints (all under `/control/`, administrator session
required, CSRF on mutations):

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/control/bootstrap/admin` | POST | first-run administrator setup (409 afterwards) |
| `/control/session` | POST / DELETE | login / logout |
| `/control/state` | GET | server state summary (counts, versions, quota-safety note) |
| `/control/providers` | GET / POST | list / add provider endpoints (credential stored once, never returned) |
| `/control/providers/{id}` | DELETE | remove endpoint, its credential and unbind its resources |
| `/control/resources` | GET / POST | list (with ladder) / add execution resources |
| `/control/resources/{id}` | DELETE | remove a resource |
| `/control/resources/{id}/enabled` | POST | enable/disable (configuration kept, registry updated) |
| `/control/resources/{id}/connection-test` | POST | quota-free test over real seam data (configuration, worker connection state, M04 health probe when evidenced); never an inference request |
| `/control/resources/{id}/generation-test` | POST | real generation probe; requires `{"confirm": true}` AND a configured tester |
| `/control/aliases` | GET | list routing aliases |
| `/control/aliases/{alias}` | PUT / DELETE | bind/unbind an alias to an EXISTING task profile (D-042) |
| `/control/clients` | GET / POST | list clients / issue a key (plaintext returned exactly once) |
| `/control/clients/{id}` | DELETE | revoke a key (effective immediately, no redeployment) |
| `/control/workers` | GET | list worker devices (M05 pairing store) with real connection state |
| `/control/workers/pairing-codes` | POST | issue a one-time pairing code (shown once; redeemed over the M05 protocol handshake) |
| `/control/workers/{id}` | DELETE | revoke a worker identity (closes its live sessions) |
| `/control/workers/{id}/rotate` | POST | rotate the per-device credential (shown once) |
| `/control/diagnostics` | GET | the shared diagnostics report (below) |
| `/control/export` | GET | secret-free configuration export |

Client keys are stored only as SHA-256 hashes and compared in constant
time through the M03 `ClientKeyDirectory`; issuing a key returns the
plaintext exactly once and never persists it. Worker pairing codes and
per-device credentials follow a hash-only discipline too — in the M05
worker identity store (salted, peppered SHA-256), which is the ONE
pairing system; the server store keeps no worker pairing state.

## Adapter composition (configuration is the only source)

Execution adapters exist only as the M09 configuration composes them
(`scarcity_router/server_composition.py`): the M04 generic
OpenAI-compatible HTTP adapter is built from each `server_direct_http`
resource's bound provider endpoint (evidence-backed preset via
`adapter_id`, exact origin via `base_url`, credential read through the
store's dispatch-only reader — never from request content), and the M05
worker-bridged adapter from each `worker_bridged` resource's
worker binding plus its declared worker-local adapter id. A
configuration that names an unknown preset, an unparseable origin or an
unknown worker fails closed at save time with a remediation; a resource
whose credential is missing stays honestly unbound (diagnostics report
a remediation, dispatch refuses definitively). The default deployment
composes NOTHING — no adapters, no worker listener — exactly as M03
shipped.

## Durable store (D-041/D-044)

One embedded SQLite-class single-file store
(`server-state.sqlite3` in the server data directory,
`$(XDG_DATA_HOME or ~/.local/share)/scarcity-router/server/` by default;
directory `0o700`, file forced `0o600`) holds: the authoritative
configuration document, the administrator verifier, administrator
sessions, client key hashes, provider credentials (the sole bounded D-044
exception) and the bounded D-043 audit trail. Worker pairing state is
NOT in this store: the M05 worker identity store (`workers.sqlite3`
beside it) is the one pairing system, and the superseded M09-side
`worker_pairings` tables were dropped by the explicit schema version 2
migration (unrelated data untouched; see D-049). Writes are
transactional with `synchronous=FULL` (crash leaves the previous or the
new state, never a partial one). The schema is server-internal and
versioned (`schema_migrations`); migrations are explicit, ordered,
applied in one transaction each, idempotent on reopen, and a future store
version fails closed. No prompt or response content is ever stored.

The configuration document (`server_config.py`) is the SINGLE source of
truth: typed, fail-closed parsing (unknown keys, unsafe ids, wrong
versions rejected), validated on every write (including D-044
router-loop refusal of a router's own origin as a provider endpoint),
persisted in exactly one place, and secret-free by construction —
provider credentials live only in the dedicated store table and are
referenced by provider id, so export and diagnostics need no filtering
to be safe.

## Web UI

Server-rendered standard-library HTML with one inline stylesheet — no
frontend framework, no client-side build, no JavaScript requirement, no
new dependency (D-013 evidence: every flow is a form plus a table;
nothing justifies a framework). Flows in the issue's priority order:
forced first-run onboarding; provider endpoints and credentials
(evidence-backed preset ids); resources with enable/disable, the
acceptance ladder and the quota-free connection test; routing aliases;
client key issuance (one-time reveal) and revocation; worker pairing
administration over the M05 pairing store (one-time code reveal,
listing with real connection state, revocation, credential rotation
with one-time reveal); diagnostics; copyable client configuration
(base URL, alias names, key-referral — never a key itself). All dynamic
values are HTML-escaped; responses carry `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, a `default-src 'none'` CSP and
`Referrer-Policy: no-referrer`.

## Diagnostics and `doctor`

One shared implementation (`scarcity_router/diagnostics.py`) serves both
the `/control/diagnostics` endpoint and the additive
`scarcity-router doctor [--json] [--server-data-dir DIR]` CLI command
(realizing the long-deferred D-016 doctor concept). Diagnostics read
stored state only — no collector call, no adapter dispatch and no
inference request is ever made; the report says so explicitly. (The
separate connection-test endpoint above is the quota-free path that may
probe a configured provider's evidenced health endpoint.) Worker rows
show the M05 pairing store's state plus the live worker endpoint's real
connection state (connected now, last authentication stamp); an offline
`doctor` run honestly reports no connection claim.

Every configured resource is reported through the closed acceptance
ladder of issue #94 — `detected`, `authenticated`,
`protocol_compatible`, `available`, `eligible`, `promotion_confirmed` —
computed only from server-held facts (configuration, store presence
flags, registry observations, administrator constraints, paired
workers, registered adapter channels). The first unmet stage carries a
concrete remediation step. `promotion_confirmed` means a recorded
promotion observation covers the evaluation instant; it stays an
observation-level state and is never execution proof (D-039/D-042).
Errors are actionable where possible ("no stored credential: set the
provider credential through the providers page") and reports never
contain credentials, key material or hashes, tokens, prompt/response
content or raw provider payloads. A real generation test exists only as
the separately-confirmed control endpoint above.

## Versioning

The control surface's own shapes are versioned additively (`schema_version`
fields on the configuration document, diagnostics report and export);
the machine-interface control endpoints never drift from frozen
machine-interface v1. Store schema and configuration document versions
are server-internal and migrate explicitly.
