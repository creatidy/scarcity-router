# Roadmap

Milestones are outcome gates, not promises to build the entire list. Do not
start a later milestone merely because it is documented; first verify that the
earlier outcome is useful in the owner's workflow.

## Current status

**M0 PASS (2026-09-01).** The M0 exit criteria have been audited and passed.
**M1 is current.** The OpenAI/Codex and Z.ai subscription collectors and the
unified status surface are implemented. Synchronous status now performs
a fresh collection with one shared observation timestamp, but no cache or stale
threshold has been chosen. M1 remains **NOT YET PASS** because live OpenAI
subscription-capacity telemetry must produce usable current quota windows in the
owner's supported environment before status can fully replace routine quota
checks. A separate `doctor` command is not currently an M1 blocker because
status exposes normalized diagnostics; defer it unless normal use demonstrates a
concrete gap. Do not start M2 selection implementation before M1 is useful in
the owner's workflow.

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
- [ ] Validate that the owner can replace routine dashboard checks with the
  provisional command in the real local workflow, without recording secrets or
  personal quota values.

**M1 STATUS: NOT YET PASS**

Remaining blocker:

- Live OpenAI collection returns real normalized windows, but the owner's
  current live response carries provider-supplied blocker evidence (a present
  valid credit state on the main quota, an additional limit bucket whose
  `spendControlReached` is not reported, and cross-bucket metering). Under
  the conservative contract preserved by D-019 the percentage pairs are
  honestly withheld, so status cannot yet replace routine OpenAI dashboard
  checks. Whether usable OpenAI percentage pairs can ever be represented
  under this contract — or whether the credit/bucket metering states need a
  v2 representation — is a product/contract decision for the human.

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

### M2 research/positioning gates

Before freezing selector thresholds and public positioning:

- reassess GitHub Project HydraFusion as current prior art. Its runtime
  `single`/cascade/critique orchestration, complete accounting, bounded
  execution, isolated review and validated routing are useful patterns, but the
  project boundary remains different: Scarcity Router is an external decision
  service centered on subscription scarcity, provider availability and user policy,
  not a prompt-executing Copilot runtime;
- validate the supported OpenAI app-server representation of banked reset
  credits, including count and expiry/details when exposed, without using
  private backend endpoints;
- validate the smallest useful Artificial Analysis integration. Use stable
  model/creator identifiers, server-side credentials and periodic cached
  refresh; do not call the data API once per selection;
- define precedence between direct runtime/account evidence, advisory provider
  status and user blackout policy;
- define a service-level execution-budget schema for compound workflow
  recommendations so the repository anti-loop rule is reflected in the
  product contract as well as agent governance.

## M3 — REST and MCP

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
