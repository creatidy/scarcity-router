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
M2 implementation is complete and closed as **PASS** (2026-09-06) after live
acceptance in the owner's real workflow. There is not yet an installable
package or final executable name. The M3a planning gate froze the REST and
MCP contracts in
[`docs/machine-interfaces.md`](docs/machine-interfaces.md) (D-028); the
minimal loopback-only REST adapter is implemented (M3b, D-030), and the thin
stdio MCP adapter is implemented (M3c, D-031). Live CLI/REST/MCP acceptance
is complete. M3 is closed as **PASS** (2026-09-07); sanitized
evidence is recorded in [`docs/m3-acceptance.md`](docs/m3-acceptance.md).

The post-M3 selector correction (issue #57, D-032) introduces catalog v2:
explicit reasoning effort, Luna Medium, Terra Medium and Sol Medium alongside
the four preserved configurations. Capability is calibrated per configuration;
reasoning effort is not subscription scarcity. Ranking compares scarcity,
capability margin, then lowest effort before preference and stable identity.
All OpenAI configurations share the evidenced `openai/codex` capacity scope.
CLI/REST/MCP machine-interface v1 shapes remain unchanged; variants remain
opaque configuration identifiers, never parsed for effort. This is not M4 or
Astra onboarding, and historical M3 PASS evidence is unchanged.

Astra Low's evidence gate (issue #49, D-033) is **blocked**, not onboarded.
The shared Work/Codex allowance is evidenced, but complete plan-dependent
capacity applicability is not. Catalog v2 and policy v6 remain unchanged. See
the [capacity decision matrix](docs/capacity-model.md#astra-onboarding-gate-issue-49)
and [conditional calibration evidence](docs/model-calibration.md#astra-low-evidence-gate-issue-49).

## Repository

Canonical development:
https://forgejo.creatidy.com/BioMedical-IT/scarcity-router

GitHub mirror:
https://github.com/creatidy/scarcity-router

Forgejo is canonical for branches, issues, pull requests, reviews and the
development workflow; `develop` is the integration branch and ordinary PRs
target `develop`. GitHub is an automatic mirror for public
visibility/discovery, read-only integrations and consumers that only support
GitHub — do not open normal issues or pull requests there. `main` is a
human-controlled promotion/release branch and is not the normal agent
integration branch.

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
  GPT-5.6 Terra Medium

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

## Local REST Service

M3b adds a minimal, loopback-only REST adapter exposing the frozen machine
interface v1 (D-028, D-030):

```bash
uv run python -m scarcity_router.server            # binds 127.0.0.1:8765
uv run python -m scarcity_router.server --port 9000
```

Exactly four endpoints:

```text
GET  /healthz     process liveness only
GET  /v1/status   normalized CapacitySnapshot v3 snapshots in a versioned envelope
POST /v1/select   {"profile_id" | "requirement", "tightening"?, "selector_policy"?, "replenishment_states"?}
POST /v1/simulate the select input plus a required "overrides" object
```

The server binds only to `127.0.0.1` (there is no bind-address option), has
no authentication, handles requests serially and adds no runtime dependency.
Request bodies are strict JSON (duplicate keys and `NaN`/`Infinity` are
rejected), unknown request keys are rejected, valid no-solution decisions
are HTTP 200 with `selected = null`, invalid client requests are HTTP 400
`invalid_request` and internal failures are HTTP 500 `internal_error`.
Provider degradation stays normalized data in successful responses. The
adapter owns no selection logic: it calls the same typed application seam
as the CLI. See
[`docs/machine-interfaces.md`](docs/machine-interfaces.md) for the complete
frozen contract.

## Local Stdio MCP

M3c exposes exactly three recommendation-only MCP tools over the official
SDK's local stdio transport. The adapter calls the application layer directly;
it does not start or call the REST server, execute model inference, accept
credentials or expose resources/prompts.

```bash
uv run python -m scarcity_router.mcp
```

Tools:

```text
scarcity_status
scarcity_select
scarcity_simulate
```

See [`examples/mcp-stdio.json`](examples/mcp-stdio.json) for a generic
external-orchestrator process configuration and
[`docs/machine-interfaces.md`](docs/machine-interfaces.md) for the shared v1
logical envelopes and error semantics.

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
| Multi-model roles, review, effort and durable execution governance | [`docs/llm-operating-policy.md`](docs/llm-operating-policy.md) |
| REST and MCP machine-interface contracts | [`docs/machine-interfaces.md`](docs/machine-interfaces.md) |
| Provider adapter expectations | [`docs/providers.md`](docs/providers.md) |
| Security invariants and threat boundaries | [`docs/security.md`](docs/security.md) |
| Experimentally established facts | [`docs/poc-evidence.md`](docs/poc-evidence.md) |
| Related projects and differentiation | [`docs/competitive-landscape.md`](docs/competitive-landscape.md) |
| Milestones and exit criteria | [`docs/roadmap.md`](docs/roadmap.md) |
| Accepted and unresolved decisions | [`docs/decisions.md`](docs/decisions.md) |
| Instructions for future agents | [`AGENTS.md`](AGENTS.md) |

## Current Project Choices

- Intended license: Apache License 2.0.
- Repository hosting: Forgejo canonical, GitHub automatic mirror.
- `Scarcity Router` is a working name pending a collision and naming search.
- Likely implementation stack: Python 3.12+, `uv`, `pytest`, typed schemas, a
  small CLI, a small HTTP layer and the official MCP SDK. This is not binding.

See the [roadmap](docs/roadmap.md) before starting implementation. M0, M1
and M2 are complete; M2 closed as PASS (2026-09-06) after live acceptance,
and no production release is claimed. Automatic live OpenAI reset-credit
acquisition is deferred; manual normalized replenishment remains supported.

The portable descriptive model policy is available at
[`model-policy.json`](model-policy.json) and the calibrated model catalog at
[`model-catalog.json`](model-catalog.json); the human-readable calibration
rationale is [`docs/model-calibration.md`](docs/model-calibration.md).
Current reference model assignments may mention models not yet onboarded into
the active selector catalog; `model-catalog.json` remains authoritative for
actual selector candidates.
External consumers needing reproducible policy should pin a commit, tag or
release rather than track a mutable integration or mirror branch.
