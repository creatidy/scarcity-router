---
description: Remediate one selected PR and hand off outstanding gates to a user-visible reviewer
---

Finish only the owner-selected Forgejo PR: $ARGUMENTS

## Current #212 Status

This notice supersedes conflicting historical #212 references below: #212/PR #213
is complete, all #1-20 are consumed, #20 approval is only at1dcb4a0, cleanupcddcac0
was separately accepted, and merge121ee3f/#212 closure are verified. No #20 reuse,
#21 or transfer. Other deliveries retain their own verified scopes and gates.

Read AGENTS.md and `.kilo/rules/35-technical-recovery.md`. Fetch the exact canonical Repo/PR/linked Issue and
directly linked owner decisions through Forgejo MCP; verify IDs, URLs, authors,
acceptance and currentness. Never select unrelated work. The implementation agent
may code, test, use Git and recover routine execution problems autonomously, but
must not invoke another model, start a session or delegate to a hidden reviewer.

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
5. The USER explicitly selects the model and starts a separate fresh user-visible
   Kilo or official Codex session with read-only source access. Never programmatically
   start it: no script/subprocess, batch model CLI, HTTP inference, Task/subagent,
   background model job or generated convenience wrapper. No persistent
   `.review-evidence-*` folders or renamed equivalent are created or required.
   Deterministic offline fixtures may use cleaned OS temporary directories only.
6. Hand off the instructions/source references in the visible harness. Do not pass
   implementation reasoning, previous reviewer conclusions, credentials or a desired
   verdict. If the next step cannot be performed through an authorized visible
   harness, return an explicit handoff naming the pending acceptance gate. This
   does not skip review or establish that the repository has no eligible work.
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

## Security And #212 Exception

Security-critical Daybreak is owner-manual through official Codex, selected visibly
as `gpt-daybreak-blue-latest`, never through Kilo or a script. It is not a
pre-implementation eligibility gate; Kilo discovery failure does not imply
`SECURITY_REVIEW_UNAVAILABLE`. The implementation agent does not invoke it.
Preserve exact refs, official execution/model provenance and independent context/tool
checks; never infer backend identity. Use the existing Router owner-manual identity
contract, including explicit owner acceptance for a permitted unobservable runtime.

For #212/PR #213 only, the direct 2026-10-20T06:26:22Z combined exception remains:
consumed #1-19 (including #19 REQUEST_CHANGES/audit PASS), maximum20, #20 reserved
but unconsumed for ONE owner-manual complete implementation AND security review.
No more ordinary Kilo, #21/reset/automatic extension. The owner reaffirmed this on
2026-10-20T18:58:08Z while retiring all local review directories and launchers.
Canonical history/provenance: Issue #212 comment #18011 and D-077. Historical
source files have been removed by owner decision; those records do not pretend
the bytes remain available or turn agent transcriptions into owner-authored evidence.
The current direct owner instruction/native conversation supplies the reaffirmation.

Provide the NEW exact HEAD and direct review instructions; Adrian manually opens
official Codex and selects Daybreak. No local packet/history reconstruction is a
prerequisite. Require explicit verified approval of BOTH correctness and security,
complete material PR scope and no unresolved blockers. A security-only, nonpositive,
incomplete or unverified result is preserved and stops for Adrian. No actual review
is consumed by deterministic checks. A descriptive owner handoff is not acceptance
or READY_TO_MERGE; only an explicit applicable `/loop` can later merge/close after
actual combined acceptance and fresh currentness. Never deploy/release/promote main.
