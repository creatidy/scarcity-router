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
Persist attempts/time across reentry, phases and sessions in the excluded delivery
ledger; renaming an obstacle or reviewer does not reset its budget. Every whole-PR
review dispatch, including failed/COMMENT attempts and failover, consumes its next
reserved chronological ordinal. The default issue-delivery ceiling is ten. A finite
D-069A owner exception is usable only after the primary independently re-fetches and
verifies canonical Repo/Issue/PR/owner-source evidence, exact issue/PR scope, maximum
and current role requirements. The excluded ledger records counts/history; it never
grants or extends authority. Reserve every ordinal before dispatch and never exceed
the exact verified maximum or patch without fresh-review capacity. Budget exhaustion
is a finite stop, not permission to weaken a gate or invent an owner decision.

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

## Independent Review Metadata

The primary alone reads canonical Repo/Issue/PR and directly linked owner-decision
objects via the exact Forgejo MCP getters, then verifies IDs, URLs, authors, times,
raw bodies and digests. The reviewer receives no direct Forgejo getter/writer: the
parent-prepared immutable packet supplies complete acceptance evidence, original
owner decisions and exact Git commit/blob/patch bytes, bound to source IDs, retrieval
provenance, timestamps and hashes. PR-body text containing prior findings is not
reviewer context; bind its digest and supply the full canonical issue acceptance and
nonhistorical owner evidence needed to evaluate scope.

The `pr-reviewer` Kilo profile denies Bash/Git, all direct Forgejo MCP tools,
search/list/history, external paths, writes and delegation before any call can execute.
Its Read rules are deny-by-default and contain no static packet grants. The primary
builds a documented `KILO_CONFIG_CONTENT` overlay for each frozen packet with
`agent.pr-reviewer.permission.read` default deny plus `allow` entries for only the
exact worktree-relative regular-file paths in the flat packet. The sole owner-authorized
exception is the independent ordinary reviewer for issue #210 / PR #211: it may replace
the 48 exact file entries with the single rule
`.review-evidence-210/pr-reviewer-input/**`. Do not generalize this exception to any
other issue, PR, reviewer role or delivery; never use basename-class or root-wildcard
grants. Verify the installed Kilo version's documented Read path normalization and
permission-rule precedence before dispatch; record the version and source/runtime
evidence. These semantics are version-specific and must be reconfirmed after a Kilo
update. Order broad denials before the packet allow.

Before this #210 grant, verify a flat, exact-revision packet of exactly 48 indexed files:
no credentials or extra entries; each entry a regular file with one link; no symlinks or
hardlinks; a real packet directory at mode `0555` and files at mode `0444`. Record every
relative path and SHA-256 together with the exact Git bindings. Do not dispatch while
another writer can mutate the packet; the parent must serialize writers and make no
packet changes during the Task. Verify the complete path/hash inventory both before
and after the Task and fail the audit on any difference. The Kilo permission grant
controls Read calls, not directory listing; the reviewer still reads only index-listed
paths/ranges, and the independent trace audit and full-PR coverage are unchanged. The
native 48-file Task preflight proves Kilo tool-permission behavior only; it does not
establish OS-level isolation.

Dispatch from a fresh Kilo CLI parent with `--pure --agent code` and the exact inherited
provider/model/variant. Its `KILO_CONFIG_CONTENT` sets `agent.code` to `* -> deny` and
`task -> allow`, alongside either the default exact-file allowlist or the one
owner-authorized #210 packet-directory rule. It may issue exactly one native
`pr-reviewer` Task and may not read files, run shell/Git, call MCP or interpret the
result. This is a transient Task transport only: the selected
primary remains the sole authority for canonical selection/currentness, packet
preparation, review ordinals, parent audit and terminal status. A newly created child
under a long-lived parent is not proof its cached agent configuration was refreshed.

Before reserving a whole-PR review ordinal, run a fresh native Task access preflight
under an equivalent deny-by-default overlay for harmless synthetic files. For #210,
use a 48-file directory-shaped fixture with the same single-directory rule; prove all
files can be read completely and sibling/absolute/traversal/symlink escapes are denied
before content. Verify the parent is Task-only and child Read-only. These probes consume
no review ordinal and demonstrate Kilo tool permissions only, not OS-level isolation.
After PASS, create a distinct overlay for the actual frozen packet: exact-file allows by
default, or the single exact #210 directory rule above. The parent verifies the packet
is owner-prepared, regular/non-symlink, inventory/hash-identical and exact to selected
refs before dispatch.
Post-execution trace audit checks that only permitted Read calls occurred and that
their bytes/ranges match the frozen packet, but this is a second gate, not a substitute
for effective pre-execution Kilo denial. Canonical currentness is fetched and rechecked
by the primary before/after the task. Reviewer permission or source-provenance gaps are
COMMENT; never broaden access to repair them.

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
