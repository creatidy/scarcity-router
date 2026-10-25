# Instructions for repository agents

## Current D-069A And #212 Status (2026-10-20)

This current notice supersedes conflicting later historical #212 text in this file
and any command/profile/rule mirror; the completed canonical state below is authoritative.
All #1-20 for #212/PR #213 are consumed; #20 approval applies only to
`1dcb4a08d9d57d02ed67bf8e7f7d15275c5b3ecf`, cleanup `cddcac0d7a475c32eb2d8ad942b4f004c1ea1256`
was separately owner-accepted, and `121ee3fe33b74dc38b2401d22e7aa6df1b124bc9` integrated
the PR and closed #212. No #20 reuse, #21 or transfer. Other deliveries keep their
own scope and gates. D-069A's default ceiling is ten; its verified, exact #210/PR #211
owner exception is maximum11 only, with nine outcomes preserved, #10 ordinary, #11
owner-manual security only after #10 passes, and no #12.

For Router owner-manual Codex security review only, backend identity may be honestly
`RUNTIME_UNOBSERVABLE` under the exact explicit owner acceptance if all other required
official binary, requested dispatch, native session/turn/input/output, context/tool,
verdict and current-ref evidence passes. Never infer `RUNTIME_VERIFIED`; mismatch or
unauthorized substitution is FAIL, other evidence gaps keep the gate open. No script,
no automatic Daybreak, generic identity-policy change, or skipped security review. See
D-069A and `docs/llm-operating-policy.md` for the complete contract.

## Security Review — Continuous PR Security and Final Project Audit

D-081 records Adrian's direct owner decision of 2026-10-25T18:46:24Z for Scarcity
Router only, effective immediately. It supersedes prospective mandatory per-PR
Daybreak gates in D-053 and related decisions, issue criteria and workflow mirrors;
historical reviews, findings and consumed ordinals are not rewritten. The older
allocations/provenance above are historical, not a new per-PR security handoff.

- Every consequential PR requires independent correctness review including applicable
  security assessment. Security-critical changes require rigorous ordinary independent
  review by a sufficiently capable model at the complete frozen PR HEAD/base.
- P0/P1 defects, unresolved material security findings and data-integrity risks block merge.
  Fix and independently verify them before integration, never defer them to the final audit.
- Daybreak is not a PR merge gate. Individual PRs do not wait for Daybreak merely
  because they are security-sensitive. With all ordinary acceptance gates satisfied,
  the authorized `/loop` continues through Forgejo integration into `develop` without
  another routine owner confirmation. D-078 conversational independence is unchanged.
- Daybreak is reserved for one final comprehensive project-level audit of a frozen
  release candidate before the first production release. This is a release acceptance
  gate, not an implementation or `develop` integration prerequisite. Audit findings
  must be corrected and verified before release; a changed candidate needs current
  audit acceptance. Scope, future access/provenance verification and official sources
  are in `docs/llm-operating-policy.md#final-release-candidate-audit`.
- No Daybreak invocation, access provisioning, release, deployment or promotion to
  `main` is authorized here. No deprecated alias is a future reviewer requirement.
  PR #209's owner pause and unresolved SQLite transition race remain intact; this
  timing change neither resumes that delivery nor waives its correctness finding.

## Mission and priority

Build a small service that recommends which available subscription-backed AI
model to use now by combining task requirements, model capabilities, current
capacity and user policy. The governing rule is: **choose the least scarce
model that is capable enough for the task**.

The product is two-mode (D-040): recommendation-only by default, plus an
optional execution gateway that serves authorized requests from the best
available resource through one OpenAI-compatible endpoint. The owner's real
workflow takes priority over novelty, provider-count metrics,
market features and abstract platform design. Read the documentation map in
`README.md` and the relevant authoritative document before changing a topic.
If documents conflict, record the conflict and resolve it explicitly in
`docs/decisions.md`; do not choose silently.

Follow Creatidy ownership and the evidence/status rules in
`docs/architecture.md#creatidy-system-alignment` (D-068); use the G01-G13
coverage matrix in `docs/roadmap.md`, with Forgejo as the task source.
Kernel owns task/workspace/harness lifecycle and outcome; Router owns
inventory/private telemetry/selection/admission/provider calls; MI owns
public knowledge; Console consumes authorized state/commands. Do not create
a second inventory/ranker/sandbox or drop known requirements for a minimal
delivery. Proof-dependent work belongs in a bounded contract/research issue.

## Repository workflow

Forgejo (`https://forgejo.creatidy.com/BioMedical-IT/scarcity-router`) is the
canonical repository for issues, pull requests, reviews, branches and
integration. GitHub (`https://github.com/creatidy/scarcity-router`) is an
automatic read-only mirror. Do not create or mutate GitHub issues, pull
requests, branches or tags unless Adrian explicitly requests a GitHub-specific
operation.

Read these rules explicitly before work; do not rely on automatic nested discovery:

- `.kilo/rules/10-task-system.md`: selection, branching and delivery gates.
- `.kilo/rules/20-forgejo-mcp.md`: canonical authority and tool boundaries.
- `.kilo/rules/30-implementation-discipline.md`: scope, autonomy and compatibility.
- `.kilo/rules/35-technical-recovery.md`: bounded autonomous blocker recovery and escalation contracts.
- `.kilo/rules/40-local-search.md`: evidence and excluded operational memory.
- `.kilo/rules/40-llm-operating-policy.md`: model and evidence discipline.
- `.kilo/rules/validation.md`: repository validation and handoff.

Commands: `/implement-issue <number|URL|unambiguous title>`,
`/review-pr <Forgejo PR number|URL>`, `/finish-pr <Forgejo PR number|URL>` and `/loop`.
A plain `Implement issue #N` follows the implementation workflow. Standalone
implementation requires owner selection. `/review-pr` is read-only; `/finish-pr`
authorizes only the selected PR's accepted-scope remediation, never merging.
Ordinary Kilo review uses a separate fresh USER-VISIBLE reviewer session, started
autonomously by an authorized `/loop` or `/finish-pr` orchestrator through native
Kilo session management. User visibility does not require the owner to start the
session or relay its result. Read `Independent Review — Conversational Separation`
below. This does not authorize hidden inference through scripts, subprocesses,
Task/subagents, background jobs, HTTP or generated wrappers; no silent substitution
or fallback is permitted. D-081's final project audit remains a separate release gate.

Only an explicit owner `/loop` delegates autonomous selection, approved canonical
PR merge into `develop`, and completed-issue closure. Its primary invocation
context is the sole orchestrator, reusing implement-issue/finish-pr. Read
`.kilo/command/loop.md`: refresh all canonical open issues each cycle, exclude
only exact invalid/wontfix/duplicate labels case-insensitively, verify explicit
gates, then order by explicit priority, required ordering and oldest registration.
Issues, not PRs, are planning authority. STOP_AND_ASK, STOP_REVISE and BLOCKED
terminate the invocation; no eligible issue yields QUEUE_EMPTY. Pre-merge exact
label exclusion returns nonterminally to SELECT after a safe checkout return,
without merging or completing that issue. No speculative issues sustain the loop.
Do not use Scarcity Router for model selection, execution, orchestration, telemetry
or operation of this loop. No cross-repository mutation, main, release or deployment
authority is granted. This is a repository development workflow, not Router product
architecture or a second controller/service/scheduler/daemon.

Use one selected issue, one feature branch and one Forgejo pull request per delivery.
Branch from `develop`, target `develop`, and leave standalone PRs open. Never
make a substantive task edit while checked out on `develop`; establish the
issue and feature branch first. `main` is human-controlled and is not the
ordinary agent integration branch.

Use one designated delivery checkout and one mutator. Temporary worktrees/isolated
evidence checkouts are allowed under the technical-recovery rule, never parallel
implementation controllers. The primary verifies canonical currentness, exact frozen
Git objects and the complete compact diff, then provides source/acceptance references
directly to the visible reviewer. Review is read-only, scoped to authorized source,
without credentials or implementation context. Do not edit/switch reviewed source
during review. No persistent `.review-evidence-*` directory, duplicated source tree,
large transcript or renamed receipt hierarchy is created or required. Deterministic
offline fixtures belong in cleaned OS temporary directories, not worktrees.
Never stash/reset unrelated owner work or bypass unmet dependencies. Do not
force-push, rewrite published history, merge
`develop` or `main` into a feature branch, create synchronization merge
commits, or push directly to `develop`. Only `/loop` may merge through its
supported canonical Forgejo PR operation and exact approval/currentness gates;
never force/auto-merge or promote to `main`.

Before completion, verify that `origin/develop..HEAD` contains only the current
issue's change, there is no unintended merge commit, and touched files are in
scope.

Commands and agents are loaded from `.kilo/command/*` and `.kilo/agents/*` by the
Kilo workspace runtime. Adding files does not guarantee dynamic availability;
a workspace reload may be required. Ordinary review uses the native Kilo session
path described below. Only a genuinely unsupported native session/result capability
after bounded recovery is a pending gate; never use a hidden model launcher, permission
for implementation self-review, native Task fallback, weakened gates or external
orchestration.

## Product boundary

The product is two-mode (D-040; see [`docs/product.md`](docs/product.md) and
the execution-gateway architecture in
[`docs/architecture.md`](docs/architecture.md)):

- **Recommendation-only mode (default):** Scarcity Router recommends; it does
  not execute. No prompt proxying, model-call execution, repository
  ingestion, source-code inspection, autonomous fallback execution,
  client-specific business logic or generic LLM gateway in this mode.
- **Execution gateway mode (optional, explicit deployment):** the
  authenticated server component may receive prompts and execute/proxy model
  traffic to authorized resources under D-040 through D-045 — including
  Ollama and local inference as execution resources (D-017 superseded) and
  the approved local Codex adapter. D-047's ZCode cancellation was superseded
  by D-061/D-063; that integrated workspace-editing lane remains a documented
  staged ownership migration (D-073, U-014, #180), not target conformance or
  permission to extend repository access. Preserve it until equivalent accepted
  Kernel-controlled functionality and its explicit compatibility/migration step.
  Still forbidden everywhere: autonomous
  fallback execution, issue-to-PR orchestration, repository management,
  generic agent workflow frameworks, general task schedulers,
  benefit-consuming actions (reset redemption stays informational) and
  executing client-supplied tools.

CLI, REST, MCP and any dashboard must call the same authoritative core; the
frozen recommendation-only interfaces keep their contracts. Keep intrinsic
capability, task requirements, runtime capacity and user policy separate;
quota never changes a capability rating. Authorization precedence and
executable-target semantics are frozen by D-042.

## Security invariants

Credentials are transient input, never product output. Never print, log,
return, persist in fixtures, commit, copy unnecessarily, send to analytics,
expose to an agent or place in an exception an authentication token, cookie or
secret. Never ask an LLM to inspect credential values. Prefer existing
authenticated local tools or stores and read them as narrowly as possible.
The sole recorded exception to transient credential handling is the
execution-gateway server component's explicit bounded store (D-044); every
other component and mode keeps the rule above.

Before attaching a credential to a request, require HTTPS and an exact
provider-host policy. Never send credentials to arbitrary endpoint overrides.
The REST listener binds to `127.0.0.1` by default; new network exposure,
credential storage or write access requires an explicit security decision —
D-044 is that decision for the execution-gateway server component (separate
administrator/inference-client/worker identities, verified TLS, no
`verify=false`, no shared default password, no bearer secrets in URLs,
admission limits, isolation rules; the full threat model is
[`docs/security.md`](docs/security.md)).
Collectors use only the bounded provider-managed OpenAI recovery exception
documented in [`docs/security.md`](docs/security.md) and
[`docs/capacity-model.md`](docs/capacity-model.md).

Use synthetic or redacted fixtures and review subprocess capture, diagnostics
and errors for secret leakage. The complete security authority is
[`docs/security.md`](docs/security.md).

## Provider edge and contracts

Provider-specific parsing belongs in provider adapters, never in the selector
or public adapters. Collectors validate known schema and semantics, preserve
useful non-secret metadata, represent unknown or changed semantics explicitly,
and fail safely rather than guessing missing telemetry as zero or full
capacity. They report retrieval time, source, freshness, status and all known
windows. Provider work requires redacted fixture and contract tests; see
[`docs/providers.md`](docs/providers.md) and
[`docs/capacity-model.md`](docs/capacity-model.md).

Do not change catalog ratings, capability claims or serialized contracts
without provenance, date/version, confidence, rationale and a human-reviewable
diff. Public CLI, REST, MCP and serialized contracts require backwards
compatibility or an explicit versioned migration and decision.

## Preserve proven operational properties

When replacing, generalizing or abstracting an existing working path, first
establish how that path currently works in the relevant real environment.
Relevant properties, when applicable, include: local vs remote operation;
interactive vs headless operation; authentication mechanism and credential
boundary; network topology; filesystem/isolation assumptions; process/service
lifecycle; deployment ordering; external tool/runtime behavior; and required
operator/manual steps. These are property classes, not permanent facts about
any installation.

An architectural abstraction may change implementation structure, but it does
not erase operational constraints of a proven working path. Existing working
behavior is a compatibility requirement unless the selected issue or the owner
explicitly authorizes changing it.

Do not promote transient environment observations into durable repository
policy merely because they were true during one implementation. Record such
facts as task/issue/PR evidence unless the owner explicitly declares them a
durable product or deployment contract.

## Independent Review — Conversational Separation

Independent review requires a fresh, separate reviewer conversation without the
implementation agent's conversational context. Independence means separation of
conversational state and judgment, not separation of filesystems or execution
environments.

The reviewer MAY use the same repository, working directory or existing checkout,
Git object database, development tools and runtime. A separate worktree, checkout,
container, sandbox, custom permission profile, duplicated source tree or dedicated
infrastructure is NOT required solely for reviewer independence. Use one only for a
specific demonstrated technical necessity, such as concurrent source mutation; do
not introduce it merely to create independence.

Give the reviewer only the task and scope, acceptance criteria, PR and exact source
references, relevant architecture/interface contracts, and source/test access
instructions. Do not transfer the implementation conversation, implementation
reasoning, private deliberations or a desired verdict. The fresh reviewer independently
assesses correctness, regressions, security where applicable, maintainability and
practical UX.

The reviewer remains read-only and reviews the exact intended source revision. The
implementation agent must not mutate the reviewed source during review. It may use
the same checkout while the source is frozen; the exact reviewed HEAD/base and current
canonical state remain mandatory.

For an owner-authorized `/loop` or selected `/finish-pr`, ordinary review-session
creation, concise prompt delivery, verdict collection and remediation handoff MUST
be handled autonomously through supported native Kilo session functionality. Use a
fresh user-visible session and collect its native result; never require owner prompt
or response relay. Do not substitute the implementation conversation or a resumed
implementation session for the independent reviewer.

If an Agent Manager worktree limitation exists, it MUST NOT be interpreted as a
requirement to create a managed worktree. Use a supported normal-session path and
existing repository access wherever possible. Report a blocker only for a real
unsupported session/result capability after the bounded recovery rules below.

Never use hidden model launchers, HTTP inference, scripts, subprocesses, Task/subagent
dispatch or generated wrappers. The native Kilo session start is permitted and
user-visible; it is not a hidden model invocation. Review independence does not
require a different model family, though an appropriate different model may be
selected when useful under the applicable model policy.

## Bounded review lifecycle

Multi-model work is bounded by default. Before workers or reviewers start, freeze
scope/threat model, expected complexity boundary and execution budget. D-069
supersedes D-015/D-029's development-delivery review/merge defaults only: repository
issue delivery defaults to 10 whole-PR review invocations, including initial,
COMMENT, invalidated reviews and corrected retries. D-069A permits a greater finite
maximum only for one exact canonical issue/linked PR after the primary verifies the
direct owner source, canonical repository/issue/PR identities, maximum and scope,
and verifies D-069A is already integrated in the freshly fetched `develop`.
The completed #212/PR #213 maximum20 exception and all consumed outcomes remain
history under D-077 and the current notice above, never transferable authority.
No delivery may use an exception present only in its own branch/PR. Absent, stale, ambiguous or
mismatched authority uses the default 10 and fails closed. `/finish-pr` and `/loop`
record minimal ordinals/outcomes/refs canonically before an imminent authorized
visible review or owner handoff. Local progress notes are optional and not an
eligibility/review/acceptance prerequisite. Reentry or model/session changes never
reset counts; unverifiable authority is not invented from agent transcriptions.
Never invoke above the effective authorized maximum or make patches
that cannot receive a fresh review within that bound.

Each review is fresh, user-visible, read-only and covers the complete PR at exact
frozen HEAD/base. Serialize dependent phases; no pushes/edits during review.
Every HEAD/base change invalidates approval. Only exact APPROVE with empty findings,
clean checkout, successful required validation and fresh canonical currentness
yields READY_TO_MERGE. Standalone delivery stops there; only explicit `/loop`
continues through merge, integrated acceptance, closure and SELECT. Genuine owner
decisions stop the whole loop with STOP_AND_ASK; remaining actionable findings
at the bound yield STOP_REVISE. Infrastructure failures require diagnosed bounded
technical recovery before BLOCKED, with failed/COMMENT ordinals preserved.
Authorized `/loop` and `/finish-pr` use native Kilo session management for ordinary
review creation and result collection. If that native session/result capability itself
is unsupported after bounded recovery, report the exact pending gate. Do not require
a separate worktree, skip acceptance, or equate a technical blocker with
repository-wide QUEUE_EMPTY.

General non-delivery work retains the descriptive `model-policy.json` defaults
(one initial review, one remediation, narrow verification, 120-minute budget and
independently bounded worker/reviewer retries). The command-specific ceiling is
not a target or an extension of product/model/security authority. Diagnose a retry;
never restart merely because the session/model changed. Complexity breaches stop
for a human decision.

**UX is a mandatory review dimension.** Every review answers
`UX impact: none` or assesses the user-facing consequences (first-run, happy
path, recovery, upgrade/model-change, unnecessary technical IDs, automatic
configuration discovery, actionable errors, necessary security friction,
repeated manual work, understandable state). A material UX regression is
`CHANGES_REQUESTED` even when the implementation is technically correct — see
[`docs/llm-operating-policy.md`](docs/llm-operating-policy.md).

The detailed operating policy is
[`docs/llm-operating-policy.md`](docs/llm-operating-policy.md), and its
machine-readable companion is [`model-policy.json`](model-policy.json).
For command delivery, D-070's shared technical-recovery rule requires classifying
engineering obstacles separately from owner decisions and attempting bounded
secret-safe recovery automatically. Record changed conditions, consumed budgets
and the STOP_AND_ASK/BLOCKED contracts before escalation; owner attention is not
a substitute for ordinary execution diagnosis.

## Validation gate

Run the smallest relevant checks during implementation and the full gate
before committing or opening/updating a PR:

```bash
uv sync --only-dev
make check
uv run basedpyright
git diff --check
```

Python type checking uses the repo-managed `basedpyright` through `uv`; plain
`pyright`, weakened rules, exclusions and baselines are not substitutes. Fix
the underlying diagnostic. Tests must be deterministic, behavior-focused and
independent of live provider quota.

## Decision and licensing discipline

Do not invent answers for unresolved issues. Record alternatives and the
evidence needed in `docs/decisions.md`; accepted choices change only through an
explicit superseding decision. Document copied or substantially adapted
MIT-licensed code and preserve its notices. Keep the project under
Apache-2.0 unless an explicit licensing decision supersedes it.
