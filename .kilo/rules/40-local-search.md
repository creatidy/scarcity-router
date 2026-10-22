# Local Search and Working Context

- Use semantic/index search for conceptual discovery, grep for exact identifiers,
  config keys/error strings, glob for paths, and local reads for authoritative
  source. Open the source behind a hit before relying on it. An empty incomplete
  index is not proof of absence.
- Prefer the smallest relevant file set. Do not scan unrelated repositories or
  use Forgejo MCP as a substitute for local checkout reads.
- After edits use diffs, tests and checks, not repeated full rereads just to confirm
  edits applied. Read further only when context or diagnosis requires it.
- Optional short acceptance/decision/command/result/blocker notes may use
  `.task_progress.md`. Before creation add it to the repository's
  Git's local exclude file (`git rev-parse --git-path info/exclude`). If already
  excluded, verify with `git check-ignore -v .task_progress.md` rather than editing
  the exclusion again. Never commit it or add it to .gitignore.
  Local notes and conversation memory do not select or authorize future issues.
- Record minimal review ordinals/actual outcomes, exact refs and legitimate decision
  provenance in canonical Issue/PR history and existing decision documentation.
  Native harness records may be referenced without duplicating large transcripts.
  Never reset counters or manufacture unavailable original owner evidence from an
  agent transcription. Local notes/directories are not eligibility, review, merge
  or acceptance prerequisites. No `.review-evidence-*` folders, renamed receipt
  hierarchies, source archives, evidence database or persistent review runner.
- Keep technical recovery classification, changed conditions and consumed budgets
  concise, canonically where needed. Before escalation record
  the five owner-decision fields or exhausted-path/external-dependency evidence from
  `.kilo/rules/35-technical-recovery.md`; never reset budgets through reentry.
