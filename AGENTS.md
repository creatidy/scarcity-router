# Instructions for repository agents

## Mission and priority

Build a small service that recommends which available subscription-backed AI
model to use now by combining task requirements, model capabilities, current
capacity and user policy. The governing rule is: **choose the least scarce
model that is capable enough for the task**.

The owner's real workflow takes priority over novelty, provider-count metrics,
market features and abstract platform design. Solve the current OpenAI and Z.ai
subscription-capacity use case before expanding scope.

Read the documentation map in `README.md` and the relevant authoritative
document before making a change. If documents conflict, stop and resolve the
conflict explicitly in `docs/decisions.md`; do not choose silently.

## Repository workflow

This project uses one issue → one feature branch → one PR, with `main` as the
integration branch and human review as the merge gate. The invariants are
authoritative and non-negotiable for agent work:

- Never make a substantive file modification while checked out on `main`.
  Establish the issue and create the feature branch before the first
  task-related edit. `main` is only for integrating merged work.
- One issue drives one feature branch and one PR. The PR targets `main`.
- An agent leaves its PR open and unmerged. Do not merge your own PR; a human
  is the merge gate.
- Before a later task starts, any uncommitted or unmerged work from an earlier
  related Scarcity Router task must be resolved through its own issue/branch/PR.
  Do not bypass an unmet dependency by opening a clean secondary worktree.
- A clean secondary worktree based on current `origin/main` is acceptable only
  to preserve genuinely unrelated user work; it must not absorb unrelated
  changes or sidestep an earlier task's work. Preserve user work untouched.
- No force-push, no rewriting published history, no merging `main` into the
  feature branch, and no synchronization merge commits.
- Before completion verify that `origin/main..HEAD` contains only this task's
  diff, that there is no unintended merge commit, and that the change touches
  only the files the issue scopes.

This discipline exists because leaving valuable uncommitted work on `main` that
a dependent agent then had to reroute is a workflow failure to prevent.

## Multi-agent orchestration safety

Multi-agent orchestration is bounded by default and is never implicitly
unlimited. Before any worker or reviewer starts, record the task scope, frozen
threat model, expected complexity boundary and execution budget. The defaults
are:

- `max_initial_review_rounds = 1`;
- `max_remediation_rounds = 1`;
- `max_final_verification_rounds = 1`;
- `max_worker_retries = 1`;
- `max_reviewer_retries = 1`;
- `max_wall_clock_minutes = 120`.

A task may override a default only explicitly, with a stated reason, before
orchestration begins. There is no implicit unlimited mode. Budget exhaustion or
failure to make meaningful progress after the retry budget means
`STOP_AND_ESCALATE_TO_HUMAN`; repeated polling is not progress.
Worker and reviewer retries are independently bounded. Each retry only recovers
an interrupted or stalled session; it is not permission to restart an entire
task or review repeatedly.

The default lifecycle is:

`implementation -> independent review -> optional single remediation -> final verification -> human merge gate`

Every reviewer finding is exactly `MERGE_BLOCKER` or `DEFER`. A
`MERGE_BLOCKER` materially violates current issue correctness, an explicit
security invariant, an explicit public/data contract, normal resource/lifecycle
safety or repository workflow integrity. A `DEFER` is out-of-scope hardening,
architecture, future compatibility, marginal robustness or scope expansion;
only blockers may trigger the single remediation. Independent review improves
decision quality inside the frozen boundary and does not authorize recursive
review/fix cycles. Independent specialist review is valuable precisely because
it is independent, but independence does not imply recursive authority over
task scope. Reviewer strength does not expand the frozen threat model. A
concern outside that threat model is `DEFER` unless it violates an existing
global security invariant.

Review starts from one immutable `reviewed_head`. Workers must not push while
review is in progress; a branch change invalidates the review result and it
must not be treated as approval. Serialize worker, reviewer, remediation and
verification phases, and never run a reviewer concurrently with the remediation
worker on the same PR. Final verification only checks the identified blockers
and obvious regressions from remediation; it is not a fresh architecture
review, and it has no automatic third review/fix cycle. A new
final-verification finding is `DEFER` unless it is an obvious critical
correctness or security regression. After final verification,
`READY_TO_MERGE` stops orchestration; a remaining `MERGE_BLOCKER` stops and
escalates to the human.

Before implementation, the orchestrator records a rough expected complexity
boundary. A material breach stops work for architectural triage rather than
accumulating review-driven defenses. Stalled or repeatedly retrying worker or
reviewer sessions consume at most the configured retry budget, then escalate to
the human. Workers, reviewers and orchestrators never merge; the human remains
the merge gate.

## Current phase

M0 is complete and M1 is the current milestone. Executable product code may now
be added only for explicitly scoped M1 work. M1 begins with read-only collector
reconnaissance and normalized status collectors, not routing. Documentation
utilities or repository metadata must not masquerade as a working broker.

## Product boundary

The product recommends; it does not execute.

Do not build prompt proxying, model-call execution, repository ingestion,
source-code inspection, a generic LLM gateway, autonomous fallback execution
or client-specific business logic. Kilo, Codex, Claude Code and other tools are
consumers, not foundations of the core.

Local inference providers, including Ollama, are not supported by Scarcity
Router. Do not add, restore or preserve a local-model provider, local runtime
health path or local-first routing mode without a new explicit product decision.

Keep these concepts separate in schemas and code:

- intrinsic model capability;
- task requirements and hard constraints;
- runtime capacity and health;
- user policy and temporary preferences.

Quota never changes a capability rating. Provider-specific parsing belongs in
provider adapters, never in the selector or public adapters. CLI, REST, MCP and
any dashboard must call the same authoritative core.

## Security invariants

Credentials are data the broker may use transiently, never product output.

Never print, log, return, persist in fixtures, commit, copy unnecessarily, send
to analytics, expose to an agent or place in an exception an authentication
token, cookie or secret. Never ask an LLM to inspect credential values. Use
existing authenticated local tools or stores read-only where possible.

Before attaching a credential to any request, enforce HTTPS and an exact
provider-host policy. Do not send credentials to arbitrary endpoint overrides.
The REST listener must bind to `127.0.0.1` by default. New network exposure,
credential storage or write access requires an explicit security decision.

Tests and documentation use structurally representative synthetic/redacted
fixtures only. Review errors, debug output and subprocess capture for secret
leakage. See `docs/security.md` for the complete invariants.

## Capacity collectors

Collectors are small adapters that translate one provider response into the
normalized model in `docs/capacity-model.md`. Collection is read-only except
for the single owner-approved exception in `docs/decisions.md` D-018 (one
provider-managed OpenAI credential refresh after the evidenced app-server
`-32603` rate-limits error, followed by one retry).

For every adapter:

- keep provider-specific logic at the edge;
- validate known schema and semantics;
- classify windows from validated metadata, not field position;
- preserve useful non-secret raw metadata without preserving credentials;
- represent unknown or changed semantics explicitly;
- fail safely to a provider status such as `schema_changed`, `unsupported` or
  `unknown`; never guess that missing telemetry means 0% or 100% remaining;
- add redacted fixture-based parser tests and contract tests;
- test schema drift, missing fields, unknown windows and secret redaction;
- report retrieval time, source mechanism, freshness, status and all known
  windows.

Do not assume `primary` always means five hours, `secondary` always means a
week, or undocumented numeric Z.ai units will remain stable. Do not silently
change a mapping when upstream changes; update evidence, fixtures and the
decision record.

## Capability and selection governance

Capability ratings are curated assessments, not measured quota and not
scientific fact. Do not add or silently change a rating without provenance,
date/version, confidence, rationale and a human-reviewable diff. A catalog
change must not be smuggled into unrelated code.

Selection must:

1. enforce hard constraints before scoring;
2. reject capability-deficient candidates;
3. apply reservations without declaring a capable model intrinsically weak;
4. consider every relevant quota window and never choose the most optimistic
   one;
5. prefer the least scarce sufficient candidate under the active policy;
6. return ranked alternatives, including useful exclusion/reservation reasons;
7. return a structured explanation and degraded-confidence state when inputs
   are unknown.

Do not return only a model identifier. Explainability is a public contract.
The broker recommends fallbacks but does not execute them.

## Engineering style

Prefer small, reviewable changes and boring abstractions. Isolate provider and
client churn at the edges. Avoid dependencies unless they materially reduce
complexity or security risk. Novelty is not a goal.

Before adding a dependency, record why the standard library or an existing
dependency is insufficient. Do not introduce a routing framework, gateway or
collector framework by default. Protocol-level interoperability and independent
implementation are preferred over runtime coupling to reference projects.

Once a public CLI, REST, MCP or serialized-data contract is released, preserve
backwards compatibility or use an explicit versioned migration and decision.
Keep profiles and policies data-driven rather than scattered constants.

Every change should include the smallest relevant verification. Provider work
requires contract/fixture tests. Selection work requires deterministic policy
tests and explanation assertions. Security-sensitive work requires negative
tests for leakage and unsafe endpoints.

Type-checking gate: Python changes require `basedpyright`; plain `pyright` is
not an acceptable substitute. The tool is repo-managed and reproducible: it is
declared as a development-only dependency in `pyproject.toml`, pinned in
`uv.lock`, and invoked through the uv-managed environment (`uv run
basedpyright`), never a global executable. Before committing or
opening/updating a PR, run `make check` (full test suite + typecheck) and
require exit 0. basedpyright runs its default `recommended` ruleset; any
finding that makes that gate fail must be resolved by fixing the underlying
typing or code structure. Do not weaken the gate by downgrading
`typeCheckingMode`, adding `reportXxx = none` overrides, excluding product or
test code, or baselining findings; a narrow suppression requires an explicit
justification reported with the change. `make typecheck` runs the type
checker alone and `make test` runs the test suite alone.

## Decision discipline

Do not invent answers for unresolved issues. Add or update an entry in
`docs/decisions.md` with status `Proposed` or `Unresolved`, alternatives and the
evidence needed. Accepted choices may be changed only through an explicit new
decision that states what it supersedes.

Document copied or substantially adapted MIT-licensed code and preserve the
required notices. Architectural inspiration alone does not require runtime
coupling. Keep the project under Apache-2.0 unless an explicit licensing
decision supersedes it.
