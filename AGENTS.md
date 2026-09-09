# Instructions for repository agents

## Mission and priority

Build a small service that recommends which available subscription-backed AI
model to use now by combining task requirements, model capabilities, current
capacity and user policy. The governing rule is: **choose the least scarce
model that is capable enough for the task**.

The owner's real workflow takes priority over novelty, provider-count metrics,
market features and abstract platform design. Read the documentation map in
`README.md` and the relevant authoritative document before changing a topic.
If documents conflict, record the conflict and resolve it explicitly in
`docs/decisions.md`; do not choose silently.

## Repository workflow

Forgejo (`https://forgejo.creatidy.com/BioMedical-IT/scarcity-router`) is the
canonical repository for issues, pull requests, reviews, branches and
integration. GitHub (`https://github.com/creatidy/scarcity-router`) is an
automatic read-only mirror. Do not create or mutate GitHub issues, pull
requests, branches or tags unless Adrian explicitly requests a GitHub-specific
operation.

Use one selected issue, one feature branch and one Forgejo pull request. Branch
from `develop`, target `develop`, and leave the PR open for human review. Never
make a substantive task edit while checked out on `develop`; establish the
issue and feature branch first. `main` is human-controlled and is not the
ordinary agent integration branch.

Do not bypass an unmet dependency with a clean secondary worktree. Preserve
unrelated user work. Do not force-push, rewrite published history, merge
`develop` or `main` into a feature branch, create synchronization merge
commits, merge a PR, or promote to `main` without explicit authorization.

Before completion, verify that `origin/develop..HEAD` contains only the current
issue's change, there is no unintended merge commit, and touched files are in
scope.

## Product boundary

Scarcity Router recommends; it does not execute. Do not build prompt proxying,
model-call execution, repository ingestion, source-code inspection, autonomous
fallback execution, client-specific business logic or a generic LLM gateway.
CLI, REST, MCP and any dashboard must call the same authoritative core.

Local inference providers, including Ollama, are not supported. Restoring one
requires an explicit product decision. Keep intrinsic capability, task
requirements, runtime capacity and user policy separate; quota never changes a
capability rating.

## Security invariants

Credentials are transient input, never product output. Never print, log,
return, persist in fixtures, commit, copy unnecessarily, send to analytics,
expose to an agent or place in an exception an authentication token, cookie or
secret. Never ask an LLM to inspect credential values. Prefer existing
authenticated local tools or stores and read them as narrowly as possible.

Before attaching a credential to a request, require HTTPS and an exact
provider-host policy. Never send credentials to arbitrary endpoint overrides.
The REST listener binds to `127.0.0.1` by default; new network exposure,
credential storage or write access requires an explicit security decision.
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

## Bounded review lifecycle

Multi-model work is bounded by default. Before any worker or reviewer starts,
freeze the task scope and threat model, record an expected complexity boundary,
and declare an execution budget. The default lifecycle is:

`implementation -> independent review -> optional single remediation -> final verification -> human merge gate`

Allow at most one initial review, one remediation and one narrow final
verification, with worker and reviewer retries bounded independently at one and
the default wall-clock budget at 120 minutes. Serialize dependent phases and
freeze one immutable `reviewed_head`; workers must not push during review.
Every finding is `MERGE_BLOCKER` or `DEFER`; only a blocker can trigger the one
remediation. Final verification checks identified blockers and obvious
remediation regressions, not a new architecture review. Budget exhaustion,
stalled progress after the retry budget or a complexity breach stops work and
escalates to a human. Workers, reviewers and orchestrators never merge.

The detailed operating policy is
[`docs/llm-operating-policy.md`](docs/llm-operating-policy.md), and its
machine-readable companion is [`model-policy.json`](model-policy.json).

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
