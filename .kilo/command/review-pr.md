---
description: Read-only independent review in a fresh user-visible Kilo session
---

Review the selected canonical Forgejo PR: $ARGUMENTS

Read `.kilo/rules/35-technical-recovery.md`, `.kilo/agents/pr-reviewer.md`, and the `Independent Review —
Conversational Separation` section in `AGENTS.md`. These are instructions for a fresh,
separate reviewer session. An authorized `/loop` or `/finish-pr` orchestrator starts
ordinary Kilo review through native Agent Manager; user visibility does not require
the owner to start the session. The reviewer must not be the implementation context
or a resumed implementation conversation. Do not use Task, shell/Python/HTTP launchers,
background inference, generated wrappers or silent model substitution.

The orchestrator verifies canonical Repo/PR/linked Issue, legitimate owner authority,
and exact HEAD/base/merge-base against Git objects before dispatch. It supplies those
refs, acceptance, relevant contracts and changed paths. The reviewer independently
uses the read-only Git/Forgejo getters allowed by `.kilo/agents/pr-reviewer.md` to
inspect the complete diff and intended source. The same checkout and Git object
database may be used; no separate worktree is required. Do not request owner approval
for routine reads. If source or authority evidence remains materially incomplete,
report the exact limitation. Use canonical records and native session references, not
local receipt folders. Access only authorized source, never credentials, implementation
conversations or unrelated work.
Do not write source/platform state, run untrusted code with ambient secrets, commit,
push, publish a formal review, merge or change permissions. If required source or
safe access is unavailable, report the exact limitation rather than bypass it.

Inspect the COMPLETE PR's actual changes: every canonical compact diff hunk,
added/modified/deleted regions and sufficient immediate/dependency context. A
complete `--binary --full-index --no-ext-diff --no-renames --unified=3` Git diff is
authoritative; do not substitute an LLM summary or silently drop hunks. Full unchanged
file bodies and references are optional unless needed for a material question.
No persistent `.review-evidence-*` folder, source copy, model transcript, preflight
model invocation or generated per-run launcher is required.

Prioritize P0/P1 and material P2 correctness/security/contract/regression findings
with concrete consequences and frozen file/line references. Incidental P3 is
nonblocking absent a material acceptance defect; no speculative scope expansion or
endless marginal search. Assess UX explicitly (`none` or concrete consequences).
Actual source/identity/context integrity and unresolved material uncertainty remain
blocking; optional unread lines or absence of reviewer-run tests alone do not.

D-081: ordinary independent correctness review includes applicable security assessment;
security-critical changes require rigorous complete-PR assessment by a sufficiently
capable model. P0/P1, unresolved material security findings and data-integrity risks
block integration, never deferred to the final audit. Daybreak is not a PR merge gate;
its final project-level frozen-candidate audit is a production-release gate under
`docs/llm-operating-policy.md#final-release-candidate-audit`, not a second mandatory
PR reviewer. Do not invoke it. D-078 conversational independence remains unchanged.

Return exact reviewed refs, verdict APPROVE/REQUEST_CHANGES/COMMENT, material
findings, limitations, checks actually performed and UX impact as the final result
of this reviewer session. The orchestrator collects it through native session
communication/retrieval; the owner must not relay it. Never infer approval from tests,
status labels or exit0. Record only minimal native session reference/outcome metadata
canonically; do not copy a large transcript into the repository. Any HEAD/base change
invalidates approval.

Default ten and finite integrated D-069A owner exceptions remain; unintegrated
D-069A grants none. #212/PR #213 is complete with all #1-20 consumed, no #20 reuse,
#21 or transfer. Preserve canonical history/D-077 rather than obsolete handoffs.
PR #209 remains owner-paused with its unresolved SQLite risk. This read-only command
never resumes that delivery, merges, closes issues, releases or deploys.
