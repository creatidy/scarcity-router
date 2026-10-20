# Validation Rules

Use `.kilo/rules/35-technical-recovery.md` for bounded secret-safe execution recovery;
it never authorizes a hidden model call.

## Required Checks

- Start with focused deterministic tests, then run `uv sync --only-dev`, `make
  check`, `uv run basedpyright`, `make package-check` when packaging/workflow is
  affected, `git diff --check` and staged whitespace before commit/PR update.
- Use repo-managed basedpyright through uv; no plain pyright, weakened rules,
  exclusions or baselines. Preserve the repository gate; no unrelated lint gate.
- Provider/capacity changes need redacted representative fixture/contract tests;
  selection changes need deterministic policy/explanation tests. No live quota.
- Tests/builds/static analysis/Git/hash operations perform NO development/review
  model inference. Model-response fixtures are synthetic/offline. No test/Make/CI/
  hook may spawn a real reviewer or call an inference endpoint for orchestration.
- Tests that observe inherited state receive deliberate synthetic HOME/cache/temp
  and minimal variables, not ambient credentials. Do not mount/copy credential
  directories or print secret values. Imports remain safe without provider access.

## Review And Handoff

- Inspect status, intended/full base diff, recent commit style, exact refs/ancestry
  and scope. Stage explicit files only; exclude secrets, caches and local state.
  Preserve unrelated owner work; never stash/reset it or rewrite published history.
- Freeze canonical HEAD/base/merge-base and acceptance/legitimate owner authority.
  Use actual Git objects and complete compact diff/changed-source regions with
  useful context. No LLM summaries or silent hunk omission. Additional unchanged
  context is a material reviewer judgment, not blanket full-file bureaucracy.
- Independent review is a fresh separate read-only USER-VISIBLE Kilo/Codex session.
  The user selects/starts the model explicitly. No programmatic launcher, Task,
  subprocess, background/HTTP inference or generated owner wrapper. If a visible
  mechanism is unavailable, hand off the exact gate rather than bypass it or
  declare the repository has no eligible work.
- Preserve source integrity, scoped permissions, no credentials/unapproved context,
  actual model/execution identity, terminal/native-session provenance and exact
  currentness. Do not infer backend identity from self-report/UI labels. Tests,
  exit0, a handoff or an agent-written comment alone are never approval. Any
  HEAD/base change invalidates approval; no writes/switches during active review.
- Record minimal real ordinals/outcomes/refs/provenance canonically. Native records
  may be referenced without large local transcript copies. No persistent
  `.review-evidence-*` folder, source duplicate, renamed receipt hierarchy or local
  history reconstruction is required for eligibility/review/merge/acceptance.
  Deleted historical original files are unavailable; their old hashes or agent
  transcriptions do not recreate owner authority. Do not reset consumed history.
- Retained `tools/reviewer_packet.py` checks are deterministic offline validators,
  not harness connectors, model launchers or mandatory workflow infrastructure.
  Their synthetic fixtures use cleaned OS temp space outside worktrees only.

## Security And Delivery

- Daybreak is manually selected/opened by Adrian in official Codex, never a script
  or Kilo call. It is not a pre-implementation eligibility gate; Kilo discovery
  failure does not imply `SECURITY_REVIEW_UNAVAILABLE`. Preserve real provenance
  and model-bound gates, no fallback reviewer or automatic retry.
- Completed #212/PR #213 exception: all #1-20 consumed, #19 REQUEST_CHANGES/audit PASS
  unchanged. Owner confirmed #20 combined approval only at1dcb4a0 and separately
  accepted verified cleanupcddcac0 on2026-10-20T21:10:00Z, integrated as121ee3f/closed.
  No #20 reuse/#21, source review attribution to the cleanup or gate transfer.
  Other deliveries retain default ten/their own verified scope and both gates.
- `CLEAN_WORKFLOW_READY_FOR_OWNER_COMBINED_REVIEW` is descriptive handoff, not
  acceptance or READY_TO_MERGE. No ordinary #212 Kilo review or #20 consumption
  during cleanup/tests. Only actual acceptance/currentness plus explicit applicable
  `/loop` authority permits merge/closure. Never main, release or deployment.
