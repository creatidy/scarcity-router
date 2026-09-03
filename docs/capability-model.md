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

The canonical machine-readable definitions for capability classes, profile
vocabulary, class/profile relationships and current workflow exemplars live in
[`model-policy.json`](../model-policy.json). This document keeps the conceptual
model and selector-facing rules; it does not duplicate that policy artifact.

## Hard properties and constraints

Some requirements are categorical or numeric, not quality scores:

- minimum input context;
- minimum/required output allowance;
- tool-calling support;
- vision support;
- reasoning mode;
- local-only or cloud allowed/disallowed;
- required provider, model or variant;
- privacy boundary;
- runtime availability.

Selection filters hard failures before comparing capability or scarcity. A
high reasoning score cannot compensate for missing vision or insufficient
context.

## Profiles

Profiles are data-driven aliases for requirements. Initial candidates are:

- `mechanical`;
- `routine_coding`;
- `deep_coding`;
- `scientific_review`;
- `editorial`;
- `general_reasoning`;
- `orchestration`;
- `translation`.

Conceptually:

```yaml
deep_coding:
  minimum:
    coding: 4
    reasoning: 4
    tool_use: 3
```

This example shows structure, not accepted ratings or final file syntax.
Advanced clients may supply raw capability minima and hard constraints directly.
Profile definitions must live in one catalog/config source, not duplicated in
CLI, REST, MCP or selector branches.

## Model entries

An initial model entry needs:

- stable provider/model/variant identity;
- hard properties and supported modes;
- capability ratings by dimension;
- assessment provenance and rationale;
- model/version date;
- confidence;
- last review date;
- human override record where applicable.

The initial catalog is restricted to models in the real workflow: GPT-5.6 Luna,
GPT-5.6 Sol, GLM-5.3, GLM-5.3-Flash and the local Qwen3.8 27B configuration.
Exact ratings are deliberately unresolved in M0 and must be established as a
separate, reviewable M2 artifact. They must not be inferred solely from price or
vendor marketing.

## Governance and uncertainty

Capability assessments are subjective curated evidence. Changes require a
human-readable diff, source, rationale, confidence and date/version. Marketing,
anecdotes and benchmarks may inform an assessment, but no single one becomes
ground truth automatically.

Do not imply false precision. The first catalog only needs enough resolution to
make trusted decisions in the owner's workflow. A continuously curated catalog
may later become a product asset, but building a universal leaderboard is out
of scope.
