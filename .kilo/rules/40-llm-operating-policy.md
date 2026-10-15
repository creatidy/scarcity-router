# LLM Operating Policy

Follow the full policy in [`docs/llm-operating-policy.md`](../../docs/llm-operating-policy.md).

- Route by role and task fit, not prestige.
- Use the least scarce fully capable model; capability deficits are never
  compensated by availability or strength elsewhere.
- Use a distinct reviewer session for consequential review.
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
  environment/reviewer failover automatically; preserve ordinals and restricted gates.
- D-069's whole-PR review ceiling is ten by default. A finite per-issue exception
  exists only through D-069A and only after canonical owner/repository/issue/PR/source
  authority, scope and maximum are independently verified; the local ledger is only
  a counter. Reviewer roles are based on current required gates, not ordinal numbers.
- For Scarcity Router `security_critical` work, Adrian invokes the mandatory official
  Codex `gpt-daybreak-blue-latest` review manually after ordinary approval/validation.
  Before handoff, reserve/persist the next owner-manual ordinal against exact frozen refs;
  name it in `READY_FOR_OWNER_SECURITY_REVIEW`. The reservation is not security approval
  or merge authority and cannot be reused; a launched attempt is consumed for any result.
  Any HEAD/base change invalidates approval. The implementation agent must never invoke
  Daybreak automatically. On this Router owner-manual path only, accept
  `RUNTIME_UNOBSERVABLE` when backend identity is not independently exposed, after
  verifying the official binary/version/provenance, requested-model invocation/dispatch,
  process/session/turn, immutable input/output, exact refs, structured verdict, separate
  context/tool audit and Adrian's explicit acceptance of the limitation. Record the
  state honestly; do not label it `RUNTIME_VERIFIED` or infer backend identity.
  `DISPATCH_VERIFIED` is dispatch configuration only. `FAIL` on an observed identity
  mismatch or unauthorized substitution. See `docs/llm-operating-policy.md` and D-069A.
  Generic runtime-identity requirements and the mandatory human security gate remain.
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
