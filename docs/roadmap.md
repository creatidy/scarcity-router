# Roadmap

Milestones are outcome gates, not promises to build the entire list. Do not
start a later milestone merely because it is documented; first verify that the
earlier outcome is useful in the owner's workflow.

## Current status

**M0 PASS (2026-09-01).** The M0 exit criteria have been audited and passed.
**M1 is current.** Executable M1 implementation may begin through explicitly
scoped issues. No M1 product code is included in this closeout.

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
capacity plus honest Ollama availability, without issuing model requests.

Scope:

- freeze the first versioned normalized capacity schema;
- OpenAI/Codex collector using app-server;
- Z.ai Coding Plan collector using the existing configured credential;
- Ollama local health/model-presence collector;
- safe discovery and `doctor` diagnostics;
- `status` CLI with all windows, reset/freshness and explicit unknown states;
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
- local status does not invent a quota percentage;
- supported discovery and freshness behavior are documented.

Implementation readiness questions to resolve first:

- supported Codex binary discovery/version policy;
- exact redacted Z.ai reset metadata mapping;
- supported Ollama health and effective-configuration calls;
- refresh/freshness defaults;
- minimal Python dependency set and package/CLI name.

## M2 — Capability catalog and selector

**Outcome:** `select --explain` produces trusted, deterministic recommendations
and simulations for the owner's actual models.

Scope:

- L0–L5 and data-driven profiles;
- hard constraints and multidimensional requirements;
- narrow, provenance-bearing catalog for Luna, Sol, GLM-5.3,
  GLM-5.3-Flash and local Qwen;
- continuous scarcity and explanatory labels;
- reservation policies and only the needed user modes;
- ranked fallbacks, exclusions and structured explanations;
- typed simulation using the same selector;
- scenario and policy-boundary tests.

Exit criteria:

- representative real scenarios choose a sufficient, least-scarce model;
- the 98%-short/2%-weekly case protects Z.ai;
- reservation permits L4/L5 while blocking unjustified lower-level use;
- capability deficits are never averaged away;
- unknown inputs are explicit and policy-controlled;
- rating provenance and human overrides are reviewable;
- the owner trusts and uses recommendations.

## M3 — REST and MCP

**Outcome:** External orchestrators can obtain the same status and decision as
the CLI through stable, minimal machine interfaces.

Scope:

- versioned REST status/provider/select/simulate contracts;
- `127.0.0.1` default binding;
- thin stdio MCP tools over the same application/core;
- parity and contract tests across CLI, REST and MCP;
- integration example proving explicit dispatch by an external orchestrator.

No MCP-specific selection logic and no prompt proxy.

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
them only after regular personal use validates the core.
