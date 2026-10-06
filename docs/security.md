# Security model

## Security objective

In its default **recommendation-only mode**, Scarcity Router handles access to
subscription telemetry, not model traffic. Its small scope is a security
feature: it has no reason to receive prompts, source code or repository
contents. The main assets are existing provider credentials, account
metadata, quota state and the integrity of routing policy.

The optional **execution gateway** (D-040) deliberately moves the server
component onto the model-request path and onto the LAN. That extension is
governed by its own explicit security decision (D-044) and the threat model
in [Execution-gateway security architecture](#execution-gateway-security-architecture-d-044);
every rule in the sections above remains in force for recommendation-only
mode and for every collector. Nothing in this document is relaxed by the
gateway — the gateway adds boundaries, it never subtracts them.

## Creatidy Ownership and Staged Native-Agent Boundary

D-068 keeps Kernel/harness/host responsible for workspace and harness
lifecycle, Router for its source/call permissions and admission. Client
shell/edit/read tools return to the harness; backend-native tools are a
separate permission domain, not permission to execute arbitrary client tools.
Compatibility must declare actual native permissions/side effects and
unobservable helper calls, not assume every harness reveals or pins them.

Current D-063 is a specific preserved exception: the official ZCode CLI
executes coding work under `--mode edit` in an administrator-authorized
project. That integrated authority conflicts with the target inference/
workspace boundary (U-014 / #180). Do not claim compliance from a passing
adapter test, silently downgrade its working mode, extend its repository
access or allow uncoordinated internal/external editors in one workspace.
The owner disposition for #180 (D-073, 2026-10-06) adopts staged migration:
Router is inference/resource backend only, with Kernel/harness owning workspaces,
tools and coding-agent lifecycle. Preserve this legacy exception until equivalent
supported replacement and explicit migration acceptance, not a silent removal.
The administrator must issue Kernel-controlled inference clients an
`inference_only: true` grant. It authorizes only the current server-direct HTTP
transport; native worker routes, including scratch-based Codex, are refused
before execution. Client tools still return to the harness. Missing/false grants
preserve legacy semantics; client names/metadata never supply authority, so
existing keys must not be assumed migrated by their label alone.

The legacy project is canonicalized and bound to its device/inode at adapter
construction, rechecked for readiness and before/after non-inference probes.
Deletion, symlink substitution or replacement is not a renewed grant. This is
not an atomic filesystem fence, native-tool isolation proof or external-editor
lease. No new general sandbox or concurrent Kernel editor is authorized.
Future native inference support must evidence its own controls; absence of
client tools or changing `edit` to `build` cannot establish it. See the
[authority/negative matrix](architecture.md#tools-and-staged-native-agent-migration).
Security-critical delivery requires ordinary independent review plus exact-base/
HEAD Daybreak review directly through Codex before completion/merge under
`docs/llm-operating-policy.md`; this is not an implementation eligibility gate.

MI receives public knowledge only, never private quota, prompts or credentials.
Console consumes authorized Router state/commands, not its database; reuse
existing admin UI. Pins are non-secret identity/provenance, not authorization
credentials or reservations. Source entitlement/dispatched target do not
prove observed free subscription use or physical model identity (#179/#184).
An unsupported output cap cannot guarantee cost, unknown usage is not zero,
and reset/credit/paid overflow actions require separate authority.

## Trust boundaries

- Existing local credential/tool stores are outside the broker and remain the
  source of truth.
- Provider collectors are the only components allowed to use credentials.
- Normalized capacity, selector, catalog and interfaces are credential-free.
- CLI/REST/MCP consumers are untrusted with respect to secrets; they receive
  only normalized safe output.
- Remote provider endpoints are trusted only after scheme and exact-host
  validation.
- Execution-gateway additions (D-044): inference clients are untrusted
  request sources authenticated by client API keys; workers are
  semi-trusted per-device identities that authenticate outbound and enforce
  local allowlists; the administrator identity is the sole trust root for
  configuration, pairing and credential issuance; the server's durable store
  and audit trail are protected assets; prompt/response content in transit
  and in memory is sensitive but is never persisted by default.
- Development CI executes untrusted pull-request code under a frozen minimal
  trust model (D-046): PR jobs receive no provider or publishing secrets,
  publishing never runs from pull-request events, and release authority is
  separate from development CI
  ([`docs/release-engineering.md`](release-engineering.md)).

## Absolute invariants

Never:

- print, log, serialize, return or expose an auth token, cookie or secret;
- include real credentials in fixtures, snapshots, telemetry, analytics,
  exceptions, command arguments, process listings or commits;
- send credentials, source code or repository data to any model, and never
  send a prompt anywhere except to the one authorized execution target
  selected for that explicitly authorized request (execution-gateway mode
  only, D-040/D-042 — never to a different model, a telemetry path or an
  analytics sink);
- ask an LLM or agent to inspect a credential value;
- copy unrelated browser profile or authentication contents;
- create another long-lived credential store by default; the
  execution-gateway server component's explicit, bounded store is the sole
  recorded exception (D-044);
- attach a credential to an arbitrary user-provided URL;
- issue model prompts as part of quota collection;
- mutate provider quota/account state from a collector, except the single
  owner-approved exception of D-018: after the evidenced OpenAI app-server
  `-32603` rate-limits failure, the collector may trigger exactly one
  provider-managed credential refresh through the official app-server
  managed-auth flow and retry the read once. The token itself is never
  received, read, stored or exposed by Scarcity Router, and no login,
  logout, account change or direct auth endpoint is ever performed.

Credential files must be read as narrowly as possible. Prefer an authenticated
local protocol such as Codex app-server over extracting browser state.

## Network controls

- REST binds to `127.0.0.1` by default, never `0.0.0.0` implicitly.
- Because this unauthenticated interface is loopback-only, every request must
  carry exactly one `Host` header whose value is `127.0.0.1` or
  `127.0.0.1:<actual-bound-port>`. Rejecting missing, duplicate or foreign
  values before route dispatch closes the DNS-rebinding gap in this boundary.
- Stdio MCP is preferred for local agent integration. It is local process IPC:
  the official SDK creates no network listener, so Host, CORS and remote MCP
  authentication concerns do not apply to this transport. The MCP client owns
  the child-process lifecycle.
- Authorization-bearing requests require HTTPS and an exact approved provider
  hostname. Validate before constructing/sending the authenticated request.
- Redirects must not carry Authorization to an unapproved origin. The safest
  initial behavior is to reject cross-origin redirects.
- Endpoint overrides are disabled or allowlisted strictly; a syntactically
  valid URL is insufficient.
- Remote exposure, CORS relaxation or LAN binding is a future explicit decision
  requiring authentication and threat analysis.

## Secret handling

Use secrets transiently and keep them in the narrowest scope. Do not cache token
values. Avoid passing them in command-line arguments or environment variables
when a safer in-process read/header is available. Do not include raw response
headers in logs.

Logging and diagnostics use allowlisted structured fields and stable reason
codes. Redaction is defense in depth, not permission to log arbitrary payloads.
Captured subprocess output must be reviewed because an upstream tool may emit
sensitive data unexpectedly.

If explicit credential configuration becomes necessary, prefer OS-native secure
storage. A permissioned file is a considered fallback, never a world-readable
default. Its format and migration require a recorded decision.

## Minimal filesystem access

OpenAI collection should interact with the chosen Codex process rather than
browser profiles. Z.ai discovery should parse only the configured Kilo auth file
and select only `zai-coding-plan`; it must not display or return other entries.

The default user configuration (D-036) is the one product-owned write
outside the package itself: `scarcity-router` creates
`$(XDG_CONFIG_HOME or ~/.config)/scarcity-router/selector-policy.json`
(directory `0o700`, file `0o600`) when it is missing, provisioning the
audited `examples/selector-policy.json` content verbatim. The file never
stores credentials, tokens, cookies or provider endpoints, and an existing
file is never silently overwritten (`install-config --force` is the only
replace path). Read back, it feeds the selector policy only; a broken file
is a loud configuration failure, never a silent fall-through.

## Public-interface data

REST, MCP, CLI and dashboard may expose:

- provider identifier and plan type;
- normalized capacity windows and reset times;
- health/status and safe diagnostic reason;
- selected model, alternatives and explanation;
- catalog/policy version.

They must not expose credential paths by default, raw Authorization material,
unredacted raw responses or unrelated account data. A verbose/debug mode does
not waive these rules.

MCP-specific input is limited to the frozen logical selection and simulation
fields. MCP must never accept credentials, provider endpoints, catalog paths or
model-execution instructions. Its tools may invoke the existing read-only
capacity collectors and the bounded D-018 provider-managed authentication
recovery, but they never execute inference, redeem replenishment, write
provider configuration or dispatch a selected model. Stdout is reserved for
MCP framing; normal diagnostics, if ever needed, go to stderr and are quiet,
safe and free of request arguments, provider payloads and local paths.

## Testing requirements

- Use synthetic or manually redacted fixtures with conspicuous fake secrets.
- Assert fake secret markers do not appear in normalized output, logs, errors or
  interface responses.
- Test malicious endpoint overrides, HTTP downgrade, redirect to another host,
  malformed auth files, unexpected subprocess output and schema drift.
- Run secret scanning before release and ensure examples contain no real values.
- Verify local-only default binding.

## Incident behavior

If a secret may have been printed, stored, committed or sent to an unapproved
host, stop the collector, preserve only non-secret diagnostic evidence, notify
the user and recommend provider-appropriate revocation/rotation. Do not repeat
the suspected value while reporting the incident.

If a schema changes, fail closed for that collector: return `schema_changed` or
`unknown`; do not guess a healthy quota. Other collectors and the core continue.

## Execution-gateway security architecture (D-044)

This section is the threat model and security boundary set for the optional
execution gateway (D-040; program A0, issue #85). It extends — never relaxes —
everything above. Implementation is distributed to the module issues: ingress
limits and isolation to M03 (#88), outbound provider HTTP to M04 (#89),
worker transport and local-adapter isolation to M05 (#90)/M06 (#91)/M07
(#92), administration to M09 (#94), and end-to-end security acceptance to
M10 (#95) — where security failures are program blockers, not documentation
notes.

### Identities and authentication

- **Three separate identity classes** with separate credentials and
  permissions: **administrator** (configuration, provider credentials,
  pairing, key issuance, revocation), **inference client** (execution
  requests only, never administration), **worker** (one per-device identity,
  execute/state-report scope only).
- No shared default password; credentials are issued on first-use paths with
  per-instance randomness; **revocation** exists for every class and takes
  effect without redeployment.
- **No bearer secrets in URLs.** Credentials travel in headers or message
  authentication fields, never in query strings, path segments or logs.
- **Verified TLS everywhere; `verify=false` does not exist as an option.**
  Non-loopback listeners require TLS with verified certificates; workers
  verify the server identity on their outbound connection. Plain HTTP is a
  SUPPORTED LOCAL TRANSPORT, not an insecure-debug escape hatch (D-056):
  loopback origins are a normal supported configuration for local clients
  (including a container sharing the host network namespace), and the
  bounded same-host container tier below is architecturally permitted;
  everything that leaves the host trust boundary requires TLS, and
  credential-bearing requests to non-local origins are never plaintext.
  Never `verify=false`, never certificate-verification bypass, never
  trust-on-first-use, never a silent HTTPS→HTTP fallback; a scheme change
  is always explicit.
- **Simple trust bootstrap:** the administrator starts pairing in the server
  UI, receives a short-lived one-time code, enters it plus the server URL on
  the worker, and the worker receives a per-device credential with rotation.
  No manual worker-IP configuration, no shared fleet secret, no certificate
  signing ceremony for ordinary users.

### Credential storage (explicit exception)

The server component keeps provider endpoints and credentials **only** from
administrator configuration — never from client request content. OS-native
secure storage is preferred where available; a permissioned file store
(`0o700` directory, `0o600` files, never world-readable) is the recorded
fallback, with format and migration recorded when chosen. Stored values are
never logged, exported (M09 exports are secret-free), returned through any
API, or attached to requests to non-configured origins. This is the sole
exception to the recommendation-mode transient-credential rule and exists
only inside the server's store. Worker-side application credentials stay on
the worker host whenever possible; Codex authentication remains entirely
provider-managed (D-018 unchanged).

### Admission limits

Enforced before any dispatch: request-body size, context/output size,
per-client concurrency, execution time and spending limits — all
administrator-configurable with safe defaults (D-043 request contract). A
client override may narrow its own limits, never expand authorization,
provider access or spending ceilings (D-042).

### Network and origin discipline

- **SSRF protection:** provider origins are fixed administrator
  configuration; clients never supply URLs, endpoints or credentials.
- **Credentials bound to configured origins:** a credential is attached only
  to its configured exact host; `Authorization` is never forwarded across
  unsafe or cross-origin redirects — cross-origin redirects are rejected.
- **Router-loop protection:** configuring the server's own execution origin
  (or another router instance's endpoint) as a provider backend is refused,
  and gateway-originated traffic is identifiable on ingress so
  router → router → router chains fail loudly instead of looping.

### Client-transport tiers (D-056)

Plain HTTP is a SUPPORTED LOCAL TRANSPORT for client traffic to the
composed HTTP listener, not an insecure-debug escape hatch; TLS remains
required whenever a connection crosses a host trust boundary. The tiers,
from [`docs/decisions.md`](decisions.md) D-056:

- **A. Loopback — supported, implemented.** `http://127.0.0.1:<port>`,
  `http://[::1]:<port>`, `http://localhost:<port>` are normal supported
  configurations for a locally running harness, a locally running Scarcity
  Router, and a container sharing the host network namespace (native
  Linux `--network host` shares the host loopback; for example ZCode →
  `http://127.0.0.1:8787/v1` → a local Scarcity Router container). No
  certificates are required. A standard Docker bridge publish of a
  loopback port (`-p 127.0.0.1:8787:8787`) is NOT currently supported in
  plaintext — the published traffic reaches the container on a
  non-loopback interface, where a plaintext bind is refused — and is part
  of the accepted container tier in B, implemented under #139.
- **B. Same-host container transport — architecturally permitted, NOT
  implemented; implementation assigned to existing child #139.** Plaintext
  HTTP between containers on one physical host is permitted only where
  deployment isolation makes the transport host-local and non-routable
  from external networks: a dedicated Docker network, an `internal: true`
  Docker network, and a host publication bound ONLY to loopback (for
  example `-p 127.0.0.1:8787:8787`) as an allowed target of the explicit
  local-container mode. Plaintext publication reachable from LAN/WAN is
  forbidden. The implementation may need the server to bind a non-loopback
  address INSIDE the container in that explicit mode (today's code refuses
  any non-loopback plaintext bind); what is forbidden is a general/default
  non-loopback plaintext bind with no deployment-boundary enforcement —
  #139's implementation must prove the deployment remains host-local and
  fail closed when the bounded mode is not explicitly selected. The tier
  applies to the ENTIRE COMPOSED HTTP LISTENER, never a
  per-surface mix: the server component serves execution,
  machine-interface, control and administration endpoints on one listener
  (D-041 one-process composition), so a host-local trust exception that
  covers inference traffic covers that whole listener; splitting an
  execution-only plaintext listener off the composed server would be a
  separate explicit decision, not part of this tier. This is never solved
  by globally allowing a non-loopback `0.0.0.0` plaintext listener. The
  implementation must keep explicitly-local container transport
  meaningfully distinct from arbitrary non-loopback exposure — an
  explicit deployment/local-transport mode may be defined, but it must be
  difficult to accidentally turn a LAN-facing endpoint into plaintext.
  Until that mode lands, non-loopback binds require TLS exactly as today.
- **C. Cross-host / LAN / VPN / remote — TLS REQUIRED (implemented).**
  Windows-to-LAN, laptop-to-server over VPN, any other machine, and
  internet-facing deployments use HTTPS. Plain HTTP over a VPN is not
  automatically local merely because the VPN itself may encrypt traffic;
  the cross-host policy stays TLS-by-default and predictable.
- **D. Remote TLS UX.** The preferred normal path for remote/self-hosted
  deployments is a public FQDN with a publicly trusted certificate and
  automated renewal (for example
  `https://scarcity-router.creatidy.com:8787`); a private CA is a supported
  advanced option and is never required for ordinary localhost operation
  (issue #139 implements the lifecycle). Never introduced in any tier:
  `verify=false`, certificate-verification bypass, trust-on-first-use, or
  silent HTTPS→HTTP fallback; a scheme change is explicit.
- **E. Worker transport — unchanged.** These tiers govern CLIENT traffic
  to the composed HTTP listener as a whole: in an explicit host-local mode
  that listener may carry execution, machine, control and administration
  HTTP over plaintext within the accepted local trust boundary, while
  authentication, authorization, CSRF and credential separation remain
  fully enforced — only transport confidentiality changes inside that
  bounded boundary, and cross-host traffic remains TLS-required. The
  worker protocol keeps its separate authentication and transport security
  semantics (verified TLS `srws://`, loopback-plaintext only as the
  bounded dev/test exception recorded in
  [`docs/worker-protocol.md`](worker-protocol.md)) unless a separate
  explicit decision changes them.

### Local runtime and adapter isolation

The target restrictions below remain the normal adapter boundary. D-063's
explicit authorized-project coding lane is the current exception/conflict,
not an implicit relaxation for other adapters or shared Kernel workspaces.
Its existing allowlists/provider-auth/TLS/path validation must be preserved
until #180 receives explicit migration authority.

- **No generic shell API:** no `/shell`, `/ssh` or arbitrary-command
  endpoint exists in any contract (server, worker or adapter); SSH is a way
  a user reaches a machine, not a router protocol.
- **Session, filesystem and tool isolation** for local adapter invocation:
  no automatic access to user projects, arbitrary filesystem paths, shell,
  global MCP configuration, plugins, browser integrations or unrelated
  conversation history; a read-only sandbox alone is not presumed
  sufficient isolation. If required isolation cannot be provided, only the
  affected adapter/mode is marked ineligible; the rest of the router keeps
  working.
- **No root/Administrator execution by default; no Docker socket mounting;
  no arbitrary repository mounting; no uncontrolled client-supplied
  subprocess flags or environment variables.**
- The worker enforces its **local adapter allowlist even if the server
  requests something else**.

### Data, logging and audit

- **No prompt/response logging by default**; no secret logging; diagnostics
  stay redacted and allowlisted. Prompt and response content exists only in
  transit/memory for the authorized request.
- The **audit trail is the minimal D-043 metadata set** (request id,
  decision id, client/profile identity, routing-policy version, state
  snapshot identity/version, selected target, actually-executed target,
  adapter version, start/end time, result status, provider-reported usage,
  estimated usage where applicable) with **bounded retention** and no
  prompt/response contents by default.
- **Logs created by local runtimes** on worker hosts (adapters, local
  inference servers) are accounted for by the same hygiene rules — M06/M07
  and M10 must verify them, not only server logs.

### Terms and subscription scope

Provider subscription and promotional terms are respected (U-009); execution
through an adapter is gated by the D-039 eligibility contract and never
proves promotional eligibility. Several apps owned by one user do not
automatically imply the right to share one personal subscription with
multiple independent users: the server is scoped to one user's own
resources — it is not a resale, team or multi-tenant quota pool.

## Out of scope for M1

Multi-user authentication, remote service exposure, centralized credential
management, analytics and organization-wide audit are not needed for the M1
collector milestone.
