# Decision log

This file records durable cross-cutting choices and unresolved decisions. Topic
details remain in their authoritative documents; entries here explain why a
direction was chosen. Dates use UTC.

## Accepted decisions

### D-001 — Recommend, do not proxy

- **Status:** Superseded in part by D-040 (2026-09-19)
- **Date:** 2026-09-01
- **Decision:** The service returns a model recommendation, alternatives and an
  explanation. It does not receive prompts, proxy model traffic, execute work or
  automatically dispatch fallbacks.
- **Reason:** This directly solves quota allocation while sharply reducing
  security exposure and integration coupling.
- **Supersession note (2026-09-19):** The recommendation-only product remains
  the default standalone mode with unchanged semantics. D-040 adds the optional
  execution gateway, which may receive prompts and execute/proxy model traffic
  when explicitly deployed and authorized. Every D-001 obligation that remains
  true for recommendation-only mode — no prompt receipt, no model-traffic
  proxying, no autonomous fallback execution, no orchestrator replacement —
  continues to govern that mode and the recommendation surfaces.

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

- **Status:** Accepted; extended by D-044 (2026-09-19)
- **Date:** 2026-09-01
- **Decision:** Reuse existing local authentication read-only, never expose
  credentials, validate HTTPS and exact hosts before Authorization, and bind
  REST to `127.0.0.1` by default.
- **Reason:** Subscription credentials are the principal sensitive asset.
- **Extension note (2026-09-19):** All D-009 defaults remain in force for every
  recommendation-only surface and collector. D-044 is the explicit security
  decision required for the execution-gateway server component's new network
  exposure, credential storage and listener defaults.

### D-010 — Licensing

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** License the project under Apache License 2.0. Preserve notices
  for any substantially adapted third-party MIT code.
- **Reason:** Permissive commercial integration plus an explicit patent grant.
  MPL-2.0 remains an alternative only after a deliberate policy change; AGPL is
  not the default.

### D-011 — Repository hosting

- **Status:** Accepted (revised in place 2026-09-07, replacing the initial
  2026-09-01 hosting choice)
- **Date:** 2026-09-07
- **Decision:** Forgejo is canonical.

  Canonical repository:
  https://forgejo.creatidy.com/BioMedical-IT/scarcity-router

  GitHub:
  https://github.com/creatidy/scarcity-router

  GitHub is an automatic secondary mirror.

  Issues, PRs, reviews, agent workflow and branch integration are canonical
  on Forgejo.

  develop is the integration branch.

  main is outside ordinary agent PR flow.
- **Reason:** Forgejo provides the more effective day-to-day development
  workflow for the owner, while GitHub remains valuable as a public/read-only
  mirror and for integrations that only support GitHub.

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

- **Status:** Superseded by D-040 (2026-09-19)
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
- **Supersession note (2026-09-19):** D-040 is that explicit product decision:
  Ollama and local inference return as *execution resources* of the optional
  execution gateway (server-direct HTTP when network-accessible, worker-bridged
  when localhost-only). The D-017 removal rationale — local-runtime operational
  instability and system interference — remains a design input for the
  gateway's isolation, health handling and honest unknown states; it is not
  evidence against restoration, and it never re-enters the *recommendation*
  collector set on its own.

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
- **Amendment (2026-09-20, issue #101 audit):** the `auth_required` snapshot
  status and the `telemetry_auth_required` eligibility reason are deliberately
  left UNREACHABLE on this surface. Source audit of pinned codex 0.154.0
  (`codex-rs/app-server/src/error_code.rs`,
  `request_processors/account_processor.rs`): every `account/rateLimits/read`
  backend failure — expired-token 401 and outage alike — is flattened into
  the generic internal error `-32603` with the cause only in the free-text
  message; the JSON-RPC `data` member is always absent; the `account/read`
  refresh outcome is discarded by its handler and never surfaces as an
  error. A failed bounded refresh/retry therefore does not prove an
  authentication condition, and free-text error parsing is forbidden by the
  security contract. Mislabeling a backend outage as auth-required would send
  operators into pointless device-auth re-logins. The vocabulary becomes
  reachable only if a future codex generation exposes a bounded, structured,
  non-secret auth-failure signal (for example typed error data or a dedicated
  error code).
- **Boundary:** This is an explicit narrow exception to the previous purely
  read-only M1 collector wording (D-003/D-009 wording and the security doc's
  collector-mutation invariant are amended accordingly). Scarcity Router is
  not a credential manager; it triggers the official managed-auth flow of the
  provider's own installed app-server and inspects nothing.

### D-019 — Codex response-generation compatibility and window coverage

- **Status:** Accepted; amended 2026-09-05 by the post-review remediation
  below
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
    `rateLimitReachedType` blocks, and an exhausted `individualLimit`
    (`remainingPercent == 0`) blocks. A non-null `rateLimitUpsell` blocks in
    either generation.
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
- **Amendment (2026-09-05, single post-review remediation):** the original
  wording kept the U-010 coupling that turned validated-but-unrepresented
  supplemental state into withheld quota pairs. That interpretation was
  incorrect and is corrected under the explicit principle:

  > Provider telemetry that v2 does not yet expose must not invalidate
  > independently validated quota facts that v2 can represent.

  Specifically: a valid `CreditsSnapshot` is validated-but-unrepresented
  supplemental availability telemetry (Codex evaluates credit availability
  alongside, never instead of, the quota percentages) and neither blocks nor
  withholds pairs; a present non-exhausted `individualLimit`
  (`remainingPercent > 0`) is an independently represented spend-control
  state that does not invalidate ordinary quota percentages, while
  `remainingPercent == 0` remains an explicit blocker; the mere presence of
  additional `rateLimitsByLimitId` buckets — the provider's explicit
  multi-bucket view — is real capacity evidence that neither degrades the
  snapshot nor erases main pairs (their windows keep the safe
  `<limitId>:<slot>` identities, and explicit blockers inside a bucket stay
  conservative); and missing/null `spendControlReached` means that one
  optional blocker signal is unavailable — it does not erase otherwise
  validated percentage pairs in the main snapshot or in any bucket.
  `status` describes the quality of the provider observation, not a scarcity
  score; M2 will own candidate-to-bucket applicability while M1 owns honest
  observation. Validation strictness is unchanged: malformed credits,
  spend-control limits, buckets and unknown structured fields still fail
  closed, no supplemental values are exposed, and the schema stays v2.

### D-020 — M2 selector inputs and capacity applicability

- **Status:** Accepted
- **Date:** 2026-09-05
- **Decision:** M2 freezes the selector input contracts before any selector
  implementation:
  - **TaskRequirement** has exactly four parts: task level (`L0`–`L5`, not a
    capability score and never a source of capability minima), explicit
    per-dimension capability minima, typed hard constraints and single-place
    profile expansion (shape frozen in `docs/capability-model.md`).
  - **Capability ratings** use the frozen ordinal scale `1..5` with missing or
    unknown represented separately (`UNKNOWN_CAPABILITY`); `0 = unknown` is
    forbidden. Dimensions are never averaged. Whether unknown capability may
    proceed is explicit policy, never silent.
  - **Hard constraints** use the frozen nine-member vocabulary
    (`minimum_input_context_tokens`, `minimum_output_tokens`,
    `requires_tool_use`, `requires_vision`, `requires_reasoning_mode`,
    `required_provider`, `required_model`, `required_variant`,
    `privacy_constraint`). Contradictions fail validation; there is no
    local/cloud constraint after D-017 and no free-form constraint framework.
  - **Model catalog entries** carry stable provider/model/variant identity
    separate from display names, quota buckets, provider aliases and model
    class; supported hard properties; context/output allowances;
    per-dimension ratings with provenance (source, source version/date,
    assessment date, confidence, rationale); an explicit reviewable
    human-override record; and capacity bindings. The initial catalog covers
    only GPT-5.6 Luna, GPT-5.6 Sol, GLM-5.3 and GLM-5.3-Flash.
  - **Capacity applicability.** M2 requires a provider-independent
    `capacity scope` concept: `(provider, scope_id)` identifies one
    normalized capacity scope, and a catalog model binds to one or more
    scopes whose consumption all participate in capacity/scarcity
    evaluation. No optimistic single-scope selection.
  - **Schema consequence.** The v2 `window_id` is diagnostic and cannot
    legally serve as a semantic scope identifier, so the first M2
    implementation prerequisite (slice M2a) is a new normalized
    capacity-contract version adding an explicit semantic capacity-scope
    identifier (expected direction: `CapacityWindow.scope_id`, safe string or
    explicitly absent/unknown). Selector implementation must not begin before
    it exists.
  - **Scarcity aggregation invariants** are frozen in
    `docs/selection-policy.md`: only applicable scopes participate, all
    relevant windows within an applicable scope participate, the most
    restrictive result governs under `balanced`, and unknown applicability is
    explicit. The penalty formula and label thresholds remain deliberately
    unfrozen until scenario calibration (U-007).
- **Reason:** M1 proved a provider may expose multiple independent or shared
  capacity buckets (`rateLimitsByLimitId`), and consumers must not infer
  semantics from diagnostic `window_id` strings. Applicability must be
  explicit normalized data, never guessed from identifiers.
- **Boundary:** This decision freezes contract shapes and sequencing only.
  It populates no ratings, implements no schema v3, changes no production
  capacity class and adds no selector code. Exact ratings and profile minima
  remain U-006/M2c; scarcity parameters remain U-007.

### D-021 — M2 evidence, policy and replenishment precedence

- **Status:** Accepted
- **Date:** 2026-09-05
- **Decision:**
  - **Deterministic precedence.** (1) Explicit configured user policy is
    hard: a configured blackout excludes a candidate for eligibility even
    when provider quota is healthy, reports `policy_blocked` (never
    `unavailable`) and never rewrites provider telemetry. (2) Successful
    direct normalized account capacity is authoritative for current quota; a
    public status page never rewrites its percentages. (3) A provider-native
    failure/high-traffic state tied to the supported access path may degrade
    or exclude a candidate under explicit policy and never changes model
    capability. (4) Official public service health is advisory: a green page
    never fabricates quota and a red page does not automatically overwrite a
    successful direct account observation; at most it contributes advisory
    degraded confidence unless a later explicit policy says otherwise.
    (5) External performance evidence such as Artificial Analysis influences
    curated catalog assessment only — never live quota, never a blackout
    bypass, never a live routing oracle.
  - **Artificial Analysis boundary.** AA is offline/periodic catalog
    evidence: not runtime capacity, not automatic truth, never called during
    `select()`. A future cached snapshot must carry stable model ID, stable
    creator ID, source/API version, retrieval time, metric identity/version,
    attribution and the human-curated mapping into capability evidence, with
    the API key kept server-side and outside the repository and agent
    prompts. Responses are not stored merely because they are available;
    only fields used by an explicit catalog assessment are retained.
  - **Replenishment.** Reset credits are replenishment opportunities, not
    current capacity. They never enter quota percentages, never pretend quota
    is restored and are never consumed by the broker. The selector input is a
    minimal safe `ReplenishmentState` (`provider`, `kind`, `available_count`,
    `details_known`, optional safely derivable `earliest_expiry`,
    `retrieved_at`). Provider free-text titles/descriptions are not exposed,
    and opaque credit IDs are not required for selection unless a later use
    case proves they are needed.
- **Reason:** M1 established that supplemental provider state must not
  invalidate or impersonate validated quota facts (D-019); selection applies
  the same discipline across policy, telemetry and external evidence classes.
- **Boundary:** Conceptual contracts and precedence only; no AA client, no
  health collector and no replenishment code in the planning change.

### D-022 — Bounded compound recommendation contract

- **Status:** Accepted; amended 2026-09-06 by the post-review remediation
  below
- **Date:** 2026-09-05
- **Decision:** If M2 later recommends more than one model call, the
  recommendation must carry a conceptual `ExecutionBudget` whose numeric
  domain is frozen per field:
  - `max_total_model_calls`: integer `>= 1`;
  - `max_legs`: integer `>= 1`;
  - `max_review_rounds`: integer `>= 0`;
  - `max_remediation_rounds`: integer `>= 0`;
  - `max_retries_per_leg`: integer `>= 0`;
  - `max_wall_clock_minutes`: integer `>= 1`.

  Every value is finite and subject to an implementation-defined, documented
  upper bound; there is no unlimited sentinel, no infinity, no
  `null = unlimited` and no omitted field = unlimited. A compound
  recommendation without a complete valid execution budget is invalid.
  Budget maxima are permissions — hard upper bounds — not requirements:
  zero is a legal value meaning the operation is not permitted by this
  recommendation, never unknown, missing, unlimited or invalid, and a
  prohibited phase is never encoded by an artificial budget of `1`
  (`max_review_rounds = 1` permits a review round up to once; it is not a
  workaround for an unrepresentable zero). Initial structural expectations
  with illustrative budgets: `single` is one solver leg — one call, one leg,
  zero review/remediation/retry maxima, wall clock `>= 1`; `cascade` is a
  first solver plus at most one escalation leg, where the escalation leg is
  a leg, not a review round (two calls, two legs, zero review/remediation
  maxima); `critique` is one solver, one independent critic, at most one
  remediation and at most one narrow final verification when explicitly
  recommended, with positive review/remediation maxima permitted and zero
  legal wherever the plan omits a phase. “Review/fix until clean” is never
  recommended. Expected consumption is aggregated over every planned leg;
  when exact token cost is unknown it is not invented — call counts and
  applicable capacity-scope accounting remain explicit. Scarcity Router
  recommends this envelope; execution and enforcement remain the external
  orchestrator's responsibility.
- **Reason:** This mirrors repository multi-agent governance (D-015) at the
  product boundary, informed by HydraFusion's Copilot CLI Research Preview
  patterns (bounded workflows, independent critique, complete resource
  accounting, explicit escalation, no open-ended review/fix cycle) without
  runtime coupling or imitation of its product surface (see
  `docs/competitive-landscape.md`).
- **Boundary:** Contract freeze only. No archetype or budget implementation
  belongs to the planning change, and compound recommendations remain an
  optional later M2 addition, not a blocker for single-model selection.
- **Amendment (2026-09-06, single post-review remediation):** the original
  wording required every budget value to be a “finite positive bounded
  integer where applicable”, which contradicted the same contract's need to
  express zero reviews, zero remediation rounds and zero retries. Zero is a
  meaningful hard upper bound — the operation is not permitted by this
  recommendation — not unknown, missing, unlimited or invalid. The numeric
  domain is now frozen per field as above: the review, remediation and
  retry maxima have minimum `0`; the call, leg and wall-clock maxima have
  minimum `1`. No other D-022 term changed.

### D-023 — Capacity contract v3 semantic scopes

- **Status:** Accepted
- **Date:** 2026-09-06
- **Supersedes:** the "planned M2a extension" aspect of D-020 only — the
  expected minimal scope direction is now the accepted v3 contract shape.
  D-020's other selector-input freezes are unchanged, and D-020's planning-time
  history is preserved as written.
- **Decision:** The normalized capacity contract is **v3**
  (`docs/capacity-model.md`). The only change from v2 is the addition of
  `CapacityWindow.scope_id`, and this field is the final M2a shape:
  - an optional, safe, provider-local semantic scope identifier
    (`scope_id: str | None`, top-level normalized window data, never inside
    `provider_metadata`);
  - `None` — serialized as an omitted field — means scope applicability is
    unknown; no magic sentinel string exists;
  - `(snapshot.provider, window.scope_id)` identifies one semantic capacity
    scope; the `scope_id` itself is never provider-prefixed;
  - `scope_id` is opaque exact-match identity: consumers may compare it for
    equality but must never split, parse, or infer model/provider/window
    semantics from its spelling; the diagnostic `provider_metadata.window_id`
    keeps its existing shape and remains non-semantic, and consumer/core code
    never derives one identifier from the other;
  - provider mappings: OpenAI windows carry the validated `limitId` —
    `"codex"` for the main `rateLimits` snapshot and the exact mirror, the
    validated map key for each additional `rateLimitsByLimitId` bucket —
    independent of period semantics; `limitName` and `normalModelSlug` are
    validated metadata and never become scope identity. Z.ai windows of
    evidenced known limit types (`TOKENS_LIMIT`, `TIME_LIMIT`) carry the
    adapter-owned `coding_plan` scope, including a known type whose
    `(unit, number)` period is unrecognized; a structurally valid but
    unevidenced provider type keeps `scope_id = None` because applicability
    for that type is not evidenced, and raw type text never becomes a scope;
  - no model-to-scope bindings, no catalog production types, no ratings and
    no selector exist in M2a; binding catalog models to scope identities is
    the M2b slice;
  - version strictness: the production capacity model accepts and constructs
    only `schema_version = 3`; serialized v2 snapshots fail closed exactly
    like any other wrong version, with no compatibility shim and no silent
    upgrade.
- **Reason:** D-020 requires explicit normalized scope applicability before
  any selector implementation and forbids deriving it from the diagnostic
  `window_id`. M1 live evidence (multi-bucket `rateLimitsByLimitId`) shows
  multiple windows — including equal periods — must coexist under distinct
  scopes, and Z.ai's unknown future limit types must not be guessed into an
  evidenced scope.
- **Boundary:** Contract change plus provider scope emission only. Every v2
  invariant is preserved (percentage pairs, canonical UTC timestamps, fixed
  durations, status/diagnostic allowlists, strict provider metadata,
  fail-closed serialized shapes, deterministic serialization). Live
  structural acceptance (2026-09-06) observed both providers at
  `schema_version = 3` / `ok` with per-window semantic scopes and distinct
  additional scopes; only structural facts (window counts, distinct scope
  counts, presence of the public `codex` main scope) were recorded — never
  personal quota values or non-public bucket identifiers. No model prompt was
  issued.

### D-024 — M2b task and model catalog core contracts

- **Status:** Accepted
- **Date:** 2026-09-06
- **Decision:** M2b adds the provider-independent core types for the future
  selector in `scarcity_router/selection_types.py` — frozen dataclasses with
  construction-time validation, `from_dict()`/`to_dict()`, exact serialized
  shapes, deterministic output, round-trip invariants and a dedicated error
  family (`SelectionContractValidationError`, deliberately separate from
  `CapacityValidationError`). The module is pure: standard library only, no
  filesystem, network or environment access, no capacity/provider/status
  changes and no `model-policy.json` loading at runtime.
  - **TaskRequirement stored shape.** A resolved `TaskRequirement` stores
    exactly three parts: `task_level` (validated `L0`–`L5` vocabulary, never a
    capability score and never a source of minima), `capability_minima` and
    `hard_constraints`.
  - **Profile expansion is a construction pathway, not a fourth serialized
    field.** M2c will define the calibrated profile definitions and the single
    authoritative expansion mechanism. M2b deliberately implements no
    profile resolver, because no numeric profile minima are accepted yet
    (`model-policy.json` records `numeric_minima_status =
    deferred_to_m2_calibration` and is unchanged by M2b).
  - **Typed identity.** `ModelRef` replaces the stringly qualified
    required-model form; `ModelIdentity` (`provider`, `model`, `variant`) is
    the stable catalog identity, separate from display names, quota buckets,
    provider aliases and capacity scopes; `CapacityScopeRef` is the exact
    `(provider, scope_id)` identity of the v3 capacity contract (D-023) and
    carries no window kind, no window id and no model data. Identifiers use
    the capacity safe-ID grammar; supported model providers are exactly
    `openai` and `zai` — adding one is an explicit contract/catalog change.
  - **Capability minima.** `CapabilityMinima` is the fixed six-dimension
    object: `None` = no minimum for this task, `1..5` = required minimum.
    Zero, out-of-range values, bools, non-integers and unknown dimensions are
    invalid. Minima are never averaged; no sufficiency scoring exists yet.
  - **Hard constraints.** The frozen nine-member vocabulary with strict typed
    validation (positive token minima, strict booleans, safe
    variant/privacy identifiers) and a validated
    `required_provider` ↔ `required_model.provider` contradiction rule:
    contradictions are validation errors, never heuristically resolved.
    `False` means "not required", never "must not support";
    `privacy_constraint` is a safe opaque policy identifier with no values or
    matching logic yet. Serialization is compact (`None` and false
    `requires_*` are omitted).
  - **Capability provenance.** A known rating (`1..5`) requires complete
    provenance: at least one `EvidenceRef`, a coarse `low|medium|high`
    confidence (no float scores), an assessment date (`YYYY-MM-DD`) and a
    non-empty rationale; a naked rating is invalid. **Unknown catalog
    capability is `rating: null`** — serialized explicitly, never `0`, never
    an omitted dimension — and an unknown assessment serializes exactly
    `{"rating": null}`; partial provenance on an unknown rating is not
    representable. All six dimensions are required in every serialized
    capability vector, so "well-formed unknown" is distinguishable from a
    missing schema dimension; extra dimensions are rejected.
  - **Human overrides.** `HumanOverride` (rating, decided_on, rationale; no
    account identity) **preserves the source rating and evidence**: the
    curated assessment is never mutated or replaced, both source and override
    remain serialized/reviewable, and `effective_rating` is only a derived
    view. An override requires an existing known base rating; original human
    judgment belongs in ordinary evidence (M2c), not the override field.
  - **Capacity bindings.** `ModelCatalogEntry.capacity_bindings`:
    `None` = model-to-scope applicability is **unknown** and must never be
    read as "consumes no subscription quota"; a non-empty tuple of
    `CapacityScopeRef` = known applicability. **The empty binding set is
    invalid** and must never serve as an optimistic "unmetered" state. A
    known binding is the exact `(provider, scope_id)` pair; bindings must be
    unique, serialize deterministically and use the model's own provider —
    **cross-provider bindings are unsupported in the initial contract**
    (no evidenced use case; requires an explicit future decision). M2b
    defines the binding contract only; no real model is bound to a scope yet.
  - **Catalog container.** `ModelCatalog` (`catalog_version >= 1`, UTC
    `updated_on` date, entries) enforces unique model identities, admits an
    empty catalog (the contract precedes the population) and serializes
    entries sorted by `(provider, model, variant)`, independent of insertion
    order. No production `model-catalog.json` artifact is created by M2b.
- **Reason:** D-020 froze the selector input contracts conceptually; M2b
  turns them into validated values so later slices can only construct
  well-formed requirements, assessments and catalog entries. Provenance and
  explicit unknown states are enforced at construction because a numeric
  rating must never exist without reviewable evidence, and unknown capability
  or unknown applicability must never silently degrade into zero, false or
  "unmetered".
- **Boundary:** Types, validation and deterministic serialization only.
  M2b adds no actual catalog entries, no capability numeric ratings, no
  profile numeric minima, no profile resolver, no sufficiency function, no
  candidate filtering, no scarcity, no reservations, no selection and no
  provider/runtime changes. Populating reviewed ratings, minima and capacity
  bindings for Luna, Sol, GLM-5.3 and GLM-5.3-Flash is the M2c slice (U-006).

### D-025 — Initial capability and task-profile calibration

- **Status:** Accepted
- **Date:** 2026-09-06
- **Resolves:** U-006. U-007 (scarcity parameters) remains open for M2d.
- **Decision:** M2c populates the first accepted content for the frozen
  M2b contracts (D-024), curated by the owner's accepted workflow-role
  calibration and verified against one bounded external evidence pass
  (2026-09-06):
  - **Four-model scope.** The catalog artifact `model-catalog.json`
    (`catalog_version = 1`, `updated_on = 2026-09-06`) contains exactly four
    routing identities: `openai/gpt-5.6-luna/max`, `openai/gpt-5.6-sol/high`,
    `zai/glm-5.3/max` and `zai/glm-5.3-flash/max`. Reasoning effort is part
    of the variant identity, never merged into the model name, and model
    classes are never identities. Catalog expansion is a future explicit
    decision.
  - **Capability calibration.** The accepted rating matrix (frozen 1..5
    scale, routing suitability in the owner's workflow) is: Luna
    4/4/3/5/5/4; Sol 5/5/5/5/5/5; GLM-5.3 5/5/4/4/5/4; GLM-5.3-Flash
    4/4/3/4/5/4 (dimension order: reasoning, coding,
    scientific_methodological, writing_editorial, tool_use,
    translation_multilingual). Sol's broad 5s are intentional — scarcity
    policy owns preservation of premium capacity, not artificially lowered
    capability. Every known rating carries at least one `EvidenceRef`, a
    coarse confidence, `assessed_on = 2026-09-06` and a rationale; ratings
    are not averaged, not percentile scores and never derived mechanically
    from one benchmark.
  - **No HumanOverride for initial curation.** These are the original
    curated ratings; `human_override` is absent everywhere. The owner's
    role judgment is recorded as ordinary owner-observation evidence
    (`accepted_workflow_role_calibration_2026-09-06`, containing no
    personal quota values or account identity).
  - **Evidence hierarchy.** (1) first-party OpenAI model documentation;
    (2) first-party Z.ai/ZCode model documentation; (3) Artificial Analysis
    as independent comparative evidence only; (4) owner observation for
    workflow-specific judgment. AA evidence is never mapped mechanically
    onto the internal scale and is never a live routing oracle (D-021).
    EvidenceRef identifiers record canonical source URLs; the evidence pass
    was a single bounded pass over first-party OpenAI, first-party Z.ai/ZCode
    and Artificial Analysis documentation pages only, with no benchmark
    archaeology and no recursive research.
  - **Hard properties.** Luna/Sol: input context 1,050,000, output 128,000,
    tools/vision/reasoning mode all known true. GLM-5.3: input 1,000,000,
    output 128,000, tools true, reasoning mode true and vision **known
    false** — an evidenced negative fact from the first-party GLM-5.3 model
    guide (`https://docs.z.ai/guides/llm/glm-5.3`), which states GLM-5.3
    currently supports **text-only inputs** for GLM Coding Plan users; it is
    not inferred from Flash being multimodal, and a known negative serializes
    explicitly (`"supports_vision": false`), never as an omitted unknown.
    GLM-5.3-Flash: input 1,000,000, output 128,000, tools/vision/reasoning
    mode known true — always-on thinking with recommended
    `reasoning_effort: max` is first-party documented
    (`https://docs.z.ai/guides/vlm/glm-5.3-flash`). Both GLM output
    allowances are explicit first-party model/Coding Plan documentation
    (128K maximum output), not copied from a local ZCode UI setting and not
    carried over from an older GLM generation. No `model_version`
    string is invented; `model_version_date` records evidenced dates
    (GPT-5.6 family GA 2026-07-09 after the 2026-06-26 Sol preview; GLM-5.3
    announcement 2026-08-14, with Z.ai's release-notes entry labeled
    2026-08-18; GLM-5.3-Flash 2026-08-26).
  - **Capacity bindings.** Each OpenAI entry binds to exactly
    `openai/codex`; each Z.ai entry binds to exactly `zai/coding_plan`
    (M2a v3 scope identities). No entry keeps unknown applicability
    (`capacity_bindings = None`) and no entry has an empty binding set.
    Absence of an additional OpenAI binding is deliberate: the additional
    live bucket observed in M1 may be a provider-specific reserve/alternate
    route, and no evidence shows Luna or Sol necessarily consumes it. No
    private/non-public live scope identifier is recorded, and no binding is
    inferred from `window_id`, `limitName` or `normalModelSlug`.
  - **Calibrated task profiles.** All eight formal profile IDs keep their
    descriptive entries and gain exactly one selector-facing numeric
    definition, `calibrated_requirement`, in `model-policy.json`
    (`schema_version` stays 1; `policy_version` 3 → 4;
    `numeric_minima_included = true`;
    `numeric_minima_status = calibrated_m2c`): mechanical L0
    (tool_use ≥ 2, writing_editorial ≥ 2); routine_coding L1
    (reasoning ≥ 2, coding ≥ 3, tool_use ≥ 3, requires_tool_use);
    deep_coding L3 (reasoning ≥ 4, coding ≥ 5, tool_use ≥ 4,
    requires_tool_use + requires_reasoning_mode); scientific_review L4
    (reasoning ≥ 4, scientific_methodological ≥ 5, writing_editorial ≥ 4,
    requires_reasoning_mode); editorial L2 (reasoning ≥ 3,
    writing_editorial ≥ 5); general_reasoning L2 (reasoning ≥ 4,
    writing_editorial ≥ 3); orchestration L3 (reasoning ≥ 4,
    writing_editorial ≥ 5, tool_use ≥ 5, requires_tool_use +
    requires_reasoning_mode); translation L4 (writing_editorial ≥ 4,
    translation_multilingual ≥ 5) — deliberately L4 because the initial
    profile means publication-quality translation, not casual translation.
  - **Profile expansion mechanism.** `TaskProfileDefinition` (safe profile
    ID + typed stored requirement; no model/provider/class affinity fields)
    and `TaskProfileCatalog` (unique IDs, deterministic serialization,
    exact lookup) live in `scarcity_router/selection_types.py` next to the
    M2b types. `resolve(profile_id)` is the only production profile
    resolver: a pure expansion returning the calibrated stored
    `TaskRequirement` — no capability inference, no model lookup, no
    scarcity, no filesystem access (the caller provides typed data) and no
    merge with explicit task inputs, which belongs to later selector input
    assembly.
  - **Capability-only scenario expectations.** Tests prove the calibration
    expresses the intended routing distinctions: mechanical,
    routine_coding and general_reasoning admit all four models;
    deep_coding admits Sol and GLM-5.3 (GLM-5.3 substitutes for Sol on
    deep technical work); scientific_review and translation admit only
    Sol; editorial and orchestration admit Luna and Sol (Luna is a viable
    orchestration/editorial model); Flash stays a real professional-
    capability model rather than an L0 toy. These are capability-only
    expectations, not selector decisions; no scarcity or select logic
    exists in M2c.
- **Reason:** M2b built validated contracts but deliberately populated no
  values; routing quality depends on curated, provenance-bearing content
  that a human can review and change deliberately. The calibration encodes
  the owner's accepted role structure — Sol as protected specialist, GLM-5.3
  as technical peer to Sol, Luna as orchestration/editorial workhorse,
  Flash as capable execution generalist — while keeping capability strictly
  separate from scarcity.
- **Boundary:** Catalog/policy content, the two profile core types and
  calibration tests only. No scarcity penalty, labels, reservations,
  capacity aggregation, provider health, replenishment, AA client, selector,
  ranking, simulation, REST/MCP, new providers or additional models. No
  changes to `capacity.py`, provider adapters or `status.py`.

### D-026 — Scarcity and resource-policy primitives

- **Status:** Accepted
- **Date:** 2026-09-06
- **Resolves:** U-007. Finalizes the M2 parameters of D-005; D-005's history
  is preserved as written.
- **Decision:** M2d implements the frozen scarcity and resource-policy
  primitives in `scarcity_router/scarcity.py` and `scarcity_router/policy.py`
  — pure, standard-library-only modules with no filesystem, network,
  environment, subprocess, clock or provider access; callers supply every
  observation and every timezone-aware evaluation instant explicitly. M2d
  does not choose a model, rank candidates or implement `select`.
  - **Continuous penalty accepted.** The D-005 proposal `(1-r)^2` is accepted
    exactly, in integer form: `penalty_units = (100 - remaining_percent)^2`
    on scale `SCARCITY_PENALTY_SCALE = 10000` (normalized penalty =
    `penalty_units / 10000`). Ranking uses the integer units, never floats.
    The penalty contains no linear or logarithmic term, no reset proximity,
    no provider price, no capability score, no model prestige and no
    provider preference; it answers only "how constrained is this applicable
    current subscription capacity".
  - **Explanatory labels frozen** (explanation only, never ranking input):
    `remaining >= 80 → plentiful`, `>= 50 → normal`, `>= 20 → scarce`,
    `>= 1 → critical`, `== 0 → unavailable`; `unknown` is never produced
    from a number and represents insufficient trustworthy capacity
    information. Two candidates labelled `scarce` may still have different
    penalties.
  - **Applicability.** Only explicit catalog `capacity_bindings` participate;
    a binding is the exact `CapacityScopeRef` `(provider, scope_id)`.
    Applicability is never inferred from `window_id`, `limitName`,
    `normalModelSlug`, model names or provider aliases.
    `capacity_bindings = None` is unknown applicability and yields an
    unknown assessment — never a guessed scope. At most one
    `CapacitySnapshot` per provider is accepted per assessment; duplicates
    fail typed validation.
  - **Aggregation.** Every window of every bound scope carrying a usable
    percentage pair participates, including provider-normalized `time`
    windows and unknown-kind windows — the resource is never reinterpreted.
    The most restrictive governs: `aggregate_penalty_units = max(window
    penalties)`, equivalently `effective_remaining_percent = min(remaining
    values)`; the label comes from the effective remaining, never an
    average. Unrelated scopes never participate even at 0%. Governing-window
    evidence is explanation-only (scope, resource, kind, remaining, optional
    diagnostic `window_id`; never parsed) and ties are broken by a stable
    canonical key over normalized fields (`provider, scope_id, resource,
    kind, window_id-or-empty`), independent of input order.
  - **Explicit exhaustion versus telemetry unknown.** Any known applicable
    window at `remaining_percent == 0` makes current capacity explicitly
    exhausted: `state = unavailable`, penalty `10000`, effective remaining
    `0` — even when another bound scope is unknown. Otherwise the
    assessment is `unknown` (no numeric penalty, no effective remaining, no
    governing evidence — unknown is incomparable to numeric scarcity, with
    no sentinel such as 0, 10000 or -1) whenever any bound scope cannot be
    completely assessed: missing provider snapshot, unknown binding, a
    snapshot whose status is not `ok`, no matching window for a bound scope,
    or an applicable window without a percentage pair. A non-`ok` snapshot
    status (`unavailable`, `auth_required`, `unsupported`, `schema_changed`,
    `unknown`) means telemetry is not trustworthy; it never means quota
    remaining is 0 and never produces `unavailable` scarcity. No
    staleness-age threshold exists in M2d: acquisition is synchronous and
    fresh-on-demand, and any future caching requires an explicit later
    decision.
  - **Unknown-capacity policy.** Exactly two modes: `degraded` (an
    unknown-capacity candidate may remain conditionally usable with
    `degraded = true`, no numeric penalty; a known sufficient candidate must
    rank ahead) and `strict` (unknown capacity is blocked). Known
    `unavailable` is blocked in both modes; known nonzero capacity is
    eligible in both, subject to other policy.
  - **Reservations target capacity scopes.** A `ReservationRule` targets a
    `CapacityScopeRef`, never a model name: Luna and Sol share
    `openai/codex` and GLM-5.3 and GLM-5.3-Flash share `zai/coding_plan`,
    so a model-specific reservation would incorrectly imply independent
    quota. `resource` must be a known normalized resource (`tokens`/`time`),
    `kind` a known window kind (`five_hour`/`weekly`), the threshold an
    integer 1..100 and `minimum_task_level` one of `L0`–`L5`. The trigger
    boundary is strict: trigger iff `remaining_percent < when_remaining_below`
    (remaining 20 at threshold 20 does not trigger; 19 does). When
    triggered, use is blocked below the minimum task level and permitted at
    or above it. A reservation never creates capacity and cannot override
    explicit exhaustion. If the target window cannot be identified or lacks
    usable percentage evidence, the reservation evaluation is explicitly
    unknown, never silently not-triggered.
  - **Blackouts.** `WeeklyBlackoutRule`s are user policy with a required
    supported-provider target (optional exact model; optional exact variant
    requiring its model; exact-identity matching, no wildcards), an IANA
    time zone validated through the standard-library `zoneinfo`, a
    duplicate-free weekday list (`mon`–`sun`, canonically ordered), strict
    24-hour `HH:MM` local times and half-open `[start, end)` semantics:
    exactly at start is blocked, exactly at end is not; `start == end` is
    invalid, never a 24-hour blackout; cross-midnight intervals block from
    start on each configured weekday through end on the following day.
    Evaluation converts a caller-supplied timezone-aware instant into the
    configured zone. A matching blackout is a hard `policy_blocked`
    exclusion that never rewrites capacity status, percentages, scarcity or
    capability. No vendor peak/off-peak schedule is hard-coded.
  - **Replenishment.** The D-021 `ReplenishmentState` is implemented
    (`provider`, safe normalized `kind`, `available_count >= 0`, strict
    `details_known`, canonical UTC `earliest_expiry` only when present,
    canonical UTC `retrieved_at`; a zero count forbids expiry; expiry
    requires known details and a positive count; no provider free-text,
    credit IDs, titles, descriptions or account identity) with visibility
    modes `ignore` (no policy effect), `advisory` (expose availability
    without recovery) and `recoverable` (`available_count > 0` exposes
    `recoverable = true` and `human_action_required = true`). Replenishment
    is never current capacity: it never changes a `ScarcityAssessment`,
    never restores eligibility and is never consumed or redeemed by the
    broker — no reset-consume call, redemption endpoint or provider
    credential mutation exists.
  - **UserPolicy container.** `policy_version >= 1`, the unknown-capacity
    and replenishment modes, reservations and blackouts, with rule IDs
    unique across the whole policy and deterministic canonical
    serialization (rule tuples sorted by `rule_id`). No ranking modes
    (`quality-first`, `conserve-openai`, `conserve-zai`), no candidate
    ordering and no `select` exist in M2d.
- **Reason:** U-007 required scenario-validated scarcity parameters before
  M2 acceptance. The accepted integer quadratic penalty, frozen label
  boundaries, most-restrictive aggregation and explicit unknown behavior
  make future ranking deterministic and prevent optimistic window
  selection, while scope-targeted reservations correctly express the
  M2c-evidenced shared scopes (`openai/codex` for Luna/Sol, `zai/coding_plan`
  for GLM-5.3/GLM-5.3-Flash).
- **Boundary:** Pure primitives, contracts and tests only. No selector, no
  ranking, no capability filtering, no provider/capacity/status changes, no
  catalog or policy artifact changes, no reset consumption, no provider
  acquisition changes and no REST/MCP. The next implementation slice is M2e
  (deterministic selector, explanation and simulation).
- **Amendment (2026-09-06, single post-review remediation):** the original
  implementation deviated from D-026's intended semantics in three narrow
  ways; this remediation conforms the code to the decision above without
  changing any of its fundamental choices. (1) A scarcity result's
  `applicable_scopes` always names the candidate's applicable capacity
  scopes — empty only when `capacity_bindings is None` — so known bindings
  are preserved on unknown telemetry (missing snapshot, non-`ok` status,
  missing scope window, percentage-unknown window): the failure is in the
  telemetry, never in the applicability. (2) Numeric
  `ScarcityAssessment` states enforce their own contract at construction
  (typed governing-window evidence before attribute access, non-empty
  applicable scopes, the governing scope belonging to the applicable
  scopes, and a fully `known` state carrying no reason codes while
  `unavailable` may combine `capacity_exhausted` with incompleteness codes,
  preserving exhaustion-dominates-unknown). (3)
  `WeeklyBlackoutRule.blocks_at` validates its own instant — a naive
  datetime is a contract error on every public path, never a host-local
  interpretation. (4) Replenishment visibility is not availability: a
  visible `ReplenishmentDecision` with `available_count == 0` carries no
  reason codes in any mode; only a positive count earns
  `replenishment_available` (and, under `recoverable`, the recoverable
  codes). No new decision number is created; D-026 remains the governing
  M2d decision.

### D-027 — Deterministic balanced selector and simulation

- **Status:** Accepted
- **Date:** 2026-09-06
- **Decision:** M2e implements the first useful recommendation path in the
  pure modules `scarcity_router/selector.py` and
  `scarcity_router/simulation.py`, composed by the application layer
  `scarcity_router/selection_app.py` and the top-level CLI dispatcher
  `scarcity_router/cli.py` (`select`, `simulate`; the existing `status`
  behavior and the direct `status.main` entry point are preserved).
  - **Authoritative selector pipeline.** Per candidate, in order: blackout
    evaluation (single caller-supplied timezone-aware instant) → hard
    constraints → capability sufficiency → scarcity assessment and the M2d
    unknown-capacity policy (contracts unchanged) → replenishment
    visibility → applicable reservations → eligible for ranking. Later
    stages are not run after a hard/capability failure merely to populate
    fields. Candidate evaluation always proceeds in canonical
    `(provider, model, variant)` order, so catalog, snapshot and input
    ordering never affect the result.
  - **Hard-property unknown fails hard requirements.** Tri-state semantics
    are preserved: a `None` candidate property fails a required feature as
    `unknown` and is never read as `False`; an explicit `False` fails as
    `unsupported`; a `False` requirement is a no-op, never "must not
    support". Numeric allowances fail as `unknown` or `insufficient`.
    Identity constraints (`required_provider`, `required_model`,
    `required_variant`) match exactly on typed identity, never display
    names. `privacy_constraint` is an exact required privacy tag: unknown
    tags fail as `privacy_unknown`, a known tag set without the required
    tag fails as `privacy_unsatisfied`; no privacy hierarchy and no
    cloud/local behavior is invented. All four current catalog entries have
    unfrozen privacy characteristics, so a current privacy constraint
    honestly produces no eligible model.
  - **Requirement tightening.** `tighten_requirement` merges an explicit
    `TaskRequirement` into a calibrated profile requirement monotonically:
    a lower explicit task level, capability minimum or numeric hard minimum
    is a validation error, never a silent `max()`; booleans combine with OR
    (`False` cannot loosen `True`); identity/privacy constraints must agree
    exactly or be newly supplied; final construction still validates
    provider/model contradictions. `--tighten` is only permitted with the
    profile path, never with an explicit requirement.
  - **Capability sufficiency and margin.** Sufficiency and margin use
    `CapabilityAssessment.effective_rating`, so a valid `HumanOverride`
    participates without mutating catalog values (original rating and
    evidence stay serialized). Unknown capability on a required dimension
    fails — the strict initial policy; no unknown-capability policy mode
    exists. Dimensions are never averaged and never compensate each other.
    The frozen `balanced` capability margin is
    `SUM(effective_rating - required_minimum)` over required dimensions
    only; unrequired dimensions do not participate; all contributions are
    nonnegative because the candidate already passed sufficiency; a
    requirement with no minima yields margin 0. No weights, no task-level
    bonus, no confidence multiplier, no benchmark score, no hard-property
    margin.
  - **Exact `balanced` ranking order.** (1) known nonzero capacity before
    unknown/degraded capacity — unknown has no numeric sentinel and is
    incomparable to a known percentage, even a critical one; (2) integer
    scarcity penalty among known-capacity candidates only; (3) capability
    margin, lower wins; (4) explicit `SelectorPolicy.preference_order`
    (listed before unlisted, then index) — a late tie-break that cannot
    override capability, hard constraints, blackout, capacity knowledge
    class, scarcity or reservations and is never inferred from classes,
    profiles, providers, catalog order or display names; (5) stable
    `(provider, model, variant)` identity.
  - **`SelectorPolicy`.** A selector-level wrapper distinct from the frozen
    M2d `UserPolicy`: exactly the `balanced` mode in this slice (no
    quality-first, no conserve-openai, no conserve-zai, no hidden provider
    penalties, no invented quality score), the `UserPolicy` resource
    policy, and the explicit preference order. The documented neutral
    default application policy is `balanced` / `degraded` /
    `advisory` with no reservations, no blackouts and no preferences — it
    is not a checked-in personal quota policy.
  - **Reservations and blackouts.** Reservations are scope-based: only
    rules whose `CapacityScopeRef` is one of the candidate's known
    capacity bindings apply; unknown bindings are never guessed into a
    reservation match (the candidate is governed by the unknown-capacity
    policy). An applicable reservation whose trigger state cannot be
    determined fails closed (`reservation_unknown`), never silently
    untriggered. A matching blackout is a hard `policy_blocked` exclusion
    that never rewrites telemetry.
  - **Replenishment.** Replenishment never changes current eligibility: a
    candidate explicitly exhausted (`unavailable`) stays excluded; it is
    surfaced in `recoverable_candidates` when the policy's visibility mode
    reports `recoverable` with `human_action_required`. Reset credits are
    visible only when a normalized `ReplenishmentState` is supplied as
    selector input; **no live reset-credit acquisition is implemented in
    M2e** — the OpenAI provider parser and acquisition files are untouched,
    and first-party `rateLimitResetCredits` wiring remains a separate M2
    closeout question after selector acceptance.
  - **Structured outputs.** `CandidateEvaluation` (identity, eligibility,
    degraded flag, one primary exclusion stage from the closed vocabulary
    `policy_blackout`/`hard_constraint`/`capability`/`capacity`/
    `reservation`, structured hard/capability failures, margin, blackout,
    scarcity, unknown-capacity, reservation and replenishment records,
    normalized reason codes) and `SelectionDecision` (evaluated instant,
    resolved requirement, catalog version/date, profile and policy
    versions, selected candidate, ordered alternatives, excluded candidates,
    closest candidates, recoverable candidates, degraded flag, reason
    codes). No raw provider payloads, credentials, account identifiers or
    benchmark dumps are representable. A valid no-solution result carries
    `no_eligible_candidate`, no alternatives, and closest candidates chosen
    by stage progress only (a later stage is closer), ordered by stable
    identity and capped at 3 — no second quality score. Requirements are
    never relaxed and no fallback bypasses capability. The reason
    vocabulary (`selected_balanced`, `no_eligible_candidate`,
    `selected_degraded_capacity`, `policy_blocked`, `hard_constraint_failed`,
    `capability_failed`, `capacity_unavailable`, `capacity_unknown_blocked`,
    `reservation_blocked`, `reservation_unknown`,
    `replenishment_recoverable`) never encodes dimensions or model names;
    details live in structured failure records.
  - **Simulation.** `simulate_selection` runs the baseline and the
    simulated decision through the SAME `select_model` core — no copied
    pipeline and no simulation-specific selector. `CapacityPercentageOverride`
    targets exactly one existing window of an `ok` snapshot that already
    carries a known percentage pair (zero or multiple matches fail typed
    validation; an ambiguous match requires an exact, never-parsed
    `window_id`), changes only the percentage pair, and never creates
    windows or snapshots or fabricates known telemetry from unknown.
    `SimulationOverrides` may also replace the selector policy, replace the
    replenishment observations (`None` retains, `[]` simulates none,
    non-empty replaces) and move the simulated evaluated instant
    (timezone-aware). Baseline inputs are never mutated.
  - **CLI.** `select` and `simulate` load the repository artifacts
    (`model-catalog.json`, `model-policy.json` — only
    `task_profiles[].id` and `.calibrated_requirement` feed selection, with
    `policy_version` retained for provenance) and user-supplied JSON files
    through strict parsing (duplicate keys, NaN/Infinity and malformed JSON
    fail safely). One invocation obtains ONE timezone-aware instant and
    passes it both to blackout evaluation and, through a fixed clock, to
    the existing `collect_status` so both provider snapshots share it. No
    model prompt, completion request or inference call is issued; capacity
    collection reuses the telemetry path and may exercise the bounded
    provider-managed auth recovery already accepted in D-018. A valid
    no-solution result exits 0; non-zero exit is reserved for invalid
    input/config/application failure — shell exit status is never a second
    selection contract. Default artifact paths are the repository-root
    files of the current source tree; U-008 remains unresolved and this is
    not the final installed-package resource layout.
- **Reason:** M2a–M2d provided validated inputs and primitives; M2e combines
  them into the deterministic least-scarce-sufficient recommendation the
  product exists for, with every frozen rule testable and every unknown
  state honest. The exact ranking order and margin formula make the
  "choose the least scarce model that is capable enough" rule auditable.
- **Boundary:** Deterministic selection, explanation and simulation only.
  No new models or ratings, no new task profiles, no ranking modes beyond
  `balanced`, no reset-credit acquisition or redemption, no runtime failure
  feedback, no quality-first/conserve modes, no REST/MCP/dashboard, no
  history or audit stores, no prompt proxying and no model execution. M2 is
  **not** PASS: M2 exit requires separate post-merge live acceptance in the
  owner's real workflow.
- **Amendment (2026-09-06, single post-review remediation):** the selection
  algorithm is unchanged; three explanation/provenance gaps are closed.
  (1) Per-candidate replenishment output preserves the complete normalized
  `ReplenishmentState` provenance (`provider`, `kind`, `available_count`,
  `details_known`, `earliest_expiry`, `retrieved_at`) next to its
  `ReplenishmentDecision` through the `ReplenishmentEvaluation` wrapper,
  and selector and simulation replenishment sets are canonicalized by
  `(provider, kind)` — output/provenance determinism only; availability,
  expiry and retrieval time never gain ranking semantics, and eligibility
  semantics are unchanged. (2) `SelectionDecision` preserves the exact
  `SelectorPolicy.preference_order` as decision provenance: ordered (never
  sorted), unique identities, serialized as an explicit list (empty list
  when absent). (3) Human `--explain` surfaces governing capacity evidence
  (provider/scope, resource, kind, remaining, diagnostic window id when
  present — never parsed) for the selected, alternative and
  capacity-excluded candidates where it exists, full reservation decisions
  (including triggered-but-permitted ones) for eligible candidates while
  continuing to render excluded candidates' decisions, and the applied
  preference order. No new decision number is created; D-027 remains the
  governing M2e decision.

### D-028 — M3 machine-interface contract (REST and MCP)

- **Status:** Accepted; non-goals amended by D-045 (2026-09-19)
- **Date:** 2026-09-06
- **Decision:** M3a freezes the machine-interface semantics for REST and MCP
  in [`docs/machine-interfaces.md`](machine-interfaces.md) — the
  authoritative M3 interface contract — before either transport is
  implemented. The contract record:
  - **Thin adapters over one core.** CLI, REST and MCP are transport
    adapters over the SAME application/core (D-007 unchanged). They parse
    transport input, call the application/core and serialize existing typed
    results; they never own selection, scarcity, provider parsing,
    simulation or policy semantics. No REST selector, MCP selector,
    REST-only simulation or MCP-only fallback logic may be created.
  - **REST surface.** Exactly `GET /healthz`, `GET /v1/status`,
    `POST /v1/select`, `POST /v1/simulate`. `/v1/status` returns the
    existing CapacitySnapshot v3 documents in the frozen envelope
    `{"schema_version": 1, "snapshots": [...]}`; `/v1/select` and
    `/v1/simulate` use typed JSON mirroring the application inputs
    (profile XOR explicit requirement, tightening only with the profile)
    and return the existing `SelectionDecision`/`SimulationResult`
    serializations inside the same envelope. Reason codes are never
    translated, provenance is never removed and results are never reduced
    to a model identifier. The earlier roadmap ideas `/v1/providers` and
    `/v1/providers/{provider}` are deferred: `/v1/status` already returns
    the full two-provider snapshot set and per-provider filtering is a
    trivial client-side operation.
  - **Frozen request parsing and field semantics.** REST request bodies are
    parsed with the application's deterministic strictness: duplicate JSON
    object keys and `NaN`/`Infinity`/`-Infinity` constants are
    `invalid_request` (HTTP 400), never framework-default lenient parsing.
    Missing and explicit-`null` field semantics are frozen for
    `/v1/select` and `/v1/simulate`: `profile_id`, `requirement` and
    `tightening` — missing or `null` = absent; `selector_policy` — missing
    or `null` = the neutral policy; `replenishment_states` — missing or
    `[]` = no observations and explicit `null` = `invalid_request` (the
    field is an array at this boundary; the baseline-replacement tri-state
    exists only in the nested simulation `overrides`, which keeps the
    existing `SimulationOverrides` semantics: missing/`null` = retain
    baseline, `[]` = none, non-empty = full replacement). Exactly one
    effective requirement source is required — effective `profile_id` XOR
    effective `requirement`; both or neither is `invalid_request`. An
    unknown `profile_id` is `invalid_request` (HTTP 400), not an internal
    error and not a no-solution. MCP freezes the **logical** structured
    error payloads (`invalid_request`, `internal_error`) that M3c must
    preserve through the eventual SDK's tool-error mechanism; a valid
    no-solution remains a successful tool result and never uses the error
    shape; no additional codes are invented.
  - **MCP surface.** Exactly three tools — `scarcity_status`,
    `scarcity_select`, `scarcity_simulate` — over the stdio transport,
    structurally mirroring the REST request bodies and returning the same
    machine-structured payloads. No per-provider tools, no internal helper
    tools, no reset redemption, no MCP-specific shorthand.
  - **MCP calls the application directly.** The MCP adapter invokes the
    application layer in-process; it does not require or invoke the local
    REST server (`MCP → REST → application` is rejected: fewer runtime
    dependencies, no server lifecycle requirement for MCP clients, the same
    Python core is available in-process and parity is easier to test
    directly).
  - **No-solution is HTTP success.** A valid no-solution result
    (`selected = null`, `no_eligible_candidate`) is HTTP 200 — never 404,
    409, 422 or 500. Invalid client requests (malformed JSON, unknown keys,
    schema-invalid inputs, exclusivity violations, invalid overrides) are
    HTTP 400 with the frozen safe envelope
    `{"error": {"code": "invalid_request", "message": "..."}}`; HTTP 400 is
    chosen over 422 because the CLI already treats invalid input as one
    failure class and no repository evidence justifies a second validation
    status. Application/internal failures are HTTP 500 with the code
    `internal_error`. The error vocabulary is closed (`invalid_request`,
    `internal_error`); messages are safe structural messages only — no
    traceback, raw provider payload, credential or local credential path.
  - **Provider degradation remains domain data.** Provider operational
    states (`unavailable`, `auth_required`, `unsupported`,
    `schema_changed`, `unknown`, exhausted windows) stay normalized data in
    successful responses; ordinary telemetry degradation is never mapped to
    an HTTP transport error, and a degraded provider during select is
    handled by the existing unknown/degraded policy, never HTTP 503.
  - **Loopback-only default REST binding; no initial REST auth.** The
    default bind is `127.0.0.1`; there is no `0.0.0.0` default, no LAN
    exposure default and no remote multi-user assumption. M3 REST is a
    local machine interface, not an internet-facing service, which is the
    only reason no authentication layer is acceptable in M3; OAuth, API
    keys, sessions, reverse-proxy auth and TLS termination are excluded,
    and any non-loopback exposure requires an explicit future security
    decision. REST and MCP never accept provider credentials from clients,
    never return credentials, never accept arbitrary provider endpoints and
    never proxy prompts (D-001, D-009 unchanged).
  - **D-018 recovery semantics are inherited.** `status`/`select`/
    `simulate` may trigger the existing capacity collectors, so they may
    exercise the bounded provider-managed authentication recovery already
    accepted in D-018. The frozen wording is: selection issues no model
    prompt and does not intentionally consume inference quota; capacity
    collection uses the existing telemetry path and may exercise the
    bounded provider-managed authentication recovery already accepted in
    D-018. The operations are never called absolutely side-effect-free; they
    never execute model inference, consume reset credits, redeem
    replenishment, write provider configuration or dispatch selected
    models.
  - **No model execution.** No interface executes models, proxies prompts or
    dispatches fallbacks (D-001 unchanged).
  - **Versioning boundary.** Capacity contract version
    (`CapacitySnapshot.schema_version = 3`), machine-interface version (the
    `/v1/` path prefix and outer envelope `schema_version = 1`), catalog
    version and policy version are separate concepts, never collapsed. The
    machine-interface contract includes both its envelope and the
    serialized domain documents exposed inside it (`CapacitySnapshot`,
    `TaskRequirement`, `SelectorPolicy`, `ReplenishmentState`,
    `SelectionDecision`, `SimulationResult`, `SimulationOverrides`).
    Within v1, additive backwards-compatible domain fields may flow through
    v1 when existing clients remain valid; an incompatible removal, rename,
    type change or semantic change in **any** exposed nested domain
    contract is an incompatible machine-interface change requiring either a
    compatibility serializer preserving the v1 wire contract or a new
    machine-interface major version; changing only a domain's internal
    version number does not by itself require a machine-interface bump when
    the v1-visible wire shape stays backwards compatible. Existing domain
    serialization is reused, never forked; M3a creates no compatibility
    serializers. MCP tool names stay simple and unversioned;
    tool documentation states they expose machine-interface contract v1.
  - **Interface parity requirement.** Equivalent logical inputs must
    produce equivalent core results through CLI, REST and MCP. Transport
    wrapping may differ; the business result must not. For deterministic
    injected inputs, unwrapped REST/MCP results equal the direct
    application/core results (typed/domain equality, not byte equality),
    and CLI JSON remains semantically equivalent to the same core result.
    M3 closeout must prove `direct application == CLI JSON == REST == MCP`
    for representative deterministic scenarios.
  - **U-008 disposition for M3.** `scarcity_router` remains the stable
    Python module/package identity for M3; the final branded
    package/executable name is not frozen yet. REST/MCP development entry
    points stay module-based (for example `python -m
    scarcity_router.server`, `python -m scarcity_router.mcp`) until
    packaging proves necessary. No package publishing or release
    infrastructure is created in M3a. U-008 remains open only for final
    release-time branding (collision search pending).
  - **Dependency choice deferred.** M3a adds no dependency and does not
    edit `pyproject.toml`. The REST implementation should prefer the
    smallest justified framework and MCP should use the official MCP Python
    SDK if it materially reduces protocol risk, but each dependency must be
    justified in its own M3b/M3c implementation issue (D-013 unchanged).
    FastAPI is not frozen merely because earlier roadmap prose mentioned
    it.
  - **No caching/database/history in M3.** REST is a single local process
    with no daemon manager, background cache, database, persistent history
    or scheduler; each request may collect current telemetry through the
    existing application path. MCP's stdio lifecycle is owned by the MCP
    client. No request caching is invented in M3.
  - **Concurrency unchanged.** One request maps to one application
    invocation using the existing deterministic synchronous provider
    collection; no parallel provider collection semantics are introduced.
    If concurrent transport requests create lifecycle/resource concerns,
    the implementation must serialize or bound them explicitly — an M3
    implementation concern, not an M2 redesign.
  - **No M3 implementation in this decision.** M3a froze contracts and
    documentation only; M3b (REST) and M3c (MCP + parity tests) are
    separate issues that start only after M3a is merged into `develop`.
- **Reason:** M2 accepted the CLI semantics through live use; M3 adds
  machine interfaces whose transport code must not invent or diverge from
  that accepted behavior. Freezing envelopes, error classes, security
  boundaries and parity before implementation keeps the transports thin,
  makes parity testable and prevents framework-chosen semantics.
- **Boundary:** Documentation and contracts only. No REST/MCP runtime, no
  dependency change, no product source change, no selection semantic
  change, no provider change, no model execution and no M3b/M3c work.
- **Amendment (2026-09-06, single post-review remediation):** the contract
  choices and frozen surface are unchanged; three review blockers are
  closed in `docs/machine-interfaces.md` and this record, and the record's
  date was corrected to the UTC convention (the reviewed head was created
  2026-09-06T23:11:16Z). (1) The versioning rule is unambiguous: the
  machine-interface contract includes the envelope AND the serialized
  domain documents exposed inside it, so an incompatible nested-domain
  change (in any exposed contract among `CapacitySnapshot`,
  `TaskRequirement`, `SelectorPolicy`, `ReplenishmentState`,
  `SelectionDecision`, `SimulationResult`, `SimulationOverrides`) is an
  incompatible machine-interface change requiring a compatibility
  serializer preserving the v1 wire contract or a new machine-interface
  major version; a domain-internal version bump alone does not force either
  when the v1-visible wire shape stays backwards compatible. (2) Request
  parsing and field semantics are frozen: duplicate JSON keys and
  `NaN`/`Infinity`/`-Infinity` constants are `invalid_request`; exact
  missing/`null` semantics for select/simulate inputs, including
  `replenishment_states: null` = `invalid_request` at the select boundary,
  the exactly-one-effective-requirement-source rule and unknown
  `profile_id` = `invalid_request`; the nested simulation override
  tri-state is preserved and not conflated; MCP freezes the logical
  `invalid_request`/`internal_error` error payloads M3c must preserve.
  (3) The design scenarios gained the matching invalid-request and MCP
  error cases. No new decision number is created; D-028 remains the
  governing M3a decision.

### D-029 — Project-wide LLM operating policy and assignment separation

- **Status:** Accepted
- **Date:** 2026-09-07
- **Decision:** Adopt [`docs/llm-operating-policy.md`](llm-operating-policy.md)
  as the authoritative human-readable multi-model operating policy and keep
  its compact durable facts in `model-policy.json` policy version 5.
  - Stable capability archetypes and stable workflow role names are
    independent from specific model assignments.
  - Current model assignments are dated, revisable descriptive metadata. They
    do not establish selector eligibility, capability ratings or capacity
    bindings.
  - GPT-6 Astra at low reasoning effort is the preferred reference for
    scientific/methodological review, semantic figure review and deep technical
    reasoning. No Astra
    capacity scope, provider telemetry, model slug, hard property or
    capability rating is accepted by this decision.
  - GPT-5.6 Sol High is the primary translation specialist and an authorized
    alternate scientific reviewer or second opinion; Luna remains the
    orchestration/editorial reference, Flash remains the routine execution
    generalist and GLM-5.3 remains the deep technical reasoner.
  - Reasoning effort is an explicit routing concern. Lowest sufficient effort
    is the governing principle, and importance or size alone does not trigger
    escalation. Dynamic reasoning-effort optimization is not implemented in
    the selector.
  - Consequential review is independent; evidence preparation is separate from
    scientific/methodological adjudication; large work uses early durable
    checkpoints; repository and artifact state outranks session UI state;
    runtime identity uses explicit attestation states; completed work is not
    repeated without material source change or a concrete regression.
  - Finite retries, bounded convergence, the immutable reviewed head, phase
    serialization and the existing stricter one-initial-review,
    one-remediation and one-narrow-final-verification policy remain in force.
- **Reason:** The owner's updated operating model requires durable separation
  between role guidance and selector data, deliberate effort selection,
  independent judgment, evidence adjudication, incremental durable work and
  honest execution provenance without changing accepted M2 selector behavior.
- **Boundary:** `model-catalog.json`, selector semantics, task-profile numeric
  minima, provider adapters, capacity parsing, runtime attestation code,
  model execution and M3b/M3c remain unchanged. Astra selector onboarding is a
  separate evidence-first follow-up. The generic principle that local
  inference may be used when stable and sufficient is overridden for this
  project by D-017: local inference remains unsupported.

### D-030 — M3b local REST implementation

- **Status:** Accepted; boundary note added by D-044/D-045 (2026-09-19)
- **Date:** 2026-09-07
- **Decision:** M3b implements the frozen D-028 REST v1 surface as a thin,
  loopback-only adapter in `scarcity_router/server.py`, using the Python
  standard library only:
  - **Implementation choice.** `http.server.HTTPServer` +
    `BaseHTTPRequestHandler`; no FastAPI/Starlette/Flask/aiohttp/Uvicorn and
    no other runtime dependency is added (`pyproject.toml` and `uv.lock`
    unchanged). Exactly four local endpoints do not justify framework
    machinery, strict JSON semantics are controlled explicitly by reusing
    `selection_app.load_strict_json`, and the deliberate synchronous
    application semantics stay synchronous.
  - **Serialized request handling.** `HTTPServer` is single-threaded, so
    requests are serialized by construction: one request, one application
    invocation, the existing deterministic synchronous provider collection.
    This satisfies the D-028 concurrency requirement without locks around
    selector internals. A 60-second socket read timeout bounds a stalled
    client; it does not bound provider collection.
  - **Loopback-only fixed binding.** The bind address is the constant
    `127.0.0.1` and is not configurable; there is no `--host`/`--bind`
    option and no `0.0.0.0` path. The only runtime flag is `--port`
    (default 8765). The public runtime entry point stays module-based under
    U-008: `python -m scarcity_router.server`.
  - **Request-body rules.** Maximum body size is frozen at
    `MAX_REQUEST_BODY_BYTES = 1_048_576` (1 MiB). POST requires exactly one
    valid decimal `Content-Length`; malformed, negative, duplicate or
    missing Content-Length and any `Transfer-Encoding` (no chunked request
    bodies) are rejected with HTTP 400 `invalid_request`. Content-Type must
    be `application/json` with an optional `charset=utf-8` parameter only;
    bodies are decoded as strict UTF-8. Duplicate JSON object keys and
    `NaN`/`Infinity`/`-Infinity` fail through the existing strict parser.
    An oversized declared body is drained with a bounded best-effort read
    (never stored or logged) so the client can read the 400 before the
    connection closes.
  - **Typed in-memory application seam.** `selection_app` gains
    `select_from_inputs` and `simulate_from_inputs`, which take typed
    in-memory inputs (profile id XOR explicit `TaskRequirement`, optional
    tightening, optional `SelectorPolicy` defaulting to the neutral policy,
    a replenishment-state tuple, the loaded catalog/profile artifacts,
    collectors and clock), resolve the requirement through the existing
    authoritative `resolve_requirement`, apply tightening exactly once,
    obtain ONE aware instant and call the existing `select_model` /
    `simulate_selection` cores. The file-based CLI runners `run_select` /
    `run_simulate` delegate to the same seam after loading their JSON
    files; the REST adapter contains no selection, requirement-resolution
    or capacity logic and uses no temp files, subprocesses or CLI
    invocation. The future M3c MCP adapter calls the same seam directly.
    `/v1/status` and CLI status share `canonical_snapshot_documents` so no
    interface keeps its own snapshot serialization.
  - **Typed client-input error boundary.** `ApplicationInputError` (in
    `errors.py`, deriving from `SelectionContractError`) marks
    client-controlled failures — strict JSON failures, request-shape
    failures, `from_dict` failures for `TaskRequirement`,
    `SelectorPolicy`, `ReplenishmentState` and `SimulationOverrides`, the
    requirement-source XOR violations (including the previously
    argparse-enforced both-present case, now enforced in
    `resolve_requirement` itself), unknown profile ids and invalid
    tightenings. The REST adapter maps it to HTTP 400 `invalid_request` by
    type, never by matching exception message text; everything else is
    HTTP 500 `internal_error` with the fixed message "internal server
    error" (no paths, tracebacks or exception details). The CLI's single
    invalid-input failure class is unchanged because the new type derives
    from `SelectionContractError`.
  - **Security and runtime scope.** No authentication, no TLS, no
    non-loopback exposure, no daemonization, no PID files, no cache,
    database or history. Request logging is suppressed entirely
    (`log_message` is a no-op), so no request body, credential, provider
    payload or traceback is ever logged; framework HTML error pages are
    replaced with body-less status responses so request material is never
    reflected. `/healthz` touches no collector, artifact or application
    component. No model execution, prompt proxy or dispatch exists.
  - **Routing.** Unknown paths (including any query or fragment suffix,
    which are not part of v1) return 404; a known path with an unsupported
    method (`HEAD`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, and each route's
    non-listed methods) returns 405 with an `Allow` header — never the
    framework-default 501. No new domain error codes are invented for
    routing responses.
- **Reason:** M3a deliberately deferred the implementation choice to M3b.
  A standard-library single-threaded server is the smallest implementation
  that satisfies the frozen contract: it adds zero dependencies while
  U-008 packaging remains narrow, makes the strict parsing explicit rather
  than framework-chosen, and provides serialization by construction.
- **Boundary:** REST transport only. No selection/scarcity/provider
  semantic change (the requirement-source XOR now enforced in
  `resolve_requirement` was previously enforced only by the CLI's
  mutually exclusive argparse group), no catalog/rating/policy change, no
  MCP implementation (M3c still pending), no auth/TLS/non-loopback option,
  no dependency change.
- **Boundary note (2026-09-19, D-044/D-045):** This decision governs the
  loopback REST v1 adapter only, and it is preserved verbatim for that adapter.
  The execution-gateway server component is a separate, authenticated,
  TLS-terminated surface set defined by D-044 and D-045 — it is never created
  by relaxing this adapter's fixed `127.0.0.1` binding, auth-free posture or
  frozen endpoint set.
- **Amendment (2026-09-07, single remediation):** Semantic failures while
  applying an otherwise schema-valid simulation override use the narrow
  `SimulationOverrideApplicationError` type and become the existing
  `invalid_request` / HTTP 400 class at the application boundary; unrelated
  selection, catalog, collector and internal failures remain HTTP 500.
  Every request also requires exactly one exact loopback `Host` value (the
  bare address or the actual bound port) before route dispatch, closing the
  DNS-rebinding gap in the no-auth loopback boundary. Content-Length decimal
  magnitudes are compared with the 1 MiB limit before integer conversion, so
  pathological digit strings fail safely as HTTP 400 without being echoed or
   logged. M3b references use D-030; D-029 remains the operating-policy
decision.

### D-031 — M3c stdio MCP implementation

- **Status:** Accepted; boundary note added by D-044/D-045 (2026-09-19)
- **Date:** 2026-09-07
- **Decision:** M3c implements the frozen machine-interface v1 MCP surface as
  a thin, local stdio adapter:
  - **Official SDK.** Adopt the official MCP Python SDK stable v2 line,
    package constraint `mcp>=2,<3`, resolved to `mcp` 2.1.1 in `uv.lock`.
    The dependency is placed in the existing development dependency group for
    the current module-based runtime. No `[project]` table, final package
    branding or `mcp[cli]` extra is introduced; installable-package runtime
    metadata remains a U-008 release/packaging concern.
  - **Low-level server.** Use `mcp.server.lowlevel.Server` with the official
    `mcp.server.stdio.stdio_server` transport. The low-level API gives M3c
    exact control over tool names, `structured_content` and `is_error` instead
    of delegating logical result/error shaping to a high-level wrapper.
  - **Surface and lifecycle.** Expose exactly `scarcity_status`,
    `scarcity_select` and `scarcity_simulate`, with no resources, prompts,
    sampling, elicitation, subscriptions, execution tools or version suffixes.
    Stdio is the only transport; the MCP client owns the process lifecycle.
    No HTTP, SSE, Streamable HTTP, auth layer or network listener is added.
  - **Application boundary.** MCP calls the shared transport-neutral
    `ApplicationDependencies` record and the typed
    `select_from_inputs`/`simulate_from_inputs` application seam in-process.
    `machine_api.py` owns the shared logical request parsing and v1 envelopes
    used by REST and MCP. MCP never imports or starts the REST runtime, invokes
    the CLI, uses temp files or duplicates selector/scarcity/provider logic.
  - **Results and errors.** Successful tools return the exact REST logical
    envelopes as `structured_content`, plus one deterministic text block with
    the same JSON. Logical `invalid_request` and `internal_error` payloads set
    `is_error = true`; valid no-solution and degraded-provider results remain
    successful domain data. Internal messages are fixed and safe.
  - **Verification.** Fixture-based tests use synthetic collectors and a
    fixed clock to prove direct/CLI/REST/MCP parity for status, select and
    simulation, cover the logical error matrix and assert no secret/error
    detail leakage. Official in-memory Client tests and a real stdio subprocess
    discovery smoke verify the transport lifecycle without live collection.
  - **Scope.** No model execution, prompt proxying, automatic dispatch,
    provider/catalog/policy/ranking change, new provider, auth input, provider
    endpoint or catalog-path input is permitted. M3 closeout and live
    acceptance are tracked separately; this decision does not by itself mark
    M3 PASS.
  - **Raw MCP parser boundary (narrow D-028 amendment).** MCP transport
    framing and JSON-RPC decoding are owned by the official MCP SDK.
    Scarcity Router's `invalid_request` logical contract begins at the
    tool-argument object delivered by that SDK. All application-level argument
    semantics remain identical to REST. Raw JSON-RPC framing failures,
    including duplicate-name/non-standard-number behavior that occurs before
    the tool handler, follow official SDK/protocol transport behavior and are
    not reimplemented by Scarcity Router.
- **Evidence:** A bounded ephemeral probe of installed MCP SDK 2.1.1 APIs
  verified `mcp.server.lowlevel.Server`, `stdio_server`, the `Tool` and
  `CallToolResult` structured fields, the official in-memory Client and the
  official stdio subprocess Client. The same SDK JSON decoder accepted a
  duplicate object name with last-value normalization and carried `NaN`,
  `Infinity` and `-Infinity` in untyped tool arguments as floats; duplicate
  evidence is therefore unavailable to the handler. Malformed JSON syntax and
  malformed JSON-RPC shape were rejected by the decoder before the tool
  handler. Typed Scarcity Router parsing remains strict for fields it owns.
- **Reason:** The official SDK preserves protocol interoperability and owns
  stdio framing, while the low-level API preserves exact M3 logical result and
  error envelopes. Avoiding custom protocol parsing prevents a second MCP
  implementation and honestly records the unavoidable raw-transport boundary;
  sharing `machine_api.py` and the application seam keeps CLI, REST and MCP
  semantics identical.
- **Boundary:** This is an MCP transport and machine-boundary decision only.
  It does not change `load_strict_json`, selector ranking, scarcity formulas,
  capacity schema, provider adapters, model catalog, model policy, ratings,
  reasoning effort, OpenAI/Z.ai acquisition, Astra onboarding or live M3
  closeout. The REST strict duplicate/non-finite JSON contract remains
  unchanged.
- **Closeout evidence (2026-09-07):** Live CLI, REST and MCP acceptance passed
  without changing any provider, capacity, selector, catalog, policy or
  machine-interface semantics. Sanitized evidence is recorded in
  [`docs/m3-acceptance.md`](m3-acceptance.md); this milestone acceptance does
  not create a new decision number.
- **Boundary note (2026-09-19, D-044/D-045):** This decision governs the local
  stdio MCP adapter only, and it is preserved verbatim for that adapter. The
  optional remote bridge (M08) is a configured client mode against the
  authenticated server component; it is never created by adding a network
  listener, authentication surface or execution capability to this stdio
  adapter. MCP remains recommendation/control only under the execution-gateway
  program (D-040).

### D-032 - Reasoning-effort-aware model configurations

- **Status:** Accepted by explicit owner/architect assignment, issue #57
- **Date:** 2026-09-08
- **Supersedes:** D-027's ranking order only by inserting effort after capability
  margin; D-025's four-entry catalog scope by adding three calibrated
  configurations; D-029's implementation-status claim that effort-aware
  selection is not implemented. Historical M2/M3 acceptance remains unchanged.
- **Contract:** Catalog v2 adds explicit `ModelCatalogEntry.reasoning_effort`:
  `none < low < medium < high < xhigh < max`. Null/absent is no configured
  value (not applicable or support unknown), not the real string setting
  `"none"`. Construction and deserialization reject unknown values, require
  effort when reasoning support is true, and require null/absent when support
  is false or unknown. No provider-specific validation or inference from
  display names, model names or variants is permitted.
- **Identity:** `ModelIdentity(provider, model, variant)` is unchanged. Variant
  remains an opaque stable configuration identifier, never parsed for effort.
  The current catalog deliberately uses readable effort spellings as variants;
  only the explicit catalog field supplies semantics.
- **Calibration:** Capabilities belong to an invocation configuration. No
  automatic cloning across efforts is allowed. Preserve all accepted Luna Max,
  Sol High and GLM vectors and explicitly encode their max/high/max/max effort.
  Add exactly Luna Medium `(3,4,3,5,5,4)`, Terra Medium `(4,5,4,4,5,4)` and
  Sol Medium `(5,5,4,5,5,4)` in reasoning/coding/scientific/writing/tool/translation
  order. These initial effort-specific judgments use medium confidence and
  dated owner calibration evidence, not invented benchmark precision.
- **Evidence:** The architect supplied accepted official facts using existing
  source identifiers `https://openai.com/index/gpt-5-6/` (2026-07-09 launch)
  and `https://developers.openai.com/api/docs/models`. Luna is cost-sensitive,
  Terra balanced intelligence/cost, Sol flagship complex-professional work;
  family coding, professional, science/health and long-context evidence places
  Terra between Luna and Sol. All three support the six normalized efforts,
  with Medium the API default; Sol remains very strong at Medium. This task
  accepts the supplied evidence, not a newly performed benchmark or web audit.
  Developer-source dates of 2026-09-08 record this supplied evidence review.
  `accepted_reasoning_effort_calibration_2026-09-08_issue_57_D-032` identifies
  the owner-approved vectors and rationing direction in issue #57 and this
  decision. Terra's evidenced properties are 1,050,000 input tokens, 128,000
  output tokens, tools/vision/reasoning true and `openai/codex` applicability.
- **Ranking:** After unchanged eligibility and reservation gates, compare
  known capacity before degraded unknown, scarcity penalty, capability margin,
  lowest reasoning effort, explicit preference, then stable identity. Unknown
  or non-applicable effort uses an explicit typed comparison state after known
  effort, with no numeric intensity or magic sentinel. It never ranks cheaper
  than `none`. No effort can rescue a capability failure or override scarcity
  or capability margin. Unrequired capability dimensions still do not count.
- **Capacity:** Model capability, reasoning intensity and subscription capacity
  scarcity are independent. All five OpenAI configurations share the observed
  `openai/codex` assessment. No effort-specific quota bucket, fake percentage,
  penalty multiplier or API-price cost is invented. Capacity v3, collectors,
  matching, scarcity formula, reservations and replenishment are unchanged.
- **Versioning and migration:** Catalog version becomes 2, updated 2026-09-08.
  External catalog authors must explicitly calibrate/encode effort for
  reasoning-capable entries; legacy absent effort is not inferred from v1
  variant strings. Null/absent remains valid for false/unknown reasoning
  support. There is no automatic legacy catalog conversion. Model-policy schema
  remains v1; compatible policy content advances to policy_version 6 because
  `reasoning_effort_policy.selector_support` changed from `not_implemented` to
  `calibrated_configurations`; task profiles, calibrated minima, workflow
  assignments and other policy semantics remain unchanged.
- **Machine compatibility:** CLI JSON, SelectionDecision/CandidateEvaluation
  field sets, REST paths and v1 envelopes, MCP tools/input schemas and errors
  remain unchanged. Selection exposes the configured identity variant;
  reconstruct effort ordering with the decision's versioned catalog and
  structured margin/scarcity evaluations. No new prose-only explanation
  subsystem or explicit public effort field is added; that is future v2 work.
- **Boundary:** No other effort configurations are calibrated; API defaults
  never auto-populate entries. Astra remains deferred to independent issue #49.
  No M4, execution, model dispatch, local inference or new architecture.
  One implementation session, no internal reviewer loop; a human freezes the
  PR head for one independent external review and remains the merge gate.

### D-033 - GPT-6 Astra Low selector onboarding evidence gate

- **Status:** Unresolved onboarding; evidence-only disposition under the explicit
  owner/architect gate for [issue #49](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/49).
- **Date:** 2026-09-09
- **Outcome:** `ASTRA_ONBOARDING_READY = NO`;
  `ONBOARDING_REQUIRES_PLAN_APPLICABILITY_EXTENSION`;
  `STOP_AND_ESCALATE_TO_HUMAN`. No Astra entry or routing change is authorized.
- **Prerequisites and current-state reconciliation:** Clean starting worktree;
  base `d76b4a09ffa4365ea22b3121086b8198a197eaef` contains merged PR #58 and
  required ancestor `f503107ca9d82bdd34239af8f9a4261f6282c70c`. D-032 is
  implemented: catalog v2 has seven configurations and policy v6. The original
  #49 four-candidate premise is historical, not current. `AGENTS.md`'s claim
  that M3 is next and the roadmap's pending-integration wording for D-032 are
  stale status descriptions: D-031 closeout and merged #58 establish M3 PASS
  and D-032 integration. This record explicitly reconciles those status
  conflicts; no historical acceptance, D-032 semantics or milestone is reopened.
- **Identity and effort:** Supplied architect-reviewed identity is GPT-6 Astra,
  release 2026-09-03, `openai` / `gpt-6-astra` / `low`, with explicit
  `reasoning_effort = low`. Official model documentation corroborates model ID
  and `low|medium|high|xhigh|max`; Astra does not support `none`. The global
  vocabulary remains unchanged, and variant is never parsed for effort.
- **Hard-property evidence:** Official model documentation confirms text/image
  input, text output, 128,000 output tokens and tool/vision/reasoning support.
  It lists a 1,050,000-token context window **and** maximum input of 922,000.
  The assignment supplied `input_context_tokens = 1_050_000`; the existing
  contract describes an input allowance. That mapping is unresolved, not
  silently accepted or substituted. Human reconciliation of total context
  versus supported input allowance is required before a catalog entry.
- **Calibration:** Preserve the architect-approved conditional vector
  `(5,5,5,5,5,4)` in reasoning/coding/scientific/writing/tool/translation order,
  with medium effort-specific confidence and rationale in
  [model-calibration.md](model-calibration.md#astra-low-evidence-gate-issue-49).
  No numeric Astra calibration is installed in the active catalog. Low is not
  low capability; comparative evidence does not establish universal dominance.
- **Capacity decision:** Shared Work/Codex allowance and its normalized main
  `openai/codex` scope are evidenced, but completeness of a codex-only Astra
  binding is not. Official guidance distinguishes Pro $100/$200 and Business
  Premium (full existing allowance) from Plus/Business Standard (limited Astra
  usage within that allowance). The current static bindings and informational
  plan cannot enforce plan/seat/access conditions. One sanitized status read
  observed OpenAI `ok`, main `codex` and one additional scope; that observation
  does not identify any Astra-specific constraint. No private scope is named
  or assigned. See the A-E matrix in
  [capacity-model.md](capacity-model.md#astra-onboarding-gate-issue-49).
- **Alternatives and evidence needed:** Keep Astra outside the catalog now.
  Generic onboarding requires authoritative model-to-scope applicability for
  every constraining limit across supported plans, safe normalized telemetry
  for those limits and evidenced access/seat semantics. A plan-applicability
  extension or an explicitly narrower support boundary requires human design
  approval; neither is implemented here. Existing multiple bindings may express
  an evidenced extra scope, but cannot infer its applicability. Whether future
  support needs new collector parsing remains unresolved; no collector change
  is required or made for this evidence-only result. Do not create another
  issue automatically; #49 remains the durable onboarding issue.
- **Marginal consumption:** Model, effort, task size and Fast mode can change
  allowance consumption. Public message ranges are not per-task coefficients.
  Scarcity remains observed remaining subscription capacity, not predicted
  marginal cost. No model multiplier, percentage deduction or API-price penalty
  is introduced; any future marginal-consumption design is deferred. Capability
  margin, effort ordering and profile minima remain D-032 unchanged.
- **Versioning and routing:** Catalog v2 and policy v6 (schema v1) are unchanged;
  capacity v3 and machine-interface v1 are unchanged. All eight current eligible
  sets and conditional future effects are recorded in model-calibration.md.
  Catalog v3 is not produced, and no Astra-specific tests or live selection
  acceptance are claimed.
- **Live and execution boundary:** One existing Kilo session; requested Astra
  Low, `RUNTIME_UNOBSERVABLE` because no independent generated-turn metadata
  verifies the complete model/effort assignment. No subagents, model switching,
  internal reviewer loop or deliberate model execution for quota research.
  Only one status observation, filtered before inspection, using existing
  collectors including the D-018 recovery boundary. No personal percentages,
  reset timestamps, raw responses or private identifiers retained. Budget is
  120 minutes with a bounded evidence pass; implementation stops at this gate.
  The docs-only PR remains open/unmerged for the human gate; M4 does not start.
- **Sources:** Official pages accessed 2026-09-09:
  [model](https://developers.openai.com/api/docs/models/gpt-6-astra.md),
  [usage guidance](https://help.openai.com/en/articles/20001516-managing-usage-with-gpt-6-astra-in-work-and-codex),
  [Work and Codex](https://help.openai.com/en/articles/20001275).
  Release date and independent benchmark comparisons are attributed to the
  architect-reviewed evidence supplied in the 2026-09-09 assignment, not a new
  benchmark run. This decision supersedes no accepted ranking or capacity rule.

### D-034 — Package Scarcity Router for local deployment

- **Status:** Accepted
- **Date:** 2026-09-09
- **Decision:** Package the application as the installable local
  distribution `scarcity-router` version `0.1.0` (issue #63), superseding the
  tooling-only no-`[project]` state of `pyproject.toml`.
  - **Build backend: Hatchling.** uv_build cannot include the
    root-authoritative `model-catalog.json` and `model-policy.json` in the
    wheel without committed in-package duplicates; Hatchling `force-include`
    maps the two single root copies into the wheel as
    `scarcity_router/model-catalog.json` /
    `scarcity_router/model-policy.json` at build time. No duplicate
    authoritative copies are committed; the sdist is a minimal allowlist
    (package, the two root artifacts, `pyproject.toml`, README, LICENSE)
    without tests, docs, tooling or local files.
  - **Metadata.** `requires-python = ">=3.12"`, Apache-2.0 (SPDX expression
    plus the LICENSE file), runtime dependency exactly `mcp>=2,<3` (the
    official SDK is now runtime metadata, not only a dev dependency), and
    the version read dynamically from the `scarcity_router.__version__`
    literal (`0.1.0`), the single committed source of truth.
    `get_version()` prefers installed distribution metadata and falls back
    to the deterministic source literal.
  - **Entry points.** Exactly the three console scripts `scarcity-router`
    (`cli:main`), `scarcity-router-mcp` (`mcp:main`) and
    `scarcity-router-server` (`server:main`); the `python -m
    scarcity_router`, `python -m scarcity_router.mcp` and `python -m
    scarcity_router.server` module forms remain supported. Parser help
    shows the invoked script name when run through a console script.
  - **Default artifact resolution.** `resolve_default_artifact` prefers the
    repository-root copies when running from a checkout and otherwise uses
    the packaged `importlib.resources` copies, depending only on module
    location — never on cwd or environment variables. Explicit
    `--catalog` / `--model-policy` overrides always bypass the defaults.
  - **Branding.** The MCP server advertises `version=get_version()`
    instead of the `"m3c"` milestone label; no other runtime surface
    changes.
  - **U-008 scope.** This resolves U-008 for local deployment naming
    (`scarcity-router`, hyphenated distribution matching the existing
    module identity `scarcity_router`). The release-time collision search
    and any publication to a public index remain open under U-008/U-009;
    nothing is published, tagged or merged by this decision.
- **Reason:** The owner needs a one-time local install with stable command
  names instead of checkout-relative module invocations, without changing
  selector, provider, REST/MCP, security or serialized contracts.
- **Boundary:** Packaging metadata, default resource resolution, entry
  points, version branding, tests, the `package-check` target and
  documentation only. No catalog/policy content or version change, no
  selection semantics change, no provider change, no REST/MCP schema
  change, no new network exposure, no credential handling change, and no
  publication.

### D-035 — Happy-hour quota-preference windows

- **Status:** Accepted
- **Date:** 2026-09-12
- **Issue:** BioMedical-IT/scarcity-router#69
- **Decision:** Add weekly happy-hour rules to the user resource policy as
  the preference-side counterpart of blackouts, driven by the owner's
  GLM-5.3-Flash usage campaign (owner-provided campaign text, 2026-09-03
  through 2026-09-20, daily 23:00-09:00 Asia/Singapore, zero quota via
  ZCode and doubled quota via other supported agents for GLM-5.3-Flash
  only).
  - **Contract.** `WeeklyHappyHourRule` mirrors `WeeklyBlackoutRule`
    exactly — exact `AvailabilityTarget` identity matching, explicit IANA
    time zone, duplicate-free canonical `mon`-`sun` weekday vocabulary,
    strict 24-hour `HH:MM` wall times, half-open `[start, end)` local
    intervals with cross-midnight support, `start == end` invalid — plus
    optional inclusive local calendar bounds `start_date`/`end_date`
    (`YYYY-MM-DD`, `start_date <= end_date`) because vendor campaigns are
    limited time; both absent means a standing recurring window. The
    schedule matcher is one shared implementation so both rule kinds can
    never drift apart at boundaries.
  - **Semantics.** During an active window, matching candidates form a
    preferred ranking group inserted after the capacity knowledge class
    and before the scarcity penalty: a preferred known-capacity candidate
    outranks a non-preferred one with healthier quota, because the
    targeted model's marginal quota cost inside the window is zero or
    discounted. The preference is ranking only and never an eligibility
    bypass: blackout, hard constraints, capability sufficiency,
    capacity exhaustion/unknown policy and reservations all still gate
    first; unknown-capacity candidates remain separated by the knowledge
    class; explicit exhaustion still excludes (campaign small print such
    as weekly caps is exactly why "free" never fabricates capacity); and
    telemetry, scarcity and capability are never rewritten. First
    matching rule in canonical `rule_id` order decides; blackout wins
    over an overlapping happy hour by stage order.
  - **Serialization compatibility.** `UserPolicy` gains the additive
    optional member `happy_hours`: documents without the key load
    unchanged with an empty tuple, and an empty tuple is never
    serialized, so pre-D-035 documents round-trip byte-identically. A
    document carrying `happy_hours` fails loudly on an older reader
    ("unknown keys") rather than being silently misread. Rule IDs remain
    unique across reservations, blackouts and happy hours. Machine
    envelopes and the `SelectionDecision` field set are unchanged; the
    per-candidate `happy_hour_decision` (present only when preferred)
    is additive inside the existing candidate payload. The decision also
    carries the additive optional member `expired_happy_hour_rules`
    (serialized only when non-empty): the rule ids whose weekly window
    would cover the evaluated instant but whose inclusive date bounds do
    not — the campaign-ended signal. It is explanation-only provenance,
    never a ranking or eligibility input, and `--explain` lists it under
    "Expired happy-hour rules"; the compact human output names the active
    preference with one `Happy hour:` line.
- **Alternatives considered:** treating a happy hour as an inverse
  blackout (hard-blocking non-targeted models) — rejected because it
  manufactures exclusions from pricing knowledge and can manufacture
  no-solution results; modeling zero-quota campaigns as capacity
  telemetry (penalty discounts or unknown rewriting) — rejected because
  quota economics must never mutate normalized capacity or capability;
  a ranked-only soft bonus below scarcity — rejected as too weak for the
  stated goal ("extremely preferable") and indistinguishable from the
  existing `preference_order` tie-break.
- **Boundary:** `scarcity_router/policy.py`, `selector.py`,
  `selection_app.py` explanation rendering, `examples/selector-policy.json`
  (campaign rule added), tests and docs. No catalog ratings change, no
  provider change, no REST/MCP schema-version change, no execution or
  gateway behavior.

### D-036 — Default user configuration location and provisioning

- **Status:** Accepted
- **Date:** 2026-09-12
- **Issue:** BioMedical-IT/scarcity-router#69
- **Decision:** The service has exactly one default configuration
  location, `$(XDG_CONFIG_HOME or ~/.config)/scarcity-router/`, holding
  the optional `selector-policy.json` user selector policy.
  - **Provisioning.** Every surface (CLI, MCP, REST process start)
    provisions the file from the audited `examples/selector-policy.json`
    when it is missing — `uv tool install` has no post-install hook, so
    first use is the effective installation step — and an existing file
    is never silently overwritten. The explicit
    `scarcity-router install-config [--force]` command provisions or
    replaces it on demand. Directory mode `0o700`, file mode `0o600`;
    the content is the checked-in example verbatim and never contains
    credentials, tokens or provider endpoints. An installed wheel
    packages the example as
    `scarcity_router/default-selector-policy.json` via the D-034
    force-include mechanism (no committed duplicate; the sdist gains
    `/examples/selector-policy.json` so wheels rebuild). The D-034
    package-check tripwire that forbids `examples/` wholesale gains
    exactly one allowlisted exception,
    `examples/selector-policy.json`, because that file is now a product
    provisioning source by this decision — the blanket rule still keeps
    every other example, test, doc and tooling artifact out of the
    distribution.
  - **Precedence.** CLI: explicit `--selector-policy FILE` wins, then
    the default config file, then the documented neutral policy, with
    `--neutral-policy` ignoring the config for one run. MCP/REST: a
    request that supplies `selector_policy` wins; a request that omits
    it runs under the server-configured default policy loaded at
    process start (`ApplicationDependencies.default_policy`), neutral
    when none resolves. This consciously refines the machine-interface
    wording "missing means the neutral policy" to "missing means the
    server default, else neutral"; envelope schemas, field sets and the
    missing-vs-null semantics are unchanged, and the decision documents
    which policy applied through its usual provenance fields.
  - **Failure behavior.** Provisioning or loading failures degrade to
    the neutral policy with one concise stderr warning and never block
    a selection; a config file that exists but fails to load directly
    (application loaders) is a loud configuration error, never a
    silent fall-through. The run that implicitly provisions the file
    announces it once on stderr
    (`note: provisioned default user config: <path>`); an existing file
    is never announced, `install-config` output is unchanged, and JSON
    stdout stays clean for piping.
- **Reason:** The owner does not want to hand-create policy files or add
  flags to the MCP command; one XDG location provisioned from the
  reviewed example gives every surface the same policy with zero wiring,
  while explicit overrides and the neutral escape hatch keep the
  previous opt-in behavior available.
- **Security note:** This is new product write access outside the
  package (previously none); it is bounded to one artifact, written with
  private permissions, content-audited, and recorded in
  `docs/security.md` ("Minimal filesystem access").
- **Boundary:** New `scarcity_router/config.py`, `cli.py` (install-config,
  `--neutral-policy`, default resolution), `mcp.py`/`server.py`
  (default-policy fallback), `selection_app.ApplicationDependencies`,
  packaging metadata, tests and docs. No selector semantics change, no
  provider change, no new network exposure, no credential handling
  change.

### D-037 — Role-split scarcity blend and the advisory short-window floor

- **Status:** Accepted
- **Date:** 2026-09-13
- **Issue:** BioMedical-IT/scarcity-router#71
- **Amends:** D-026 / U-007 (the cross-window most-restrictive aggregation
  rule only; the penalty function, label boundaries, reservation semantics
  and unknown-capacity policy are unchanged)
- **Decision:** Split every applicable capacity window into one of two
  frozen roles — a code-level mapping, never user configuration — and blend
  the two role aggregates into one scarcity penalty.
  - **Roles.** `kind: weekly` windows are *strategic* (long-horizon
    subscription health; exhaustion blocks for days). `kind: five_hour`
    windows and `resource: time` windows (the Z.ai `TIME_LIMIT`
    normalization) are *tactical* (whether work can happen right now;
    exhaustion self-heals in hours). Unknown-kind token windows are
    conservatively *strategic*, preserving the pre-D-037 most-restrictive
    outcome for exactly that shape; unknown-resource windows never carry a
    percentage pair and therefore never reach the blend.
  - **Blend.** Within each role the pre-existing most-restrictive rule
    governs: each role is represented by `min(remaining)` over its known
    applicable windows across all bound scopes. With `a` the tactical and
    `b` the strategic representative, and the owner's planning assumption
    that weekly quota equals five × five-hour quota (`WEEKLY_TO_FIVE_HOUR_RATIO
    = 5`, six total units):
    `units = a + 5*b`, `effective_remaining_percent = units // 6`
    (integer floor, 0..100). A role with no known windows defaults its slot
    to the other role's value, so a single-role candidate keeps exactly its
    own percentage (`only strategic b -> 6b//6 = b`; `only tactical a ->
    6a//6 = a`) — no capacity is invented for a missing bucket. The penalty
    and label remain the frozen pure functions of the blended effective:
    `penalty_units = (100 - effective)^2` on scale 10000,
    `label = scarcity_label(effective)`. Ranking compares the same integer
    units as before; the ranking-key order (knowledge class → happy-hour
    preference → penalty → capability margin → effort → preference order →
    identity) is unchanged.
  - **Evidence.** `ScarcityAssessment` gains the additive optional members
    `strategic_window` and `tactical_window` (`GoverningWindowEvidence`,
    serialized only when that role had a known window, role-consistent,
    never fabricated for a missing role). For a known assessment the
    governing window is the strategic representative when one exists, else
    the tactical one; the old invariant "governing remaining equals
    effective remaining" is replaced by blend consistency
    (`effective == blended(tactical, strategic)`), so a serialized known
    assessment without role evidence fails validation loudly on a new
    reader. As with D-035's additive members this is accepted because
    decision deserialization is not an input boundary in this slice: the
    REST/MCP/CLI surfaces render `to_dict` output and never ingest stored
    decisions. Exhaustion semantics are unchanged: any known applicable
    window at 0% — in either role — is `unavailable` (governing exhausted
    window, role members absent), and unknown/incomplete telemetry is
    still never read as a number.
  - **Advisory short-window floor.** `SelectorPolicy` gains the additive
    optional member `short_window_floor_percent` (integer 0..100; absent
    means the documented default 10, `0` disables). For an eligible
    candidate with known capacity whose tactical representative is below
    the floor, the selector sets the additive per-candidate advisory flag
    `short_window_below_floor` (serialized only when true; eligible
    candidates still carry no reason codes, so this is a flag, not a
    code). The flag never demotes, excludes or rewrites ranking — the
    answer to "the short window may not finish the task, consider another
    provider" is the warning plus the exact ranking order, not a hidden
    eligibility stage. `render_select_human` warns when the *selected*
    candidate is flagged, and `--explain` shows both role representatives.
- **Owner cases frozen by test:** tactical/strategic 80/20 → effective 30,
  penalty 4900; 20/80 → 70, 900; 5/95 → 80, 400 (preferred); 95/5 → 20,
  6400. A healthy short window still cannot hide a critical weekly one
  (weekly weight 5/6), and a drained short window no longer hides a
  healthy weekly one.
- **Alternatives considered:** configurable per-kind weights — rejected:
  new tuning knobs for no evidenced need and an averaging story that
  breaks explainability; reset-proximity penalty using `resets_at` —
  rejected: D-026 explicitly excludes reset proximity, `resets_at` is
  honestly unknown on several provider shapes, and the duration proxy in
  the role split achieves the intent with one frozen integer; lexicographic
  (weekly, then 5h) ordering without a blend — rejected: cannot express
  "5h=5/weekly=95 is strategically healthier than 5h=95/weekly=5" as a
  strength, only as a tie; floor-as-ranking-class (demoting below-floor
  candidates behind all feasible ones) — rejected by owner guidance: the
  5h=5%/weekly=95% candidate must *rank first* against 5h=95%/weekly=5%,
  so a short-window deficit must warn, not demote.
- **Boundary:** `scarcity_router/scarcity.py`, `selector.py`
  (`SelectorPolicy`, candidate evaluation flag), `selection_app.py`
  rendering, `scarcity_router/__init__.py` exports, tests and docs. No
  provider adapter changes (roles derive from already-normalized
  `resource`/`kind`), no catalog ratings change, no REST/MCP
  schema-version change, no execution or gateway behavior.


### D-038 — GLM-5.3 reasoning-effort identities and evaluation-only profiles

- **Status:** Accepted
- **Date:** 2026-09-15
- **Issue:** BioMedical-IT/scarcity-router#79
- **Related:** Infrastructure/creatidy-autonomy#11 (M3.1), D-032

Z.ai documents GLM-5.3 reasoning effort as a request parameter
(`reasoning_effort`: `low`/`high`/`max`, thinking forced enabled) on the same
chat-completions surface the Coding Plan exposes. Representing only the
default `max` identity made the routed variant unenforceable downstream: a
router decision carries `provider/model/variant`, but the catalog offered no
lower-effort identities to select.

Decision:

1. Catalog v3 adds `zai/glm-5.3` at `high` and `low` with conservative,
   separately evidenced ratings (medium/low confidence; Max ratings are not
   copied — see `docs/model-calibration.md`). No flash variants, no new
   providers.
2. Policy v7 adds the `evaluation_profiles` section. Evaluation profiles are
   a distinct profile class: they MUST declare `evaluation_only: true` and
   MUST pin the exact identity under evaluation in their hard constraints.
   They resolve only by explicit `profile_id` and therefore cannot silently
   affect ordinary routing, while formal `task_profiles` keep the
   no-provider/model-name invariant (D-025 acceptance unchanged).
3. The M3.1 calibration consumes those profiles from creatidy-autonomy
   through the frozen REST v1 `profile_id` path only.

- **Status update (2026-09-15, after the bounded calibration):** all six live
  runs (two per variant, frozen snapshot creatidy-autonomy@f9a7d16) returned
  structurally valid, evidence-correct results; the low effort found zero
  issues on a snapshot with five independently verified ones (twice), while
  high and max produced materially useful findings. Policy v8 therefore adds
  the calibrated production profile `repository_review` (L3, minima
  reasoning 4 + coding 4, `requires_reasoning_mode`) so bounded reviews
  cannot silently route to the low effort after catalog v3; the choice among
  qualifying identities stays with scarcity. The high-vs-max reliability
  question remains open at this sample size (2/2 valid for both) and no
  single-variant pin was introduced.

Alternatives considered: pinning the model inside formal task profiles
(rejected: violates the D-025 profile invariant); sending an explicit
`required_model` requirement from the client (rejected: provider/model
identity belongs to router configuration, not the task boundary); adding
OpenAI-equivalent effort aliasing (out of scope; no evidence).

### D-039 — Execution-eligibility contract and pre-routing provider exclusion

- **Status:** Accepted
- **Date:** 2026-09-16
- **Issue:** BioMedical-IT/scarcity-router#82
- **Related:** Infrastructure/creatidy-autonomy#15 (M4.1 owner policy
  change), Infrastructure/creatidy-onprem#16, D-019, D-018

M4.1 changes the owner's OpenAI policy: subscription-included allowance is
allowed for unattended execution; purchased-credit usage and API PAYG are
forbidden. First-party evidence re-verified 2026-09-16 (help.openai.com
articles 12642688 and 11369540): included usage is consumed first and usage
then draws from any credit balance, with no documented opt-out for draw-down;
auto-reload is a UI setting; a mid-task continuation can cross the
allowance/credit boundary and balances can go negative. Consequently the
D-019 "validated-but-unrepresented" outcome is no longer sufficient for
execution gating: a `credits.hasCredits=true` account still produces a
healthy `status="ok"` snapshot, so the selector would route to a billing
state the owner forbids, and callers would have to reject after selection
instead of routing to the next safe provider.

Decision:

1. New provider-generic `ExecutionEligibility` contract (own
   `schema_version = 1`, `scarcity_router/eligibility.py`), separate from
   CapacitySnapshot v3 and never collapsed with it. Closed state vocabulary:
   `eligible`, `allowance_unavailable`, `policy_blocked`, `unknown`. Closed
   reason-code vocabulary covering the forbidden credit state
   (`purchased_credits_present`), unestablishable mandatory account fields
   (`credits_state_unknown`, `ordinary_usage_unknown`,
   `spend_control_state_unknown`), allowance blockers/exhaustion
   (`ordinary_usage_not_allowed`, `spend_control_reached`,
   `rate_limit_reached`, `individual_limit_exhausted`, `upsell_present`,
   `included_window_exhausted`), and unreadable telemetry
   (`telemetry_unavailable`, `telemetry_auth_required`,
   `telemetry_unsupported`, `telemetry_invalid`). Unknown == unsafe: missing
   mandatory fields are never read as safe.
2. The Codex collector parses eligibility and the capacity snapshot from the
   SAME decoded `account/rateLimits/read` result in one app-server session
   (`OpenAICodexObservation`); expected operational failures pair a safe
   failure snapshot with a fail-closed `unknown` report. Auth mode is not
   classified here (the surface carries no auth-mode member); auth
   verification belongs to the execution side's own pre-call guard.
3. The selector gains a first-class `execution` exclusion stage, ordered
   first in `EXCLUSION_STAGES` (a provider whose subscription execution path
   may not start at all is furthest from runnable), with per-state primary
   reason codes `execution_policy_blocked`, `execution_allowance_unavailable`,
   `execution_unverified`. `select_model` takes `eligibility_reports`
   (at most one per provider); absence of a report for a provider means the
   stage never applies to it — never "eligible". Excluded candidates carry
   the paired report (`execution_eligibility`) as structured explanation.
4. `/v1/status` and `scarcity_status` expose the reports as an additive
   envelope field `eligibility` (present only when reports exist), which is
   an additive backwards-compatible machine-interface v1 domain change;
   CLI `status --json` keeps its released snapshot-array shape. Excluded
   candidates in `/v1/select` decisions carry the same data.
5. Discovery gains an explicit, validated `binary_path` override (env hook
   `SCARCITY_ROUTER_CODEX_BIN` at the application's default collector) for
   production's pinned standalone Codex install; VS Code extension discovery
   remains the default. A misconfigured path degrades to the same safe
   `unavailable` snapshot + `unknown` report, never an error surface.
   Shared membership/strictness helpers of the Codex parser were made
   module-public (`membership_valid`, `KNOWN_*_MEMBERS`) for the sibling
   parser; parser semantics are unchanged.

Alternatives considered: capacity-schema v4 with eligibility fields
(rejected: mixes capacity observation with execution/billing policy and
forces a major migration of a frozen contract for a provider-asymmetric
concept); caller-side gating on raw snapshots (rejected: the credits state is
not representable in v3 and the fallback-after-selection shape is exactly
what M4.1 forbids); a configurable per-provider eligibility policy
(rejected: fail-closed structural gating is not a user preference).

### D-040 — Two-mode product and the optional execution gateway

- **Status:** Accepted (owner-approved product decision, 2026-09-19)
- **Date:** 2026-09-19
- **Issue:** BioMedical-IT/scarcity-router#85 (program map A0)
- **Supersedes:** D-001 (in part — see D-001's supersession note), D-017
- **Confidence:** High for the product boundary; module-level design carries
  normal implementation risk delegated to M01–M10 (#86–#95).
- **Decision:** Scarcity Router remains one independent Apache-2.0 OSS
  repository and gains an **optional execution gateway** alongside the
  existing **recommendation-only mode**:
  1. **Recommendation-only mode is the default standalone mode and is
     unchanged.** The CLI, loopback REST v1 and stdio MCP surfaces keep their
     frozen contracts, security boundaries and semantics. Installation and
     operation without the server component, a worker or Docker remain exactly
     what exists today (M10 guards this).
  2. **The optional execution gateway may receive prompts and execute/proxy
     model traffic** when explicitly deployed and authorized. This is the
     deliberate partial supersession of D-001. The gateway serves one
     OpenAI-compatible endpoint so that any OpenAI-SDK client (one
     `base_url`, one client API key, one model/profile identifier) can have
     requests served from the best available authorized resource — API
     providers, Ollama/local inference, or approved local Codex/ZCode
     adapters — under the existing least-scarce-capable discipline.
  3. **Ollama and local inference return as execution resources**
     (server-direct over HTTP when network-accessible, worker-bridged when
     localhost-only), superseding D-017's blanket removal. The D-017
     operational-instability rationale remains a design input for isolation,
     health handling and honest unknown states.
  4. **Not authorized by this decision:** autonomous coding/agent frameworks,
     arbitrary command execution on hosts, issue-to-PR orchestration,
     repository management, generic agent workflow frameworks, a general task
     scheduler, `scarcity run <task>`-style interfaces, reset-credit
     redemption or any benefit-consuming action (those remain information
     unless a separate explicit decision authorizes acting on them).
     Repositories and client-side tools remain controlled by the client; SSH
     is a way a user may reach a machine, not a router orchestration
     protocol. An execution mode with undefined or unbounded start time must
     not silently replace a synchronous HTTP request.
  5. **The goal is efficient use of heterogeneous AI access** —
     subscriptions, eligible promotions, metered APIs, prepaid APIs and
     local models/local GPU. The same model name must never be assumed to
     mean the same resource, entitlement, quota pool, cost model or
     promotional eligibility (D-042).
  6. Every existing invariant that remains true — security invariants,
     provider-edge discipline, quota-never-changes-capability, licensing —
     is unchanged. AGENTS.md, `docs/product.md`, `docs/security.md` and the
     README are rewritten to the two-mode boundary; history is preserved in
     this log.
- **Reason:** The owner's real workflow now includes serving OpenAI-compatible
  clients from heterogeneous subscription, API and local capacity. Proxying
  under explicit authorization with least-scarce-capable routing delivers
  that value without sacrificing the safe recommendation-only default for
  every existing user.
- **Alternatives considered:** a separate gateway product/repository
  (rejected: duplicates the routing core and the collector set, guarantees
  drift); silently extending machine-interface v1 with execution endpoints
  (rejected: breaks the frozen D-028 boundary and its clients); building the
  agent/orchestrator features clients sometimes ask for (rejected: explicit
  non-goal, scope creep with security exposure).
- **Boundary:** Product decision and documentation only (A0). No gateway,
  worker, adapter, UI or packaging implementation is authorized by this entry;
  implementation is delegated to M01–M10 under the A0 architecture (D-041
  through D-045).

### D-041 — Execution-gateway module architecture, server/worker responsibilities and durable state

- **Status:** Accepted
- **Date:** 2026-09-19
- **Issue:** BioMedical-IT/scarcity-router#85 (A0); module issues #86–#95
- **Confidence:** Medium-high; module boundaries are frozen, internal module
  design belongs to each module issue.
- **Decision:** The execution gateway is specified as **logical module
  boundaries inside the existing Python package, not microservices**. The
  authoritative module map, responsibility boundaries, per-module input/output
  contracts and the existing-file-to-module mapping are
  [`docs/architecture.md`](architecture.md) (execution-gateway section). This
  decision freezes:
  1. **Responsibility separation.** Resource State/Registry/Collectors own
     observation (what exists, whether it is reachable, freshness, cost,
     pools — never routing). The Routing Core owns the pure deterministic
     decision (which executable target — never I/O, never provider parsing).
     The Execution Coordinator owns one execution's lifecycle (admission,
     concurrency reservation, dispatch, streaming, cancellation, usage
     accounting — never routing decisions, never provider parsing; providers
     are reached only through execution adapters). No module crosses another's
     boundary; the routing core stays import-clean of providers, network and
     subprocess exactly as today.
  2. **One server component, one native worker component.** Exactly one
     server process serves the OpenAI-compatible execution surface, the
     control API and the lightweight web UI (M09), and terminates the worker
     protocol. Exactly one native worker (Windows/Linux/WSL, M05) bridges
     localhost-only resources to the server through an outbound TLS/WSS
     connection. Module boundaries introduce **no** additional containers,
     databases or deployed services.
  3. **Server vs worker split.** The server holds server-side credentials
     (administrator configuration, provider API keys, client API keys, worker
     identities), aggregates state, makes routing decisions, coordinates
     execution and writes audit records. The worker holds only its per-device
     pairing identity and local allowlists; provider application credentials
     stay local to the worker host whenever possible (Codex auth remains
     provider-managed per D-018's boundary); the worker performs no routing
     decisions and enforces its local adapter allowlist even against server
     requests.
  4. **Durable state is minimal.** The server keeps one embedded durable
     store (SQLite-class single-file store inside its data directory) holding
     configuration state, identities, usage accounting and the bounded audit
     trail; no external database, cache or message-queue service is
     introduced. The store's schema is server-internal, not a public
     serialized contract; migrations are explicit and tested. In-memory
     operation remains valid for slices that need no durable state; the
     exact schema lands with the module that first needs it (M03/M09) under
     this requirement. Worker keeps only its identity file and local
     configuration.
  5. **U-003 assignment.** The deferred refresh/staleness policy is resolved
     by M01 (#86), scoped to the server's state store and its bounded
     polling/cache with explicit freshness semantics; the synchronous
     one-shot collection behavior of the local recommendation-only surfaces
     is unchanged.
  6. **Language and structure preserved.** The current implementation
     language, core code and package structure are kept; new gateway/worker
     modules sit beside the existing application layer. No new directory tree
     is imposed without evidence.
- **Reason:** The owner-approved extension must reuse the accepted routing
  core and collector discipline rather than fork them; logical boundaries
  inside one package keep a single deployable recommendation-only artifact
  while making each module independently implementable and reviewable.
- **Alternatives considered:** microservice decomposition per boundary
  (rejected: no demonstrated need, multiplies deployment and security
  surface); extending the existing collectors with routing knowledge
  (rejected: violates D-003/D-002 separation that has kept provider drift
  contained); a second selector for gateway requests (rejected: D-042 forbids
  a second scoring system).
- **Boundary:** Architecture and module contracts only. No runtime behavior
  is implemented or changed by this decision.

### D-042 — Route-decision contract, authorization precedence and the entitlement/quota-pool model

- **Status:** Accepted
- **Date:** 2026-09-19
- **Issue:** BioMedical-IT/scarcity-router#85 (A0); implementation in M02 (#87)
  with M01 (#86) inputs
- **Confidence:** High for precedence and model semantics; field-level
  serialization is M02's to define under the A0 contract map.
- **Decision:** The routing core gains a **route-decision contract** for
  executable targets, defined in [`docs/architecture.md`](architecture.md)
  and reflected in [`docs/selection-policy.md`](selection-policy.md). This
  decision freezes:
  1. **Executable target, not model name.** A route decision identifies a
     concrete executable target that separates: the physical
     model/variant (`ModelIdentity`); the execution channel/surface
     (server-direct HTTP adapter, worker-bridged adapter, local CLI/app
     adapter); the entitlement in use (subscription-included, promotional,
     PAYG metered, prepaid credits, local/ungated); the quota pool(s) the
     entitlement draws from (confirmed shared pools referenced explicitly);
     and the client routing profile under which the decision was made. The
     same model name never implies any of these.
  2. **Entitlement and quota-pool semantics** (detailed in
     [`docs/capacity-model.md`](capacity-model.md)): every quota fact carries
     an observation class — `direct_observation`, `provider_telemetry`,
     `estimate`, `local_limit` or `unknown`; tokens reported by an adapter
     are never equated with subscription quota percentages; resources
     sharing one confirmed quota pool are never counted as independent
     capacity, and unconfirmed sharing is never assumed in either direction
     (the same subscription discovered through Desktop, CLI, Windows or WSL
     is not multiple pools, nor is one pool assumed without verification).
     A routing preference based on a promotion is distinct from proof that a
     specific execution qualifies for it (D-039 gating remains the proof
     path). Scarcity Router never assumes it observes all account usage
     happening outside the router.
  3. **Authorization precedence (frozen).**
     `administrator constraints > client authorization > request
     requirements > configured routing profile > explicitly selected
     target/model > optimization preferences`.
     Each layer may only narrow the space allowed by the layers above it; a
     client override may narrow permissions and must never expand
     authorization, provider access or spending limits. If the caller
     explicitly requests a concrete model/effort/target, it is pinned and
     **never silently replaced**: when a pinned target violates a stronger
     layer (unauthorized, incompatible or blocked), the request fails with
     an explicit error rather than being re-routed.
  4. **Profiles/aliases are bindings, not a second scoring system.**
     Administrator-defined profile aliases may occupy the `model` field of
     OpenAI-compatible clients, but each alias resolves to the existing
     task/profile requirement model (`model-policy.json` profiles and
     `TaskRequirement`). There is no second simplified scoring system and no
     mandatory LLM request classifier; capability constraints inferred from
     request structure (tools present, structured output requested) are
     compatibility requirements, not rankings.
  5. **Recommendation-to-execution binding.** A gateway-era route decision
     carries a `decision_id` and an executable-target reference for its
     selected candidate. A client that first used MCP `select` (or REST/CLI
     select) may pin that target reference in a subsequent gateway execution
     request; the gateway then runs **admission only** (authorization,
     limits, availability, compatibility) and never re-runs competitive
     ranking, so no unexpected second routing decision occurs. Frozen
     alongside: **a recommendation is not automatically a reservation, a
     capacity guarantee or an execution guarantee**; no capacity is reserved
     between select and execution unless a future explicit versioned
     decision adds reservations. D-022's bounded compound recommendation
     remains a contract, not an executor.
  6. **Existing semantics preserved.** Identical inputs and evaluation time
     still produce identical decisions (D-027/D-032/D-037); quota state
     never raises capability ratings; `/v1/select` and `simulate`
     semantics are preserved, and any extension flows through the additive
     machine-interface v1 rules (D-028).
- **Reason:** OpenAI-compatible clients supply almost no requirement
  information; correctness therefore depends on binding their three fields
  to the authoritative requirement/policy model and on separating the five
  target dimensions so quota, authorization and compatibility mistakes
  cannot hide inside a model string.
- **Alternatives considered:** routing on bare model names with
  provider-level entitlement lookup (rejected: the same name across
  channels/pools is exactly the failure mode the program exists to prevent);
  an LLM-based request classifier deriving requirements from prompt content
  (rejected: nondeterministic, prompt-inspecting, unnecessary); treating a
  recommendation as a reservation (rejected: no capacity guarantee can be
  made without provider-side reservations that do not exist).
- **Boundary:** Contract semantics only. No selector, policy or interface
  code changes here; M02 implements under this contract and M01 supplies the
  state inputs.

### D-043 — Execution contracts: ingress request, compatibility matrix, lifecycle, worker protocol and audit metadata

- **Status:** Accepted
- **Date:** 2026-09-19
- **Issue:** BioMedical-IT/scarcity-router#85 (A0); implementation in M03 (#88),
  M04 (#89), M05 (#90), with matrix evidence from M06 (#91)/M07 (#92)
- **Confidence:** Medium-high; wire-level formats are module-owned, the
  semantic rules below are frozen.
- **Decision:** The execution surface's contracts are defined in
  [`docs/architecture.md`](architecture.md) and versioned per D-045. This
  decision freezes their semantic rules:
  1. **Request contract.** The minimum ingress is `GET /v1/models` and
     `POST /v1/chat/completions` with SSE streaming. Requests are validated
     for capability before any inference: unsupported capabilities are
     rejected before inference or routed only to a backend that actually
     supports and is authorized for them. The Responses API is a later
     explicit sub-scope with a documented supported subset; a fake
     `/v1/responses` that silently drops unsupported semantics is forbidden.
  2. **OpenAI compatibility matrix.** Compatibility is recorded per
     (adapter, adapter version, model/backend) across the dimensions:
     roles and conversation history, streaming, `tool_calls`, tool results,
     structured output, reasoning controls, context limits, error semantics,
     usage reporting and cancellation — each cell `PASS`, `PARTIAL`,
     `UNSUPPORTED` or `UNKNOWN` with dated evidence and tested version.
     `UNKNOWN` and `UNSUPPORTED` fail closed. An agentic CLI backend is not
     automatically an OpenAI-compatible backend; concatenating messages
     into a text prompt is not sufficient compatibility.
  3. **Execution lifecycle.** Admission → bounded concurrency reservation →
     dispatch → stream → completion/cancellation → usage accounting.
     Frozen rules: client disconnect/cancellation propagates to the selected
     backend where supported; the backend is never silently replaced after a
     response stream has started; retries after ambiguous execution state
     must not blindly duplicate inference consumption (exactly-once
     execution is not promised); one external request may cause multiple
     internal provider calls and usage/accounting represents this honestly.
     Client-supplied tools return to the CLIENT as `tool_calls`; the router
     never automatically executes client-provided tools as a local shell
     command, Codex MCP, ZCode MCP or any local tool.
  4. **Worker protocol.** One simple versioned message protocol over a
     single outbound TLS/WSS connection from worker to server: no inbound
     worker port, no manual worker-IP configuration, no routine certificate
     maintenance for ordinary users. Handshake performs protocol-version
     negotiation (incompatible versions fail safely), per-device
     authentication and heartbeat; message classes cover state reports,
     execute, stream chunks, cancellation and usage reports; reconnect is
     bounded with backoff and network loss must not automatically duplicate
     an already-started request. There is no generic `/shell`, `/ssh` or
     arbitrary-command message; the worker enforces its local adapter
     allowlist even if the server requests more.
  5. **Audit-metadata contract (minimal, frozen field set).** Each executed
     request records: request id, decision id, client/profile identity,
     routing-policy version, state snapshot identity/version, selected
     target, actually-executed target, adapter version, start/end time,
     result status, provider-reported usage, and estimated usage where
     applicable. The default audit trail contains **no prompt or response
     contents**; retention is bounded and administrator-configurable with a
     bounded non-zero default. Diagnostics remain redacted and allowlisted.
- **Reason:** These rules are the difference between an honest gateway and a
  silent-substitution proxy: capability truth per backend, explicit
  lifecycle limits, worker trust that never becomes remote code execution,
  and an audit trail that explains decisions without hoarding user content.
- **Alternatives considered:** optimistic capability normalization
  (rejected: fail-open compatibility is the classic silent-corruption
  failure of "OpenAI-compatible" backends); an inbound worker port with
  mTLS both ways (rejected: breaks NAT/VPN users, certificate maintenance);
  full prompt/response logging for debugging (rejected: privacy and
  retention burden; redacted diagnostics suffice).
- **Boundary:** Contract semantics only; M03/M04/M05 implement, M06/M07
  supply matrix evidence, M10 verifies end to end.

### D-044 — Security architecture of the execution-gateway server component

- **Status:** Accepted (explicit security decision required by D-009's
  extension rule and AGENTS.md for new network exposure, credential storage
  and write access)
- **Date:** 2026-09-19
- **Issue:** BioMedical-IT/scarcity-router#85 (A0); distributed to M03 (#88)
  ingress, M04 (#89) outbound provider HTTP, M05 (#90) worker transport,
  M06 (#91)/M07 (#92) local runtime integrations, M09 (#94) administration,
  M10 (#95) E2E verification
- **Extends:** D-009; boundary notes added to D-030/D-031
- **Confidence:** High for the boundary rules; mechanism details (exact key
  formats, TLS library) are module-owned.
- **Decision:** The execution-gateway **server component** adds authenticated
  network surfaces with their own security decision; every
  recommendation-only surface keeps today's boundary unchanged. The complete
  threat model is [`docs/security.md`](security.md) (execution-gateway
  section). Frozen rules:
  1. **Three identity classes with separate credentials and permissions:**
     administrator, inference client, worker. No shared default password; no
     bearer secrets in URLs; issuance and revocation exist for each class;
     client API keys authorize inference, never administration.
  2. **Verified TLS everywhere; no `verify=false`.** The server's non-
     loopback listeners require TLS with verified certificates. Workers
     connect outbound over TLS/WSS and verify the server; the server never
     needs to reach a worker inbound. Plain-HTTP localhost exceptions are
     allowed only as explicit bounded administrator-configured origins
     (e.g. a loopback or LAN Ollama endpoint), never for
     credential-bearing requests to non-local origins.
  3. **Pairing/trust bootstrap is simple and explicit:** the administrator
     initiates pairing in the server UI, receives a short-lived one-time
     pairing code, enters it (plus the server URL) on the worker, and the
     worker receives a per-device credential with rotation and revocation.
     No manual IP allowlists, no shared fleet secret, no certificate
     signing ceremony for ordinary users.
  4. **Server-side credential storage is bounded and explicit:** provider
     endpoints and credentials come only from administrator configuration;
     OS-native secure storage is preferred where available and a
     permissioned file store (`0o600`, never world-readable) is the
     recorded fallback. This is the sole, explicit exception to the
     recommendation-mode "credentials are transient input" rule, and it
     exists only inside the server component's store. Credentials are never
     logged, never exported, never sourced from client request content, and
     never sent to non-configured origins.
  5. **Limits enforced at admission before dispatch:** request-body size,
     context/output size, concurrency, execution time and spending limits —
     administrator-configurable, with safe defaults.
  6. **Router-loop protection:** the router's own endpoint (and another
     router instance's endpoint) must not silently serve as a provider
     backend; configuring the server's own execution origin as a provider
     is refused, and ingress identifies gateway-originated traffic so
     chained routers fail loudly rather than loop.
  7. **SSRF and redirect discipline:** provider origins are fixed
     administrator configuration (no client-supplied URLs); credentials are
     bound to configured origins; `Authorization` is never forwarded across
     unsafe/cross-origin redirects (reject rather than follow).
  8. **Local-adapter isolation (M06/M07):** session, filesystem and tool
     isolation; no automatic access to user projects, arbitrary paths,
     shell, global MCP configuration, plugins, browser integrations or
     unrelated conversation history; a read-only sandbox alone is not
     presumed sufficient. No root/Administrator execution by default; no
     Docker socket mounting; no arbitrary repository mounting; no
     uncontrolled client-supplied subprocess flags or environment
     variables. Provider-managed credentials stay provider-managed
     (D-018 boundary unchanged).
  9. **Logging and audit:** no prompt/response logging by default; no
     secret logging; redacted diagnostics; the minimal audit metadata of
     D-043 with bounded retention; logs created by local runtimes on
     worker hosts are accounted for by the same hygiene rules.
  10. **Provider terms and subscription scope:** provider subscription and
      promotional terms are respected (U-009); several apps owned by one
      user must not automatically imply the right to share one personal
      subscription with multiple independent users — the server is scoped
      to one user's own resources, not a resale or multi-tenant quota
      pool.
- **Reason:** The gateway moves the product onto the model-request path and
  onto the LAN; that exposure is acceptable only with separate identities,
  verified TLS, explicit trust bootstrap, bounded credential storage and
  admission limits designed before implementation, not retrofitted.
- **Alternatives considered:** unauthenticated LAN deployment with a shared
  token (rejected: no revocation, no identity separation, breaks the
  spending-limit guarantee); mTLS for every client (rejected: ordinary
  OpenAI SDK clients cannot do client certificates; API keys are the client
  convention); storing credentials in environment variables or world-
  readable config (rejected: violates the existing storage discipline).
- **Boundary:** Security architecture only. Implementation and verification
  are distributed to the module issues above; M10's security acceptance
  scenarios are program blockers.

### D-045 — Machine-interface coexistence and the versioned OpenAI-compatible execution surface

- **Status:** Accepted
- **Date:** 2026-09-19
- **Issue:** BioMedical-IT/scarcity-router#85 (A0); M03 (#88) implements,
  M08 (#93) guards
- **Amends:** D-028 (and the `docs/machine-interfaces.md` non-goals)
- **Confidence:** High.
- **Decision:** The existing machine-interface v1 and the new
  OpenAI-compatible execution surface are **separate contracts that never
  share a version**, and the frozen v1 surfaces are preserved:
  1. **Machine-interface v1 is untouched.** `GET /healthz`, `GET /v1/status`,
     `POST /v1/select`, `POST /v1/simulate` on the loopback REST adapter,
     the stdio MCP tools and the CLI keep their frozen semantics, paths,
     envelopes and loopback/unauthenticated boundary (D-028/D-030/D-031).
     Additive evolution continues only under the D-028
     backwards-compatibility rules.
  2. **The OpenAI-compatible execution surface is a new, separately
     versioned contract** ("execution surface v1"): `GET /v1/models` and
     `POST /v1/chat/completions` (plus SSE), served by the authenticated
     server component. Its `/v1/` prefix is the OpenAI client convention
     and is **not** machine-interface v1; the two path sets are disjoint
     (`/healthz`, `/v1/status`, `/v1/select`, `/v1/simulate` vs
     `/v1/models`, `/v1/chat/completions`), so one listener may serve both
     in server deployments without ambiguity, but the contracts, versions,
     error vocabularies and security boundaries remain separate documents.
     Execution-surface errors follow OpenAI-compatible client conventions;
     they never reuse or extend the closed `invalid_request`/
     `internal_error` machine-interface vocabulary.
  3. **The loopback REST v1 adapter is never the execution server.** The
     execution server is a distinct component with its own listener
     defaults, TLS and authentication (D-044); it is never produced by
     relaxing the frozen adapter's binding or endpoint set. In server
     deployments, machine-interface-style control operations (status/
     select/simulate equivalents and administration) are exposed through
     the server's authenticated control API with equivalent semantics
     under the M08 parity rules — same core, explicitly versioned.
  4. **The parity requirement extends, not forks.** The M08 guardrail suite
     keeps `direct == CLI == REST == MCP` green throughout the program and
     extends it to cover gateway-era additive fields; server-mode control
     responses remain semantically equal to local ones for equivalent
     state/policy. The optional remote bridge (M08) is explicit
     configuration with explicit failure — a configured remote server that
     fails is surfaced as an error, never silently degraded to local state.
  5. **Versioning discipline.** Execution-surface v1 evolves additively
     within its version; any incompatible change requires a new major
     version and an explicit migration decision, exactly like
     machine-interface v1. The worker protocol carries its own independent
     protocol version with negotiation (D-043).
- **Reason:** Existing users and the M08 parity suite depend on frozen v1
  semantics; OpenAI-compatible clients depend on standard OpenAI paths.
  Naming the two contracts separately and keeping their path sets disjoint
  satisfies both without compatibility serializers or silent semantic
  mixing.
- **Alternatives considered:** extending machine-interface v1 with
  `/v1/chat/completions` (rejected: an execution endpoint inside an
  unauthenticated loopback contract is a security contradiction and a
  semantic category error); versioning the execution surface as
  machine-interface v2 (rejected: implies a migration relationship that
  does not exist — the contracts serve different client populations);
  requiring a separate port or hostname for the execution surface
  (deferred to deployment choice, not contract: the contract separation is
  what is frozen here).
- **Boundary:** Contract-coexistence rules only; no endpoint is implemented
  by this decision.

### D-046 — CI and release-engineering foundation: development CI on Forgejo, public releases from stable main

- **Status:** Accepted
- **Date:** 2026-09-19
- **Issue:** BioMedical-IT/scarcity-router#97; consumed by M10 (#95), precedes
  M01–M09 implementation
- **Confidence:** High.
- **Decision:** CI and public release engineering are governed by one
  authority split and one trust boundary, detailed in
  [`docs/release-engineering.md`](release-engineering.md):
  1. **Forgejo is the only development-CI authority.** Forgejo Actions runs
     workflow `ci`, job `check` (the stable required-status-check name for
     `develop` branch protection) on every PR to `develop` and every push to
     `develop`. It executes the documented validation gate plus
     `make package-check` — the repository-authoritative build, inspection
     and isolated-install smoke — and keeps no separate test list. GitHub
     hosts exactly one workflow, the tag-driven public release pipeline; no
     duplicate development CI exists there.
  2. **The CI trust model is frozen:** pull-request code is untrusted; PR
     jobs run with `contents: read` only, receive no provider or publishing
     secrets, and get no privileged runner access (no Docker socket, host
     mounts, privileged containers or broad private-network reachability).
     Publishing never runs from pull-request events, and release authority
     is separate from development CI. Deterministic tests never require
     secrets.
  3. **Public releases originate only from stable `main` through deliberate
     human SemVer tags `vX.Y.Z`.** The release workflow fails closed unless
     the tag is valid SemVer, the package version equals the tag version
     (single source literal `__version__` in `scarcity_router/__init__.py`),
     the tagged commit is reachable from `main`, the artifacts are built from
     that exact commit in the same run, and the artifacts pass
     `make package-check` before publication. Versions are never bumped
     automatically and `develop` is never promoted automatically.
  4. **Publication uses short-lived identities only:** GitHub artifact
     attestations (Sigstore, OIDC) and PyPI Trusted Publishing; no long-lived
     PyPI token may be introduced. Every release carries `SHA256SUMS`.
     External configuration that cannot live in Git (PyPI trusted publisher,
     the protected `pypi` environment, the Forgejo runner boundary, and
     `develop` branch protection) is recorded as explicit owner actions,
     never claimed as configured.
  5. **Future artifact contracts are recorded, not faked:** the GHCR server
     image (`ghcr.io/creatidy/scarcity-router`; tags `X.Y.Z`, `X.Y`,
     `latest` = latest stable only; amd64 + arm64 when justified) and the
     Windows worker packages (`scarcity-worker-X.Y.Z-windows-x64.msix`/
     `.zip`; Windows runners; MSIX preferred subject to evidence; code
     signing for release quality) have frozen contracts and insertion points
     in the release workflow, but no artifacts, no placeholder binaries and
     no simulated publish jobs until a real supported artifact exists.
  6. **Linux/WSL Python installation after the first PyPI publication** is
     `uv tool install scarcity-router` with `pipx` as the conventional
     alternative; no custom apt/RPM/pacman repositories; a Homebrew tap is
     deferred; Python distribution stays distinct from native Linux-worker
     packaging.
- **Reason:** Untrusted development input must never equal release authority,
  and the release path must fail closed against every condition it cannot
  verify. Landing the foundation before #86–#95 implementation keeps CI/CD
  architecture out of the module issues, and the honest implemented/future
  artifact split continues the U-008/U-009 discipline against documented
  fiction.
- **Alternatives considered:** GitHub as a second development-CI authority
  (rejected: duplicate runs, configuration drift, wider attack surface);
  automatic releases from `develop` or automatic version bumps (rejected:
  `main` must stay stable and human-controlled); a long-lived PyPI API token
  (rejected: Trusted Publishing/OIDC is available); custom Linux package
  repositories (rejected: maintenance burden without a use case); bespoke
  artifact signing (rejected: attestations plus checksums satisfy the
  provenance need without new infrastructure).
- **Boundary:** Infrastructure and contracts only. No release is performed by
  the foundation; no runtime Scarcity Router behavior changes. The only
  tooling change is making `tools/package_check.py` read its expected version
  from the authoritative source literal so the same smoke check validates
  release builds of any version.

### D-047 — M07 closeout: no ZCode execution adapter; Stage 2 NO-GO

- **Status:** Accepted (owner product decision)
- **Date:** 2026-09-20
- **Issue:** BioMedical-IT/scarcity-router#92 (M07); resolves U-013
- **Confidence:** High.
- **Decision:** M07 Stage 1 is complete — the dated feasibility evidence is
  [`docs/zcode-adapter-stage1-evidence.md`](zcode-adapter-stage1-evidence.md)
  (sources retrieved 2026-09-19; local runtime 3.14.0; no live inference
  probes) — and, after reviewing that evidence, the owner decided **NO-GO /
  cancelled for M07 Stage 2**: no ZCode execution adapter is planned.
  1. **No supported programmatic surface.** Stage 1 found no official,
     supported ZCode CLI, public execution API, SDK, headless execution
     surface or other stable programmatic trigger suitable for Scarcity
     Router (evidence doc sections 2, 8).
  2. **Undocumented internals are explicitly not an acceptable
     implementation path.** Integrating through the runtime's internal
     IPC/private mechanisms (the `desktop-attached-remote` Unix-socket path)
     or through unofficial wrappers is out of bounds for this Apache-2.0 OSS
     project; reverse engineering the IPC would also violate vendor terms.
  3. **Vendor terms.** Proxy-style automation of the ZCode runtime is not
     sufficiently supported by the vendor terms without explicit written
     authorization (terms effective 2026-06-15, re-verified 2026-09-19;
     evidence doc section 5).
  4. **Reopen condition.** M07 may be reconsidered only when ZCode ships an
     official supported programmatic interface (CLI, API, SDK or headless
     automation surface) — or another official supported integration surface
     becomes available — AND the applicable vendor terms permit the intended
     use. There is no repository-side polling or monitoring code; this
     trigger is evaluated from external vendor observation, and any reopening
     goes through a new superseding decision.
  5. **ZCode Desktop is not Z.ai API access.** This decision concerns only
     the ZCode desktop application as an execution backend. Z.ai Coding Plan
     execution remains part of the generic supported HTTP-provider path owned
     by M04 (#89) through the vendor-documented OpenAI-compatible endpoint —
     distinct resource and entitlement per D-042, terms suitability under
     U-009. The M07 NO-GO does not remove Z.ai HTTP/API support.
  6. **History preserved.** The Stage-1 evidence document, U-013's question
     list and answer provenance, and all prior decisions remain unchanged
     historical records; no milestone is renumbered.
- **Reason:** The Stage-1 bottom line was a research recommendation; this
  entry records the owner's subsequent product decision, which is
  authoritative. Wrapping an undocumented desktop runtime would couple a
  public repository to an unstable internal mechanism the vendor neither
  supports nor licenses for this use, while the supported
  subscription-backed Z.ai execution channel already exists through the M04
  generic adapter path.
- **Alternatives considered:** an experimental-only ZCode track under
  written vendor consent, full D-044 containment, exact version pinning and
  honest accounting (not taken: it still requires vendor authorization the
  owner has not obtained, and the evidence bottom line recommends against
  it); continuing through the unofficial `zcode-cli` wrapper (rejected:
  terms exposure, no redistribution rights, silent version drift); leaving
  #92 open (rejected: it would misrepresent cancelled Stage 2 as active
  implementation work).
- **Boundary:** Program closeout only — documentation, decision records and
  Forgejo issue state. No runtime behavior, no adapter code, no contract
  change; recommendation-only and execution-gateway contracts are untouched.

### D-048 — M09 implementation choices: control/UI stack, durable store format and administrator authentication

- **Status:** Accepted (implementation-level choices delegated by
  D-013/D-041/D-044; issue BioMedical-IT/scarcity-router#94)
- **Date:** 2026-09-20
- **Issue:** BioMedical-IT/scarcity-router#94 (M09)
- **Confidence:** High for the store and auth mechanisms; the minimal-UI
  decision is a deliberate D-013 non-choice, recorded with the evidence
  the issue required.
- **Decision:** M09 lands the server component's administration surface
  with these concrete choices (full contract:
  [`docs/control-surface.md`](control-surface.md)):
  1. **One server process, injected control plane.** The M03 execution
     server accepts an optional control-plane attachment
     (`GatewayControlSurface` protocol) and dispatches the control paths
     to it; the composition entry point is
     `python -m scarcity_router.control_server`. No second deployed
     administration service exists, and without an attached control plane
     the server behaves exactly as M03 defined it.
  2. **Web UI stack: none.** Server-rendered standard-library HTML with
     one inline stylesheet; no frontend framework, no client-side build,
     no JavaScript requirement, zero new runtime dependencies. Evidence
     (the D-013 justification the issue required): every administrator
     flow is one form plus one table over an authenticated JSON core;
     interactive richness (drag-drop, live charts, optimistic editing)
     appears in no flow; a framework would add a build toolchain, a
     shipping bundle and a CSP/script surface to a security-sensitive
     admin boundary for no demonstrated need. The stdlib `http.server`
     already serves the M03 surface, so the marginal cost of
     server-rendered pages is a few pure functions.
  3. **Durable store: stdlib `sqlite3` single file** (D-041's
     SQLite-class embedded store, no new dependency), one connection
     guarded by a lock, `synchronous=FULL` transactional writes,
     explicit `schema_migrations` table with ordered one-transaction
     migrations and fail-closed refusal of future versions, directory
     `0o700` / file `0o600` permissioned storage (the recorded D-044
     fallback; OS-native storage remains the preferred alternative where
     a deployment provides it). Administrator passwords are PBKDF2
     (HMAC-SHA256, per-instance salt) verifiers; session tokens, CSRF
     tokens, client keys, worker tokens and pairing codes are stored
     only as SHA-256 hashes with constant-time comparison; provider
     credentials are the sole plaintext values and live in one dedicated
     table read only by the dispatch seam — never by export, listing or
     diagnostics.
  4. **Single source of truth.** The store's configuration document is
     the only authoritative copy of administrator configuration; the
     secret-free export is a projection of that row. The M03-style
     client-keys FILE remains a valid headless input for the M03-only
     entry point, and `--import-client-keys` performs a one-time,
     never-overwriting migration of its hashes into the store.
  5. **Diagnostics are shared and offline.** One module produces the
     report for both the `/control/diagnostics` endpoint and the new
     additive `scarcity-router doctor` CLI command (realizing the
     deferred D-016 doctor concept); it reads stored state only — never
     a collector, adapter dispatch or inference request — reports each
     resource through the closed detected/authenticated/
     protocol-compatible/available/eligible/promotion-confirmed ladder
     with per-stage remediation, and redacts by construction.
- **Reason:** The issue's goal is install-and-operate without
  understanding internal topology or hand-editing files, under A0's
  security architecture; the smallest mechanism that satisfies each
  domain was chosen so the audit surface (one SQL file, no framework,
  no JS) stays reviewable by one person.
- **Alternatives considered:** a JavaScript frontend framework or
  htmX-style layer (rejected: no demonstrated need, D-013; adds a build
  and script surface to the admin boundary); JSON/flat-file
  configuration instead of SQLite (rejected: no transactional
  crash-safety, no bounded audit retention, concurrent admin/session
  writes need locking anyway); OS keyring as the primary credential
  store (deferred: correct D-044 preference where available, but no
  portable stdlib access exists; the permissioned file is the recorded
  fallback and the retrieval seam is injectable); admin bearer tokens in
  URL query for CLI convenience (rejected outright: D-044 forbids bearer
  secrets in URLs).
- **Boundary:** Implementation choices for issue #94 only. No selector,
  routing, provider or frozen-interface change; M10 owns packaging,
  installers and end-to-end acceptance.

### D-049 — Wave integration: ONE pairing system (M05 mechanics) and configuration-composed execution adapters

- **Status:** Accepted (M04/M05/M09 integration wave,
  `program/m04-m05-m09-parallel`; resolves the cross-workstream conflict
  the parallel merges created)
- **Date:** 2026-09-20
- **Issue:** the M04 x M05 x M09 integration wave (issues #89/#90/#94)
- **Confidence:** High — the conflict was structural (two independently
  built pairing systems), the reconciled design keeps every frozen
  contract intact, and the migration is explicit and tested.
- **Conflict:** M09 shipped an administration-facing worker-pairing
  surface (its own `worker_pairings` tables, pairing-code issuance AND
  an HTTP redemption endpoint) built on the assumption M05's transport
  would consume it later; M05, unable to see M09, built its own complete
  pairing system (code redemption inside the verified-TLS protocol
  handshake, `WorkerIdentityStore` + `WorkerAdminService` +
  `WorkerEndpoint`). The wave forbids two pairing systems; a choice was
  forced.
- **Decision:**
  1. **M05's mechanics are the sole pairing system.** M09's
     `/control/workers` endpoints and the workers UI page delegate to
     `WorkerAdminService`/`WorkerEndpoint` (issue, list, revoke,
     rotate). Redemption is ONLY the protocol handshake — the HTTP
     `/control/worker-pairing/redeem` endpoint is removed; admin code
     ISSUANCE stays in the control API/UI. Liveness display comes from
     the endpoint's real session table and authentication stamps, not
     from a second bookkeeping table.
  2. **The superseded M09 `worker_pairings` table is DROPPED by store
     schema version 2** (explicit, tested migration). No data conversion
     exists or is meaningful: that table held only M09-format code/token
     HASHES whose redemption path is retired, and M05 hashes with a
     per-store pepper salt that cannot reproduce them; pending codes and
     revoked rows carry no convertible state. Every unrelated table
     (configuration, administrator identity, sessions, client keys,
     provider secrets, audit) is untouched.
  3. **ONE Ollama translation.** M05's provisional worker-side
     translation is replaced by an adaptation of the shared M04
     translation core on the `ollama` preset's evidenced policy
     (`worker_local_translation.OpenAICompatibleLoopbackTranslation`);
     the M05 `LoopbackTranslation` protocol remains as the test seam.
  4. **Execution adapters are composed only from M09 administrator
     configuration** (`server_composition.py`): the M04 HTTP adapter
     from provider endpoints plus store-held credentials (dispatch-only
     reader), the M05 worker-bridged adapter from resource→worker
     bindings plus the declared worker-local adapter id; validation
     fail-closed (preset resolvable, origin parseable, worker known);
     the default deployment composes nothing. The composed server runs
     the optional M05 worker-protocol listener (off by default, TLS
     beyond loopback) with heartbeat liveness reaping. Resource→worker
     ownership comes only from M09 administrator configuration.
     Authenticated state reports establish observations/liveness but
     never execution ownership.
- **Reason:** D-041 assigns worker identity to the server's durable
  state and D-044 defines the pairing bootstrap as a one-time code
  redeemed over verified TLS — M05 implemented exactly that contract,
  while M09's HTTP redemption reduced the trust bootstrap to a
  bearer-style HTTP POST. Keeping M05's mechanics preserves the
  security design; keeping M09's admin surface (issuance, listing,
  revocation, rotation) preserves the operability M09 delivered. The
  migration drops only data whose consuming protocol no longer exists.
- **Alternatives considered:** keeping both systems (rejected outright:
  two pairing systems violate the wave's one-system requirement and
  would allow pairing-code redemption over plain HTTP); embedding M05's
  tables inside the M09 `ServerStore` (rejected: M05's store is a
  reviewed, permissioned, peppered unit shared with the standalone
  endpoint entrypoint; merging would touch its reviewed hash discipline
  for no functional gain); one-time conversion of M09 pairing rows into
  M05 identities (rejected: impossible without storing or cracking
  hashes — M09 rows hold unsalted SHA-256 of tokens whose presentation
  path is retired; re-pairing is a one-form operation); making the M09
  store schema absorb a version-less table ignore (rejected: fail-closed
  migration discipline requires an explicit version bump with a tested
  migration).
- **Boundary:** Integration of the three merged workstreams only. No
  selector, routing-core or coordinator change; frozen surfaces
  (`server.py`, `machine_api.py`, `mcp.py`) byte-identical; `cli.py`
  additive doctor behavior only re-pointed at the surviving pairing
  store; no M10 packaging work.

### D-050 — Server-direct availability observations: readiness probing wired to the M01 refresh contract

- **Status:** Accepted (v0.1.0 release-readiness program,
  `release/v0.1.0-readiness`; fixes the first-run execution blocker the
  program's audit found)
- **Date:** 2026-09-21
- **Issue:** release-readiness audit of `develop` @ `9d6192e` (Phase 4
  first-run acceptance; follow-through of the M01 (#86)/M03 (#88)
  availability design)
- **Confidence:** High — the composed server shipped with no production
  path that ever observes a `server_direct_http` resource, so every
  such resource was permanently `resource_never_observed` and every
  pinned execution failed `503 target_unavailable`; the M10 e2e masked
  this by injecting healthy observations through the test seam. The fix
  uses only seams and contracts that already existed (the M01
  `refresh_due` request-loop contract, the M04 adapter's quota-free
  readiness probe, `apply_resource_observation`) and is pinned by new
  no-injection e2e tests.
- **Decision:**
  1. **The request loop observes due server-direct resources.** Per the
     M01 contract ("there is no background refresh; the server's
     request loop calls `refresh_due`"), the composed server's execution
     ingress calls the control plane's `prepare_execution_admission`
     before admission: every enabled, bound, composed
     `server_direct_http` resource whose polling cadence is due is
     probed once through the M04 adapter's quota-free readiness probe,
     and the outcome is recorded through `apply_resource_observation`.
     No background poller thread exists.
  2. **Default polling cadence for server-direct registrations.** A
     registration without an explicit `poll_interval_seconds` gets
     300 s at the configuration boundary (server-side policy is
     authoritative per M01); an explicit administrator value always
     wins. Worker-reported resources are unaffected (the M05 transport
     observes them).
  3. **Reachability probing on the DOCUMENTED endpoint path.** Presets
     with an evidenced native health endpoint (Ollama: `GET /api/version`)
     keep probing it. Presets without one are probed with a GET on
     their documented chat endpoint path — no undocumented provider
     endpoint is invented and no inference quota is consumed. Any
     well-formed HTTP answer records health `ok` with the exact status
     in the probe note (availability is the fact being observed;
     capability, quota and usage facts are untouched); `401/403` record
     `auth_required`; `404/410` record `schema_changed` (the configured
     origin does not serve the documented path); transport/TLS/timeout
     failures record `unavailable`. Nothing observed (no binding, no
     composed adapter) leaves the resource honestly unobserved.
  4. **The connection test records its probe result** as the resource's
     observation, so the diagnostics remediation ("run the connection
     test to probe now") is a real action, and the acceptance ladder
     reflects what the probe saw.
  5. **Z.ai Coding Plan preset endpoint path corrected** to the
     documented base URL path (`/api/coding/paas/v4/chat/completions`):
     administrators configure a bare origin, so the preset path must
     carry the documented base path — the rule the OpenRouter preset
     already followed. The evidence reference (docs.z.ai devpack
     quick-start, retrieved 2026-09-20) is unchanged and now actually
     honored end to end.
- **Reason:** A release-blocking first-run defect: the documented
  onboarding flow (configure provider → configure resource → issue
  client key → execute) dead-ended in a permanent `503
  target_unavailable` with a remediation ("wait for its collection
  cadence or trigger a health check") that no action could satisfy.
  Recording observed reachability is honest telemetry — it fabricates
  no capacity and changes no capability rating — and it completes the
  designed-but-unwired M01 refresh seam instead of weakening the
  fail-closed availability gate.
- **Alternatives considered:** synthesizing "healthy" observations from
  configuration alone (rejected: fabricates health telemetry the server
  never observed — violates the fail-closed collector discipline);
  dropping the availability gate for pinned execution (rejected: a
  routing-semantics change forbidden at this stage and it hides real
  outages); probing provider-documented `GET /models` endpoints on API
  presets (rejected for now: adds provider-edge evidence obligations
  without unblocking the preset that has no documented models endpoint
  — the bare-endpoint GET achieves the same reachability fact uniformly);
  deriving observations from real executions (rejected: circular — the
  first execution is exactly what the never-observed gate blocks).
- **Boundary:** Availability-observation plumbing only. No routing-core,
  coordinator, contract-version or serialized-contract change; the
  frozen recommendation-only surfaces are untouched; worker-bridged
  observation (M05 reports) untouched; compatibility-matrix cells and
  their evidence untouched.

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

- **Status:** Resolved (2026-09-19, issue #86 / M01, under D-041's
  assignment); the synchronous M1 decision below is unchanged
- **Decision (M1, 2026-09-05, unchanged):** Every `status` invocation performs a fresh sequential collection
  and establishes one canonical UTC millisecond `retrieved_at` immediately for
  that observation attempt. The same value is passed to OpenAI and Z.ai;
  provider observations are never independently timestamped.
- **Resolution (2026-09-19, issue #86):** The deferred retained-state and
  staleness remainder is implemented as explicit, bounded per-resource
  freshness in `scarcity_router/resource_state.py`, scoped to the server's
  in-memory resource registry (D-041):
  1. **Administrator policy is authoritative.** Every resource's
     administrator registration carries a required positive
     `freshness_ttl_seconds` and an optional `poll_interval_seconds`; that
     registration policy is the server's freshness/polling authority.
     Observation documents (including worker reports) carry only
     `observed_at` — no policy fields — so an accepted observation can
     never enlarge its own freshness window or change its polling cadence.
     The registry evaluates every observation against its registration's
     TTL, and configured capabilities/cost are registration-owned the same
     way: one canonical value per resource, no silent observation
     override.
  2. `classify_freshness` evaluates each observation against an explicit,
     injectable instant: fresh while its age is at most the registration
     TTL, stale strictly beyond. A future-dated observation is rejected at
     application time and fails closed at evaluation instead of being
     treated as fresh; producing server-comparable observation times (and
     any clock-skew tolerance protocol) is the reporting side's
     responsibility (M05). Registry reads expose exactly `fresh`, `stale`
     or `never_observed` per resource; staleness never rewrites an
     observation's own health status, and stale or unknown state is never
     read as usable, zero or full.
  3. Polling is bounded and pull-driven: a resource with a configured
     `poll_interval_seconds` is `refresh_due` when it has never been
     observed or its last observation is at least that old; the server's
     request loop (M03) decides when to act on it. No background threads,
     timers or daemons exist at this boundary.
  4. The registry is deliberately in-memory; the durable SQLite-class
     server store remains D-041/M03/M09 scope and no external cache,
     database or message-queue service is introduced.
  Alternatives considered: a background refresh daemon (rejected: hidden
  concurrency and nondeterministic behavior in a first version; the
  pull-driven contract gives M03 the same effect explicitly); one global
  fixed TTL (rejected: freshness policy is per-resource administrator
  configuration, and a global default would hide real per-surface
  differences such as local Ollama health versus provider telemetry);
  retention of freshness/polling policy on observation documents (rejected
  in review: it let a worker-reported observation redefine how long its
  own state is treated as fresh — policy belongs to the administrator
  registration alone); retaining snapshots durably now (rejected: no
  evidenced need before the M03/M09 server store exists, and persisted
  state would need its own explicit migration story).
- **Boundary:** This resolution is scoped to the server's resource-state
  registry. The recommendation-only surfaces keep their synchronous
  fresh-collection behavior (no cache, no TTL) exactly as the M1 decision
  above defines it.
- **Residual:** Concrete TTL/polling defaults for real deployments are
  administrator-configuration territory (M09) and should be validated
  against observed collector latency/reliability when the server ships
  (M03/M10).

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
- **Status:** RESOLVED by M2c / D-025 (2026-09-06). The four-model catalog
  (`model-catalog.json`) and the eight calibrated task profiles
  (`model-policy.json`, `calibrated_requirement`) are populated with
  provenance, confidence and rationale, and are pinned by calibration
  acceptance tests (`tests/test_model_calibration.py`) and documented in
  `docs/model-calibration.md`. Future rating changes follow D-025's update
  governance.

### U-007 — Scarcity parameters and policy boundaries

- Validate `(1-r)^2`, label thresholds, reservation boundary behavior and
  unknown ordering through scenario tests before M2 acceptance.
- Reset proximity is preserved but not included in the first formula.
- **Status:** RESOLVED by M2d / D-026 (2026-09-06); aggregation amended by
  D-037 (2026-09-13). The penalty function
  (`penalty_units = (100 - remaining_percent)^2` on integer scale 10000),
  the exact label boundaries, the most-restrictive multi-window/multi-scope
  aggregation, the reservation comparison semantics (strict `<` threshold,
  minimum task level, scope-targeted rules) and the unknown-capacity policy
  boundary (no numeric unknown penalty; `degraded`/`strict` modes) are now
  frozen and scenario-tested in `tests/test_scarcity.py` and
  `tests/test_resource_policy.py`. Profile minima were already resolved
  separately by D-025 (resolving U-006). D-037 narrows the aggregation to
  most-restrictive *within* each window role and blends the roles.

### U-008 — Package, CLI and final project name

- Decide only after a collision search and before publishing an installable M1.
- **Status:** Narrowly resolved for M3 by D-028 (2026-09-06):
  `scarcity_router` is the stable Python module/package identity, and
  REST/MCP development entry points remain module-based until packaging
  proves necessary. The final branded package/executable name remains
  unresolved until the release-time collision search.
- **Status update (2026-09-09, D-034):** resolved for local deployment —
  the distribution is `scarcity-router` `0.1.0` with the three console
  scripts. Open only for the public-index collision search and any
  publication naming.

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

### U-012 — Codex execution-adapter uncertainties (registered by A0)

- **Status:** Stage 1 evidence recorded (2026-09-19, M06 #91); per-bullet
  statuses below. Stage 2 implementation waits for A0 (#85) and M05 and must
  re-verify official documentation with date and tested version at
  implementation time, per the issue's own evidence rule.
- **Date:** 2026-09-19
- **Decision:** A0 deliberately does not guess the following; each is answered
  only by dated, versioned evidence in M06 Stage 1:
  - runtime discovery across Desktop-only, CLI and VS Code installations, and
    Windows versus WSL profile separation;
  - authentication prerequisites for unattended execution (the D-018 boundary
    — Scarcity Router never touches tokens — is not in question; whether the
    official app-server/CLI flows permit the execution path is);
  - model selection and reasoning/effort selection through the app-server;
  - quota scope of executed work relative to the D-039 eligibility contract;
  - conversation roles/history support, streaming, cancellation, tool-call
    behavior, structured output and usage reporting (compatibility-matrix
    cells for M03);
  - which app-server protocol fields are stable enough to depend on
    (generation-aware parsing per D-019/U-011 remains the containment).
- **Stage 1 answers (2026-09-19; full evidence, sources and confidence in
  `docs/codex-adapter-stage1-evidence.md`):**
  - *Runtime discovery / Windows vs WSL profiles* — **answered (narrowed)**.
    U-001's VS Code extension layout re-confirmed on a current installation
    (extension `26.908.40401`, `codex-cli 0.154.0-alpha.6.2`); official CLI
    install channels documented (installer, npm, brew); Windows-native vs
    WSL2 are separate profiles with separate default CODEX_HOME stores and
    distinct sandbox mechanisms (elevated/unelevated vs bubblewrap; WSL1
    unsupported since 0.115). Narrowed residual: driving the Desktop app's
    bundled binary externally is undocumented (UNKNOWN); PATH/CLI discovery
    is still unimplemented in the collector (U-001 residual (b);
    `SCARCITY_ROUTER_CODEX_BIN` covers pinned installs).
  - *Authentication prerequisites for unattended execution* — **answered**.
    Official flows: ChatGPT-managed (browser + device-code) with Codex-side
    automatic refresh and cached-credential reuse; API-key auth is
    usage-billed PAYG (policy-forbidden for execution under D-039/M4.1);
    enterprise Codex access tokens are Business/Enterprise only and are
    delivered via stdin (`codex login --with-access-token`);
    `chatgptAuthTokens` (host-supplied tokens) is experimental and excluded
    by D-018 (the router would become a token holder); the official
    `auth.json` copy fallback is excluded by issue policy. D-018's
    `account/read {"refreshToken": true}` is the documented managed-refresh
    mechanism; `account/read`'s `account.type`/`requiresOpenaiAuth` is the
    official pre-call auth-mode signal for the execution-side guard.
  - *Model and reasoning/effort selection* — **answered**. `model/list`
    (per-model `supportedReasoningEfforts`, `defaultReasoningEffort`),
    `thread/start {model}`, per-turn `turn/start {model, effort, summary}`;
    live handshake + `model/list` probe on `0.154.0-alpha.6.2`.
  - *Quota scope of executed work* — **answered**. ChatGPT-managed auth
    draws the ChatGPT plan quota (documented `account/rateLimits/read`
    envelope matches the U-010/U-011/D-019 validated mapping;
    `account/usage/read` requires ChatGPT-backed auth); API-key auth is
    PAYG. Quota-scope evidence only; promotional eligibility is never
    inferred and D-039 gating is unchanged.
  - *Roles/history, streaming, cancellation, tool calls, structured output,
    usage reporting* — **answered as draft matrix cells** (M03 input), with
    honest tested-version limits: handshake/model-list probed live; all
    turn-level cells are official-documentation evidence against the
    `rust-v0.155.1` schemas and stay subject to Stage 2 re-verification.
    Headlines: streaming PASS (stdio JSONL; WebSocket transport
    experimental/unsupported — excluded), cancellation PASS
    (`turn/interrupt`), structured output PASS (`turn/start.outputSchema`),
    tool results PASS (`toolOutput`), usage reporting PASS
    (`thread/tokenUsage/updated`, `account/usage/read`), roles/history
    PARTIAL (Responses-API item mapping; `baseInstructions`/
    `developerInstructions`), tool_calls PARTIAL only via the experimental
    dynamic-tools gate — UNSUPPORTED on the stable surface, so Stage 2 must
    choose explicitly.
  - *Stable vs experimental protocol fields* — **narrowed**. The app-server
    protocol now has an official stability contract: experimental
    methods/fields are gated behind `capabilities.experimentalApi` and the
    server rejects them (`<descriptor> requires experimentalApi
    capability`; machinery verified in `codex-rs/app-server-protocol/src/
    experimental_api.rs` at `rust-v0.155.1`); version-pinned
    `generate-ts`/`generate-json-schema` artifacts exist. The stable
    surface relevant to Stage 2 is enumerated in the evidence document.
    Residuals: the `codex app-server` subcommand still self-labels
    `[experimental]` in CLI help (0.154.0-alpha.6.2), the WebSocket
    transport is documented as experimental and unsupported, and
    generation-aware parsing per D-019/U-011 with fail-closed disable
    remains the containment.
- **Evidence needed:** official documentation
  (https://developers.openai.com/codex/app-server,
  https://developers.openai.com/codex/auth) re-verified with date and tested
  version at implementation time; local capability probes; only credentials
  authorized for this work.

### U-013 — ZCode execution feasibility uncertainties (registered by A0)

- **Status:** Resolved (2026-09-20) by the M07 Stage-1 evidence plus owner
  decision D-047; Stage 2 is NO-GO/cancelled (#92 closed). The question list
  and Stage-1 answers below are retained as provenance.
- **Date:** 2026-09-19
- **Decision:** A0 deliberately does not guess the following; each is answered
  only by dated evidence in M07 Stage 1:
  - whether an official, stable, headless ZCode execution path exists at all
    (unofficial wrappers are research evidence only, never proof of API
    support or redistribution rights);
  - vendor terms for subscription/idle-time usage through a third-party
    router (re-verify https://zcode.z.ai/en/terms with date);
  - runtime discovery, authentication, output format, cancellation, tool
    behavior, permissions and version stability;
  - actual usage/quota accounting, kept separate from promotional-eligibility
    questions (execution success never proves promotional eligibility);
  - isolation of session/filesystem/tools from unrelated conversation history
    and global plugins.
- **Evidence needed:** vendor documentation
  (https://zcode.z.ai/en/docs/welcome, /en/docs/idle-time-tasks, /en/docs/hooks,
  /en/terms, https://docs.z.ai/devpack/overview) re-verified with dates;
  prefer official documentation and local capability probes; no credentials
  beyond those authorized for this work.
- **Stage 1 evidence (M07, 2026-09-19):** recorded in
  [`docs/zcode-adapter-stage1-evidence.md`](zcode-adapter-stage1-evidence.md)
  (all sources retrieved 2026-09-19; local runtime 3.14.0; no live
  inference probes). Per-bullet status:
  - *official stable headless path* — **answered: NO.** The product is a
    desktop Electron ADE; install docs and the full sidebar document no
    CLI/headless/SDK surface; every unattended channel (Automations,
    idle-time tasks, Bot Channel, Remote Control/Development) is UI- or
    chat-driven with no external trigger API. The local
    `zcode-cli`/`zcode-server.cjs` runtime executes agents internally over
    undocumented Unix-socket IPC in `desktop-attached-remote` mode — an
    internal mechanism, not a supported interface (evidence doc sections
    2.4–2.6).
  - *vendor terms for subscription/idle-time use through a third-party
    router* — **narrowed, still open on consent.** Terms effective
    2026-06-15 re-verified 2026-09-19: account exclusivity (III.3), no
    lending/renting (III.4), and the prohibition on using ZCode as an
    "unauthorized proxy server" (IV.3) leave the gateway use case
    unresolved without explicit vendor consent; no redistribution right in
    the bundled runtime exists (evidence doc section 5).
  - *runtime discovery, authentication, output format, cancellation, tool
    behavior, permissions, version stability* — **answered for Stage 1.**
    Discovery signals and desktop-attached authentication documented;
    output format/cancellation UNKNOWN (no surface); `tool_calls`
    UNSUPPORTED (client tools must never execute locally, D-043); four
    permission modes mapped with auto-connect MCP and auto-enable plugin
    trust behavior; version churn high (eight releases 2026-08-20 through
    2026-09-19) — any future integration must pin 3.14.0 exactly and fail
    closed on drift (evidence doc sections 2, 3).
  - *usage/quota accounting vs promotional eligibility* — **answered as
    separated.** Account-level 5-hour/weekly/MCP pools are observable and
    already collected by the existing `zai_usage_endpoint` collector;
    per-session local records exist; idle-time runs are vendor-documented
    as free and non-consuming. Promotional eligibility (off-peak rates,
    reset cards, time-boxed promos) stays UNKNOWN for router-executed work
    per D-039 (evidence doc sections 2.12, 4).
  - *isolation of session/filesystem/tools from unrelated history and
    global plugins* — **answered: NOT met by the product.** Sessions
    inherit history; project MCP auto-connects without approval; plugins
    auto-enable with code-execution trust; D-044 isolation is achievable
    only via full external containment (dedicated OS identity, dedicated
    `HOME`, sanitized workspace) (evidence doc section 6).
  - **Bottom line (M07 Stage 1, 2026-09-19): NO-GO for a supported
    adapter; Stage 2 not recommended to start.** Experimental-only remains
    possible under written vendor consent, full D-044 containment, exact
    version pinning with fail-closed disable, honest account-level
    accounting, and no runtime redistribution. The supported
    subscription-backed execution channel is the GLM Coding Plan
    OpenAI/Anthropic-compatible API through M04 (distinct resource per
    D-042; terms under U-009), not a ZCode runtime wrapper.
- **Resolution (2026-09-20, D-047):** The owner accepted the Stage-1 bottom
  line as a product decision: Stage 1 is complete and Stage 2 is cancelled;
  no ZCode execution adapter is planned and #92 is closed as completed
  research. Reopening requires an official supported ZCode programmatic
  interface (or another official supported integration surface) AND
  applicable vendor terms permitting the intended use, recorded through a
  new superseding decision; no repository-side monitoring exists. The
  bullets above are historical question/answer provenance and are unchanged.

### D-051 — Compact first-run setup dialog for the packaged Windows worker

- **Status:** Accepted (issue #113; remediates the v0.1.0 first-launch
  release blocker observed on Windows 11)
- **Date:** 2026-09-22
- **Issue:** #113 — the standalone Windows ZIP (`scarcity-worker.exe`)
  told users to run `scarcity-router-worker pair`, a Python console
  script the ZIP does not contain; `docs/m10-acceptance.md` already
  claimed the Windows package "asks for pairing information in its
  first-run dialog", which was untrue.
- **Confidence:** High — the implementation reuses every existing seam
  (`WorkerRuntime.pair`, `WorkerLocalStore`, `WorkerOrigin`,
  `build_local_adapter_registry`, the tray restart machinery), adds one
  small stdlib GUI surface, and is discriminatingly tested on every
  platform.
- **Decision:**
  1. **One compact dialog, two modes — not a configuration
     application.** The packaged executable's no-arg launch routes
     unpaired workers into a first-run setup dialog (server origin +
     one-time code + optional "Enable local Ollama") and paired workers
     into the tray; the tray gains a "Worker settings..." action that
     reopens the SAME dialog without the pairing section. The dialog is
     stdlib `tkinter`, owned by the packaging tree
     (`packaging/windows/scarcity_worker_setup_view.py`) exactly like
     the pystray tray view — the library keeps its dependency set and
     the GUI-independent core (`scarcity_router.worker_setup`) is
     unit-tested everywhere. No Electron/Qt/web stack, no wizard, no
     second process, no configuration server.
  2. **One pairing system.** The dialog and the packaged `pair` CLI
     command both drive `worker_setup.pair_worker`, a thin orchestration
     over the existing `WorkerRuntime.pair` (protocol handshake +
     `WorkerLocalStore` persistence); the Python console script keeps
     driving `WorkerRuntime.pair` directly. The packaged `pair` command
     attaches the parent console best-effort so output is visible from
     PowerShell/cmd despite the windowed build. The pairing code is
     never persisted, logged or echoed in failure text.
  3. **Typed, versioned, non-secret local settings in the EXISTING
     store.** The settings document (`WorkerLocalSettings`: optional
     control-UI origin + optional loopback Ollama
     resource/host/port) persists as one strictly parsed JSON value in
     `WorkerLocalStore` (`local_settings` key; schema version 1; exact
     key set; bounded size; atomic SQLite write). Malformed documents
     fail closed into a recoverable settings state — never a guess,
     never a second pairing code. Precedence: explicit CLI `run` flags
     that select an adapter replace the stored selection for that
     process; otherwise the stored settings drive the registry, which
     is rebuilt through the existing `build_local_adapter_registry` on
     every (re)connect so a settings save takes effect via the tray's
     existing restart machinery.
  4. **The allowlist stays local (D-044).** The GUI can only describe a
     loopback Ollama endpoint; `worker_setup` refuses non-loopback hosts
     before anything is paired or persisted, and `LoopbackOllamaAdapter`
     re-validates at its own construction. The server can never expand
     the allowlist remotely; server-side setup (adding the
     `worker_bridged` resource) remains server-side.
  5. **Honest platform surface.** Windows-native Codex execution stays
     `platform_not_evidenced`: the dialog exposes no Codex section (its
     source contains no codex token, asserted by test), the settings
     schema rejects unknown fields, and the evidenced Linux/WSL Codex
     path remains CLI/config-driven and fail-closed. TLS discipline is
     unchanged (verified certificates; certificate-trust problems are
     actionable errors, never bypassed).
- **Reason:** The standalone product must be usable from a double-click
  with no Python, no repository and no manually created files, while
  worker-local adapter authority (D-044) requires a LOCAL configuration
  surface. The smallest professional mechanism that satisfies both is a
  single stdlib dialog over the existing stores; docs truth
  (m10-acceptance's first-run claim) is restored by making the claim
  true.
- **Alternatives considered:** Electron/Qt/web-UI configuration app
  (rejected: a new application and dependency surface for one dialog,
  contrary to the owner's explicit constraint); pystray-native dialogs
  (rejected: pystray has no form/dialog capability; would still need a
  GUI toolkit); a sidecar JSON file next to the store (rejected:
  `WorkerLocalStore.save_value` already gives atomic, bounded,
  validated single-row persistence — a second file adds a second
  durability story without migration need); exposing Windows Codex in
  the dialog behind a warning (rejected: advertising an unevidenced
  execution surface, however caveated, invites use before evidence);
  auto-deriving the control-UI origin only from ports (kept as the
  documented fallback — https://HOST:8787 — with the stored setting and
  the `--server-ui-url` flag as the explicit overrides).
- **Boundary:** The packaged Windows worker's first-run experience
  (issue #113) only. No selector, routing, protocol, frozen-interface
  or server-side change; the M05 worker protocol and the four console
  scripts are untouched.

### D-052 — Windows UI ownership model: one owner per event loop, async crossings

- **Status:** Accepted (issue #113; remediates the live Windows defect
  found during the PR #114 acceptance retest)
- **Date:** 2026-09-24
- **Issue:** #113 — on a live Windows 11 host, opening the tray's
  "Worker settings..." dialog left the settings window unresponsive,
  controls failing to transition, the window not reliably regaining
  foreground/focus, and the tray callback occupied: the dialog's
  tkinter mainloop ran synchronously inside the pystray menu callback,
  i.e. one toolkit's event loop was nested inside another toolkit's
  message loop. The tray's Win32 loop could not service ANY action
  (status, control UI, diagnostics, reconnect, quit) while the dialog
  was open, and Quit could not deterministically tear the process down.
- **Confidence:** High — the failure mechanism is the documented
  threading model of both toolkits (pystray invokes menu handlers on
  its message-loop thread; tkinter owns exactly the thread that runs
  its mainloop), and the replacement model is discriminatingly
  unit-tested on every platform (`tests/test_worker_first_run.py::
  TrayUiOwnershipTests`).
- **Decision:**
  1. **Every long-lived event loop has exactly one owner.** The
     Windows tray/Win32 message loop is owned by the packaging
     adapter's tray thread (pystray `Icon.run`); every tkinter dialog
     root is created, mainlooped and destroyed on ONE UI-owner thread
     driven by the library's `windows_tray.UiDispatcher`
     (`scarcity-router-ui`, started lazily, dialogs strictly serial);
     the worker runtime owns its session thread; the process main
     thread owns restart/quit coordination.
  2. **No toolkit event loop is ever run synchronously inside another
     toolkit's callback.** Tray menu handlers only marshal or signal:
     "Show worker status" and shell/browser/folder opens go to
     throwaway worker threads; "Worker settings..." posts the dialog
     task to the UI dispatcher and returns; "Reconnect / restart" and
     "Quit" set events. The first-run dialog runs on the UI-owner
     thread while the coordination thread waits on the result future —
     a plain sequential wait, not a nested loop.
  3. **One active settings window, enforced at post time.** A
     non-blocking settings slot is taken in the menu callback (tray
     thread) before posting; requests arriving while a dialog is
     active are dropped — a queued request would open a second window
     the moment the first closed.
  4. **Cross-component requests are asynchronous signals.** UI thread
     → session: the existing restart hook (threading events +
     cooperative runtime stop). UI thread → store: the store's own
     lock (atomic single-row writes) on the dialog's short-lived save
     worker. Tray → UI: dispatcher posts. Settings persistence keeps
     its single home in `WorkerLocalStore`; the session thread reads
     it only at (re)connect through the existing registry rebuild.
  5. **Deterministic teardown beats daemon-kill.** The dispatcher
     exposes a close signal; views implement the optional
     `arm_close_request` capability (the packaged tkinter view polls
     it via `after`) so Quit unwinds an open dialog, drains and joins
     the UI thread within a bounded timeout before `tray_main`
     returns. Quit with the dialog open discards the dialog (cancel
     semantics); in-flight store writes are atomic, so nothing is
     half-persisted.
- **Reason:** The pre-D-052 nesting was an accidental combination of
  toolkit callbacks: it blocked the tray message loop for the dialog's
  lifetime, broke Win32 focus/foreground behavior, made repeated menu
  actions race, and left process exit conditional on a user closing a
  window. The owner's acceptance requires the tray to remain a
  responsive control surface while the dialog is open.
- **Alternatives considered:** keep the nested dialog and only fix the
  observed field behavior (rejected: the unsafe event-loop ownership
  IS the defect; any local fix would leave the freeze/determinism
  hazards); run tkinter on the process main thread and pystray via
  `run_detached` (rejected: the two toolkits would still interleave on
  one thread and the tray loop would remain blocked by dialogs); a
  separate dialog process with IPC (rejected: a new process/IPC layer
  for one dialog, contrary to the packaging constraint); replacing
  pystray/tkinter with a single toolkit (rejected: a GUI migration is
  out of scope for the defect and would discard the D-051 compact
  dialog).
- **Boundary:** The packaged Windows worker's GUI threading only
  (issue #113). No selector, routing, protocol, frozen-interface,
  server-side or CLI change; the onboarding semantics, pairing path,
  store schema and persistence formats are untouched.

### D-053 — Dynamic execution sources: ExecutionSource, ModelInventory, track-floor adoption and multi-source workers

- **Status:** Accepted (program `program/dynamic-execution-sources`, umbrella
  issue #116; children #117–#123)
- **Date:** 2026-09-24
- **Base:** `develop` @ `98dd25a` (canonical Forgejo; the owner merged
  develop→main as PR #115 immediately before program start)
- **Confidence:** High for the contracts and invariants below; module-level
  mechanics (parser shapes, UI layouts) carry normal implementation risk and
  are covered by the program's discriminating tests.
- **Context:** Live controlled-Codex evidence (2026-09-24, `codex-cli
  0.155.0-alpha.16.3`): with explicit `{}` params the real runtime answers
  `account/read` (`account.type=chatgpt`) and `model/list` (advertising
  `gpt-6-luna`, `gpt-6-sol`, `gpt-6-astra`, `gpt-5.5`, `gpt-5.6-luna/sol/
  terra`, `gpt-daybreak-blue-latest`), while Scarcity Router's adapter omits
  the `params` member for argument-less methods and receives `JSON-RPC
  -32600` — the evidenced cause of `Codex snapshot health = unknown` on a
  logged-in home. Requiring a manual physical-model slug per resource
  (`gpt-5.6-sol`) ages badly: each provider generation would otherwise need
  manual reconfiguration.
- **Decision:**
  1. **ExecutionSource is a first-class administrator configuration.** One
     source = one configured, independently authenticated source of
     executable model capacity (`source_id`, `kind` — `codex_subscription`
     first, `label`, owning `worker_id`, `entitlement`, optional explicit
     `quota_pool_id`, adoption policy). The configuration document gains an
     additive `sources` domain (`CONFIG_SCHEMA_VERSION` 1 → 2; a v1 document
     remains valid and means zero sources). The user configures the SOURCE;
     the source discovers physical models; physical models remain the exact
     execution targets. The three concepts (source / physical model /
     derived resource) are never collapsed.
  2. **Codex request shapes are repaired method-specifically.** The adapter
     sends an explicit `params` object exactly where the evidenced runtime
     schema requires one — `account/read` → `{}`; `model/list` first page →
     `{}`; later pages → `{"cursor": "..."}` — and never globally forces
     `{}` onto every JSON-RPC method. Tests reject an absent `params`
     member for these methods with `-32600`. Reasoning-effort vocabulary is
     extended additively with `ultra` (runtime-reported on GPT-6 Sol/Astra);
     all existing vocabulary members are unchanged.
  3. **Discovery runs on the worker; inventory crosses the worker protocol
     as a typed, bounded document.** `WORKER_PROTOCOL_VERSION` 1 → 2 under
     the existing hello negotiation: at negotiated version 2 the state
     report carries an optional bounded `inventories` section (per source:
     `source_id`, `adapter_id`, `observed_at`, closed-vocabulary auth state,
     runtime name/version, per-model slug + runtime-reported reasoning
     efforts; bounded entries/pages; no credentials, no account metadata
     such as email or raw account ids, no raw provider payloads — only the
     normalized fields the router needs). A v1 peer pair behaves exactly as
     today (no inventories); a v2 worker against a v1 server negotiates 1
     and honestly reports nothing new. The runtime is authoritative for
     availability: no static duplication of currently available models or
     efforts.
  4. **Adapter KIND is separated from adapter INSTANCE.** The worker may
     run several instances of the `codex` adapter kind, one per enabled
     source: instance id `codex:<source_id>` (the legacy bare `codex` id
     remains the v1-compatible default instance). Each instance owns an
     isolated controlled CODEX home (`state_dir/codex-sources/<source_id>/`,
     `0o700`), its own auth probe, its own inventory and its own execution
     identity; no credential or home is ever shared between sources and no
     source can execute through another's home. The `LocalAdapterRegistry`
     duplicate-id rule is unchanged (ids differ by instance); the worker
     allowlist stays local authority (D-044/D-051). One native worker
     process serves any number of sources.
  5. **Tracks are stable capability families; adoption is conservative and
     floor-based.** A reviewed, versioned track registry
     (`model-tracks.json`) maps slug structure to tracks (`openai/luna`,
     `openai/sol`, `openai/astra`, restricted `daybreak`). A discovered
     model is DISCOVERED → CLASSIFIED (track known, naming structure safely
     parsed) → ROUTABLE only when: the track has an approved conservative
     capability FLOOR; the runtime reports the requested effort; the
     source/auth/health gates pass; and compatibility evidence permits the
     requested feature. Inheritance is ONLY the deliberately approved track
     floor — never a copy of an older generation's full ratings; stronger
     ratings arrive later through explicit, provenance-bearing catalog
     updates. Unclassified models stay discovered/not-routable; nothing is
     routed merely because the runtime lists it.
  6. **Derived exact resources.** The server materializes one exact,
     deterministic executable resource per adopted model per source
     (`resource_id = <source_id>:<slug>`, `worker_bridged`, exact slug and
     runtime-advertised efforts, entitlement from the source, quota pool =
     the source's pool — two independent accounts therefore default to two
     pools with unknown sharing, and the same physical model via two
     sources stays two targets, per D-042). Deterministic lifecycle: absent
     from an authenticated inventory → health `unavailable`
     (`model_absent_from_source`); absent from `RETIRE_AFTER_MISSES = 3`
     consecutive authenticated inventories → retired (deregistered; audit
     history and existing pins remain interpretable; reappearance
     re-materializes). No pinned request is ever rewritten. Derived
     registrations are in-memory derived state like every worker-reported
     observation (U-003/D-041): the source, not a second durable store, is
     their origin, and a restart re-derives them from the next inventory.
     D-049's "state reports never confer ownership" is reconciled
     explicitly: ownership remains administrator-granted at SOURCE
     granularity (the source names its worker); the report only supplies
     discovered inventory inside that grant, subject to adoption policy.
  7. **Routing and exact binding are untouched.** The routing core, its
     frozen gate order, pin/admission semantics (`admit_pinned_target`
     never re-ranks) and the D-043 audit field set are unchanged; selection
     consumes one catalog view whose derived entries carry the track floor
     with explicit `derived_from_track` provenance — no second scoring
     system. Discovery happens strictly BEFORE selection; execution remains
     exact (`source`, resource, provider, physical model, effort,
     entitlement, pool, worker, surface all fixed at dispatch; selected vs
     executed target stay equal in audit).
  8. **GPT-6 reconciliation.** `model-catalog.json` gains reviewed,
     provenance-bearing entries for `gpt-6-luna`, `gpt-6-sol`,
     `gpt-6-astra` (conservative ratings, dated 2026-09-24 evidence: the
     live controlled runtime inventory plus re-verified official
     documentation; this entry exercises D-033's evidence gate for Astra).
     GPT-5.6 entries remain while still available and useful; no mechanical
     rename. Default policy no longer requires obsolete GPT-5.6 manual
     configuration.
  9. **Daybreak Blue is a restricted track, not a routable family member.**
     `gpt-daybreak-blue-latest` classifies `security_review /
     restricted_access / defensive_security`: discovery never proves
     execution authorization; restricted models are visible in the source
     view but never materialized as routable resources and never silently
     substituted for any request. The durable security-review gate is a
     governance contract (Program Execution Mode): work classified
     `security_critical` requires an independent `gpt-daybreak-blue-latest`
     review; when access is unavailable the record states
     `SECURITY_REVIEW_UNAVAILABLE` and the gate stays open. Scarcity Router
     does not gain a multi-stage orchestration engine; the independent
     review lives outside the routing core, and this program's own gate is
     recorded in the program report.
  10. **UX is a mandatory review dimension with named fixes in this
      program:** friendly worker labels shown before opaque ids (ids stay
      available for audit); `worker authenticated` (pairing) distinguished
      from `provider/source authenticated` (provider login) everywhere the
      ladder is rendered; normal source setup never asks for a physical
      model slug (exact pins are an explicit ADVANCED mode); a read-only
      source view shows connected/auth state, detected models and
      routable/restricted/error counts without raw protocol structures;
      errors are actionable. Governance (AGENTS.md +
      `docs/llm-operating-policy.md`) makes every future review answer
      `UX impact: none` or assess the ten-point UX checklist, with a
      material UX regression being `CHANGES_REQUESTED`. Windows private-CA
      friction is recorded; TLS is not weakened and no certificate-ignore
      path is added.
  11. **Reliability envelope.** Discovery is bounded and deterministic per
      source (bounded timeout, pages — the existing `MAX_MODEL_PAGES`
      discipline —, inventory size and refresh cadence via the source's
      registration policy); a discovery failure isolates to its source
      (never the worker, other sources, the server or recommendation-only
      mode); failures back off with bounds, never loop unbounded, never
      hide a fallback.
- **Reason:** The product's recurring manual step — configuring exact
  physical slugs per resource — is the direct consequence of a
  resource-shaped configuration model. Naming the SOURCE and deriving
  exact resources from a typed runtime inventory removes the recurring
  work while strengthening, not weakening, the exact-binding discipline:
  every dispatch still names and executes one exact physical target, now
  guaranteed present in a fresh authenticated inventory.
- **Alternatives considered:** server-side discovery through a new
  provider API (rejected: the local runtime is the authoritative,
  already-authenticated availability surface, and server-side collection
  would duplicate provider credentials server-side against D-044's
  minimal-storage rule); auto-adopting "latest model always wins"
  (rejected: fabricates capability; violates quota-never-changes-
  capability's sibling principle that runtime listing never proves
  capability); writing derived resources into the administrator
  configuration document (rejected: the config document is
  administrator-owned; derived state would corrupt export/review
  semantics); a new inventory worker-protocol message family with its own
  ack (rejected: the state report already has the ack path and atomic
  application; version-2 fields are the smallest honest carrier); one
  worker process per source (rejected: multiplies pairing, transports and
  host footprint for what is an instance-addressing problem); collapsing
  source and resource into one entity with a wildcard model (rejected:
  destroys the exact-target contract D-042 freezes).
- **Reconciliation note (implementation, same review):** quota-pool
  membership is registration-owned policy, exactly like freshness/polling
  policy: the M01 observation check compares identities EXCLUDING
  `quota_pool_ids`, and the read model composes pools from the
  registration alone. A worker-reported observation therefore can never
  alter pool membership — including the default `pool-<source_id>`
  derivation and any administrator override — which is what makes the
  D-042 sharing rule enforceable on derived resources.
- **Boundary:** Program architecture decision for #116. Frozen v1 machine
  interfaces stay byte-compatible (additive `ultra` effort vocabulary
  only); the worker protocol moves to negotiated version 2 with v1 peers
  unaffected; D-049's ownership clause is amended only as stated in
  point 6, and the M01 identity-match note above narrows the
  observation/registration equality to the observation-relevant fields.

## Superseding a decision

Add a new numbered entry with its status, date, evidence and `Supersedes: D-nnn`.
Do not rewrite history or change an accepted decision silently.
