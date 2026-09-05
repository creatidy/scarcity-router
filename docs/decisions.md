# Decision log

This file records durable cross-cutting choices and unresolved decisions. Topic
details remain in their authoritative documents; entries here explain why a
direction was chosen. Dates use UTC.

## Accepted decisions

### D-001 — Recommend, do not proxy

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** The service returns a model recommendation, alternatives and an
  explanation. It does not receive prompts, proxy model traffic, execute work or
  automatically dispatch fallbacks.
- **Reason:** This directly solves quota allocation while sharply reducing
  security exposure and integration coupling.

### D-002 — Four independent inputs

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Capability, task requirement, runtime capacity and user policy
  remain separate domain concepts.
- **Reason:** Quota changes scarcity, not model quality; static mappings cannot
  represent changing subscription headroom.

### D-003 — Normalize at provider edges

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Small read-only provider adapters emit a shared capacity model.
  Provider parsing is forbidden in the selector and public interfaces.
- **Reason:** Provider drift is expected; it must degrade one adapter rather
  than destabilize the core.

### D-004 — Consider every relevant quota window

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Effective headroom uses the most constrained relevant validated
  window, never the most optimistic one. Unknown semantics remain explicit.
- **Reason:** A 98%-remaining five-hour window can coexist with only 2% weekly
  remaining.

### D-005 — Continuous scarcity with reservations

- **Status:** Accepted concept; parameters to validate in M2
- **Date:** 2026-09-01
- **Decision:** Use a continuous scarcity penalty, initially proposed as
  `(1-r)^2`, while presentation labels and reservation thresholds remain
  configurable. A reserved model remains eligible for sufficiently high-level
  work.
- **Reason:** Avoid discontinuous routing at arbitrary percentage boundaries and
  preserve premium capacity without declaring it unavailable.

### D-006 — Multidimensional capability, narrow catalog

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Capability uses domain dimensions plus hard properties; no
  global model tier. The initial catalog covers only the five real model
  families/configurations in scope and carries provenance/confidence.
- **Reason:** Suitability is domain-specific and ratings are curated judgments,
  not quota measurements or universal scientific rankings.

### D-007 — CLI, REST and MCP share one core

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** CLI is first; REST becomes the generic machine contract; MCP is
  a thin adapter, preferably stdio locally. Business logic remains in one core.
- **Reason:** Clients vary, while selection semantics must not.

### D-008 — Initial providers and roadmap order

- **Status:** Superseded by D-017 (2026-09-05)
- **Date:** 2026-09-01
- **Decision:** The superseded v0.1 scope explored OpenAI/Codex, Z.ai Coding
  Plan and local Ollama. Claude was to be evaluated next only after a
  security/maintenance review.
- **Reason:** Solve the owner's workflow before pursuing provider breadth.

### D-009 — Security defaults

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Reuse existing local authentication read-only, never expose
  credentials, validate HTTPS and exact hosts before Authorization, and bind
  REST to `127.0.0.1` by default.
- **Reason:** Subscription credentials are the principal sensitive asset.

### D-010 — Licensing

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** License the project under Apache License 2.0. Preserve notices
  for any substantially adapted third-party MIT code.
- **Reason:** Permissive commercial integration plus an explicit patent grant.
  MPL-2.0 remains an alternative only after a deliberate policy change; AGPL is
  not the default.

### D-011 — Public hosting

- **Status:** Accepted for public release
- **Date:** 2026-09-01
- **Decision:** GitHub is canonical and the owner's Forgejo receives an
  automatic mirror.
- **Reason:** Public discovery, contributions, Issues, Discussions, Actions and
  security ecosystem should live on the canonical host.

### D-012 — Name remains provisional

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** `Scarcity Router`/`model-broker` is a working label only. Perform a
  GitHub, package registry, domain and general collision search before release.
- **Reason:** Implementation value precedes branding; the final name should be
  memorable and searchable rather than another generic router name.

### D-013 — Likely stack is guidance, not architecture

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Python 3.12+, `uv`, `pytest`, typed models, a small CLI/HTTP layer
  and official MCP SDK are the likely stack. Each dependency still needs to
  justify itself at implementation time.
- **Reason:** The work is modest I/O, schemas and deterministic policy; a large
  framework chain would weaken the intended stable control point.

### D-014 — Portable model policy artifact

- **Status:** Accepted
- **Date:** 2026-09-03
- **Decision:** Keep the canonical portable model policy as machine-readable
  [`model-policy.json`](../model-policy.json) at the repository root. It records
  non-exclusive descriptive model classes, capability vocabulary, task-profile
  relationships and dated owner workflow exemplars.
- **Reason:** Other repositories can consume one stable JSON artifact without
  parsing Markdown, while the selector still evaluates explicit capability
  minima, hard constraints, runtime capacity and scarcity/reservation policy
  independently.
- **Boundary:** Classes are not rankings and do not participate directly in
  eligibility. `translation_multilingual` is a first-class capability
  dimension; a dedicated orchestration/long-context quality dimension remains
  deferred. Numeric ratings and profile minima remain an M2 calibration task.

### D-015 — Bounded multi-agent orchestration

- **Status:** Accepted
- **Date:** 2026-09-05
- **Observed failure:** An unbounded review/fix feedback loop allowed an
  independent reviewer and implementation worker to keep expanding the work.
- **Why dangerous:** It consumes unbounded time, expands scope, grows
  complexity, invalidates moving-head reviews and lets a reviewer optimize
  correctness without an explicit cost function.
- **Decision:** Every multi-agent or orchestrated task must declare an explicit
  execution budget, review limits, frozen scope/threat model and stop
  conditions before workers start. The default lifecycle is implementation,
  independent review, at most one remediation, narrowly scoped final
  verification and a human merge gate. Findings are `MERGE_BLOCKER` or
  `DEFER`; only blockers can trigger remediation. Worker and reviewer retries
  are independently bounded at one by default, and a retry only recovers a
  stalled or interrupted session rather than restarting the task or review.
- **Escalation:** Exhaustion of either retry budget, stalled progress after its
  retry budget or a complexity-budget breach stops orchestration and escalates
  to a human.
  Workers, reviewers and orchestrators never merge automatically.

### D-016 — Provisional module status surface

- **Status:** Superseded by D-017 (2026-09-05)
- **Date:** 2026-09-05
- **Decision:** The historical three-provider status experiment used
  `uv run python -m scarcity_router status`, one caller-created observation
  timestamp and safe human/JSON output. It is retained only as context for the
  superseding two-provider status surface.
- **Reason:** This provides one useful local read-only status surface without
  choosing package metadata or a final executable name under U-008 and without
  adding a CLI, provider or configuration framework.
- **Boundary:** This was not REST, MCP, selection, caching or model execution.
- **M1 audit:** A separate `doctor` command is not an M1 blocker now that
  supported discovery/configuration validation and normalized diagnostics are
  visible through `status`. A richer doctor surface is deferred unless normal
  use demonstrates a concrete diagnostic gap.

### D-017 — Local Ollama support removed

- **Status:** Accepted
- **Date:** 2026-09-05
- **Supersedes:** D-008's three-provider scope, D-016's three-provider status
  surface, the local-runtime portion of U-002, U-003's three-provider
  timestamp boundary, and U-005's local inspection/transport decisions.
- **Decision:** Remove Ollama and local-model support from Scarcity Router
  completely. The initial supported environment is OpenAI subscription capacity
  plus Z.ai Coding Plan subscription capacity. There is no supported mechanism
  to discover, query, configure, select or recommend a local model.
- **Reason:** The owner's workflow demonstrated unacceptable local runtime
  operational instability and system interference. Maintaining that provider
  class added complexity without sufficient practical value.
- **Contract effect:** Schema v2 removes `LocalRuntime`,
  `CapacitySnapshot.local_runtime`, their serialization and their diagnostics.
  The status application contains exactly the `openai` and `zai` collectors and
  requires no local-model configuration.
- **M1 effect:** M1 remains **NOT YET PASS**. The remaining blocker is usable
  current OpenAI subscription-capacity windows in the owner's supported
  environment. Any future restoration requires a new explicit product decision.

### D-018 — Bounded provider-managed OpenAI auth recovery

- **Status:** Accepted (owner-approved product/security decision, 2026-09-05)
- **Date:** 2026-09-05
- **Observed failure:** The installed Codex app-server answers
  `account/rateLimits/read` with its JSON-RPC internal error `-32603` when the
  provider-managed auth token it holds is stale, while account metadata stays
  visible. A single official `account/read` request with
  `{"refreshToken": true}` (the installed protocol generation's documented
  managed-auth refresh) restored live quota reads (see
  `docs/poc-evidence.md`, 2026-09-05).
- **Decision:** The OpenAI collector may request one provider-managed
  authentication refresh as a bounded recovery mechanism, exactly when:
  - `initialize` succeeded;
  - the request phase is exactly `account/rateLimits/read`;
  - the matching response is a structurally valid JSON-RPC error whose
    numeric code is exactly `-32603` (error message text is never read);
  and the sequence is exactly: one `account/read` with `refreshToken: true`
  (new request id), then one `account/rateLimits/read` retry (new request id),
  inside the same bounded app-server session and deadline. No loops, no
  backoff, no retry framework.
- **Security boundary:** The token remains entirely provider-managed. Scarcity
  Router never receives, reads, copies, serializes, logs or stores a token;
  the refresh response's account payload is consumed only as protocol framing
  and never interpreted or retained. No login, logout, account change, browser
  inspection, direct auth endpoint or custom credential storage is introduced.
- **Recovery oracle:** A successful `account/read(refreshToken=true)` response
  is not proof that the token renewed (the installed response shape does not
  expose the refresh outcome); the successful subsequent rate-limits retry is
  the only proof of effective recovery. If the refresh request or the retry
  returns a protocol error, the collector fails closed to the existing safe
  `unknown`/`telemetry_unknown` snapshot. The initial rate-limits read remains
  non-mutating, so healthy status calls never refresh.
- **Boundary:** This is an explicit narrow exception to the previous purely
  read-only M1 collector wording (D-003/D-009 wording and the security doc's
  collector-mutation invariant are amended accordingly). Scarcity Router is
  not a credential manager; it triggers the official managed-auth flow of the
  provider's own installed app-server and inspects nothing.

### D-019 — Codex response-generation compatibility and window coverage

- **Status:** Accepted
- **Date:** 2026-09-05
- **Supersedes:** U-010's window-coverage clause ("a main snapshot missing
  either expected window kind never reports `ok`") and U-011's rule that the
  `ordinaryUsageAllowed` permission member governs every live response.
- **Decision:**
  - **Two generations.** The adapter explicitly supports both evidenced
    response generations: the current upstream generation whose envelope
    carries `ordinaryUsageAllowed` (explicit `true` required for ordinary
    usage; `false`/null blocked), and the installed supported generation
    (`codex-cli 0.151.0-alpha.7.2`) whose envelope predates the member. For
    the legacy generation the permission member is never manufactured;
    permission is evaluated from the evidenced legacy blocker contract:
    explicit `spendControlReached == true` blocks, any validated non-null
    `rateLimitReachedType` blocks, v2-unrepresentable credit/spend states
    block, and missing/null `spendControlReached` remains the legacy
    conservative unavailable state that withholds percentage pairs. A
    non-null `rateLimitUpsell` blocks in either generation.
  - **Window coverage is evidence-based.** A provider may legitimately omit
    a quota window. A validated, unblocked main snapshot with at least one
    window whose percentage pair is usable is healthy even when the other
    expected window kind is absent. The absent window is simply absent —
    never synthesized, never guessed at. A snapshot with no window anywhere
    remains insufficient evidence (`unknown`), as does a snapshot whose
    windows all lack usable pairs. Duplicate known periods and malformed
    windows still fail closed.
- **Reason:** Live post-refresh responses from the installed binary (see
  `docs/poc-evidence.md`, 2026-09-05) carry additional limit buckets and may
  supply one main window; the previous structural coverage rule turned honest
  provider evidence into fabricated ignorance.
- **Boundary:** Adapter-edge semantics under the frozen v2 contract. No new
  v2 fields or diagnostics; additional-bucket semantics are unchanged.

## Unresolved decisions

### U-001 — Codex binary discovery and compatibility

- Which installation sources and minimum versions are supported in M1?
- How is the selected binary made visible without exposing unrelated paths?
- Evidence needed: discovery experiments outside the tested VS Code extension.
- **Status:** Narrowly resolved for M1 (2026-09-03); residuals below
- **Decision:** Supported discovery is exactly the VS Code ChatGPT
  extension layout: non-symlink `openai.chatgpt-*` directories under
  `~/.vscode/extensions` or `~/.vscode-server/extensions` (the
  remote-server layout is the directly evidenced PoC environment), scanned
    read-only, ordered deterministically by extension version descending on
    Linux x86-64; Linux ARM64 and Darwin are unsupported until a working
    descriptor-bound execution
   strategy is evidenced. A
  candidate is usable only when its intermediate `bin/<platform>`
  directories and package/executable paths are non-symlink validated beneath
  the selected root, its `codex` file is regular and executable, and a
  duplicate-key-rejecting `codex-package.json` beside it
  validates `layoutVersion` 1 and `variant` `codex`. No installation maps
  to `unavailable`; an unusable installation maps to `unsupported`. There
  is no PATH search, no browser-profile inspection, no install/upgrade, no
  user-configuration mutation, and no generic binary-search framework.
- **Evidence:** 2026-09-03 reconnaissance recorded in
  `docs/poc-evidence.md` ("2026-09-03 M1 Codex collector reconnaissance"):
  the observed extension/package layout, `codex-cli 0.151.0-alpha.7.2`
  matching the PoC, JSONL framing facts, and the absence of a PATH `codex`.
- **Visibility:** the selected binary path, extension version and codex
  version are validated in-process but deliberately not surfaced: the v2
  capacity contract (U-002) has no field for them, and v2 diagnostics are a
  frozen allowlist. Reporting the selected binary/version safely is
  deferred to the M1 `doctor`/`status` work under a future decision.
- **Residuals:** (a) minimum/maximum codex version policy — no version gate
  is enforced. The package `layoutVersion` validates only the installation
  *filesystem layout* during discovery; it is **not** a protocol
  compatibility pin. The real protocol compatibility boundary is runtime
  validation: strict JSONL framing plus the deliberately validated
  `RateLimitSnapshot` shape (U-010), which fails closed to
  `schema_changed`/`unknown` on any drift; (b) other installation sources
  (npm `@openai/codex`, standalone binaries, other editors' extension
  roots) are unsupported until separately evidenced; (c) platform
   directories other than the directly evidenced Linux x86-64 directory are
   unsupported until descriptor-bound execution is evidenced; (d) OpenAI
   app-server failure
  wire shapes (auth required in particular) remain uncaptured, so protocol
  error responses normalize to `unknown` rather than a more specific
  status.

### U-002 — Exact first serialized capacity contract

- **Status:** Superseded by D-017 (2026-09-05)
- **Date:** 2026-09-02
- **Decision:** The superseded M1 adapters targeted an internal v1 capacity
  contract with provider-independent windows, safe diagnostics and no raw
  provider data. Its detailed historical shape is retained only in repository
  history; the current internal contract is v2 in `docs/capacity-model.md`.
- **Reason:** Two independent subscription adapters need the same provider-free
  semantics while Z.ai and Codex wire formats evolve independently.
- **Boundary:** This remains an internal adapter/core contract, not REST, MCP or
  CLI versioning. U-003 owns refresh and staleness policy; M2 owns scarcity and
  selection.

### U-003 — Refresh and staleness policy

- **Status:** Partially resolved for synchronous M1 status (2026-09-05)
- **Decision:** Every `status` invocation performs a fresh sequential collection
  and establishes one canonical UTC millisecond `retrieved_at` immediately for
  that observation attempt. The same value is passed to OpenAI and Z.ai;
  provider observations are never independently timestamped.
- **Boundary:** This resolves the on-demand observation behavior only. No cache
  TTL, background refresh, freshness score, stale threshold or timeout policy
  is invented here.
- **Evidence needed:** Real owner workflow use and observed collector
  latency/reliability before choosing any retained-snapshot or staleness policy.

### U-004 — Z.ai reset metadata and schema drift

- **Status:** Resolved (narrowed residual remains), 2026-09-01
- **Decision:** The reset-field mapping is fixed to `nextResetTime`, a
  13-digit epoch-**millisecond** value carried by every observed window. Window
  identity is the validated `(type, unit, number)` combination:
  `{(TOKENS_LIMIT,3,5): five-hour tokens, (TOKENS_LIMIT,6,1): weekly tokens}`.
  A `TIME_LIMIT` entry is a distinct non-token window and is not a tokens window.
  The future adapter reports any unlisted `(unit, number)` (or a `TOKENS_LIMIT`
  missing `unit`/`number`) as an **unknown** window with preserved raw fields,
  and never defaults a percentage to 0 or 100 — the unknown-window policy from
  `docs/capacity-model.md` governs selection. `percentage` is the **used**
  percentage (evidence-backed via the `TIME_LIMIT` counter triple).
- **Evidence:** 2026-09-01 M1 reconnaissance recorded in
  `docs/poc-evidence.md` ("2026-09-01 M1 reconnaissance") and the redacted
  fixtures in `tests/fixtures/zai-coding-plan/` (known, unknown-window,
  missing-weekly, degraded-values, schema-changed and auth-failed shapes).
- **Narrowed residual (not blocking M1 collector):** (a) confirm used-orientation
  with a second-snapshot check that `percentage` moves with consumption;
  (b) treat the `(unit, number)` mapping and `nextResetTime` cadence as
  provider-specific evidence, not permanent constants, and re-verify on schema
  change; (c) exact `TIME_LIMIT` counter semantics (`usage`/`currentValue`/
  `remaining`) are observed but only the used/remaining reading is relied on.

### U-005 — Local runtime inspection contract

- **Status:** Superseded by D-017 (2026-09-05)
- **Decision:** Earlier M1 work explored a read-only local runtime inspection
  contract and a bounded transport implementation. Those implementation and
  evidence decisions are historical only; no local runtime is a supported
  Scarcity Router input after D-017.

### U-006 — Initial capability ratings and profile thresholds

- No exact model scores are accepted yet.
- Evidence needed: documented benchmark/experience sources, dated model
  versions, confidence and owner review during M2.

### U-007 — Scarcity parameters and policy boundaries

- Validate `(1-r)^2`, label thresholds, reservation boundary behavior and
  unknown ordering through scenario tests before M2 acceptance.
- Reset proximity is preserved but not included in the first formula.

### U-008 — Package, CLI and final project name

- Decide only after a collision search and before publishing an installable M1.

### U-009 — Provider terms and public supportability

- Confirm that public distribution of each collector is compatible with current
  provider terms and maintenance expectations before release.

### U-010 — Codex rate-limit snapshot semantics under v2

- **Status:** Resolved, 2026-09-03 (updated twice same day after schema
  review against the exact tagged schema `rust-v0.151.0-alpha.7.2`)
- **Decision:** The OpenAI adapter validates the complete evidenced
  `GetAccountRateLimitsResponse` envelope and normalizes it under the
  existing v2 contract as follows:
  - **Envelope.** For input deserialization, the JSON Schema requires only the
     `rateLimits` member: missing is drift (`schema_changed`).
     `rateLimitsByLimitId` and `rateLimitResetCredits` are nullable optional
     members, so missing and explicit `null` are accepted input states. The
     tagged Rust serializer does not skip these `Option` fields and the
     TypeScript shape requires both keys; the normal tagged success processor
     emits the map. When the map is present, its exact `codex` mirror is
     required and must equal top-level `rateLimits`; a map without that mirror
     is drift. `rateLimits`
    is required to be the nine-member snapshot (`limitId`, `limitName`,
    `primary`, `secondary`, `credits`, `individualLimit`,
    `spendControlReached`, `planType`, `rateLimitReachedType`); snapshot
    members are option-typed, so missing and null both mean an absent
    state there. Additive scalar members are tolerated at every level;
    additive *structured* members under unknown keys fail closed to
    `schema_changed`.
    Tagged integer fields are width-checked: window `usedPercent` and
    spend-control `remainingPercent` are i32, while window durations/resets,
    reset-credit counts and reset-credit timestamps are i64; out-of-width
    values are schema drift.
  - **Typed states.** `credits` (evidenced `CreditsSnapshot`:
    required boolean `hasCredits`, required boolean `unlimited`, optional
    `balance` as string-or-null), `individualLimit` (evidenced
    `SpendControlLimitSnapshot`: four required fields with string `limit`/
    `used` and integer `remainingPercent`/`resetsAt`) and
    `rateLimitResetCredits` (integer `availableCount` plus optional typed
    `credits` rows requiring `id`, `resetType`, `status` and `grantedAt`, with
    optional nullable `expiresAt`, `title` and `description`) are type-validated: malformed shapes
    are `schema_changed`. Valid credits or individual-limit states have no v2
    representation: they degrade to `status: "unknown"` and withhold the
    percentage pairs (`percentage_unknown` per window); an individual limit
    with `remainingPercent == 0` is a backend blocker. A valid reset-credit
    summary is supplemental telemetry and does not block or withhold current
    quota pairs. Missing/null `spendControlReached` is unavailable and
    conservatively withholds pairs; the `limit` and `used`
    values are strings and are validated structurally only; they are never
    parsed or compared.
  - **Identity.** The main `limitId` must be exactly the evidenced quota
    identity `"codex"`; anything else is `schema_changed`, never healthy.
  - **Coverage.** The main snapshot missing either expected window kind
    (five-hour or weekly) degrades to `status: "unknown"` with validated
    partial windows preserved (`telemetry_unknown`); two slot windows
    sharing one known period are `schema_changed`. An absent window is
    never synthesized and never reported as healthy emptiness.
  - **Backend blockers.** Evidenced blocker classes never yield a healthy
    snapshot: a non-null `rateLimitReachedType` (the exact snake_case enum
    members, with camelCase and arbitrary strings rejected as drift);
    `spendControlReached == true`; an exhausted
    `individualLimit`; and, in any additional bucket, its own reached
    flag, spend-control blocker, exhausted individual limit, or a window
    at `usedPercent == 100`. Each degrades to `status: "unknown"` with
    `telemetry_unknown` and withholds the main percentage pairs. A
    present-but-unblocked additional bucket also degrades to `unknown`
    (v2 cannot represent capacity metered across buckets) while keeping
    the main windows' validated pairs. Known exhaustion of the main quota
    *without* any blocker stays `ok` with the `(100, 0)` pair.
  - **Additional buckets.** The exact success response mirrors the main
    snapshot under `rateLimitsByLimitId["codex"]`; that entry is accepted only
    when it validates consistently with top-level `rateLimits`. Every other
    entry validates as a full quota snapshot with the same membership and
    identity rules (the map key must equal the bucket's `limitId`, be safe to
    compose, and must not shadow `"codex"`). Every validated bucket window is
    emitted with a distinct safe `<limitId>:<slot>` identity; equal periods are
    not merged or discarded. Bucket window coverage is not enforced (a bucket
    may legitimately carry one window).
  - **Plan labels.** `plan` accepts every exact tagged `PlanType` member
    the v2 safe-ID grammar permits as-is (underscores included): `free`,
    `go`, `plus`, `pro`, `prolite`, `team`, `business`, `edu`, `edu_plus`,
    `edu_pro`, `enterprise`, `ent26`, `enterprise_cbp_automation`,
    `enterprise_cbp_usage_based`, `self_serve_business_prolite`,
    `self_serve_business_usage_based`, `unknown`. Values are preserved
    verbatim, never rewritten; a present nonmember is `schema_changed`, never
    silently omitted or leaked.
    `run` is not a member of the tagged enum and is not retained.
  - **Decoding.** JSONL decoding is ambiguity-safe: duplicate object keys
    at any depth, literal NaN/Infinity constants, non-finite exponent
    results such as `1e10000`, and adversarial deep nesting are all
   rejected as protocol drift (`schema_changed`), without broad exception
   swallowing; hybrid messages carrying `method` together with
   `result`/`error` are invalid drift, never silently ignored; the
   installation package file is decoded under the same strict rules.
  - **Request framing.** The generated client notification uses method
    `initialized`, omits `jsonrpc`, and the `account/rateLimits/read` request
    omits `params` because its generated `Option<()>` parameter is empty.
    Request IDs accept strings or signed i64 integers structurally; this
    collector matches only its own numeric IDs `1` and `2`.
    A matching initialize response requires string `userAgent`, `codexHome`,
    `platformFamily` and `platformOs`; response errors require signed i64
    integer `code` and string `message`, without retaining error text.
- **Evidence:** the 2026-09-01 PoC shape plus the 2026-09-03 reconnaissance
  in `docs/poc-evidence.md`, including read-only serde string-table
  inspection of the installed codex binary cross-checked against the
  review-confirmed generated schema for `rust-v0.151.0-alpha.7.2`; no live
  capture was possible (the read errored during reconnaissance), so the
  PoC shape remains the validated success mapping.
- **Boundary:** this is adapter-edge semantics under the frozen v2 contract;
  it adds no v2 fields or diagnostics and does not preempt U-003
  (freshness) or M2 (scarcity/selection).

### U-011 — Current Codex rate-limit response compatibility

- **Status:** Resolved, 2026-09-05 (live acceptance superseded by D-018 and
  D-019; the parser-semantics clauses below remain historical context for the
  current generation)
- **Supersedes:** U-010's previous-schema member set and its rule that a
  missing/null `spendControlReached` is always unusable. U-010's historical
  tagged-schema evidence remains intact above.
- **Previous evidence:** the installed supported binary is
  `codex-cli 0.151.0-alpha.7.2`; its generated v2 schema was inspected in
  temporary storage and contains the earlier nine-member snapshot without
  `ordinaryUsageAllowed`, `accountId`, `rateLimitUpsell` or `normalModelSlug`.
- **Current evidence:** upstream `openai/codex` commit
  `a7a4321593c77933c18f84ba9bd28eba095759d8`, including the current v2 JSON and
  TypeScript schemas, backend rate-limit client/types, app-server account
  processor, and TUI rate-limit recovery tests/handling.
- **Decision:**
  - recognize and validate `ordinaryUsageAllowed` as boolean-or-null;
    explicit `true` is required before ordinary quota percentages are usable;
    `false`, null and absence are blocked/insufficient evidence and withhold
    all percentage pairs;
  - when ordinary permission is explicitly true, missing/null
    `spendControlReached` is not itself a blocker, matching current upstream
    recovery handling. Explicit `true`, any non-null validated
    `rateLimitReachedType`, an exhausted `individualLimit`, valid but
    v2-unrepresentable credits, and additional-bucket blockers retain their
    existing conservative behavior;
  - recognize `rateLimitUpsell` as opaque backend presentation data. Its
    internal structure is not parsed, serialized, logged or exposed. Its
    non-null presence is treated as a blocker because current upstream recovery
    requires no upsell; this does not turn known data into generic schema drift;
  - recognize and validate `accountId` as string-or-null solely for protocol
    compatibility, never retaining or emitting its value. Recognize and
    validate `normalModelSlug` as string-or-null, but do not add it to v2;
  - keep additive unknown structured members fail-closed while retaining the
    existing safe tolerance for unknown scalar members. No v2 schema change is
    introduced.
- **Live result:** one safe live shape read and one post-fix acceptance retry
  both reached the installed app-server but received a protocol error before a
  quota result. The normalized OpenAI state remained
  `unknown`/`telemetry_unknown` with no windows. This decision records parser
  compatibility only; it does not claim live OpenAI acceptance or M1 completion.
- **Resolution (2026-09-05):** the protocol error was root-caused to stale
  provider-managed auth and is recovered by D-018's bounded refresh; the
  live responses observed after recovery are the legacy generation without
  `ordinaryUsageAllowed` and may supply fewer windows than the current
  schema, so D-019 now governs generation compatibility and window coverage
  for live input.
- **Evidence record:** the current schema distinction and sanitized live result
  are recorded in `docs/poc-evidence.md`; synthetic current-shape coverage is
  in `tests/fixtures/openai-codex-appserver/` and
  `tests/test_openai_codex_parser.py`.

## Superseding a decision

Add a new numbered entry with its status, date, evidence and `Supersedes: D-nnn`.
Do not rewrite history or change an accepted decision silently.
