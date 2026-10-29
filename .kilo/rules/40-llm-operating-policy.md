# LLM Operating Policy

Follow the full policy in [`docs/llm-operating-policy.md`](../../docs/llm-operating-policy.md).

D-081: every consequential PR requires ordinary independent correctness review
including applicable security assessment. Security-critical work receives rigorous
review by a sufficiently capable model; P0/P1, material security findings and
data-integrity risks block integration, never deferred to the final audit.
Daybreak is not a PR merge gate. It is reserved for one final comprehensive frozen
project release-candidate audit before production release, not a development gate.
No routine owner handoff or automatic Daybreak is authorized. D-078 is unchanged.

## Router Owner-Manual Codex Amendment (D-069A)

Historical assurance, not a new PR handoff: for Scarcity Router's owner-manual security review only, the accepted owner decision
permits `RUNTIME_UNOBSERVABLE` solely when the official Codex path cannot independently
expose backend identity. Official binary/provenance, requested-model dispatch, native
session/turn/input/output, context/tool audit, exact refs and a structured APPROVE with
no blockers remain mandatory. Never infer `RUNTIME_VERIFIED` from UI/configuration or
self-report. Mismatch/substitution is FAIL; other missing evidence keeps the gate open.
No automatic Daybreak, hidden launcher, skipped review or generic identity weakening.

- Route by role and task fit, not prestige.
- Use the least scarce fully capable model; capability deficits are never
  compensated by availability or strength elsewhere.
- Use a fresh distinct reviewer conversation for consequential review. Under an
  authorized `/loop` or `/finish-pr`, the orchestrator may start this ordinary Kilo
  session through native Agent Manager; it is user-visible and does not require the
  owner to start it or relay results. Model identity follows applicable policy and
  must not be silently substituted.
- No scripts/subprocesses, Task/subagent dispatch, background/HTTP inference or
  generated model-launch wrapper. The final project-level Daybreak audit remains a
  separate future release gate and is never started automatically by this workflow.
- No persistent local review-evidence folders or replacement receipt hierarchy.
  Use Git/canonical records/native harness references; offline fixtures use cleaned
  OS temporary directories. This does not alter authorized Router product inference.
- PR delivery review uses a fresh user-visible read-only Kilo session through native
  session management. Require actual session identity, model, source permissions
  and genuine native execution/context provenance; do not claim filesystem isolation
  or backend identity from dispatch metadata. A separate worktree is optional only
  for a demonstrated technical need.
- LLM output is not evidence. Keep evidence preparation separate from expert
  scientific or methodological adjudication.
- Follow D-068's ownership and Agreed/Verified/Proposed/To-prove distinction;
  use `docs/architecture.md` and the G01-G13 matrix in `docs/roadmap.md`.
  Registered proof-dependent work is not an implemented integration.
- For large work, create an early durable checkpoint and continue in bounded
  units.
- Repository and artifact state outrank session UI state. Before retrying,
  inspect output, runtime metadata, disk, Git and durable artifacts.
- Retry only with a concrete diagnosis or changed strategy. Do not repeat
  identical failures.
- Command delivery follows `.kilo/rules/35-technical-recovery.md`: infrastructure
  failure is not an implementation finding or owner decision. Use bounded authorized
  deterministic environment recovery. Authorized `/loop` and `/finish-pr`
  orchestrators start ordinary fresh user-visible Kilo reviewer sessions; the owner
  need not start or relay them. Preserve ordinals and restricted gates; no hidden
  failover.
- Completed PASS gates, adjudication, remediation, translation and validation
  remain completed unless source changes materially or a concrete regression is
  identified.
- Choose the lowest sufficient reasoning effort. Importance or size alone does
  not justify maximum effort.
- Runtime identity uses `REQUESTED`, `DISPATCH_VERIFIED`, `RUNTIME_VERIFIED`,
  `RUNTIME_UNOBSERVABLE` and `FAIL`. Never infer it from model self-report,
  prompts, task titles or UI labels alone.
- Prefer a small ready queue; do not maximize concurrency.

D-084 removes static Router model and role-assignment artifacts. The harness
owns review assignments; Router eligibility uses source-bound evidence and
local authorization. D-017's local-inference exclusion is superseded by
D-040; local inference remains an execution-gateway resource.
