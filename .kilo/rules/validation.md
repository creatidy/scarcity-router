# Validation Rules

## Required Checks

- Run the repository's final validation before finalizing changes: `make
  check` (full test suite plus typecheck).
- Python changes require `basedpyright` invoked through the uv-managed
  environment (`uv run basedpyright`); plain `pyright` is not an acceptable
  substitute.
- Provider/capacity work requires redacted fixture and contract tests;
  selection work requires deterministic policy tests and explanation
  assertions.
- Run `uv sync --only-dev`, `make check`, `uv run basedpyright` and
  `git diff --check` before committing or opening/updating a PR. Preserve the
  existing repository gate; do not add Model Intelligence's Ruff gate or weaken
  type rules. Inspect staged whitespace with `git diff --cached --check` too.

## Review and Handoff

- Before commit inspect status, intended diff, recent commit style and full delta
  against the recorded canonical develop SHA. Stage explicit intended files only;
  exclude secrets, caches, runtime state and `.task_progress.md`. Verify exact
  HEAD, base ancestry and clean status afterwards; never undo others' work.
- Parent prepares the offline locked development environment at exact clean PR HEAD
  in the designated delivery or temporary evidence checkout under the recovery rule.
  Use explicit sanitized child environments, synthetic HOME/cache/temp and fixture
  values for tests that can observe inherited state. Never expose ambient secrets,
  mount credential directories unnecessarily or print environment values. Prefer
  read-only required mounts with separate writable synthetic scratch paths.
  No edits or branch switches to reviewed checkouts during review.
  Reviewer inspects frozen Git objects/full base delta and verifies HEAD
  and clean status before/after checks. Ignored validation artifacts are allowed;
  tracked edits and Git/Forgejo mutations are not. Inspect checks before running;
  an allowlist does not make arbitrary repository code safe.
- Unavailable safe validation/tool state requires bounded technical recovery under
  `.kilo/rules/35-technical-recovery.md`, not immediate owner escalation or skipped
  checks. Independent source verification may use a separately prepared exact public
  pinned checkout; reviewer verifies claims and provenance, not parent conclusions.
- `/finish-pr` records each reserved ordinal, reviewed HEAD/base/verdict, remediation
  commits, checks and currentness in the excluded delivery ledger. Only an exact
  native reviewer result plus final MCP/Git currentness and required validation
  can yield READY_TO_MERGE. Standalone finish never merges or closes the issue.
- Handoff includes issue/PR URLs, exact base/HEAD, substantive files, executed
  checks/results and genuine blockers. Never claim unobserved validation or writes.

## Test Quality

- Start with focused tests while iterating, then finish with the full
  repo-level validation.
- Keep tests deterministic and behavior-focused. Mock external boundaries
  such as network, credentials, subprocesses and clocks; never mock the
  behavior being tested.
- Use fixtures shaped like the real producer output, structurally
  representative and redacted; do not prove behavior only with invented
  fields, and never record real credentials or personal quota values.

## Runtime Safety

- Keep imports safe without requiring runtime secrets or provider access at
  import time.
- Preserve safe behavior when provider credentials or access are
  unavailable: operational provider states remain normalized status data and
  tests must not depend on live quota.
