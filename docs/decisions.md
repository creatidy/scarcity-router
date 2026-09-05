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

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** v0.1 proves OpenAI/Codex, Z.ai Coding Plan and local Ollama.
  Claude is the next provider only after a security/maintenance evaluation.
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
  version are validated in-process but deliberately not surfaced: the v1
  capacity contract (U-002) has no field for them, and v1 diagnostics are a
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

- **Status:** Resolved
- **Date:** 2026-09-02
- **Decision:** M1 adapters target the v1 internal serialized contract in
  `docs/capacity-model.md`. Every snapshot carries integer `schema_version: 1`,
  stable `provider` and `source` identifiers, optional safe `plan`, required
  UTC `retrieved_at`, one of the six frozen provider statuses, an unordered
  `windows` array, optional `local_runtime` facts and allowlisted diagnostic
  codes. Account identifiers, freshness/cache fields, raw provider responses and
  provider-specific orientation fields are excluded.
- **Window semantics:** A window has independent normalized `resource`
  (`tokens`, `time` or `unknown`) and `kind` (`five_hour`, `weekly` or
  `unknown`) values, optional positive `duration_seconds`, an optional validated
  percentage pair, optional canonical `resets_at` and an optional safe opaque
  provider `window_id`. Known period kinds carry fixed durations. Unknown
  windows survive normalization and array position has no meaning. A pair is
  either `(used_percent, remaining_percent)` with an exact complement or is
  omitted; an unvalidated provider orientation never becomes a guessed value.
- **Time and local facts:** `retrieved_at` and `resets_at` use UTC RFC 3339
  strings with exactly three fractional-second digits and `Z`. Ollama uses an
  empty quota-window array plus reachability, model-presence and independently
  validated configured/effective context fields; it never uses an unlimited or
  100% quota sentinel.
- **Boundary:** This is an internal M1 adapter/core contract, not REST, MCP or
  CLI versioning. `U-003` still owns age, refresh, caching and staleness policy;
  M2 still owns scarcity and selection.
- **Reason:** Two independent subscription adapters need the same provider-free
  semantics while Z.ai and Codex wire formats evolve independently. Omitting
  account and freshness fields keeps v1 minimal and avoids prematurely
  publishing security-sensitive or policy-shaped data.
- **Evidence and validation:** The current OpenAI/Codex and Z.ai observations,
  including known five-hour/weekly windows, unknown-window behavior, reset
  conversion and Z.ai's evidence-backed used-oriented percentage, informed the
  adapter boundary only. The normalized contract contains no Z.ai raw
  `percentage`, epoch-millisecond value, `unit`/`number` pair or Codex
  `primary`/`secondary` concept. The required OpenAI, Z.ai, schema-drift,
  authentication, Ollama and missing-versus-zero scenarios are model-checked in
  the contract document.
- **Rejected alternatives:** Keeping `freshness`, stale thresholds or cache
  lifetime in the snapshot would preempt `U-003`; using one raw provider
  percentage would leak orientation; storing independently supplied used and
  remaining values would allow contradictions; treating unknown windows as
  healthy or discarding them would lose evidence; account IDs and raw metadata
  were rejected for security and minimality; encoding local availability as a
  fake quota percentage was rejected as semantically false.

### U-003 — Refresh and staleness policy

- On-demand versus cached collection, timeouts and stale-use thresholds are not
  yet chosen.
- Evidence needed: observed collector latency/reliability and real workflow use.

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

### U-005 — Ollama inspection contract

- Select the supported local calls for health, model presence and effective
  configuration; distinguish configured from effective context.
- Evidence needed: a local PoC against the actual Qwen configuration.
- **Status:** Narrowly resolved for M1 (2026-09-04, hardened same day
  after review); residuals below
- **Decision:** The local collector makes at most three read-only GETs
  against one explicitly configured local endpoint — two when the validated
  listing proves the configured model absent. The endpoint is
  canonicalized before any I/O: plain `http` on exactly the numeric
  loopback hosts `127.0.0.1` or `::1` (`localhost` and every other name
  are rejected outright, so no DNS/hosts-file/proxy escape path exists);
  the omitted port canonically defaults to the documented Ollama port
  11434, never an implicit socket default; empty query/fragment
  delimiters, whitespace/control characters and non-root paths are
  rejected; proxies are disabled for the connection and redirects are
  never followed; at most one attempt per read. The reads are
  `GET /api/version` (validated envelope with a usable, bounded,
  printable version string — control, format and padding code points
  rejected — = the reachability fact),
  `GET /api/tags` (exact `name`-identity model presence) and
  `GET /api/ps` (effective context). `model_presence` is `missing` only
  when a reachable runtime's validated listing lacks the configured name,
  and `unknown` on runtime/listing failures; `/api/ps` supplemental
  failures preserve the tags-derived presence while omitting the optional
  effective context. `configured_context_tokens` comes only from the
  explicit configuration boundary; `effective_context_tokens` comes only
  from a validated positive integer `context_length` on the configured
  model's loaded `/api/ps` entry whose validated `sha256:<64 lowercase
  hex>` digest agrees with the listing's validated digest — a missing,
  invalid or mismatched digest preserves reachability/presence but
  degrades the telemetry to `unknown` and withholds the effective context
  rather than attributing it to an unverifiable model image; the digest is
  never emitted. The effective context is never taken from the configured
  value and never from the `/api/tags` `details.context_length` model-file
  metadata. Every response body first passes strict HTTP framing validation:
  a declared `Content-Length` must be fully satisfied, conflicting
  length/transfer headers and unsupported transfer codings fail closed, and
  truncated chunked bodies are rejected. It then decodes under a strict JSON
  contract (duplicate object keys at any depth, NaN/Infinity constants and
  non-finite floats such as `1e10000`, integers outside the validated
  signed 64-bit band, recursion-limit nesting and decoder resource failures
  all normalize to
  `schema_changed`), every transport result is narrowly
  protocol-validated before use (integer HTTP status plus callable
  `read`; body chunks must be `bytes`; a malformed response object or
  contract-violating chunk degrades safely instead of raising), and one
  monotonic collection deadline is enforced end to end:
  each read runs inside a bounded non-daemon worker; the raw socket is
  captured at connection setup (family from the validated numeric
  literal; `getaddrinfo` restricted to `AI_NUMERICHOST`, so name
  resolution and DNS are impossible) and stays valid across any
  `Connection: close` ownership transfer to the response object. Cancellation
  is synchronized with handle registration, so a late handle is cancelled
  immediately. Every return or raise performs non-blocking raw-socket
  `shutdown`/`close` and a bounded worker join, raising instead of returning
  if a worker could still be blocked. The worker itself never invokes
  response/connection closes; socket and file-descriptor resources are
  released exclusively through non-blocking `shutdown`/`close` on the
  registered raw-socket handles. A deadline expiring during the
  listing or loaded-model read degrades the snapshot to `unknown` —
  never a false `ok` — while preserving the already-validated
  reachability/presence facts. Cleanup is redacted end
  to end: socket-handle lookup, close invocation and cancellation can raise
  provider-controlled text without it ever escaping.
  Response-operation failures of any kind — including
  provider-controlled exception text — normalize to safe outcomes and are
  never propagated or logged; an unexpected internal worker error is
  re-raised rather than swallowed. Error responses are not read; raw-socket
  cleanup is used instead of response/connection close. Duplicate listed names and any malformed/drifted
  body fail
  closed; a healthy local runtime reports `windows: []` with no quota
  semantics. There is no generation, no model loading for inspection, no
  pull/delete and no runtime/config mutation.
- **Evidence:** 2026-09-04 reconnaissance in `docs/poc-evidence.md`
  ("2026-09-04 M1 Ollama local runtime reconnaissance"): live envelope
  shapes for all three reads against Ollama `0.33.1` plus the installed
  binary's serialization table for the `context_length` field name.
- **Residuals:** (a) a live **populated** effective-context value has not
  been observed (nothing was loaded during reconnaissance; loading solely
  for inspection is side-effectful and was not performed) — the populated
  path is synthetic-fixture-tested only; (b) `context_length` stability
  across Ollama releases is unevidenced — the local interface must not be
  described as stable — and the parser fails closed on its absence;
  (c) the reconnaissance runtime was the installed `0.33.1`
  service, not the owner's loaded Qwen configuration, so load-state
  behavior of the real workflow remains to be observed in use.

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

### U-010 — Codex rate-limit snapshot semantics under v1

- **Status:** Resolved, 2026-09-03 (updated twice same day after schema
  review against the exact tagged schema `rust-v0.151.0-alpha.7.2`)
- **Decision:** The OpenAI adapter validates the complete evidenced
  `GetAccountRateLimitsResponse` envelope and normalizes it under the
  existing v1 contract as follows:
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
    are `schema_changed`. Valid credits or individual-limit states have no v1
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
    (v1 cannot represent capacity metered across buckets) while keeping
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
    the v1 safe-ID grammar permits as-is (underscores included): `free`,
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
- **Boundary:** this is adapter-edge semantics under the frozen v1 contract;
  it adds no v1 fields or diagnostics and does not preempt U-003
  (freshness) or M2 (scarcity/selection).

## Superseding a decision

Add a new numbered entry with its status, date, evidence and `Supersedes: D-nnn`.
Do not rewrite history or change an accepted decision silently.
