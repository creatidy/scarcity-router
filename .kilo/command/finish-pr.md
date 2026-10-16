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
persistent technical recovery budget coexists with the effective review ceiling below;
every failed/COMMENT/failover whole-PR attempt consumes its reserved ordinal.

Delivery budget: default at most 10 whole-PR review invocations per issue delivery,
INCLUDING the initial review, COMMENT, invalidated reviews and corrected retries.
Only a D-069A finite exception can authorize a greater ceiling for the exact named
issue/linked PR. Before reserving any ordinal above 10, the primary re-fetches the
canonical Repo, Issue, PR and directly linked owner-decision object(s) through MCP;
verifies exact identities/links, direct source provenance and digest, finite maximum,
scope and current refs; and confirms D-069A is integrated in freshly fetched `develop`.
An exception on the current branch or in its unmerged PR is not effective. Missing,
stale, ambiguous or mismatched evidence leaves the default 10 effective. Revalidate
before every exceptional reservation; local ledgers count but never grant authority.
Before the first review verify `.task_progress.md` is Git-locally excluded, then
append a delivery ledger keyed by issue/PR (and `/loop` invocation ID if applicable).
Before EVERY task dispatch reserve/persist the next review ordinal, frozen SHAs
and pending result. Record returned verdict, remediation commits and checks.
Reinvoking `/finish-pr`, changing phase, reviewer task, model or session MUST reuse
the same delivery counter, not initialize zero. If prior dispatch/count recovery
is ambiguous or unavailable, BLOCKED. Do not erase history or reset a stopped
delivery; STOP_REVISE still requires an explicit owner decision, not a new PR/name.

1. Fetch canonical Repo/Issue/PR and directly linked owner-decision objects through
   the exact Forgejo MCP getters. Require an open, unmerged canonical
   BioMedical-IT/scarcity-router PR targeting `develop`. Verify IDs, URLs, authors,
   timestamps, raw bodies/digests, owner-source provenance and exact Git refs. Use ONE
   designated source checkout, safely switch to the existing PR branch, or create
   its local tracking branch at fetched PR HEAD only if absent. Never create a
   replacement branch. Preserve unrelated owner work and use bounded isolated recovery.
2. Require a clean source checkout at exact HEAD/base/merge-base. The primary owns
   direct Forgejo/Git retrieval, full diff inspection and sanitized executable
   validation. Do not place this checkout or any environment/credential path in the
   reviewer Task. The parent must not edit/switch the source checkout while review
   workspaces are active.
3. The primary builds a complete immutable indexed packet containing the full canonical
   issue acceptance body, raw owner sources, full PR patch, every changed code/test
   file, exact source/blob IDs and ranges, and pinned external-source evidence. Bind
   every file to origin, revision and digest. Exclude PR-body content, previous review
   findings, credentials, symlinks/hardlinks, unexpected files and incomplete coverage.
   Validate the packet and exact base-to-HEAD changed-file map with
   `tools/reviewer_packet.py` before any reviewer Task.
4. Prepare a sealed packet-only workspace outside the source checkout and a per-run
   Kilo 7.8.8 permission overlay using the byte-identical canonical reviewer prompt.
   Invoke `tools/reviewer_packet.py prepare` with the source packet/manifest, frozen
   source checkout, exact pinned Kilo source checkout, isolated workspace/config
   parents, installed Kilo binary and sanitized TMPDIR/HOME. The helper validates the
   packet, prepares the workspace/overlay, and runs `kilo --pure debug agent code` and
   `pr-reviewer` under the exact production environment before any Task. Require the
   parent to expose Task only, the reviewer to expose Read only on exact packet paths,
   and the effective prompt hash to match the frozen profile. Reopened capabilities,
   missing/ambiguous rules or inability to verify fail before Task. Reuse the returned
   `KILO_CONFIG_DIR`, `KILO_CONFIG_CONTENT` and `KILO_DISABLE_PROJECT_CONFIG=1`
   unchanged. This helper validates/prepares only and never launches a Task.
5. Run a real native Task preflight against a copy of the complete production packet
   plus harmless sibling, absolute, traversal and symlink canaries. The fresh
   `kilo --pure --agent code` parent runs in the packet-only workspace with the same
   verified environment and has Task only; instruct it to make exactly one foreground
   native Task call with `subagent_type: pr-reviewer`, `background: false`, no
   `task_id` and no model override.
   Require complete allowed-file reads and deny-before-content for every escape. Audit
   the actual preflight trace and unchanged production packet inventory. Preflights
   are not whole-PR reviews and consume no review ordinal. Any failure aborts before
   whole-PR dispatch.
6. Prepare the production packet-only workspace without canaries, repeat the
   effective-agent check, and re-confirm packet/ref hashes. Then reserve/persist the
   next whole-PR ordinal before dispatch. Start a fresh foreground
   `kilo --pure --agent code`
   parent with the same verified Kilo config/environment, working directory set to the packet-only
   workspace. The parent must use only native Task, passing only packet identity/index
   and review instructions. Never pass checkout/environment paths, PR-body content,
   prior findings or parent conclusions. The child uses only native Read on exact
   `packet/<file>` paths.
7. After Task completion, audit the complete parent/child trace, every Read path and
   range/permission decision, packet post-inventory and fresh canonical currentness.
   These post-execution checks are separate from pre-access Kilo debug and native
   preflight. A returned verdict alone cannot approve; malformed, incomplete or
   mismatched traces fail closed.
   Missing native Kilo configuration/debug evidence aborts before Task; never
   self-review, substitute another reviewer, widen access or use an external service.
8. Re-fetch PR metadata and canonical develop. APPROVE with empty findings,
    matching current HEAD/base SHAs, clean checkout, successful required validation
    and still open/unmerged develop target yields ordinary APPROVE. Only a
    non-security-critical delivery with all other gates passed may return
    READY_TO_MERGE; security-critical work continues to the owner gate below. Never merge.
   Changed HEAD/base invalidates approval. Diagnose external changes, malformed results
   and infrastructure failures; recover safe currentness and use a materially changed
   execution strategy within the persistent recovery/review budgets. Never race a
   concurrent writer, approve stale state or retry on a new session/model alone.
9. REQUEST_CHANGES: first check findings against accepted scope and authority.
   Genuine incompatible acceptance, architecture or material scope decisions
   yield OWNER_DECISION_NEEDED with concrete alternatives. Otherwise reproduce
   each actionable finding. Before patching check the effective delivery budget;
   at the effective ceiling return STOP_REVISE without patches that cannot receive a fresh review.
   Remediate all in scope and add regression tests.
   Classify each as direct incomplete remediation or genuinely new semantic/
   architectural defect; record that distinction and the reviewed HEAD/verdict.
10. Run focused validation and full `make check`; inspect status, intended diff,
   full base delta, whitespace and recent commit style. Verify configured and
   effective author/committer are Adrian Tkacz <adrian.tkacz@creatidy.com>.
   Stage only intended files, commit normally and push the SAME PR branch through
   canonical Git. Never amend, squash, force-push or rewrite published history.
   Recheck remote/MCP head and return to step 2 with a NEW reviewer task.
11. The default ceiling is 10; never exceed it without the freshly verified D-069A
   authority above. At the effective review bound an exact valid APPROVE may yield
   READY_TO_MERGE; genuine decisions yield OWNER_DECISION_NEEDED and finite
   infrastructure failures BLOCKED. Remaining actionable findings yield STOP_REVISE;
   summarize recurring patterns,
   especially substantial new semantic/architectural defects across repository product boundaries. Never
   reset the issue delivery's review count or patch indefinitely. The ceiling is
   not a target: stop as soon as an owner decision is clearly required.
12. COMMENT: distinguish review findings, infrastructure failure and unresolved
   judgment. Diagnose and automatically change the environment or authorized
   independent reviewer/tool path for infrastructure failure; resolve uncertainty
   through bounded evidence/adjudication, not a fabricated defect. Follow the recovery
   rule's terminal contracts only when paths/budget are exhausted or a genuine owner
   decision remains. Preserve all consumed ordinals, including malformed/failed attempts.

For `security_critical` work, ordinary APPROVE alone does not yield completion
or READY_TO_MERGE. After required validation and ordinary review pass at a stable
exact base/HEAD with no changes pending, reserve and persist the next chronological
owner-manual security ordinal against those exact refs and return
`READY_FOR_OWNER_SECURITY_REVIEW`. The implementation agent never launches Codex or
Daybreak automatically. Adrian performs the official `gpt-daybreak-blue-latest`
review manually under `docs/llm-operating-policy.md`. Before recording any assurance
level, verify official binary/provenance, requested-model dispatch, invocation/session/
turn, immutable input/output, exact refs and independent context/tool audit. Any
HEAD/base change invalidates the reserved result and requires a fresh owner review
after ordinary currentness is restored. This mandatory pre-completion/pre-merge gate
is not a pre-implementation eligibility gate; Kilo model-discovery failure does not
imply `SECURITY_REVIEW_UNAVAILABLE`. No fallback reviewer is permitted. Preserve
failed/unavailable attempts as consumed; do not merge before the owner security gate.

Return one final report: issue/PR URLs, every reviewed HEAD/base/verdict,
review ordinals, remediation commits, final exact HEAD, validation/limitations and exactly one
terminal status READY_TO_MERGE, READY_FOR_OWNER_SECURITY_REVIEW, STOP_REVISE,
OWNER_DECISION_NEEDED or BLOCKED.
Optional concise final issue/PR comments are reporting only; reviewer task results
are the handoff. Do not require owner relaying, Forgejo review publication,
external orchestration, release, deployment or main promotion.
When reused by `/loop`, READY_TO_MERGE is internal; map OWNER_DECISION_NEEDED to
STOP_AND_ASK and stop the entire invocation. Other blockers/bounds terminate the
loop too, never skip to another issue. Merge/completion remain loop.md's procedure.
