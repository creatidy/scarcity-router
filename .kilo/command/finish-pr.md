---
description: Finish an existing PR through bounded standalone review and in-scope remediation
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
For other deliveries, a greater ceiling requires a D-069A finite exception verified
against its direct canonical owner source after D-069A is integrated in freshly fetched
`develop`. The sole exception to that mechanism is Adrian's direct, one-time D-069/D-077
amendment for Issue #212/PR #213: maximum 14 total; #1-11 remain consumed, #12 is the
next ordinary review, #13 is an additional ordinary review only after diagnosed/corrected
#12 failure or owner-manual Daybreak after valid #12 APPROVE, and #14 is the final manual
Daybreak if #13 was needed for ordinary review. No #15, reset or automatic extension.
The exact owner source is the Section D excerpt in
`.review-evidence-212/owner-d069-review-ceiling-amendment-2026-10-19-max14.txt`, from
Adrian's direct Kilo message at `2026-10-19T10:39:34Z`; D-077 binds its SHA-256. Verify
that direct source, exact Repo/Issue/PR identities, scope and current refs before any
exceptional reservation. This is not D-069A, is not transferable, and is not a general
exception mechanism. Missing, stale, ambiguous or mismatched authority leaves the
applicable default ceiling in effect. Revalidate before every exceptional reservation;
local ledgers count but never grant authority.
Before the first review verify `.task_progress.md` is Git-locally excluded, then
append a delivery ledger keyed by issue/PR (and `/loop` invocation ID if applicable).
Before EVERY whole-PR reviewer launch reserve/persist the next review ordinal, frozen SHAs
and pending result. Record returned verdict, remediation commits and checks.
Reinvoking `/finish-pr`, changing phase, reviewer process, model or session MUST reuse
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
    reviewer process. The primary must not edit/switch the source checkout while review
   workspaces are active.
3. From the clean frozen source checkout compute the sorted exact base-to-HEAD
   changed-path list and `standalone_packet_path_plan`. This path inventory, not source
   bytes, is the input to the standalone synthetic preflight. Preserve the bound HEAD,
   base and merge-base. Do not build the real evidence packet yet.
4. Create a complete production-shaped synthetic packet with
   `tools/reviewer_packet.py synthetic-packet --parent <private-temp-dir> --head <sha>
   --base <sha> --merge-base <sha> --changed-path <path>` repeated for every frozen
   changed path in sorted order, plus `--owner-evidence-path <path>` for each exact
   owner-source or transcription-receipt path listed in the packet plan. The fixture
   contains the production inventory (index, patch, pinned Kilo sources, owner sources/
   receipts and one indexed source file per changed path) but harmless synthetic bytes
   only. Use a
    separate profile config directory containing a generated copy with the same
    standalone identity/mode/prompt and only exact Kilo worktree-relative Read rules
    for indexed `packet/<file>` files in its frontmatter; the prompt supplies only
    their exact relative Read `filePath` values. Kilo 7.8.8 resolves these against
    the session directory before checking the exact worktree-relative permission rule.
    Run `tools/reviewer_packet.py prepare --synthetic --preflight` with this packet,
    `--profile` set to the tracked `.kilo/agents/pr-reviewer-standalone.md`, and
    `--profile-source-root` set to the clean frozen checkout (omit `--source-root` for
    the synthetic packet), installed Kilo 7.8.8 binary, synthetic HOME/XDG_CONFIG/
    state/cache/TMP paths, existing authenticated `XDG_DATA_HOME` and approved
    provider/model/variant. It verifies the tracked profile declares `mode: primary`,
    the resolver mode is CLI-runnable (`primary` or `all`, never `subagent`), the
    prompt hash matches and Read is the sole enabled tool with exact packet rules.
    `KILO_CONFIG_CONTENT` is `{}`; the per-run profile carries only exact worktree-relative allow rules.
    Set `KILO_NO_DAEMON=1` so the CLI uses this per-run configuration instead of an
    existing daemon.
    The helper only prepares/validates and never starts a reviewer session.
5. Run one real standalone `kilo --pure run --agent pr-reviewer-standalone` preflight
   against the synthetic packet plus harmless probes outside the packet workspace:
   sibling, parent, absolute, traversal and symlink-escape canaries. Use the exact provider,
    model, variant, environment and profile config returned by prepare. Do not use
    `Task`, a resumed session or any other agent. Verify the actual exported session
    identity, `parentID: null`, exact packet workspace, model dispatch and native trace.
    Require complete allowed-file reads, deny-before-content for every escape, no
    other tools, no prior-context input and an unchanged production packet inventory.
    Preflights are not whole-PR reviews and consume no ordinal. Any failure aborts
    before whole-PR dispatch; never broaden permissions or claim OS isolation.
6. Only after every synthetic runtime/acceptance gate passes, freshly re-fetch Repo,
    Issue, PR, linked owner objects and `develop`; re-confirm exact HEAD/base/merge-base.
    Build the complete real immutable packet at those exact refs, including canonical
    issue acceptance, direct owner sources/receipts, full patch, all changed code/test
    files, exact Git blobs/ranges and pinned Kilo source files. Exclude PR-body contents,
    prior findings, implementation reasoning, credentials, symlinks/hardlinks and extras.
     Run `tools/reviewer_packet.py prepare` against the real packet with `--profile` set
     to the tracked standalone profile and `--source-root`/`--profile-source-root` both
     set to the clean frozen checkout plus the pinned
    external-source checkouts. Require the actual packet file plan to equal the synthetic
    preflight plan, validate exact refs/provenance/content hashes, and re-resolve the
    final primary profile/permissions. Any mismatch aborts before an ordinal reservation.
7. Reserve/persist the next whole-PR ordinal and exact frozen SHAs before dispatch.
    Start exactly one fresh standalone Kilo CLI process/session with the prepared
    `standalone_run_argv`, approved provider/model/variant and exact launch environment.
    Do not add `--continue`, `--session`, `--fork`, Task or another agent. The session
    receives only the context-minimal prompt with PR identity and frozen refs; never
    pass checkout/environment paths, PR-body content, prior findings or implementation
    conclusions. Its enabled tool set is native Read only on exact `packet/<file>` paths.
    Pipe `--format json` directly to `tools/reviewer_packet.py capture-session-id` under
    `set -o pipefail`; never print or save the raw event stream containing returned bytes.
8. After the standalone CLI exits, export the exact session in memory and audit the
    full trace: selected agent/mode, no parent/resume, model dispatch, every Read
    path/range and effective permission, complete file coverage, final JSON result,
    packet post-inventory and fresh canonical currentness. Do not print or persist raw
    packet contents. These post-execution checks are separate from static Kilo debug
    and the standalone preflight. A verdict alone cannot approve; malformed,
    incomplete or mismatched traces fail closed. Never self-review, substitute a
    reviewer/model, widen access or use an external reviewer service.
9. Re-fetch PR metadata and canonical develop. APPROVE with empty findings,
    matching current HEAD/base SHAs, clean checkout, successful required validation
    and still open/unmerged develop target yields ordinary APPROVE. Only a
    non-security-critical delivery with all other gates passed may return
    READY_TO_MERGE; security-critical work continues to the owner gate below. Never merge.
   Changed HEAD/base invalidates approval. Diagnose external changes, malformed results
   and infrastructure failures; recover safe currentness and use a materially changed
   execution strategy within the persistent recovery/review budgets. Never race a
   concurrent writer, approve stale state or retry on a new session/model alone.
10. REQUEST_CHANGES: first check findings against accepted scope and authority.
   Genuine incompatible acceptance, architecture or material scope decisions
   yield OWNER_DECISION_NEEDED with concrete alternatives. Otherwise reproduce
   each actionable finding. Before patching check the effective delivery budget;
   at the effective ceiling return STOP_REVISE without patches that cannot receive a fresh review.
   Remediate all in scope and add regression tests.
   Classify each as direct incomplete remediation or genuinely new semantic/
   architectural defect; record that distinction and the reviewed HEAD/verdict.
11. Run focused validation and full `make check`; inspect status, intended diff,
   full base delta, whitespace and recent commit style. Verify configured and
   effective author/committer are Adrian Tkacz <adrian.tkacz@creatidy.com>.
   Stage only intended files, commit normally and push the SAME PR branch through
   canonical Git. Never amend, squash, force-push or rewrite published history.
    Recheck remote/MCP head, return to step 3, rebuild the path plan and repeat the
    standalone synthetic preflight before a NEW standalone reviewer process.
12. The default ceiling is 10; use only a freshly verified D-069A exception for
   other deliveries or the exact nontransferable D-077 direct owner amendment for
   #212/PR #213 described above. For #212, #12 is the next ordinary review; #13 is
   ordinary only after diagnosed/corrected #12 failure, otherwise it is owner-manual
   Daybreak after valid #12 APPROVE; #14 is the manual gate if #13 was ordinary. No
   #15. After a valid ordinary APPROVE, reserve the next owner-manual ordinal and return
   READY_FOR_OWNER_SECURITY_REVIEW; never report READY_TO_MERGE while that gate is open.
   At the effective review bound an exact valid APPROVE may yield READY_TO_MERGE only
   for deliveries with no remaining owner-manual gate;
   genuine decisions yield OWNER_DECISION_NEEDED and finite
   infrastructure failures BLOCKED. Remaining actionable findings yield STOP_REVISE;
   summarize recurring patterns,
   especially substantial new semantic/architectural defects across repository product boundaries. Never
   reset the issue delivery's review count or patch indefinitely. The ceiling is
   not a target: stop as soon as an owner decision is clearly required.
13. COMMENT: distinguish review findings, infrastructure failure and unresolved
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
Optional concise final issue/PR comments are reporting only; audited standalone
session results are the handoff. Do not require owner relaying, Forgejo review publication,
external orchestration, release, deployment or main promotion.
When reused by `/loop`, READY_TO_MERGE is internal; map OWNER_DECISION_NEEDED to
STOP_AND_ASK and stop the entire invocation. Other blockers/bounds terminate the
loop too, never skip to another issue. Merge/completion remain loop.md's procedure.
