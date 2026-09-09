# LLM Operating Policy

## Purpose

This document is the authoritative human-readable policy for multi-model work
performed around Scarcity Router. It governs role assignment, task routing,
authoring, review, evidence handling, durable progress, provenance and
bounded recovery. It does not expand the active selector catalog.

The machine-readable companion is [`model-policy.json`](../model-policy.json).
The active selector identities and capability calibration remain authoritative
in [`model-catalog.json`](../model-catalog.json) and
[`docs/model-calibration.md`](model-calibration.md).

## Core principles

- Keep model capability, task requirements, runtime capacity and user policy as
  separate concepts.
- Choose the least costly or least scarce model that is fully capable of the
  task, but never sacrifice a required capability merely to save quota.
- Capability minima and hard constraints must all pass. Quota never changes a
  capability rating, and availability is not capability.
- Model classes are non-exclusive archetypes, not rankings. Class membership
  does not establish selector eligibility.
- Execution generalists are capable professional workers, not
  mechanical-only or low-capability models. Escalate because of difficulty,
  not task size, prestige or file count.
- Translation and multilingual quality are first-class concerns.
- Scarcity Router recommends models and bounded alternatives; it does not
  execute model calls or autonomous fallback workflows.

## Stable capability archetypes

The provider-independent archetypes remain:

- `EXECUTION_GENERALIST`
- `ORCHESTRATION_SYNTHESIS`
- `DEEP_TECHNICAL_REASONER`
- `SCIENTIFIC_METHODOLOGICAL_SPECIALIST`
- `TRANSLATION_EDITORIAL_SPECIALIST`

These are descriptive lenses for task fit. The selector still resolves explicit
task requirements, filters hard constraints, requires every capability minimum,
evaluates applicable capacity and policy, ranks sufficient candidates and
explains the result. It never chooses directly from an archetype.

## Current reference model mapping

These are the owner's dated operating references, not catalog entries or
capability ratings:

### GPT-5.6 Luna / OpenAI / max

Primary roles are `PROGRAM_ORCHESTRATOR`, `ORCHESTRATION_SYNTHESIS` and
`EDITORIAL_AUTHOR`. It is also a general non-specialist execution fallback and
the execution fallback when the preferred execution model is unavailable. It
fits orchestration, long-context synthesis, authoring, editorial restructuring,
ordinary repository work, mechanical evidence preparation and QA.

### GPT-6 Astra / OpenAI / low

The current reference specialist is `SCIENTIFIC_METHODOLOGICAL_SPECIALIST`,
`DEEP_TECHNICAL_REASONER` and `SEMANTIC_FIGURE_REVIEWER`. It fits
scientific/methodological review, expert evidence adjudication, difficult
cross-document reasoning, difficult technical reasoning, semantic scientific
figure review and final whole-project expert review. Its default approved
reasoning effort is `low`; low effort is an effort setting, not a low
capability rating.

Astra appears in reference assignments only. It is not selector-eligible until
separate capability, hard-property and capacity-applicability evidence is
calibrated and accepted.

### GPT-5.6 Sol / OpenAI / high

The primary role is `TRANSLATION_EDITORIAL_SPECIALIST` and
`PL_TRANSLATION_EDITOR`. Sol is an authorized alternate for
`SCIENTIFIC_METHODOLOGICAL_SPECIALIST`, including an independent second
opinion, targeted scientific confirmation and continuity with Sol-reviewed
artifacts. A second specialist review is not automatic for every Astra review.

### GLM-5.3-Flash / Z.ai / max

The primary role is `EXECUTION_GENERALIST` and
`ROUTINE_EXECUTION_WORKER`. It fits coding, repository operations, extraction,
evidence preparation, tests, QA, normal debugging, routine implementation and
high-throughput technical work. It is not a mechanical-only or low-capability
model.

### GLM-5.3 / Z.ai / max

The primary role is `DEEP_TECHNICAL_REASONER`. Use it when Flash lacks
reasoning margin or the task is intrinsically difficult. Escalation is caused
by difficulty, not by size.

## Task routing

Task routing is role- and requirement-driven. A role assignment is a useful
starting reference; it does not override the active task profile, hard
constraints, capability minima, capacity applicability or user reservations.

The active selector remains the least-scarce-sufficient implementation. Catalog
v2 (D-032) contains Luna Medium/Max, Terra Medium, Sol Medium/High and
GLM-5.3/GLM-5.3-Flash Max. Existing M2 vectors are preserved; the three new
Medium configurations have explicit approved calibration. Reference operating assignment and active
selector eligibility are different layers: Astra is now a preferred external
operating assignment, but Scarcity Router cannot recommend it until
catalog/capacity onboarding is complete.

## Implementation vs judgment

For consequential work, select the smallest workflow that provides the needed
confidence. A useful pattern is:

```text
execution/preparation
-> authoring where needed
-> independent specialist review for consequential judgment
-> finite mechanical remediation
-> narrow specialist recheck of the meaningful delta
-> translation after source verification
-> final mechanical QA
```

This is a pattern, not an unconditional eight-stage workflow. Purely
mechanical work does not automatically require specialist review. The selected
workflow must have explicit stop conditions and a finite budget.

## Review independence

Consequential review uses a distinct session from the author or implementer.
A different model family is useful when it materially improves independence,
but is not mandatory in every case. A second complete specialist review is not
the default. Use one only when uncertainty remains, evidence conflicts, the
consequence is unusually high or independent confirmation materially increases
confidence.

The existing Scarcity Router governance remains authoritative: freeze one
immutable reviewed head, serialize worker/reviewer/remediation phases, allow
one initial review, at most one remediation and one narrow final verification,
then stop at the human merge gate. Review independence does not expand scope.

## Evidence work

**LLM OUTPUT IS NEVER EVIDENCE.** Models may locate, extract, organize, build
candidate evidence packets and identify contradictions. Candidate evidence is
not adjudicated support.

Keep these roles separate:

- `EVIDENCE_PREPARATION` extracts candidate sources and builds a ledger.
- `SCIENTIFIC_METHODOLOGICAL_REVIEW` decides whether the evidence actually
  supports the claim and whether the method is correct.

An execution worker must not silently transform `candidate source` into
`source supports claim` unless adjudication is explicitly within its assigned
expert role. A specialist recheck should cover only a scientifically
meaningful delta when the source has not materially changed.

## Incremental durable work

Large artifacts and analyses are produced in bounded units:

```text
bounded unit
-> inspect
-> extract/reason
-> durable write/checkpoint
-> validate
-> next unit
```

The first durable write should happen early. Do not require a model to hold a
whole analysis in context before writing an artifact, checkpoint or commit.

## Durable state

Before assuming an interrupted or stalled task failed, inspect:

- actual model output;
- observable runtime metadata;
- filesystem and worktree state;
- commits and uncommitted changes; and
- durable artifacts and validation checkpoints.

Durable repository and artifact state outrank session UI state. A dispatch is
not proof of completion, and a hanging session card is not proof of failure.

## Model provenance and attestation

Execution governance uses these states:

- `REQUESTED`: the controller requested a provider/model/variant/effort
  assignment.
- `DISPATCH_VERIFIED`: machine-visible dispatch configuration matches the
  requested assignment.
- `RUNTIME_VERIFIED`: generated-turn or runtime metadata independently confirms
  the required assignment.
- `RUNTIME_UNOBSERVABLE`: runtime identity cannot be independently observed;
  this is not automatically failure.
- `FAIL`: observed runtime identity conflicts with the required model-bound
  assignment.

Do not infer runtime identity from a prompt, task title, UI label alone or model
self-report. Historical provenance is immutable; changing today's assignment
does not rewrite historical records.

Scarcity Router currently recommends models but does not execute them. Runtime
attestation is therefore execution-harness/orchestration governance, not a new
`SelectionDecision` field and not selector implementation in this task.

## Retry / anti-loop policy

Retries are finite. A retry requires a concrete diagnosis or a materially
changed strategy. Progress means new durable state, such as an artifact,
meaningful commit, advanced gate, new concrete blocker or validated checkpoint.
Do not repeat identical failures.

The project default remains one initial review, one remediation and one narrow
final verification. Worker and reviewer retries remain bounded as specified in
`model-policy.json` and `AGENTS.md`. Budget exhaustion or a complexity breach
stops the task and escalates to a human.

## Review/remediation convergence

Review findings are classified as `MERGE_BLOCKER` or `DEFER`. Remediation is
mechanical where possible and is limited to the one permitted round. Final
verification checks the identified blockers and obvious remediation
regressions; it is not an invitation to start a new architecture review. A
moving branch invalidates a review result.

## Completed work stays completed

Do not redo a passed gate, adjudicated claim, confirmed remediation, accepted
translation or validated build unless the underlying source materially changed
or a later gate identifies a concrete regression. A fresh session or model is
not a reason to repeat completed work.

## Scarcity and availability

Scarcity is evaluated only after hard constraints and capability sufficiency,
over every applicable capacity scope and relevant quota window. Unknown
applicability remains explicit. Prefer the least scarce sufficient candidate,
but never use availability to compensate for a capability deficit. Reservations
protect scarce capacity without declaring a capable model intrinsically weak.

## Reasoning-effort policy

Reasoning effort is a routing parameter. Use the lowest reasoning effort that
reliably satisfies the task. Escalate effort only when a bounded attempt shows
insufficient reasoning margin, the problem demonstrates unresolved difficulty,
or there is evidence that additional depth is likely to materially help.

Do not escalate merely because a task is important, large, expensive or
prestigious. The current reference default for GPT-6 Astra is `low`.

D-032 implements effort-aware ranking among explicitly calibrated catalog
configurations, not arbitrary dynamic effort generation. The selector compares
known capacity, scarcity penalty, capability margin, lowest configured effort,
preference and identity in that order after all eligibility gates. Effort comes
from the catalog field, never from opaque variants. Existing GLM Max, Luna Max,
Sol High and Flash Max capability vectors remain unchanged. The model-policy
schema remains v1; compatible policy content is policy_version 6 because
`reasoning_effort_policy.selector_support` changed from `not_implemented` to
`calibrated_configurations`. Task profiles, profile minima and dated reference
assignments are unchanged.

## Concurrency

Prefer a small ready queue. Parallelize only genuinely independent lanes whose
state cannot collide. Do not maximize agent count or use concurrency as a
substitute for durable checkpoints and bounded orchestration.

## Durable role names and assignment layer

Stable role IDs are model-independent:

```text
PROGRAM_ORCHESTRATOR
ROUTINE_EXECUTION_WORKER
EDITORIAL_AUTHOR
DEEP_EXECUTION_WORKER
SCIENTIFIC_REVIEWER
SEMANTIC_FIGURE_REVIEWER
PL_TRANSLATION_EDITOR
```

The dated `workflow_role_assignments` section in `model-policy.json` records
the owner's current descriptive references and authorized alternates. It is
revisable metadata, is not selector-facing and establishes neither model
eligibility nor capacity binding.

## Project-specific overrides

The reusable generic operating principle is that local inference may be used
when it is stable and sufficient and disabled when it is unstable. Scarcity
Router has the stricter accepted project override in D-017: local inference,
including Ollama, is completely unsupported. Restoring it requires a new
explicit product decision. No local model is in the active catalog.

The repository's multi-agent policy is also stricter than a generic two-round
remediation suggestion. The one-remediation default, immutable review head,
serialized phases and human merge gate remain in force.

## Failure handling

Before rerouting or restarting, ask: **did the model fail, or did the
execution harness fail?** Inspect durable output, runtime evidence, disk state,
Git state and artifacts first. Then choose exactly one bounded response:

- recover existing output;
- retry with a changed strategy;
- route to another capable model; or
- stop with a concrete blocker.

Never reflexively restart from zero.

## Current practical routing

The current dated reference assignments are:

```text
PROGRAM_ORCHESTRATOR -> GPT-5.6 Luna / OpenAI / max
ROUTINE_EXECUTION_WORKER -> GLM-5.3-Flash / Z.ai / max; Luna fallback
EDITORIAL_AUTHOR -> GPT-5.6 Luna / OpenAI / max
DEEP_EXECUTION_WORKER -> GPT-6 Astra / OpenAI / low reference; GLM-5.3 / Z.ai / max alternate
SCIENTIFIC_REVIEWER -> GPT-6 Astra / OpenAI / low reference; GPT-5.6 Sol / OpenAI / high alternate
SEMANTIC_FIGURE_REVIEWER -> GPT-6 Astra / OpenAI / low reference; GPT-5.6 Sol / OpenAI / high alternate
PL_TRANSLATION_EDITOR -> GPT-5.6 Sol / OpenAI / high; GPT-6 Astra / OpenAI / low alternate
```

These references are not a promise that every named model is currently
available or selector-eligible.

## Scarcity Router product boundary

Scarcity Router reads normalized subscription capacity, evaluates task
requirements and user policy, and recommends a model with ranked alternatives
and an explanation. It does not proxy prompts, execute models, inspect
repositories for clients, dispatch fallback calls, manage credentials or
become a generic LLM gateway. REST, MCP and any future dashboard must call the
same authoritative core; they must not introduce execution or role-assignment
logic of their own.
