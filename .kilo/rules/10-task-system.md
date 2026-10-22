# Issue Delivery Workflow

## Current #212 Status

This notice supersedes conflicting historical #212 references below: #212/PR #213
is complete, all #1-20 are consumed, #20 approval is only at1dcb4a0, cleanupcddcac0
was separately accepted, and merge121ee3f/#212 closure are verified. No #20 reuse,
#21 or transfer. Other deliveries retain their own verified scopes and gates.

- Outside an explicit owner `/loop`, start implementation only when Adrian selects one Forgejo issue by
  number, URL or unambiguous title. Fetch the actual issue via MCP before planning
  or editing. If the selection is ambiguous, clarify; never infer work from order,
  age, labels, milestones, Projects, branches, documentation queues or memory.
- Do not create issues without explicit authorization. No Program Execution Mode,
  external controller, execution graph or generic planning framework. Autonomous
  selection is permitted ONLY by the explicit `/loop` exception below.
  An explicit owner documentation/planning mandate can authorize deduplicated
  registration of its main/gap issues; it does not authorize implementing the
  registered features. Record the actual mandate and use returned Forgejo IDs.
- `/loop` delegates successive issue selection to its sole primary invocation
  context under `.kilo/command/loop.md`, not a second controller. Refresh all open
  canonical Scarcity Router issues each cycle; exclude exact invalid/wontfix/duplicate labels
  case-insensitively before historical PR interpretation, verify explicit gates,
  then explicit priority/required ordering/oldest registration. Open PRs are not
  planning authority. Unresolved owner decisions are ineligible; if a genuine
  decision arises for selected work, STOP_AND_ASK terminates the entire loop.
  Preserve STOP_REVISE dispositions; an open PR cannot authorize restarting them.
  No issue registration is authorized merely to keep `/loop` running.
- For an explicit owner `/loop` invocation only: Before WAIT, BLOCKED, STOP_AND_ASK
  or QUEUE_EMPTY, apply loop.md's dependency
  evidence check: refresh the FULL canonical open queue/relevant dependencies and
  accepted upstream producer revisions, trace required edges backward and assess
  each unfinished same-repository predecessor's own eligibility. Open state is not
  a blocker; eligible predecessors use existing priority/ordering/age rules.
  Record each unmet condition's owner, evidence, exact check/action and
  implementation/integration/acceptance/retirement stage. External producer gates
  affect only dependent issues; unrelated verified eligible work may continue.
  Reassess cached gates on producer/contract changes. Downstream acceptance is not
  an upstream implementation prerequisite without explicit canonical evidence.
  Missing evidence is UNVERIFIED with authorized read-only recovery, not invented
  readiness or a gate. QUEUE_EMPTY requires a complete fresh repository-scoped
  queue with no verified eligible work and separate exclusions/verified gates/
  unverified checks; it does not establish whole-program inactivity. WAIT must
  name the exact external action or owner decision. Preserve started deliveries,
  genuine owner-decision stops, review bounds and final acceptance gates.
  Standalone implementation, finish and review retain selected-issue/PR-local
  evidence recovery with no unrelated full-queue inspection authority.
- Before substantive delivery, apply loop.md's post-selection eligibility revalidation.
  A freshly proven missing prerequisite/producer/contract makes an untouched issue
  temporarily ineligible: retain history/counters, keep it open, safely return to
  clean develop and rebuild the full canonical queue; continue SELECT nonterminally.
  All-gated queues yield QUEUE_EMPTY. Existing implementation commits/current PRs/
  substantive changes require preservation, not silent de-selection. Genuine new
  owner decisions still stop; eligible-issue execution failures use bounded recovery.
- Verify the canonical remote, fetch current `develop`, record its exact SHA and
  inspect files/status/branches. Demonstrate access by successful operations.
  Use one designated delivery checkout and only one mutator at a time. Temporary
  worktrees/evidence checkouts follow `.kilo/rules/35-technical-recovery.md`.
  Preserve unrelated changes/branches;
  never stash/reset others' work. If unrelated changes prevent safe switching,
  diagnose and attempt safe isolated recovery before a precise blocker.
- Create an ordinary branch named `issue-<number>-<short-topic>` from that recorded
  fetched SHA in this checkout. Use normal Git transport. Never implement
  directly on `develop`; never target, modify, merge into or promote `main`.
- Confirm accepted scope, implement the smallest coherent change, run checks,
  inspect status/full base delta, commit only intended files, push to canonical
  Forgejo, create one PR to `develop` via MCP and post a concise issue update.
- Standalone implementation/finish/review never merge or auto-merge. Only explicit
  `/loop` authorizes the supported Forgejo PR merge to develop after fresh exact
  approval/currentness gates, verified integrated acceptance then issue closure.
  Never direct-push develop, touch main, release, promote or deploy. Outside that
  completion gate keep the issue open at handoff; use `Refs #N`,
  not automatic closing keywords. Report issue/PR, base/head SHAs, validation and
  genuine blockers; READY_FOR_REVIEW means implemented and verified, not approved.
- `/finish-pr` selects an existing PR and authorizes only its linked issue's
  accepted-scope remediation. Under the direct D-077 owner amendment, the sole
  `/loop` or authorized `/finish-pr` primary may start one fresh visible ordinary Kilo
  reviewer through Agent Manager using `pr-reviewer-standalone`; it inherits the
  exact current primary model unless the owner explicitly selects another. The
  primary supplies exact refs, acceptance and complete compact diff and retrieves the
  structured result through the native Agent Manager/Kilo session without owner
  copy/paste. This remains a distinct read-only session, not self-review or a second
  controller. No Task/subagent, scripts/subprocesses, HTTP inference, silent
  substitution or fallback. Daybreak remains owner-manual in official Codex.
  If the supported visible Agent Manager path fails, preserve the attempted ordinal
  and hand off its exact infrastructure gate; do not substitute a hidden path, skip
  acceptance or declare repository-wide QUEUE_EMPTY.
  Default to at most 10 whole-PR review invocations per issue delivery, including
  initial, COMMENT and retries. The sole separate direct-owner supersession is for
  #212/PR #213: maximum20 and consumed #1-19 are preserved. The final direct
  `2026-10-20T06:26:22Z` D-077 exception permits P1 correction/validation then #20
  owner-manual combined implementation/security Daybreak without prior ordinary
  APPROVE; no more ordinary Kilo reviews or #21. Both gates need explicit verified
  combined approval, not a security-only result. The 2026-10-20T18:58:08Z owner
  instruction preserves this exception but retires local receipts/launchers and
  requires the NEW exact HEAD in the owner's visible Codex. Verify legitimate
  native owner authority and
  canonical issue/PR scope/currentness before each reservation; this is not D-069A
  and does not transfer. Any other larger finite
  maximum requires D-069A authority: verify its direct owner source, exact canonical
  repository/issue/linked-PR identity, maximum and scope before reservation, and
  confirm D-069A is already integrated in freshly fetched `develop`. A branch cannot
  use its own unintegrated rule. Otherwise the effective ceiling remains 10. Persist
  minimal ordinals/outcomes/refs canonically before imminent authorized visible
  review/handoff; no local directory/progress file is a prerequisite. Finish
  reentry, internal phase, new reviewer process/model/session cannot
  reset the counter.
  Current APPROVE yields READY_TO_MERGE, never a standalone merge; only `/loop`
  may continue through its separate merge/completion gates.
  At the bound return STOP_REVISE with new defects versus incomplete fixes and
  recurring architectural/semantic patterns. Material scope/architecture decisions
  yield OWNER_DECISION_NEEDED only after recording the recovery rule's decision
  contract. Unavailable tools require bounded autonomous technical recovery before
  a precise finite BLOCKED; keep every failed/COMMENT ordinal.
  In `/loop` map OWNER_DECISION_NEEDED to STOP_AND_ASK; stop, never skip selected work.

## Issue Quality

Agent-created issues must include:

- **Goal**
- **Why**
- **Scope**
- **Acceptance Criteria**
- **Constraints**
- **Links**

Issue bodies must be implementation-grade, not copied plan fragments or
scratch notes. Acceptance criteria must be verifiable. If scope changes
materially, update the issue body before continuing.

Do not create duplicate or speculative follow-up issues; update or link an
existing issue instead.
