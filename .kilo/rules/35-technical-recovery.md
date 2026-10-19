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
next reserved ordinal. The default effective ceiling is 10. The sole separate
direct-owner supersession is #212/PR #213: D-077 records maximum 12, with #11 the
final ordinary review and #12 owner-manual Daybreak only after valid #11 APPROVE.
Verify the preserved direct owner source receipt and exact canonical scope/currentness
before that reservation; this is not D-069A and cannot transfer. For other deliveries,
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

The primary alone fetches canonical Forgejo Repo/Issue/PR and directly linked
owner-decision objects with exact getters (`forgejo-mcp_get_repo`,
`forgejo-mcp_get_pull_request_by_index`, `forgejo-mcp_get_issue_by_index` and only
the directly linked `forgejo-mcp_get_issue_comment`), and fetches/verifies the Git
objects. Bind IDs, canonical URLs, author/timestamp/raw-body digests, full HEAD/base/
merge-base, blobs and the complete merge-base-to-HEAD diff against the primary-prepared
clean local Git objects into a sealed, indexed packet. Verify the exact merge
base/full diff. The reviewer has no direct Forgejo getter, no Bash/Git and receives
Read-only packet access with no checkout, source/environment paths, prior review
results or parent conclusions. The reviewer must not inspect credential-bearing Git
configuration or invoke credential helpers.

The committed `.kilo/agents/pr-reviewer.md` and
`.kilo/agents/pr-reviewer-standalone.md` remain statically deny-all. For each
standalone review, the primary builds a per-run Kilo config directory with a generated
profile copy whose identity/mode/prompt match the tracked standalone profile and whose
frontmatter contains only exact Kilo worktree-relative Read patterns for indexed files
under wildcard deny. Although Kilo 7.8.8's Read schema describes `filePath` as absolute,
the handler resolves relative values against `instance.directory`; supply only exact
indexed `packet/<file>` values in the prompt. For this sealed
non-VCS workspace, Kilo resolves `projectID=global` and uses the filesystem root as its
worktree, so permission patterns are exact paths relative to that root (for example,
`tmp/.../workspace/packet/index.md`), not merely `packet/index.md`. Do not add absolute,
directory or prefix allow rules. The profile config is separate from the packet-only
workspace outside the source checkout. Keep per-run `kilo.json` and `KILO_CONFIG_CONTENT`
empty (`{}`), and set `KILO_NO_DAEMON=1` so the CLI cannot attach to a server using another
configuration. Use installed Kilo 7.8.8 resolver/source semantics: merge order is
runtime-specific, `findLast` rules are significant, and static/global/managed/saved
rules can override an assumed resolver. Before any reviewer session, inspect
`kilo debug agent
pr-reviewer-standalone` under the exact production environment. Require the tracked
profile frontmatter to be `mode: primary`, the resolved Kilo mode to be CLI-runnable
(`primary` or `all`, never `subagent`), the exact prompt hash and Read as the sole
enabled tool; evaluate exact per-file profile-frontmatter winners, default-deny paths and
forbidden-tool denies. Missing output, default-agent fallback, any broad capability or
an unverifiable merge order aborts before reserving an ordinal.
The r2 actual session used the correct absolute filePath but was denied by `read:* deny`.
Its `projectID=global`, together with the pinned Project/Read source, establishes that
the runtime permission pattern was rooted at `/`; the profile instead allowed
`packet/index.md`, so the wildcard correctly won. In r3 the model supplied a different
nonexistent absolute path; Kilo returned `File not found` before requesting Read
permission. Thus r3's missing metadata was a path-resolution failure, not an
authorization result. `KILO_NO_DAEMON=1` also prevents a possible shared-server config
mismatch; prior trace does not prove whether a daemon was attached. The prompt must bind
every allowed file to its exact relative packet path, and the audit may accept only the
expected prompt verbatim or Kilo's exact literal-double-quote wrapper. These preflights
consume no review ordinal.

The standalone Kilo CLI process has its working directory set to a sealed temporary
workspace whose only entry is a `packet/` directory; it does not receive the source
checkout. Launch a fresh `kilo --pure run --agent pr-reviewer-standalone` primary
session without continuation, resume, fork or Task arguments. Verify the exported
session records the exact agent, `parentID: null`, packet-only directory and requested
provider/model/variant. The actual enabled tool set is native Read only for exact
worktree-relative packet patterns listed in the generated profile. Do not pass checkout/
environment paths, credentials, issue comments outside the packet or previous
findings. Do not grant wildcard getters,
search/list, Forgejo writes, directory enumeration, external network tools, memory,
Task/delegation or external-file Read access.

Before reserving a whole-PR review ordinal, run the same standalone CLI path against
the complete production-equivalent synthetic packet plus harmless sibling, parent,
absolute, traversal and symlink-escape canaries outside the workspace. Require every
authorized file to be read completely and every escape denied before content. Audit
the actual standalone session, permission decisions, Read calls/returned bytes,
forbidden-tool absence and unchanged packet inventory. Static resolver output alone
is not proof. Pre-access debug/preflight and post-review session-export/packet audits
are separate mandatory gates. Preserve the full native session in Kilo's local store
by exact session ID; export it in memory and persist only filtered trace/result
metadata, not raw packet contents. Preflights do not consume whole-PR ordinals. The
only directory-level Read exception remains the owner-authorized issue #210 / PR #211
scope; it never transfers to #212 or other deliveries. Do not claim OS isolation.

## Public Evidence and Reviewer Failover

A public-research reviewer inspects cited material independently from pinned source
bytes in the packet. If a pinned source is inaccessible, try an alternative available
read path, then fetch/clone the exact public revision in an isolated evidence checkout.
Connector failure is not an implementation finding.
The primary must preserve pin, origin and provenance while staging exact revision
content, Git blob/source digest and relevant ranges. The reviewer verifies the material
claims against those bytes; implementer research conclusions are not independent
verification. The primary's research conclusions are not independent verification.
Parent conclusions are not evidence. If a material source/pin is
absent or inconsistent in the packet, report that concrete gap. Reviewer external
network is intentionally denied, so do not fetch/clone or treat that denial alone as a
change finding. The primary may use an isolated, credential-free public-source
checkout to prepare the pinned evidence before sealing the packet.

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
