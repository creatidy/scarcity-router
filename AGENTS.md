# Instructions for repository agents

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
Both use the same fresh isolated `.kilo/agents/pr-reviewer.md` native task;
implementation self-review and resumed reviewers are not independent review.
Consume task results directly; Forgejo review publication is optional, never
orchestration state or an owner-relayed handoff.

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
implementation controllers. Review frozen Git objects/current clean PR branch
read-only; the parent must not edit or switch reviewed checkouts while the reviewer runs.
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
a workspace reload may be required. Missing native task/agent support requires
bounded authorized independent reviewer failover before a finite blocker, never
permission for parent self-review, weakened gates or external orchestration.

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
  ownership conflict (U-014, #180), not target conformance or permission to
  extend repository access. Preserve it until explicit migration authority.
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

## Bounded review lifecycle

Multi-model work is bounded by default. Before workers or reviewers start, freeze
scope/threat model, expected complexity boundary and execution budget. D-069
supersedes D-015/D-029's development-delivery review/merge defaults only: repository
issue delivery uses at most 10 whole-PR review invocations, including initial,
COMMENT, invalidated reviews and corrected retries. `/finish-pr` and `/loop`
reserve each ordinal BEFORE dispatch in Git-locally excluded `.task_progress.md`.
Reentry, phase, task, model or session changes never reset the delivery counter;
missing/ambiguous recovery is BLOCKED. Never dispatch review 11 or make patches
that cannot receive a fresh review within the bound.

Each review is fresh, foreground, read-only and covers the complete PR at exact
frozen HEAD/base. Serialize dependent phases; no pushes/edits during review.
Every HEAD/base change invalidates approval. Only exact APPROVE with empty findings,
clean checkout, successful required validation and fresh canonical currentness
yields READY_TO_MERGE. Standalone delivery stops there; only explicit `/loop`
continues through merge, integrated acceptance, closure and SELECT. Genuine owner
decisions stop the whole loop with STOP_AND_ASK; remaining actionable findings
at the bound yield STOP_REVISE. Infrastructure failures require diagnosed bounded
technical recovery before BLOCKED, with failed/COMMENT ordinals preserved.

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

**Security-critical work carries an additional independent
`gpt-daybreak-blue-latest` review gate** when the work is explicitly
classified `security_critical`. If that restricted access is unavailable the
record states `SECURITY_REVIEW_UNAVAILABLE` and the gate stays open at the
human decision point; no substitute model may close it — see
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
