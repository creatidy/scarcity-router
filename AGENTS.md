# Instructions for repository agents

## Mission and priority

Build a small, local-first service that recommends which available AI model to
use now by combining task requirements, model capabilities, current capacity
and user policy. The governing rule is: **choose the least scarce model that is
capable enough for the task**.

The owner's real workflow takes priority over novelty, provider-count metrics,
market features and abstract platform design. Solve the current OpenAI, Z.ai
and Ollama use case before expanding scope.

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

Collectors are small, read-only adapters that translate one provider response
into the normalized model in `docs/capacity-model.md`.

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

Type-checking gate: before committing or opening/updating a PR, run
`make check` and require it to pass with **0 type errors** (exit 0). The
gate is `basedpyright` — plain `pyright` is not an acceptable substitute —
running at its full default strict ruleset over `scarcity_router/` and
`tests/`; the `tools/` reconnaissance scripts are excluded from the gate
because they are not part of the product contract. Warnings are reported
but do not block the build (gated on errors only, see `failOnWarnings = false`
in `pyproject.toml`). Do not weaken the gate by overriding `typeCheckingMode`
or adding broad `reportXxx = none` suppressions to make it pass; fix the
underlying typing. `make typecheck` runs the type checker alone and
`make test` runs the unit tests alone.

## Decision discipline

Do not invent answers for unresolved issues. Add or update an entry in
`docs/decisions.md` with status `Proposed` or `Unresolved`, alternatives and the
evidence needed. Accepted choices may be changed only through an explicit new
decision that states what it supersedes.

Document copied or substantially adapted MIT-licensed code and preserve the
required notices. Architectural inspiration alone does not require runtime
coupling. Keep the project under Apache-2.0 unless an explicit licensing
decision supersedes it.
