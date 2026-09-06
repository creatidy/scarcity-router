# Roadmap

Milestones are outcome gates, not promises to build the entire list. Do not
start a later milestone merely because it is documented; first verify that the
earlier outcome is useful in the owner's workflow.

## Current status

**M0 PASS (2026-09-01).** The M0 exit criteria have been audited and passed.
**M1 PASS (2026-09-05).** The OpenAI/Codex and Z.ai subscription collectors
and the unified status surface are implemented — collection is read-only
except for the single bounded provider-managed auth-recovery exception of
D-018 — and live collection in the owner's supported environment returns
healthy normalized quota windows for both providers: bounded provider-managed
auth recovery (D-018) restored live OpenAI reads, and the D-019 remediation
guarantees that supplemental provider state never invalidates validated quota
facts. A fresh synchronous collection with one shared observation timestamp
backs every `status` call; no cache or stale threshold has been chosen. A
separate `doctor` command was not an M1 blocker because status exposes
normalized diagnostics; defer it unless normal use demonstrates a concrete
gap. Do not start M2 selection implementation before its planning gates are
met.

**M2 planning gate (2026-09-05).** The selector input contracts, semantic
capacity applicability, evidence/policy precedence, replenishment semantics,
the Artificial Analysis boundary, the bounded compound-recommendation
contract and the staged implementation sequence below are frozen in
documentation (D-020, D-021, D-022). Selector implementation must not begin
before the M2a capacity-applicability slice exists.

**M2a complete (2026-09-06).** The first M2 implementation slice is done:
capacity contract v3 (D-023) adds the normalized semantic
`CapacityWindow.scope_id`, both providers emit validated scope identities
(OpenAI: the validated `limitId` per snapshot/bucket; Z.ai: `coding_plan`
for evidenced limit types), `window_id` remains diagnostic-only, and no
core/consumer code infers scope from identifiers. Live structural
acceptance in the owner's environment returned `schema_version = 3` /
`ok` for both providers with per-window semantic scopes and distinct
additional OpenAI scopes (structural facts only; no personal values or
non-public bucket identifiers recorded). No model-to-scope bindings, catalog
types, ratings or selector code exist yet.

**M2b complete (2026-09-06).** The second M2 implementation slice is done:
the provider-independent task and model-catalog core contracts (D-024) live
in `scarcity_router/selection_types.py` — `TaskRequirement` (stored
three-part shape), `CapabilityMinima`, the nine-member typed
`HardConstraints` with validated `required_provider`/`required_model`
contradictions, `ModelIdentity`/`ModelRef`/`CapacityScopeRef`,
tri-state `ModelHardProperties`, provenance-bearing `CapabilityAssessment`
records (unknown = explicit `{"rating": null}`; known ratings require
complete evidence, confidence, date and rationale), preserved `HumanOverride`
records, `ModelCatalogEntry` with unknown/known `capacity_bindings`
semantics and the deterministic `ModelCatalog` container. No ratings, no
profile minima, no profile resolver, no catalog artifact and no selector
exist yet; populating the reviewed catalog is M2c.

**M2c complete (2026-09-06).** The third M2 implementation slice is done:
the first reviewable, provenance-bearing catalog content and calibrated
task profiles (D-025, resolving U-006) — exactly four routing identities
(`openai/gpt-5.6-luna/max`, `openai/gpt-5.6-sol/high`, `zai/glm-5.3/max`,
`zai/glm-5.3-flash/max`) in `model-catalog.json` with curated six-
dimensional ratings, evidenced tri-state hard properties, explicit
`openai/codex` and `zai/coding_plan` capacity bindings and complete rating
provenance (no `HumanOverride` for initial curation); the eight formal task
profiles each carry exactly one selector-facing `calibrated_requirement`
(`model-policy.json` policy_version 4, numeric minima now calibrated); the
pure profile-expansion mechanism (`TaskProfileDefinition` /
`TaskProfileCatalog.resolve`) lives beside the M2b types; calibration
acceptance tests pin the capability-only eligible sets; the rationale and
evidence are documented in `docs/model-calibration.md`. No scarcity, no
reservations, no selector and no catalog expansion exist yet; the next
implementation slice is M2d.

**M2d complete (2026-09-06).** The fourth M2 implementation slice is done:
the frozen scarcity and resource-policy primitives (D-026, resolving U-007)
live in the pure modules `scarcity_router/scarcity.py` and
`scarcity_router/policy.py` — the continuous integer penalty
`(100 - remaining_percent)^2` (scale 10000) with exact explanatory label
boundaries; candidate capacity applicability over explicit
`capacity_bindings` with most-restrictive multi-window/multi-scope
aggregation and unrelated scopes ignored; explicit `known`/`unknown`/
`unavailable` assessment states where explicit exhaustion wins and
non-`ok` telemetry stays unknown with no numeric penalty; unknown-capacity
modes `degraded`/`strict`; scope-targeted reservation rules (strict `<`
threshold, minimum task level) reflecting the shared `openai/codex` and
`zai/coding_plan` scopes; timezone-aware weekly blackout rules with
half-open `[start, end)` semantics, cross-midnight support and no
hard-coded vendor schedule; the normalized `ReplenishmentState` with
`ignore`/`advisory`/`recoverable` visibility that never changes current
scarcity and is never consumed; and the `UserPolicy` container with
deterministic canonical serialization. Scenario tests cover the 98/2
weekly-critical case, shared-scope equality, unrelated 0% buckets,
reservation boundaries, DST/cross-midnight blackouts and the exhausted-
but-recoverable distinction. No selector, no ranking and no catalog
changes exist yet; the next implementation slice is M2e.

**M2e complete (2026-09-06).** The fifth and final planned M2
implementation slice is done: the deterministic `balanced` selector,
explanation and simulation (D-027) live in the pure modules
`scarcity_router/selector.py` and `scarcity_router/simulation.py`, composed
by `selection_app.py` and the top-level CLI dispatcher (`status` preserved;
`select` with `--profile`/`--requirement`/`--tighten`/`--explain`/`--json`
and `simulate` with typed overrides). The frozen candidate pipeline runs
blackout → hard constraints → capability → scarcity/unknown-capacity
policy → replenishment visibility → applicable reservations; capability
uses `effective_rating` (HumanOverride-aware, no averaging); the exact
ranking order is known-capacity-before-degraded-unknown, scarcity penalty,
capability margin, explicit preference, stable identity; reservations are
scope-based and fail closed when unknown; replenishment never restores
eligibility; structured no-solution results carry closest candidates by
stage progress; simulation applies typed overrides to copies of the inputs
and re-runs the same selector core without mutating live observations.
Scenario, ranking, hard-constraint, tightening, no-solution, simulation and
CLI tests pin the behavior; D-027 records the frozen semantics.

**M2 PASS (2026-09-06).** M2 implementation slices M2a–M2e, live acceptance
and closeout are complete. Sanitized closeout evidence:

- 796 tests pass
- basedpyright 0 errors / 0 warnings / 0 notes
- live OpenAI normalized status structurally healthy
- live Z.ai normalized status structurally healthy
- `openai/codex` applicability observed
- `zai/coding_plan` applicability observed
- six representative real profile selections were capability-consistent
- explanations were reconstructable
- 98/2 scenario passed
- blackout scenario passed
- reservation block/permit boundary passed
- unknown/capability/HumanOverride contracts passed
- no model execution occurred

Owner closeout choices:

- OWNER TRUST GATE: PASS
- RESET VISIBILITY GATE: PASS FOR M2 — manual normalized replenishment
  visibility is sufficient for M2. Automatic live OpenAI reset-credit
  acquisition is deferred and is not a reason to reopen M2.

Next planned milestone: M3 — REST and MCP. Status: not started. M3 begins
only after an explicitly selected or created Forgejo issue/task, never
merely because it is documented here.

## M0 — Repository foundation

**Outcome:** A new contributor or agent can understand the product and begin M1
without the original project brief or chat history.

Deliverables:

- product scope and non-goals;
- component and dependency boundaries;
- normalized capacity concept;
- capability/task/profile concept;
- selector, scarcity and reservation policy concept;
- provider collector requirements;
- security invariants;
- confirmed/assumed/future PoC evidence;
- competitive context;
- decision log, licensing/hosting intent and implementation roadmap;
- authoritative agent instructions.

Exit criteria:

- all required documentation exists and links resolve;
- each topic has one primary source of truth;
- confirmed facts are separated from assumptions;
- uncertainties are recorded rather than invented;
- security invariants are prominent and consistent;
- no executable broker, fake endpoint or fake provider behavior exists.

## M1 — Capacity collectors and normalized status

**Outcome:** One command reliably shows current OpenAI and Z.ai subscription
capacity without issuing model requests.

Scope:

- freeze the first versioned normalized capacity schema;
- OpenAI/Codex collector using app-server;
- Z.ai Coding Plan collector using the existing configured credential;
- safe discovery and normalized diagnostics through `status` (a separate
  `doctor` command is deferred);
- provisional module `status` command with all windows, reset/freshness
  timestamps and explicit unknown states;
- redacted fixtures and contract tests for every adapter;
- endpoint, redaction and local-binding security tests where applicable.

Non-scope:

- intelligent model selection;
- capability ratings;
- REST/MCP/dashboard;
- model execution or automatic fallback;
- Claude support.

Exit criteria:

- the owner can replace routine dashboard checks with `status`;
- a weekly-critical/short-window-healthy state is represented correctly;
- an adapter schema failure does not break other providers or become guessed
  capacity;
- no secret appears in output, tests or logs;
- supported discovery and freshness behavior are documented.

### M1 implementation state and closeout

- [x] OpenAI/Codex production collector implemented and fixture-tested.
- [x] Z.ai Coding Plan production collector implemented and fixture-tested.
- [x] Unified `status` collection and human renderer implemented.
- [x] Deterministic normalized JSON status output implemented.
- [x] One shared observation timestamp passed to both collectors.
- [x] Provider operational failures remain status data and do not suppress
  other provider snapshots.
- [x] No model request is issued by status collection.
- [x] Bounded provider-managed OpenAI auth recovery implemented and
  fixture-tested (D-018): live OpenAI collection now returns real normalized
  quota windows with populated reset instants.
- [x] Both evidenced Codex response generations and evidence-based window
  coverage supported without synthesizing absent windows (D-019).
- [x] Supplemental-telemetry principle verified live: validated quota pairs
  survive credits, additional buckets and unavailable optional blocker
  signals, with explicit blockers still degrading honestly (D-019
  remediation).
- [x] The owner can replace routine dashboard checks with the provisional
  command in the real local workflow (live acceptance 2026-09-05), without
  recording secrets or personal quota values.

**M1 STATUS: PASS (2026-09-05)**

Sanitized closeout evidence:

```text
OpenAI live normalized subscription capacity observed: yes
Z.ai live normalized subscription capacity observed: yes
Provider-managed auth recovery exercised: yes
OpenAI quota windows with usable percentage pairs observed: yes
Multiple provider buckets coexist in one validated observation: yes
No missing window synthesized
No supplemental credit/account data exposed
No model prompt issued
No personal quota values recorded
```

### Follow-up health signals after the core M1 status surface

Provider status is a useful advisory input, but it must not delay or replace
direct account-capacity telemetry:

- OpenAI: evaluate the official `status.openai.com` machine-readable status and
  relevant Codex/CLI components as an advisory service-health signal. Direct
  account capacity remains separate.
- Z.ai/GLM: do **not** integrate `status.hellozai.com`; it belongs to the
  unrelated Zai Payments company. No official public Z.ai/GLM status page has
  been identified in current provider documentation. Prefer provider-native
  failure/high-traffic signals and record `unknown` when authoritative service
  health is unavailable.

## M2 — Capability catalog and selector

**Outcome:** `select --explain` produces trusted, deterministic recommendations
and simulations for the owner's actual models.

Scope:

- L0–L5 and data-driven profiles;
- hard constraints and multidimensional requirements;
- narrow, provenance-bearing catalog for Luna, Sol, GLM-5.3 and
  GLM-5.3-Flash;
- continuous scarcity and explanatory labels;
- reservation policies and only the needed user modes;
- ranked fallbacks, exclusions and structured explanations;
- typed simulation using the same selector;
- scenario and policy-boundary tests;
- explicit timezone-aware provider/model availability schedules and blackout
  windows. The initial personal policy must be able to exclude Z.ai during
  configured peak hours; provider peak/off-peak times must be configuration,
  not hard-coded assumptions, because provider-side definitions can change;
- replenishment metadata such as OpenAI banked Codex reset credits. A reset is
  an available recovery option, not already-restored quota: the broker may
  surface a candidate as recoverable under explicit policy, but never consume a
  reset or pretend current windows have already been refreshed;
- advisory provider health as a separate input from capability and quota;
- optional external capability/performance evidence from Artificial Analysis,
  cached and provenance-bearing. It may inform curated ratings but is neither
  live capacity nor an automatic selector truth source;
- bounded compound-workflow recommendations if/when the selector recommends
  more than one model call. Any `single`, cascade or critique-style plan must
  carry explicit limits for legs/reviews/remediation/retries/time and account
  for expected consumption across all legs. Scarcity Router recommends this
  envelope; it still does not execute the workflow.

Exit criteria:

- representative real scenarios choose a sufficient, least-scarce model;
- the 98%-short/2%-weekly case protects Z.ai;
- a configured Z.ai blackout excludes it deterministically and explains why;
- reservation permits L4/L5 while blocking unjustified lower-level use;
- capability deficits are never averaged away;
- unknown inputs are explicit and policy-controlled;
- OpenAI reset availability is visible without being silently consumed or
  treated as already-restored quota;
- external benchmark evidence has source/version/freshness provenance and can
  be overridden by curated local knowledge;
- any compound workflow recommendation is bounded by construction and never
  recommends an open-ended review/fix loop;
- rating provenance and human overrides are reviewable;
- the owner trusts and uses recommendations.

### M2 research/positioning gate resolutions (2026-09-05)

The M2 planning gate resolved the contract questions; remaining calibration
belongs to the implementation slices.

- HydraFusion reassessed as current prior art (now a GitHub Copilot CLI
  Research Preview): bounded workflows, independent critique, complete
  accounting, explicit escalation and no open-ended review/fix cycle are
  useful patterns, but the product boundary remains different
  (`docs/competitive-landscape.md`).
- The evidenced OpenAI app-server reset-credit representation
  (`availableCount` plus optional capped detail rows) is frozen as the
  replenishment contract: replenishment opportunities, never current capacity
  (D-021).
- The Artificial Analysis integration boundary is frozen — offline/periodic
  catalog evidence, never called during `select()` — while any actual cached
  integration remains an optional later addition (D-021).
- Precedence between direct runtime/account evidence, provider-native
  failure, advisory public status and user blackout policy is frozen (D-021).
- The service-level execution-budget contract for compound workflow
  recommendations is frozen (D-022).
- Threshold calibration — scarcity formula, labels, reservation boundaries
  and profile minima — remains open (U-006, U-007) and belongs to M2c/M2d.

### M2 implementation sequence

Implementation proceeds in small, deterministic slices:

- **M2a — normalized capacity applicability (complete 2026-09-06).**
  Implemented the semantic capacity-scope contract/version from D-020:
  capacity contract v3 with `CapacityWindow.scope_id` (D-023). No selector.
- **M2b — TaskRequirement and ModelCatalog core types (complete
  2026-09-06).** Implemented the provider-independent core contracts (D-024)
  in `scarcity_router/selection_types.py`: capability scale, task-requirement
  validation (profile expansion recorded as a construction pathway deferred
  to M2c), hard constraints, model-catalog schema/provenance/override types
  and capacity-binding fields against the v3 scope identities. Still no
  scarcity selector.
- **M2c — curated initial ratings and profile calibration (complete
  2026-09-06).** Populated only Luna, Sol, GLM-5.3 and GLM-5.3-Flash with
  explicit provenance in `model-catalog.json`; froze the eight profile
  minima as `calibrated_requirement` in `model-policy.json` and implemented
  the pure profile-expansion mechanism; scenario tests pin the
  capability-only eligible sets (resolves U-006, D-025).
- **M2d — scarcity and policy primitives (complete 2026-09-06).**
  Implemented the frozen scarcity parameters (D-026, resolving U-007):
  `scarcity_router/scarcity.py` (integer quadratic penalty, labels,
  binding applicability, most-restrictive aggregation,
  known/unknown/unavailable assessment states) and
  `scarcity_router/policy.py` (unknown-capacity modes, scope-targeted
  reservations, timezone-aware blackouts, `ReplenishmentState` with
  visibility modes, the `UserPolicy` container). No selector.
- **M2e — deterministic selector, explanation and simulation (complete
  2026-09-06).** Implemented the pure `balanced` selector, structured
  explanation and simulation over the same core (D-027): `select` with
  profile/explicit/tightened requirements, `--explain` and `--json`, and
  `simulate` with typed capacity/policy/replenishment/evaluated-at
  overrides that never mutate live observations.

M2 is closed as PASS (2026-09-06); see the closeout evidence above. Later
optional M2 additions — official health advisory, cached Artificial Analysis
evidence and compound-workflow recommendations, plus automatic live
reset-credit acquisition — remain accepted deferrals, not blockers or open
acceptance items.

## M3 — REST and MCP

**Status:** Not started. M3 begins only after an explicitly selected or
created Forgejo issue/task.

**Outcome:** External orchestrators can obtain the same status and decision as
the CLI through stable, minimal machine interfaces.

Scope:

- versioned REST status/provider/select/simulate contracts;
- `127.0.0.1` default binding;
- thin stdio MCP tools over the same application/core;
- parity and contract tests across CLI, REST and MCP;
- integration example proving explicit dispatch by an external orchestrator.

No MCP-specific selection logic and no prompt proxy. If compound workflow
recommendations exist by M3, REST/MCP expose the same bounded execution envelope
rather than inventing interface-specific orchestration behavior.

## M4 — Minimal dashboard and recipes

**Outcome:** Capacity and a representative selection are legible at a glance,
and common clients can integrate without maintained bespoke plugins.

Scope:

- tiny local dashboard using existing service data;
- screenshot-quality `status` and `select` presentation;
- recipes for Kilo, Claude Code, Codex and generic MCP/shell clients;
- no large frontend framework unless evidence justifies it.

## M5 — Claude collector evaluation and implementation

**Outcome:** Claude subscription capacity is supported only if a secure,
maintainable telemetry mechanism is validated.

First gate: document auth source, provider terms, endpoint stability, credential
exposure and maintenance cost. If the gate fails, record `unsupported` rather
than using browser scraping or delaying earlier value.

## Deferred possibilities

Runtime failure feedback, history/audit, team quota pools, fleet policy, central
dashboard, signed catalog releases and commercial curation are hypotheses. Add
them only after regular personal use validates the core. Provider-performance
histories and automatic replenishment actions remain deferred until selection
evidence shows they add value; the core broker must not absorb those systems by
default.
