# Capability and task model

## Separation from capacity

A capability profile is a curated claim about what a model can reliably do.
It does not change when quota changes. Capacity may make a model scarce or
temporarily unusable; it never makes the model intrinsically weaker.

## Task levels

The stable provider-independent difficulty scale is:

| Level | Name | Intent |
| --- | --- | --- |
| L0 | Mechanical | Deterministic search, extraction, formatting, inventory, link checks and repetitive QA with minimal judgment. |
| L1 | Routine | Well-defined low-ambiguity work with obvious expected behavior. |
| L2 | Standard | Normal professional work requiring understanding and some reasoning. |
| L3 | Advanced | Substantial reasoning, implementation complexity, debugging, ambiguity resolution or architecture. |
| L4 | Expert | Subtle, high-value work where mistakes materially matter, including difficult implementation, methodological review or scientific interpretation. |
| L5 | Critical | Work requiring the strongest suitable available capability because errors are especially costly or reasoning is genuinely frontier-level. |

L5 does not mean “always select the most expensive model.” Domain fit and hard
requirements still apply.

Task level is a convenience signal and must not replace capability dimensions.
Profiles translate common task names into explicit requirements.

Task level is not a capability score. There is no shortcut such as
“L4 means every capability minimum is 4”: minima come only from the profile or
explicit requirements. Task level participates in reservation eligibility
(minimum task level) and explanation; capability sufficiency is always
evaluated per dimension.

## Current capability dimensions

The current policy vocabulary may assess:

- `reasoning`;
- `coding`;
- `scientific_methodological`;
- `writing_editorial`;
- `tool_use`;
- `translation_multilingual`.

Use an ordinal scale only as a practical routing rubric, not as scientific
measurement. Additional dimensions such as factual reliability, long-context
behavior, agentic execution or vision may be added only when they change a real
routing decision. Schema evolution must permit new dimensions without
redesigning model identity or capacity.

`translation_multilingual` is a first-class dimension because translation
quality changes a real routing decision; it must not be inferred solely from
`writing_editorial`. A dedicated orchestration or long-context quality
dimension is intentionally deferred. Orchestration is currently represented by
high reasoning, strong `tool_use`, meaningful `writing_editorial` and adequate
hard context/output requirements until routing experiments show that a new
dimension is necessary.

This six-dimension set is the frozen M2 vocabulary. No orchestration dimension
and no factuality/reliability dimension is added yet; a new dimension requires
a demonstrated routing need and an explicit contract change.

## Capability rating scale

Capability ratings use a small frozen ordinal scale:

```text
1..5
```

Missing or unknown ratings are represented separately, never as `0`, because
unknown and known-low capability are different states. `0 = unknown` is
forbidden. The intended rubric is approximately:

| Rating | Routing rubric |
| --- | --- |
| 1 | Minimal / weak for the dimension. |
| 2 | Limited but usable. |
| 3 | Solid professional capability. |
| 4 | Strong / expert-grade. |
| 5 | Exceptional / frontier-grade for the dimension. |

These are routing rubrics, not scientific measurements. The accepted
per-model ratings are curated in [`model-catalog.json`](../model-catalog.json)
with full provenance (M2c, D-025) and documented in
[`docs/model-calibration.md`](model-calibration.md).

## TaskRequirement contract

M2 freezes the conceptual task-requirement contract. A `TaskRequirement` has
exactly four distinguishable parts:

1. **Task level** — `L0`–`L5` as defined above, supplied explicitly or as a
   profile-provided default. It is not a capability score and never generates
   capability minima.
2. **Capability minima** — explicit per-dimension minima on the frozen scale,
   e.g. `coding >= 4`, `reasoning >= 4`, `tool_use >= 3`. Every specified
   minimum must be met by the corresponding model rating; capability
   dimensions are never averaged. A missing model rating for a required
   dimension is `UNKNOWN_CAPABILITY`, not zero. Whether unknown capability is
   permitted to proceed must be explicit policy; it must never silently pass
   sufficiency.
3. **Hard constraints** — the typed vocabulary in the next section.
4. **Profile expansion** — profiles expand in exactly one place into task
   level, capability minima and hard constraints.

Explicit task inputs may tighten a profile expansion (raise a minimum, add a
constraint) but never silently loosen it, and contradictory explicit
requirements are a validation error, never heuristically resolved.

**Implemented stored shape (M2b, D-024).** The core type
(`scarcity_router/selection_types.py`) is a resolved requirement storing
exactly the first three parts — `task_level`, `capability_minima` and
`hard_constraints`. Profile expansion is a *construction pathway* into those
three parts, not a fourth serialized field. M2c (D-025) implemented the
expansion mechanism and the calibrated profile definitions:
`TaskProfileDefinition.to_requirement()` and the authoritative
`TaskProfileCatalog.resolve(profile_id)` return exactly the calibrated stored
requirement — pure, with no capability inference, no model lookup and no
merge with explicit task inputs (that merge belongs to later selector input
assembly).

The canonical machine-readable definitions for capability classes, profile
vocabulary, class/profile relationships and current workflow exemplars live in
[`model-policy.json`](../model-policy.json). This document keeps the conceptual
model and selector-facing rules; it does not duplicate that policy artifact.

## Reasoning effort and selector identity

Catalog v2 (D-032) represents calibrated invocation configurations with an
explicit `reasoning_effort: str | null` field, independently of capability
ratings and subscription capacity. The normalized order is
`none < low < medium < high < xhigh < max`. String `"none"` is a real effort
setting; null/absent means no configured effort, never zero intensity.

Construction and deserialization validate the vocabulary. Reasoning support
`true` requires known effort; `false` or unknown support requires null/absent
effort. These rules are provider-independent. Neither model/display names nor
`identity.variant` supply effort semantics. Variant remains an opaque stable
configuration identifier. Capabilities are curated separately per configuration;
Sol Medium does not automatically inherit Sol High's vector.

Selection uses lowest effort only after capability sufficiency, capacity and
reservation eligibility, scarcity penalty and capability margin. Unconfigured
effort sorts after known effort in a separate typed state, not a magic numeric
sentinel. All current selectable reasoning-capable entries have known effort.

The exact initial additions are Luna Medium, Terra Medium and Sol Medium;
existing Luna Max, Sol High and both GLM Max vectors are preserved. All five
OpenAI configurations consume the same evidenced `openai/codex` scope. No
effort-specific quota penalty or API-price metric exists. Other supported API
efforts are deferred until independently calibrated, not generated from defaults.

External catalog authors migrate explicitly to v2 by encoding reviewed effort
values; legacy absent effort for reasoning-capable entries fails validation
rather than being guessed from variants. M3 machine-interface v1 identity and
decision field sets remain unchanged: current variants identify configurations,
and the versioned catalog supplies explicit effort for reconstruction. An
explicit effort field in public decisions is deferred to a future v2 interface.

Reference role assignments can name a model or effort setting that is not yet
in the active catalog. Such assignments are descriptive metadata only and do
not establish capability ratings, capacity bindings or selector eligibility.

## Hard constraints

Hard constraints are categorical or numeric requirements, not quality scores.
The initial M2 vocabulary is exactly:

| Constraint | Meaning |
| --- | --- |
| `minimum_input_context_tokens` | Smallest acceptable input context allowance. |
| `minimum_output_tokens` | Smallest acceptable output allowance. |
| `requires_tool_use` | Tool-calling support is required. |
| `requires_vision` | Vision input support is required. |
| `requires_reasoning_mode` | A provider reasoning/presentation mode is required. |
| `required_provider` | Restrict candidates to one provider. |
| `required_model` | Restrict candidates to one model. |
| `required_variant` | Restrict candidates to one variant. |
| `privacy_constraint` | A privacy boundary the candidate must satisfy. |

There is no local/cloud constraint and no local runtime after D-017, and no
generic free-form constraint framework: a new constraint kind requires an
explicit contract change, not a stringly-typed escape hatch.

**Implemented typing (M2b, D-024).** `required_model` is a typed `ModelRef`
(`provider`, `model`) rather than one qualified string, so the contradiction
rule is validated, not parsed: when `required_provider` and
`required_model.provider` are both supplied they must match exactly, or
construction fails with `SelectionContractValidationError`. Model providers
are exactly `openai` and `zai` until an explicit contract change;
`required_variant` and `privacy_constraint` are safe opaque identifiers, and
no privacy policy values or matching logic exist yet. Serialization is
compact: `None` values and false `requires_*` members are omitted.

Contradictions fail validation. For example, `required_provider=openai`
together with a `required_model` of another provider is a validation error,
never something the selector resolves heuristically.

Selection filters hard failures before comparing capability or scarcity. A
high reasoning score cannot compensate for missing vision or insufficient
context. Task minima are evaluated against catalog allowances — what the
model supports — which are catalog data, not task requirements.

## Profiles

Profiles are data-driven aliases for requirements. The formal profile IDs
remain:

- `mechanical`;
- `routine_coding`;
- `deep_coding`;
- `scientific_review`;
- `editorial`;
- `general_reasoning`;
- `orchestration`;
- `translation`.

Each profile expands, in one place, into the three requirement parts of the
`TaskRequirement` contract: a task level, per-dimension capability minima and
hard constraints. Expansion is a pure mapping — a profile never names a model,
a provider or a class as its output.

Conceptually:

```yaml
deep_coding:
  minimum:
    coding: 4
    reasoning: 4
    tool_use: 3
```

This example shows structure, not accepted ratings or final file syntax.
The accepted numeric minima are calibrated per profile as
`calibrated_requirement` in [`model-policy.json`](../model-policy.json)
(M2c, D-025) and verified through capability-only scenario tests.
Advanced clients may supply raw
capability minima and hard constraints directly.
Profile definitions must live in one catalog/config source, not duplicated in
CLI, REST, MCP or selector branches.

In `model-policy.json`, the top-level `task_profiles` array is the formal
selector-facing vocabulary. A class's `typical_task_profiles` contains only
those formal profile IDs. Descriptive class use cases that are not formal
profiles belong in `typical_workflow_roles` and do not participate in
selection.

## Model catalog entries

The initial catalog is restricted to the models in the real workflow:

- GPT-5.6 Luna;
- GPT-5.6 Terra;
- GPT-5.6 Sol;
- GLM-5.3;
- GLM-5.3-Flash.

No Claude, no local models, no universal catalog.

Each entry carries, conceptually:

- **Stable catalog identity** — `provider`, `model`, `variant`. Identity is
  stable and separate from the display name, the provider quota bucket,
  provider-internal aliases and the descriptive model class. Model class never
  becomes identity.
- **Configured reasoning effort** - the explicit normalized invocation setting,
  not capability, identity parsing or subscription scarcity.
- **Supported hard properties** — tool use, vision, reasoning mode and privacy
  characteristics the entry can honestly claim.
- **Input context allowance and output allowance** — what the subscription
  model actually provides, evaluated against task hard-constraint minima.
- **Capability ratings** — per first-class dimension, on the frozen scale, or
  explicitly unknown.
- **Rating provenance** — per rating: source, source identifier/version/date,
  assessment date, confidence and rationale.
- **Human override record** — explicit and reviewable; an override never
  silently overwrites source evidence.
- **Capacity bindings** — the capacity scopes (see `docs/capacity-model.md`)
  whose consumption constrains this entry. A model may bind to more than one
  scope, because multiple quota constraints may apply simultaneously; the
  bindings are explicit normalized data, never derived from diagnostic window
  identifiers.

**Implemented semantics (M2b, D-024).** The core types in
`scarcity_router/selection_types.py` enforce these rules at construction:

- A known rating (`1..5`) requires complete provenance — at least one
  evidence reference, a coarse `low|medium|high` confidence, an assessment
  date and a non-empty rationale. A naked rating is invalid.
- Unknown capability is the explicit serialized state
  `{"rating": null}` — never `0` and never an omitted dimension. All six
  dimensions are required in every serialized capability vector, so a
  well-formed "unknown" is distinguishable from a missing schema dimension,
  and extra dimensions are rejected.
- A human override never mutates the source assessment: the curated rating
  and its evidence stay serialized and reviewable, and the effective value is
  a derived view. An override requires an existing known base rating.
- `capacity_bindings` is `None` when model-to-scope applicability is
  **unknown** — which must never be read as "this model consumes no
  subscription quota" — and a non-empty set of exact `(provider, scope_id)`
  references when applicability is known. The empty set is invalid and must
  never serve as an optimistic "unmetered" state. Known bindings are unique,
  use the model's own provider and serialize deterministically;
  cross-provider bindings are unsupported in the initial contract (D-024).
- The catalog container enforces unique identities, admits an empty catalog
  (the M2b contract precedes the M2c population) and serializes entries
  sorted by `(provider, model, variant)` independent of insertion order.

The accepted values are established as reviewable artifacts:
[`model-catalog.json`](../model-catalog.json) for ratings, provenance and
capacity bindings and [`model-policy.json`](../model-policy.json) for profile
minima, with rationale in [`docs/model-calibration.md`](model-calibration.md).
They must not be inferred solely from price or vendor
marketing.

## Governance and uncertainty

Capability assessments are subjective curated evidence. Changes require a
human-readable diff, source, rationale, confidence and date/version. Marketing,
anecdotes and benchmarks may inform an assessment, but no single one becomes
ground truth automatically.

Do not imply false precision. The first catalog only needs enough resolution to
make trusted decisions in the owner's workflow. A continuously curated catalog
may later become a product asset, but building a universal leaderboard is out
of scope.
