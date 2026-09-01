# Scarcity Router

> **Use the best AI model you can afford to spend right now.**

Scarcity Router is a planned local-first, open-source service that reads current AI
subscription capacity, combines it with model capabilities and a user's policy
for preserving scarce models, and recommends which model an agent should use.

The core rule is simple:

> **Choose the least scarce model that is capable enough for the task.**

The project is currently in **M0: documentation and contract design**. There is
no installable or executable broker yet. Commands and responses below are
target UX, not claims about implemented behavior.

## Why it exists

An orchestrator can consume little quota itself while dispatching substantial
work to a child model. Static mappings cannot react when one subscription has
98% of its five-hour window remaining but only 2% of its weekly window
remaining. Scarcity Router is intended to make that changing scarcity visible and
actionable without editing repository policy every day.

The initial real-world environment is deliberately narrow:

- OpenAI subscription capacity exposed by Codex app-server;
- Z.ai Coding Plan capacity exposed by its read-only usage endpoint;
- a local Ollama model, initially `qwen3.8:27b-3090-q4km-160k`.

## Product boundary

Scarcity Router will inspect capacity, evaluate task requirements and recommend a
model with ranked fallbacks and an explanation.

It will not:

- proxy prompts or model responses;
- read source code or repository contents;
- copy browser sessions;
- execute model calls or coding tasks;
- replace Codex, Kilo, Claude Code or another orchestrator;
- become a generic LLM gateway.

Credentials remain at their existing local source whenever practical. They
must never appear in logs, fixtures, API responses or agent context.

## Target experience

The first useful interaction should make capacity obvious:

```text
$ Scarcity Router status

Provider       5h remaining   Weekly remaining   State
OpenAI              94%              48%         NORMAL
Z.ai                98%               2%         CRITICAL
Qwen local        n/a               n/a           READY
```

Selection should be equally direct and explainable:

```text
$ Scarcity Router select deep-coding --level 4 --explain

SELECTED
  GPT-5.6 Luna Max

WHY
  task requires deep-coding at L4
  selected model satisfies the hard and capability requirements
  OpenAI weekly capacity is acceptable
  Z.ai weekly capacity is critical and protected

ALTERNATIVES
  1. Qwen3.8 local — available, lower capability margin
  2. GLM-5.3 — capable, but currently reserved
```

These examples illustrate the intended contract. Values are illustrative, not
live readings.

## Architecture at a glance

Four inputs remain independent:

1. **Model capability** — what a model can reliably do.
2. **Task requirement** — what this task needs.
3. **Runtime capacity** — what quota or local availability exists now.
4. **User policy** — which scarce resources should be preserved.

Provider collectors normalize telemetry at the edge. A provider-independent
selector applies hard constraints, capability requirements, scarcity and user
policy. CLI, REST and MCP adapters expose the same core decision; none owns
business logic.

## Documentation map

Each topic has one primary source of truth:

| Topic | Authoritative document |
| --- | --- |
| Product purpose, users and boundaries | [`docs/product.md`](docs/product.md) |
| Components and dependency boundaries | [`docs/architecture.md`](docs/architecture.md) |
| Runtime quota and local availability | [`docs/capacity-model.md`](docs/capacity-model.md) |
| Task levels, profiles and model capabilities | [`docs/capability-model.md`](docs/capability-model.md) |
| Eligibility, scarcity, reservation and ranking | [`docs/selection-policy.md`](docs/selection-policy.md) |
| Provider adapter expectations | [`docs/providers.md`](docs/providers.md) |
| Security invariants and threat boundaries | [`docs/security.md`](docs/security.md) |
| Experimentally established facts | [`docs/poc-evidence.md`](docs/poc-evidence.md) |
| Related projects and differentiation | [`docs/competitive-landscape.md`](docs/competitive-landscape.md) |
| Milestones and exit criteria | [`docs/roadmap.md`](docs/roadmap.md) |
| Accepted and unresolved decisions | [`docs/decisions.md`](docs/decisions.md) |
| Instructions for future agents | [`AGENTS.md`](AGENTS.md) |

## Current project choices

- Intended license: Apache License 2.0.
- Intended public hosting: GitHub canonical, Forgejo automatic mirror.
- `Scarcity Router` is a working name pending a collision and naming search.
- Likely implementation stack: Python 3.12+, `uv`, `pytest`, typed schemas, a
  small CLI, a small HTTP layer and the official MCP SDK. This is not binding.

See the [roadmap](docs/roadmap.md) before starting implementation. M0 contains
documentation only; M1 begins with normalized status collectors, not routing.

