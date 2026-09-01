# Product definition

## Problem

People using several AI subscriptions and local models do not have one stable
answer to: “Which capable model should this task consume right now?” Quota is
split across rolling windows, subscriptions do not map cleanly to per-token
prices, and an orchestrator may consume a different provider's quota through
child work. Static repository mappings become wrong as capacity changes.

The immediate user currently checks OpenAI and Z.ai usage manually. A typical
misleading state is a provider with nearly all of a short window remaining but
almost none of its weekly allowance. The product succeeds first by removing
that repeated manual check and avoiding accidental consumption of the scarce
subscription.

## Value proposition

Scarcity Router recommends the least scarce model that satisfies the task. It
combines four independent inputs:

- a curated, multidimensional model capability catalog;
- provider-independent task requirements and hard constraints;
- measured runtime capacity and health;
- user reservation policy and temporary preferences.

The result contains a selected model, ranked fallbacks and a human-readable,
structured explanation.

## Initial users and environment

The owner is the first and primary user. The initial workflow uses an AI
orchestrator with OpenAI/Codex and Z.ai subscription models plus a local Ollama
fallback. Supporting this workflow reliably is more important than broad
provider coverage.

Potential later consumers include Kilo, Codex, Claude Code, OpenClaw, shell
scripts, IDE extensions and dashboards. They integrate through stable
interfaces rather than forcing model traffic through the broker.

## In scope

- read-only subscription capacity collection;
- local-model availability and configuration health;
- normalized capacity and capability schemas;
- L0–L5 task levels, data-driven profiles and raw requirements;
- hard-constraint filtering;
- continuous scarcity, reservation and preference policies;
- model recommendation, ranked alternatives and explanation;
- simulation of capacity and policy states;
- CLI first, followed by REST and MCP over the same core;
- a minimal local dashboard and integration recipes after the core is useful.

## Explicit non-goals

- Prompt, response or tool-call proxying.
- Reading user prompts, source code or repository content.
- Executing model calls or automatically dispatching fallbacks.
- Replacing an agent or orchestration environment.
- Generic API-cost optimization or billing aggregation.
- A multi-tenant SaaS, distributed control plane or agent operating system.
- Maximizing provider count.
- Publishing a universal model leaderboard.

The broker decides *where work should go*. The external orchestrator decides
*what the work is* and performs it.

## Product principles

- Simple over clever; explicit over magical.
- Measured telemetry over inferred quota.
- Normalized core over provider-specific leakage.
- Read-only access over privileged access.
- Local processing over cloud processing when equivalent.
- Explainable decisions over opaque scoring.
- Stable core over many integrations.
- Real workflow value over novelty or market theater.

## Positioning

The concise public distinction is **“Not another AI proxy.”** The tool should be
safe to adopt precisely because it does not need source code or model traffic.
An eventual zero-configuration experience can be framed as: **“Uses the AI
subscriptions you're already logged into.”** That promise may be made only for
collectors proven safe and supportable.

## Validation

Personal success means the owner uses the broker regularly, trusts its choices,
checks provider dashboards less often and does not unexpectedly exhaust a
subscription through orchestration.

OSS signals—installations, stars, contributors, adapter contributions and
integration recipes—are secondary. Commercial investment is justified only
after the local product survives provider changes and demonstrates recurring
value. A possible later commercial layer may curate capability data or support
teams and fleets, but those features are not present commitments.

