# Roadmap

This page summarizes current direction. It is development planning, not
required onboarding. The detailed completed-milestone record is preserved in
[`docs/history/roadmap.md`](history/roadmap.md), and live acceptance evidence
is in [`docs/m3-acceptance.md`](m3-acceptance.md).

## Current Status

Scarcity Router currently provides:

- normalized subscription-capacity status for OpenAI/Codex and Z.ai Coding
  Plan (with the D-039 execution-eligibility reports);
- deterministic, explainable model selection and typed simulation;
- a loopback-only REST interface and a local stdio MCP adapter over the same
  application/core;
- a provenance-bearing catalog and data-driven task profiles;
- an installable local package (D-034) with a default user configuration
  (D-036);
- the implemented optional execution gateway (D-040 through D-049): one
  composed server with an authenticated OpenAI-compatible execution surface,
  control API and web UI, generic OpenAI-compatible HTTP/Ollama adapters,
  a native outbound-TLS worker, and the Codex worker-local adapter — with
  distribution and acceptance work complete subject to the explicit external
  gates recorded in
  [`docs/m10-acceptance.md`](m10-acceptance.md).

Recommendation-only operation is the default and remains fully supported
throughout everything below.

## Execution-Gateway Program (A0, issues #85–#95) — COMPLETE

The owner-approved optional execution gateway (D-040, 2026-09-19) is
specified (A0, #85) and fully implemented. Completion state per module,
with the honest evidence and remaining external gates:

| Planning id | Issue | Scope | Completion state |
| --- | --- | --- | --- |
| A0 | #85 | Architecture, decisions, contracts | Complete |
| M01 | #86 | Resource registry, state, and collectors for executable resources | Complete |
| M02 | #87 | Routing core, policy, and client-requirement binding for executable targets | Complete |
| M03 | #88 | OpenAI-compatible gateway and execution coordinator | Complete |
| M04 | #89 | Generic OpenAI-compatible HTTP adapter and Ollama integration | Complete |
| M05 | #90 | Native worker, pairing, and execution transport | Complete |
| M06 | #91 | Codex adapter (Stage 1 evidence + Stage 2 implementation) | Complete subject to the recorded `EXTERNAL_ACCEPTANCE_GATE: LIVE_CODEX_SUBSCRIPTION` live-acceptance gate |
| M07 | #92 | ZCode adapter feasibility | Stage 1 complete; Stage 2 cancelled (D-047) |
| M08 | #93 | MCP, REST, and CLI compatibility; optional remote mode | Complete |
| M09 | #94 | Configuration, web UX, and diagnostics | Complete |
| M10 | #95 | Distribution, installation, update, and end-to-end acceptance | Implementation and deterministic acceptance complete subject to the explicit external gates in [`docs/m10-acceptance.md`](m10-acceptance.md) |

The acceptance records are
[`docs/m10-acceptance.md`](m10-acceptance.md) (platform support table,
sixteen-mission-scenario E2E matrix, measured first-run friction,
external gates) and
[`docs/m10-security-acceptance.md`](m10-security-acceptance.md) (the
26-row security matrix mapped to the D-044 threat model). The remaining
external gates — live Codex subscription acceptance, live Windows
acceptance, Windows code signing and PyPI Trusted Publisher
configuration — are owner actions recorded there and in
[`docs/release-engineering.md`](release-engineering.md); none of them is
unrecorded implementation work.

M07 ran as an independent research track and never blocked the
program: its Stage-1 feasibility evidence is complete
([`docs/zcode-adapter-stage1-evidence.md`](zcode-adapter-stage1-evidence.md))
and Stage 2 was cancelled by owner decision (D-047) — no ZCode execution
adapter is planned. Reopening requires an official supported ZCode
programmatic interface and applicable vendor terms. That decision concerns
the ZCode desktop application only: Z.ai Coding Plan execution remains in
scope through M04's generic OpenAI-compatible HTTP adapter, which is a
different access path from the ZCode runtime.

The CI and public release-engineering foundation (#97,
[`docs/release-engineering.md`](release-engineering.md), D-046) is in
place: Forgejo development CI runs the authoritative gate plus the
package check on every `develop` push and PR, and the tag-driven public
release pipeline is implemented with its owner-side activation steps
recorded.

## Future Direction

- **Provider evaluation:** add another subscription provider only after its
  telemetry, authentication, security and maintenance boundary is evidenced.
- **Release readiness:** resolve packaging, naming and distribution questions
  (U-008/U-009) after regular use validates the workflows.
- **Optional product extensions:** provider health, runtime feedback, signed
  catalog releases, team policy and bounded compound recommendations only
  when a concrete use case justifies them.

GPT-6 Astra remains outside the active catalog until capability, hard-property
and capacity-applicability evidence is complete and human-reviewed. This does
not change the current catalog or selector behavior.

## Scope Guard

The product is two-mode (D-040): recommendation-only by default, plus the
optional execution gateway exactly as authorized by D-040 through D-045. It
does not inspect repositories, become a generic LLM gateway or agent
framework, orchestrate issues/PRs, schedule background tasks, or execute
client-supplied tools. New work starts from an explicitly selected Forgejo
issue and must preserve the authoritative contracts and security boundaries.
