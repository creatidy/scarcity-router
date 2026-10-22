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
  Under D-077's direct owner amendment, the sole authorized primary may start ordinary
  Kilo review through a single initial Agent Manager prompt using
  `pr-reviewer-standalone`; the session inherits the current primary model unless the
  owner explicitly selects another. Include exact refs, complete compact diff and
  acceptance, then retrieve the structured native result without owner copy/paste.
  No programmatic hidden launcher, Task/subagent, model subprocess, background/HTTP
  inference or generated wrapper. Daybreak remains owner-manual through official
  Codex. If the visible Manager result path fails, preserve the attempted ordinal and
  report its exact failure instead of bypassing the gate or claiming no eligible work.
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
- Exact #212/PR #213 combined exception (owner 2026-10-20T06:26:22Z, reaffirmed
  18:58:08Z): #1-19 consumed outcomes stay, including #19 REQUEST_CHANGES/audit PASS;
  #20 reserved/unconsumed for ONE full implementation AND security review in the
  owner's visible Codex on the NEW HEAD. Both explicit verified approvals, complete
  material scope and no unresolved blockers are required. Security-only/nonpositive/
  incomplete/unverified results stop for Adrian. Default ten elsewhere; no #21.
- `CLEAN_WORKFLOW_READY_FOR_OWNER_COMBINED_REVIEW` is descriptive handoff, not
  acceptance or READY_TO_MERGE. No ordinary #212 Kilo review or #20 consumption
  during cleanup/tests. Only actual acceptance/currentness plus explicit applicable
  `/loop` authority permits merge/closure. Never main, release or deployment.
