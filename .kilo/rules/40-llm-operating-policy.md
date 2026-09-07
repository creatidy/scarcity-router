# LLM Operating Policy

Follow the full policy in [`docs/llm-operating-policy.md`](../../docs/llm-operating-policy.md).

- Route by role and task fit, not prestige.
- Use the least scarce fully capable model; capability deficits are never
  compensated by availability or strength elsewhere.
- Use a distinct reviewer session for consequential review.
- LLM output is not evidence. Keep evidence preparation separate from expert
  scientific or methodological adjudication.
- For large work, create an early durable checkpoint and continue in bounded
  units.
- Repository and artifact state outrank session UI state. Before retrying,
  inspect output, runtime metadata, disk, Git and durable artifacts.
- Retry only with a concrete diagnosis or changed strategy. Do not repeat
  identical failures.
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
active catalog remains authoritative for selector eligibility, and Scarcity
Router's D-017 project decision excludes local inference.
