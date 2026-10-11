---
description: Finish an existing PR through bounded native review and in-scope remediation
---

Finish the owner-selected Forgejo PR: $ARGUMENTS

Operate in the primary implementation context. Read AGENTS.md and its rules.
This invocation authorizes only the selected PR's linked issue scope and normal
remediation commits/pushes; no additional issue, replacement PR or merge.
An explicit owner `/loop` may reuse this procedure in its sole primary context;
selection/merge/closure authority lives only in loop.md, never in `/finish-pr`.
Apply `.kilo/rules/35-technical-recovery.md` before any terminal escalation. Its
persistent technical recovery budget coexists with the ten-review ceiling below;
every failed/COMMENT/failover whole-PR attempt consumes its reserved ordinal.

Delivery budget: at most 10 whole-PR review invocations per issue delivery,
INCLUDING the initial review, COMMENT, invalidated reviews and corrected retries.
Before the first review verify `.task_progress.md` is Git-locally excluded, then
append a delivery ledger keyed by issue/PR (and `/loop` invocation ID if applicable).
Before EVERY task dispatch reserve/persist the next review ordinal, frozen SHAs
and pending result. Record returned verdict, remediation commits and checks.
Reinvoking `/finish-pr`, changing phase, reviewer task, model or session MUST reuse
the same delivery counter, not initialize zero. If prior dispatch/count recovery
is ambiguous or unavailable, BLOCKED. Do not erase history or reset a stopped
delivery; STOP_REVISE still requires an explicit owner decision, not a new PR/name.

1. Fetch actual PR metadata and linked issue via `forgejo-mcp`. Require an open,
   unmerged canonical BioMedical-IT/scarcity-router PR targeting `develop`. Verify
   canonical remote, fetch current base/head and inspect status/branches. Use ONE
   designated delivery checkout; safely switch to the existing PR branch, or create its local
   tracking branch at the fetched PR HEAD if absent. Never create a replacement
   branch. Temporary delivery worktrees/evidence checkouts follow the recovery rule. Never stash/reset or overwrite
   others' work. Unrelated uncommitted changes preventing safe switching, local
   divergence or conflicting work require diagnosis and safe isolated recovery;
   never classify ordinary checkout inconvenience as an owner decision.
2. Require the current PR branch to be clean and match MCP HEAD. Freeze exact
   HEAD, base and merge base; run required `make check`, prepare its offline locked
   environment and inspect
   reviewer permissions and secret-safe execution strategy. Review frozen objects/current
   clean branch in the designated checkout or prepared exact-revision evidence checkout.
   The parent must not edit/switch branches while the reviewer is active.
   Missing native task/agent requires diagnosed authorized native reviewer failover
   before BLOCKED, never permission to self-review, widen permissions or use an external service.
3. Spawn a fresh foreground native `task` with `subagent_type: pr-reviewer`,
   `background: false`, no `task_id`. Supply only PR URL/number, exact expected
   HEAD/base and whole-PR review instructions with the exact checkout and prepared
   environment/source paths plus provenance (not implementation conclusions).
   Prepare and audit the acceptance-only object manifest and result capture under
   `.kilo/rules/35-technical-recovery.md#reviewer-context-and-result-capture`.
   Do not supply past findings, implementation reasoning or a requested outcome.
   The agent owns the result contract/model. Parse its JSON and require the exact
   frozen SHAs and valid field types/verdict. Consume returned findings directly.
   Recheck local HEAD/clean status; a changed or dirty checkout invalidates review.
4. Re-fetch PR metadata and canonical develop. APPROVE with empty findings,
   matching current HEAD/base SHAs, clean checkout, successful required validation
   and still open/unmerged develop target yields READY_TO_MERGE / APPROVE. Never merge.
   Changed HEAD/base invalidates approval. Diagnose external changes, malformed results
   and infrastructure failures; recover safe currentness and use a materially changed
   execution strategy within the persistent recovery/review budgets. Never race a
   concurrent writer, approve stale state or retry on a new session/model alone.
5. REQUEST_CHANGES: first check findings against accepted scope and authority.
   Genuine incompatible acceptance, architecture or material scope decisions
   yield OWNER_DECISION_NEEDED with concrete alternatives. Otherwise reproduce
   each actionable finding. Before patching check the remaining delivery budget;
   at 10 return STOP_REVISE without patches that cannot receive a fresh review.
   Remediate all in scope and add regression tests.
   Classify each as direct incomplete remediation or genuinely new semantic/
   architectural defect; record that distinction and the reviewed HEAD/verdict.
6. Run focused validation and full `make check`; inspect status, intended diff,
   full base delta, whitespace and recent commit style. Verify configured and
   effective author/committer are Adrian Tkacz <adrian.tkacz@creatidy.com>.
   Stage only intended files, commit normally and push the SAME PR branch through
   canonical Git. Never amend, squash, force-push or rewrite published history.
   Recheck remote/MCP head and return to step 2 with a NEW reviewer task.
7. Never dispatch review 11. At the 10-review bound an exact valid APPROVE may
   yield READY_TO_MERGE; genuine decisions yield OWNER_DECISION_NEEDED and finite
   infrastructure failures BLOCKED. Remaining actionable findings yield STOP_REVISE;
   summarize recurring patterns,
   especially substantial new semantic/architectural defects across repository product boundaries. Never
   reset the issue delivery's review count or patch indefinitely. The ceiling is
   not a target: stop as soon as an owner decision is clearly required.
8. COMMENT: distinguish review findings, infrastructure failure and unresolved
   judgment. Diagnose and automatically change the environment or authorized
   independent reviewer/tool path for infrastructure failure; resolve uncertainty
   through bounded evidence/adjudication, not a fabricated defect. Follow the recovery
   rule's terminal contracts only when paths/budget are exhausted or a genuine owner
   decision remains. Preserve all consumed ordinals, including malformed/failed attempts.

For `security_critical` work, ordinary APPROVE alone does not yield completion
or READY_TO_MERGE. After required validation and ordinary review pass at a stable
exact base/HEAD with no changes pending, run the additional independent
`gpt-daybreak-blue-latest` review directly through Codex, not Kilo, under
`docs/llm-operating-policy.md`. This is a pre-completion / pre-merge gate, not a
pre-implementation eligibility gate. Kilo discovery failure does not imply
`SECURITY_REVIEW_UNAVAILABLE`. Record exact reviewed base/HEAD, reviewer identity,
Codex execution path, findings and verdict; any HEAD/base change requires fresh
security review. No fallback reviewer is permitted. Apply bounded technical
recovery to direct Codex problems before declaring required access unavailable.

Return one final report: issue/PR URLs, every reviewed HEAD/base/verdict,
review ordinals, remediation commits, final exact HEAD, validation/limitations and exactly one
terminal status READY_TO_MERGE, STOP_REVISE, OWNER_DECISION_NEEDED or BLOCKED.
Optional concise final issue/PR comments are reporting only; reviewer task results
are the handoff. Do not require owner relaying, Forgejo review publication,
external orchestration, release, deployment or main promotion.
When reused by `/loop`, READY_TO_MERGE is internal; map OWNER_DECISION_NEEDED to
STOP_AND_ASK and stop the entire invocation. Other blockers/bounds terminate the
loop too, never skip to another issue. Merge/completion remain loop.md's procedure.
