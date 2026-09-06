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

- **Status:** Accepted
- **Date:** 2026-09-07
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
    version and policy version are separate concepts, never collapsed.
    Within v1, additive optional fields require existing clients to remain
    valid; removals, renames or semantic changes require a new version or
    an explicit versioned migration. Existing domain serialization is
    reused, never forked. MCP tool names stay simple and unversioned;
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
- **Status:** RESOLVED by M2d / D-026 (2026-09-06). The penalty function
  (`penalty_units = (100 - remaining_percent)^2` on integer scale 10000),
  the exact label boundaries, the most-restrictive multi-window/multi-scope
  aggregation, the reservation comparison semantics (strict `<` threshold,
  minimum task level, scope-targeted rules) and the unknown-capacity policy
  boundary (no numeric unknown penalty; `degraded`/`strict` modes) are now
  frozen and scenario-tested in `tests/test_scarcity.py` and
  `tests/test_resource_policy.py`. Profile minima were already resolved
  separately by D-025 (resolving U-006).

### U-008 — Package, CLI and final project name

- Decide only after a collision search and before publishing an installable M1.
- **Status:** Narrowly resolved for M3 by D-028 (2026-09-07):
  `scarcity_router` is the stable Python module/package identity, and
  REST/MCP development entry points remain module-based until packaging
  proves necessary. The final branded package/executable name remains
  unresolved until the release-time collision search.

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
