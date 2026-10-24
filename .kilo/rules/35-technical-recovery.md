# Bounded Technical Recovery

## Current #212 Status

This notice supersedes conflicting historical #212 references below: #212/PR #213
is complete, all #1-20 are consumed, #20 approval is only at1dcb4a0, cleanupcddcac0
was separately accepted, and merge121ee3f/#212 closure are verified. No #20 reuse,
#21 or transfer. Other deliveries retain their own verified scopes and gates.

This shared development-workflow contract applies to implementation, validation,
review and remediation. It does not authorize product changes, persistent services,
new dependencies, paid/live inference, external effects or broader access.

Ordinary independent review is a fresh-conversation requirement, not a separate
filesystem requirement. Authorized `/loop` and `/finish-pr` workflows start a new
user-visible Kilo reviewer through native session management and collect its result
without owner relay. The reviewer may use the existing repository/checkout while the
source is frozen; do not create a worktree, container or sandbox solely for reviewer
independence.

## Classify Before Escalating

For `/loop`, first apply `.kilo/command/loop.md` post-selection eligibility
revalidation. A confirmed pre-implementation prerequisite/producer/contract gate
is issue ineligibility, not machinery failure or a new owner decision: retain
history/counters and return nonterminally to SELECT with a fresh full queue.
Unavailable inspection is not proof of a gate; recover tools/access first.
Once substantive delivery exists, preserve it under existing rules. Genuine new
owner decisions still stop; this exception never bypasses review or security gates.

For an explicit owner `/loop` invocation only: Before WAIT, BLOCKED, STOP_AND_ASK
or QUEUE_EMPTY, also apply loop.md's dependency
evidence check against the FULL fresh canonical queue and relevant accepted producer
revisions. Trace required edges backward and evaluate unfinished same-repository
predecessors; open state or a historical gate is not execution-failure evidence.
Record owner, evidence, exact required check/action and implementation/integration/
acceptance/retirement stage for each actual unmet condition. Reassess changed
producer/contracts; downstream acceptance cannot gate upstream implementation
without explicit canonical evidence. Missing evidence is UNVERIFIED: attempt
authorized read-only evidence recovery, never invent readiness or an absent
contract. Verified external gates affect only dependent issues, not unrelated
eligible work. WAIT names the exact external action/owner decision; QUEUE_EMPTY
distinguishes exclusions, verified gates and unverified checks for this repository,
not the whole Creatidy program. None of this weakens started-delivery preservation,
immediate genuine owner-decision stops, review bounds or final acceptance.
Standalone implementation, finish and review retain selected-issue/PR-local
evidence recovery with no unrelated full-queue inspection authority.

Before returning control to the owner, classify the obstacle and record evidence:

- **A: Engineering / execution blocker.** Missing tools/runtime, unsafe ambient
  environment, broken dependencies, unsuitable checkout/filesystem, unavailable
  public-source connector or insufficient reviewer execution path. Diagnose and
  attempt the minimum sufficient authorized technical remediation automatically.
- **B: Genuine owner decision.** Material architecture, authority/trust boundary,
  scope/acceptance, security/isolation, private access, cost/live effects, destructive
  action, product boundary, dependency-adoption commitment or undelegated integration
  authority. Only this class normally yields STOP_AND_ASK / OWNER_DECISION_NEEDED.

A blocker is not automatically a decision. Owner attention is scarce: do not ask
the owner how to run tests, use Docker, sanitize an environment, repair ordinary
tool state, choose equivalent reviewer mechanisms or relay obtainable public evidence.

## Recovery Budget and Strategy

Use the smallest safe, reversible, in-scope mechanism. Before recovery begins,
record the diagnosis, relevant changed condition and expected proof. Allow at most
three materially different recovery attempts per obstacle and at most 120 minutes
of technical recovery per issue delivery (or the existing smaller execution budget).
Keep minimal attempts/time across reentry, phases and sessions canonically;
local notes are optional, not a gate. Renaming an obstacle or reviewer does not
reset its budget. Every whole-PR
review dispatch, including failed/COMMENT attempts and failover, also consumes the
next reserved ordinal. The default effective ceiling is 10. The sole separate
direct-owner supersession is #212/PR #213: the final D-077 amendment records maximum
20, consumed #1-19 unchanged. The final `2026-10-20T06:26:22Z` exception permits
the P1 correction/validation and #20 owner-manual combined correctness/security
Daybreak without ordinary APPROVE, for this delivery only. No more ordinary Kilo,
#21, blind retry or counter reset. If the combined result is nonpositive/incomplete/
unverified, preserve it and stop for the owner. The current owner reaffirms the
exception while retiring local files/scripts. Verify legitimate native owner
source/current reaffirmation, not deleted local paths or transcriptions, and
canonical scope/currentness before each reservation; this is not D-069A and cannot
transfer. For other deliveries,
only a finite D-069A exception verified against the direct owner source and exact
canonical Repo/Issue/linked-PR scope, with D-069A already integrated into freshly
fetched `develop`, can raise the cap. Missing/stale/ambiguous authority leaves the
default 10 effective. Never
dispatch beyond the effective ceiling or patch without capacity for fresh review.
Budget exhaustion is a finite stop, not permission to weaken a gate or invent an owner
decision.

No blind retries means never repeat a failed operation with the same relevant
inputs and environment. It does not mean stop after one failed approach. A corrected
configuration already within authority, clean environment/container, exact source
checkout, different authorized reviewer/tool path, smaller reproducer or repaired
test infrastructure can justify another bounded attempt. Session/model change alone
is not a diagnosis. Inspect output/artifacts and actual state before repeating any
effectful operation; never duplicate an uncertain write.
Technical-recovery progress narration is NONTERMINAL, not an owner handoff or
new confirmation requirement. Continue already-authorized work in the same
primary invocation after recovery, retaining all gates and counters. For uncertain
validation process/results, inspect live state and recover durable logs/exit status
before a diagnosed rerun; never launch duplicate validation processes or claim an
unobserved PASS. A lost process record is an evidence gap, not a terminal contract.

Available mechanisms, where applicable, are optional engineering remedies for a
demonstrated technical need; none is an independence prerequisite for review:

- Construct an explicit sanitized child environment, synthetic HOME, cache and temp
  directories; install only the repository's locked/approved development dependencies.
- Use an ephemeral Docker/container environment when lighter isolation is insufficient.
  Docker is an execution mechanism, not product dependency adoption. No persistent
  service, privileged container, Docker socket mount or security weakening is implied.
- Prepare a temporary worktree/checkout only for a demonstrated need such as
  concurrent source mutation, preserving unrelated owner work. Use one designated
  delivery checkout and one mutator; auxiliary checkouts are evidence-only, not
  parallel implementation controllers. Parent never edits or switches any reviewed
  checkout while its reviewer is active. A reviewer may use the same frozen checkout.
- Mount the repository read-only where mutation is unnecessary, only required paths,
  and separate writable synthetic scratch paths. Do not mount host credential directories.
- Split independent source inspection from safe test execution when they need different
  environments, or reproduce a claim with a smaller synthetic/offline proof. Preserve
  mandatory full validation and explicitly open any real-environment acceptance gap.
- If native Kilo session/result capability is genuinely unavailable after bounded
  recovery, prepare a concise visible handoff naming that exact pending gate. Preserve
  frozen PR scope, model-bound gates, read-only access and conversational independence.
  This is not a routine owner-start/result-relay requirement. Recovery never authorizes
  scripts, subprocesses, Task/delegation, background/HTTP inference or a hidden substitute.

## Secret-Safe Environments

Tests/tools that can observe inherited state must receive only deliberate synthetic
values and minimum operational variables, not the owner's ambient environment.
Environment-inheritance tests inherit synthetic test values, never real credentials.
Do not mount SSH, cloud, provider/model, Forge, browser or other credential directories
unless the exact already-authorized operation requires them; do not copy secrets into
images or print environment values. Diagnostics retain names/categories, not values.
Keep authenticated platform operations outside test sandboxes. If unauthorized real
credentials are genuinely necessary, record the specific evidence gap; do not ask for
them unless required for the selected acceptance and subject to an owner decision.

## Native Review Sessions

For an authorized ordinary review, verify actual Git objects and canonical
Repo/Issue/PR/owner authority, then start a fresh user-visible Kilo reviewer through
native session management. Provide only the task/scope, acceptance, exact frozen refs,
relevant contracts and source instructions; do not pass the implementation
conversation, reasoning, private deliberations or a desired verdict. Use the existing
checkout when safe. A separate reviewer worktree is not an independence requirement.

Keep the source frozen for the full review. Collect the final reviewer result through
native session communication or exact-session retrieval; do not require the owner to
copy prompts or responses. This native Kilo session start is permitted and visible;
no script, subprocess, Task/subagent, background inference, HTTP call or wrapper may
launch a model. If the native session/result capability itself is unsupported, apply
bounded technical recovery and report the exact pending gate without skipping review
or claiming QUEUE_EMPTY.

Use native harness records and minimal canonical ordinals/outcomes/provenance, not
persistent local receipts, copied source/transcripts or reconstructed evidence trees.
Historical deleted files are unavailable; historical hashes/transcriptions do not
recreate their original bytes or grant authority. Recover legitimate native owner
records or obtain direct owner confirmation only when authority is truly unresolved.
For #212 the current owner explicitly reaffirms pending/unconsumed #20 combined
Daybreak and forbids scripts; use D-077/Issue #212 history, not deleted paths.

For public-source access failure, try an alternative available read path, then fetch/clone the exact public revision
in safe temporary space when authorized; preserve pin, origin and provenance.
The visible reviewer verifies actual claims; implementer research conclusions are not independent verification.
Connector failure is not an implementation finding. Preserve model-bound gates, read-only permissions and reviewer independence.
Do not bypass tool restrictions or silently substitute a required restricted reviewer.
Public source inspection remains bounded, pinned and read-only where needed.
Distinguish review finding, infrastructure failure and reviewer disagreement / uncertainty;
never accept incomplete review as APPROVE or silently substitute models. Keep all
consumed attempts, exact refs and real outcomes without storing large local histories.
Deterministic offline fixtures use cleaned normal OS temporary directories only.

## Terminal Contracts

Before STOP_AND_ASK / OWNER_DECISION_NEEDED, record internally and durably:

1. The exact unresolved decision.
2. Why it is owner-controlled rather than an engineering problem.
3. Reasonable autonomous remediation paths considered.
4. Why they cannot resolve it without changing authority, architecture, security,
   scope, cost or another owner-controlled commitment.
5. The smallest set of materially distinct choices; never fabricate alternatives
   when one sensible engineering remediation exists.

BLOCKED requires exhausted authorized technical paths/budget or an external condition
the agent cannot change with any authorized workaround. Class-B decisions instead
require STOP_AND_ASK / OWNER_DECISION_NEEDED with all five decision fields above.
State the exact missing capability/dependency, attempted paths, remaining evidence gap
and consumed budget. A reviewer lacking a separate worktree is not a blocker when the
existing repository and source tools can inspect the frozen revision. A genuine
unsupported native session/result capability is classified and recovered under the
same bounded rule. Do not ask an artificial question for an external non-decision
blocker. STOP_REVISE remains the review-bound disposition for actionable defects,
not a technical environment failure.

Continue implementation -> validation -> independent review -> ordinary remediation
-> fresh validation/review until exact independent APPROVE with acceptance satisfied,
genuine budget exhaustion, an external condition with no authorized workaround or a
class-B decision. Only a genuinely unsupported native Kilo session/result capability
after bounded recovery remains a review handoff gate. Ordinary user-visible reviewer
starts are handled by the authorized orchestrator; do not require owner initiation or
an artificial infrastructure question. This never authorizes hidden model execution.
