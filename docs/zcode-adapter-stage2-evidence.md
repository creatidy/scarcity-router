# M07 Stage 2 reopen evaluation: ZCode CLI execution evidence

- **Issue:** BioMedical-IT/scarcity-router#92 (M07)
- **Date:** 2026-09-28
- **Status:** Re-evaluation performed per the D-047 reopen condition.
  Gates A, B and D are carried from the first evaluation unchanged; gate
  C was re-evaluated on the owner's direction (2026-09-28) against the
  single-owner deployment scope — **PASS** (decision **D-061**,
  revised). **Stage 2 is REOPENED**: implementation is authorized
  subject to the section-7 constraints and retained exclusions. This
  document remains evidence-only; no adapter code is part of this
  change.
- **Supersedes (via D-061):** D-047's Stage-2 NO-GO, through the
  point-4 reopen procedure D-047 itself defined.
  [`docs/zcode-adapter-stage1-evidence.md`](zcode-adapter-stage1-evidence.md)
  remains a valid historical record; this document adds current
  observations against that reopen condition.

## 1. What changed since Stage 1 (2026-09-19)

Stage 1 found **no official supported programmatic surface** and closed
Stage 2 (D-047). ZCode has since published a first-party, open-source CLI:

- Repository: `github.com/zai-org/ZCode`, first-party code under root
  `LICENSE` (Apache-2.0), `NOTICE.md` present.
- Release **v3.14.3**, published 2026-09-24 (commit `29628c9`); README
  states "2026-9-23: 更新至 ZCode v3.14.3 版本".
- The Agent CLI source lives at `apps/zcode-cli/` (workspace root
  package `zcode-cli` `0.16.9`, injected into the built bundle
  `dist/zcode.cjs` as its reported version; the inner `@zcode/cli`
  package manifest is `0.1.0`; product release is v3.14.3).
- `NOTICE.md` officially documents non-interactive automation:
  "独立 CLI 通过 `--prompt` 执行非交互任务时，未指定 `--mode` 会采用
  yolo" (the standalone CLI running non-interactive tasks via `--prompt`
  adopts yolo mode when `--mode` is unspecified). Its automation-risk
  table warns that subagents, workflows and background tasks
  ("子代理、工作流与后台任务") "可继续调用模型、执行命令并产生费用或
  外部副作用" (may continue calling models, executing commands, and
  incurring costs or external side effects).

All sources in this document were retrieved or probed **2026-09-28**
unless stated otherwise.

## 2. Verified CLI surface (first-party: v3.14.3 source @ `29628c9`, plus the locally built binary)

| Surface | Evidence |
| --- | --- |
| Non-interactive execution | `--prompt <text>` / `-p` (built binary help: "Run a single prompt without opening the TUI"); `--target <objective>` (goal mode; mutually exclusive with `--prompt` per `apps/zcode-cli/packages/cli/src/run.ts:46`) |
| Workspace selection | `--cwd <path>` |
| Permission modes | `--mode <build\|edit\|plan\|yolo>`; validation in `run.ts:136-141`; built-binary help prints "(default: yolo for --prompt)"; source constant `DEFAULT_HEADLESS_PROMPT_MODE = "yolo"` (`run.ts:42`) |
| Structured output | `--output-format <text\|json\|stream-json>` with strict validation (`run.ts:115-128`: an unknown value is an error, never silent text); `--json` boolean |
| Streaming events | `stream-json` writes one NDJSON session event per line, terminated by a final `type:"result"` line carrying the same fields as `--json` (`packages/cli/src/prompt-command.ts:347-373`) |
| Result semantics | Success: exit `0` + final result (JSON fields: `sessionId`, `traceId`, `turnId?`, `response`, `usage?`, `projection.status`, token/context numbers). Failure: exit `1`, `Error: <message> (traceId: …)` on stderr (`prompt-command.ts:420-432`) |
| Signal handling | `registerCliShutdownHandlers` aborts the run controller on SIGINT/SIGTERM and runs bounded cleanup (`prompt-command.ts:144-151`) |
| Approval enforcement | Permission service: `plan` = read-only only; `build` = read-only + low-risk session-local state, side-effecting/high/critical tools require approval (`packages/core/src/permission/service.ts:450-507`); with no permission client attached the default broker DENIES ("No permission client configured for <tool>", `packages/core/src/permission/broker.ts:24-31`), and the headless workflow path imports `createDenyPermissionBroker` explicitly |
| Other | `app-server` / `agent-server` protocol commands exist (`isProtocolServerInvocation`, `arguments.ts:112-126`); `--disallowed-tools` per-run tool denylist; `--resume <sessionId>`; `doctor --json` runtime probe; `login [zai\|bigmodel]` browser-OAuth auth and a shared Z.AI credential store (`logout`: "Remove the shared Z.AI login credentials") |

Deserialization note: the final stream line and `--json` output are
documented, strictly validated at the flag level, and were parsed live
(section 4). Per-field schema drift is still possible across CLI
versions; any future adapter must parse conservatively (the same
fail-closed discipline as the Codex adapter evidence re-pins).

## 3. Permission-model analysis (task gate B)

The hazard named in the reopen task is real and official: headless
`--prompt` **defaults to yolo** (help text, `NOTICE.md`, and source
agree). But a supported safe path exists:

1. `--mode build` (or `plan`/`edit`) is a first-party supported flag.
2. In `build` mode, read-only tools and low-risk session-local state run;
   anything with side effects, high or critical risk returns an `ask`
   decision.
3. A headless run has no permission client to answer `ask`; the default
   `DenyPermissionBroker` denies the tool call. Failure is therefore
   fail-closed: an unrestricted action is refused, not silently executed.
4. Defense-in-depth is available via `--disallowed-tools` (per-run tool
   removal).

**Gate B verdict: PASS** — an acceptable headless permission model is
available, provided an adapter always passes `--mode build` (never
relying on the yolo default) and treats a denial as a typed outcome.

## 4. Live feasibility spike (2026-09-28, this workstation)

Method: the official v3.14.3 source (`29628c9`) was built with its own
toolchain (`pnpm@10.33.2`, Node 24.19.0, Linux x64; `corepack pnpm
--filter @zcode/cli... build` → `dist/zcode.cjs`, 29.7 MB). No wrapper
scripts; the bundle was invoked directly as `node <argv…>`. Probes ran in
a disposable workspace (`/tmp/zcode-spike`); no repository or user
configuration was modified; no credentials were printed or exported.

| Probe | Result |
| --- | --- |
| `--version` | `0.16.9`, exit 0 |
| `--help` | Full command/flag listing, exit 0 (quoted in section 2) |
| `doctor --json` | Exit 0; structured `{cli:{name,version}, runtime:{arch,cwd,execPath,node,platform}, packaging:{default,sea}}`; no secrets |
| Trivial prompt, `--output-format stream-json --mode build --cwd <tmp> --no-browser` | Ran the full headless pipeline: 6 NDJSON events (`session.titleUpdated`, `turn.started`, `session.updated`×3, `turn.failed`), then exit 1 with a structured error payload |
| Failure classification | `turn.failed` payload carried typed attribution: `providerErrorCode=MODEL_TLS_VALIDATION_FAILED`, `providerId`, `modelId`, `providerKind=openai-compatible`, `transport=sse`, `retryable=false`; stderr carried the exception; exit 1 |
| Model steering via `/model` slash prefix | Not honored headless (same pinned model used); no supported headless model flag exists in help |
| SIGTERM at ~1.5 s | Process terminated promptly (exit 143); no orphaned children (`pgrep` clean); bounded cleanup path registered in source |
| Timeout bounding | Every probe wrapped in `timeout N`; the CLI respects process kill; no TTY required |

**Environmental caveat (recorded honestly):** the workstation's shared
ZCode configuration pins a custom provider
(`providerId=new-provider`, `modelId=sr-pin:precision-codex-live:…`),
whose endpoint currently fails the CLI's TLS validation
(`MODEL_TLS_VALIDATION_FAILED`). The CLI **refused the endpoint instead
of bypassing certificate validation** (correct fail-closed behavior), so
a *successful* model completion was not observed in this environment.
Credentials were present and used (the run reached a live provider
call). The remaining live gate for a green completion is a TLS-valid
provider endpoint (e.g. the Z.ai Coding Plan login path via
`zcode login zai`, which requires an interactive browser OAuth and was
not exercised). Nothing about the CLI surface itself blocked the spike.

**Gate D verdict: PASS for the interface** (invocation, structured
events, failure typing, exit-status discrimination, timeout bounding,
stderr separation, OS-boundary cancellation all demonstrated);
completion-path success remains environment-blocked as described.

## 5. Terms and licensing (task gate C — re-evaluated 2026-09-28)

Gate C was re-evaluated on the owner's direction against the concrete
deployment scope below, after the first pass of this document concluded
UNCLEAR from a Stage-1-style reading. The re-evaluation uses only
current official ZCode/Z.ai sources, all retrieved **2026-09-28**:

- **S1** — ZCode Terms of Service, effective 2026-06-15
  (https://zcode.z.ai/en/terms) — the same terms version Stage 1
  analyzed; analyzed here in full.
- **S2** — Z.AI platform Terms of Use, updated 2026-04-14
  (https://docs.z.ai/legal-agreement/terms-of-use.md).
- **S3** — GLM Coding Plan Usage Policy
  (https://docs.z.ai/devpack/usage-policy.md).
- **S4** — Subscriptions, Fees, and Payment, §4 Usage Rules
  (https://docs.z.ai/legal-agreement/subscription-terms.md).
- **S5** — GLM Coding Plan Overview (https://docs.z.ai/devpack/overview).
- **S6** — devpack guide "Using GLM Coding Plan in ZCode"
  (https://docs.z.ai/devpack/tool/zcode.md).

**Deployment scope under evaluation** (single-owner local
integration): one natural-person account owner; the owner's own paid
GLM Coding Plan; ZCode installed and authenticated through supported
mechanisms; Scarcity Router launches the official ZCode CLI locally as
a subprocess; all credentials remain local to ZCode's own store; no
third party receives access to the account; no public or multi-user
gateway is provided; no resale, no account pooling, no quota
circumvention; no scraping, private IPC, reverse engineering or
spoofing; ordinary coding work inside the owner's authorized
workspaces.

### 5.1 Is local subprocess invocation "using ZCode as a proxy server"? — No

S1 §IV prohibits "any activities that endanger the network or system
security of ZCode", including "using ZCode as a virtual server,
unauthorized proxy server, or mail server". The clause sits in a list
of network-infrastructure misuse. In the proposed architecture no
resource is proxied, to no one, through no network or service boundary:
ZCode is not placed in front of any resource for any requester. The
only model-traffic client remains the ZCode CLI process itself,
authenticated as the account owner and speaking to Z.ai endpoints
exactly as in interactive use; Scarcity Router launches it as a local
child process, exposes no listener that fronts ZCode, relays no network
traffic through it, and holds no credentials. What the prohibition
*would* reach — an endpoint relaying third parties' requests through
ZCode, resale of access, account pooling (cf. S4 §4.2: "you may not
resell, sub-resell, repackage, aggregate, proxy or otherwise provide
the GLM Coding Plan to any third party ... nor may you use the GLM
Coding Plan to provide model capabilities as a service to third
parties") — is exactly what the scope excludes. The first evaluation's
statement that "a router that receives requests and executes them
through the ZCode runtime is, by ordinary meaning, proxy-style use"
inferred the prohibition from the product's routing function; that
inference is retracted.

### 5.2 Does owner-controlled software count as a "third party"? — No

S1 §III makes the account "for your exclusive use only" and voids
"any direct or indirect authorization of a third party to use your
account"; §III further prohibits gifting, lending, renting,
transferring or selling to "anyone other than the original registrant";
S3 prohibits "Account sharing or multi-user access"; S4 §4.3 licenses
the plan "only to the individual natural person associated with such
account" and flags "bulk or automated usage on behalf of others". In
every one of these clauses a third party is another person or
organization distinct from the registrant. The Terms themselves supply
the distinction the question turns on: S1 §III deems "All activities
conducted through your account" to be "your actions", and S1 §IV states
that "All operations performed by ZCode based on your instructions
constitute an extension of your own actions ... your own conduct,
under your control and at your sole responsibility". Software acting
for the account owner is the owner's instrument, not another principal;
no other natural or legal person receives or exercises account access.
In the proposed deployment the only person whose work reaches ZCode is
the registrant, so no direct or indirect third-party authorization
occurs. No clause in S1–S4 equates the owner's own software with a
third party, and the deeming language above affirmatively cuts against
that reading.

### 5.3 Do the Terms independently prohibit automation or headless use? — No

The automated-means clauses are purpose-bound. S1 §IV prohibits
"Accessing, obtaining, or monitoring any data, content, or gaining
access to unauthorized servers or accounts ... by means of deep
linking, page scraping, bots, spiders, or other automated methods";
S2 §III.4(b) likewise forbids automated means used "for the purpose of
accessing, obtaining, or monitoring any unauthorized data, content, or
servers/accounts". The object is scraping and unauthorized access —
what is reached by the automation — not the fact of invoking the
vendor's own product. A wording asymmetry is acknowledged: S1 lacks
S2's "unauthorized" qualifier on "data, content". The literal reading
is governed by the chapeau — the list item prohibits activities "that
endanger the network or system security of ZCode", so the automated
means must be used for the security-endangering access the item
describes; a reading that banned any automated use of ZCode's own
documented automation surfaces (the CLI's `--prompt` path, section 2;
`NOTICE.md`'s unattended-task risk table; the vendor's own usage policy
contemplating subagent concurrency, S3) would condemn the product's
first-party functionality. Official first-party material presents
headless automation as a supported pattern, and S1 §IV's own
contemplation of ongoing AI-assisted operations under oversight
confirms the reading: automation is regulated — by
the oversight duty and the security-file clause, both recorded as
constraints in section 7 — not forbidden.

### 5.4 Does the paid GLM Coding Plan satisfy the commercial-use clause? — Yes

S1 §VI.2: "You fully understand and agree that, unless you have entered
into a separate agreement with us or have subscribed to the
corresponding services on ZCode and paid the applicable fees, you may
only use ZCode and its Generated Content for non-commercial, personal
research and study purposes." The Terms themselves lift the
non-commercial limitation upon a paid subscription to the corresponding
service; a separate written authorization is not required by their text
for that path. Official documentation establishes that the owner's paid
GLM Coding Plan is such a corresponding service for coding: S5 ("The
GLM Coding Plan is a subscription package designed specifically for
AI-powered coding"; paid tiers; the official campaign has paid-plan
users using GLM-5.3-Flash via ZCode), S6 (the vendor's own guide to
consuming the plan inside ZCode, including the "Use Subscription"
binding step), and S1 §IV/VIII (ZCode offers paid subscription plans
billed "in accordance with the paid subscription rules displayed on
the interface"). The deployment under evaluation is exactly this: the
owner's own paid plan, consumed through the official tool, for the
owner's own coding work. The exception covers the subscribed coding
use; it does not cover providing capabilities to third parties, which
S4 §4.2 prohibits separately and which remains excluded.

The related supported-tools rule (S3: the plan "may only be used within
officially supported tools and products"; S4 §4.2: quota "only used
within officially supported tools", with "general-purpose API access or
any scenarios outside such tools" — e.g. "directly invoking model APIs
from your own applications, bots, websites, SaaS products or other
systems" — reserved to a separate written agreement) is satisfied by
construction: the only model client is the official ZCode tool itself.
An adapter that extracted plan credentials or called the coding
endpoints directly from Scarcity Router would fall outside this
evaluation.

### 5.5 Remaining clauses — none clearly prohibits this deployment

Clause-by-clause against the stated scope: account exclusivity (S1
§III) — not triggered (5.2); the network-security list (S1 §IV,
including scraping and the virtual/proxy/mail-server item) — not
triggered (5.1, 5.3); reverse engineering — inapplicable, the CLI is
official Apache-2.0 code invoked through its documented argv; the
security-file clause (S1 §IV: "strictly prohibited from modifying,
deleting, or attempting to bypass any local files containing such
security restrictions") — not triggered and affirmatively respected
(constraint 3, section 7); third-party provision clauses (S1 §IV; S4
§4.2) — not triggered; the oversight duty (S1 §IV: "you should
maintain appropriate oversight and intervene manually or terminate the
operation when necessary") — an obligation, satisfiable by the gate-B
design, not a prohibition; "Any other use that may damage our
interests" (S1 §IV) — a generic reservation, insufficient under the
evaluation rule absent a causal connection, and none exists for
single-owner paid use through the official tool. Four further clauses
were swept and not reached: the traffic/engagement clause (S1 §IV:
manipulating, redirecting or hijacking "traffic, readership, or
engagement related to ZCode's products and services" — local process
orchestration manipulates no ZCode-facing traffic); the competing-models
clause (S1 §IV: developing or training technologies "that compete
directly or indirectly with ZCode" — Scarcity Router trains no models
and is a routing/recommendation layer over providers); the high-risk
automated-decision scenario limit (S1 §IV; S2 §III.6(a): automated
decisions in healthcare, legal, credit and similar domains — coding
model selection is outside it); and S2's Additional Terms for API
Services (they govern direct Z.ai platform API integrations — a
materially different architecture; the deployment here consumes the
plan through the official tool, see 5.4).

**Gate C verdict: PASS, scoped to the deployment scope stated above.**
The two interpretive points (5.1, 5.2) are textual interpretations of
official sources, not vendor statements; both are supported by the
clauses' objects and by S1's own deeming language. The first
evaluation's UNCLEAR rested on the proxy-style inference (5.1), an
absent third-party analysis (5.2), and no analysis of §VI.2's
paid-subscription exception (5.4). D-047's caution was formed when no
official external interface existed and the contemplated path was
undocumented desktop IPC — a materially different fact pattern from
the official CLI evaluated here.

## 6. Gate summary and consequence

| Gate | Verdict | Basis |
| --- | --- | --- |
| A — supported external interface | **PASS** | Section 1–2: first-party Apache-2.0 CLI, documented `--prompt` headless path, structured output, official release v3.14.3 |
| B — acceptable headless permission model | **PASS** | Section 3: `--mode build` + fail-closed deny broker; yolo default is explicit and avoidable |
| C — terms permit the intended integration | **PASS (scoped)** | Section 5: clause-by-clause official-source analysis against the single-owner deployment scope; constraints and exclusions retained |
| D — live feasibility | **PASS (interface)**, completion environment-blocked | Section 4 |
| E — production adapter | **AUTHORIZED, not entered here** | A–D pass; implementation authorized subject to section 7; this change remains documentation-only |

Consequence: gates A–D pass and gate C passes under the recorded
single-owner scope with the section-7 constraints and retained
exclusions. **M07 Stage 2 is REOPENED**: **D-061** (revised) supersedes
D-047's NO-GO through D-047's own point-4 reopen procedure, and
implementation is authorized. The adapter itself is a follow-up task.

## 7. Implementation constraints and retained exclusions

The implementation path reuses existing infrastructure: the D-056
responsibility boundary and capability-intersection model, the Codex
worker-adapter patterns (spawn/argv, bounded drain, cancellation, typed
failure classification, redaction), and the compatibility-matrix
evidence discipline (D-043) — no new execution framework is needed.
Design requirements carried forward from gates B and C:

1. Always pass an explicit safe permission mode (`--mode build` or
   stricter); never rely on the yolo default; treat tool denials as
   typed outcomes.
2. Never extract, log, or reuse ZCode/Z.ai credentials; authentication
   stays inside the CLI's supported login/binding mechanisms.
3. Never modify, delete, or bypass ZCode's security-restriction or
   permission configuration (S1 §IV); approvals flow only through
   supported flags and modes.
4. Consume the plan only through the official CLI; never call the
   coding endpoints directly from Scarcity Router (S3; S4 §4.2).
5. Maintain operator oversight: bounded runtimes, cancellation, typed
   outcomes and reviewable logs (S1 §IV).
6. Pin capability claims to dated CLI versions (D-043 discipline).

**Retained exclusions** — outside this evaluation, each requiring a new
decision before any such use: sharing credentials or account access
with any third party; a public or multi-user ZCode gateway; resale or
repackaging of access; account pooling; quota circumvention; use of the
plan outside officially supported tools; providing model capabilities
as a service to third parties (S4 §4.2); and modifying or bypassing
ZCode's security-restriction or permission configuration (constraint 3).
Any widening of the deployment
scope beyond the single-owner integration voids this evaluation.
