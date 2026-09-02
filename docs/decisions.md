# Decision log

This file records durable cross-cutting choices and unresolved decisions. Topic
details remain in their authoritative documents; entries here explain why a
direction was chosen. Dates use UTC.

## Accepted decisions

### D-001 — Recommend, do not proxy

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** The service returns a model recommendation, alternatives and an
  explanation. It does not receive prompts, proxy model traffic, execute work or
  automatically dispatch fallbacks.
- **Reason:** This directly solves quota allocation while sharply reducing
  security exposure and integration coupling.

### D-002 — Four independent inputs

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Capability, task requirement, runtime capacity and user policy
  remain separate domain concepts.
- **Reason:** Quota changes scarcity, not model quality; static mappings cannot
  represent changing subscription headroom.

### D-003 — Normalize at provider edges

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Small read-only provider adapters emit a shared capacity model.
  Provider parsing is forbidden in the selector and public interfaces.
- **Reason:** Provider drift is expected; it must degrade one adapter rather
  than destabilize the core.

### D-004 — Consider every relevant quota window

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Effective headroom uses the most constrained relevant validated
  window, never the most optimistic one. Unknown semantics remain explicit.
- **Reason:** A 98%-remaining five-hour window can coexist with only 2% weekly
  remaining.

### D-005 — Continuous scarcity with reservations

- **Status:** Accepted concept; parameters to validate in M2
- **Date:** 2026-09-01
- **Decision:** Use a continuous scarcity penalty, initially proposed as
  `(1-r)^2`, while presentation labels and reservation thresholds remain
  configurable. A reserved model remains eligible for sufficiently high-level
  work.
- **Reason:** Avoid discontinuous routing at arbitrary percentage boundaries and
  preserve premium capacity without declaring it unavailable.

### D-006 — Multidimensional capability, narrow catalog

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Capability uses domain dimensions plus hard properties; no
  global model tier. The initial catalog covers only the five real model
  families/configurations in scope and carries provenance/confidence.
- **Reason:** Suitability is domain-specific and ratings are curated judgments,
  not quota measurements or universal scientific rankings.

### D-007 — CLI, REST and MCP share one core

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** CLI is first; REST becomes the generic machine contract; MCP is
  a thin adapter, preferably stdio locally. Business logic remains in one core.
- **Reason:** Clients vary, while selection semantics must not.

### D-008 — Initial providers and roadmap order

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** v0.1 proves OpenAI/Codex, Z.ai Coding Plan and local Ollama.
  Claude is the next provider only after a security/maintenance evaluation.
- **Reason:** Solve the owner's workflow before pursuing provider breadth.

### D-009 — Security defaults

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Reuse existing local authentication read-only, never expose
  credentials, validate HTTPS and exact hosts before Authorization, and bind
  REST to `127.0.0.1` by default.
- **Reason:** Subscription credentials are the principal sensitive asset.

### D-010 — Licensing

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** License the project under Apache License 2.0. Preserve notices
  for any substantially adapted third-party MIT code.
- **Reason:** Permissive commercial integration plus an explicit patent grant.
  MPL-2.0 remains an alternative only after a deliberate policy change; AGPL is
  not the default.

### D-011 — Public hosting

- **Status:** Accepted for public release
- **Date:** 2026-09-01
- **Decision:** GitHub is canonical and the owner's Forgejo receives an
  automatic mirror.
- **Reason:** Public discovery, contributions, Issues, Discussions, Actions and
  security ecosystem should live on the canonical host.

### D-012 — Name remains provisional

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** `Scarcity Router`/`model-broker` is a working label only. Perform a
  GitHub, package registry, domain and general collision search before release.
- **Reason:** Implementation value precedes branding; the final name should be
  memorable and searchable rather than another generic router name.

### D-013 — Likely stack is guidance, not architecture

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision:** Python 3.12+, `uv`, `pytest`, typed models, a small CLI/HTTP layer
  and official MCP SDK are the likely stack. Each dependency still needs to
  justify itself at implementation time.
- **Reason:** The work is modest I/O, schemas and deterministic policy; a large
  framework chain would weaken the intended stable control point.

## Unresolved decisions

### U-001 — Codex binary discovery and compatibility

- Which installation sources and minimum versions are supported in M1?
- How is the selected binary made visible without exposing unrelated paths?
- Evidence needed: discovery experiments outside the tested VS Code extension.

### U-002 — Exact first serialized capacity contract

- **Status:** Resolved
- **Date:** 2026-09-02
- **Decision:** M1 adapters target the v1 internal serialized contract in
  `docs/capacity-model.md`. Every snapshot carries integer `schema_version: 1`,
  stable `provider` and `source` identifiers, optional safe `plan`, required
  UTC `retrieved_at`, one of the six frozen provider statuses, an unordered
  `windows` array, optional `local_runtime` facts and allowlisted diagnostic
  codes. Account identifiers, freshness/cache fields, raw provider responses and
  provider-specific orientation fields are excluded.
- **Window semantics:** A window has independent normalized `resource`
  (`tokens`, `time` or `unknown`) and `kind` (`five_hour`, `weekly` or
  `unknown`) values, optional positive `duration_seconds`, an optional validated
  percentage pair, optional canonical `resets_at` and an optional safe opaque
  provider `window_id`. Known period kinds carry fixed durations. Unknown
  windows survive normalization and array position has no meaning. A pair is
  either `(used_percent, remaining_percent)` with an exact complement or is
  omitted; an unvalidated provider orientation never becomes a guessed value.
- **Time and local facts:** `retrieved_at` and `resets_at` use UTC RFC 3339
  strings with exactly three fractional-second digits and `Z`. Ollama uses an
  empty quota-window array plus reachability, model-presence and independently
  validated configured/effective context fields; it never uses an unlimited or
  100% quota sentinel.
- **Boundary:** This is an internal M1 adapter/core contract, not REST, MCP or
  CLI versioning. `U-003` still owns age, refresh, caching and staleness policy;
  M2 still owns scarcity and selection.
- **Reason:** Two independent subscription adapters need the same provider-free
  semantics while Z.ai and Codex wire formats evolve independently. Omitting
  account and freshness fields keeps v1 minimal and avoids prematurely
  publishing security-sensitive or policy-shaped data.
- **Evidence and validation:** The current OpenAI/Codex and Z.ai observations,
  including known five-hour/weekly windows, unknown-window behavior, reset
  conversion and Z.ai's evidence-backed used-oriented percentage, informed the
  adapter boundary only. The normalized contract contains no Z.ai raw
  `percentage`, epoch-millisecond value, `unit`/`number` pair or Codex
  `primary`/`secondary` concept. The required OpenAI, Z.ai, schema-drift,
  authentication, Ollama and missing-versus-zero scenarios are model-checked in
  the contract document.
- **Rejected alternatives:** Keeping `freshness`, stale thresholds or cache
  lifetime in the snapshot would preempt `U-003`; using one raw provider
  percentage would leak orientation; storing independently supplied used and
  remaining values would allow contradictions; treating unknown windows as
  healthy or discarding them would lose evidence; account IDs and raw metadata
  were rejected for security and minimality; encoding local availability as a
  fake quota percentage was rejected as semantically false.

### U-003 — Refresh and staleness policy

- On-demand versus cached collection, timeouts and stale-use thresholds are not
  yet chosen.
- Evidence needed: observed collector latency/reliability and real workflow use.

### U-004 — Z.ai reset metadata and schema drift

- The exact raw reset-field mapping and behavior for unknown numeric units need
  redacted fixtures and tests.
- Default remains fail-safe unknown semantics.

### U-005 — Ollama inspection contract

- Select the supported local calls for health, model presence and effective
  configuration; distinguish configured from effective context.
- Evidence needed: a local PoC against the actual Qwen configuration.

### U-006 — Initial capability ratings and profile thresholds

- No exact model scores are accepted yet.
- Evidence needed: documented benchmark/experience sources, dated model
  versions, confidence and owner review during M2.

### U-007 — Scarcity parameters and policy boundaries

- Validate `(1-r)^2`, label thresholds, reservation boundary behavior and
  unknown ordering through scenario tests before M2 acceptance.
- Reset proximity is preserved but not included in the first formula.

### U-008 — Package, CLI and final project name

- Decide only after a collision search and before publishing an installable M1.

### U-009 — Provider terms and public supportability

- Confirm that public distribution of each collector is compatible with current
  provider terms and maintenance expectations before release.

## Superseding a decision

Add a new numbered entry with its status, date, evidence and `Supersedes: D-nnn`.
Do not rewrite history or change an accepted decision silently.
