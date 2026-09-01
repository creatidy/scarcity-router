# Competitive landscape

This is a map of useful prior art, not a mandate to match every feature. The
project should reuse sound concepts and protocols while keeping runtime
dependencies small. Reassess this document before public positioning because
these projects evolve quickly.

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
classification and routing chains across local, subscription and premium
models.

Learn from:

- understandable complexity levels;
- local-versus-premium policy;
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
+ local models
+ explicit preservation of scarce capacity
+ no access to the prompt or model traffic
```

## Strategic conclusion

The opportunity is not “routing has never been done.” The opportunity is a
small, safe decision service for subscription headroom that integrates with
existing orchestrators without becoming their gateway. Originality is not an
engineering goal; clarity of boundary and usefulness in the real workflow are.

Before copying any implementation, verify the exact source revision and license,
record provenance, and preserve required notices. Prefer protocol-level
interoperability and independently written adapters.

