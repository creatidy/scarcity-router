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
- Before starting any reviewer session, verify the Kilo 7.8.8 effective standalone
  primary profile under the exact runtime configuration. The dedicated
  `pr-reviewer-standalone` profile must be `mode: primary` and statically deny all
  tools. Prepare a per-run `KILO_CONFIG_DIR` with a generated copy whose reviewer
  identity/mode/prompt are unchanged and whose frontmatter has only exact relative
  and matching workspace-absolute Read allows for indexed packet files under wildcard
  deny. Keep per-run `kilo.json` and `KILO_CONFIG_CONTENT`
  empty (`{}`): r1's actual standalone session denied the first allowed Read under
  `agent read:* deny` despite a passing static env-overlay result. Use synthetic
  HOME/XDG config and `KILO_DISABLE_PROJECT_CONFIG=1`; preserve the authenticated Kilo
  data path without reading `~/.config/kilo/kilo.jsonc` or exposing credential values.
  Inspect the effective agent debug output under those same variables; require the
  tracked profile to declare primary, a CLI-runnable resolved mode (`primary` or
  `all`, never `subagent`), and fail if Kilo falls back to a default agent, a tool/path
  reopens, or exact rules cannot be resolved.
  Do not infer runtime permissions from Markdown text alone.
- Run a real standalone `kilo --pure run --agent pr-reviewer-standalone` synthetic
  preflight with the complete production-equivalent packet and harmless sibling,
  parent, absolute, traversal and symlink-escape canaries before reserving a whole-PR
  ordinal. Require complete allowed reads, denial before content, exact reviewer
  identity/model dispatch, no prior session context, no forbidden tool calls and a
  full exportable native session trace. This preflight proves Kilo tool permissions
  only, not OS-level isolation. Any unauthorized bytes, incomplete allowed read,
  default fallback, missing trace or additional capability aborts before #8.
- The reviewer is one fresh standalone primary session in a dedicated workspace that
  contains only the sealed packet, never a checkout or source/environment path. No
  Task, continuation, session-resume, fork, memory or parent context is available. The
  effective reviewer tool set is native Read only on exact indexed files; Forgejo MCP,
  Git, Bash, search/list, external network tools, enumeration, delegation and write
  access are denied. The only network request is the selected authenticated model
  inference. Do not transfer #210's owner-specific directory grant to another
  delivery, and make no OS-sandbox claim.
- The reviewer independently examines the entire authorized raw packet, complete
  patch, all changed code/test sources and pinned evidence. It checks internal ID/ref/
  blob/source/digest consistency and assesses correctness, security, architecture, UX
  and tests by inspection. It must not claim direct Forgejo/Git verification or that
  it ran tests. The primary owns currentness retrieval and executable validation;
  those primary conclusions do not substitute for reviewer judgment.
- After the standalone CLI exits, the primary exports the exact session in memory and
  audits selected identity/no parent, model dispatch, all actual tool calls, every
  Read path/range/permission decision, reviewer result, unchanged packet inventory
  and fresh canonical currentness. Do not print or persist raw packet contents; retain
  the native session ID/export digest and filtered audit summary. A verdict alone
  cannot approve; a missing, malformed, incomplete or mismatched trace fails closed.
  Only an exact standalone reviewer APPROVE with empty findings, exact refs, full
  coverage, explicit UX impact and fresh matching currentness/validation can yield
  ordinary-review approval. Report dispatch metadata honestly; never infer model
  backend identity.
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
