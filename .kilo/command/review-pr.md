---
description: Read-only independent review in a fresh user-visible Kilo session
---

Review the selected canonical Forgejo PR: $ARGUMENTS

Read `.kilo/rules/35-technical-recovery.md` and the `Independent Review —
Conversational Separation` section in `AGENTS.md`. These are instructions for a fresh,
separate reviewer session. An authorized `/loop` or `/finish-pr` orchestrator starts
ordinary Kilo review through native Agent Manager; user visibility does not require
the owner to start the session. The reviewer must not be the implementation context
or a resumed implementation conversation. Do not use Task, shell/Python/HTTP launchers,
background inference, generated wrappers or silent model substitution.

Verify canonical Repo/PR/linked Issue, exact acceptance/legitimate owner authority,
HEAD/base/merge-base and the intended source. The reviewer may use the same checkout
and Git object database; a separate worktree is not required. Use actual source,
canonical records and native session references, not local receipt folders. Access
only authorized source, never credentials, implementation conversations or unrelated
work.
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

Return exact reviewed refs, verdict APPROVE/REQUEST_CHANGES/COMMENT, material
findings, limitations, checks actually performed and UX impact as the final result
of this reviewer session. The orchestrator collects it through native session
communication/retrieval; the owner must not relay it. Never infer approval from tests,
status labels or exit0. Record only minimal native session reference/outcome metadata
canonically; do not copy a large transcript into the repository. Any HEAD/base change
invalidates approval.

Default ten and finite integrated D-069A owner exceptions remain for other deliveries;
unintegrated D-069A grants none. For #212/PR #213, no further ordinary Kilo review is authorized. #1-19 stay consumed;
#20 remains reserved/unconsumed for Adrian's ONE manually opened official Codex
`gpt-daybreak-blue-latest` combined full-PR correctness AND security review. Verify
the original direct owner decision/current reaffirmation via the native owner
conversation and canonical acceptance/D-077; Issue #212 comment #18011 is an agent
metadata transcription, not owner-authored evidence. No deleted local source is
required or claimed available. Both explicit approvals and verified execution/
context/currentness are required; a security-only or incomplete/unverified/nonpositive
result stops for Adrian. No script, automatic invocation, #21, merge or issue closure.
