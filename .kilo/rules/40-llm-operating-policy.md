# LLM Operating Policy

Follow the full policy in [`docs/llm-operating-policy.md`](../../docs/llm-operating-policy.md).

- Route by role and task fit, not prestige.
- Use the least scarce fully capable model; capability deficits are never
  compensated by availability or strength elsewhere.
- Use a distinct reviewer session for consequential review.
- All development/review model selection and session starts are explicit and
  user-visible in Kilo or official Codex. No scripts/subprocesses, Task/delegation,
  background/HTTP inference or generated model-launch wrapper. Prepare direct
  source/ref instructions and hand off the pending gate instead of hiding execution.
- No persistent local review-evidence folders or replacement receipt hierarchy.
  Use Git/canonical records/native harness references; offline fixtures use cleaned
  OS temporary directories. This does not alter authorized Router product inference.
- PR delivery review uses a fresh user-visible read-only Kilo/Codex session,
  never programmatic dispatch. Require actual session identity, explicit selected
  model, source permissions and genuine native execution/context provenance;
  do not claim OS isolation or backend identity from dispatch metadata.
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
  deterministic environment recovery; reviewer starts remain an explicit visible
  user action. Preserve ordinals and restricted gates, no hidden failover.
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
