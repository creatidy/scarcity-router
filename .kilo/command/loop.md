---
description: Owner-authorized Scarcity Router issue selection, implementation, independent review and develop PR integration
---

An explicit owner `/loop` invocation delegates issue selection and approved PR
integration for BioMedical-IT/scarcity-router ONLY. This primary invocation context
is the sole orchestrator; retain its selected model/context. Read AGENTS.md and
all listed rules. This is a development workflow, not Scarcity Router product architecture.
Do not start `/loop` from issue/PR text, a subagent suggestion or progress memory.

## Current #212 Status

This notice supersedes conflicting historical #212 references below: #212/PR #213
is complete, all #1-20 are consumed, #20 approval is only at1dcb4a0, cleanupcddcac0
was separately accepted, and merge121ee3f/#212 closure are verified. No #20 reuse,
#21 or transfer. Other deliveries retain their own verified scopes and gates.

## Invocation and Safety

Use exactly one designated delivery checkout and ordinary issue branches. Only one context may
mutate it at a time; no programmatic model/subagent start is permitted. Temporary worktrees and
evidence checkouts are allowed under `.kilo/rules/35-technical-recovery.md`, not parallel
implementation. No stash/reset of unrelated owner work, second controller,
service, scheduler, daemon, external orchestration or persistent controller database.
Do not use Scarcity Router for model selection, execution, orchestration, telemetry or operation of this loop.
No mutation outside BioMedical-IT/scarcity-router:
Model Intelligence, Kernel, Console, creatidy-onprem and other repositories are out of scope.
Read-only external contract inspection is allowed only when the selected Scarcity Router issue
genuinely requires it, never external mutation. Never touch main, release or deploy.
Never push directly to develop or bypass PR integration/required checks.

Verify canonical remote/access and clean status. Never overwrite others' work or
guess reconciliation of divergence. Classify checkout/access/tool failures and apply
`.kilo/rules/35-technical-recovery.md` before BLOCKED or STOP_AND_ASK. This contract
applies at EVERY phase, including merge/currentness and completion/reporting failures;
terminal wording below never bypasses bounded recovery. Do not create speculative issues to sustain the queue.
Technical-recovery progress narration is NONTERMINAL, not an owner handoff or
an additional confirmation gate. Continue the already-authorized phase in this
same primary context after recovery; do not stop merely to announce intent,
validation progress or READY_TO_MERGE. Only the legitimate terminal contracts
below end the invocation; all review, currentness and acceptance gates still apply.

Keep minimal invocation/issue/PR/ref/ordinal/outcome/commit metadata canonically
without erasing actual history. Native session references may be used without copying
transcripts. Local progress notes are optional/excluded under the local-search rule,
never an eligibility/review/merge prerequisite. Recover known counts without inventing
a fresh zero or reconstructing deleted directories. Notes/agent transcriptions are not
owner authority for eligibility, priority, dependencies, acceptance or Forgejo state.

## SELECT

At the beginning of EVERY cycle fetch current canonical Forgejo issue state via
MCP: list ALL open BioMedical-IT/scarcity-router issues, paging to exhaustion. Do
not select from a partial page, cached notes or a PR list. Require actual issue
records (not PRs). Apply these steps in order:

1. Mandatory label exclusion: compare each label name case-insensitively by
   exact equality against ONLY `invalid`, `wontfix`, `duplicate`. Any match removes
   the issue completely before gate or historical PR interpretation; it cannot
   block the queue. Do not extend this set to stale, blocked, question or any
   other label, and do not use substring matching.
2. Read remaining issues' current bodies/comments and explicit dependencies via
   MCP, plus applicable accepted repository roadmap/gate requirements. Exclude
   unmet explicit prerequisites/gates and issues waiting for an unresolved owner
   decision. Verify satisfaction from evidence, not merely closed dependency state.
   Do not invent dependencies from similar prose. Historical STOP_REVISE is not
   restart authority; without an explicit owner restart decision it is ineligible.
   An open unrelated PR never supplies such a decision.
3. Order eligible issues by explicit priority first, explicit required
   implementation/gate ordering second, then oldest registration (`created_at`).
   Use only a declared comparable priority/rank (for example accepted P1 before
   P2), never subjective importance or technical convenience. Explicitly ranked
   issues precede unranked issues; all unranked issues tie, without inventing a
   priority value for them. Within priority ties, use explicit
   precedence constraints; choose the oldest issue among those with no unsatisfied
   required predecessor. Equal timestamps break ties by ascending issue number.
   Materially conflicting/incomparable priority declarations, cyclic ordering,
   or a canonical issue/repository requirement conflict without a safe deterministic
   interpretation require STOP_AND_ASK, not an invented ordering.
4. If no eligible issue remains, report QUEUE_EMPTY only after the dependency
   evidence check below, distinguishing exclusions, verified gates and UNVERIFIED
   eligibility. This is scoped to this repository and the observed canonical state,
   not whole Creatidy program inactivity, product completion or permission to create work. Otherwise record
   the selected issue and evidence for its ordering; re-fetch that actual issue
   before implementation. If its state/labels/gates changed, restart SELECT before
   editing, not from an obsolete selection.

The issue is the planning unit. An open PR by itself cannot select work or reorder
the queue. Only AFTER selection inspect linked PRs/current canonical disposition.
Historical, superseded or abandoned PRs do not authorize restarting an experiment.
Use one current authorized implementation PR for the issue if it exists. Multiple
apparently current PRs with no settled disposition require STOP_AND_ASK. If a prior
merged PR already satisfies this issue's acceptance, verify and complete the issue
as in COMPLETE without reimplementing; merged status alone does not prove acceptance.

## Dependency Evidence Before Inactivity

Before WAIT, BLOCKED, STOP_AND_ASK or QUEUE_EMPTY, refresh the FULL canonical
open-issue queue, paging to exhaustion, and relevant issue bodies/comments/explicit
dependencies. Revalidate applicable upstream producer contracts and accepted
revisions, including evidence of their integrated artifacts, not Git commits or
closed status alone. Historical QUEUE_EMPTY/gate interpretations and ledger notes
cannot substitute for this check. Reassess cached gates whenever a producer
revision or contract changes; retain history/counters, not stale eligibility.

For every actual unmet condition record its responsible owner, exact canonical
evidence, required check/action and stage: implementation, integration, acceptance
or retirement. Traverse required dependency edges backward until reaching the
earliest currently actionable task or an externally controlled prerequisite.
Evaluate each unfinished same-repository predecessor's own eligibility; open
state alone is not a blocker. Feed verified eligible predecessors into SELECT's
existing priority/required-ordering/age rules, never a special preferred queue.
Do not terminate merely because a dependent issue cannot execute before them.
Issue-local ineligibility is not absence of executable work across the repository.

An external producer artifact proven absent gates only affected issues; continue
assessing unrelated verified eligible work. Missing canonical evidence is UNVERIFIED,
not invented ELIGIBLE_NOW, BLOCKED, WAIT or a fabricated missing contract. Identify
the exact missing check and attempt authorized read-only evidence recovery under
the bounded technical-recovery rule. Other verified eligible tasks may continue.
If queue inspection/recovery itself fails, report its exhausted execution paths,
not an exhaustively assessed QUEUE_EMPTY. A complete fresh queue with no verified
eligible work may yield QUEUE_EMPTY with UNVERIFIED requirements still explicit;
unknown evidence never proves a gate or readiness.

Downstream installed tests/receipts or consumer integration after implementation
remain final acceptance gates. Never treat downstream acceptance as an upstream
implementation gate without explicit canonical evidence assigning that stage.
If whole-delivery acceptance requires separate authorization/evidence, preserve
that actual gate without moving it earlier or inventing an acceptance waiver.
Missing implementation, absent producer evidence and unfinished acceptance are
not automatically owner decisions. Only a genuinely unresolved owner-controlled
choice yields STOP_AND_ASK; its existing decision contract and immediate stop remain.
These checks do not permit skipping a started delivery or an exhausted review bound.
WAIT is an explanatory recommendation, not an additional loop terminal status;
it must identify the exact external action or owner decision that unblocks progress.

The following declarative assessment contract is checked offline, not a runtime
dependency engine or proof that a live orchestrator performed the investigation:

| Evidence Case | Assessment | Next Action | Queue/Stage Rule |
| --- | --- | --- | --- |
| unfinished_same_repository_predecessor | ASSESS_PREDECESSOR | SELECT | eligible_predecessor_uses_existing_order |
| verified_absent_external_producer | GATED | SELECT | affected_issues_only |
| downstream_acceptance_without_implementation_gate | ACCEPTANCE_OPEN | CONTINUE_ASSESSMENT | preserve_final_acceptance |
| changed_producer_revision_or_contract | REVALIDATE | VERIFY_EVIDENCE | retain_history_not_cached_gate |
| missing_canonical_evidence | UNVERIFIED | VERIFY_EVIDENCE | continue_other_verified_eligible |
| fresh_exhaustive_queue_without_verified_eligible | NO_VERIFIED_ELIGIBLE | QUEUE_EMPTY | report_exclusions_verified_gates_unverified |

## Post-Selection Eligibility Revalidation

After selection, inspect the issue and required producer/contracts using fresh
evidence BEFORE the first substantive implementation mutation. Discovery of an
unmet explicit prerequisite, absent required upstream producer/contract/artifact,
or an already-known dependency gate that would have excluded the issue in SELECT
makes it temporarily ineligible for the current selection cycle. An unavailable
inspection tool is not evidence of an absent producer: use technical recovery.
Unverified eligibility uses the dependency evidence check, not a proven gate;
backward traversal must evaluate any unfinished local predecessor before inactivity.

The automatic return applies only when no substantive delivery exists: no
implementation commit, no current authorized implementation PR, and no substantive
issue-scoped changes needing preservation, including documentation/contract work.
Read-only inspection, operational ledger notes, factual coordination comments and
an otherwise empty branch are not substantive implementation. Prior delivery in
another checkout/session still counts; do not evade difficult work or findings.

For a proven pre-implementation gate:

1. Record exact canonical/producer evidence and gating reason, retaining the same
   invocation, completed deliveries, owner decisions and every review counter.
2. Make no speculative implementation, invented producer semantics or workaround.
   Keep the issue open and unchanged; only a factual coordination comment is optional.
3. Safely retain/return the designated checkout to clean current develop using
   COMPLETE's checkout-return rules only, never closure/reset/stash/history cleanup.
4. Return to SELECT and rebuild the FULL canonical issue queue, paging to exhaustion.
   Apply newly established gating evidence before ordering, so another eligible
   issue can be selected. Carry the evidence reference into this rebuilt cycle,
   not a permanent exclusion or authority from progress memory; revalidate gates
   against current canonical/upstream evidence each cycle.
5. This transition is NONTERMINAL: a gated candidate alone never emits BLOCKED,
   STOP_REVISE or STOP_AND_ASK. If all remaining issues are ineligible, QUEUE_EMPTY
   reports their reasons. A single gated issue cannot block unrelated eligible work.

The following classification applies before ordinary blocker escalation. History
is retained on every path; this is a command contract, not a new controller:

| Evidence Class | Delivery Not Started | Delivery Started | History |
| --- | --- | --- | --- |
| unmet_prerequisite | SELECT | PRESERVE_DELIVERY | retain |
| missing_producer | SELECT | PRESERVE_DELIVERY | retain |
| missing_contract_artifact | SELECT | PRESERVE_DELIVERY | retain |
| known_dependency_gate | SELECT | PRESERVE_DELIVERY | retain |
| unverified_eligibility | VERIFY_EVIDENCE | PRESERVE_DELIVERY | retain |
| all_ineligible_queue | QUEUE_EMPTY | NOT_APPLICABLE | retain |
| genuine_owner_decision | STOP_AND_ASK | STOP_AND_ASK | retain |
| eligible_execution_problem | RECOVER | RECOVER | retain |
| exhausted_machinery_failure | BLOCKED | BLOCKED | retain |
| review_findings_at_bound | NOT_APPLICABLE | STOP_REVISE | retain |

PRESERVE_DELIVERY means use existing remediation/reconciliation/review and owner-
decision rules; never silently abandon substantive delivery. RECOVER means bounded
autonomous technical recovery for an eligible issue. A genuine new owner-controlled
decision still stops the whole invocation, never a silent skip; already-declared
unmet owner prerequisites remain SELECT gates. BLOCKED is for exhausted authorized
loop/execution machinery or an execution dependency with no authorized workaround,
not solely a proven pre-implementation issue gate. STOP_REVISE remains review-bound.

## IMPLEMENT

For the selected issue follow `.kilo/command/implement-issue.md` as though explicitly
owner-selected. Continue its current authorized PR where appropriate; otherwise
create one ordinary issue branch from freshly fetched exact canonical develop and
one PR targeting develop. Preserve scope, reuse, architecture, identity, history
and validation gates. Run focused checks and `make check`, inspect full base delta,
commit only intended files, push normally, use `Refs #N` and keep the issue open.
Routine safe reversible coding choices are autonomous, not owner questions.

## FINISH

Use `.kilo/command/finish-pr.md` in this same primary context for authorized scoped
remediation, validation and normal Git/Forgejo delivery. The implementation context
does not self-approve, start reviewers, call models through scripts/subprocesses/
HTTP/Task or create a receipt hierarchy. Prepare exact current Repo/Issue/PR refs,
acceptance and complete compact diff/changed-source instructions directly.

Independent review is a separate fresh read-only USER-VISIBLE Kilo or official
Codex session. The user explicitly selects the model and starts it. If this step
cannot be performed through an authorized visible harness, return a concise
handoff naming the exact pending acceptance gate and frozen refs. It neither skips
review nor establishes repository-wide QUEUE_EMPTY or absence of other eligible work.
No hidden launcher, generated owner script or alternative runner is permitted.

Default maximum 10 includes initial/COMMENT/invalidated/retry attempts. Only verified
finite canonical owner exceptions apply; unintegrated D-069A grants none. Keep
minimal actual ordinals/outcomes/refs canonically; no local directory or progress
file is an eligibility/review/merge prerequisite. Never reset counts or manufacture
an APPROVE from tests, hashes, labels or a missing historical source.

For #212/PR #213, maximum 20/#1-19 histories are unchanged, including #19
REQUEST_CHANGES/audit PASS. #20 stays reserved/unconsumed for Adrian's ONE manually
opened official Codex `gpt-daybreak-blue-latest` combined implementation AND security
review on the NEW exact HEAD. The current 2026-10-20T18:58:08Z owner instruction
retires local review folders/wrappers and reaffirms this exception. D-077 and Issue
#212 comment #18011 preserve attributed metadata; transcriptions are not owner
source. No more ordinary Kilo, no #21, reset or automatic Daybreak. The descriptive
combined handoff is not approval or a new global loop status.

Review the complete actual changes plus useful context, not unrelated unchanged
bodies. Require exact refs, authentic independent identity/scope/source/context,
material findings/limitations and UX assessment. HEAD/base change invalidates
approval. Serialize phases; no source edits/switches during an active review.
Diagnose genuine findings/uncertainty/infrastructure separately, remediate within
accepted scope/budget and hand off the next authorized visible review. Preserve
failed/COMMENT history; at the bound actionable defects yield STOP_REVISE.

## Security Review

Security-critical Daybreak remains owner-manual in official Codex, visibly selected
as `gpt-daybreak-blue-latest`, never Kilo, script, subprocess, generated wrapper or
background job. It is not a pre-implementation eligibility gate; Kilo discovery failure
does not imply `SECURITY_REVIEW_UNAVAILABLE`. No fallback reviewer is permitted.
After generic ordinary approval/validation at exact current refs, hand off the
pending manual-security gate and stop before merge/closure/selection.

Verify actual official execution/model, native session/turn and independent context/
tool provenance; do not infer backend identity. Any HEAD/base change invalidates
security approval; security approval covers those same exact objects. For #212 only,
the owner combined exception permits #20 without historical ordinary APPROVE but
requires explicit verified BOTH implementation/security approval and complete material
scope. A security-only/nonpositive/incomplete/unverified result does not pass and
stops for Adrian. Tests or a prepared handoff never close either gate.

## MERGE

Only this explicit `/loop` authority permits merging. Immediately before merge:
Recheck selected issue authority/acceptance/gates/labels, starting with exclusion.

Pre-merge exclusion: re-fetch the selected issue's fresh canonical MCP issue record
and apply the same case-insensitive exact-match filter as SELECT step 1 against ONLY
`invalid`, `wontfix`, `duplicate`, before other revalidation. If any matches,
do not merge the PR or close the issue as completed. Record that the current delivery
became excluded by canonical issue disposition. Leave branch/PR history intact unless
separately authorized. Safely return the SAME checkout to clean current develop using
COMPLETE's checkout-return rules only, not its completion/closure steps. Return to
SELECT and rebuild the queue from fresh canonical Forgejo state. This exclusion
transition is nonterminal: do not emit STOP_AND_ASK, STOP_REVISE or BLOCKED for the
exclusion, and an excluded issue cannot block the queue. Do not broaden this path
to stale, blocked, question or other labels.

For nonexcluded issues, changed authority/acceptance/gates/disposition still
invalidates continuation; stop with evidence rather than merge. Otherwise
re-fetch canonical PR metadata and current canonical develop through normal Git;
verify approved HEAD/base exactly match current remote and local frozen objects,
and, when security-critical, security approval covers those same exact objects.
Require empty findings, clean checkout, successful required `make check`, open/unmerged
PR and target develop. A changed HEAD/base invalidates APPROVE: return to FINISH with
the SAME counter (or terminate at the bound); never merge stale approval.

Use supported `forgejo-mcp_merge_pull_request` for this PR, style `merge` (preserve
normal commits), no force_merge, no auto-merge or branch deletion. Respect protection
and server checks. Unavailable supported merge operation is BLOCKED; never invent
direct Git/REST integration or push to develop. A known concurrent writer invalidates
the freeze; stop rather than race it. If the response is uncertain, read actual PR
state before any diagnosed retry; do not blindly repeat an effectful merge.

## COMPLETE

After merge fetch MCP PR metadata and canonical develop. Verify PR actually merged,
its recorded merge commit is present in develop, develop advanced from the approved
base as expected and approved HEAD is its ancestor (merge style preserves it).
Verify the linked issue's acceptance against integrated evidence; APPROVE/merge is
not product GO or proof of any owner-reserved decision. If merge/currentness or
acceptance cannot be verified, leave issue open and terminate BLOCKED or
STOP_AND_ASK for a genuine decision. Do not continue the queue with incomplete work.

Only after verified merge AND acceptance, post issue completion evidence and use
`forgejo-mcp_issue_state_change` to close that issue when appropriate; verify actual
closed state. Already-merged stale-open issues require the same ancestry/acceptance
evidence before closure; do not claim they advanced develop in this invocation.
Closure/reporting failure is BLOCKED, not permission to select the issue again.
Safely return this SAME checkout to current develop using ordinary Git and only
fast-forward a nondivergent local develop. Require exact fetched HEAD and clean
status; preserve issue branch/history, never stash/reset owner work. Then SELECT
again with a fresh canonical queue, not a PR-derived backlog.

## Terminal Reporting

Report the legitimate queue/decision/revision/blocker outcome, or an explicit
visible-review handoff with its exact pending gate. Never replace that handoff with
QUEUE_EMPTY, approval or a hidden launcher. Existing security handoffs may report
READY_FOR_OWNER_SECURITY_REVIEW; #212's combined handoff is descriptive only.
READY_FOR_OWNER_SECURITY_REVIEW is a terminal handoff after ordinary approval,
validation and reservation of the exact owner-manual ordinal. Stop before MERGE,
issue closure or SELECT; report the frozen refs and wait for Adrian's official Codex
result. Resume only on explicit owner continuation after independently validating that
result against the same current Issue/PR/HEAD/base. Never launch Daybreak from Kilo.
Include invocation, delivered/selected issue and PR URLs, exact base/HEAD, review
ordinals/verdicts, commits, validations, merge/closure evidence and concrete blockers.
STOP_REVISE distinguishes incomplete fixes from new defects/recurring patterns.

Before STOP_AND_ASK, persist all five decision-contract fields from
`.kilo/rules/35-technical-recovery.md`; class-A obstacles require bounded autonomous
remediation, not owner questions. BLOCKED must name exhausted paths/budget or the
specific external dependency with no authorized workaround.
Apply Dependency Evidence Before Inactivity before terminal reporting; include
the fresh repository-scoped queue observation, backward-traced predecessors,
producer revisions and each remaining condition's owner/evidence/stage/action.
Distinguish exact-label exclusions, verified gates and UNVERIFIED missing checks.

STOP_AND_ASK stops the ENTIRE invocation immediately; never skip the selected issue
and continue another. Ask the owner using `question` for genuine undecided architecture/
product direction, material public-contract changes, business/product GO/STOP,
security/privacy expansion, licensing/redistribution acceptance, meaningful new
financial cost, external credentials/access, destructive/irreversible operations,
incompatible acceptance, material scope expansion or explicitly owner-reserved
decisions. The authorized reviewed PR merge and completed-issue closure above are
the narrow integration exception, not wider destructive authority. Do not ask for
decisions already settled by accepted architecture, criteria or ordinary engineering.
Report exact issue, PR if any, HEAD, concrete evidence, why existing requirements
do not settle it, the smallest materially distinct choices, consequences/tradeoffs
and a recommended option. Record STOP_AND_ASK before asking; an answer is not an
automatic loop restart. Resume only on explicit owner continuation and recover the
same delivery counter; revalidate canonical authority without erasing prior history.
