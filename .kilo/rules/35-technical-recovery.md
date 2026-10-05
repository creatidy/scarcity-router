# Bounded Technical Recovery

This shared development-workflow contract applies to implementation, validation,
review and remediation. It does not authorize product changes, persistent services,
new dependencies, paid/live inference, external effects or broader access.

## Classify Before Escalating

For `/loop`, first apply `.kilo/command/loop.md` post-selection eligibility
revalidation. A confirmed pre-implementation prerequisite/producer/contract gate
is issue ineligibility, not machinery failure or a new owner decision: retain
history/counters and return nonterminally to SELECT with a fresh full queue.
Unavailable inspection is not proof of a gate; recover tools/access first.
Once substantive delivery exists, preserve it under existing rules. Genuine new
owner decisions still stop; this exception never bypasses review or security gates.

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
Persist attempts/time across reentry, phases and sessions in the excluded delivery
ledger; renaming an obstacle or reviewer does not reset its budget. Every whole-PR
review dispatch, including failed/COMMENT attempts and failover, also consumes the
next reserved ordinal within the existing ten-review ceiling. Never dispatch review
11 or patch without capacity for fresh review. Budget exhaustion is a finite stop,
not permission to weaken a gate or invent an owner decision.

No blind retries means never repeat a failed operation with the same relevant
inputs and environment. It does not mean stop after one failed approach. A corrected
configuration already within authority, clean environment/container, exact source
checkout, different authorized reviewer/tool path, smaller reproducer or repaired
test infrastructure can justify another bounded attempt. Session/model change alone
is not a diagnosis. Inspect output/artifacts and actual state before repeating any
effectful operation; never duplicate an uncertain write.

Available mechanisms, where applicable:

- Construct an explicit sanitized child environment, synthetic HOME, cache and temp
  directories; install only the repository's locked/approved development dependencies.
- Use an ephemeral Docker/container environment when lighter isolation is insufficient.
  Docker is an execution mechanism, not product dependency adoption. No persistent
  service, privileged container, Docker socket mount or security weakening is implied.
- Prepare a temporary worktree/checkout at the frozen revision, preserving unrelated
  owner work. Use one designated delivery checkout and one mutator; auxiliary review
  and public-source checkouts are evidence-only, not parallel implementation controllers.
  Parent never edits or switches any reviewed checkout while its reviewer is active.
- Mount the repository read-only where mutation is unnecessary, only required paths,
  and separate writable synthetic scratch paths. Do not mount host credential directories.
- Split independent source inspection from safe test execution when they need different
  environments, or reproduce a claim with a smaller synthetic/offline proof. Preserve
  mandatory full validation and explicitly open any real-environment acceptance gap.
- Use another available authorized independent native reviewer/tool path automatically
  after diagnosing infrastructure failure. Preserve the complete frozen PR contract,
  result schema, model-bound gates, read-only permissions and reviewer independence.
  Do not bypass tool restrictions or silently substitute a required restricted reviewer.

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

## Public Evidence and Reviewer Failover

A public-research reviewer must inspect cited material independently. If a pinned
source is inaccessible, try an alternative available read path, then fetch/clone the
exact public revision into an isolated evidence checkout within authorized network
access. Verify the specific material claims and preserve pin, origin and provenance
in review evidence. Parent may stage source bytes but its research conclusions are
not independent verification. One process's connector failure is not an implementation
finding. If network/source access is unavailable everywhere authorized, report that
specific external evidence gap, not an artificial owner question.

Distinguish **review finding** (evidence the change is wrong/incomplete), **review
infrastructure failure** (no verdict can be established) and **reviewer disagreement /
uncertainty** (evidence exists, judgment unresolved). Infrastructure failure calls for
changed environment/strategy, not identical retries or fabricated defects. Uncertainty
calls for bounded independent evidence/adjudication; escalate only a genuine class-B
decision. Never accept incomplete review as APPROVE. Retain all failed/COMMENT ordinals
and obtain a fresh whole-PR review at exact HEAD/base after implementation changes.

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
and consumed budget. A reviewer lacking tools is insufficient while a suitable clean
environment/container/alternate authorized reviewer path remains. Do not ask an
artificial question for an external non-decision blocker. STOP_REVISE remains the
review-bound disposition for actionable defects, not a technical environment failure.

Continue implementation -> validation -> independent review -> ordinary remediation
-> fresh validation/review until exact independent APPROVE with acceptance satisfied,
genuine budget exhaustion, an external condition with no authorized workaround or a
class-B decision. Never stop merely because the first reviewer environment is inconvenient.
