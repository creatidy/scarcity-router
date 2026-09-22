# Product definition

## Problem

People using several AI subscriptions do not have one stable
answer to: “Which capable model should this task consume right now?” Quota is
split across rolling windows, subscriptions do not map cleanly to per-token
prices, and an orchestrator may consume a different provider's quota through
child work. Static repository mappings become wrong as capacity changes.

The immediate user currently checks OpenAI and Z.ai usage manually. A typical
misleading state is a provider with nearly all of a short window remaining but
almost none of its weekly allowance. The product succeeds first by removing
that repeated manual check and avoiding accidental consumption of the scarce
subscription.

A second problem follows: most AI clients speak only the OpenAI API
(`base_url`, `api_key`, `model`) and cannot consume a recommendation, so the
user cannot act on the best choice without manually re-configuring each
client. The optional execution gateway (D-040) closes that gap for users who
want it, without taking anything away from recommendation-only users.

## Value proposition

Scarcity Router recommends the least scarce model that satisfies the task. It
combines four independent inputs:

- a curated, multidimensional model capability catalog;
- provider-independent task requirements and hard constraints;
- measured runtime capacity and health;
- user reservation policy and temporary preferences.

The result contains a selected model, ranked fallbacks and a human-readable,
structured explanation.

Optionally (D-040, program A0 / issues #85–#95), the execution gateway lets a
user point any OpenAI-compatible client at one Scarcity Router endpoint and
have requests served from the best available authorized resource — API
providers, Ollama/local inference, or the approved local Codex adapter —
under the same least-scarce-capable discipline. The goal is efficient use of
heterogeneous AI access: subscriptions, eligible promotions, metered APIs,
prepaid APIs and local models/local GPU.

## Operating modes

1. **Recommendation-only (default).** Exactly the product described above:
   CLI, loopback REST and stdio MCP over one authoritative core. No server
   component, worker, container or execution.
2. **Execution gateway (optional, explicit deployment).** An authenticated
   server component plus, where needed, a native worker. Every
   recommendation-only user and interface keeps working unchanged (M08/M10
   acceptance criteria).

## Initial users and environment

The owner is the first and primary user. The initial workflow uses an AI
orchestrator with OpenAI/Codex and Z.ai subscription models. Supporting this
workflow reliably is more important than broad provider coverage.

Potential later consumers include Kilo, Codex, Claude Code, OpenClaw, shell
scripts, IDE extensions and dashboards. They integrate through stable
interfaces; with the optional gateway they may additionally send execution
traffic to the Scarcity Router endpoint itself.

## In scope

- subscription capacity collection (read-only telemetry reads, plus the
  single bounded provider-managed OpenAI auth-recovery exception of
  `docs/decisions.md` D-018);
- normalized capacity and capability schemas;
- L0–L5 task levels, data-driven profiles and raw requirements;
- hard-constraint filtering;
- continuous scarcity, reservation and preference policies;
- model recommendation, ranked alternatives and explanation;
- simulation of capacity and policy states;
- CLI, REST and MCP over the same authoritative core;
- a minimal local dashboard and integration recipes after the core is useful;
- the optional execution gateway program (A0, issues #85–#95): OpenAI-
  compatible execution, generic HTTP/Ollama adapters, a native worker, the
  Codex adapter where evidence supports it, configuration/web UX,
  distribution — each behind its own module issue and validation gate. The
  proposed ZCode execution adapter was cancelled by owner decision (D-047)
  after M07 Stage-1 evidence; Z.ai Coding Plan execution itself stays in
  scope through the generic HTTP adapter (M04).

## Explicit non-goals

Absolute non-goals in every mode:

- Reading user prompts beyond what an explicitly authorized execution request
  contains; reading source code, repository content or browser sessions.
- Replacing an agent or orchestration environment; autonomous fallback
  execution; issue-to-PR orchestration; repository management; a generic
  agent workflow framework.
- A general task scheduler; `scarcity run <task>`-style interfaces; an
  execution mode with undefined or unbounded start time silently replacing a
  synchronous HTTP request.
- Reset-credit redemption or similar benefit-consuming actions; they remain
  information unless a separate explicit decision authorizes acting on them.
- Generic API-cost optimization or billing aggregation.
- A multi-tenant SaaS, distributed control plane or agent operating system;
  sharing one personal subscription with multiple independent users.
- Maximizing provider count; publishing a universal model leaderboard.

Conditional boundary: prompt, response and tool-call proxying and model-call
execution are outside recommendation-only mode (D-001 as superseded in part by
D-040) and inside the optional execution gateway exactly as far as D-040
through D-045 authorize. Client-supplied tools always return to the client as
`tool_calls`; the router never executes them.

The broker decides *where work should go*. In execution mode it also carries
the authorized request to the chosen resource — it still never decides *what
the work is* and never performs the client's tool calls.

## Product principles

- Simple over clever; explicit over magical.
- Measured telemetry over inferred quota.
- Normalized core over provider-specific leakage.
- Least-privilege access; new exposure only through explicit security
  decisions (D-009/D-044).
- Subscription capacity over generic API-cost assumptions; entitlements,
  quota pools and promotional eligibility are distinct facts, never implied
  by a model name (D-042).
- Explainable decisions over opaque scoring.
- Stable core over many integrations.
- Real workflow value over novelty or market theater.

## Positioning

The default product remains **“Not another AI proxy.”**: a safe local
recommender that needs no model traffic. The optional execution gateway is a
**personal execution gateway**, not a generic LLM gateway: one user's own
authorized resources behind one OpenAI-compatible endpoint, chosen by the
same explainable least-scarce-capable discipline. The eventual
zero-configuration promise — **“Uses the AI subscriptions you're already
logged into.”** — applies only to collectors and adapters proven safe and
supportable.

## Validation

Personal success means the owner uses the broker regularly, trusts its
choices, checks provider dashboards less often and does not unexpectedly
exhaust a subscription through orchestration. For the gateway program,
success additionally means standard OpenAI SDK clients complete multi-turn
tool-using conversations through one endpoint with honest capability
handling, and recommendation-only operation is provably unchanged (M08/M10).

OSS signals—installations, stars, contributors, adapter contributions and
integration recipes—are secondary. Commercial investment is justified only
after the local product survives provider changes and demonstrates recurring
value. A possible later commercial layer may curate capability data or support
teams and fleets, but those features are not present commitments.
