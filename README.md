# Scarcity Router

> Choose the least scarce model that is capable enough for the task.

Scarcity Router is a local decision service for people using multiple
subscription-backed AI models. It combines current capacity, curated
capabilities, task requirements and user policy, then returns a recommendation
with alternatives and an explanation.

## What It Does

The broker answers **which capable model should this task consume now?** It
considers quota windows, capability, constraints and reservations, then prefers
the least scarce sufficient candidate.

It is a recommendation service, not a model gateway. It does not receive
prompts, proxy model traffic, read source code or repositories, execute model
calls, dispatch fallbacks, or replace an orchestrator such as Kilo, Codex or
Claude Code.

## Quick Start

Requirements:

- Python 3.12 or newer;
- `uv`.

Provider access is described in [Supported Providers](#supported-providers).

The package is not published to any index; install it locally from a
checkout (or a built wheel) into an isolated `uv` tool environment:

```bash
uv tool install /path/to/scarcity-router-checkout
# or, after `uv build --no-sources`:
uv tool install dist/scarcity_router-0.1.0-py3-none-any.whl
```

This exposes exactly three commands:

- `scarcity-router` — the `status` / `select` / `simulate` CLI;
- `scarcity-router-mcp` — the stdio MCP adapter;
- `scarcity-router-server` — the loopback REST adapter.

The installed commands load the packaged default catalog (version 2) and
model policy (version 6) as package resources, so they work without a
repository checkout and independent of the current directory; explicit
`--catalog` and `--model-policy` flags always override the defaults.

From a checkout instead, sync the repository-managed tools and run the
module CLI:

```bash
uv sync --only-dev
uv run python -m scarcity_router --help
```

In a checkout the defaults resolve to the repository-root
`model-catalog.json` and `model-policy.json`.

## Check Capacity

Show one fresh normalized status snapshot for each supported provider:

```bash
uv run python -m scarcity_router status
uv run python -m scarcity_router status --json
```

Provider failures and unknown windows remain explicit status data; missing
telemetry is never turned into zero or full capacity. Status collection does not
issue a model prompt or model request.

## Select A Model

Use a calibrated task profile or provide a complete requirement document:

```bash
uv run python -m scarcity_router select --profile routine_coding --explain
uv run python -m scarcity_router select --profile deep_coding --json
uv run python -m scarcity_router select --requirement task.json
```

The result includes the selected candidate, alternatives, exclusions, capacity
evidence, policy reasons and degraded/unknown indicators. A valid no-solution
result is explicit. `simulate` applies typed overrides and runs the same
selector:

```bash
uv run python -m scarcity_router simulate \
  --profile routine_coding --overrides simulation.json
```

## MCP Integration

MCP is the primary machine-integration path for local orchestrators. Start
the stdio adapter through the installed command:

```bash
scarcity-router-mcp
```

or, from a checkout:

```bash
uv run python -m scarcity_router.mcp
```

The installed process configuration recipe is
[`examples/mcp-stdio.json`](examples/mcp-stdio.json). It advertises exactly:

- `scarcity_status` — current normalized provider capacity;
- `scarcity_select` — a recommendation for a profile or explicit requirement;
- `scarcity_simulate` — a recommendation with typed simulation overrides.

The tools call the same application/core as the CLI and REST interface. They
use stdio, accept no credentials or provider endpoints, and never execute
inference. Envelopes are defined in
[`docs/machine-interfaces.md`](docs/machine-interfaces.md).

### Global Kilo deployment note

To use Scarcity Router globally in Kilo, add the
[`examples/mcp-stdio.json`](examples/mcp-stdio.json) recipe (a
`type: local` MCP server launching the installed `scarcity-router-mcp`
command) to the `mcp` section of your Kilo configuration. This repository
never edits your global Kilo configuration itself; the copy-paste is a
manual owner action, and it requires the package to be installed as a `uv`
tool (or the recipe adjusted to the checkout-based module command).

## Supported Providers

- **OpenAI through Codex app-server** — reads subscription capacity from the
  locally available app-server.
- **Z.ai Coding Plan** — reads the existing Kilo `zai-coding-plan` credential
  and the provider's normalized usage endpoint.

These adapters report subscription capacity, not generic API pricing. Local
inference providers are not supported. See
[`docs/providers.md`](docs/providers.md) for discovery, normalization and
provider-change behavior.

## Privacy / Product Boundary

Credentials stay in their existing local provider-managed source whenever
possible. Collectors use them transiently, never return or persist their
values, and only emit normalized safe status. The REST adapter binds to
`127.0.0.1` by default; MCP uses local stdio.

The service does not inspect prompts, source code, repository contents or
browser sessions. It does not proxy requests, call models, redeem reset credits
or automatically execute a fallback. Read
[`docs/security.md`](docs/security.md) for the complete credential and network
boundary.

## Documentation

A normal adopter can stop after **USE IT**. Technical contracts, development
rules and historical evidence remain available without being part of onboarding.

### USE IT

- This README — quick start, capacity checks, selection and MCP.
- [`examples/mcp-stdio.json`](examples/mcp-stdio.json) — installed MCP process
  configuration recipe.

### UNDERSTAND IT

- [`docs/product.md`](docs/product.md) — purpose and boundaries.
- [`docs/architecture.md`](docs/architecture.md) — components and dependencies.
- [`docs/capacity-model.md`](docs/capacity-model.md) — normalized capacity.
- [`docs/capability-model.md`](docs/capability-model.md) — requirements and capabilities.
- [`docs/selection-policy.md`](docs/selection-policy.md) — eligibility and ranking.
- [`docs/machine-interfaces.md`](docs/machine-interfaces.md) — REST and MCP.
- [`docs/providers.md`](docs/providers.md) — provider adapters.
- [`docs/security.md`](docs/security.md) — secrets and network boundaries.

### DEVELOP IT

- [`AGENTS.md`](AGENTS.md) — durable repository and agent rules.
- [`docs/llm-operating-policy.md`](docs/llm-operating-policy.md) — bounded
  multi-model execution and review governance.
- [`Makefile`](Makefile) — the reproducible test and type-check gate.

### AUDIT / HISTORY

These documents preserve decisions, evidence, calibration, acceptance records,
competitive context and roadmap history. They are provenance, not required
onboarding:

- [`docs/decisions.md`](docs/decisions.md)
- [`docs/roadmap.md`](docs/roadmap.md)
- [`docs/history/roadmap.md`](docs/history/roadmap.md) — detailed milestone record.
- [`docs/poc-evidence.md`](docs/poc-evidence.md)
- [`docs/m3-acceptance.md`](docs/m3-acceptance.md)
- [`docs/model-calibration.md`](docs/model-calibration.md)
- [`docs/competitive-landscape.md`](docs/competitive-landscape.md)

## Development

Run the repository gate from a checkout:

```bash
uv sync --only-dev
make check
uv run basedpyright
git diff --check
```

Package artifact and isolated-install checks:

```bash
make package-check
```

Forgejo is canonical for issues, branches, pull requests and reviews; `develop`
is the integration branch. GitHub is an automatic read-only mirror for public
visibility and integrations that require GitHub.

The project is licensed under the Apache License 2.0.
