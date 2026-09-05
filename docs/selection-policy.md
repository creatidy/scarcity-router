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
- Model catalog and its version/provenance.
- Current normalized capacity snapshots and freshness.
- User policy mode, reservations and explicit overrides.
- Optional explicit provider/model availability schedules or blackout windows.
- Optional advisory provider/service health evidence.
- Optional replenishment metadata such as banked quota reset opportunities.
- Optional external benchmark/performance evidence used to curate the model
  catalog, never as live capacity.

## Deterministic decision sequence

1. **Resolve requirements.** Expand a profile in one place, merge permitted
   explicit requirements and validate contradictions.
2. **Filter hard constraints.** Remove candidates that cannot satisfy context,
   modality, tool, privacy, locality, provider/model or runtime requirements.
   Explicit provider/model blackout schedules compile to hard temporary
   exclusions at this stage.
3. **Check capability sufficiency.** Remove candidates below any required
   dimension. Record each failed dimension; do not average a critical deficit
   away with strength elsewhere.
4. **Apply capacity eligibility.** Exclude explicitly unavailable or exhausted
   candidates. Handle unknown/stale capacity under the active policy and mark
   degraded confidence. Advisory provider health can degrade or exclude a
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

## Scarcity aggregation

Subscription scarcity uses the normalized continuous function and all relevant
windows defined in `capacity-model.md`. The maximum per-window penalty governs,
so a healthy five-hour window cannot hide an exhausted weekly window.

Local models have no subscription scarcity penalty. If a local model satisfies
all requirements, `balanced` may select it. Runtime unavailability, missing
model data or hard-property failure remains disqualifying.

The exact penalty and label thresholds are accepted only after scenario tests
at M2. They are documented now to prevent an undocumented scoring function from
appearing in code.

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

## Advisory service health

Provider/service health is independent from model capability and account quota.
The selector should prefer direct evidence in this order unless a later decision
supersedes it:

1. direct current account/runtime observation relevant to the candidate;
2. explicit provider-native failure/high-traffic signal from the supported
   access path;
3. official public status metadata relevant to that product/component;
4. optional third-party monitoring only when explicitly configured.

An aggregate public status page must not override a successful direct local or
account observation, and a green status page must not fabricate available
quota. Conversely, a direct provider high-traffic/error signal may justify a
short-lived degraded state even when published status is green.

Current planning notes:

- OpenAI's official status service exposes machine-readable global/component
  health and includes Codex-related components; treat it as advisory.
- `status.hellozai.com` is unrelated Zai Payments infrastructure and must not be
  used for Z.ai/GLM selection.
- no authoritative public Z.ai/GLM status page has been identified; use
  provider-native failure signals and explicit unknown state instead of
  inventing one.
- Ollama direct reachability/model-presence from the local collector is the
  authoritative local availability signal. Rich telemetry from the separate
  `ollama-monitoring` project is optional future evidence, not required for
  basic eligibility.

## External capability and performance evidence

Artificial Analysis may be used as one provenance-bearing input when curating
the model catalog. Its useful data includes stable model/creator identifiers,
benchmark indices, pricing and observed performance metrics such as throughput
and latency.

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
- `local-first`: prefer a sufficient healthy local candidate before cloud;
- `subscription-first`: prefer sufficient subscription capacity before local;
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

Useful archetypes include:

- `single`: one model solves the task;
- `cascade`: an efficient model attempts first and a bounded gate may escalate
  once to a stronger model;
- `critique`: one solver, one independent read-only critic and at most one
  bounded remediation/verification cycle.

These are archetypes, not a requirement to copy any specific runtime. Every
compound recommendation must carry an explicit execution envelope covering at
least maximum legs/reviews/remediation/retries/wall-clock budget and expected
aggregate scarcity/capacity consumption. It must never recommend “review and fix
until clean”.

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
