# M07 Stage 2 reopen evaluation: ZCode CLI execution evidence

- **Issue:** BioMedical-IT/scarcity-router#92 (M07)
- **Date:** 2026-09-28
- **Status:** Re-evaluation performed per the D-047 reopen condition; Stage 2
  remains NO-GO (decision **D-061**). This document preserves the dated
  evidence for that evaluation. It does not authorize an implementation.
- **Supersedes nothing:** D-047 and
  [`docs/zcode-adapter-stage1-evidence.md`](zcode-adapter-stage1-evidence.md)
  remain valid historical records; this document adds current observations
  against the D-047 reopen condition (point 4).

## 1. What changed since Stage 1 (2026-09-19)

Stage 1 found **no official supported programmatic surface** and closed
Stage 2 (D-047). ZCode has since published a first-party, open-source CLI:

- Repository: `github.com/zai-org/ZCode`, first-party code under root
  `LICENSE` (Apache-2.0), `NOTICE.md` present.
- Release **v3.14.3**, published 2026-09-24 (commit `29628c9`); README
  states "2026-9-23: 更新至 ZCode v3.14.3 版本".
- The Agent CLI source lives at `apps/zcode-cli/` (workspace
  `@zcode/cli`, built bundle `dist/zcode.cjs`; package reports its own
  version `0.16.9`; product release is v3.14.3).
- `NOTICE.md` officially documents non-interactive automation:
  "独立 CLI 通过 `--prompt` 执行非交互任务时，未指定 `--mode` 会采用
  yolo" (the standalone CLI running non-interactive tasks via `--prompt`
  adopts yolo mode when `--mode` is unspecified) and warns that
  "无人值守的自动化……可继续调用模型、执行命令并产生费用或外部副作用"
  (unattended automations may continue calling models, executing
  commands, and incurring costs or external side effects).

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
| Signal handling | `registerCliShutdownHandlers` aborts the run controller on SIGINT/SIGTERM and runs bounded cleanup (`prompt-command.ts:142-151`) |
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

## 5. Terms and licensing (task gate C)

ZCode Terms of Service, effective **2026-06-15**, re-retrieved
**2026-09-28** (https://zcode.z.ai/en/terms) — **the same terms version
Stage 1 analyzed and D-047 relied on**. The operative findings are
unchanged (full analysis: Stage-1 evidence doc section 5):

1. **Account exclusivity (III):** "Your ZCode account is associated with
   your personal information and is for your exclusive use only";
   lending/renting/transferring access is prohibited. Owner-only gateway
   scoping narrows this but does not authorize third-party-style
   routing.
2. **Proxy prohibition (IV):** prohibited conduct includes "using ZCode
   as a virtual server, **unauthorized proxy server**, or mail server".
   A router that receives requests and executes them through the ZCode
   runtime is, by ordinary meaning, proxy-style use; whether it is
   "authorized" requires explicit vendor consent. **This remains the
   central terms risk for the M07 use case.**
3. **Automation methods (IV):** accessing data or services "by means of
   deep linking, page scraping, bots, spiders, or other automated
   methods" is prohibited; `NOTICE.md` documenting unattended CLI
   automation as a product behavior is not a license for proxy-style
   integration by a third-party service.
4. **Oversight duty (IV):** operations executed by ZCode are "an
   extension of your own actions"; users must review high-risk
   operations and maintain oversight — an unattended execution channel
   sits in tension with this absent an explicit safe-mode design (which
   gate B shows is buildable).
5. **No quota/account pooling:** the terms' billing and exclusivity
   clauses prohibit pooling or circumvention designs.

What *did* change since D-047: the interface now exists (section 1) and
is Apache-2.0 first-party code, so the *reverse-engineering* and
*redistribution* concerns from Stage 1 are greatly reduced — invoking
the officially shipped CLI is neither reverse engineering nor
redistribution. The **proxy-authorization gap is untouched**: no written
vendor authorization exists, and the owner has not obtained one.

**Gate C verdict: UNCLEAR — treated as BLOCKED for production** (the
reopen task's rule applies: uncertainty is not silently treated as
permission).

## 6. Gate summary and consequence

| Gate | Verdict | Basis |
| --- | --- | --- |
| A — supported external interface | **PASS** | Section 1–2: first-party Apache-2.0 CLI, documented `--prompt` headless path, structured output, official release v3.14.3 |
| B — acceptable headless permission model | **PASS** | Section 3: `--mode build` + fail-closed deny broker; yolo default is explicit and avoidable |
| C — terms permit the intended integration | **UNCLEAR / BLOCKED** | Section 5: terms unchanged since D-047; proxy-style use still lacks vendor authorization |
| D — live feasibility | **PASS (interface)**, completion environment-blocked | Section 4 |
| E — production adapter | **NOT ENTERED** | Requires A–C pass; C does not pass |

Consequence per the reopen task's own rule ("a clean BLOCKED result is
preferable to an integration built on an unsupported or unsafe
contract"): no adapter is implemented in this round. **D-061** records
the updated reopen state.

## 7. What would reopen Stage 2

1. **Written vendor authorization** for proxy-style automation of the
   ZCode runtime (or a terms revision that clearly permits it), scoped
   at minimum to owner-account, single-administrator deployments; or
2. A vendor-official integration surface whose terms explicitly cover
   router-style execution (for example a documented execution API).

Upon either condition, the implementation path is already sketched by
existing infrastructure: the adapter would reuse the D-056
responsibility boundary and capability-intersection model, the Codex
worker-adapter patterns (spawn/argv, bounded drain, cancellation,
typed failure classification, redaction), and the compatibility-matrix
evidence discipline (D-043) — no new execution framework is needed.
Gate B's design requirement carries forward unchanged: always pass
`--mode build`, never rely on the yolo default, treat tool denials as
typed outcomes, and pin capability claims to dated CLI versions.
