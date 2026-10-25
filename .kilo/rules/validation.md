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
- Independent review is a fresh separate read-only USER-VISIBLE Kilo session.
  Authorized `/loop` and `/finish-pr` orchestrators start and collect it through
  native Kilo session management; do not require the owner to start it or relay
  results. A separate worktree is not required. No Task/subagent, subprocess,
  background/HTTP inference or generated wrapper may launch a model. If the native
  session/result capability is genuinely unavailable after bounded recovery, report
  the exact gate rather than bypassing it or declaring the repository has no eligible
  work.
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

- D-081 requires ordinary independent correctness review including applicable
  security assessment of the complete frozen PR. Security-critical changes receive
  rigorous sufficiently capable review; P0/P1, unresolved material security findings
  and data-integrity risks block integration and must be corrected/independently verified.
- Daybreak is not a PR merge gate. The final comprehensive frozen release-candidate
  audit is a production-release acceptance gate, not an implementation/develop
  integration prerequisite. No routine per-PR manual-security reservation/handoff.
  Audit scope and future access verification: `docs/llm-operating-policy.md#final-release-candidate-audit`.
  No Daybreak run, provisioning, credentials or production action is authorized.
- #212/PR #213 is complete, all #1-20 consumed, no #20 reuse/#21/transfer. Preserve
  canonical history and D-077, not obsolete pending handoffs. PR #209 remains owner-paused
  with its unresolved SQLite transition race; it is neither waived nor deferred.
- Only actual ordinary acceptance/currentness plus explicit applicable `/loop`
  authority permits merge/closure, without another routine owner confirmation.
  Never main, release or deployment.
