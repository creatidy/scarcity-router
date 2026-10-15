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
persistent technical recovery budget coexists with D-069's default-ten review
ceiling; a D-069A exception is available only after exact canonical owner authority,
issue/PR scope and finite maximum are independently verified, not from the local
ledger. Every failed/COMMENT/failover whole-PR attempt consumes its reserved ordinal.

Delivery budget: default at most 10 whole-PR review invocations per issue delivery,
INCLUDING the initial review, COMMENT, invalidated reviews and corrected retries.
Only an exact, canonical owner-authorized D-069A exception can raise the ceiling for
the selected issue/PR. Before the first review verify `.task_progress.md` is Git-locally excluded, then
append a delivery ledger keyed by issue/PR (and `/loop` invocation ID if applicable).
Before EVERY whole-PR review Task dispatch reserve/persist the next review ordinal,
frozen SHAs and pending result. Synthetic access-preflight Tasks are not whole-PR
reviews and consume no ordinal. Record review verdicts, remediation commits and checks.
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
   HEAD/base/merge-base; run all required validation in a parent-prepared sanitized
   environment. Prepare the complete flat, owner-read-only packet with issue acceptance,
   canonical Repo/PR metadata, owner sources, exact Git objects, patches/source copies,
   provenance and digests. The parent owns MCP/Git currentness and bundle generation
   and must not change the packet or refs while the reviewer is active. The
   `pr-reviewer` profile has no static Read grants. Default to a per-invocation
   `KILO_CONFIG_CONTENT` overlay with Read deny plus `allow` entries for every exact
   relative regular-file path in the flat packet; do not use directories or path/filename
   globs. The one owner-authorized exception is an ordinary review of issue #210 / PR
   #211: its per-invocation overlay may replace the 48 file entries with the single
   exact rule `.review-evidence-210/pr-reviewer-input/**`. It grants no other directory,
   reviewer role, issue or delivery. In every overlay set `agent.code` to `* -> deny`
   and `task -> allow`. Launch a fresh Kilo CLI parent with `--pure --agent code` and
   the exact current provider/model/variant. This context is a one-call Task transport
   only; this `/finish-pr` primary remains the sole selector, packet preparer, ledger
   owner and auditor.
   For this exception, seal the exact-revision packet as 48 files: verify the packet
   root is a real directory, every indexed relative path is present, there are no
   extra files, credentials, symlinks or hardlinks, and every entry is a regular
   single-link file. Record the complete path/SHA-256 inventory and exact Git bindings;
   use directory mode `0555` and file mode `0444`. No other parent or worker may write
   the packet while the Task is active. Recompute the complete inventory and hashes
   immediately afterward; any difference fails the parent audit. These are packet and
   workflow safeguards, not OS-level process isolation.
   Before reserving a whole-PR ordinal, run a fresh native synthetic 48-file Task
   preflight with the same directory-rule shape and prove complete reads plus
   denial-before-content for sibling, absolute, traversal and symlink paths. Verify the
   parent is Task-only and the child is Read-only. It consumes no review ordinal. The
   preflight establishes Kilo permission behavior only; it does not establish an OS
   boundary. Before a later #210 review, rebuild and validate its current exact-revision
   packet and use only `.review-evidence-210/pr-reviewer-input/**`. The 2026-10-15
   direct owner instruction did not authorize ordinal #10 or Daybreak; this grant does
   not reserve or authorize either. Missing native task/agent requires diagnosed
   authorized native failover before BLOCKED; never widen reviewer tools.
3. Start a fresh Kilo CLI Task parent with the exact packet overlay and spawn one fresh
   foreground native `task` with `subagent_type: pr-reviewer`, `background: false`,
   no `task_id`. The parent transport may not read the packet, access Forgejo, run
   shell/Git commands or audit/interpret the result. Supply only the prepared packet
   path, packet digest, expected HEAD/base/merge-base, and independent whole-PR review
   instructions. Do not pass past findings, implementation reasoning, PR-body history
   or a desired verdict. The reviewer has native Read only; Bash/Git/Forgejo/search/list/
   task/history access is denied at runtime. It independently inspects raw packet
   evidence and returns the stable JSON contract. The reviewer does not execute commands.
4. Parent audits the actual native task export: only permitted Read calls, exact bundle
   paths/ranges/bytes, complete source/patch coverage, no symlinks, and no Bash/Git/
   Forgejo/other tool calls. For the #210 directory exception, independently verify
   every Read was index-listed and the pre-/post-Task 48-file path/hash inventories
   match; the directory grant does not relax full-PR coverage. Re-fetch canonical
   PR/issue/develop and recheck the local clean HEAD/base/merge-base after the task.
   APPROVE with empty findings, matching current refs, parent-audit PASS and successful
   required validation yields READY_TO_MERGE / APPROVE. Never merge here. Any changed
   HEAD/base invalidates approval. Diagnose malformed packets/results and currentness
   failures with bounded changed-condition recovery; never race a concurrent writer or
   widen permissions.
5. REQUEST_CHANGES: first check findings against accepted scope and authority.
   Genuine incompatible acceptance, architecture or material scope decisions
   yield OWNER_DECISION_NEEDED with concrete alternatives. Otherwise reproduce
   each actionable finding. Before patching check the remaining delivery budget;
   at the exact effective maximum return STOP_REVISE without patches that cannot
   receive a fresh review in the remaining capacity. No owner exception transfers
   to another issue/PR.
   Remediate all in scope and add regression tests.
   Classify each as direct incomplete remediation or genuinely new semantic/
   architectural defect; record that distinction and the reviewed HEAD/verdict.
6. Run focused validation and full `make check`; inspect status, intended diff,
   full base delta, whitespace and recent commit style. Verify configured and
   effective author/committer are Adrian Tkacz <adrian.tkacz@creatidy.com>.
   Stage only intended files, commit normally and push the SAME PR branch through
   canonical Git. Never amend, squash, force-push or rewrite published history.
   Recheck remote/MCP head and return to step 2 with a NEW reviewer task.
7. Never dispatch beyond the exact effective canonical ceiling. A valid explicit
   exception is D-069A-only and applies solely to its verified issue/PR/scope. An
   exact ordinary APPROVE may satisfy only ordinary review; if security-critical
   owner review remains, reserve and persist the next owner-manual security review
   ordinal against the exact frozen refs before returning
   READY_FOR_OWNER_SECURITY_REVIEW and stop. Name that reserved ordinal in the handoff;
   its pending slot blocks further work, cannot be reused, and any launched attempt stays consumed
   regardless of outcome. Genuine
   decisions yield OWNER_DECISION_NEEDED and finite infrastructure failures BLOCKED.
   Remaining actionable findings yield STOP_REVISE; summarize recurring patterns,
   especially substantial new semantic/architectural defects across product boundaries.
   Never reset the issue delivery's review count or patch indefinitely. The ceiling is
   not a target: stop as soon as an owner decision is clearly required.
8. COMMENT: distinguish review findings, infrastructure failure and unresolved
   judgment. Diagnose and automatically change the environment or authorized
   independent reviewer/tool path for infrastructure failure; resolve uncertainty
   through bounded evidence/adjudication, not a fabricated defect. Follow the recovery
   rule's terminal contracts only when paths/budget are exhausted or a genuine owner
   decision remains. Preserve all consumed ordinals, including malformed/failed attempts.

For `security_critical` work, ordinary APPROVE alone does not yield completion
or READY_TO_MERGE. For Scarcity Router, after required validation and a fresh ordinary
APPROVE at frozen base/HEAD with no changes pending, reserve and persist the next
owner-manual security review ordinal and exact refs in the same delivery ledger before
returning `READY_FOR_OWNER_SECURITY_REVIEW`. Adrian personally performs only that
reserved mandatory independent `gpt-daybreak-blue-latest` review through official Codex.
The implementation agent must never invoke Daybreak/Codex automatically or substitute
another model. Name the reserved ordinal in the open/unmerged PR handoff. A pending
reservation blocks further work and cannot be reused; once launched it remains consumed
regardless of success/failure/invalidated output. Verify the owner-supplied manual result
per D-069A: official Codex executable/version/provenance, actual requested-model
invocation/dispatch, captured process/session/turn, immutable input/output, exact refs,
structured verdict and independent context/tool audit. For this Router manual path only,
`RUNTIME_UNOBSERVABLE` may satisfy the identity-observation part when the supported
runtime does not expose backend identity and Adrian explicitly accepts that limit after
all other evidence passes. Never claim `RUNTIME_VERIFIED` or infer identity from config,
CLI text or self-report; `DISPATCH_VERIFIED` is configuration only. An observed mismatch
or unauthorized substitution is `FAIL`. Generic identity policy remains unchanged.
Any HEAD/base change invalidates approval. No fallback reviewer.

Return one final report: issue/PR URLs, every reviewed HEAD/base/verdict,
review ordinals, remediation commits, final exact HEAD, validation/limitations and exactly one
terminal status READY_TO_MERGE, READY_FOR_OWNER_SECURITY_REVIEW, STOP_REVISE,
OWNER_DECISION_NEEDED or BLOCKED. READY_FOR_OWNER_SECURITY_REVIEW is a non-merged
manual owner gate, not security acceptance.
Optional concise final issue/PR comments are reporting only; reviewer task results
are the handoff. Do not require owner relaying, Forgejo review publication,
external orchestration, release, deployment or main promotion.
When reused by `/loop`, READY_TO_MERGE is internal; map OWNER_DECISION_NEEDED to
STOP_AND_ASK and stop the entire invocation. Other blockers/bounds terminate the
loop too, never skip to another issue. Merge/completion remain loop.md's procedure.
