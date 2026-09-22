# Provider Collectors

## Shared Adapter Contract

Each supported provider adapter performs one small telemetry acquisition and maps
it to a normalized capacity snapshot. Acquisition is read-only except for the
one bounded, owner-approved exception recorded in `docs/decisions.md` (D-018):
after the evidenced OpenAI app-server `-32603` rate-limits failure the
collector may request exactly one provider-managed credential refresh and
retry the read once. It must:

- discover/configure its source without copying secrets where practical;
- validate authentication source, endpoint scheme and exact host before use;
- parse into the normalized model without leaking raw provider types inward;
- preserve all relevant quota windows and reset times;
- return explicit health/failure status and safe diagnostics;
- carry retrieval time, safe plan metadata when known and source mechanism;
  account identifiers are not part of the capacity snapshot;
- emit the semantic capacity scope (`scope_id`) of every validated window
  from adapter evidence, and keep the diagnostic `window_id` separate;
- have redacted fixtures, parser tests and contract tests;
- treat new fields tolerantly and changed required semantics conservatively;
- never print, return or persist credentials.

Collector failures are isolated. `status` should still report a healthy source
when another source is `auth_required`, `schema_changed` or `unknown`.

## OpenAI Through Codex App-Server

The proven mechanism launches a locally available `codex app-server`, speaks
JSONL, sends `initialize`, sends the `initialized` notification, and calls
`account/rateLimits/read`. It obtains subscription rate limits without a model
prompt. The generated tag's notification method is exactly `initialized`, and
the read request omits `params` because its generated option is empty.

Implementation requirements:

- discover a compatible Codex binary explicitly and report the selected binary
  and protocol/version safely;
- supervise subprocess lifetime, timeouts, malformed JSONL and stderr without
  leaking sensitive content;
- identify windows using `windowDurationMins` and validated fields, not the
  `primary`/`secondary` position alone;
- validate `usedPercent`, preserve `resetsAt`, `planType` and reached status;
- require explicit current-backend `ordinaryUsageAllowed: true` before
  reporting usable ordinary quota pairs; false, null or missing permission is
  unknown, while a missing/null `spendControlReached` is clear only under that
  explicit permission;
- recognize `accountId`, `rateLimitUpsell` and `normalModelSlug` at the
  provider boundary without exposing them; a non-null upsell remains an
  upstream recovery blocker, but its presentation content is not part of
  normalized output;
- never fall back to browser-cookie scraping as an incidental convenience;
- fail with a clear unsupported/schema status if protocol behavior changes.

Status (M1): the collector is implemented and fixture-tested in
`scarcity_router/providers/openai_codex.py` (pure parser
`parse_codex_rate_limits_result` and the JSONL message classifier) and
`scarcity_router/providers/openai_codex_acquisition.py`
(`collect_openai_codex_capacity`). It performs deterministic read-only discovery
of the supported Codex installation, bounded app-server supervision, strict
JSONL validation, safe failure mapping, terminate/kill cleanup and — only on
the evidenced rate-limits `-32603` internal error — the single bounded
provider-managed auth refresh and one retry of D-018. The parser validates the
complete evidenced response envelope across both supported schema generations,
typed credit and spend states, reset-credit summaries, additional metered
buckets and backend blockers. Unrepresentable states degrade to `unknown`
without inventing quota. Every emitted window carries the capacity contract
v3 semantic scope: the validated `limitId` (`"codex"` for the main snapshot,
the validated bucket key for each additional bucket), independently of period
semantics; `limitName` and `normalModelSlug` never become scope identity and
the diagnostic `<limitId>:<slot>` `window_id` stays separate
(`docs/decisions.md` D-023).

The current upstream app-server schema also defines `ordinaryUsageAllowed`,
`accountId`, `rateLimitUpsell` and `normalModelSlug`; these fields are
explicitly handled at the adapter edge without expanding the normalized
contract or weakening the
unknown-structured-field rule. Current upstream semantics, the
two-generation compatibility rule and the supplemental-telemetry principle
are recorded in `docs/decisions.md` (U-011, D-019). Live OpenAI collection
returns healthy normalized windows with usable percentage pairs;
supplemental provider state that the normalized contract does not expose
(credits, additional
buckets, unavailable optional blocker signals) never invalidates the
independently validated quota facts, while explicit provider blockers
degrade honestly.

Supported discovery is exactly the VS Code ChatGPT extension layout documented
in `docs/decisions.md` (U-001): non-symlink `openai.chatgpt-*` directories under
`~/.vscode/extensions` or `~/.vscode-server/extensions`, on currently supported
Linux x86-64 hosts, with a validated `codex-package.json` and executable. No
installation maps to `unavailable`; an installation whose layout cannot be
validated maps to `unsupported`.

The provisional `uv run python -m scarcity_router status` command composes this
collector with the Z.ai normalized snapshot without exposing discovery paths or
versions. The automated suite contains no live-account test and all transport
tests use synthetic JSONL process fakes and fixtures under
`tests/fixtures/openai-codex-appserver/`.

## Z.ai Coding Plan

The proven mechanism reads the existing Kilo provider entry identified as
`zai-coding-plan` (credential type `api`) and makes:

```text
GET https://api.z.ai/api/monitor/usage/quota/limit
Authorization: <existing credential>
```

The credential is transient input only. Before attaching it, the adapter must
require HTTPS and the exact expected host. Endpoint overrides must pass an
equally strict allowlist; arbitrary URLs are forbidden.

The observed response contains a plan level and a list of typed limits. In the
observed/current schema:

```text
unit=3, number=5 -> five-hour token window
unit=6, number=1 -> weekly token window
```

These are validated adapter mappings, not universal constants. Unknown units or
combinations must preserve safe metadata and yield unknown semantics rather than
being guessed. Array position is not semantic.

Status (M1): the response parser is implemented and fixture-tested in
`scarcity_router/providers/zai.py` (`parse_zai_quota_response`), and the secure
production acquisition shell is implemented and unit-tested in
`scarcity_router/providers/zai_acquisition.py` (`collect_zai_capacity`). It
performs strict credential discovery, fixed HTTPS destination validation, one
redirect-free bounded GET and safe failure mapping to the normalized statuses.
Every window of an evidenced known limit type carries the capacity contract
v3 semantic scope `coding_plan` — including a known type with an unrecognized
`(unit, number)` period — while an unevidenced provider type keeps the scope
unknown and its raw type text never becomes a scope (`docs/decisions.md`
D-023).

Each observed window carries `nextResetTime`, a 13-digit epoch-millisecond value,
and `percentage` is the used percentage. Both are provider evidence, not a
permanent contract; the authoritative record and fail-safe fixtures live in
`docs/poc-evidence.md` and `tests/fixtures/zai-coding-plan/`.

The currently observed Kilo auth location is `~/.local/share/kilo/auth.json`.
Discovery reads only the bounded auth file and selects only the
`zai-coding-plan` entry with the evidenced `type == "api"` and non-empty `key`
shape. It must never dump the file or return other entries. No second long-lived
token store is created.

The provisional status command composes this collector with the OpenAI
normalized snapshot. The automated suite contains no live credential-dependent
integration test; transport tests use mocked fakes and synthetic secrets.

## Execution Adapters (Execution-Gateway Program)

The execution-gateway program (D-040; A0 = issue #85, module issues
M03–M07) extends the same provider-edge discipline from telemetry to
execution. The rules below govern every execution adapter; the module map
and contracts are in [`docs/architecture.md`](architecture.md).

- **One inference implementation per provider, two transports at most.** A
  provider's execution logic exists once in its adapter; it is reached
  either server-direct (HTTP reachable) or worker-bridged (localhost-only).
  There are never two Ollama implementations, and no duplicate collectors
  for a provider/account exist merely because execution was added — the
  existing Codex and Z.ai collectors remain the single telemetry source.
- **Provider differences stop at the adapter edge.** The execution
  coordinator never parses provider payloads; adapters validate schema and
  semantics, map errors, fail closed on drift and report capabilities
  honestly through the OpenAI compatibility matrix (D-043) — `UNKNOWN` and
  `UNSUPPORTED` never pass. An agentic CLI backend is not automatically an
  OpenAI-compatible backend; concatenating messages into a text prompt is
  not sufficient compatibility.
- **Configuration is administrator-owned.** Provider URLs and credentials
  come from administrator configuration (M09), never from client request
  content; outbound HTTP uses verified TLS, credentials are bound to
  configured exact origins, cross-origin redirects carrying
  `Authorization` are rejected, and plain-HTTP localhost is a bounded
  explicit exception only where justified (e.g. loopback Ollama)
  (D-044).
- **The generic OpenAI-compatible HTTP adapter (M04)** serves configurable
  providers with evidence-based differences — presets for at least OpenAI,
  DeepSeek, OpenRouter and Z.ai — and never claims full compatibility where
  features differ. The Z.ai preset is the vendor-documented
  OpenAI-compatible Coding Plan endpoint and is the supported
  subscription-backed Z.ai execution channel (D-047): the ZCode desktop
  application is not an M04 backend, and the M07 NO-GO does not remove
  Z.ai HTTP/API support. PAYG and Coding-Plan/entitlement channels of the
  same vendor stay distinct resources with distinct entitlements and pools
  (D-042). Ollama is direct HTTP when network-accessible and
  worker-bridged when localhost-only; no automatic model downloads, GPU
  driver installation or GPU lifecycle management.
- **Local CLI/app adapters (M06 Codex)** run through the worker
  (or server where reachable), reuse the existing discovery knowledge
  (U-001, D-019), respect the D-018 provider-managed auth boundary
  unchanged, and are bounded by the local-adapter isolation rules of
  D-044. Open uncertainties are registered, not guessed: U-012 (Codex).
  The M07 ZCode execution adapter was cancelled by owner decision (D-047)
  after Stage-1 evidence found no official supported programmatic surface;
  U-013 is resolved and the evidence is preserved in
  [`docs/zcode-adapter-stage1-evidence.md`](zcode-adapter-stage1-evidence.md).
- **Contract tests:** every execution adapter ships redacted fixtures,
  parser/protocol tests and compatibility-matrix evidence with dated
  versions; provider drift disables the affected adapter safely while the
  rest of the router keeps working.

### M04 status: generic OpenAI-compatible HTTP adapter (issue #89, 2026-09-20)

Implemented as ONE generic adapter (`scarcity_router/providers/openai_http_adapter.py`)
serving the `server_direct_http` channel through evidence-based presets
(`scarcity_router/providers/openai_http_presets.py`): OpenAI API,
DeepSeek, OpenRouter, Z.ai Coding Plan, Ollama (network-reachable) and a
generic administrator-configured OpenAI-compatible endpoint. There are no
per-provider gateways: provider differences live only in the typed
translation policies of each preset, and every unevidenced request feature
is explicitly refused (never silently dropped or forwarded on a guess).

- **One implementation, two transports.** The wire translation core
  (`scarcity_router/providers/openai_http_core.py`) is the single
  OpenAI-compatible semantic implementation and is import-clean of the
  coordinator and of any transport. The worker-bridged path (M05,
  integrated) invokes the same core worker-side through the narrow
  loopback adaptation (`scarcity_router/worker_local_translation.py`)
  and relays the normalized chunks and call observations through the
  worker protocol; the server composes execution adapters only from
  administrator configuration (`scarcity_router/server_composition.py`),
  and API-only operation works without a worker (the honest empty
  default registers no adapters at all).
- **Administrator configuration.** Provider origins and credentials come
  only from typed `ResourceBinding` configuration keyed by registry
  `resource_id` (built by the M09 composition seam; credentials flow
  only from the server store's dispatch-only reader); request content
  can never supply or alter an origin, a path or a credential. The Z.ai
  Coding Plan preset is subscription-backed (`subscription_included`)
  and stays distinct from the vendor's PAYG platform API (a generic
  configuration, `payg_metered`) — matching model names never merge
  access classes (D-042).
- **Ollama.** Reached through the same generic adapter; model discovery
  (`GET /api/tags`) and health/readiness (`GET /api/version`) are native,
  read-only endpoints that never consume inference quota; explicit
  administrator capability configuration applies (default matrix cells
  are the documented-evidence defaults, narrowed or raised only with the
  administrator's own dated evidence via M09). No automatic model
  downloads, no GPU driver installation, no GPU lifecycle management.
- **Readiness probing (D-050).** Presets with an evidenced native health
  endpoint (Ollama: `GET /api/version`) probe it. Every other preset is
  probed with a GET on its DOCUMENTED chat endpoint path — no
  undocumented provider endpoint is invented and no inference quota is
  consumed. Any well-formed HTTP answer records health `ok` with the
  exact status in the note (availability is the fact being observed);
  credential rejection records `auth_required`, a missing documented
  path records `schema_changed`, and transport failures record
  `unavailable`. The composed server probes due server-direct resources
  on its request loop and the control connection test records its probe
  result (see `docs/control-surface.md`).
- **Outbound security (D-044).** Verified TLS only (`verify=false` does
  not exist); plain HTTP only for explicit loopback origins; redirects
  never followed (Authorization never crosses origins); bounded response
  bodies and streams under the dispatch deadline; the
  `X-Scarcity-Router-Gateway` marker is stamped on every outbound request
  and any response carrying it is refused (router → router is forbidden).
- **Evidence.** Dated compatibility evidence and per-preset default
  matrix cells live in `scarcity_router/providers/openai_http_evidence.py`
  (provider documentation retrieved 2026-09-20; see the preset evidence
  records for the exact URLs and retrieval caveats). Facts that could not
  be re-verified stay `PARTIAL`/`UNKNOWN`, and the generic preset is
  `UNKNOWN` in every cell (fail closed) until the administrator supplies
  evidence.

### M06 status: Codex execution adapter (issue #91 Stage 2, 2026-09-20)

Implemented as ONE worker-local adapter
(`scarcity_router/worker_codex_adapter.py`, allowlist id `codex`) that
drives the official `codex app-server` subprocess over stdio JSONL. It is
reached only through the M05 worker bridge (channel `worker_bridged`,
resource `local_adapter_id: "codex"`, D-049); there is no
server-to-local-Codex path and no second worker protocol. Evidence base:
[`docs/codex-adapter-stage1-evidence.md`](codex-adapter-stage1-evidence.md)
(Stage 1 plus the dated Stage 2 implementation section); protocol shapes
follow the version-pinned generated schemas at tag `rust-v0.155.1`.

- **Discovery (deterministic, bounded, read-only).** Order: the
  administrator pin (`--codex-bin PATH`, a regular executable non-symlink
  file), then the official standalone CLI install (`codex` on `PATH`),
  then the U-001 VS Code ChatGPT extension layout (the collector's
  read-only discovery, reused by import). Desktop-bundled Codex is NOT
  discovered (Stage 1: reachability UNKNOWN). Discovery alone never makes
  a resource eligible.
- **Version contract.** `<binary> --version` must parse as
  `codex-cli X.Y.Z…` at or above `0.154.0` (the evidenced supported
  generation is the 0.154/0.155 series). Older, unparseable or failed
  probes leave the resource ineligible (`version_unsupported` /
  `version_unparseable` / `version_probe_failed`).
- **Authentication (D-018 boundary absolute).** Tokens are never read,
  copied, serialized or logged. Auth state is verified with the official
  `account/read` method and reduced to a typed verdict; account emails and
  plan labels are discarded. Only `account.type == "chatgpt"` is eligible
  for execution. API-key auth is explicitly NOT an execution resource
  (PAYG conversion is forbidden by owner policy); the remediation is
  always the official `codex login` — never token extraction or
  `auth.json` copying. The execution adapter is STRICTLY READ-ONLY against
  the provider: it performs NO provider-state mutation of any kind. The
  single owner-approved mutation exception (the bounded managed-auth
  refresh after the evidenced `-32603` shape) belongs to the OpenAI
  capacity collector alone, in its `account/rateLimits/read` phase
  (docs/decisions.md D-018, unamended) — it is deliberately NOT extended
  to this adapter. On any `account/read` protocol error the adapter fails
  closed to `auth_unverified` with the official interactive sign-in
  remediation (browser or device code); the error's free text is never
  read (issue #101 audit: it cannot prove an auth condition anyway).
- **Isolation profile (all official mechanisms, adapter-composed, never
  client-influenced).** A dedicated controlled `CODEX_HOME` is created
  and owned by the adapter under the worker's state directory (tree
  `0o700`, generated minimal `config.toml` — no `mcp_servers`, no
  plugins/apps/connectors, no trust defaults; never the user's
  `~/.codex`), and the `initialize` handshake verifies the runtime
  adopted it (`controlled_home_not_adopted` otherwise). Threads are
  ephemeral; each attempt gets a fresh scratch working directory
  (`0o700`, removed after the call); `thread/start` pins
  `sandbox: "workspace-write"` and `approvalPolicy: "never"`;
  `turn/start` pins a detailed `sandboxPolicy`
  (`{"type": "workspaceWrite", "writableRoots": [<scratch>],
  "networkAccess": false}`); approval server-requests are answered with
  the `cancel` decision (deny + stop the turn), never accepted;
  `dangerFullAccess` is never sent. The child environment is
  adapter-constructed (`CODEX_HOME`, `PATH`, `HOME` only) and runs in its
  own process group (whole-group cleanup at shutdown, reaping proven
  before the call returns). Sandbox prerequisites are probed:
  Linux/WSL2 requires bubblewrap on `PATH`
  (`sandbox_prerequisite_missing` otherwise); WSL1 is ineligible
  (`wsl1_unsupported`); Windows-native and other platforms are honestly
  ineligible (`platform_not_evidenced`) on this code base's Linux
  verification evidence.
- **Forbidden surfaces are never called.** `thread/shellCommand`,
  `process/*`, `fs/*`, `dynamicTools` + `item/tool/call`, the
  `chatgptAuthTokens` login mode and every config-mutating method
  (`config/value/write`, `config/batchWrite`, `marketplace/*`,
  `skills/config/write`, `externalAgentConfig/import`). The
  `initialize` handshake omits `experimentalApi` (stable surface only).
  Tool-bearing requests (message `tool` role, `tool_calls`, or a
  non-empty `tools` list) are rejected BEFORE anything executes —
  client tools return to clients (D-043) and the experimental
  dynamic-tools flow is not enabled.
- **Mapping (D-043 matrix obligations).** `system` →
  `baseInstructions` (joined when repeated); `developer` →
  `developerInstructions`; prior `user`/`assistant` messages →
  `thread/inject_items` Responses API items (structure preserved, never
  collapsed); the final user message → `turn/start` input (a
  conversation not ending with a user message is rejected before
  execution). `response_format: json_schema` →
  `turn/start {outputSchema}` after object/size/depth validation;
  schema-less `json_object` is explicitly rejected, never silently
  dropped. `max_output_tokens` and non-empty `generation_params` are
  likewise REJECTED before execution (`request_parameters_unsupported`):
  no evidenced stable-surface mapping exists, and silently dropping
  requested semantics is forbidden (the M04 refuse-not-drop precedent).
  Within the resource's configured physical model, the selected slug and
  `reasoning_effort` are verified against `model/list`
  (`supportedReasoningEfforts`) BEFORE execution and pinned per turn —
  the runtime can never silently choose another model or effort. An
  unknown `turn/completed` status fails closed;
  `failed` turns map to SAFE notes derived only from the documented
  `codexErrorInfo` vocabulary (free-text error bodies, which can carry
  prompt content, are never read). Streaming maps
  `item/agentMessage/delta` to `text_delta` chunks (bounded per delta
  and cumulative); `turn/completed {status: "completed"}` → `stop` +
  the assembled message; `interrupted` → cancelled. Cancellation
  observes the cancel event or the absolute deadline, sends exactly one
  bounded `turn/interrupt` and reports `cancelled` — never completed
  after a confirmed cancellation. Usage comes only from
  `thread/tokenUsage/updated` (`inputTokens`/`outputTokens`); absent
  usage stays absent. Exactly one Codex invocation happens per
  dispatched call: a process exit before the turn starts is a
  definitive failure; a process loss afterwards is reported with the
  `unknown` call-observation status so the ambiguity survives to the
  audit trail — never retried, never a second process, never a
  fallback.
- **Physical model identity (D-042).** The configured resource represents
  ONE physical model: the worker requires an explicit `--codex-model
  SLUG` with `--allow-codex` (validated as a safe id; never guessed from
  the installed Codex default, the account state or `model/list`'s
  first/default entry — `model/list` is only runtime verification).
  `codex` is the execution SURFACE (`local_adapter_id: "codex"`), never
  a model. Before any execution — before any thread, turn or even
  process spawn — the adapter rejects a call whose selected
  `ModelIdentity` provider/model does not match the physical model its
  configured resource represents (typed `model_not_served` rejection; no
  substitution, no silent mapping). Resource-level identity is
  variant-less: the routing core's binding rule (exact
  `(provider, model)`) binds every calibrated variant of the physical
  model, so a `gpt-5.6-sol` resource serves the shipped calibrated
  `medium`/`high` identities directly. Server-side, the composition
  builds the compatibility-matrix cells for each configured Codex
  resource from the reviewed M06 evidence
  (`scarcity_router/codex_worker_evidence.py`; dated Stage-2 matrix) —
  built-in evidence is the ceiling; configuration cannot raise it.
- **Resource snapshots.** The worker reports an honest eligibility
  observation per codex resource (discovery → version → sandbox →
  controlled home → bounded `account/read` verdict) in the closed
  capacity v3 vocabulary (`ok`, or `unavailable` / `unsupported` /
  `auth_required` / `schema_changed` / `unknown` with the status-level
  diagnostic). The `auth_required` snapshot is reachable here on
  STRUCTURED `account.type` evidence (missing or API-key auth), unlike
  the conflated rate-limits telemetry surface (D-018 amendment).
  Binary installed is not eligible; a successful probe never proves
  promotional eligibility (D-042).
- **Remediation per installation type** (official steps only; never
  token copying — the credential is always provider-managed):
  - **Desktop-only Codex (macOS/Windows; Linux preview):** the desktop
    bundle is not externally drivable (UNKNOWN). Install the Codex CLI
    through the official installer and sign in once.
  - **CLI installation:** macOS/Linux
    `curl -fsSL https://chatgpt.com/codex/install.sh | sh`; Windows
    `irm https://chatgpt.com/codex/install.ps1 | iex` (PowerShell);
    alternatives: `npm install -g @openai/codex`,
    `brew install --cask codex`. Then `codex login` (browser or device
    code). Diagnosis: `codex doctor`.
  - **VS Code integration:** install the `openai.chatgpt` extension and
    sign in once; the extension vendors a `codex` binary the adapter
    discovers read-only.
  - **Controlled-home sign-in (required for execution):** the adapter's
    `CODEX_HOME` starts empty by design (no user `~/.codex` reuse).
    Sign in ONCE against it with the official flow:
    `CODEX_HOME=<worker state dir>/codex/codex-home codex login`.
    Thereafter Codex refreshes its own provider-managed credential during
    use; the adapter itself never triggers or performs a refresh (strictly
    read-only), so a stale session surfaces as the typed `auth_unverified`
    rejection whose remediation is re-running the official login.
  - **Windows:** prefer the native Windows agent profile with the
    `elevated` sandbox; the adapter currently reports Windows-native
    hosts ineligible (`platform_not_evidenced`) until that profile is
    behaviorally verified (M10).
  - **WSL:** run the CLI installer inside WSL2; bubblewrap is required
    (`sudo apt install bubblewrap` / `sudo dnf install bubblewrap`;
    mind the documented Ubuntu 24.04 AppArmor userns note). WSL1 is
    unsupported since codex 0.115. Keep `CODEX_HOME` on the Linux side;
    the Windows `%USERPROFILE%\.codex` and the WSL `~/.codex` are
    separate devices and never assumed shared.
- **Worker registration.** `python -m scarcity_router.worker_client run
  --allow-codex --codex-model <physical-model-slug> --resource
  <registry-resource-id> [--codex-bin PATH]` (`--codex-resource` when it
  runs alongside `--allow-ollama`; `--codex-model` is REQUIRED with
  `--allow-codex`). The resource is registered with provider `openai`,
  the configured physical model slug (e.g. `gpt-5.6-sol`), entitlement
  `subscription_included`, channel `worker_bridged`; the server binds it
  through the resource's `local_adapter_id: "codex"` and the
  administrator's resource registration must name the SAME physical
  model (a mismatch is rejected fail-closed on either side). A worker
  may register codex alongside ollama; both are gated by the worker's
  unconditional allowlist.

## Later Providers

Claude subscription is the highest-priority later collector because it would
materially expand usefulness. It must not delay the initial two-provider scope
and must be preceded by an auth, security and maintenance evaluation. Other
providers are added based on user demand, stable telemetry, maintenance burden
and routing value, not a provider-count target.

## Contract-Test Expectations

For each adapter, freeze minimal redacted provider-shaped fixtures and assert:

- successful normalization;
- all active windows survive normalization;
- window semantics are classified only from validated evidence;
- missing/unknown semantics remain unknown;
- every validated window carries its evidenced semantic `scope_id`, the
  diagnostic `window_id` stays separate, and no output consumer parses
  either identifier;
- used/remaining validation and reset preservation;
- error/status mapping;
- no credential-shaped value appears in output, diagnostics or snapshots;
- provider changes do not crash the core;
- failures from one provider do not suppress the other provider's snapshot.

Live integration checks may complement fixtures, but tests must not require or
record the owner's credentials.
