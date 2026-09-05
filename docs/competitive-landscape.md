# Competitive landscape

This is a map of useful prior art, not a mandate to match every feature. The
project should reuse sound concepts and protocols while keeping runtime
dependencies small. Reassess this document before public positioning because
these projects evolve quickly.

## GitHub Project HydraFusion

GitHub announced Project HydraFusion as a research-preview runtime orchestration
layer in Copilot in September 2026. It chooses among `single`, cascade and
critique execution patterns, can select models across providers, and optimizes a
quality/cost/latency trade-off while actually executing the workflow.

The strongest ideas to learn from are operational rather than branding:

- selectivity: use a compound workflow only when it is expected to improve the
  result;
- complete accounting across drafting, critique, revision, escalation, retry
  and fallback rather than pricing only the first call;
- bounded execution with explicit time/cancellation behavior;
- isolated read-only review rather than allowing the critic to mutate the
  workspace it is judging;
- fail-safe application when a workflow is cancelled or validation fails;
- validated workflow/model bindings and availability before execution;
- recording role, outcome, cost, latency and diagnostics for every leg.

HydraFusion is close enough to validate the broader market direction, but its
product boundary is materially different. It is a prompt-executing Copilot
runtime that constructs and runs multi-model workflows. Scarcity Router remains
an external recommendation/decision service whose differentiating inputs are:

```text
subscription quota headroom and reset/replenishment options
+ multiple rolling quota windows
+ provider availability and advisory health
+ explicit user reservations and provider blackout schedules
+ capability evidence with provenance
```

Scarcity Router does not need to beat HydraFusion at runtime orchestration. Its
value is deciding which scarce resources an external orchestrator should use,
and under what bounded execution envelope, without becoming the request path.
If compound workflow recommendations are added, `single`/cascade/critique are
useful archetypes rather than an implementation to clone. The service should
recommend explicit limits and expected aggregate consumption; the consuming
orchestrator remains responsible for execution and enforcement.

## CodexBar

[CodexBar](https://github.com/steipete/CodexBar) demonstrates broad subscription
usage telemetry, provider-specific collectors, usage windows and reset
visibility. Its strongest relevance is at the collection edge.

Learn from:

- small provider adapters;
- normalized usage presentation;
- fixture/contract testing;
- Codex app-server and Z.ai handling;
- endpoint and credential safety patterns.

Default posture: implement against the underlying provider protocol where
practical; do not make CodexBar a runtime dependency. It is MIT-licensed at the
time of this review. If code is copied or substantially adapted, preserve the
required attribution and notices. Independent use of ideas and patterns should
be documented without creating unnecessary coupling.

## Chuzom

[Chuzom](https://github.com/Chuzom/Chuzom) is relevant for task-complexity
classification and routing chains across subscription and premium models.

Learn from:

- understandable complexity levels;
- quota-preservation policy;
- quota/cost preservation concepts;
- explicit fallback ordering.

Scarcity Router differs by not proxying or executing the routed request and by
making live subscription capacity an independent normalized input. Chuzom is
MIT-licensed at the time of this review; the same attribution rule applies to
substantially adapted code.

## Herdr Model Capacity

The identified project is
[`shrivatsas/herdr-model-capacity`](https://github.com/shrivatsas/herdr-model-capacity).
Its relevant conceptual signal is the separation:

```text
provider -> account -> capacity windows -> normalized state
```

That decomposition supports Scarcity Router's decision to keep capacity telemetry
independent from task routing. The repository could not be fully reviewed
during M0, so implementation details and license must be verified before any
reuse. Treat it as conceptual prior art, not a dependency decision.

## OmniRoute

[OmniRoute](https://github.com/diegosouzapw/OmniRoute) demonstrates real demand
for quota-aware routing, provider health, capability discovery and integration
with coding agents. It is also intentionally a broad gateway ecosystem with a
single endpoint and automatic fallback.

Learn from the demand signal and operational vocabulary. Do **not** copy its
product scope: Scarcity Router must not become a full gateway, receive prompts,
pool model traffic or execute fallback calls.

## RouteLLM, Not Diamond and OpenRouter

- [RouteLLM](https://github.com/lm-sys/RouteLLM) studies and implements routing
  between stronger and weaker models, emphasizing quality/cost trade-offs and
  evaluation.
- [Not Diamond](https://notdiamond.ai/) represents learned/task-aware commercial
  model routing focused on achieving quality at lower cost.
- [OpenRouter Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router)
  selects a model for a prompt while OpenRouter also serves requests through a
  unified API/provider layer.

These are useful for routing theory, task classification, evaluation and the
strong/weak model problem. Their dominant frame is prompt-aware routing and API
economics. Scarcity Router's initial problem is different:

```text
existing subscription plans
+ multiple rolling quota windows
+ provider availability and health
+ explicit preservation of scarce capacity
+ user scheduling and preservation policy
+ no access to the prompt or model traffic
```

## Artificial Analysis as evidence, not a router

Artificial Analysis is not a direct routing competitor, but its documented data
API is useful prior art for maintaining a provenance-bearing capability catalog.
It provides stable model/creator identifiers plus benchmark, pricing, throughput
and latency data. These signals can help curate or challenge internal ratings.

The integration boundary should stay narrow:

- periodically cache relevant data rather than call it once per selection;
- use stable IDs and record source/version/freshness;
- treat benchmark metrics as evidence, not as an automatic replacement for the
  project's multidimensional capability model;
- never confuse public/API benchmark performance with current subscription
  capacity or the owner's observed workflow quality;
- keep its API credential server-side and outside repository files, fixtures,
  logs and agent prompts.

## Strategic conclusion

The opportunity is not “routing has never been done.” HydraFusion makes that
claim even less useful. The opportunity is a small, safe decision service for
**scarce subscription capacity and user-controlled preservation policy** that
integrates with existing orchestrators without becoming their gateway.

A useful positioning sentence is:

> Scarcity Router decides which capable resource you should spend now; the
> orchestrator decides how to execute the task.

Over time the decision may include a bounded workflow envelope as well as a
single model, but subscription headroom, provider availability, replenishment
options, time-based provider policy and explainability remain the distinctive
center of gravity.

Before copying any implementation, verify the exact source revision and license,
record provenance, and preserve required notices. Prefer protocol-level
interoperability and independently written adapters.
