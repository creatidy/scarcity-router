---
description: Remediate one selected PR and coordinate native independent review
---

Finish only the owner-selected Forgejo PR: $ARGUMENTS

Read AGENTS.md and `.kilo/rules/35-technical-recovery.md`. Fetch the exact canonical Repo/PR/linked Issue and
directly linked owner decisions through Forgejo MCP; verify IDs, URLs, authors,
acceptance and currentness. Never select unrelated work. The implementation agent
may code, test, use Git and recover routine execution problems autonomously. For an
ordinary review, it starts a fresh user-visible Kilo reviewer through native session
management; it does not self-review or invoke hidden inference.

1. Fetch canonical develop and the existing PR branch. Verify exact HEAD/base/
   merge-base, ancestry and status in the designated checkout. Preserve unrelated
   work; no stash/reset, history rewrite, synchronization merge or replacement PR.
2. Recover minimal review ordinals/outcomes from canonical Issue/PR records and
   existing decision documentation; native Kilo/Codex session references may be
   used where actually available. Default maximum ten, with only verified finite
   owner exceptions under D-069/D-077. For other deliveries a D-069A exception must
   already be integrated into freshly fetched develop and verified for its finite
   original owner scope; unintegrated D-069A has no authority. Reentry does not reset history. Agent-authored
   comments are transcriptions, not original owner authority. Missing original
   authority is not repaired by a local hash or an agent-written receipt.
3. Understand/reproduce genuine accepted-scope findings, apply the smallest fix
   and meaningful deterministic tests. Follow bounded secret-safe recovery; ask
   only genuine owner-controlled decisions. Run `uv sync --only-dev`, focused
   checks, `make check`, `uv run basedpyright`, `make package-check` where relevant,
   and working/staged whitespace checks. Inspect the complete intended/base delta,
   commit explicit files under the owner identity and push the same PR normally.
4. If independent review is pending, prepare concise instructions directly: exact
   Issue/PR acceptance and legitimate owner-decision references, frozen HEAD/base/
   merge-base, complete canonical compact Git diff and changed regions with useful
   context, validation and exact pending gate. Use actual Git objects and canonical
   records, not duplicated source trees, receipt hierarchies or large transcripts.
   Optional deeper source inspection is the reviewer's material judgment.
5. Start a fresh separate, user-visible ordinary Kilo reviewer session through the
   native Agent Manager using the existing primary `.kilo/agents/pr-reviewer.md` role.
   This is authorized workflow orchestration, not a hidden model
   launcher. Do not require the owner to start the session. Use the existing checkout
   when safe; a separate worktree/checkout is not an independence requirement. The
   implementation agent freezes the exact source until review completes. Never use
   scripts/subprocesses, batch model CLI, HTTP inference, Task/subagent, background
   model jobs or generated wrappers. No persistent `.review-evidence-*` folders or
   renamed equivalent are created or required.
6. Give the reviewer only task/scope, acceptance, exact PR/HEAD/base/merge-base refs,
   relevant contracts and source access instructions. Do not pass implementation
   conversation/history, reasoning, private deliberations, prior desired conclusions,
   credentials or a preferred verdict. Collect the final result through native Kilo
   session communication or exact-session retrieval; do not ask the owner to copy or
   relay prompts/results. If that native capability itself is unsupported, recover
   within the technical budget and name the exact remaining gate; do not skip review.
7. Record actual invocation ordinal, selected model/harness, native session reference
   when available, exact reviewed refs, verdict, material findings/limitations and
   explicit UX assessment minimally in canonical records. Reserve only an imminent
   authorized review or required owner handoff; a pending reservation is not execution.
   Approval needs authentic independent scope/identity, complete actual changes,
   source integrity, no unauthorized access/context and no unresolved blocking
   finding. Exit0, labels, self-report or tests alone are not approval. Any HEAD/base
   change invalidates approval; do not edit source during an active review.
8. COMMENT/REQUEST_CHANGES consume an actual attempt. Distinguish substantive
   defects, uncertainty and infrastructure failure; do not manufacture blockers
   from optional unread unchanged lines or style preferences. Remediate only when
   authorized and a subsequent independent gate can still be satisfied, or when
   the owner explicitly grants remediation-only/combined acceptance. No automatic
   model fallback, review retry or ceiling extension. At the bound report STOP_REVISE
   for actionable defects; report genuine external/decision blockers honestly.

## Security And Delivery

D-081: every consequential PR requires ordinary independent correctness review
including applicable security assessment. Security-critical changes require rigorous
review by a sufficiently capable model of the complete frozen PR. P0/P1 defects,
unresolved material security findings and data-integrity risks block integration;
correct and independently verify them, never defer them to the final audit.

Daybreak is not a PR merge gate. Its final comprehensive frozen release-candidate
audit is a production-release acceptance gate, not an implementation or develop
integration prerequisite. Follow `docs/llm-operating-policy.md#final-release-candidate-audit`;
do not invoke Daybreak or reserve routine per-PR manual-security ordinals.
Ordinary exact correctness/security APPROVE, empty findings, validation and currentness
yield READY_TO_MERGE, including security-critical PRs. Standalone finish never merges;
only an explicit applicable `/loop` can later merge/close after all acceptance gates.
No further routine owner confirmation is required inside that authorized loop.

#212/PR #213 is complete, all #1-20 consumed; retain D-077/current root notice and
canonical history, no #20 reuse or transfer. PR #209 remains owner-paused with its
unresolved SQLite finding; this timing change neither resumes nor waives it.
Never deploy/release/promote main or infer backend identity from a dispatch label.
