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

- Before commit, the primary inspects status, intended diff, recent commit style and
  full delta against the recorded canonical develop SHA. Stage explicit intended
  files only; exclude secrets, caches, runtime state and `.task_progress.md`. Verify
  exact HEAD, base ancestry and clean status afterwards; never undo others' work.
- The primary prepares the offline locked development environment at exact clean PR
  HEAD, in the designated delivery or temporary evidence checkout under the recovery
  rule. Tests observing inherited state receive explicit synthetic HOME/cache/temp and
  fixture values. Never expose ambient secrets, mount credential directories
  unnecessarily or print environment values. No edits or branch switches to source
  checkouts occur while review is active.
- The primary independently fetches canonical Repo/Issue/PR and linked owner records,
  verifies IDs/URLs/authors/timestamps/bodies/digests and exact HEAD/base/merge-base/Git
  object bindings, then prepares a complete immutable indexed packet. Include the raw
  acceptance/owner sources, full PR patch, every changed source and test, source ranges,
  and exact pinned external evidence when required. Bind every file to origin,
  revision and digest. The packet must exclude credentials, prior review results,
  unexpected files, symlinks and hardlinks.
- Before creating any reviewer Task, verify the actual Kilo 7.8.8 effective parent and
  reviewer permission sets under the exact runtime configuration. The committed
  `pr-reviewer` profile statically denies all tools. Prepare a per-run `KILO_CONFIG_DIR`
  with a byte-identical copy of that canonical profile and use `KILO_CONFIG_CONTENT`
  for parent Task-only and exact reviewer Read paths. Inspect the effective agent
  debug/config result under those same variables; fail before Task if static, global,
  managed, saved or per-run configuration reopens a forbidden capability or the rules
  cannot be resolved. Do not infer effective permissions from Markdown text alone.
- Run a real native Task preflight with the complete production-equivalent packet
  and harmless canaries before reserving a whole-PR ordinal. Require complete allowed
  reads and denial before content for sibling, absolute, traversal and symlink escapes.
  This preflight proves Kilo tool permissions only, not OS-level isolation. If any
  unauthorized bytes are returned or an allowed read is incomplete, abort before the
  whole-PR Task. Pre-access effective-rule verification and this native preflight are
  separate controls.
- The Kilo `code` parent is a fresh `--pure` CLI context with Task-only effective
  access. Its native Task launches the same `pr-reviewer` identity against a sealed
  packet-only workspace, not a checkout or source/environment path. Reviewer access is
  Read-only to the exact indexed files; direct Forgejo MCP, Git, Bash, search/list,
  network, memory, enumeration, delegation and write access remain denied. Do not
  transfer #210's owner-specific directory grant to another delivery.
- The reviewer independently examines the entire authorized raw packet, complete
  patch, all changed code/test sources and pinned evidence. It checks internal ID/ref/
  blob/source/digest consistency and assesses correctness, security, architecture, UX
  and tests by inspection. It must not claim direct Forgejo/Git verification or that
  it ran tests. The primary owns currentness retrieval and executable validation;
  those primary conclusions do not substitute for reviewer judgment.
- After Task, the primary independently audits the actual parent/child trace, all
  reviewer Read paths/ranges/permission decisions, unchanged packet inventory and
  fresh canonical currentness. A returned verdict alone cannot approve; a missing,
  malformed, incomplete or mismatched trace fails closed. Only an exact native reviewer
  APPROVE with empty findings, full coverage, UX assessment and a fresh matching
  currentness/validation gate can yield the ordinary-review approval.
- Unavailable safe validation/tool state requires bounded technical recovery under
  `.kilo/rules/35-technical-recovery.md`, not immediate owner escalation or skipped
  checks. Independent pinned public-source material is staged in the packet so the
  reviewer can inspect it without external access.
- `/finish-pr` records each reserved ordinal, reviewed HEAD/base/verdict, remediation
  commits, validation, packet hashes and currentness in the excluded delivery ledger.
  Standalone finish never merges or closes the issue. `/loop` alone retains merge and
  completion authority after all gates.
- Handoff includes issue/PR URLs, exact base/HEAD, substantive files, executed checks,
  packet/preflight/trace evidence and genuine blockers. Never claim unobserved
  validation or writes.

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
