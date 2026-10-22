---
description: Remediate one selected PR and hand off outstanding gates to a user-visible reviewer
---

Finish only the owner-selected Forgejo PR: $ARGUMENTS

Read AGENTS.md and `.kilo/rules/35-technical-recovery.md#reviewer-context-and-result-capture`.
Fetch the exact canonical Repo/PR/linked Issue and
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
    Check `.kilo/rules/35-technical-recovery.md#prospective-review-capacity` before
    remediation or reserving any review ordinal.
3. Understand/reproduce genuine accepted-scope findings, apply the smallest fix
   and meaningful deterministic tests. Follow bounded secret-safe recovery; ask
   only genuine owner-controlled decisions. Run `uv sync --only-dev`, focused
   checks, `make check`, `uv run basedpyright`, `make package-check` where relevant,
   and working/staged whitespace checks. Inspect the complete intended/base delta,
   commit explicit files under the owner identity and push the same PR normally.
 4. Freeze exact Repo/Issue/PR identities, HEAD/base/merge-base, review authorization,
    acceptance, validation and complete canonical compact diff. Reserve the next
    ordinal minimally and canonically before the imminent review dispatch.
 5. Under the direct D-077 owner amendment, start one fresh read-only ordinary Kilo
    reviewer using a single initial `agent_manager` local-session prompt and the
    selectable `pr-reviewer-standalone` agent. Include the complete Git diff and exact
    issue criteria in that prompt. Resolve the current primary's exact provider-
    qualified model and pass it explicitly as `model`, with the same provider and
    known reasoning variant. Only Adrian may explicitly select another permitted
    identity for this review. Highspeed models and automatic provider fallback are not
    authorized. On model mismatch or provider error, stop without retrying or
    substitution. Retain the Agent Manager reply ID/session identity. Do not use Task,
    shell/Python/HTTP model launchers, background model jobs or generated wrappers.
    Daybreak remains owner-manual through official Codex only.
  6. Retrieve the structured reviewer JSON directly through the native Agent Manager
     `replyTo` channel; if that reply is absent after the session completes, use
     `kilo_local_recall` on that exact session. Validate schema and exact refs, then
     validate it using the pure `inspect_agent_manager_result` and
     `agent_manager_handoff_action` helpers in the existing `tools/review_result.py`;
     record the minimal outcome/session provenance canonically. Route actionable
     `REQUEST_CHANGES` directly back into this same primary implementation context;
     do not require owner copy/paste. `APPROVE` advances only to the next authorized
     gate. No persistent `.review-evidence-*` folder, source duplicate, receipt
    hierarchy or transcript archive is required; OS temporary space is transient.
    If the visible Manager result path fails, preserve the attempted ordinal and
    diagnose it rather than silently starting a second session or using a hidden
    launcher.
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
    from optional unread unchanged lines or style preferences. Remediate only within
    accepted scope and before dispatching a new whole-PR review. After diagnosed
    remediation and required validation, continue automatically with one fresh
    Agent Manager reviewer only if every remaining ordinary/restricted role fits
    within the verified cap. A changed HEAD gets no inherited verdict; never repeat
    the same failed operation without a concrete changed condition. No model fallback
     or ceiling extension. At the bound preserve history and keep the PR open and
     unmerged; report STOP_REVISE for actionable defects or the exact owner-only gate.

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
