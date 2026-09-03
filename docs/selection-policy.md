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

## Deterministic decision sequence

1. **Resolve requirements.** Expand a profile in one place, merge permitted
   explicit requirements and validate contradictions.
2. **Filter hard constraints.** Remove candidates that cannot satisfy context,
   modality, tool, privacy, locality, provider/model or runtime requirements.
3. **Check capability sufficiency.** Remove candidates below any required
   dimension. Record each failed dimension; do not average a critical deficit
   away with strength elsewhere.
4. **Apply capacity eligibility.** Exclude explicitly unavailable or exhausted
   candidates. Handle unknown/stale capacity under the active policy and mark
   degraded confidence.
5. **Apply reservations.** Below a configured capacity threshold, keep a model
   eligible only at or above the reservation's minimum task level. Reservation
   changes eligibility for this task, not capability.
6. **Rank sufficient eligible candidates.** Under `balanced`, prefer lower
   scarcity penalty, then the smallest adequate capability margin, then stable
   configured preference and stable model identity as deterministic ties.
7. **Produce explanation.** Return the winner, alternatives, exclusions,
   capacity evidence, applied policy and reason codes.

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
constraints.

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
- capacity/reservation exclusions;
- no automatic relaxation of requirements.

## Output contract

A decision must include:

- selected provider/model/variant or an explicit no-selection state;
- ordered alternatives;
- excluded/reserved candidates when useful for diagnosis;
- normalized reason codes plus readable reasons;
- task/profile requirements used;
- relevant capacity windows, timestamps and state;
- active policy and reservation rules;
- catalog/policy versions;
- degraded/unknown indicators.

Reasons should be concise enough for an agent but complete enough for a human.
The public response never includes credentials or unredacted provider payloads.

## Simulation

Simulation applies typed overrides to a copy of normalized capacity or policy,
then runs the same selector. It must not mutate live observations or call a
model. Output distinguishes current and simulated inputs and decisions. This is
valuable for tests, policy debugging, demonstrations and documentation.

## Runtime feedback (deferred)

A future client may report quota exhaustion or temporary provider failure. Such
feedback may create short-lived runtime state until refresh, but is not required
for the first selector. It must never silently alter catalog capability.
