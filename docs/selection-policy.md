# Selection and policy

## Objective

The default selector chooses the least scarce candidate that is capable enough
for the task. It does not maximize capability without regard to scarcity, and
it does not choose a weak model merely because it is abundant.

The portable descriptive class and profile policy is maintained in
[`model-policy.json`](../model-policy.json); this document remains authoritative
for the selector decision sequence and scarcity behavior.

## Inputs

- A task profile or explicit capability minima.
- Task level L0–L5.
- Hard constraints.
- Model catalog and its version/provenance, including the capacity scopes
  each model is bound to (its capacity applicability).
- Current normalized capacity snapshots and freshness.
- User policy mode, reservations and explicit overrides.
- Optional explicit provider/model availability schedules or blackout windows.
- Optional advisory provider/service health evidence.
- Optional replenishment metadata (`ReplenishmentState`) such as banked quota
  reset opportunities.
- Optional external benchmark/performance evidence used to curate the model
  catalog, never as live capacity.

## Deterministic decision sequence

1. **Resolve requirements.** Expand a profile in one place, merge permitted
   explicit requirements and validate contradictions.
2. **Filter hard constraints.** Remove candidates that cannot satisfy context,
    modality, tool, privacy, provider/model or runtime requirements.
   Explicit provider/model blackout schedules compile to hard temporary
   exclusions at this stage.
3. **Check capability sufficiency.** Remove candidates below any required
   dimension. Record each failed dimension; do not average a critical deficit
   away with strength elsewhere.
4. **Apply capacity eligibility.** Exclude explicitly unavailable or exhausted
   candidates. Evaluate only the capacity scopes the candidate is bound to;
   unknown model-to-scope applicability is explicit and policy-controlled,
   never guessed. Handle unknown/stale capacity under the active policy and
   mark degraded confidence. Advisory provider health can degrade or exclude a
   candidate only under an explicit policy; it does not rewrite capability or
   quota.
5. **Apply reservations and replenishment policy.** Below a configured capacity
   threshold, keep a model eligible only at or above the reservation's minimum
   task level. A banked reset or similar replenishment option can make a
   candidate *recoverable* under explicit policy, but is not treated as current
   remaining quota and is never consumed by the broker.
6. **Rank sufficient eligible candidates.** Under `balanced`, prefer lower
   scarcity penalty, then the smallest adequate capability margin, then stable
   configured preference and stable model identity as deterministic ties.
7. **Produce explanation.** Return the winner, alternatives, exclusions,
   capacity evidence, applied policy and reason codes, including schedule,
   health or replenishment reasons when relevant.

The “smallest adequate capability margin” tie-breaker avoids consuming frontier
capability when two options are equally scarce and both suffice. It must never
override a capability minimum.

## Scarcity aggregation boundary

Subscription scarcity is computed only over a candidate's applicable capacity
scopes and their windows, as defined in `capacity-model.md`. The planning
freeze fixes these invariants:

1. Only capacity scopes applicable to a candidate participate. An unrelated
   model-specific bucket must not penalize a candidate that does not consume
   it.
2. Within an applicable scope, all relevant windows participate.
3. Across all applicable windows and scopes, the most restrictive scarcity
   result governs under `balanced`. A healthy short window must never hide a
   critical weekly window.
4. Unknown applicability is explicit and policy-controlled; it is never
   resolved by choosing the most optimistic scope or window.

The initial candidate set contains supported subscription-backed models only.
Every candidate is evaluated against the relevant provider capacity windows;
there is no alternate API-cost or abundance score standing in for subscription
scarcity.

The exact penalty function and NORMAL/SCARCE/CRITICAL-style label thresholds
are deliberately not frozen here. They require scenario calibration in a
later PR (U-007); they are documented as open now to prevent an undocumented
scoring function from appearing in code.

## Reservations

A reservation rule conceptually states:

```yaml
reserve:
  openai/gpt-5.6-sol:
    window: weekly
    when_remaining_below: 20
    minimum_task_level: 4
```

Above the threshold the model competes normally. Below it, L0–L3 work does not
consume the reserved model; L4–L5 work may. Boundary comparison (`<` versus
`<=`) must be explicit in the final config schema and tested.

Reservations may target a provider, account, model or variant only where the
capacity relationship is known. A shared provider quota must not be treated as
independent per-model quota.

## Provider schedules and peak-hour policy

The user may define timezone-aware availability schedules or blackout windows at
provider/model scope. These are policy, not capability and not provider
telemetry.

The first concrete requirement is to support a personal rule equivalent to:

```yaml
provider_policy:
  zai:
    blackout:
      - schedule: <configured peak-hours expression>
        timezone: <explicit timezone>
        reason: preserve Z.ai for off-peak use
```

During a matching blackout, Z.ai candidates are excluded before capability
ranking even if quota is healthy. The explanation must say the provider is
policy-blocked, not unavailable or incapable.

Do not hard-code a vendor's current definition of peak/off-peak hours. Z.ai
documentation describes off-peak benefits and dynamic resource behavior, and
some off-peak/reset-card parameters are explicitly dynamic. The user's desired
schedule therefore belongs in configuration and must carry an explicit timezone
and deterministic boundary semantics.

## Replenishment and reset opportunities

Replenishment options are distinct from the currently active quota windows.
Examples include banked OpenAI Codex reset credits that can refresh usage limits
when deliberately redeemed.

The frozen M2 semantics are:

> Reset credits are replenishment opportunities, not current capacity.

They never enter current quota percentages, never pretend quota has been
restored and are never consumed by the broker.

The selector input is a separate concept, `ReplenishmentState`, carrying only
the minimum safe facts needed for selection:

- `provider`;
- `kind`;
- `available_count`;
- `details_known`;
- `earliest_expiry`, only when safely derivable and needed;
- `retrieved_at`.

The evidenced Codex shape (`availableCount` plus an optional `credits` list
whose detail rows may carry `id`, `resetType`, `status`, `grantedAt`,
`expiresAt`, `title` and `description`) stays at the provider edge. The
detail list may be absent or capped even when `availableCount` is larger, so
`details_known` is honest about coverage. Provider free-text titles and
descriptions are never exposed, and opaque credit IDs are not required for
selection unless a later use case proves they are needed.

The selector must not silently pretend a banked reset has already been applied.
Instead, policy may choose among explicit behaviors such as:

- ignore replenishment and judge only current capacity;
- report replenishment as advisory context;
- treat an otherwise sufficient candidate as `recoverable`, with an explicit
  human action required before use.

The initial personal workflow should be able to take available OpenAI resets
into account so that scarce-looking current windows do not hide substantial
recoverable capacity. The broker still does not call the reset-consume method;
redemption is an external/user action. When the supported Codex app-server
exposes reset count and expiry/details, preserve that provenance and freshness
without using private backend endpoints.

## Evidence and policy precedence

Selection inputs differ in authority. The frozen precedence is:

1. **Hard user policy.** An explicit configured user blackout is a hard policy
   exclusion. It wins for eligibility even when provider quota is healthy. It
   does not rewrite provider telemetry. The explanation reports
   `policy_blocked`, never `unavailable`.
2. **Direct capacity evidence.** A successful direct normalized account
   observation is authoritative for current quota. A public status page
   cannot rewrite its percentages.
3. **Provider-native runtime failure evidence.** A provider-native
   failure/high-traffic state tied to the actual supported access path may
   degrade or exclude a candidate according to explicit policy. It never
   changes model capability.
4. **Official public status.** Official service health is advisory. A green
   global page never fabricates quota, and a red global page does not
   automatically overwrite a successful direct account observation. At most
   it contributes advisory degraded confidence unless a later explicit policy
   says otherwise.
5. **External performance evidence.** Artificial Analysis and similar sources
   influence curated capability catalog assessment only. They never determine
   live quota, bypass blackout policy, override direct account telemetry or
   act as a live routing oracle.

## Advisory service health

Provider/service health is independent from model capability and account
quota; its authority is fixed by the precedence above.

Current planning notes:

- OpenAI's official status service exposes machine-readable global/component
  health and includes Codex-related components; treat it as advisory.
- `status.hellozai.com` is unrelated Zai Payments infrastructure and must not be
  used for Z.ai/GLM selection.
- no authoritative public Z.ai/GLM status page has been identified; use
  provider-native failure signals and explicit unknown state instead of
  inventing one.

## External capability and performance evidence

Artificial Analysis may be used as one provenance-bearing input when curating
the model catalog. Its useful data includes stable model/creator identifiers,
benchmark indices, pricing and observed performance metrics such as throughput
and latency.

The frozen M2 boundary:

```text
AA is offline/periodic catalog evidence
NOT runtime capacity
NOT automatic truth
NOT called during select()
```

A future cached AA snapshot must carry, at minimum: stable model ID, stable
creator ID, source/API version, retrieval time, metric identity/version,
snapshot provenance, attribution and the human-curated mapping into internal
capability evidence. API responses are not stored merely because they are
available; only fields used by an explicit catalog assessment are retained.
The API key remains server-side and outside repository files, fixtures,
output and agent prompts.

It must not become a hidden dynamic scoring oracle:

- cache/refresh it periodically rather than request it per selection;
- record data source, model identifier, metric/version and observation time;
- map only metrics whose meaning is understood to internal capability evidence;
- preserve human-curated ratings and observed workflow evidence as separately
  attributable inputs;
- never use its pricing or API-provider performance as a substitute for
  subscription capacity;
- keep the API key outside repository files, fixtures, output and agent prompts.

The selector remains deterministic over a frozen catalog/policy snapshot even
when external evidence helped create that snapshot.

## Policy modes

Initial candidate modes are:

- `balanced`: least scarce sufficient model;
- `quality-first`: prefer greater capability margin, still respecting hard
  constraints and explicit reservations;
- `conserve-openai` and `conserve-zai`: add a documented preference/penalty to
  protect the named provider.

The initial implementation should add only modes needed by real use. Direct
temporary preferences such as “Sol emergency-only” may be represented by the
same policy layer. Modes cannot fabricate capacity or bypass explicit privacy
constraints. Provider blackout schedules are orthogonal hard policy and must
not be weakened by a mode unless the user explicitly overrides them.

## Bounded compound workflow recommendations

Scarcity Router remains a recommendation service and does not execute model
calls. If a later selector recommends a compound workflow rather than one model,
the workflow itself becomes part of the resource decision and must be bounded by
construction.

A compound recommendation carries a conceptual `ExecutionBudget` with at
least:

```text
max_total_model_calls
max_legs
max_review_rounds
max_remediation_rounds
max_retries_per_leg
max_wall_clock_minutes
```

The frozen numeric domain is explicit per field:

```text
max_total_model_calls    integer >= 1
max_legs                 integer >= 1
max_review_rounds        integer >= 0
max_remediation_rounds   integer >= 0
max_retries_per_leg      integer >= 0
max_wall_clock_minutes   integer >= 1
```

Every value is finite and subject to an implementation-defined, documented
upper bound. There is no unlimited sentinel, no infinity, no
`null = unlimited` and no omitted field = unlimited; a compound
recommendation without a complete valid execution budget is invalid.

Budget maxima are permissions — hard upper bounds — not requirements.
Zero is a legal value meaning “this operation is not permitted by this
recommendation”; it is not unknown, missing, unlimited or invalid, and a
prohibited phase must never be encoded by inventing an artificial budget of
`1`. For example, `max_review_rounds = 1` permits a review round up to once;
it must not be exercised merely because zero were unrepresentable.

The initial workflow archetypes and their maximum structural expectations,
with illustrative budgets, are:

- `single`: one solver leg. When represented with an `ExecutionBudget`:
  `max_total_model_calls = 1`, `max_legs = 1`, `max_review_rounds = 0`,
  `max_remediation_rounds = 0`, `max_retries_per_leg = 0` and
  `max_wall_clock_minutes >= 1`. A plain single-model recommendation does
  not invent review, remediation or retry capacity.
- `cascade`: a first solver and at most one escalation leg. A bounded
  two-leg cascade may use `max_total_model_calls = 2`, `max_legs = 2`,
  `max_review_rounds = 0`, `max_remediation_rounds = 0` and
  `max_retries_per_leg = 0` or another explicitly allowed bounded value.
  The escalation leg is a leg, not a review round.
- `critique`: one solver, one independent critic, at most one remediation and
  at most one narrow final verification when explicitly recommended. Review
  and remediation maxima may be positive, for example
  `max_review_rounds = 1` and `max_remediation_rounds = 1`, but zero remains
  legal wherever the recommended plan omits that phase.

These are archetypes, not a requirement to copy any specific runtime. A
compound recommendation must never recommend “review and fix until clean”.

Expected model/resource consumption is aggregated over every planned leg.
When exact token consumption is unavailable, it is not invented: call counts
and applicable capacity-scope accounting remain explicit even when exact
token cost is unknown.

This mirrors repository multi-agent governance at the product boundary: the
service may recommend a bounded plan to an external orchestrator, while the
orchestrator is responsible for enforcement, accounting and execution.

## Unknown and no-solution behavior

Default ordering prefers a known healthy sufficient candidate over one with
unknown or stale capacity. Unknown is not silently equated to either zero or
full capacity.

If only unknown-capacity candidates meet the task, the broker may select the
best one with `degraded: true`, the exact unknown reason and an explicit warning.
A strict user policy may instead return no selection.

If no candidate is sufficient, return a structured no-solution result with:

- failed hard/capability requirements;
- closest candidates and their deficits;
- capacity/reservation/schedule/health exclusions;
- recoverable candidates requiring an explicit replenishment action;
- no automatic relaxation of requirements.

## Output contract

A decision must include:

- selected provider/model/variant or an explicit no-selection state;
- ordered alternatives;
- excluded/reserved candidates when useful for diagnosis;
- normalized reason codes plus readable reasons;
- task/profile requirements used;
- relevant capacity windows, timestamps and state;
- active policy and reservation/schedule rules;
- advisory health and replenishment state when used;
- catalog/policy versions and external-evidence provenance where relevant;
- degraded/unknown indicators;
- for a compound recommendation, the bounded execution envelope and aggregate
  expected resource accounting.

Reasons should be concise enough for an agent but complete enough for a human.
The public response never includes credentials or unredacted provider payloads.

## Simulation

Simulation applies typed overrides to a copy of normalized capacity or policy,
then runs the same selector. It must not mutate live observations or call a
model. Output distinguishes current and simulated inputs and decisions. This is
valuable for tests, policy debugging, demonstrations and documentation.

Simulation should eventually cover blackout windows, replenishment availability
and advisory health so policy behavior can be tested without waiting for real
peak hours, outages or quota exhaustion.

## Runtime feedback (deferred)

A future client may report quota exhaustion or temporary provider failure. Such
feedback may create short-lived runtime state until refresh, but is not required
for the first selector. It must never silently alter catalog capability.
