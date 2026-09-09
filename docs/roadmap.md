# Roadmap

This page summarizes current direction. It is development planning, not
required onboarding. The detailed completed-milestone record is preserved in
[`docs/history/roadmap.md`](history/roadmap.md), and live acceptance evidence
is in [`docs/m3-acceptance.md`](m3-acceptance.md).

## Current Status

Scarcity Router currently provides:

- normalized subscription-capacity status for OpenAI/Codex and Z.ai Coding
  Plan;
- deterministic, explainable model selection and typed simulation;
- a loopback-only REST interface and a local stdio MCP adapter over the same
  application/core;
- a provenance-bearing catalog and data-driven task profiles.

The module-based local entry point works from a checkout. A published
installable package, stable executable name and release packaging are not yet
provided.

## Future Direction

- **M4:** a minimal local dashboard and integration recipes for common clients.
- **Provider evaluation:** add another subscription provider only after its
  telemetry, authentication, security and maintenance boundary is evidenced.
- **Release readiness:** resolve packaging, naming and distribution questions
  after regular local use validates the core workflow.
- **Optional product extensions:** consider provider health, runtime feedback,
  signed catalog releases, team policy and bounded compound recommendations
  only when a concrete use case justifies them.

GPT-6 Astra remains outside the active catalog until capability, hard-property
and capacity-applicability evidence is complete and human-reviewed. This does
not change the current catalog or selector behavior.

## Scope Guard

The service remains recommendation-only: it does not proxy prompts, execute
model calls, dispatch fallbacks, inspect repositories or become a generic LLM
gateway. New work starts from an explicitly selected Forgejo issue and must
preserve the authoritative contracts and security boundaries.
