# Scarcity Router

> **Use the best AI model you can afford to spend right now.**

Scarcity Router is a planned open-source decision service that reads current
subscription capacity, combines it with model capabilities and user policy, and
recommends which model an agent should use.

The core rule is simple:

> **Choose the least scarce model that is capable enough for the task.**

The project completed **M0: documentation and contract design** on 2026-09-01,
**M1: capacity collectors and normalized status** on 2026-09-05, and all
five M2 implementation slices on 2026-09-06: **M2a: semantic capacity
scopes** (capacity contract v3), **M2b: TaskRequirement and ModelCatalog
core types**, **M2c: curated initial ratings and profile calibration**,
**M2d: scarcity and policy primitives** and **M2e: deterministic selector,
explanation and simulation**. The two subscription collectors, the
provisional `status` command, the `select` command (with `--explain` and
`--json`) and the `simulate` command are implemented. The calibrated
catalog lives in [`model-catalog.json`](model-catalog.json) with rationale
in [`docs/model-calibration.md`](docs/model-calibration.md); the selector
and simulation live in `scarcity_router/selector.py` and
`scarcity_router/simulation.py` with the frozen ranking semantics
documented in [`docs/selection-policy.md`](docs/selection-policy.md).
M2 implementation is complete; M2 exit/live acceptance in the owner's real
workflow is still pending. There is not yet an installable package, final
executable name, REST service or MCP server.

## Why It Exists

An orchestrator can consume little quota itself while dispatching substantial
work to a child model. Static mappings cannot react when one subscription has
98% of its five-hour window remaining but only 2% of its weekly window
remaining. Scarcity Router is intended to make that changing subscription
scarcity visible and actionable without editing repository policy every day.

## Initial Supported Environment

The initial real-world environment is deliberately narrow:

- OpenAI subscription capacity exposed by Codex app-server;
- Z.ai Coding Plan capacity exposed by its read-only usage endpoint.

There is no supported local inference provider. Restoring one requires a new
explicit product decision.

## Product Boundary

Scarcity Router will inspect capacity, evaluate task requirements and recommend a
model with ranked fallbacks and an explanation.

It will not:

- proxy prompts or model responses;
- read source code or repository contents;
- copy browser sessions;
- execute model calls or coding tasks;
- replace Codex, Kilo, Claude Code or another orchestrator;
- become a generic LLM gateway.

Credentials remain at their existing provider-local source whenever practical.
They must never appear in logs, fixtures, API responses or agent context.

## Target Experience

The first useful interaction should make subscription capacity obvious:

```text
$ Scarcity Router status

Provider       5h remaining   Weekly remaining   State
OpenAI              94%              48%         NORMAL
Z.ai                98%               2%         CRITICAL
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
  1. GLM-5.3 — capable, but currently reserved
```

The selection example remains target UX styling for a later milestone. The
implemented M2e development interface is the provisional module CLI below:
`status`, and `select`/`simulate` with the exact `balanced` ranking
documented in [`docs/selection-policy.md`](docs/selection-policy.md).

## M1 Status

### Prerequisites

- Python 3.12 or newer.
- `uv`.
- A supported local Codex app-server installation for OpenAI status, when that
  provider is needed.
- An existing configured Kilo `zai-coding-plan` credential for Z.ai status,
  when that provider is needed.

Install the repository's development tooling with:

```bash
uv sync --only-dev
```

The final package and executable name remain unresolved under U-008. Until that
decision is made, invoke the provisional module surface directly.

### Human-Readable Status

Run:

```bash
uv run python -m scarcity_router status
```

The command performs one fresh sequential collection in `openai`, then `zai`
order. It uses one shared UTC millisecond observation timestamp. A
representative safe output is:

```text
Observed at 2026-09-05T09:00:00.123Z
Provider openai status=ok plan=plus
  window kind=five_hour resource=tokens used=35% remaining=65% reset=2026-09-05T12:00:00.000Z scope=codex id=primary
  window kind=weekly resource=tokens used=52% remaining=48% reset=2026-09-12T09:00:00.000Z scope=codex id=secondary
Provider zai status=auth_required
  windows=none
  diagnostics=auth_required
```

All displayed values come from normalized snapshots. Unknown or exhausted
windows remain explicit, and degraded provider diagnostics are shown without raw
response bodies, credentials, paths, subprocess text or account data.

Status issues no model prompt and does not intentionally consume inference
quota. OpenAI capacity collection normally performs only telemetry reads;
after the evidenced app-server `-32603` internal-error condition it may
request one provider-managed credential refresh and retry the read once
(`docs/decisions.md` D-018). The token itself is never read, stored or
exposed by Scarcity Router.

### JSON Status

Use `--json` for the same ordered snapshots in machine-readable form:

```bash
uv run python -m scarcity_router status --json
```

The result is a JSON array containing exactly the OpenAI and Z.ai normalized
`CapacitySnapshot.to_dict()` values. The internal capacity contract is schema
v3; this is not a competing provider-specific schema.

Operational provider states such as `unavailable`, `auth_required`,
`unsupported`, `schema_changed`, `unknown` and an exhausted window produce
status output and exit 0. Internal or contract failures exit non-zero.

### Select And Simulate

Recommend the least scarce capable model for a calibrated task profile:

```bash
uv run python -m scarcity_router select --profile routine_coding
uv run python -m scarcity_router select --profile deep_coding --explain
uv run python -m scarcity_router select --profile deep_coding --json
```

Requirements can also be supplied explicitly or tighten a profile
monotonically:

```bash
uv run python -m scarcity_router select --requirement task.json
uv run python -m scarcity_router select --profile deep_coding --tighten stricter.json
```

Optional flags: `--selector-policy FILE` (defaults to the documented neutral
policy), `--replenishment FILE` (normalized `ReplenishmentState` list),
`--catalog FILE` and `--model-policy FILE` (defaults to the repository-root
artifacts). A valid no-solution decision is a legitimate result and exits 0.

Simulation applies typed overrides to copies of the current inputs and runs
the SAME selector core for the CURRENT and SIMULATED decisions:

```bash
uv run python -m scarcity_router simulate \
  --profile routine_coding --overrides simulation.json
```

with an overrides file such as (synthetic values):

```json
{
  "capacity_percentages": [
    {
      "provider": "zai",
      "scope_id": "coding_plan",
      "resource": "tokens",
      "kind": "weekly",
      "remaining_percent": 2
    }
  ]
}
```

Selection issues no model prompt and does not intentionally consume
inference quota. It reuses the existing status telemetry path, including the
bounded provider-managed OpenAI auth recovery accepted in D-018. Live
reset-credit acquisition is not implemented: reset credits are visible only
when a normalized `ReplenishmentState` file is supplied.

## Architecture At A Glance

Four inputs remain independent:

1. **Model capability** — what a model can reliably do.
2. **Task requirement** — what this task needs.
3. **Subscription capacity** — what provider quota exists now.
4. **User policy** — which scarce resources should be preserved.

Provider collectors normalize telemetry at the edge. A provider-independent
selector applies hard constraints, capability requirements, scarcity and user
policy. CLI, REST and MCP adapters expose the same core decision; none owns
business logic.

## Documentation Map

Each topic has one primary source of truth:

| Topic | Authoritative document |
| --- | --- |
| Product purpose, users and boundaries | [`docs/product.md`](docs/product.md) |
| Components and dependency boundaries | [`docs/architecture.md`](docs/architecture.md) |
| Subscription quota and provider capacity | [`docs/capacity-model.md`](docs/capacity-model.md) |
| Task levels, profiles and model capabilities | [`docs/capability-model.md`](docs/capability-model.md) |
| Eligibility, scarcity, reservation and ranking | [`docs/selection-policy.md`](docs/selection-policy.md) |
| Provider adapter expectations | [`docs/providers.md`](docs/providers.md) |
| Security invariants and threat boundaries | [`docs/security.md`](docs/security.md) |
| Experimentally established facts | [`docs/poc-evidence.md`](docs/poc-evidence.md) |
| Related projects and differentiation | [`docs/competitive-landscape.md`](docs/competitive-landscape.md) |
| Milestones and exit criteria | [`docs/roadmap.md`](docs/roadmap.md) |
| Accepted and unresolved decisions | [`docs/decisions.md`](docs/decisions.md) |
| Instructions for future agents | [`AGENTS.md`](AGENTS.md) |

## Current Project Choices

- Intended license: Apache License 2.0.
- Intended public hosting: GitHub canonical, Forgejo automatic mirror.
- `Scarcity Router` is a working name pending a collision and naming search.
- Likely implementation stack: Python 3.12+, `uv`, `pytest`, typed schemas, a
  small CLI, a small HTTP layer and the official MCP SDK. This is not binding.

See the [roadmap](docs/roadmap.md) before starting implementation. M0 and M1
are complete, and the M2 implementation slices M2a–M2e have landed (capacity
scopes, selection-input contracts, calibration, scarcity and policy
primitives, and the deterministic selector with explanation and simulation);
M2 exit/live acceptance is still pending, and no production release is
claimed.

The portable descriptive model policy is available at
[`model-policy.json`](model-policy.json) and the calibrated model catalog at
[`model-catalog.json`](model-catalog.json); the human-readable calibration
rationale is [`docs/model-calibration.md`](docs/model-calibration.md).
External consumers needing reproducible policy should pin a commit or release
rather than assume `main` never changes.
