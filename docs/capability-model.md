# Capability and task model

## Separation from capacity

A capability profile is a curated claim about what a model can reliably do.
It does not change when quota changes. Capacity may make a model scarce or
temporarily unusable; it never makes the model intrinsically weaker.

## Shared-System Requirements and Knowledge

Kernel produces task/harness requirements; Router interprets them through
the existing multidimensional rubric and monotonic eligibility. Quality
minima are independent of quota/cost, L-level shorthand alone is insufficient,
and unrepresentable/unsupported requirements must not be silently dropped.
The current nine hard-constraint members are not proof of a complete rich
TaskSpec/harness/data-authority contract; #175 owns the exact coverage/mapping
study and producer-consumer acceptance. No second quality scale or new fields
are specified here.

MI owns public model/interface/benchmark/offer evidence. Router's local
catalog and reviewed adoption floors remain a working bootstrap/projection
until #176 proves admitted versioned snapshot consumption and #185 migration.
Retain provenance and calibration version; conflicting/expired knowledge must
follow the future approved admission contract, never renew an expired
promotion merely because refresh failed. No Router crawler or independent
public-facts database, and no private quota/credentials sent to MI. Runtime
outcome calibration (#177) needs local contextual evidence and human review;
raw success percentages are not causal capability ratings.

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

This six-dimension set is the current vocabulary. No orchestration dimension
and no factuality/reliability dimension is included; a new dimension requires a
demonstrated routing need and an explicit contract change.

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
with full provenance and documented in
[`docs/model-calibration.md`](model-calibration.md).

## TaskRequirement contract

The task-requirement contract has exactly four distinguishable parts:

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

The core type (`scarcity_router/selection_types.py`) stores exactly the first
three parts — `task_level`, `capability_minima` and `hard_constraints`.
Profile expansion is a *construction pathway* into those three parts, not a
fourth serialized field. `TaskProfileDefinition.to_requirement()` and
`TaskProfileCatalog.resolve(profile_id)` return the calibrated stored
requirement as a pure operation, with no capability inference, model lookup or
merge with explicit task inputs.

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
rather than being guessed from variants. Machine-interface v1 identity and
decision field sets remain unchanged: current variants identify configurations,
and the versioned catalog supplies explicit effort for reconstruction. An
explicit effort field in public decisions is deferred to a future v2 interface.

Reference role assignments can name a model or effort setting that is not yet
in the active catalog. Such assignments are descriptive metadata only and do
not establish capability ratings, capacity bindings or selector eligibility.

## Hard constraints

Hard constraints are categorical or numeric requirements, not quality scores.
The current vocabulary is exactly:

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

The typed representation uses a `ModelRef` for `required_model`
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
`calibrated_requirement` in [`model-policy.json`](../model-policy.json) and
verified through capability-only scenario tests.
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

### Kernel ordinary-meaning local interpretation (#175)

**Status: Implemented local reception after contract-first inspection.
Not current Kernel #51 transmission or live task readiness.**
Kernel producer `a9a65006cc8cc5b5e4468746a18ff51daf742e6b` receives approved
ordinary `Declaration` v1: exact original UTF-8 JSON with `version`, `outcome`,
`criteria`, `quality`, `interface`, `context`, `unknowns`, `paths`, `network`,
`effects`, `recipes` and `provenance`. Other than integer version and text
outcome, these are string arrays, not calibrated model requirements. Its
`RequirementsHandoff` retains the draft/revision/evidence, exact decision and
Program digest; actual `ScarcityRouterAllocator.translate_ordinary` preserves
them and refuses before transport. Closed Kernel #49 therefore proves approved
meaning and intact refusal, not successful mapping, privacy or task readiness.
Kernel #51 adopts agreed mapping after Router #175; it is not a circular
prerequisite for Router to define this interpretation.

The bounded extension is a versioned local interpretation of **explicit
data already accepted by that producer**, not new Kernel fields or a prose
classifier. A single `quality` string with prefix
`scarcity-router.requirement.v1:` carries a JSON object containing required
`requirement` (the existing complete `TaskRequirement`) and optional `profile_id`.
Numerical minima and task level must be supplied in that object. Optional
profile expansion uses the existing `TaskProfileCatalog` once, as a monotone
base for the explicit requirement; its presence never guesses a profile from
outcome, price, L-level or model name. The complete marker bytes are approved
as part of the original declaration, not an out-of-band weaker replacement.
Every supplied dimension retains the existing scale and independent threshold.
For a profile marker the explicit requirement must already contain its complete
expanded floor; implicit missing floor values refuse. This keeps the approved
bytes sufficient even if a later profile lowers its calibration. A cached
interpretation records profile version **and actual expansion** and refuses
rebinding when either changes or disappears. Fresh interpretation may retain
the original explicit floor but can never weaken it. Profile-plus-requirement
means profile-plus-tightening internally; the existing recommendation source
XOR is unchanged.

A single optional `interface` string with prefix
`scarcity-router.request.v1:` carries the existing strict `RequestBinding`
shape for structural channel requirements and explicit identity/pin constraints.
It supplies no grant, model inventory, per-request allowlist, endpoint,
credential, workspace or runtime policy. Ordinary profile choice is in the
quality marker; configured client aliases remain the existing trusted routing
layer rather than an additional interpretation mechanism. No new REST/MCP/
OpenAI/worker operation or version is proposed by these local marker names.
Missing interface marker can mean no additional structural demands, never
proof of compatible current channels. `profile_alias` in a marker is unsupported:
trusted configured client aliases already exist outside this grammar. Marker
identity/pins must agree with existing binding rather than replace it.

| Producer field | Proposed coverage and owner | Required refusal/limit |
| --- | --- | --- |
| `version` and exact bytes | Strict pinned producer v1; digest/correlation only | Unknown keys/version, duplicate keys, booleans-as-version, malformed or oversized input refuse without echoing content |
| `quality` | Explicit versioned complete requirement; optional existing profile floor; Router owns interpretation/rubric | Missing/duplicate marker or other quality text is missing/unsupported meaning; no ordinary L0/reference or empty-minimum fallback |
| `interface` | Explicit existing structural/pin binding; Router compatibility evidence gates all requested features | Other text such as `UTF-8 file` is not an inference/harness protocol assertion; unsupported text refuses, not guessed compatible |
| `context` | Current producer text is not a typed context/locality/privacy guarantee | Nonempty unaccounted text, including `private local context; no egress`, blocks mapping; no invented privacy tag, local resource or approximation |
| `unknowns` | Kernel currently blocks approval/handoff when nonempty | Direct/malformed consumer input containing unknowns also refuses; empty array is not a capability or permission claim |
| `outcome`, `criteria`, `recipes` | Kernel-owned result scope/verification; exact full declaration remains the subject | Not model quality minima and not executed or treated as authority by Router; mapped model requirements do not attest result acceptance |
| `paths`, `network`, `effects` | Kernel-owned scoped requests, checked against trusted policy outside proposer data | No Router client/admin grant follows from them; require the actual Kernel gate before execution and current Router authorization at admission |
| `provenance` | Preserve subject/digest and interpretation version, not a trusted issuer declaration | Text/model/issue claims cannot authenticate approval or widen scope; no prompt/private-account/credential echo in diagnostics |

Interpretation output must account for every original field and preserve the
original approved subject/digest and mapping revision. Successful mapping means
only a justified model/channel requirement projection, **not** approval, task
admission, privacy enforcement, successful transmission or live readiness.
Unsupported/missing meaning must give field-specific actionable refusal before
any selection/transport. Owner approval is checked by Kernel's trusted current
composition, not by a client-supplied `approved` boolean or issuer string.
`kernel_requirements.interpret_kernel_declaration` accepts only the exact raw
declaration, not prompts or a second approval/authority system. Kernel retains
the full draft/revision/evidence/decision/Program handoff; Router's projection
reports its original declaration digest, interpretation version, inspected
schema revision, coverage and actual profile expansion. The evidence pin is not
the origin of arbitrary input. No original meaning, private scope/account
contents or credentials are echoed in diagnostics or projection repr.
Binding requires the current declaration digest supplied after Kernel's trusted
approval/subject checks; a cached projection for changed meaning refuses.
Digest equality is correlation, not authentication, fresh evidence or a grant.

The owning types' conventions apply without new meanings: producer arrays are
not null/false, version is an exact integer; unknown keys/versions and nested
duplicate keys/nonfinite/malformed JSON refuse. Optional numeric/capability
null means no supplied minimum, **not** unknown adequacy; bool false means no
additional positive demand, not permission or a denial. Request booleans reject
explicit null. A present null profile id refuses. Nonempty `unknowns` refuses
even though the pinned Kernel already prevents its approval. No optional
convention permits missing ordinary quality to become generic L0.

For supplied interpreted requirements, recommendation and resource eligibility
must use the existing core: profile floor, explicit requirement and structural
requirements tighten monotonically; authorization remains an independent
intersection. At fixed task level, policy, observations and task context,
stronger capability/hard/channel constraints never enlarge candidates.
Task-level changes affect shipped reservation eligibility independently and
are not an alternative quality score or a claimed global monotonicity theorem.
Contradictions,
unsupported privacy, missing compatibility and unknown resource facts fail
through existing validation/refusal rather than being dropped. Generic
unprofiled callers without ordinary interpreted requirements retain shipped
L0/structural behavior. No second scale, inventory or ranker.
An optional internal `RouteRequest.task_requirement` carries the projection
through **both** competitive routing and exact admission. No synthetic
per-request profiles or separate selector are constructed. Supplied resolved
tool/reasoning/context/output demands also gate actual channel compatibility,
context and output limits; a caller's smaller output ceiling is a contradiction,
not permission to raise cost silently. Vision has no evidenced generic channel
binding here and refuses rather than inheriting model-only support.
Exact admission additionally invokes the selector's same per-candidate hard
and capability predicates on the exact bound catalog identity, without ranking,
repairing or substituting a target. Request model/variant/resource pins must
agree. Additive admission refusal codes reuse `hard_constraint_failed`,
`capability_failed` and `pinned_request_failed`; existing fields/versions and
generic source shapes do not change. Consumers must treat unrecognized refusal
codes as refusal, never permission. Independent structural context demands are
combined by maximum before resolution; a smaller request context does not
weaken an explicit/profile floor, while a smaller explicit floor remains invalid.

Contract-first inspection precedes implementation. Positive producer-shaped fixtures distinguish
the actual producer's accepted string-array shape from this proposed semantic
grammar; do not claim the pinned Kernel allocator currently performs successful
mapping. Existing real exact-encoding/private-local fixture remains refused.
Kernel #51's adoption and #174's executable consumer/real-harness acceptance are
later receipts, not grounds for fictional current integration or circular
pre-implementation gating. Root local interpretation and requirement/routing
pipeline conformance are testable now; the pinned Kernel SPI still unconditionally
refuses before transport. Exact-current transmission scope must be recorded.
Kernel #51's controller integration must generate/review these typed markers,
not require ordinary operators to hand-author nested JSON/pins for each task.

The historical initial catalog was restricted to these real-workflow models:

- GPT-5.6 Luna;
- GPT-5.6 Terra;
- GPT-5.6 Sol;
- GLM-5.3;
- GLM-5.3-Flash.

The active artifact is catalog v5, including D-053 reviewed generation/track
floor entries (e.g. Astra Low), not only that initial list. This is not a
universal public model database. Local inference can be an execution resource
under D-040; it is not automatically an entry in the subscription recommender.

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

The core types in `scarcity_router/selection_types.py` enforce these rules at
construction:

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
- The catalog container enforces unique identities, permits an empty catalog
  when no entries are configured, and serializes entries sorted by
  `(provider, model, variant)` independent of insertion order.

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
