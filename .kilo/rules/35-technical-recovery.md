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
review dispatch, including failed/COMMENT attempts and failover, also consumes the
next reserved ordinal within the default ten-review ceiling. Never dispatch review
11 under that default or patch without capacity for all mandatory fresh reviews.
Only an attested issue-local exception follows the prospective procedure below.
Budget exhaustion is a finite stop,
not permission to weaken a gate or invent an owner decision.

## Prospective Review Capacity

Before ANY remediation that changes HEAD/base or any new review reservation,
the primary reads the persisted, Git-locally excluded `.task_progress.md` delivery
ledger. Recover every reserved ordinal and outcome, including failed, COMMENT,
invalidated and tool-policy-violating reviews; reentry never resets or discards them.
Missing/ambiguous ledger recovery is BLOCKED, never zero. Require exact nonnegative
integer counts (not booleans/floats), contiguous unique ordinals 1 through `used`,
and agreement between the recovered count and reservations.

The effective ceiling is the default 10 unless independently attested explicit
canonical owner authority changes it for this exact issue/PR. Ledger claims alone
never authorize an exception. Require an exact positive integer ceiling and
`used <= ceiling`; reject any proposed next ordinal above it. This does not change
the global ten-review policy: #205/PR206 alone has the one-time ceiling12 in full
original owner decision comment16179. Superseding owner correction comment16250
preserves all consumed ordinals1-10 and ceiling12: fresh ordinary11, then restricted12
only after valid ordinary APPROVE with empty findings and verified context/tool audit
at the SAME exact HEAD/base. No review13 or automatic extension. Ordinals are
chronological counters, not immutable roles; derive roles from current approval
dependencies, never freeze a future security slot while ordinary approval is missing.

For the proposed round compute `remaining = ceiling - used` and the missing
mandatory roles using exact current HEAD/base approvals with empty findings and
verified context/tool audit, validation and currentness. Stale/invalid approval is
missing. Any HEAD/base-changing patch invalidates BOTH security-critical approvals;
count ordinary plus restricted fresh whole-PR reviews, not merely the next review.
Non-security work needs only ordinary review. A stable HEAD with one exact current
approval needs only the other gate; both current approvals need zero. Capacity
alone is NOT approval/currentness and cannot waive any acceptance gate.

| Round State | Required Fresh Roles | Required Count |
| --- | --- | --- |
| security_patch_or_both_missing | ordinary,restricted | 2 |
| ordinary_patch_or_missing | ordinary | 1 |
| stable_ordinary_approved | restricted | 1 |
| stable_restricted_approved | ordinary | 1 |
| stable_all_approved | none | 0 |

Require `remaining >= required_count` AND an authorized ordinal/role sequence that
can close every missing gate, including ordinary-before-restricted order. Numeric
space alone cannot authorize security before ordinary approval. For #205, ten consumed
slots plus two missing fresh roles is PASS with attested ceiling12: next11 ordinary,
then12 restricted only after current audited ordinary approval. If11 requests changes,
a further patch needs two fresh roles but only one slot remains: STOP_REVISE before
edits. If12 does not approve, STOP_REVISE, never13. Genuine findings remain valid
regardless of inconvenient capacity. Persist the prospective PASS/refusal, recovered count,
effective ceiling with canonical provenance, exact HEAD/base, proposed object
change, required roles/count and dependency-derived sequential role check in the SAME excluded ledger
BEFORE mutating or reserving. A finding with insufficient closable capacity yields
STOP_REVISE before edits; uncertain count/authority yields BLOCKED. At the ceiling,
both exact current approvals may advance through existing merge gates without an
extra dispatch. This declarative procedure and its offline consistency fixtures
are not a second controller or proof of runtime enforcement.

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

## Reviewer Context and Result Capture

This is repository-development preparation/capture, not a product gateway or
second orchestrator. Preserve the fresh native `pr-reviewer` binding, complete-PR
JSON schema, read-only tools and additional direct-Codex restricted security gate.

Before each review, parent prepares an acceptance-only packet. Start with neutral
canonical issue/PR links and exact frozen base/HEAD/merge-base, not a verdict or
remediation narrative. Include the complete canonical issue body, full original
text/context of applicable owner decisions and accepted ADRs/producer contracts.
For each object record canonical URL, object/comment ID, author, timestamp,
retrieval path and content digest; local sources also carry exact Git revision/path.
The parent identifies applicable authoritative decisions before dispatch. Do not
ask the reviewer to browse comment history to distinguish authority from old findings.
No blanket comment history, previous reviews/findings, implementer summaries,
private conversations, local progress or favorable excerpts enter the packet.
Missing decision provenance is an evidence gap, not permission to paraphrase it.

The packet's object manifest names only exact read operations and IDs:
`forgejo-mcp_get_pull_request_by_index`, `forgejo-mcp_get_issue_by_index`, and, only
for an attested owner decision, `forgejo-mcp_get_issue_comment`. Read frozen source
locally. Do not grant wildcard get/list/search or review/history getters. These
tool-name restrictions are NOT an argument-ID firewall: parent must audit actual
post-Task tool routes/arguments against the manifest. A forbidden history read
invalidates approval even if the verdict says APPROVE. If trace access is absent,
record the unverified context/access gap; self-report alone does not close it.

Every reviewer Bash call is one allowlisted command, with no `&&`, `;`, `|`,
redirection, command substitution or interpreter escape. Audit actual shell calls,
not just claimed checks_run. Tests of guidance/fixtures do not establish actual
Kilo permission enforcement. Parent prepares safe validation before dispatch;
reviewers cannot widen tool access or repair infrastructure themselves.

For external CLI capture, use existing Kilo `background_process` tracking or a
foreground POSIX blocked wait. `tools/review_result.py` is an optional stdlib
adapter around one explicit parent-prepared argv/environment, never model
selection, authorization, budget reservation, shell wrapping or retry dispatch.
It must not be imported by product code or distributed as a product command.
Parent reserves the existing ordinal and checks ordinary approval/currentness
before a restricted launch. No literal issue/budget/model defaults live in the helper.
Parent verifies actual installed executable/version and records that observation,
then supplies `Context`, `TextPolicy`, `executable_version` and the prepared argv.
No prompt/argv/environment values or credential material enter receipts.

Allocate fresh 0700 directories below the expected parent-private directory;
keep the receipt store outside the reviewer's filesystem view, and expose only
the separate native output directory. Verify runtime output is Git-locally excluded
before launch. Pass explicit child environment, never ambient inheritance; native
file creation uses umask077. The POSIX helper requires no-follow dirfds and owned
single-link 0600 files, rejecting aliases, foreign owners and permissive modes.
It never normalizes permissions. Unsupported operator capabilities are a recovery
limitation, not a Linux-only Router product requirement.

An exclusive durable launch claim prevents redispatch. Observe actual executable,
host/PID, native session when available, frozen objects and bounded sanitized
progress. Distinguish prepared, started/running, exited, retrieved, validated and
rejected. Requested model is not observed identity: receipts stay UNVERIFIED
unless a separate independent attestation establishes runtime identity. CLI exit,
completed turn, extraction, schema, revision, content safety, substantive verdict
and parent approval are separate. Status0 means complete capture of ANY verdict,
not APPROVE; schema0/process0/model self-report never close a review gate.

Native `--output-schema` requests generation format, not local validation.
`--output-last-message` failure/non-JSON can coexist with CLI exit0. If and only if
the native file is missing, use the LAST `item.completed` agent_message from the
final successfully completed turn, never initial commentary/reasoning/tool text.
Native file safety/schema rejection cannot be bypassed by JSONL fallback; conflicting
sources reject. Preserve typed reason, size/hash, known-field types/missing fields
and observed mode before exact owned-raw cleanup. Retain only safety-screened
bounded known structured envelopes, explicitly NOT_APPROVAL; unsafe data is not
persisted. Screening is conservative, not proof arbitrary prose has no unknown secret.
Public long-token exceptions require exact parent-attested Git tracked paths/pins
and manifest provenance, never generic hex/token exemptions; explicit secrets and
forbidden fields have precedence. Parent owns provenance verification independently.

On handle loss, reopen the same private stores and call `recover`; inspect durable
claim/spawn/turn/exit/receipt before any retry. A claim without exit remains uncertain,
not launch permission. Recovering/revalidating existing output consumes no new model
call or review ordinal. Never infer the subtype/verdict of historically discarded output.
Validate caller context, durable launch claim shape/types and original stored binding
before ANY recovery mutation: metadata scrubbing, receipt rewriting, verdict
invalidation and native cleanup included. Compare exact canonical JSON types, not
permissive boolean/integer equality; incoming metadata never replaces the claim.
Missing/corrupt claim or binding fails typed without writes. Wrong HEAD/base leaves
all completed/rejected evidence and cleanup state unchanged; correct original
context remains recoverable. Only valid same-context policy rescreening may scrub
newly unsafe retained data; receipt integrity and sticky source rejection remain.
Distinguish validated finite internal diagnostic/status fields from untrusted
reviewer payloads and freeform identity/provenance strings. A forbidden literal
must still reject reviewer content, never null an internal required enum. Prepare
typed sanitized records before writes; the existing receipt rejection journal
keeps pending payload/metadata disposal recoverable across interrupted writes and
policy relaxation without upgrading rejection or authorizing a new launch.
A queued claim must preserve the verified original's fields and exact JSON types,
except string-to-null disposal. Recovery of an interrupted final receipt/claim
transition requires that proven discard-only transformation and a still-matching
original claim, never caller replacement identity. Finish native disposal before
irreversibly tombstoning the claim; blocked cleanup retains original retry authority.
After retrieval, parent continues already-authorized execution in the same invocation:
check context/tool audit, validation and fresh canonical state, apply the substantive
verdict and all model-bound gates, or perform bounded changed-condition recovery.

Integration evidence is separate from synthetic helper tests. Dated native contract
inspection used official Codex0.159.3 source pin
`01fc69f4026735edfdf6789820549727a4867b11` at
`https://github.com/openai/codex`; private synthetic actual-binary evidence belongs
in excluded delivery artifacts, not this rule. This pin is provenance, not a durable
version requirement. Synthetic backend capture cases do not prove paid/live model
identity, tool activation, source access or permission enforcement. Independently
verify the complete frozen source/read-only inspection path before accepting a
restricted review; supported no-tools execution needs a complete source packet,
not an assumed live tool path. Preserve any unevidenced integration gate explicitly.

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
