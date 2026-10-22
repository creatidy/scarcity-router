# LLM Operating Policy

Follow the full policy in [`docs/llm-operating-policy.md`](../../docs/llm-operating-policy.md).

## Router Owner-Manual Codex Amendment (D-069A)

For Scarcity Router's owner-manual security review only, the accepted owner decision
permits `RUNTIME_UNOBSERVABLE` solely when the official Codex path cannot independently
expose backend identity. Official binary/provenance, requested-model dispatch, native
session/turn/input/output, context/tool audit, exact refs and a structured APPROVE with
no blockers remain mandatory. Never infer `RUNTIME_VERIFIED` from UI/configuration or
self-report. Mismatch/substitution is FAIL; other missing evidence keeps the gate open.
No automatic Daybreak, hidden launcher, skipped review or generic identity weakening.

- Route by role and task fit, not prestige.
- Use the least scarce fully capable model; capability deficits are never
  compensated by availability or strength elsewhere.
- Use a distinct reviewer session for consequential review.
- For ordinary Agent Manager review, the base default is the exact provider-qualified
  model selected by the current primary. The active direct owner override #18480 sets
  `GPT-6.1 Sol` / `openai` / `high` for all review starts until superseded. Adrian may
  explicitly choose another permitted identity per review. Highspeed and automatic
  workspace/provider fallback are not authorized. An unresolved identity, mismatch or
  provider error is a stop condition, never authority to retry or substitute.
- Development model selection/starts remain explicit and user-visible. For ordinary
  PR review only, the direct D-077 owner amendment permits the sole authorized primary
  `/loop` or `/finish-pr` context to start a fresh visible Kilo Agent Manager session
  using the read-only `pr-reviewer-standalone` agent. The task explicitly binds the
  active owner-selected `GPT-6.1 Sol`/`openai`/`high` identity; only an explicit
  per-review Adrian override may change it. Never rely on workspace defaults, implicit
  provider routing or fallback. The primary
  supplies exact refs, acceptance and complete compact Git diff, and retrieves the
  structured native result without owner
  copy/paste. No scripts/subprocesses, Task/delegation, background/HTTP inference or
  generated model launcher; no self-review or second orchestrator.
- No persistent local review-evidence folders or replacement receipt hierarchy.
  Use Git/canonical records/native harness references; offline fixtures use cleaned
  OS temporary directories. This does not alter authorized Router product inference.
- PR delivery review uses a fresh separate user-visible read-only Kilo/Codex session.
  Ordinary Kilo sessions may be started by the authorized primary through Agent
  Manager under D-077; preserve actual session/model identity, source permissions and
  native context provenance, never infer OS isolation or backend identity from
  dispatch metadata. Official security Daybreak remains manually started by Adrian.
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
  deterministic environment recovery; ordinary review uses the visible Agent Manager
  path only under the direct owner amendment. Preserve ordinals and restricted gates,
  with no hidden failover or automatic Daybreak.
- Completed PASS gates, adjudication, remediation, translation and validation
  remain completed unless source changes materially or a concrete regression is
  identified.
- Choose the lowest sufficient reasoning effort. Importance or size alone does
  not justify maximum effort.
- Runtime identity uses `REQUESTED`, `DISPATCH_VERIFIED`, `RUNTIME_VERIFIED`,
  `RUNTIME_UNOBSERVABLE` and `FAIL`. Never infer it from model self-report,
  prompts, task titles or UI labels alone.
- Prefer a small ready queue; do not maximize concurrency.

The role-assignment metadata is descriptive and non-selector-facing. The
active catalog remains authoritative for selector eligibility; D-017's
local-inference exclusion is superseded by D-040, and local resources return
only as execution-gateway resources through the module issues.
