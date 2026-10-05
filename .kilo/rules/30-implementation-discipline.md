# Implementation Discipline

## Branching and Scope

- Branch from `develop`, target `develop`, and never mutate `main`.
- Keep changes focused on the selected issue or explicit user request; make
  the smallest coherent issue-scoped change using existing local patterns.
- No force-push, no rewriting published history, no merging `main` into the
  feature branch, and no synchronization merge commits without explicit
  approval.
- Before completion, verify that `origin/develop..HEAD` contains only this
  task's diff and that the change touches only the files the issue scopes.

## Execution

- Commit count is not acceptance. Use small coherent reviewable commits as needed,
  including remediation; never squash/amend/force-push or rewrite published history
  merely to reduce commit count.
- The selected issue authorizes ordinary safe/reversible scoped engineering
  choices. State safe assumptions and proceed autonomously. Ask only for genuine
  unresolved architecture/product direction, material public contracts/scope,
  incompatible acceptance, security/privacy expansion, licensing/redistribution,
  meaningful cost, external credentials/access, destructive/irreversible action
  or explicitly owner-reserved decisions. In `/loop`, STOP_AND_ASK stops the whole
  invocation, not just the issue.
- Break complex changes into sequential, validated steps. Inspect the result
  of each risky step before continuing.
- If a command or tool call fails, change the hypothesis, inputs, or
  remediation before retrying; do not loop on the same failure.
- Report blockers honestly; do not hide correctness or reliability gaps in
  final prose.
- Apply `.kilo/rules/35-technical-recovery.md` before owner escalation: classify
  class-A execution obstacles versus class-B decisions, try the minimum sufficient
  existing mechanism and persist changed conditions/budgets. Command delivery uses
  its bounded recovery attempts plus persistent review ordinals, not a one-failure
  stop. General non-delivery retry defaults remain unchanged; session/model changes
  alone are not diagnosis. STOP_REVISE is evidence, not restart or implicit reuse authority;
  resuming requires an explicit owner decision. Do not create follow-ups without
  explicit authorization or expand scope to keep a loop running.

## Material refactors, redesigns and generalizations

Before implementing a material refactor, redesign or generalization of an
existing working path, establish:

1. the existing working/operator path relevant to the change;
2. the operational properties that path currently depends on;
3. the intended compatibility delta.

The compatibility delta identifies only what matters: behavior being
preserved; behavior intentionally changed; new or removed operator/manual
steps; assumptions that require validation on a real environment. No new
standalone artifact or template is required; the evidence may live in the
selected issue, the PR, task progress or implementation reasoning.

- A new recurring manual step requires explicit justification.
- Loss of an existing supported operating mode requires owner or issue
  authority.
- Do not infer environment facts from architecture abstractions.
- When current environment facts matter, discover and verify them for the
  task instead of relying on stale documentation or model memory.
- Do not turn one observed environment into a universal platform requirement.

## Completion

- Do not call work complete unless the acceptance criteria are met or
  explicitly revised.
- `Done` means integrated into `develop`; it does not imply production
  release or `main` promotion.
- If a criterion cannot be met after bounded authorized recovery, document the exact
  gap. Request human input only for a genuine owner decision, not an external
  non-decision blocker; never silently weaken acceptance.

## Communication

Keep commits, PR descriptions, issue comments, and final reports concise:
what changed, why, what was validated, what remains risky, and which Forgejo
operations actually succeeded.
