# Codex execution-adapter Stage 1 evidence (M06, issue #91)

This document records the Stage 1 technical verification for the Codex
execution adapter (M06). It is **evidence only**: no Codex execution adapter
is implemented here (that is Stage 2, which depends on A0 #85 and M05).
Every material conclusion carries its evidence class, retrieval date,
tested/local version where applicable, and confidence. Absence of evidence is
recorded as UNKNOWN, never inferred. No credential, token, account email or
personal identifier appears anywhere in this document; all probe output was
sanitized to structure before recording.

- **Date of evidence:** 2026-09-19 (UTC)
- **Scope frozen by:** issue #91 Stage 1 list, U-012, D-043 compatibility
  matrix dimensions, D-044 local-adapter isolation, D-039 eligibility
- **Evidence classes used:** `official-doc` (developers.openai.com Codex
  documentation), `official-source` (openai/codex GitHub repository, tagged
  sources — evidence of implementation, not of documented stability),
  `local-probe` (read-only/structural probes on this machine),
  `secondary` (community/unofficial — none used)

## Method

### Sources consulted (all retrieved 2026-09-19)

Official documentation (`official-doc`), fetched as the pages' own Markdown
renderings (`.md` suffix, officially offered by the docs site):

- https://developers.openai.com/codex/app-server — the App Server protocol
  (transports, message schema, threads/turns/items, models, sandbox policy,
  approvals, auth endpoints, rate limits, experimental gating)
- https://developers.openai.com/codex/auth — sign-in methods, credential
  storage, headless/automation authentication
- https://developers.openai.com/codex/quickstart — App/IDE/CLI availability
  and first-run sign-in
- https://developers.openai.com/codex/cli — CLI install channels
- https://developers.openai.com/codex/app — ChatGPT desktop app (Codex host)
- https://developers.openai.com/codex/linux/linux-app — desktop app for
  Linux (preview): supported distributions/architectures
- https://developers.openai.com/codex/windows/windows-app — Windows desktop
  app, Windows-native vs WSL agent, CODEX_HOME sharing
- https://developers.openai.com/codex/windows/windows-sandbox — elevated /
  unelevated native sandbox modes, Windows version matrix
- https://developers.openai.com/codex/windows/wsl — WSL2 usage, WSL1 removal
- https://developers.openai.com/codex/ide — IDE extension (VS Code, Cursor,
  Windsurf, Insiders; Xcode/JetBrains integrations)
- https://developers.openai.com/codex/non-interactive-mode — `codex exec`
  automation flags, JSONL events, structured output, automation auth
- https://developers.openai.com/codex/sandboxing — sandbox prerequisites
  (Seatbelt / bubblewrap / Windows sandbox)
- https://developers.openai.com/codex/models — model choice per surface;
  current model slugs (`gpt-5.6-sol`, `gpt-6-astra`; GPT-5.5 retirement
  2026-10-14)
- https://developers.openai.com/codex/feature-maturity — official maturity
  vocabulary (under development / experimental / beta / stable / deprecated)
- https://developers.openai.com/codex/open-source — which components are
  open source (CLI, SDK, app-server sources; IDE extension and cloud are not)
- https://developers.openai.com/codex/changelog — official version history
  (Codex CLI 0.155.1 current; 0.154.0 series)
- https://developers.openai.com/codex/enterprise/access-tokens — Codex
  access tokens (ChatGPT Business/Enterprise; app-server-based automation)
- https://developers.openai.com/codex/config-file/config-reference —
  `CODEX_HOME`, `model_reasoning_effort`, `mcp_servers.*`,
  `requirements.toml`, `cli_auth_credentials_store`, managed restrictions
- https://developers.openai.com/codex/permission-modes — sandbox vs approvals

Official source (`official-source`), tag `rust-v0.155.1` (latest stable
release, published 2026-09-18) unless noted:

- https://github.com/openai/codex/releases — release list (0.155.1 stable;
  0.156.0-alpha.* prereleases, one published 2026-09-19)
- `codex-rs/app-server-protocol/schema/json/v2/` — 273 generated JSON Schema
  files for the v2 protocol; inspected
  `GetAccountRateLimitsResponse.json`, `TurnStartParams.json`,
  `ThreadStartParams.json`
- `codex-rs/app-server-protocol/src/lib.rs` and
  `codex-rs/app-server-protocol/src/experimental_api.rs` — the
  experimental-field gating machinery (`#[experimental(...)]` derive,
  `experimental_reason()`, message
  `"<reason> requires experimentalApi capability"`)

Local probes (`local-probe`), host: WSL2 Linux x86_64, user home
`/home/<user>`; performed 2026-09-19:

- P1 `which codex` — no `codex` on PATH (exit 1).
- P2 `ls ~/.vscode-server/extensions/openai.chatgpt-*` — one installation:
  extension `openai.chatgpt-26.908.40401-linux-x64` (newer than the PoC
  environment's `26.825.51511`).
- P3 extension layout: `bin/linux-x86_64/codex-package.json` present with
  `layoutVersion: 1`, `variant: "codex"`, `version: "0.154.0-alpha.6.2"`,
  `target: "x86_64-unknown-linux-musl"`, `entrypoint: "bin/codex"`; sibling
  `codex` is a static-pie ELF x86-64 executable.
- P4 `codex --version` → `codex-cli 0.154.0-alpha.6.2` (read-only, no auth).
- P5 bounded structure-only app-server handshake (`/tmp` driver, killed
  after 6 s; no account method, no turn, no inference): `initialize` →
  `initialized` → `model/list` over default stdio. Sanitized observed
  structure: `initialize` result object with string members
  `userAgent`, `codexHome`, `platformFamily`, `platformOs` (matches the
  collector's validated shape); unsolicited notifications interleaved
  between responses (`configWarning`, `remoteControl/status/changed` —
  method + params keys only recorded); `model/list` result `{"data": [...],
  "nextCursor": str}` with per-model fields including `id`, `model`,
  `displayName`, `hidden`, `isDefault`, `supportsPersonality`,
  `defaultReasoningEffort`, `supportedReasoningEfforts` (6 entries on the
  default model), `inputModalities`, plus additive members not in the
  official example (`additionalSpeedTiers`, `availabilityNux`,
  `defaultServiceTier`, `modelSpecialty`, `multiAgentVersion`,
  `serviceTiers`, `upgrade`, `upgradeInfo`).
- P6 `codex app-server --help`, `codex --help`, `codex login --help`,
  `codex exec --help` — flag/subcommand surface (details below).
- No live model inference was performed; no account/rateLimits read was
  re-captured (the existing collector's D-018/D-019 evidence stands); no
  credential-bearing file was read (`~/.codex/auth.json` existence noted
  only).

### Related repository evidence reused (not re-derived)

- `docs/poc-evidence.md` (2026-09-01/03/05): app-server JSONL framing,
  `GetAccountRateLimitsResponse` shape generations, `-32603` stale-auth
  discriminator, D-018 recovery oracle.
- `docs/decisions.md` D-018, D-019, D-039, D-042, D-043, D-044, U-001,
  U-010, U-011, U-012.

## Stage 1 checklist findings

Each item: finding, evidence (class + source + date), tested version where
applicable, confidence, and what remains unknown.

### 1. Runtime discovery

**Finding.** The U-001 VS Code ChatGPT extension discovery layout is
unchanged and re-confirmed on a current installation (P2/P3/P4, 2026-09-19):
`openai.chatgpt-<version>-<platform>` under `~/.vscode-server/extensions`
with `bin/<platform>/codex-package.json` (`layoutVersion: 1`,
`variant: "codex"`) beside a vendored static `codex` binary. Official CLI
install channels now exist and are documented (`official-doc`, CLI page):
standalone installer (`install.sh` / `install.ps1`), npm `@openai/codex`,
Homebrew cask — so PATH-discoverable CLI installs are officially supported
installation methods, but Scarcity Router discovery of them is not
implemented (U-001 residual (b)); the D-039 `SCARCITY_ROUTER_CODEX_BIN`
override already makes a pinned standalone install reachable today. The
Desktop app (macOS/Windows; Linux preview) bundles its own Codex agent;
no official documentation describes driving the Desktop bundle's binary
externally, so Desktop-only external app-server reachability is **UNKNOWN**.

**Version:** extension `26.908.40401`, `codex-cli 0.154.0-alpha.6.2`.
**Evidence:** local-probe (P1–P4); official-doc (CLI, app pages).
**Confidence:** high for the extension layout; medium for the official CLI
install surface; UNKNOWN for Desktop-bundle reachability.

### 2. Authentication prerequisites for unattended execution

**Finding.** Official auth modes (`official-doc`, auth + app-server pages):

- **API key** (`account/login/start type:"apiKey"`, CLI `--with-api-key`
  stdin, `CODEX_API_KEY` env for exec): billed at standard API usage rates —
  this is PAYG, which the owner's policy (D-039/M4.1) forbids for execution.
  OpenAI recommends API-key auth for CI/automation; that recommendation is
  recorded but is **not** the Scarcity Router execution path for
  subscription capacity.
- **ChatGPT managed** (`chatgpt` browser flow, `chatgptDeviceCode`
  device-code flow): Codex owns the OAuth flow, persists tokens, and
  refreshes them automatically during use. This is the subscription path.
- **ChatGPT external tokens** (`chatgptAuthTokens`, experimental, requires
  `experimentalApi`): the *host app* supplies and refreshes access tokens.
  This mode would make Scarcity Router a token holder/refreshor and is
  therefore **excluded** by the D-018 boundary, regardless of its
  experimental status.
- **Amazon Bedrock** (`account/read type:"amazonBedrock"`) — out of scope.

**Unattended paths on a consumer (Plus/Pro) subscription:** (a) one
interactive sign-in (browser or device code) whose cached credential Codex
then refreshes itself (`codex exec` "reuses saved CLI authentication by
default"; auth doc: "Codex refreshes tokens automatically during use before
they expire"); (b) the app-server `account/read {"refreshToken": true}`
managed refresh — the exact official mechanism D-018 already uses as
bounded recovery. **Enterprise access tokens** (Business/Enterprise
workspaces only) are the official token for trusted non-interactive
app-server automation with ChatGPT-managed entitlements, delivered via
`printenv CODEX_ACCESS_TOKEN | codex login --with-access-token` (stdin,
never argv). A manual `auth.json` copy fallback exists in the official docs
for CI; it is **excluded** here (issue #91 acceptance criterion: never
manual extraction/copying of authentication tokens; AGENTS.md security
invariants).

`account/read` reports `account.type` (`apiKey` | `chatgpt` |
`chatgptAuthTokens` | `amazonBedrock`) and `requiresOpenaiAuth` — an
official signal the execution side's own pre-call guard (D-039 §2) can use
to verify ChatGPT-backed auth before dispatch. `account/read` response
examples include the account `email`; any Stage 2 consumer must not retain
it (current collector behavior already discards account payloads).

**Version:** docs current 2026-09-19; CLI flags verified on
`0.154.0-alpha.6.2` (P6). **Evidence:** official-doc; local-probe (help
text). **Confidence:** high. **Unknown:** whether Codex's in-place cached
credential survives long unattended idle periods without any refresh
(the refresh oracle of D-018 applies per session; operational cadence is a
Stage 2 concern).

### 3. Desktop-only installation

**Finding.** The ChatGPT desktop app (macOS and Windows; Linux preview:
Ubuntu 24.04/26.04, Debian 13, Fedora 43/44, Arch, x64+ARM64) hosts Codex
as one of its agents and signs in with the ChatGPT account. Officially
documented remediation when the app-server/CLI is additionally needed:
install the Codex CLI through its official installer and sign in once —
on Windows the desktop app and native CLI **share the same
`%USERPROFILE%\.codex`** (auth and config; official-doc windows-app page),
so a Desktop-only user gains an execution-capable app-server without any
second credential flow. On macOS/Linux the desktop app and CLI use the
user's `CODEX_HOME`/`~/.codex` (sharing not separately re-documented for
Linux preview — confidence medium). Driving the Desktop app's own bundled
agent binary externally is not documented (**UNKNOWN**).

**Evidence:** official-doc (app, windows-app, linux-app, cli pages).
**Confidence:** high for the Windows sharing statement and install steps;
medium for Linux-preview home sharing; UNKNOWN for external driving of the
desktop bundle.

### 4. CLI installation

**Finding.** Official channels (`official-doc`, CLI page): macOS/Linux
`curl -fsSL https://chatgpt.com/codex/install.sh | sh`; Windows
`powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"`;
npm `npm install -g @openai/codex`; Homebrew `brew install --cask codex`;
unattended installs documented via `CODEX_NON_INTERACTIVE=1` (quickstart).
First run offers ChatGPT or API-key sign-in. `codex doctor` ("Diagnose
local Codex installation, config, auth, and runtime health", P6) is the
official remediation/diagnosis entry point. `codex app-server` ships in
the same binary (default stdio transport). The `app-server` subcommand
self-labels `[experimental]` in `--help` at `0.154.0-alpha.6.2` even
though the protocol is now officially documented — see the stability
section.

**Evidence:** official-doc; local-probe P6. **Confidence:** high.

### 5. VS Code integration

**Finding.** The official IDE extension (`openai.chatgpt`, marketplace)
supports VS Code, VS Code Insiders, Cursor and Windsurf (Xcode and
JetBrains have separate integrations). The extension is **not open source**
(official open-source page) and communicates with the same app-server
implementation internally ("the interface Codex uses to power rich clients
(for example, the Codex VS Code extension)" — app-server page). The
extension vendors its own `codex` binary in the U-001 discovery layout
(re-confirmed P2–P4). In WSL, VS Code uses the Remote/WSL extension and
`~/.vscode-server/extensions` — the directly evidenced U-001 environment.
The extension is therefore a discovery source for an execution-capable
binary, not a separately required component.

**Evidence:** official-doc (ide, app-server, open-source pages);
local-probe P2–P4. **Confidence:** high.

### 6. Windows versus WSL profiles

**Finding.** Officially distinct profiles (`official-doc`, windows-app,
windows-sandbox, wsl, sandboxing pages):

- **Native Windows agent:** PowerShell + native Windows sandbox with two
  modes — `elevated` (preferred: dedicated lower-privilege sandbox users,
  filesystem permission boundaries, firewall rules, private desktop by
  default) and `unelevated` (fallback: restricted token + ACL boundaries,
  weaker network isolation); configurable via
  `[windows] sandbox = "elevated"|"unelevated"` in `config.toml` and
  enterprise-enforceable via `requirements.toml`
  `allowed_sandbox_implementations`. Windows 11 recommended; Windows 10
  ≥1809 best-effort (ConPTY). App-server exposes
  `windowsSandbox/setupStart` + `windowsSandbox/setupCompleted`.
- **WSL2 agent:** Codex runs inside the Linux environment with the Linux
  (bubblewrap) sandbox. WSL1 was supported through Codex 0.114; from 0.115
  the Linux sandbox moved to bubblewrap and **WSL1 is no longer supported**.
  bubblewrap is a stated prerequisite on Linux/WSL2 (or the bundled helper
  with unprivileged user namespaces).
- **Separate CODEX_HOME by default:** the Windows app uses
  `%USERPROFILE%\.codex`; a CLI inside WSL uses the Linux `~/.codex`; they
  do **not** share config, cached auth, or session history unless synced or
  `CODEX_HOME` is pointed across the boundary (official remediation:
  `export CODEX_HOME=/mnt/c/Users/<user>/.codex`).
- VS Code from WSL runs the extension inside the WSL remote
  (`~/.vscode-server` layout).

**Stage 1 conclusion:** Windows-native and WSL are separate profiles for
discovery (distinct binaries, CODEX_HOME stores and sandbox mechanisms),
each holding its own provider-managed copy of the same account credential.
Per D-042, multiple installations of one subscription are not multiple
quota pools and pool sharing is not assumed in either direction without
verification; the capacity observation (`account/rateLimits/read`) is
account-scoped, so N installations of one account must normalize to one
scope, not N.

**Version:** statements current for the 0.154/0.155 series. **Evidence:**
official-doc. **Confidence:** high.

### 7. Model selection

**Finding.** Officially supported through the app-server: `model/list`
(paginated; `includeHidden` for full catalog; per-model
`supportedReasoningEfforts`, `defaultReasoningEffort`, `inputModalities`,
`isDefault`, `upgrade`, `hidden`), `thread/start {model}`, and per-turn
`turn/start {model}` (documented: per-turn overrides "become the defaults
for later turns on the same thread"). CLI equivalents: `-m/--model`
(interactive and `codex exec`). Config: `models.new_thread.model`.
Live probe P5 confirmed `model/list` behavior on
`0.154.0-alpha.6.2`. Current catalog slugs per the models page:
`gpt-5.6-sol`, `gpt-6-astra` (GPT-5.5 retires 2026-10-14); catalog
membership is provider-side data and must never be hard-coded by the
adapter (D-019 generation-aware discipline).

**Evidence:** official-doc + official-source (`ModelListResponse.json`)
+ local-probe P5. **Confidence:** high.

### 8. Reasoning/effort selection

**Finding.** `turn/start {effort}` (schema type `ReasoningEffort`) and
`turn/start {summary}` (reasoning summary); `model/list`
`supportedReasoningEfforts`/`defaultReasoningEffort` drive valid choices;
config keys `model_reasoning_effort` and
`models.new_thread.model_reasoning_effort` (`xhigh` documented as
model-dependent). Probe P5 observed six supported efforts on the default
model (values not recorded — no need). Selection is therefore a documented
stable surface.

**Evidence:** official-doc; official-source (`TurnStartParams.json`);
local-probe P5. **Confidence:** high.

### 9. Quota scope of executed work (relative to D-039)

**Finding.** Executed turns consume the account's quota according to the
active auth mode: under ChatGPT-managed auth, turns consume the ChatGPT
plan's Codex allowance, observed through `account/rateLimits/read`
(documented officially with the exact envelope our collector validates:
single-bucket `rateLimits` view, `rateLimitsByLimitId` multi-bucket view,
`rateLimitResetCredits` summary, per-bucket `planType`/`credits`/
`rateLimitReachedType` members — matching U-010/U-011/D-019's validated
mapping), plus `account/rateLimits/updated` notifications. Under API-key
auth, usage is billed as standard API usage (PAYG) — the state D-039's
owner policy forbids. `account/usage/read` (token-activity summaries)
explicitly "requires authentication backed by Codex services"; API-key-only
and Bedrock auth do not work — additional confirmation that subscription
telemetry is ChatGPT-scoped. The rate-limits result carries no auth-mode
member (D-039 §2 unchanged); `account/read`'s `account.type` is the
official pre-call auth-mode signal for the execution-side guard.

**Boundary kept:** this describes which quota pool executed work draws
from; it is **not** evidence of promotional or subscription eligibility,
and execution success never proves eligibility (issue #91 acceptance
criteria; D-042). The eligibility contract (`ExecutionEligibility`,
`scarcity_router/eligibility.py`) remains the only routing gate; no code
changes were made in Stage 1.

**Version:** docs 2026-09-19; schema tag `rust-v0.155.1`. **Evidence:**
official-doc; official-source. **Confidence:** high.

### 10. Conversation roles and history

**Finding.** The app-server models conversations as **thread → turn →
item** with documented lifecycle methods: `thread/start`, `thread/resume`,
`thread/fork` (with `lastTurnId` and `ephemeral` forks), `thread/read`
(`includeTurns`), `thread/list` (cursor pagination; `sourceKinds` filter
defaulting to interactive sources `cli`/`vscode`, with `appServer`,
`exec`, `subAgent*`, `unknown` as other values), `thread/archive`/
`delete`/`unarchive`, `thread/unsubscribe`, `thread/compact/start`.
`thread/inject_items` appends raw **Responses API items** (e.g.
`{"type":"message","role":"assistant","content":[{"type":"output_text",
...}]}`) to a loaded thread's model-visible history without starting a
turn — the official bridge for external role/history content.
`turn/start.input` accepts `text`, `image`, and `localImage` items; tool
results arrive via `toolOutput`. Thread-start params include
`baseInstructions` and `developerInstructions` (schema, 0.155.1) — the
mapping surface for OpenAI `system`/`developer` roles. Item types include
`userMessage`, `agentMessage` (with optional `phase` `commentary`/
`final_answer`), `reasoning`, `commandExecution`, `fileChange`,
`mcpToolCall`, `dynamicToolCall`, `functionCallOutput`, `plan`,
`webSearch`, `contextCompaction`. Historical turns can be read back
(`thread/read includeTurns`; `thread/turns/list` is experimental).

**Compatibility-matrix implication:** roles/history are mappable but not
1:1 with Chat Completions — the adapter must translate
system/developer/user/assistant/tool roles into Responses-API items and
thread parameters. Recorded as PARTIAL with the mapping obligations below.

**Evidence:** official-doc; official-source (`ThreadStartParams.json`,
`ThreadInjectItemsParams.json`). **Confidence:** high for the surface;
medium for full role-fidelity (e.g. `tool` role assistant-side history
replay) until Stage 2 exercises it.

### 11. Streaming

**Finding.** Streaming is the native app-server event model: after
`turn/start`, the client reads `turn/started`, `item/started`,
`item/completed`, `item/agentMessage/delta`, `item/reasoning/
summaryTextDelta`, `item/reasoning/summaryPartAdded`,
`item/reasoning/textDelta`, `item/commandExecution/outputDelta`,
`turn/completed` (status `completed` | `interrupted` | `failed`) and other
notifications on the same transport. Default transport is stdio JSONL;
`initialize` must precede any other request per connection. The WebSocket
transport is documented as "experimental and unsupported" for production —
the execution adapter must use stdio (or the Unix-socket WebSocket variant
only with an explicit future decision; default off here). Clients may
suppress specific notifications via `optOutNotificationMethods`.

**Evidence:** official-doc; local-probe P5 (interleaved notifications
observed; response matching by id required — consistent with the PoC
evidence). **Confidence:** high.

### 12. Cancellation

**Finding.** `turn/interrupt` is the documented cancellation request:
success returns `{}` and the turn finishes with `status: "interrupted"`.
`command/exec/terminate` stops a `command/exec` session;
`thread/unsubscribe` detaches a client; `thread/backgroundTerminals/
terminate` (experimental) stops background terminals. This maps directly
onto the D-043 requirement that client disconnect/cancellation propagates
to the backend where supported.

**Evidence:** official-doc (app-server page). **Confidence:** high that the
mechanism is documented and stable-surface; **not live-tested in Stage 1**
(no turn was executed) — live propagation timing is a Stage 2 verification
item.

### 13. Isolation (against `docs/security.md` / D-044 local-adapter rules)

**Finding.** An isolation profile consistent with D-044 appears
constructible entirely from official mechanisms, but each knob requires
Stage 2 behavioral verification before the adapter is eligible:

Official mechanisms available:

- **Dedicated `CODEX_HOME`** (environment variable; officially supported
  for exactly this kind of boundary, per the Windows/WSL sharing doc): a
  separate home isolates the execution runtime's `config.toml` (including
  global `mcp_servers.*`, plugins, hooks), credentials store and session
  store from the user's interactive Codex. This is the primary lever for
  "no global MCP configuration, plugins, browser integrations".
- **Pinned `cwd` + sandbox policy per thread/turn:** `thread/start` /
  `turn/start` accept `cwd`, `approvalPolicy` and `sandboxPolicy`
  (`readOnly` with optional restricted `access` roots;
  `workspaceWrite` with explicit `writableRoots`, optional restricted
  `readOnlyAccess`, `networkAccess: false`/restricted; `externalSandbox`;
  `dangerFullAccess` must never be used by the adapter). Sandboxing is
  platform-native (Seatbelt / bubblewrap / Windows elevated-unelevated
  sandbox users) and applies to spawned commands.
- **`ephemeral` threads** (`thread/start.ephemeral` present in the 0.155.1
  schema; `codex exec --ephemeral` documented) avoid persisting rollout
  files; `thread/list` defaults to interactive `sourceKinds` (`cli`,
  `vscode`) so app-server threads are a separate class by default — both
  reduce accidental adoption of unrelated conversation history.
- **`codex exec` hardened flags** (`--ignore-user-config`,
  `--ignore-rules`) exist for the non-interactive path; the app-server
  path needs the CODEX_HOME + config discipline instead.
- **Approvals:** with `approvalPolicy` set so that no out-of-sandbox
  action proceeds unattended, approval server-requests
  (`item/commandExecution/requestApproval`,
  `item/fileChange/requestApproval`) must be declined/cancelled by the
  adapter — never auto-accepted.

Forbidden / must-not-use surfaces identified by name in the official docs:

- `thread/shellCommand` — "runs outside the sandbox with full access and
  doesn't inherit the thread sandbox policy" (never expose).
- `process/*` — explicit process control outside Codex's sandbox
  (experimental; never expose).
- `fs/*` v2 filesystem API — absolute-path filesystem operations (never
  expose to request content).
- `dynamicTools` + `item/tool/call` — client-executed tools flow is
  experimental (see tool-call section); if used in Stage 2 it must be
  gated, and client tool payloads must never be executed locally by the
  router (D-043).
- Config-mutating methods (`config/value/write`, `config/batchWrite`,
  `marketplace/*`, `skills/config/write`, `externalAgentConfig/import`)
  must never be called by the execution adapter against a user CODEX_HOME.

**Honest limits:** the sandbox is enforced by Codex, not by Scarcity
Router; a read-only sandbox alone is not proof of sufficient isolation
(D-044), and Stage 1 did not behaviorally verify any sandbox
configuration (no command execution was performed). Local runtimes also
write their own logs (`CODEX_HOME/log`, `.sandbox/sandbox.log`) —
the security doc requires M06/M10 to account for those logs. If Stage 2
cannot verify the profile above, only the affected adapter/mode is marked
ineligible and the rest of the router keeps working.

**Evidence:** official-doc (app-server, sandboxing, windows-sandbox, config
reference); official-source (ThreadStartParams). **Confidence:** high that
the mechanisms exist as described; the *sufficiency* of the composed
profile is UNVERIFIED (Stage 2).

### 14. Tool-call behavior

**Finding.** Two distinct tool surfaces exist and must not be conflated:

- **Codex-internal tools** (`commandExecution`, `fileChange`, `webSearch`,
  `mcpToolCall` against *Codex-configured* MCP servers, `collabToolCall`,
  apps/connectors): Codex executes these itself inside its sandbox. An
  execution adapter must not configure MCP servers/apps for the runtime
  (see isolation) and must not present Codex-internal tool activity as
  OpenAI `tool_calls`.
- **Client-executed dynamic tools:** `thread/start {dynamicTools}` +
  `item/tool/call` server request + `item/completed dynamicToolCall` —
  explicitly documented as **experimental** ("Dynamic tool calls
  (experimental)" section; schema-level gating via
  `capabilities.experimentalApi`). `turn/start {toolOutput}` (stable)
  supplies standalone tool output without a user turn.

**Compatibility-matrix implication:** a D-043-conformant `tool_calls`
round trip (client tools return to the client; the router never executes
them) is possible only through the experimental dynamic-tools flow on the
current generation. On the stable surface alone, client tool round trips
are not available (`UNSUPPORTED` there). Stage 2 must either (a) accept
the experimental gate with the D-019/U-011 containment (generation-aware
parsing, safe disable on contract change) and record `PARTIAL`, or
(b) ship a first iteration with `tool_calls` unsupported and fail closed
for tool-requiring requests. Tool *results* fed back via `toolOutput` are
stable.

**Evidence:** official-doc. **Confidence:** high for the documentation;
the dynamic-tools flow is UNVERIFIED live (experimental).

### 15. Structured output

**Finding.** `turn/start {outputSchema}` is documented: "Optional JSON
Schema used to constrain the final assistant message for this turn"
(schema-verified field on `TurnStartParams`, 0.155.1); it applies only to
the current turn. The non-interactive path documents
`codex exec --output-schema` with example output. Mapping from OpenAI
`response_format: json_schema` is therefore direct for turns; no
thread-level default schema exists.

**Evidence:** official-doc; official-source. **Confidence:** high
(documented); live behavior UNVERIFIED in Stage 1 (no turn executed).

### 16. Usage reporting

**Finding.** Three documented surfaces: (1) `thread/tokenUsage/updated`
notifications — "usage updates for the active thread"; (2)
`account/usage/read` — ChatGPT token-activity summary and daily buckets
(ChatGPT-backed auth only); (3) `codex exec --json` `turn.completed`
events carrying `usage: {input_tokens, cached_input_tokens,
output_tokens, reasoning_output_tokens}`. `turn/completed` itself is the
turn-end signal; per-turn usage mapping for the D-043 audit contract
(provider-reported usage) is available from (1)/(3). The goal API
(`thread/goal/*`) also tracks `tokensUsed`/`timeUsedSeconds` for budgeted
threads.

**Evidence:** official-doc. **Confidence:** high for availability; exact
field parity with the D-043 audit fields is a Stage 2 mapping task.

### 17. Stable versus experimental App Server protocol fields

**Finding.** The protocol now has an official stability contract
(`official-doc`, app-server page, "Experimental API opt-in"):

> Omit `capabilities` (or set `experimentalApi: false`) to stay on the
> stable API surface, and the server rejects experimental methods/fields.
> Set `capabilities.experimentalApi: true` to enable experimental methods
> and fields. [...] rejects with `<descriptor> requires experimentalApi
> capability`.

The official source implements this as a typed derive
(`codex-rs/app-server-protocol/src/experimental_api.rs`, tag
`rust-v0.155.1`): `#[experimental(...)]` markers on types/fields/enum
variants, collected via `inventory`, producing exactly the documented
error message. Version-pinned generated schemas
(`codex app-server generate-ts | generate-json-schema`, "specific to the
Codex version you ran") give Stage 2 a reproducible per-version contract
artifact.

**Stable (documented, no experimental label) surface relevant to Stage 2:**
`initialize`/`initialized`; `thread/start|resume|fork|read|list|
archive|unarchive|delete|unsubscribe|metadata/update|compact/start`;
`thread/inject_items`; `turn/start|steer|interrupt`; `model/list`;
`command/exec|write|resize|terminate`; `account/read|login/start|
login/cancel|logout`; `account/rateLimits/read` (+`account/rateLimits/
updated` notifications); `account/usage/read`;
`account/rateLimitResetCredit/consume`;
`account/sendAddCreditsNudgeEmail`; `account/workspaceMessages/read`;
approval server-requests; `config/read`; `configRequirements/read`;
`experimentalFeature/list`. Residual caution: the `codex app-server`
subcommand itself still prints `[experimental]` in `--help` at
`0.154.0-alpha.6.2` (P6), and the WebSocket transport is "experimental and
unsupported". The stability contract is therefore *protocol-method-level*
via `experimentalApi` gating plus the feature-maturity vocabulary — not a
blanket CLI-level guarantee.

**Experimental / deprecated (must not be load-bearing):**
`thread/turns/list`, `thread/items/list`, `thread/backgroundTerminals/*`,
`process/*`, `environment/info`, `collaborationMode/list`,
`permissionProfile/list` (beta), `dynamicTools` + `item/tool/call`,
`chatgptAuthTokens` login mode, `historyMode: "paginated"`
(currently returns `-32601`; reads fail closed), experimental
`parentThreadId`/`ancestorThreadId` filters, `excludeTurns`,
`plugin/*` ("under development; don't call from production clients"),
`thread/rollback` (deprecated), `item/fileChange/outputDelta`
(deprecated compatibility notification). `experimentalFeature/list`
exposes per-flag `stage` (`beta`/`underDevelopment`/`stable`/
`deprecated`/`removed`) at runtime, matching the official feature-maturity
vocabulary.

**Containment unchanged:** generation-aware parsing per D-019/U-011
remains the real defense; the `experimentalApi` gate and version-pinned
schemas narrow what "stable" means but do not replace fail-closed
validation and safe disable on contract change.

**Evidence:** official-doc; official-source; local-probe P6.
**Confidence:** high.

## Draft OpenAI compatibility-matrix contribution (for M03)

Per D-043: keyed by (adapter, adapter version, model/backend); values
PASS/PARTIAL/UNSUPPORTED/UNKNOWN with dated evidence and tested version.
`UNKNOWN` and `UNSUPPORTED` fail closed.

| Dimension | Value | Tested version | Evidence (dated 2026-09-19) | Notes / conditions |
|---|---|---|---|---|
| Roles and conversation history | PARTIAL | `codex-cli 0.154.0-alpha.6.2` (handshake + `model/list` probes only; turn-level docs-only, schemas at `rust-v0.155.1`) | official-doc + official-source + local-probe | thread/turn/item model; `thread/inject_items` (Responses API items); `baseInstructions`/`developerInstructions` for system/developer roles; system→thread mapping and tool-role history replay unverified |
| Streaming | PASS | `0.154.0-alpha.6.2` (notification behavior observed; delta semantics docs-only) | official-doc + local-probe | stdio JSONL transport; `item/*` deltas + `turn/*` lifecycle; WebSocket transport experimental/unsupported — excluded |
| `tool_calls` | PARTIAL (conditional) / UNSUPPORTED (stable surface) | docs-only (no turn executed) | official-doc | Client-executed tools require `dynamicTools` + `item/tool/call` behind `experimentalApi` — experimental; without that gate the stable surface has no client tool round trip. Stage 2 must choose and record per D-019/U-011 containment |
| Tool results | PASS | docs-only | official-doc | `turn/start {toolOutput}` (stable) supplies standalone tool output; appears as `functionCallOutput` items |
| Structured output | PASS | docs-only | official-doc + official-source | `turn/start {outputSchema}` JSON Schema constrains the final assistant message; per-turn only; maps from `response_format` |
| Reasoning controls | PASS | `0.154.0-alpha.6.2` (`model/list` probe: per-model efforts) | official-doc + local-probe | `model/list` `supportedReasoningEfforts`/`defaultReasoningEffort`; `turn/start {effort, summary}`; config `model_reasoning_effort` |
| Context limits | PARTIAL | docs-only | official-doc | `codexErrorInfo.ContextWindowExceeded` + `thread/compact/start` documented; explicit per-model context-window discovery (`modelProvider/capabilities/read`) not established in Stage 1 |
| Error semantics | PARTIAL | docs-only | official-doc | `turn/completed {status:"failed", error:{message, codexErrorInfo{httpStatusCode}, additionalDetails}}`; documented `codexErrorInfo` vocabulary (`UsageLimitExceeded`, `Unauthorized`, `BadRequest`, `SandboxError`, ...); JSON-RPC error layer documented; exact OpenAI-client error-code mapping unverified |
| Usage reporting | PASS | docs-only | official-doc | `thread/tokenUsage/updated`; `account/usage/read` (ChatGPT-backed auth only); `codex exec --json` `turn.completed` usage fields |
| Cancellation | PASS | docs-only | official-doc | `turn/interrupt` → `status:"interrupted"`; `command/exec/terminate`; live propagation timing unverified |

Matrix-cell honesty rule honored: cells whose evidence is documentation
only are marked with the schema/series version inspected and remain
subject to the issue's requirement of re-verification "with date and
tested version at implementation time" (Stage 2). No cell is recorded as
PASS from execution success alone; no promotional or subscription
eligibility is inferred anywhere.

## Documented scenarios per installation type

Official installation/remediation steps only; never manual token
extraction or `auth.json` copying.

### Desktop-only Codex (macOS / Windows; Linux preview)

- Installed: ChatGPT desktop app (chatgpt.com/download; Microsoft Store /
  `winget install --id 9PLM9XGG6VKS -s msstore` on Windows; `.deb`/`.rpm`/
  Arch script on Linux preview). Sign in with the ChatGPT account.
- Remediation when the execution adapter needs an app-server/CLI: install
  the Codex CLI via the official installer and run `codex login` once
  (browser) — on Windows the desktop app and CLI share
  `%USERPROFILE%\.codex`, so no second credential flow is needed; on
  Linux preview treat the homes as separate until verified.
- Diagnosis: `codex doctor`.

### CLI installation

- macOS/Linux: `curl -fsSL https://chatgpt.com/codex/install.sh | sh`;
  Windows: `powershell -ExecutionPolicy ByPass -c "irm
  https://chatgpt.com/codex/install.ps1 | iex"`; alternative: npm
  `@openai/codex`, Homebrew `brew install --cask codex`; unattended
  install: `CODEX_NON_INTERACTIVE=1`.
- Auth remediation (subscription path): interactive `codex login`
  (browser) or the device-code flow; thereafter Codex refreshes the
  provider-managed credential itself. Enterprise (Business/Enterprise
  workspaces): `printenv CODEX_ACCESS_TOKEN | codex login
  --with-access-token`.
- Execution-relevant flags: `codex app-server` (stdio), `codex exec`
  with `--sandbox`, `--ephemeral`, `--ignore-user-config`,
  `--ignore-rules`, `--json`, `--output-schema`, `-m`.

### VS Code integration

- Install the `openai.chatgpt` extension from the marketplace in VS Code,
  VS Code Insiders, Cursor or Windsurf; sign in once.
- The extension vendors a `codex` binary in the U-001 discovery layout;
  Scarcity Router discovery reads it read-only (no upgrade, no mutation).
- Remediation when the extension is present but the CLI is not: install
  the CLI (shares the provider-managed credential store; a second sign-in
  is not required once CODEX_HOME is shared — verify per platform in
  Stage 2).

### Windows

- Prefer the native Windows sandbox (`elevated`; `unelevated` fallback
  when administrator-approved setup is blocked); Windows 11 recommended,
  Windows 10 ≥1809 best-effort; `winget` should be available.
- WSL2 is the documented alternative profile ("Choose WSL when you need
  Linux-native tooling, your workflow already lives in WSL2, or neither
  native Windows sandbox mode meets your needs").
- Profile separation: `%USERPROFILE%\.codex` (Windows) vs `~/.codex`
  (WSL CLI); do not assume shared state.

### WSL

- Prereqs: `wsl --install` (PowerShell, admin); inside WSL run the
  install.sh CLI installer; VS Code users install the WSL extension and
  work from `~/.vscode-server` (U-001 evidenced layout).
- Sandbox prerequisite: bubblewrap (`sudo apt install bubblewrap` /
  `sudo dnf install bubblewrap`); AppArmor userns profile note for Ubuntu
  24.04 documented by OpenAI.
- Keep repositories under the Linux home (not `/mnt/c`); WSL1 is not
  supported since Codex 0.115.

## Quota scope vs promotional eligibility (kept separate)

- Executed work under ChatGPT-managed auth draws from the ChatGPT plan
  quota visible via `account/rateLimits/read`; under API-key auth it is
  usage-billed (PAYG). This is quota-scope evidence only.
- Promotional eligibility, plan entitlements and workspace rights are
  never inferred from telemetry or from execution success (D-039, D-042,
  issue #91 acceptance criteria). The eligibility parser and
  `ExecutionEligibility` contract are unchanged by Stage 1.

## Not tested / not determinable in Stage 1

- **No live turn execution** (no inference, no quota consumption): all
  turn-level matrix cells (tool calls, structured output, cancellation
  timing, streaming deltas, usage field parity, error mapping) rest on
  official documentation plus the version-pinned schemas. The issue
  explicitly requires re-verification with date and tested version at
  implementation time (Stage 2).
- **`account/rateLimits/read` live shape re-capture** was not repeated in
  this probe set; the D-018/D-019 validated mapping and 0.155.1 schemas
  stand as evidence. A live re-read is part of Stage 2's bounded probes.
- **Sandbox sufficiency** (readOnly / workspaceWrite / network
  restriction / Windows elevated mode) was not behaviorally verified —
  no command was executed in any sandbox. Until verified, the composed
  isolation profile is a design, not a fact.
- **Dynamic tools (experimental) round trip** — not exercised.
- **Desktop-bundle external driving** — no official documentation exists;
  recorded UNKNOWN.
- **macOS-specific desktop/CLI CODEX_HOME sharing** — not separately
  documented in the pages consulted; medium confidence.
- **`modelProvider/capabilities/read` payload** — existence documented;
  payload shape not inspected in Stage 1.
- **Windows/ARM64 and macOS** local behavior — this host is Linux x86_64;
  no probes were possible or attempted there.
- **Long-idle credential survival** (how long a cached ChatGPT credential
  lasts without refresh in unattended operation) — operational question,
  deferred to Stage 2/M10.

## Stage 2 implementation (2026-09-20, UTC)

Stage 2 implemented the verified supported subset as ONE worker-local
execution adapter for the M05 worker seam:

- `scarcity_router/worker_codex_adapter.py` — the adapter (allowlist id
  `codex`); channel semantics unchanged server-side (`worker_bridged` with
  resource `local_adapter_id: "codex"`, D-049; no new channel, no second
  worker protocol, no server-to-local-Codex path).
- `scarcity_router/worker_client.py` — worker registration flags
  `--allow-codex`, `--resource` / `--codex-resource`, `--codex-bin`.
- `tests/codex_fake_appserver.py` + `tests/test_worker_codex_adapter.py` —
  a deterministic fake App Server (spawned through the adapter's injected
  process seam) and 70+ behavior tests; no test executes a real binary, and
  every scripted string is synthetic.
- `tests/test_interfaces_guardrails.py` — the package-write guardrail now
  names the adapter's bounded controlled-home provisioning as the second
  allowed provisioning path (worker state directory only, `0o700`/`0o600`),
  alongside the D-036 `config.py` provisioning.

### What was re-verified live (local-probe, 2026-09-20)

Host unchanged (WSL2 Linux x86_64). Probes were bounded, read-only and
sanitized to structure before recording; NO turn was executed, NO
`account/read` or `account/rateLimits/read` was sent, and no credential or
user `~/.codex` content was touched. The probe ran against a FRESH empty
`CODEX_HOME` created for the probe (never the user's home):

- `codex --version` on the extension binary
  (`openai.chatgpt-26.908.40401-linux-x64`) → `codex-cli 0.154.0-alpha.6.2`
  (unchanged from Stage 1's P4).
- `initialize` → `initialized` over stdio JSONL: the result object carries
  exactly the four required string members (`userAgent`, `codexHome`,
  `platformFamily`, `platformOs`), and `codexHome` echoed the probe's
  controlled `CODEX_HOME` — the exact structure the adapter's handshake
  validates (including its adoption check). Unsolicited notifications were
  interleaved between responses (`configWarning`-shaped,
  `remoteControl/status/changed` — method + params keys only recorded),
  re-confirming the structural classification/tolerant-handling design.
- `model/list` (sent after `initialize` on the fresh, unsigned-in
  controlled home) answered with a JSON-RPC error object (integer `code` +
  string `message`; the message text was not parsed or recorded) instead of
  the Stage 1 P5 success envelope. This is consistent with an auth-gated
  model listing and motivates the implemented order: the adapter verifies
  auth (`account/read`) BEFORE `model/list`. The SUCCESS envelope therefore
  still rests on Stage 1 P5 (2026-09-19) plus the version-pinned schema —
  recorded honestly as not re-verified live against a signed-in home.

Protocol wire shapes implemented in Stage 2 were pinned against the
generated JSON schemas at tag `rust-v0.155.1` (inspected 2026-09-20):
`ThreadStartParams` (`ephemeral`, `cwd`, `approvalPolicy`
(`"untrusted"|"on-request"|"never"`), `sandbox` mode string,
`baseInstructions`, `developerInstructions`), `TurnStartParams` (`input`
text items, `model`, `effort` (open string), `outputSchema`,
`cwd`, detailed `sandboxPolicy`), the `SandboxPolicy` tagged union
(`workspaceWrite` with `writableRoots` + boolean `networkAccess`, default
false), `ThreadInjectItemsParams` (`threadId`, `items`), `TurnStartResponse`
(`turn.id`), `TurnCompletedNotification` (`turn.status` =
`completed|interrupted|failed|inProgress`, `turn.error.message` +
`turn.error.codexErrorInfo`), the protocol-level `codexErrorInfo`
camelCase vocabulary (`contextWindowExceeded`, `usageLimitExceeded`,
`unauthorized`, `sandboxError`, `httpConnectionFailed{…}`, …),
`ThreadTokenUsageUpdatedNotification` (`tokenUsage.last/total` with
`inputTokens`/`outputTokens`), `AgentMessageDeltaNotification` (`delta`),
`ModelListResponse` (`data[].model`/`id`,
`data[].supportedReasoningEfforts[].reasoningEffort`), `GetAccountResponse`
(`requiresOpenaiAuth`, `account.type` = `apiKey|chatgpt|amazonBedrock`),
and the approval server-request pair
(`item/commandExecution/requestApproval` /
`item/fileChange/requestApproval` answered with
`{"decision": "cancel"}` — the documented deny-AND-interrupt decision;
`source: codex-rs/app-server-protocol/src/protocol/v2/item.rs` at the same
tag).

### The implemented isolation profile (design now code, not a fact claim)

Every knob Stage 1 listed is implemented and behavior-tested (via the
recorded spawn specification and a protocol trace the fake server writes):
controlled `CODEX_HOME` (`0o700`, minimal generated `config.toml`,
handshake-verified adoption), ephemeral threads, per-attempt scratch `cwd`
(`0o700`, removed after the call), `workspaceWrite` with
`writableRoots=[<scratch>]` and `networkAccess: false` pinned per turn,
`approvalPolicy: "never"` plus the defensive `cancel` answer for any
arriving approval request, minimal child environment
(`CODEX_HOME`/`PATH`/`HOME`), process-group-owned child with bounded
terminate/kill/reap, strict JSONL budgets (line, cumulative bytes, event
count, unknown-notification count, per-delta and cumulative message chars),
and bounded stderr capture (never forwarded). What Stage 1 left UNVERIFIED
remains honestly unverified: the *sufficiency* of the composed sandbox
against a real agent workload (no real command execution was exercised —
the fake server models the protocol, and live verification requires a
signed-in subscription home, which is an M10 acceptance concern).

### Stage 2 compatibility-matrix cells (D-043)

Keyed by (`codex worker-local adapter`, `1.0.0`, `codex-cli
0.154.0-alpha.6.2` / schemas `rust-v0.155.1`). Evidence classes:
`local-probe` (2026-09-20, structure-only), `official-source` (pinned
schemas), `official-doc`, and `test-evidence` (the deterministic fake-based
suite, which verifies THIS adapter's mapping, never the live backend).
Cell values use exactly the closed D-043 vocabulary (`PASS`, `PARTIAL`,
`UNSUPPORTED`, `UNKNOWN`) — one value per cell. Where a cell's behavior is
test-verified in THIS adapter's mapping but live turn-level confirmation
against a signed-in subscription is still pending (M10 acceptance work),
the single conservative cell value is `PARTIAL` and the mapping evidence
lives in the notes; `UNKNOWN` and `UNSUPPORTED` fail closed.

| Dimension | Value | Tested version | Evidence (dated 2026-09-20) | Notes |
|---|---|---|---|---|
| Roles and conversation history | PARTIAL | `0.154.0-alpha.6.2` (handshake only) | test-evidence (adapter mapping verified) + official-source (`ThreadStartParams`, `ThreadInjectItemsParams`) + local-probe | mapping test-verified: system→`baseInstructions` (joined), developer→`developerInstructions`, prior user/assistant→`inject_items` Responses items, final user→turn input; conversations not ending with a user message rejected before execution; assistant-side `tool` history replay unsupported (rejected); live fidelity of the full mapping pending M10 |
| Streaming | PARTIAL | notification behavior local-probe (2026-09-19/20) | test-evidence (adapter mapping verified) + official-doc | adapter mapping test-verified: `item/agentMessage/delta` → `text_delta` (bounded per-delta and cumulative); assembled final message; stdio only; live delta semantics pending M10 |
| `tool_calls` | UNSUPPORTED (stable surface) | — | official-doc (Stage 1 §14); enforced in code | requests carrying `tool` role, `tool_calls` or `tools` are rejected BEFORE execution; `dynamicTools` is never enabled (client tools stay client-side, D-043) |
| Tool results | UNSUPPORTED (not mapped in Stage 2) | — | — | `turn/start {toolOutput}` is documented stable, but mapping tool-result round trips is deferred; fail closed |
| Structured output | PARTIAL | schemas `rust-v0.155.1` | test-evidence (adapter mapping verified) + official-source (`TurnStartParams.outputSchema`) | adapter mapping test-verified: `json_schema` → `outputSchema` after object/size(64 KiB)/depth(32) validation; `json_object` explicitly rejected; per-turn only; live schema enforcement pending M10 |
| Reasoning controls | PARTIAL | `model/list` effort field local-probe (2026-09-19) | test-evidence (adapter mapping verified) + official-source + local-probe | adapter mapping test-verified: exact binding — slug must be listed, effort must be in `supportedReasoningEfforts`, both pinned per turn; mismatches rejected before execution; live acceptance of pinned turns pending M10 |
| Context limits | PARTIAL | docs-only | official-doc (Stage 1 §10 draft) | `codexErrorInfo: contextWindowExceeded` maps to a safe failure note; no per-model context-window discovery implemented |
| Error semantics | PARTIAL | schemas `rust-v0.155.1` | test-evidence (adapter mapping verified) + official-source (`codexErrorInfo` camelCase vocabulary) | adapter mapping test-verified: `turn/completed {failed}` → typed failure with a note from the closed vocabulary only; free-text error bodies never read; unknown status fails closed; exact OpenAI-client error-code parity pending M10 |
| Usage reporting | PARTIAL | schemas `rust-v0.155.1` | test-evidence (adapter mapping verified) + official-source (`ThreadTokenUsageUpdatedNotification`) | adapter mapping test-verified: `inputTokens`→`prompt_tokens`, `outputTokens`→`completion_tokens` from `tokenUsage.last` (fallback `total`); absent usage stays absent; cached/reasoning components not represented; live field parity pending M10 |
| Cancellation | PARTIAL | official-doc | test-evidence (adapter mapping verified) + official-doc | adapter mapping test-verified: cancel event or deadline → exactly one bounded `turn/interrupt` (bounded ack wait) → cancelled result; never completed after confirmed cancellation; approval requests answered `cancel`; live propagation timing pending M10 |

**M10-B end-to-end evidence note (2026-09-20, integration branch).** The
M10-B acceptance suite (`tests/test_e2e_codex_acceptance.py`) re-verified
the mapping half of the following cells through the FULL composed stacks
(OpenAI-compatible client → server coordinator → real worker protocol →
this adapter → the deterministic fake App Server), with synthetic
compatibility-matrix cells supplied programmatically as test evidence:

- *Roles and conversation history*: system→`baseInstructions`,
  developer→`developerInstructions`, prior user/assistant turns via
  `thread/inject_items` (roles verified in order), final user message as
  turn input — verified through the HTTP surface end to end.
- *Streaming*: deltas delivered as SSE text chunks through the gateway
  and worker bridge, assembled in order; `[DONE]` terminator; observed
  surface shape: no explicit terminal `finish_reason` frame on this path
  (the adapter emits text deltas only).
- *Structured output*: `response_format` `json_schema` forwarded verbatim
  as `turn/start {outputSchema}` through the stack.
- *Reasoning controls*: pinned model slug + `reasoning_effort` forwarded
  verbatim per turn; an effort absent from the runtime's own listing is
  rejected before any thread/turn exists (verified through the stack).
- *Usage reporting*: fake-reported usage lands in the audit trail as
  provider-reported through the composed server; absent usage stays
  absent (never zero).
- *Cancellation*: client disconnect → gateway → worker cancel → exactly
  one `turn/interrupt` while the turn is open; outcome audited as
  cancelled; never completed after the cancellation.
- *Error semantics*: worker-side typed failures surface as explicit
  client errors (`backend_failure`, `ambiguous_execution_state` with the
  audit record) — the closed-note mapping verified end to end.

Every cell VALUE above is unchanged: all turn-level cells remain
`PARTIAL` because the live half — confirmation against a signed-in
subscription home — is still pending. That live half is recorded as
`EXTERNAL_ACCEPTANCE_GATE: LIVE_CODEX_SUBSCRIPTION` in
[`docs/m10-acceptance.md`](m10-acceptance.md); a structure-only probe
against the real binary (2026-09-20: handshake + controlled-home
adoption pass, unsigned-in `account/read` fails closed) is recorded
there as supplementary evidence and never substitutes for a live turn.

### Not tested / honest gaps after Stage 2

- **No live turn execution** (no inference, no quota consumption): every
  turn-level cell above is therefore `PARTIAL` — the adapter-mapping half
  is test-verified against the pinned protocol, the live half is not
  proven; live backend confirmation for a signed-in subscription home
  (delta semantics, interrupt timing, `inject_items` on ephemeral
  threads, structured-output enforcement) is M10 end-to-end acceptance
  work.
- **The execution adapter is strictly read-only against the provider**
  (remediation-round 1 correction of an earlier draft claim): it performs
  NO provider-state mutation — the bounded D-018 managed-auth refresh
  remains collector-only (D-018, unamended), and an `account/read`
  protocol error fails closed to `auth_unverified` with the official
  sign-in remediation. `max_output_tokens` and non-empty
  `generation_params` are likewise rejected before execution
  (`request_parameters_unsupported`, refuse-not-drop).
- **`model/list` success envelope** was not re-verified live on
  2026-09-20 (the fresh probe home is unsigned-in and the probe set is
  forbidden from auth actions); it rests on Stage 1 P5 + the pinned
  schema. The adapter fails closed on any structural drift.
- **Long-idle credential survival** in the controlled home, and the
  operational cadence of the D-018 refresh under execution load, remain
  M10/M09 operational questions.
- **Windows-native and macOS profiles** are coded as honest
  `platform_not_evidenced` ineligibility; behaviorally verifying the
  Windows elevated/unelevated sandbox profile (or macOS Seatbelt) is
  future work on appropriate hardware.
- **Desktop-bundled Codex** remains UNKNOWN and undiscovered.
- The guardrail extension (package-write scan) is the only existing-test
  change; it is documented in the guardrail docstring itself.
