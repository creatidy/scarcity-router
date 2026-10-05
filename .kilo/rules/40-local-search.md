# Local Search and Working Context

- Use semantic/index search for conceptual discovery, grep for exact identifiers,
  config keys/error strings, glob for paths, and local reads for authoritative
  source. Open the source behind a hit before relying on it. An empty incomplete
  index is not proof of absence.
- Prefer the smallest relevant file set. Do not scan unrelated repositories or
  use Forgejo MCP as a substitute for local checkout reads.
- After edits use diffs, tests and checks, not repeated full rereads just to confirm
  edits applied. Read further only when context or diagnosis requires it.
- For sufficiently complex work keep short acceptance/decision/command/result/
  blocker state in `.task_progress.md`. Before creation add it to the repository's
  Git's local exclude file (`git rev-parse --git-path info/exclude`). If already
  excluded, verify with `git check-ignore -v .task_progress.md` rather than editing
  the exclusion again. Never commit it or add it to .gitignore.
  Local notes and conversation memory do not select or authorize future issues.
- `/loop` and `/finish-pr` append an excluded issue-delivery review ledger: invocation
  ID where applicable, issue/PR/branch, frozen base/HEAD, ordinal reserved BEFORE
  dispatch, result, commits/checks and terminal state. Recover it across reentry or
  model/session changes; never reset a counter or erase earlier delivery history.
  Missing/ambiguous recovery is BLOCKED. This bounds operation, not issue authority:
  eligibility/priority/dependencies/acceptance/current state come from refreshed
  canonical evidence, never from the ledger. Do not introduce a controller database.
- Append technical recovery classification, diagnosis, changed conditions, attempts,
  elapsed recovery time and result to the same ledger. Before escalation persist
  the five owner-decision fields or exhausted-path/external-dependency evidence from
  `.kilo/rules/35-technical-recovery.md`; never reset budgets through reentry.
