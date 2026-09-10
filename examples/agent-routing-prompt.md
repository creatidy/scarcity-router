# Dynamic model routing with Scarcity Router

You have access to the local MCP server:

```text
scarcity-router
```

with these tools:

```text
scarcity_status
scarcity_select
scarcity_simulate
```

Use Scarcity Router to choose the model/configuration for AI work dynamically.

Do not maintain or follow a static mapping such as:

```text
coding -> model A
review -> model B
translation -> model C
```

unless the project explicitly requires a particular model as a hard constraint.

The project decides **what capability the task requires**.

Scarcity Router decides **which currently available model/configuration should perform it**.

## When to query Scarcity Router

Call `scarcity_select` at a **dispatch boundary**: the start of a new bounded
unit of AI work.

Examples of new dispatch boundaries:

```text
start implementation
start an independent review
start scientific/methodological adjudication
start translation
start a materially different project phase
reroute after a real provider/dispatch failure
```

Do NOT query Scarcity Router:

```text
before every prompt
before every file edit
before every tool call
before every conversational turn
repeatedly while the same bounded work unit is progressing normally
```

The selected configuration is **sticky for the current bounded work unit**.
Capacity changing while work is already progressing is not by itself a reason
to restart or reroute the work.

## Normal routing procedure

Before starting a bounded work unit:

1. Determine what kind of task this actually is.
2. Select the smallest Scarcity Router profile that honestly describes its requirements.
3. Call `scarcity_select`.
4. Read the returned decision.
5. Dispatch the work to the selected configuration.
6. Keep that assignment for the bounded work unit.

Use the existing profiles approximately as follows:

```text
mechanical
    deterministic repository operations, simple formatting,
    low-reasoning mechanical work

routine_coding
    localized implementation, ordinary tests, straightforward debugging,
    clear requirements and limited architectural impact

deep_coding
    difficult debugging, architecture, cross-cutting implementation,
    contract changes, security-sensitive or reasoning-heavy engineering

scientific_review
    scientific or methodological adjudication, evidence interpretation,
    consequential technical/scientific review

editorial
    authoring, restructuring and professional prose where translation is
    not the primary task

general_reasoning
    analysis and synthesis that do not fit a more specialized profile

orchestration
    planning, coordinating work, cross-document synthesis and deciding
    how bounded units should be structured

translation
    high-quality multilingual translation or language editing
```

Do not choose `deep_coding` merely because a task is large or important.

Do not choose `scientific_review` merely because the project is scientific.

Choose according to the capability actually required by this work unit.

## Calling `scarcity_select`

When a calibrated profile is sufficient, call:

```text
scarcity_select
profile_id = <profile>
```

For example:

```text
scarcity_select
profile_id = routine_coding
```

or:

```text
scarcity_select
profile_id = scientific_review
```

Do not call `scarcity_status` first merely to decide which model is available.
`scarcity_select` is the routing operation and evaluates current capacity as
part of the selection path.

Use `scarcity_status` separately when you need to diagnose or explain provider
capacity.

Use `scarcity_simulate` only for explicit what-if analysis. It is not the
normal dispatch mechanism.

## When a profile is not sufficient

If the task has requirements that a standard profile does not represent, use
an explicit requirement or a profile plus valid tightening.

Examples include requirements for:

```text
vision
tool use
minimum context
specific provider/model
specific privacy property
higher capability minimum
```

Never weaken an existing profile merely to obtain a cheaper or more available
model.

If you cannot express a material task requirement without guessing, ask the
human rather than inventing selector semantics.

## How to interpret the result

A normal successful decision contains a selected candidate.

Use the returned:

```text
decision.selected.identity.provider
decision.selected.identity.model
decision.selected.identity.variant
```

as the selected configuration. Treat the identity as opaque configuration data.
Do not infer capability, reasoning effort or quota semantics by parsing the
model name or `variant`.

Also preserve:

```text
catalog_version
profile_policy_version, when present
degraded
```

for compact routing provenance.

The decision may also contain `alternatives`, `excluded` and `reason_codes`.
`alternatives` are eligible candidates already ordered by the selector.
`excluded` candidates are NOT fallbacks. Do not dispatch them.

## No-solution result

If:

```text
decision.selected = null
```

Scarcity Router found no candidate that satisfies the active requirements and
policy.

Do NOT:

```text
lower capability requirements
ignore a hard constraint
pick the historically preferred model
choose an excluded candidate
pretend that an unavailable provider is usable
```

Report:

```text
SCARCITY_ROUTER_NO_SOLUTION
```

and ask the human how to proceed.

## Dispatch through the execution harness

Scarcity Router recommends a configuration. It does not execute the task.

If the execution harness supports the selected configuration, dispatch the
bounded work unit using the exact returned provider/model/variant.

If the current session already runs that configuration, continue in the current
session. If a new worker session is required, create it with the selected
configuration.

Do not silently replace the result with your preferred model.

## Harness cannot dispatch the selected configuration

A model may be selector-eligible while the current execution harness cannot
launch it.

In that case report:

```text
SELECTED_BY_ROUTER = <provider/model/variant>
HARNESS_DISPATCH_AVAILABLE = NO
```

Inspect the ordered `alternatives`.

You may use the first alternative that is returned as eligible by Scarcity
Router and is actually dispatchable by the current harness.

Record that this was a harness fallback, not the primary Scarcity Router
selection. Never use an `excluded` candidate.

If neither the selected candidate nor any eligible alternative can be
dispatched:

```text
STOP_AND_ASK_HUMAN
```

Do not create a permanent local fallback table.

## MCP unavailable

If the `scarcity-router` MCP server itself is unavailable, do not fabricate
current capacity or pretend dynamic routing succeeded.

Report:

```text
DYNAMIC_ROUTING_AVAILABLE = NO
SCARCITY_ROUTER_MCP = UNAVAILABLE
```

If the project defines an explicit emergency static fallback policy, it may be
used only under that policy and must be visibly recorded as:

```text
ROUTING_MODE = STATIC_EMERGENCY_FALLBACK
```

Otherwise ask the human.

## Independent review

Implementation and independent review are separate dispatch boundaries.
Do not automatically reuse the implementation model for the review.

Before starting the reviewer session, classify the review task and call
`scarcity_select` again.

For example:

```text
ordinary implementation review -> deep_coding
scientific/methodological review -> scientific_review
language/translation review -> translation
```

The review remains a distinct session with a frozen artifact/head even if
Scarcity Router happens to select the same model configuration.

If project policy explicitly requires a different model family and the current
selector cannot express that constraint safely, ask the human rather than
ignoring the independence requirement.

## When to reroute an in-progress unit

Do not reroute merely because a later capacity observation might produce a
different recommendation.

Re-query during the current work unit only after a material event such as:

```text
actual provider or dispatch failure
demonstrated insufficient model capability
material change in task requirements
explicit human request to reconsider routing
```

Otherwise finish the bounded unit and query again at the next dispatch boundary.

## Routing provenance

For meaningful work, record only compact provenance such as:

```text
routing_source: scarcity-router-mcp
profile: deep_coding
selected: provider/model/variant
catalog_version: <version>
profile_policy_version: <version if present>
runtime_identity: DISPATCH_VERIFIED | RUNTIME_VERIFIED |
                  RUNTIME_UNOBSERVABLE | FAIL
```

Do not persist routine personal capacity details such as:

```text
quota percentages
reset timestamps
private provider scope identifiers
raw telemetry
```

unless the project explicitly requires them.

## Core rule

When deciding which AI model should perform a new bounded unit of work:

```text
describe the task
        ↓
ask Scarcity Router
        ↓
use the selected capable configuration
        ↓
keep it sticky for that work unit
```

Do not substitute static model preference for dynamic routing.
