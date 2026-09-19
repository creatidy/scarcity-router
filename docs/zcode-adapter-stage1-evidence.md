# M07 Stage 1: ZCode execution feasibility evidence

- **Issue:** BioMedical-IT/scarcity-router#92 (M07, Stage 1 only)
- **Date:** 2026-09-19 (all retrievals and probes performed on this date, UTC)
- **Scope:** feasibility evidence only. No adapter code, no tests, no CI
  changes. Stage 2 implementation depends on A0 (#85) and M05 and is out of
  scope here. ZCode may remain experimental, disabled by default or
  unsupported without blocking the execution-gateway program.
- **Evidence classes:** `official-doc` (vendor documentation/terms),
  `official-behavior` (vendor-documented product behavior),
  `local-probe` (safe read-only probes on the owner's machine; no live
  inference was invoked), `secondary` (community/unofficial; never
  load-bearing for a "supported" claim).
- **Security:** no credentials, account identifiers, session identifiers,
  prompt or response contents were read or recorded. Config files were
  inspected at key level only. No live model-inference probe was run; all
  local probes are static/read-only.

## 1. Method and sources

Native in-product browsing was unavailable in the research environment;
official pages were retrieved via server-side readers and direct HTTP
fetches. Every URL below was retrieved on **2026-09-19**.

### 1.1 Official sources consulted (`official-doc`)

| Source | Retrieval date | Key content |
|---|---|---|
| https://zcode.z.ai/en/docs/welcome | 2026-09-19 | Product positioning: "Agentic Development Environment (ADE)", desktop workspace + Remote + Bot Channel; ZCode Agent for GLM-5.3 |
| https://zcode.z.ai/en/docs/install | 2026-09-19 | Desktop Electron app only (macOS arm64/x64, Windows x64/ARM64, Linux x64/ARM64 AppImage/deb/rpm); **no CLI, no npm package, no headless install documented** |
| https://zcode.z.ai/en/docs/configuration | 2026-09-19 | Provider/connection model; Coding Plan endpoints; API-key vs account binding; `~/.zcode/v2/config.json` honored keys |
| https://zcode.z.ai/en/docs/idle-time-tasks | 2026-09-19 | Idle-time tasks: free, no plan-quota consumption, eligibility-checked, desktop + local projects only, run on the user's machine, UI-created |
| https://zcode.z.ai/en/docs/automations | 2026-09-19 | Scheduled automations: local-machine execution, four permission modes, max 20 tasks, **no external trigger API** |
| https://zcode.z.ai/en/docs/bot-channel | 2026-09-19 | WeChat/Feishu chat relay into the existing desktop session; **no HTTP API**; Telegram not supported (as of retrieval) |
| https://zcode.z.ai/en/docs/mcp-services | 2026-09-19 | ZCode is an MCP **client only**; scopes and auto-connect behavior; import from Claude Code/Codex configs |
| https://zcode.z.ai/en/docs/plugin | 2026-09-19 | Plugin system: machine/workspace level, auto-enable on install, Claude Code plugin-format compatibility, no sandbox documented |
| https://zcode.z.ai/en/docs/subagents | 2026-09-19 | Foreground/background subagents; user-level config only; no programmatic/headless use documented; cancellation not covered |
| https://zcode.z.ai/en/docs/agents | 2026-09-19 | Four execution modes; tool capabilities; thought levels; **no CLI/API invocation documented** |
| https://zcode.z.ai/en/docs/safety-confirm | 2026-09-19 | Permission model mapping (see section 8) |
| https://zcode.z.ai/en/docs/usage-stats | 2026-09-19 | Usage accounting: local App Usage records + remote Coding Plan quota pools; quota reset cards |
| https://zcode.z.ai/en/docs/remote-development | 2026-09-19 | Remote workspaces: user-supplied SSH/WSL/Docker targets; desktop GUI remains the entry point; **no API/headless access** |
| https://zcode.z.ai/en/docs/hooks | 2026-09-19 | Local hook subprocess protocol; config at `~/.zcode/cli/config.json`; PermissionRequest allow/deny semantics |
| https://zcode.z.ai/en/docs/qa | 2026-09-19 | Config layout (`~/.zcode/cli`, `~/.zcode/v2/credentials.json` encrypted per device); endpoint table; Linux/WSL troubleshooting; known config breaking changes |
| https://zcode.z.ai/en/changelog | 2026-09-19 | Release history 3.8.1 (2026-08-20) through 3.14.0 (2026-09-19) |
| https://zcode.z.ai/en/terms | 2026-09-19 (version effective 2026-06-15) | Terms of service; quotes in section 5 |
| https://zcode.z.ai/en/privacy | 2026-09-19 | Exists; not load-bearing for Stage 1 |
| https://docs.z.ai/devpack/overview | 2026-09-19 | GLM Coding Plan quota model: tiers, 5-hour/weekly credit pools, off-peak 50% rate, credit formula, tool support |

The full documentation sidebar was enumerated on 2026-09-19 from
`/en/docs/welcome`; it contains **no** CLI, headless, SDK, automation-API or
settings-reference page. Closest surfaces are Automations, Hooks, Bot
Channel, Remote Development/Control, MCP and Safety Confirmation.

### 1.2 Local probes (`local-probe`), all 2026-09-19

Performed on the owner's WSL2 machine (Ubuntu, linux 6.18 WSL2), which runs
an authorized, owner-installed ZCode 3.14.0 environment. All probes were
read-only; none invoked model inference; outputs were sanitized before
recording.

1. `command -v zcode` — not on `PATH`. No `zcode`/`zcode-cli` binary in
   `~/.local/bin`, `/usr/local/bin`, `~/bin`, npm global prefix or dpkg.
2. Process discovery (`ps`): running processes named `zcode-cli` (several)
   and `zcode-server.cjs` executed by a bundled private Node runtime at
   `~/.zcode/server/node`; helper MCP process `zcode-node-repl-mcp`.
3. Environment of a running `zcode-cli` process (names only plus two
   non-secret values): `ZCODE_APP_VERSION=3.14.0`;
   `ZCODE_SERVICE_AUTHORITY_MODE=desktop-attached-remote`;
   `ZCODE_RUNTIME_ENV=production`; `ZCODE_ENV=production`. Endpoint vars
   present with hostnames only (all vendor-controlled HTTPS origins:
   `chat.z.ai` OAuth origin, `api.z.ai` business API, `zcode.z.ai` service
   base). Paths redacted.
4. Directory structure: `~/.zcode/{cli,v2,server,plugin-workspace,tmp}`;
   `~/.zcode/cli/{config.json,agents,artifacts,db,exec,log,memories,plugins,rollout}`;
   `~/.zcode/v2/{certs,credentials.json,provider_config.json,runtime,setting.json,tasks-index.sqlite*,telemetry-state.json}`.
   `~/.zcode/cli/config.json` top-level key observed: `mcp` only.
   `~/.zcode/v2/setting.json` keys are desktop-app settings (e.g.
   `autoDownloadAndInstallUpdates`, `keepAwakeWhileRunning`,
   `modelIoFullRetentionEnabled`, `providerFamilyConnectionSelections`).
   `provider_config.json` top level: `config`, `schemaVersion`. Values not
   read.
5. Static bundle inspection of `~/.zcode/server/zcode-server.cjs`
   (11,527,731 bytes, mtime 2026-09-19): grep for `headless`,
   `nonInteractive`, `non-interactive`, `one-shot`, `oneShot`, `sdkMode`,
   `stdinMode` — **zero occurrences each**. Internal launch-style flags
   present in the bundle include `--stdio`, `--socket`,
   `--permission-broker-socket`, `--permission-preflight`,
   `--background-mode`, `--workspace-key`, `--controller-variant` (internal
   contract, undocumented). No user-facing `Usage:` CLI help text found.
6. Socket discovery (`ss`): each `zcode-cli` process listens on one local
   Unix-domain socket (path redacted; internal IPC with the desktop
   process). No TCP listener observed for these processes.
7. Session store structure (names only, contents never read): per-session
   directories under `~/.zcode/cli/exec/sess_*`; SQLite store at
   `~/.zcode/cli/db/db.sqlite*`; model-I/O JSONL under
   `~/.zcode/cli/rollout/model-io-*.jsonl` (retention gated by
   `modelIoFullRetentionEnabled`).

A live `--version`/`--help` invocation of the runtime was deliberately **not**
attempted: the binary is not independently installed on `PATH`, it runs in
`desktop-attached-remote` authority mode owned by the desktop app, and
spawning a second instance could disturb the owner's live sessions. The
version is established from the live process environment instead.

### 1.3 Secondary evidence (`secondary`, not load-bearing)

- https://github.com/valeriikot/zcode-cli ("zcode-app-cli", retrieved
  2026-09-19): an MIT-licensed **unofficial** terminal client that extracts
  the bundled agent runtime (`resources/glm`) out of the ZCode Desktop
  Electron bundle and launches it under a third-party TUI. Self-described as
  not affiliated with or endorsed by Z.ai; ~1 star, solo project; its own
  README warns readers to confirm redistribution rights before publishing
  the npm package. **This confirms only that the desktop bundle contains an
  extractable agent runtime. It is not evidence of an official headless
  API, of long-term compatibility, of redistribution rights, or of
  promotional eligibility.**

## 2. Stage-1 checklist findings

Each finding: claim, evidence class, confidence, and residual unknowns.

### 2.1 Vendor documentation — what the vendor documents

ZCode is documented as a **desktop Electron "Agentic Development
Environment"** for GLM-5.3 (`official-doc`, high confidence). The documented
invocation surfaces are:

- the desktop GUI (primary);
- **Automations** — time-scheduled tasks, created and managed in the UI,
  running locally on the user's machine; max 20 tasks; four permission
  modes; run history states (in progress / succeeded / failed / skipped);
  **no external trigger API** (`official-doc`, high);
- **Idle-time tasks** — queued work the vendor runs for free during spare
  capacity, **not consuming plan quota**, eligibility-checked server-side
  ("Unable to verify eligibility right now" state), desktop + local projects
  only, machine must stay awake, daily creation cap, six run states
  (queued/paused/running/completed/failed/cancelled), background subagents
  not supported (`official-doc`, high);
- **Bot Channel** — WeChat/Feishu chat relay into an existing desktop
  session; workspace access scoping; interactive approval cards; **no HTTP
  API** (`official-doc`, high);
- **Remote Development** — agent execution moved to a user-supplied
  SSH/WSL/Docker target while the desktop GUI keeps account/config/task
  entry; **no API or headless mode** (`official-doc`, high);
- **Remote Control / mobile** — control of the desktop app from mobile;
  automations and idle-time tasks are not visible/manageable there
  (`official-doc`, high).

**UNKNOWN:** any undocumented internal automation interface (see 2.4) — its
existence is observable locally, its contract is not documented anywhere.

### 2.2 Vendor terms (version effective 2026-06-15, retrieved 2026-09-19)

Full analysis in section 5. Summary: the terms do not describe or permit a
programmatic third-party router use of the desktop runtime; account
exclusivity and the prohibition on using ZCode as an "unauthorized proxy
server" make the execution-gateway use case legally risky without explicit
vendor consent (`official-doc`, high confidence in what the text says;
medium-high confidence in the interpretation).

### 2.3 Local runtime/protocol behavior

Confirmed present and running: a private Node runtime hosting
`zcode-server.cjs` plus per-session `zcode-cli` processes in
`desktop-attached-remote` authority mode, each exposing one local Unix-socket
IPC endpoint (`local-probe`, high confidence in observations). The IPC
protocol is undocumented; no TCP listener is exposed. The bundle contains no
headless/one-shot/non-interactive mode strings (`local-probe`, high
confidence in the negative grep; the bundle is a single 11.5 MB snapshot, so
this is version-specific evidence).

**UNKNOWN:** the IPC message contract, handshake and authorization of the
local sockets; whether they are stable across releases (changelog evidence
suggests no).

### 2.4 Runtime discovery

Supported discovery signal (what an integration could safely rely on):
`~/.zcode/` tree with the documented FAQ layout (`~/.zcode/cli`,
`~/.zcode/v2`), the bundled server at `~/.zcode/server/zcode-server.cjs`,
process names `zcode-cli`/`zcode-server.cjs`, and `ZCODE_APP_VERSION`
(`official-doc` FAQ + `local-probe`, high). There is no independently
installed CLI binary on `PATH` on this machine; the runtime is
desktop-attached (`local-probe`, high). Windows-side desktop-app install
location was not identified in Stage 1 (not needed for the verdict).

### 2.5 Authentication

Documented model (`official-doc`, high): account binding via OAuth
("Continue with Z.ai" / "Continue with BigModel") or API-key mode per
provider; connection settings (`apiKey`, `baseURL`, `apiKeyRequired`,
`headers`) stored in `~/.zcode/v2/config.json`; login credentials in
`~/.zcode/v2/credentials.json` encrypted per device (`official-doc` FAQ,
high). Locally observed store files match (`local-probe`). The D-018
boundary (the router never touches provider tokens) is satisfiable in
principle because a locally driven runtime would use its own already-
authenticated credentials — **but no supported interface exists through
which a third party can drive execution at all**, so the authentication
question for an adapter is moot today. Authentication for third-party
invocation: **UNSUPPORTED (interface gap, not a credential gap).**

### 2.6 Headless execution — verdict

**VERDICT: NO — no official, stable, headless execution path exists as of
2026-09-19.** Exact supporting evidence:

1. The official install path is a desktop Electron app only; no CLI, npm
   package or headless option is documented (`official-doc`: /docs/install;
   full sidebar enumeration) — high confidence.
2. Every documented unattended/remote surface (Automations, Idle-time
   tasks, Bot Channel, Remote Control, Remote Development) is UI- or
   chat-driven, local-machine-bound, and explicitly lacks an external
   trigger API (`official-doc`, per-page) — high confidence.
3. The local agent runtime supports headless agent execution **internally**
   (this research session itself ran inside the desktop-attached runtime;
   per-process Unix-socket IPC observed), but in
   `desktop-attached-remote` authority mode with an undocumented internal
   protocol, no help text, and no stability commitment (`local-probe`,
   high confidence in observations; the internal path is explicitly **not**
   an official supported interface) — medium confidence that no documented
   alternative exists anywhere (absence of documentation is evidence of
   absence of *support*, verified across the full sidebar, changelog and
   FAQ on 2026-09-19).
4. The unofficial wrapper (`secondary`) proves only that the runtime is
   technically extractable; per the issue and U-013 it never constitutes
   proof of API support, compatibility or redistribution rights.

Consequently the honest classification for the execution-adapter purpose is
**NO-GO for a supported adapter**; an *experimental-only* probe remains
conceivable but is conditioned as in section 10.

### 2.7 Output format

No structured external output contract exists for any invocation surface:
the desktop renders sessions; hooks emit a documented local JSON event
protocol for lifecycle events (not model output); model I/O may be retained
locally as JSONL gated by a settings flag (`official-doc` hooks +
`local-probe` structure). For OpenAI-compatible response mapping: **UNKNOWN
(no surface to test) — fails closed per D-043.**

### 2.8 Cancellation

Documented cancellation is UI-only: the terms state the user may click stop
at any time to interrupt execution (`official-doc` terms IV.6); automation
run history records states including failed/skipped; idle-time tasks expose
a cancelled state in the UI (`official-doc`, high). No programmatic
cancellation contract is documented: **UNKNOWN** for adapter purposes.

### 2.9 Tool behavior

ZCode's agent executes its own local tools (files, shell, browser,
subagents, MCP) inside the user's environment; subagents may run foreground
(parallel) or background; a subagent cannot spawn subagents; backgrounded
Explore subagents are read-only (`official-doc`, high). For the router this
is a semantic mismatch with D-043: client-supplied tools must return to the
client as `tool_calls` and must never be executed locally by the backend.
ZCode has no documented mechanism to hand tool calls back to an external
caller: **UNSUPPORTED for client-side tool_calls; UNKNOWN for the internal
contract.**

### 2.10 Permissions

The permission model is explicitly mapped (required before any execution
description, per the issue):

- Four execution modes: **Ask before changes** (default), **Edit
  automatically**, **Plan mode**, **Full access** (`official-doc`
  /docs/agents, /docs/safety-confirm; high).
- Permission-request handling: options to allow once/always or reject
  once/always; unanswered questions auto-continue after a timeout (default
  5 minutes) while permission requests wait; automations use the same four
  modes, defaulting to "Ask before changes" (`official-doc`, high).
- Hooks can auto-allow or auto-deny on `PermissionRequest`, but explicit
  deny rules, plan-mode write bans and hard tool limits cannot be bypassed
  by a hook allow (`official-doc` /docs/hooks; high).
- **Risk-relevant trust behavior:** project-declared MCP servers connect
  automatically at session start without per-session approval; plugins
  install enabled by default with code-execution trust; one-click import of
  Claude Code/Codex MCP configs (`official-doc` /docs/mcp-services,
  /docs/plugin; high).

Unattended operation therefore requires either Full access mode or
hooks-based allows — both of which reduce the human gate that D-044's
isolation rules assume must be replaced by environment containment.

### 2.11 Version stability

Changelog (retrieved 2026-09-19): releases 3.8.1 (2026-08-20), 3.9.1
(08-25), 3.9.2 (08-26), 3.10.1 (08-28), 3.10.2 (08-31), 3.11.2 (09-04),
3.12.3 (09-17), 3.14.0 (09-19) — **eight releases in one month**, current
3.14.0 matching the locally installed version (`official-doc` +
`local-probe`, high). No SemVer/LTS/backward-compatibility commitment is
documented; the FAQ itself records breaking config changes between versions
(e.g. a subagent frontmatter field rename), and only some releases carry
notes (`official-doc`, high). A marketing-page render retrieved the same
day still advertised 3.11.2 in one cached view and 3.14.0 in another —
minor evidence of fast-moving distribution. **Conclusion: version churn is
high; any future integration must pin the exact runtime version and fail
closed on drift (issue acceptance criterion).**

### 2.12 Actual usage/quota accounting (kept separate from promotions)

- **Account-level quota is observable and already collected:** the Coding
  Plan exposes a 5-hour credit pool, a weekly credit pool and a monthly MCP
  tool quota, visible in ZCode's Coding Plan usage tab and served by the
  Z.ai usage endpoint (`official-doc` /docs/usage-stats + /docs/qa). This
  is the same account quota the existing `zai-coding-plan` collector
  (`zai_usage_endpoint`, `GET https://api.z.ai/api/monitor/usage/quota/limit`)
  already normalizes — router-side observation of subscription quota does
  **not** require any ZCode runtime execution (`official-doc` + existing
  repo evidence in `docs/providers.md`, high).
- **Local per-session usage records exist** (App Usage tab: tokens,
  sessions, messages, per-model breakdowns read from local session
  records; model-I/O JSONL locally) (`official-doc` + `local-probe`
  structure, high). No per-request external usage API is documented
  (`official-doc`, high).
- **Idle-time task runs are vendor-documented as free and non-consuming**
  of plan quota, subject to server-side eligibility checks and daily
  creation caps (`official-doc`, high).
- **Promotional/campaign behavior is separate and unproven:** the devpack
  documents off-peak 50% credit rates (peak Mon–Fri 14:00–18:00 SGT), tier
  allowances (Lite 2,000/10,000; Pro 12,000/60,000; Max 28,000/140,000
  credits per 5-hour/weekly window), a credit formula with token
  multipliers (GLM-5.3: input 6.9, cached input 1.7, output 24), quota
  reset cards issued off-peak (3.8.1+), and a time-boxed promotion
  (unlimited GLM-5.3-Flash via ZCode 23:00–09:00 plus doubled quota on
  other agents) (`official-doc` /docs.z.ai/devpack/overview + /docs/usage-
  stats, high). **None of this is documented as applying to usage executed
  by a third-party router through the desktop runtime; per D-039,
  promotional eligibility for router-executed work remains UNKNOWN and
  must not be assumed.**

## 3. Draft compatibility-matrix contribution (for M03/D-043)

Key: (adapter = `zcode-local-runtime`, adapter version = `3.14.0`, backend
= GLM-5.3 via GLM Coding Plan account binding). Retrieved/tested
2026-09-19. Per D-043, `UNKNOWN` and `UNSUPPORTED` fail closed; with this
row, admission of the ZCode surface fails closed today.

| Dimension | Cell | Tested version | Evidence (dated 2026-09-19) |
|---|---|---|---|
| Roles and conversation history | UNKNOWN | 3.14.0 | No supported external invocation surface exists (no headless/CLI/API documented; official docs + install page). Internal session model exists but protocol undocumented. |
| Streaming | UNKNOWN | 3.14.0 | Desktop UI streams responses (official behavior); no external streaming contract documented. |
| `tool_calls` (client-supplied tools) | UNSUPPORTED | n/a | D-043 forbids backends executing client tools locally; ZCode's model executes its own tools in the user environment; no mechanism returns OpenAI-style tool_calls to an external caller (official docs). |
| Tool results | UNSUPPORTED | n/a | Same rationale as `tool_calls`. |
| Structured output | UNKNOWN | 3.14.0 | Internal output-format config strings exist in the local bundle (static probe); no external contract documented. |
| Reasoning controls | PARTIAL | 3.14.0 | Thought level Low/High/Max documented in-app; `reasoning_effort` translation documented for configured OpenAI-protocol providers (official docs). Not settable through any supported external invocation surface. |
| Context limits | PASS (model-level) | 3.14.0 | GLM-5.3 1M-token context documented by vendor; `[1m]`-suffixed models fixed at 1M in model config (official docs). Provider-documented, not surface-tested. |
| Error semantics | UNKNOWN | 3.14.0 | No external error vocabulary documented for any invocation surface. |
| Usage reporting | PARTIAL | 3.14.0 | Account-level 5-hour/weekly/MCP pools observable remotely (official usage-stats docs; already collected via existing `zai_usage_endpoint` collector); local per-session records exist. No per-request external usage contract. |
| Cancellation | UNKNOWN | 3.14.0 | UI stop only (terms IV.6; automation/idle-time run states); no programmatic cancellation contract. |

## 4. Runtime discovery, authentication and accounting boundaries

- **Discovery (safe, supported):** `~/.zcode/` presence with the FAQ layout,
  `~/.zcode/server/zcode-server.cjs`, process names `zcode-cli` /
  `zcode-server.cjs`, `ZCODE_APP_VERSION` env on the live process, and
  per-process local Unix sockets. Version read from the live process env,
  not from an independent CLI entry point (there is none on `PATH`).
- **Authentication boundary:** all provider credentials remain inside the
  ZCode per-device encrypted store or `~/.zcode/v2/config.json`; a router
  integration would never read them (D-018 unchanged). The blocker is not
  credential access but the absence of any supported drive-in interface.
- **Accounting boundary:** subscription quota observation (existing
  collector) and execution accounting are separable. Even if execution
  existed, per-request usage attribution to the router would need the
  local session records or an undocumented interface — today only
  account-level windows are honestly attributable (same limitation class
  as the existing coding-plan collector: account-level windows, not
  per-request).

## 5. Terms and licensing findings (`official-doc`; separate from execution findings)

Quotes and close paraphrases from the ZCode Terms of Service, version
effective **2026-06-15**, retrieved **2026-09-19**
(https://zcode.z.ai/en/terms). Operator: JINGSHENG HENGXING TECHNOLOGY
PTE.LTD.; Singapore law; SIAC arbitration.

1. **Account exclusivity (III.3):** "Your ZCode account is associated with
   your personal information and is for your exclusive use only. Without
   the consent of ZCode, any direct or indirect authorization of a third
   party to use your account or access the information under your account
   shall be invalid." — An execution gateway serving users *other than the
   subscription owner* through this account is clearly outside the terms.
   Owner-only use narrows but does not resolve the next point.
2. **No lending/renting (III.4):** users shall not "gift, lend, rent,
   transfer, sell, or otherwise authorize anyone other than the original
   registrant" to use the account.
3. **Proxy/server prohibition (IV.3):** prohibited conduct includes
   "using ZCode as a virtual server, unauthorized proxy server, or mail
   server". A router that receives API requests and executes them through
   the ZCode runtime is, by ordinary meaning, using ZCode as a proxy
   server; whether it is "authorized" would require explicit vendor
   consent that does not exist today. **This is the central terms risk for
   the M07 use case.** D-044's one-user scoping reduces the resale
   concern but does not authorize proxy-style use.
4. **No reverse engineering / extraction (IV.3):** prohibited to
   reverse-engineer "any algorithms, source code, or mechanisms of ZCode",
   to extract data "by any means", and to bypass or circumvent security
   measures. Extracting and redistributing the bundled runtime (as the
   unofficial wrapper does) is therefore not something this project may do
   or rely on; the bundled runtime must never be redistributed.
5. **No competing-model training (IV.3):** prohibited to develop, train or
   improve competing algorithms/models using the service.
6. **User responsibility for operations (IV.6):** "All operations performed
   by ZCode based on your instructions constitute an extension of your own
   actions"; the user is "deemed to have expressly authorized ZCode to
   access and operate your local device environment"; the vendor itself
   recommends running ZCode in a VM/sandbox for security; the user may
   click stop at any time to interrupt. — The vendor's own sandbox
   recommendation supports D-044's isolation approach.
7. **Third-party sharing responsibility (IV.7):** making ZCode services
   available to any third party is the user's independent responsibility
   and liability.
8. **Third-party API key mode (IV.10):** when the user configures their own
   third-party API keys, "ZCode acts solely as a local execution channel" —
   the vendor explicitly frames tool-side API-key usage; it does not
   address router usage of ZCode itself.
9. **Commercial use of generated content (VI.2):** with an active paid
   subscription, users may use ZCode and its generated content for
   commercial purposes; without one, only non-commercial personal research
   and study.
10. **Licensing of the bundled runtime:** no open license is granted for
    the runtime itself; no redistribution right is documented. Apache-2.0
    status of this repository is unaffected; no vendor code may be copied
    into it.

**Terms conclusion (medium-high confidence):** subscription-backed
execution of third-party client traffic through a router is not addressed
approvingly anywhere in the terms, and clause IV.3's "unauthorized proxy
server" prohibition plus III.3's account-exclusivity clause make the
execution-gateway use case non-compliant or of unresolved legality without
explicit written vendor consent. Terms findings are recorded separately
from execution feasibility and never imply eligibility (D-039).

## 6. Isolation assessment vs D-044 (local-adapter isolation rules)

| D-044 requirement | ZCode 3.14.0 reality | Assessment |
|---|---|---|
| Session isolation from unrelated conversation history | One user account holds all sessions; side conversations inherit main-session history; forking carries history; no documented "fresh isolated session" interface | NOT met by product; would require OS-level containment (dedicated OS user/VM/container with dedicated `HOME`) |
| Filesystem isolation | No sandbox documented; vendor terms themselves recommend VM/sandbox; remote development can place agent execution inside a user-supplied Docker container, but configured per-workspace in the GUI, not scriptable | PARTIAL path exists (Docker/WSL remote targets) but not automatable through a supported interface |
| Tool isolation | Agent executes shell/browser/files locally per its four permission modes; unattended use requires Full access or hook-based allows | Containment must be external (OS sandbox); product alone cannot bound it |
| No global MCP/plugin import | Project-scoped `.zcode/config.json` MCP servers auto-connect without approval; plugins install enabled by default with code-execution trust; Claude Code marketplace preloaded; one-click import from `~/.claude`/`~/.codex` configs | NOT met by product defaults; an experimental sandbox would need a dedicated `HOME` plus sanitized workspace (no `.zcode/`, `.agents/`, plugin dirs) |
| No root execution / no Docker socket / no arbitrary mounts | No product mechanism enforces this | External containment responsibility |
| Provider-managed credentials stay provider-managed (D-018) | Credentials live in ZCode's per-device encrypted store / v2 config | Compatible in principle |
| Permission model explicitly mapped before execution | Mapped (section 2.10) | Done |

**Conclusion:** D-044 isolation is achievable only by containing the entire
ZCode process tree in a dedicated environment (separate OS identity, dedicated
`HOME`, sanitized workspace, no inherited plugin/MCP config). No product
feature provides these guarantees; several product defaults (auto-connect
MCP, auto-enabled plugins, history inheritance) actively work against it.

## 7. Not tested / not determinable in Stage 1

- **No live inference probe was run.** Rationale: the local runtime is
  desktop-attached and actively in use by the owner; driving it (or
  spawning a second instance) risks disturbing live sessions and would
  consume the owner's authorized quota without being necessary for the
  verdict — documentation alone settles the headless question negatively.
- **IPC protocol details** (message schema, handshake, authorization) —
  undocumented; static bundle analysis shows internal flags only. Reverse
  engineering them would violate the vendor terms (IV.3) and is out of
  scope.
- **Windows-side desktop install details** on this machine — not needed
  for the verdict; the WSL-side runtime was sufficient.
- **Idle-time task eligibility mechanics** — server-side eligibility
  checking is documented as existing, but its rules ("Unable to verify
  eligibility right now") are not public.
- **Per-request usage attribution through any future execution path** —
  no interface exists to test.
- **Whether Z.ai would grant explicit consent for router-style automation**
  — requires a vendor conversation; not attempted (owner decision).

## 8. Bottom-line recommendation

**NO-GO for a supported ZCode execution adapter on the current product
(2026-09-19); do not start Stage 2 on this evidence.** An experimental-only
track remains defensible but only under all of the following conditions:

1. **Explicit vendor consent** covering automation-driven use of the ZCode
   runtime and the "unauthorized proxy server" clause (IV.3), obtained by
   the owner in writing; without it, terms risk is unresolved, not merely
   undocumented.
2. **Dedicated containment** per D-044: separate OS identity, dedicated
   `HOME`, sanitized workspace with no inherited plugins/MCP config,
   no privileged execution — the product provides none of this itself.
3. **Fail-closed version pinning:** adapter active only for the exact
   evidenced runtime version (3.14.0 as of 2026-09-19); any drift or
   contract change disables it safely (issue acceptance criterion);
   re-enablement requires new dated evidence.
4. **Honest accounting:** account-level quota only (existing collector);
   promotional/campaign eligibility stays UNKNOWN and out of routing
   preferences until separately proven (D-039).
5. **No redistribution** of any bundled runtime code; no reverse
   engineering of the internal IPC; the unofficial wrapper remains
   research evidence only.

**Supported alternative that already exists:** the GLM Coding Plan
subscription is vendor-documented for direct API-key use through
OpenAI-compatible (`https://api.z.ai/api/coding/paas/v4`) and
Anthropic-compatible (`https://api.z.ai/api/anthropic`) endpoints — that is
the honest subscription-backed execution channel for the gateway (M04
generic adapter; distinct resource and entitlement per D-042, terms
suitability tracked under U-009), and it does not require wrapping the
ZCode desktop runtime. ZCode-specific value (idle-time capacity, desktop
automations) has **no supported external interface** and should be treated
as unavailable to the router until the vendor ships one or consents
explicitly.

**Re-verification trigger:** any vendor release of an official CLI/SDK,
automation API, or automation-terms change reopens this question; re-verify
https://zcode.z.ai/en/terms and the docs sidebar with a fresh date before
any Stage 2 decision.
