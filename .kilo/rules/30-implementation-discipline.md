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

- Break complex changes into sequential, validated steps. Inspect the result
  of each risky step before continuing.
- If a command or tool call fails, change the hypothesis, inputs, or
  remediation before retrying; do not loop on the same failure.
- Report blockers honestly; do not hide correctness or reliability gaps in
  final prose.

## Completion

- Do not call work complete unless the acceptance criteria are met or
  explicitly revised.
- `Done` means integrated into `develop`; it does not imply production
  release or `main` promotion.
- If a criterion cannot be met, document the blocker and request human input.

## Communication

Keep commits, PR descriptions, issue comments, and final reports concise:
what changed, why, what was validated, what remains risky, and which Forgejo
operations actually succeeded.
