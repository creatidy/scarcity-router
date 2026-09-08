# Model calibration

## Catalog v2 correction (D-032)

Issue #57 adds three separately calibrated invocation configurations on
2026-09-08. Catalog version is now 2; model-policy remains v5 with unchanged
profile minima. The original M2c record below remains historical provenance,
not a claim that its four-entry eligible sets are still the entire catalog.

| Configuration | reasoning | coding | scientific_methodological | writing_editorial | tool_use | translation_multilingual |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Luna Medium | 3 | 4 | 3 | 5 | 5 | 4 |
| Terra Medium | 4 | 5 | 4 | 4 | 5 | 4 |
| Sol Medium | 5 | 5 | 4 | 5 | 5 | 4 |

The architect-supplied official family launch/developer evidence uses the same
source identifiers documented below, with Medium the API default and Terra
positioned between Luna and Sol. This task accepts those external facts rather
than claiming new measurements. Effort-specific vectors are the explicit
owner-approved `accepted_reasoning_effort_calibration_2026-09-08_issue_57_D-032`
judgment, with medium confidence, per-dimension rationales and assessment date.
Luna Medium fails reasoning-4 tasks; Terra Medium's writing 4 preserves Luna
Max's orchestration role; Sol Medium's scientific and translation 4 preserve
Sol High's specialist role. No vectors are automatically copied across efforts.

All three additions have 1,050,000 input tokens, 128,000 output tokens,
tool/vision/reasoning support true, family version date 2026-07-09 and binding
`openai/codex`. No effort-specific capacity is evidenced or invented. The four
original vectors, hard properties, assessment provenance and dates are unchanged;
their configured max/high/max/max effort is now explicit catalog data.

Current capability-only eligible sets, pinned by calibration tests:

| Profile | Eligible configurations |
| --- | --- |
| mechanical / routine_coding | All seven |
| deep_coding | Terra Medium, Sol Medium, Sol High, GLM-5.3 Max |
| scientific_review / translation | Sol High |
| editorial | Luna Medium, Luna Max, Sol Medium, Sol High |
| general_reasoning | All except Luna Medium |
| orchestration | Luna Max, Sol Medium, Sol High |

These are eligibility sets, not provider preferences. Scarcity still precedes
capability margin and effort. No additional supported API effort is calibrated;
Astra issue #49 and M4 remain independent.

## Historical M2c calibration

The human-reviewable record of the initial capability and task-profile
calibration (D-025, resolving U-006). The machine-readable authorities are
[`model-catalog.json`](../model-catalog.json) and
[`model-policy.json`](../model-policy.json); this document explains the
accepted values and their provenance. Ratings are curated routing claims,
not measured quota and not scientific fact.

- **Calibration date:** 2026-09-06 (`assessed_on`, `last_reviewed_on`,
  `updated_on` / `updated_at`)
- **Catalog version:** 1 · **Policy version:** 4

## Operating-policy update after M2 calibration

Policy v5 (2026-09-07) makes GPT-6 Astra at low reasoning effort the preferred
reference scientific/methodological specialist and a deep-technical reasoning
option.

This does not modify catalog v1. Astra is not yet selector-eligible because
capability calibration, hard properties and semantic capacity applicability
have not been evidenced and accepted. The capability-only eligible sets below
record catalog v1; the v2 correction above supersedes their scope. The historical M2c calibration date, ratings,
provenance and four-model scope remain unchanged.

## Rating rubric

The frozen ordinal scale of `docs/capability-model.md`:

| Rating | Routing rubric |
| --- | --- |
| 1 | Minimal / weak for the dimension. |
| 2 | Limited but usable. |
| 3 | Solid professional capability. |
| 4 | Strong / expert-grade. |
| 5 | Exceptional / frontier-grade for the dimension. |

Ratings describe routing suitability in the owner's workflow. They are not
percentile scores, not derived mechanically from one benchmark, and not
provider marketing grades. Quota never changes a rating.

## Model rating table

| Model | reasoning | coding | scientific_methodological | writing_editorial | tool_use | translation_multilingual |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT-5.6 Luna Max | 4 | 4 | 3 | 5 | 5 | 4 |
| GPT-5.6 Sol High | 5 | 5 | 5 | 5 | 5 | 5 |
| GLM-5.3 Max | 5 | 5 | 4 | 4 | 5 | 4 |
| GLM-5.3-Flash Max | 4 | 4 | 3 | 4 | 5 | 4 |

Every known rating carries at least one evidence reference, a
`low|medium|high` confidence, the assessment date and a rationale in
`model-catalog.json`. No `HumanOverride` is used for this initial calibration:
these are the original curated ratings, and the owner's role judgment is
recorded as ordinary owner-observation evidence
(`accepted_workflow_role_calibration_2026-09-06`).

### Why the calibration is shaped this way

- **GPT-5.6 Luna Max — orchestration / synthesis / strong general
  professional work.** Strong reasoning but not the escalation ceiling;
  strong coding; excellent synthesis/editorial behavior; excellent
  tool-oriented workflow capability; usable scientific and multilingual
  capability, but not the reserved specialist.
- **GPT-5.6 Sol High — specialist / highest-quality adjudication.** Frontier
  reasoning and coding; scientific/methodological specialist;
  publication-quality editorial and translation specialist; excellent tool
  use. The broad 5s are intentional: scarcity and reservations — not
  artificially lowered capability — protect Sol from routine use.
- **GLM-5.3 Max — deep technical reasoner.** Frontier-grade technical
  reasoning and coding; excellent long-horizon tool execution; strong but not
  designated specialist scientific/editorial/translation capability. It
  competes directly with Sol on difficult engineering work before scarcity
  policy is applied.
- **GLM-5.3-Flash Max — execution generalist / high-throughput agent.**
  Strong reasoning and coding rather than a "cheap weak model"; excellent
  tool execution; good writing/multilingual capability; competent but not
  specialist-grade scientific methodology. Flash wins routine work later
  because it is capable enough and usually less scarce — not because ratings
  are inflated.

## Hard-property table

| Model | input context | output | tools | vision | reasoning mode |
| --- | ---: | ---: | --- | --- | --- |
| GPT-5.6 Luna Max | 1,050,000 | 128,000 | true | true | true |
| GPT-5.6 Sol High | 1,050,000 | 128,000 | true | true | true |
| GLM-5.3 Max | 1,000,000 | 128,000 | true | **false** | true |
| GLM-5.3-Flash Max | 1,000,000 | 128,000 | true | true | true |

- GLM-5.3 `supports_vision = false` is an **evidenced negative fact**, not an
  inference: the first-party GLM-5.3 model documentation
  (`https://docs.z.ai/guides/llm/glm-5.3`) states that GLM-5.3 currently
  supports **text-only inputs** for GLM Coding Plan users. It is unrelated to
  Flash being multimodal. A known negative must serialize explicitly
  (`"supports_vision": false`); `None` stays reserved for genuinely unknown
  properties.
- The GLM output allowances are explicit first-party model/Coding Plan
  documentation: the GLM-5.3 and GLM-5.3-Flash guides record a maximum output
  of **128K tokens** (with the 1M-token context window). They are not copied
  from a local ZCode UI setting and not carried over from an older GLM
  generation.
- Reasoning is always enabled for both GLM entries; the GLM-5.3 efforts are
  `low`, `high`, `max`, and `reasoning_effort: max` is recommended for
  GLM-5.3-Flash (`https://docs.z.ai/guides/vlm/glm-5.3-flash`).

## Capacity bindings

| Model | Bound scope | Rationale |
| --- | --- | --- |
| GPT-5.6 Luna Max | `openai/codex` | Consumes the OpenAI Codex subscription scope. |
| GPT-5.6 Sol High | `openai/codex` | Consumes the OpenAI Codex subscription scope. |
| GLM-5.3 Max | `zai/coding_plan` | Served by the GLM Coding Plan scope. |
| GLM-5.3-Flash Max | `zai/coding_plan` | Served by the GLM Coding Plan scope. |

Absence of an additional OpenAI binding is **deliberate**: M1 observed a live
additional bucket, but M2c has no evidence that Luna or Sol necessarily
consumes it, and no private/non-public live scope identifier is recorded.
Bindings are explicit catalog data, never inferred from `window_id`,
`limitName` or `normalModelSlug`. A future explicit catalog entry for a
reserve/provider-specific variant can add the corresponding binding when
evidence exists.

## Task profiles

The eight formal profile IDs are unchanged; each gained exactly one
selector-facing numeric definition (`task_profiles[].calibrated_requirement`
in `model-policy.json`; descriptive fields remain metadata). The pure
expansion mechanism is `TaskProfileDefinition.to_requirement()` /
`TaskProfileCatalog.resolve(profile_id)` in `scarcity_router/selection_types.py`.

| Profile | Level | Capability minima | Hard constraints |
| --- | --- | --- | --- |
| mechanical | L0 | tool_use ≥ 2, writing_editorial ≥ 2 | — |
| routine_coding | L1 | reasoning ≥ 2, coding ≥ 3, tool_use ≥ 3 | requires_tool_use |
| deep_coding | L3 | reasoning ≥ 4, coding ≥ 5, tool_use ≥ 4 | requires_tool_use, requires_reasoning_mode |
| scientific_review | L4 | reasoning ≥ 4, scientific_methodological ≥ 5, writing_editorial ≥ 4 | requires_reasoning_mode |
| editorial | L2 | reasoning ≥ 3, writing_editorial ≥ 5 | — |
| general_reasoning | L2 | reasoning ≥ 4, writing_editorial ≥ 3 | — |
| orchestration | L3 | reasoning ≥ 4, writing_editorial ≥ 5, tool_use ≥ 5 | requires_tool_use, requires_reasoning_mode |
| translation | L4 | writing_editorial ≥ 4, translation_multilingual ≥ 5 | — |

`translation` is L4 on purpose: the initial workflow's translation profile
means professional / technical / scientific / publication-quality translation
where semantic fidelity and terminology preservation materially matter. An
ordinary-translation profile can be added later if a real routing need appears.

## Expected capability-eligible scenarios

Capability-only expectations (ignoring scarcity, capacity and policy),
verified by `tests/test_model_calibration.py` with a test-only helper that
requires every specified minimum to pass against `effective_rating` and fails
unknown ratings. These are not final selector decisions.

| Profile | Eligible models |
| --- | --- |
| mechanical | Luna, Sol, GLM-5.3, GLM-5.3-Flash |
| routine_coding | Luna, Sol, GLM-5.3, GLM-5.3-Flash |
| deep_coding | Sol, GLM-5.3 |
| scientific_review | Sol |
| editorial | Luna, Sol |
| general_reasoning | Luna, Sol, GLM-5.3, GLM-5.3-Flash |
| orchestration | Luna, Sol |
| translation | Sol |

Critical implications of the calibration:

- GLM-5.3 can substitute for Sol on deep technical work.
- It cannot silently substitute for Sol on the initial scientific-review or
  publication-quality translation profile.
- Luna is a viable orchestration/editorial model.
- Flash remains a real professional-capability model, not an L0 toy.

## Evidence sources and rationale by model

Evidence hierarchy for this calibration: (1) first-party OpenAI model
documentation; (2) first-party Z.ai/ZCode model documentation; (3) Artificial
Analysis as independent comparative evidence; (4) explicit owner-observation
evidence for workflow-specific judgments. External evidence supports the
accepted calibration; it does not replace it, and no AA metric is mapped
onto the internal 1..5 scale. Evidence was collected in one bounded pass on
2026-09-06; `EvidenceRef.date` records the intrinsic source date where the
source is an announcement, otherwise the access date.

- **OpenAI (first-party).** The GPT-5.6 announcement positions Sol as the
  flagship for complex professional work — SOTA coding, knowledge work and
  science — with agentic emphasis (programmatic tool calling, computer use,
  multi-agent support), and Luna as the cost-efficient, fast tier. The API
  model documentation records the 1.05M context window, 128K max output,
  tool support and vision input for both entries.
  (`https://openai.com/index/gpt-5-6/`,
  `https://developers.openai.com/api/docs/models`)
- **Z.ai (first-party).** The GLM-5.3 release positions it as the flagship
  coding/agentic model — large coding gains over GLM-5.2, long-horizon and
  tool-heavy strength, `low|high|max` thinking effort with max recommended
  for coding, 1M-token evaluations — with benchmark results comparable to
  the GPT-5.6 frontier. The GLM-5.3 model guide
  (`https://docs.z.ai/guides/llm/glm-5.3`) records the Coding Plan
  availability, **text-only inputs**, 1M context window, **128K maximum
  output** and always-on reasoning with `low|high|max` efforts. The
  GLM-5.3-Flash guide
  (`https://docs.z.ai/guides/vlm/glm-5.3-flash`) shows always-on
  thinking with `reasoning_effort: max` recommended, native multimodal/visual
  understanding, 1M context, **128K maximum output**, strong tool calling,
  and coding/automation strength at flash cost.
  (`https://z.ai/blog/glm-5.3`,
  `https://docs.z.ai/release-notes/new-released`,
  `https://docs.z.ai/devpack/overview`,
  `https://docs.z.ai/guides/llm/glm-5.3`,
  `https://docs.z.ai/guides/vlm/glm-5.3-flash`)
- **Artificial Analysis (independent comparative).** The Intelligence Index
  places GLM-5.3 and GLM-5.3-Flash close to the GPT-5.6 frontier (GLM-5.3
  around 60, Flash around 57 on the published index; Flash also shows
  agentic parity with Sol max on AA's agentic index), supporting frontier
  closeness rather than a one-dimensional ordering.
  (`https://artificialanalysis.ai/models/gpt-5-6-sol`,
  `https://artificialanalysis.ai/models/comparisons/glm-5-3-vs-gpt-5-6-sol-xhigh`,
  `https://artificialanalysis.ai/models/glm-5-3-flash`)
- **Owner observation.** `accepted_workflow_role_calibration_2026-09-06`
  records the accepted real-workflow role assignment (no personal quota
  values, no account identity). It is the primary evidence where external
  benchmarks cover the owner's workflow poorly: writing/editorial fit,
  scientific-methodological adjudication, translation, orchestration, and
  practical coding/tool behavior.

No random SEO comparison pages, no benchmark aggregators with unclear
methodology, no social-media claims as primary evidence, and no vendor-price
as capability evidence were used.

## Model version dates

| Entry | `model_version_date` | Meaning |
| --- | --- | --- |
| GPT-5.6 Luna Max / Sol High | 2026-07-09 | First-party general-availability announcement of the GPT-5.6 family (Sol, Terra, Luna) across ChatGPT, Codex and the API; a limited Sol preview preceded it on 2026-06-26. |
| GLM-5.3 Max | 2026-08-14 | Z.ai GLM-5.3 announcement date (the release blog's evaluation data is reported as of 2026-08-14). Z.ai's developer release-notes entry is labeled 2026-08-18. |
| GLM-5.3-Flash Max | 2026-08-26 | GLM-5.3-Flash release date in Z.ai's developer release notes (corroborated by Artificial Analysis' model FAQ). |

Note: the M2c work order's expected GPT-5.6 date was 2026-07-29; first-party
evidence supports 2026-07-09 (GA), which the catalog records. No
`model_version` string is recorded — none is evidenced, and inventing one is
worse than leaving the optional field absent.

## Explicit uncertainty

- Workflow-specific dimensions (writing/editorial, translation,
  scientific-methodological adjudication, orchestration fit) rest primarily
  on owner observation; they carry `medium` confidence where external
  evidence is thin.
- Comparative confidence is highest for coding/reasoning/tool use, where
  first-party positioning and independent benchmarks agree.
- GLM-5.3's lack of vision input is a first-party documented current state
  ("text-only inputs"); a future first-party change would be a catalog
  update under the governance below, not a silent edit.
- AA index values drift with methodology versions; they are supporting
  comparative evidence only and are never mapped mechanically onto the
  internal scale.

## Update governance

Ratings are curated routing claims, not immutable truths. A later rating
change requires new evidence, a human-readable rationale, a
`catalog_version` increment, a `last_reviewed_on` update, tests showing the
routing impact, and human review. Ratings are not automatically rewritten
when a benchmark changes. The next implementation slice is M2d — scarcity and
policy primitives; U-007 (scarcity parameters) remains open.
