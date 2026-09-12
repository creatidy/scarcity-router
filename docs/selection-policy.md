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
- Optional provider/model happy-hour windows (quota-preference schedules,
  typically limited-time vendor campaigns).
- Optional advisory provider/service health evidence.
- Optional replenishment metadata (`ReplenishmentState`) such as banked quota
  reset opportunities.
- Optional external benchmark/performance evidence used to curate the model
  catalog, never as live capacity.

The task-requirement and model-catalog input contracts are the pure, validated
types in `scarcity_router/selection_types.py`. Scarcity assessment and the
resource-policy primitives (unknown modes, reservations, blackouts,
replenishment visibility and the policy container) are pure, deterministic
primitives in `scarcity_router/scarcity.py` and
`scarcity_router/policy.py`. The deterministic selector, explanation and
simulation are in `scarcity_router/selector.py` and
`scarcity_router/simulation.py`, composed by
`scarcity_router/selection_app.py` and the CLI.

## Balanced selector

The selector evaluates candidates in canonical `(provider, model, variant)`
order through the pipeline: blackout (one caller-supplied aware instant) →
hard constraints → capability sufficiency → scarcity and the
unknown-capacity policy → replenishment visibility → applicable
reservations → ranking. Later stages never run after a hard/capability
failure merely to populate fields.

**Exact `balanced` ranking order.** After all eligibility filters:

1. known nonzero capacity before unknown/degraded capacity — unknown has no
   numeric sentinel and is incomparable to any known percentage, even a
   critical one (a known 1% candidate ranks ahead of a degraded unknown);
2. happy-hour quota-preference group — a candidate preferred by an active
   happy-hour window ranks ahead of one that is not (D-035; within the same
   knowledge class only, never across it, and never against eligibility);
3. integer scarcity penalty (`penalty_units`) among known-capacity
   candidates only;
4. capability margin, lower wins;
5. lowest explicit catalog `reasoning_effort`, in normalized order
   `none < low < medium < high < xhigh < max`; null/absent uses an explicit
   unconfigured comparison state after known effort, not a numeric sentinel;
6. explicit `SelectorPolicy.preference_order` (listed before unlisted, then
   index) — a late tie-break only: it cannot override capability, hard
   constraints, blackout, happy-hour preference, capacity knowledge class,
   scarcity, capability margin or reservations, and it is never inferred
   from model classes, profile names, provider names, catalog order or
   display names;
7. stable `(provider, model, variant)` identity.

**Independent concepts.** Model capability is not reasoning effort and neither
is subscription scarcity. Effort is curated catalog configuration data, never
parsed from variant/display/model/provider names. It does not alter capacity
observations, scarcity penalties, scope matching or capability requirements.
All five OpenAI configurations share `openai/codex` telemetry, so their capacity
assessments are equal. Smaller effort cannot rescue an incapable configuration
or outrank better scarcity or a smaller capability surplus. No API-price or
effort-specific quota penalty is introduced.

**Capability margin.** Exactly
`SUM(effective_rating - required_minimum)` over the required dimensions
only. Unrequired dimensions do not participate; all contributions are
nonnegative because the candidate already passed sufficiency; a requirement
with no minima yields margin 0. No averaging, no weights, no task-level
bonus, no confidence multiplier and no benchmark score. Sufficiency and
margin use `CapabilityAssessment.effective_rating`, so a valid
`HumanOverride` participates without mutating catalog values. An unknown
rating on a required capability dimension fails (strict initial policy);
dimensions never compensate each other.

**Hard-constraint evaluation.** Tri-state hard properties are honest:
`None` fails a required feature as `unknown` and is never read as `False`;
an explicit `False` fails as `unsupported`; a `False` requirement is a
no-op, never "must not support". `privacy_constraint` is an exact required
privacy tag — unknown tags fail as `privacy_unknown`, a known tag set
without the required tag fails as `privacy_unsatisfied`; no hierarchy, no
cloud/local assumptions. The current catalog's privacy characteristics are
unfrozen, so a privacy constraint honestly yields no eligible model today.

**Requirement tightening.** A profile requirement may be tightened by a
full explicit `TaskRequirement` (`--tighten`) under monotone rules: a lower
task level, capability minimum or numeric hard minimum is a validation
error, never a silent `max()`; booleans combine with OR; identity/privacy
constraints must agree exactly. Tightening is never permitted with an
explicit requirement path.

**Reservations.** Only rules whose scope is one of the candidate's known
capacity bindings apply; unknown bindings are never guessed into a
reservation match. An applicable reservation whose trigger state cannot be
determined fails closed (`reservation_unknown`) — it is never silently
treated as untriggered.

**Replenishment.** Replenishment never changes current eligibility. A
currently exhausted candidate stays excluded; it is surfaced as
`recoverable` (with `human_action_required`) when the active visibility
mode reports it. Each per-candidate replenishment record retains the
complete normalized `ReplenishmentState` provenance (provider, kind,
availability, details state, optional earliest expiry, retrieval time)
next to its decision, and replenishment sets are canonicalized by
`(provider, kind)` — output determinism only, never ranking semantics.
Reset credits are visible only when a normalized `ReplenishmentState` is
supplied as selector input; live reset-credit acquisition is not part of
the current service.

**Provenance and explanation.** `SelectionDecision` preserves the exact
`SelectorPolicy.preference_order` (ordered, never sorted; empty list when
absent) so a preference-decided tie is reconstructable from the decision
alone. Human `--explain` surfaces governing capacity evidence (scope,
resource, kind, remaining, diagnostic window id when present) for the
selected, alternative and capacity-excluded candidates where it exists,
full reservation decisions — including triggered-but-permitted ones — for
eligible candidates, and the applied preference order.

D-032 preserves machine-interface v1 and the SelectionDecision serialized
field set. Current selected `identity.variant` values identify the configured
effort without changing identity semantics; use the explicit field in the
decision's versioned catalog to reconstruct effort comparisons. Variant is
never parsed by production logic. Public explicit effort output is deferred.

**Outputs.** The selector returns a structured `SelectionDecision`:
selected candidate, alternatives in exact ranking order, excluded
candidates with one primary exclusion stage
(`policy_blackout`, `hard_constraint`, `capability`, `capacity`,
`reservation`) and normalized reason codes, closest candidates for a
no-solution result (stage progress only, capped at 3, no second quality
score), recoverable candidates and degraded flags. Requirements are never
relaxed and no fallback bypasses capability.

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
6. **Rank sufficient eligible candidates.** Under `balanced`, prefer the
   happy-hour quota-preference group, then lower scarcity penalty, then the
   smallest adequate capability margin, then lowest configured reasoning
   effort, then stable configured preference and stable model identity as
   deterministic ties.
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

## Scarcity parameters

U-007 is resolved: the D-005 concept is accepted with exact parameters.

**Continuous penalty.** For every numerically known applicable window,

```text
penalty_units = (100 - remaining_percent)^2
scale         = 10000            (SCARCITY_PENALTY_SCALE)
```

so remaining 100% costs 0 units, 80% costs 400, 50% costs 2500, 20% costs
6400, 2% costs 9604, 1% costs 9801 and 0% costs 10000. Ranking must compare
the integer units, never normalized floats. The penalty contains no linear
or logarithmic term, no reset proximity, no provider price, no capability
score, no model prestige and no provider preference. Scarcity answers only
one question: how constrained is this applicable current subscription
capacity?

**Explanatory labels.** Labels are explanation only and never replace the
continuous penalty; two candidates both labelled `scarce` may still have
different penalties. Exact boundaries:

```text
remaining >= 80  -> plentiful
remaining >= 50  -> normal
remaining >= 20  -> scarce
remaining >= 1   -> critical
remaining == 0   -> unavailable
```

`unknown` is never produced from a numeric percentage; it represents
insufficient trustworthy capacity information.

**Applicability.** Only the candidate's explicit catalog
`capacity_bindings` participate; applicability is never inferred from
`window_id`, `limitName`, `normalModelSlug`, model names or provider
aliases. Within every applicable scope, all windows carrying a usable
percentage pair participate — token and provider-normalized `time`
windows alike, so a restrictive Z.ai `TIME_LIMIT` window may govern while
the token windows look healthy. Unrelated scopes never participate, even
at 0%.

**Aggregation.** Across all applicable windows of all bound scopes the most
restrictive result governs: `aggregate_penalty_units = max(window
penalties)`, equivalently `effective_remaining_percent = min(remaining
values)`. The label derives from the effective remaining, never an average.
Governing-window evidence is explanation-only and is tie-broken by a stable
canonical key over normalized fields (provider, scope_id, resource, kind,
window_id-or-empty), independent of input order.

**Unknown versus unavailable.** Explicit exhaustion wins: any known
applicable window at `remaining_percent == 0` makes the assessment
`unavailable` (penalty 10000, effective remaining 0) even when another
bound scope is unknown. Otherwise the assessment is `unknown` — with no
numeric penalty and no effective remaining, because unknown is
incomparable to numeric scarcity — whenever any bound scope cannot be
completely assessed: missing provider snapshot, unknown capacity binding,
a snapshot whose status is not `ok`, no matching window for a bound scope,
or an applicable window without a percentage pair. A scarcity result
always names the candidate's applicable scopes: they are empty only when
the capacity bindings themselves are unknown (`capacity_bindings = None`);
known bindings are preserved even when their telemetry is incomplete,
because the failure is in the telemetry, never in the applicability. A
non-`ok` snapshot status means telemetry is not trustworthy; it never
means quota is exhausted, and telemetry acquisition failure is never
reported as `unavailable` scarcity. At most one snapshot per provider is
accepted per assessment; duplicates fail typed validation.

**No stale threshold.** Current status acquisition is synchronous and
fresh-on-demand; if caching is added, stale behavior will require an explicit
decision.

**Unknown-capacity policy.** Exactly two modes exist:

- `degraded`: an unknown-capacity candidate may remain conditionally
  usable (`degraded = true`), carries no numeric scarcity penalty, and a
  known healthy sufficient candidate must rank ahead;
- `strict`: an unknown-capacity candidate is blocked.

Known `unavailable` (explicit exhaustion) is blocked in both modes; known
nonzero capacity is eligible in both, subject to other policy.

## Reservations

A reservation rule states a preservation threshold over a capacity **scope**,
not a model. The calibration proves the reason: Luna, Terra and Sol all bind
to `openai/codex`, and GLM-5.3 and GLM-5.3-Flash both bind to
`zai/coding_plan` — they consume one shared subscription quota together, so
a model-specific reservation would incorrectly imply independent per-model
quota.

```yaml
reserve:
  - rule_id: protect-openai-weekly
    scope:
      provider: openai
      scope_id: codex
    resource: tokens
    kind: weekly
    when_remaining_below: 20
    minimum_task_level: L4
```

The trigger boundary is strict and frozen: the reservation triggers iff
`remaining_percent < when_remaining_below`. At a threshold of 20, remaining
20 does **not** trigger and remaining 19 does. When triggered, use is
blocked below `minimum_task_level` (L0–L3 above) and permitted at or above
it (L4–L5). If the reservation's target window cannot be identified or
lacks usable percentage evidence, the evaluation is explicitly unknown —
never silently treated as not triggered.

A reservation does not create capacity: it cannot override explicit
exhaustion, and a high task level cannot make a 0%-remaining scope usable.
`resource` must be a known normalized resource (`tokens` or `time`) and
`kind` a known window kind (`five_hour` or `weekly`), so the target window
is always concretely identifiable. The example above is documentation of
the mechanism, not a checked-in user default.

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

The current mechanics are:

- the target names a supported provider, optionally an exact model and an
  exact variant (a variant requires its model); matching is exact identity,
  with no wildcard string syntax;
- the rule carries an explicit IANA time zone (validated through the
  standard-library `zoneinfo`), a duplicate-free weekday list (`mon`–`sun`)
  and strict 24-hour `HH:MM` local times;
- intervals are half-open `[start, end)`: exactly at start is blocked,
  exactly at end is not; `start == end` is invalid and is never interpreted
  as a 24-hour blackout; a cross-midnight interval (for example
  `17:00 -> 03:00` on Monday) blocks from Monday 17:00 inclusive through
  Tuesday 03:00 exclusive;
- evaluation converts a caller-supplied timezone-aware instant into the
  configured zone, so DST transitions are handled by the time-zone database,
  never by constructing ambiguous local timestamps as the source of truth;
- a matching blackout returns the normalized `policy_blocked` exclusion and
  never rewrites capacity status, `remaining_percent`, scarcity penalties or
  model capability.

Do not hard-code a vendor's current definition of peak/off-peak hours. Z.ai
documentation describes off-peak benefits and dynamic resource behavior, and
some off-peak/reset-card parameters are explicitly dynamic. The user's desired
schedule therefore belongs in configuration and must carry an explicit timezone
and deterministic boundary semantics.

## Happy hours (quota-preference windows)

Happy hours are the preference-side counterpart of blackouts (D-035). A
vendor may make consumption of one model cheap or free for a limited period —
for example a usage campaign in which one model's quota is not counted, or
counted at a discount, during nightly off-peak hours. During such a window,
the conservation question flips: the cheap model should absorb the work so
full-price plans are preserved. A happy-hour rule states exactly that window
and target:

```yaml
happy_hours:
  - rule_id: zai-flash-campaign-night-sgt
    target:
      provider: zai
      model: glm-5.3-flash
    timezone: Asia/Singapore
    weekdays: [mon, tue, wed, thu, fri, sat, sun]
    start_local: "23:00"
    end_local: "09:00"
    reason_code: glm53flash_campaign_zero_quota
    start_date: "2026-09-03"
    end_date: "2026-09-20"
```

The schedule reuses the blackout mechanics exactly: an explicit IANA time
zone, the duplicate-free `mon`–`sun` weekday vocabulary, strict 24-hour
`HH:MM` local times, half-open `[start, end)` intervals with cross-midnight
support (`start == end` is invalid), and evaluation of a caller-supplied
timezone-aware instant converted into the rule's zone. Unlike a blackout, a
happy hour may be limited-time: the optional inclusive local calendar bounds
`start_date`/`end_date` restrict the window to a campaign period, and both
absent means a standing recurring window.

Semantics and boundaries:

- **Preference, never eligibility.** During an active window the matching
  candidates form a preferred ranking group after the capacity knowledge
  class: a preferred candidate outranks a non-preferred one with healthier
  quota, because its marginal quota cost is zero or discounted. The
  preference can never resurrect an excluded candidate: blackout, hard
  constraints, capability sufficiency, capacity exhaustion/unknown policy
  and reservations all still gate first.
- **Knowledge class still dominates.** A happy-hour candidate with
  unknown/degraded capacity still ranks behind a known healthy sufficient
  candidate. Unknown capacity is never ranked against a numeric scarcity
  value, and a preference is not evidence.
- **Exhaustion still excludes.** A candidate at 0% stays excluded even
  inside its happy hour. "Free right now" is user-side pricing knowledge,
  never fabricated capacity; a campaign's own small print (weekly caps,
  client-version gates) is exactly why the broker does not pretend
  exhaustion away.
- **No telemetry or capability rewrite.** The decision never touches
  capacity status, `remaining_percent`, scarcity penalties or model
  capability; `quota never changes a capability rating` holds in both
  directions. The explanation names the governing rule and its configured
  reason code, never "unavailable" or "incapable".
- **Determinism.** Rules are stored canonically sorted by `rule_id`; the
  first matching active rule decides. Blackout always wins over an
  overlapping happy hour because it is an eligibility stage.

## Replenishment and reset opportunities

Replenishment options are distinct from the currently active quota windows.
Examples include banked OpenAI Codex reset credits that can refresh usage limits
when deliberately redeemed.

The current semantics are:

> Reset credits are replenishment opportunities, not current capacity.

They never enter current quota percentages, never pretend quota has been
restored and are never consumed by the broker.

The normalized D-021 contract is implemented as the typed
`ReplenishmentState` in `scarcity_router/policy.py`, carrying only:

- `provider`;
- `kind` (a safe normalized identifier; the evidenced OpenAI concept is
  `rate_limit_reset`);
- `available_count` (integer `>= 0`);
- `details_known` (strict boolean);
- `earliest_expiry`, only when safely derivable and needed (requires known
  details and a positive count; a zero count never carries an expiry);
- `retrieved_at`.

No provider free-text, credit IDs, titles, descriptions or account identity
are representable.

Visibility is an explicit policy mode:

- `ignore`: replenishment does not affect policy output;
- `advisory`: expose replenishment availability, `available_count` and the
  details/provenance state, but do not mark current capacity recovered;
- `recoverable`: when `available_count > 0`, expose
  `recoverable = true` and `human_action_required = true`. This still does
  not make current capacity eligible: a currently exhausted candidate may be
  presented as *recoverable but not currently eligible*.

Visibility is not availability: a visible decision with
`available_count == 0` (advisory or recoverable) carries no availability
reason code — the numeric zero itself is the normalized explanation, and no
invented code exists for it.

The evidenced Codex shape (`availableCount` plus an optional `credits` list
whose detail rows may carry `id`, `resetType`, `status`, `grantedAt`,
`expiresAt`, `title` and `description`) stays at the provider edge. The
detail list may be absent or capped even when `availableCount` is larger, so
`details_known` is honest about coverage. Provider free-text titles and
descriptions are never exposed, and opaque credit IDs are not required for
selection unless a later use case proves they are needed.

The selector must not silently pretend a banked reset has already been
applied. The three modes above are the frozen behaviors; the broker never
calls any reset-consume method, adds a redemption endpoint or mutates
provider credentials — redemption is an external/user action. When the
supported Codex app-server exposes reset count and expiry/details, preserve
that provenance and freshness without using private backend endpoints.

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

The boundary for external performance evidence is:

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

The selector currently uses `balanced`, which chooses the least scarce
sufficient model. The policy model also leaves room for these future modes:

- `balanced`: least scarce sufficient model;
- `quality-first`: prefer greater capability margin, still respecting hard
  constraints and explicit reservations;
- `conserve-openai` and `conserve-zai`: add a documented preference/penalty to
  protect the named provider.

These modes modify candidate ordering while respecting hard constraints and
explicit reservations. The resource-state and preservation primitives remain
separate from ranking and are used by the application and selector. Direct
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

Simulation applies typed overrides to a copy of normalized capacity or
policy, then runs the SAME selector core (`select_model`) — there is no
copied ranking pipeline and no simulation-specific selector. It must not
mutate live observations and must not call a model. A
`CapacityPercentageOverride` targets exactly one existing window of an `ok`
snapshot that already carries a known percentage pair and changes only that
percentage pair; it never creates windows or snapshots and never turns
unknown telemetry into fabricated known telemetry. Overrides may also
replace the selector policy, replace the replenishment observations and
move the simulated evaluated instant. Output distinguishes CURRENT and
SIMULATED inputs and decisions. This is valuable for tests, policy
debugging, demonstrations and documentation.

Blackout and happy-hour windows are already covered by this mechanism:
replacing the selector policy and moving `evaluated_at` exercises any
schedule without waiting for real peak hours or campaign nights.
Replenishment availability and advisory health acceptance follow the same
pattern.

## Runtime feedback (deferred)

A future client may report quota exhaustion or temporary provider failure. Such
feedback may create short-lived runtime state until refresh, but is not required
for the first selector. It must never silently alter catalog capability.
