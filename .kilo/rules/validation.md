# Validation Rules

Use `.kilo/rules/35-technical-recovery.md` for bounded secret-safe execution recovery;
it never authorizes a hidden model call.

## Required Checks

- During development run focused deterministic tests and checks appropriate to
  the change, plus working/staged whitespace checks before commit. Canonical CI
  runs the full required gate for the exact committed source or explicitly
  identified merge tree before integration: `uv sync --only-dev`, `make check`
  (including `uv run basedpyright`), `git diff --check`, and the applicable
  `make package-check` and other acceptance gates. Local full runs are for
  diagnosis or when CI cannot supply a required proof, not a routine duplicate
  before every commit.
  A local substitute must actually prove the missing boundary; it cannot turn a
  missing CI result or platform-specific check into a claimed CI/platform PASS.
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

## Test Evidence And Efficiency

- Define each scenario once where practical and do not run it twice without a
  distinct evidentiary purpose. Preserve the union of assertions when removing
  actual duplicates; checkout, installed wheel, sdist rebuild, image, upgrade,
  and native-platform boundaries may require separate runs. Do not remove a
  boundary merely because the scenario text is similar. Avoid separately
  hand-copying the same case matrix between a test runner and package script;
  execute the shared definition in deliberately chosen environments instead.
- Reuse an immutable built artifact within one run only when its identity,
  provenance and required boundary match. Keep mutable databases, directories,
  ports, process identities and fixtures independent unless shared state is the
  behavior under test. Preserve installed-runtime proof without undeclared
  dependencies; verify installed artifact origin in workers and representative
  subprocesses as well as the parent when those processes run installed tests.
- Parallelize only after verifying isolation, preferably with processes. Serialize
  or truly isolate global host state, shared services, markers and signals. Do
  not mask races with larger timeouts or automatic retries. For any phased or
  sharded suite, automatically verify complete, disjoint case identities including
  parameterized cases, not merely matching counts; report any unsupported case.
- For performance comparisons record wall-clock, principal phases, run/skip
  counts and runner conditions before and after. A single timing is an observation,
  not a future guarantee. Report unavailable native, security, external,
  failure-recovery or other required proof as UNPROVED, not PASS.
- These are prospective design and evidence requirements, not a claim that the
  existing CI, installed-artifact tests or test partitioning already meet them.

## Review And Handoff

- Inspect status, intended/full base diff, recent commit style, exact refs/ancestry
  and scope. Stage explicit files only; exclude secrets, caches and local state.
  Preserve unrelated owner work; never stash/reset it or rewrite published history.
- Freeze canonical HEAD/base/merge-base and acceptance/legitimate owner authority.
  Use actual Git objects and complete compact diff/changed-source regions with
  useful context. No LLM summaries or silent hunk omission. Additional unchanged
  context is a material reviewer judgment, not blanket full-file bureaucracy.
- Verify the successful CI run's actual checked-out commit or explicit merge tree,
  all required step results and current HEAD/base before integration; a green badge
  without source identity is insufficient. An independent reviewer inspects the
  code, assertions and relevant exact-run evidence, and runs targeted checks when
  needed; a second full CI-equivalent run is not automatic.
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
