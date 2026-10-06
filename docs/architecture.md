# Architecture

## Context

Scarcity Router is a local decision service between capacity sources, curated
data and clients. In its default **recommendation-only mode** it does not sit
on the model request path:

```text
provider telemetry ────────> collectors ──> normalized capacity
model catalog ────────────────────────────> capability data
task/profile ─────────────────────────────> requirements
user policy ──────────────────────────────> reservations/preferences
                                             |
                                             v
                                          selector
                                             |
                              selected + fallbacks + explanation
                                             |
                                   CLI / REST / MCP / UI
```

The optional **execution gateway** (D-040, program map A0 / issues #85–#95)
adds a second mode: an authenticated server component exposes one
OpenAI-compatible endpoint so standard clients can have requests served from
the best available authorized resource — API providers, Ollama/local
inference, or the approved local Codex adapter — under the same
least-scarce-capable discipline. The gateway reuses this recommendation core;
it does not replace it. The gateway is additionally a **harness-independent
execution backend** (D-056): it is not a backend for any one client — ZCode
is the first demanding representative harness that exposed gaps in the
execution contract, and the intended client population also includes Kilo,
Cline, other coding-agent harnesses, OpenAI-compatible SDK clients, simple
scripts and Kernel-controlled harnesses. D-068 supersedes the earlier
reference to Router-owned agent/orchestration algorithms. The
full module map, contracts and security architecture of the gateway are in
[Execution-gateway architecture](#execution-gateway-architecture-a0-program)
below and in [`docs/security.md`](security.md); the harness responsibility
boundary and semantic compatibility contract are in
[Harness-independent execution backend](#harness-independent-execution-backend-d-056-146)
below.

## Creatidy System Alignment

### Authority and evidence

Source: **Creatidy shared system architecture**, v1.0, 2026-10-04,
`Creatidy_architektura_systemu_2026-10-04.md`, owner-supplied SHA-256
`4e64121599ac30896afb77574b2fd16cddfc3420afde37108611715c24b56e92`.
The full file was not found in workspace searches; this alignment uses the
owner's repository-specific brief covering source sections 1-5, 7-13 and
14-18. The hash is a supplied reference, not independently verified bytes.
The distinct products are Creatidy Kernel (`creatidy-kernel`) and Scarcity
Router (`scarcity-router`), alongside Model Intelligence and Console. Only
their actual canonical names are used; no alias, rename or extra product is
introduced.

The audit baseline is canonical `develop` at
`8d9d4b04bcb23fe19ff702b6209fbcb1537cdf1b`, fetched on 2026-10-04. It happens
to equal the source's historical Router checkpoint; neither is a deploy
claim. Issue/PR state alone is not implementation evidence. This document
uses four distinct statuses:

- **Agreed:** binding owner direction, recorded by D-068.
- **Verified:** a revision-specific code/test or attributed observation;
  revalidate before calling it current. Reading a test is not running it.
- **Proposed clarification:** a review recommendation with rationale;
  requires owner review and an explicit decision before it changes a contract.
- **To prove:** evidence or a decision is missing; the named contract/research
  issue must conclude feasibility, an evidenced adaptation or an owner decision.

The [G01-G13 matrix](roadmap.md#creatidy-requirement-coverage) links evidence,
document ownership and registered work. It is not a second task queue.

### Ownership and target flow

The shared mission is an open, local-first, observable system for individuals
and small teams, optimizing the path to an accepted result across money,
subscription quota, time, corrections, review and owner attention.

| Product / role | Owns | Does not own |
| --- | --- | --- |
| Kernel | Intent, TaskSpec/WorkUnit/Attempt, authority, workspace, harness lifecycle, durable state, verification, review/remediation and outcome evidence | Provider inventory, private quota telemetry or a copied Router ranker |
| Scarcity Router | Providers/accounts/sources/pools, private telemetry, channel compatibility, cost/selection policy, admission, gateway and authorized provider calls | Task intake, workspace/harness orchestration, task acceptance or a generic sandbox |
| Model Intelligence | External evidence about models/interfaces/benchmarks/public offers, provenance, validity, conflicts and versioned knowledge | Credentials, private remaining quota, local runtime authority or routing |
| Console | Cross-product views and forwarding authorized commands to their owner | A scheduler, ranking, authority database or direct access to another product's state database |
| Existing harness | Model-tool-result loop under Kernel's documented adapter control | Implicit expansion of task authority or hidden substitution within a pinned Attempt |

Agreed target, **not today's proven end-to-end integration**:

```text
Kernel -> harness adapter -> existing harness -> workspace
existing harness -> Router gateway -> authorized execution source
MI -> admitted versioned knowledge -> Router
Kernel / Router / MI -> owned state and events -> Console / CLI
```

Public products must not require private `creatidy-onprem`. Dependencies on
public modules are allowed; separation does not require four daemons,
Kubernetes, a common database or a message broker. Preserve the current
single composed execution server and optional worker. The local REST
recommender is not that execution server.

### Selection, identity and authority

Router chooses only within registered/client-authorized inventory intersected
with task capability minima, protocol/harness compatibility, data/cost
authority and sufficiently fresh source state. Quality minima cannot be
relaxed to save quota. No candidate is a correct explained result; manual
pinning is exceptional, not required normal UX. Kernel produces requirements,
not another available-model catalog. The nine current hard constraints and
structural channel requirements are not proof of complete rich TaskSpec
coverage (#175); generic unprofiled requests currently use L0/no invented
minima, and this audit does not change their shipped behavior.

The current public recommendation (`SelectionDecision`) and executable
route (`RouteDecision`) are different. Authenticated `/v1/select` still calls
the recommendation seam, not resource-aware routing. The pure exact-admission
mechanism and gateway `sr-pin:<resource_id>/<provider>/<model>/<variant>`
with optional `@<decision_id>` are reuse points, not proof of Kernel/harness
consumption (#174). A pin is not a credential, reservation or availability
guarantee; admission rechecks current state and authority without a substitute.
A new role or separately authorized Attempt may get a new decision. This
does not redefine all existing non-pinned clients.

`route_request` narrows a resource pin to its exact provider/model/variant
within that resource's currently bound identities (#178), never a sibling
variant. Identity drift or contradictory explicit model/variant constraints
produce no solution without substitution. The public gateway pin path uses
the unchanged exact `admit_pinned_target`; these paths are not conflated.
Gateway effort discovery/resolution and pinned dispatch use the catalog's
independent configured effort (#179/D-071), like D-057 recommendation output.
Opaque variants are never interpreted as effort; plan-managed lanes carry null.

Model, provider, resource, source/account/pool, access mode, effort, harness
and adapter version remain distinct. Variant is opaque; null, literal `none`,
unknown and unsupported are not synonyms. Requested/resolved/dispatched/
observed facts and their confidence are separate; dispatch audit is not
physical-model attestation. A plan-managed channel may pin its lane without
confirming the physical model, so it cannot satisfy a specific-model
requirement it cannot prove. Configured subscription entitlement alone is
not proof of observed billing or promotional eligibility (#179/#184).

Chat Completions is implemented; Responses and Anthropic Messages ingress
are not. Compatibility belongs to the actual harness/protocol/adapter/source
version combination with PASS/PARTIAL/UNKNOWN/UNSUPPORTED and dated evidence.
PARTIAL requires a documented mapping, not borrowed guarantees from a name.
Worker v3 enables bounded tool continuation; current v4 adds reasoning
preservation, with downlevel refusal where semantics cannot be retained
(#181/#185). Auxiliary/subagent model identity, cost and control remain
evidence-dependent, not presumed exposed by every harness (#180/#184).

### Tools and staged native-agent migration

Client shell/edit/read tools return to the harness; Router never executes
them. Backend-native tools are a separate declared capability/permission
domain. The integrated D-063 ZCode path deliberately launches a coding
harness in an administrator-authorized project under `--mode edit`. That is
a current-state deviation from the target inference/workspace ownership,
not proof the target is met. Do not silently remove it, switch to scratch
or extend it to Kernel's workspace. The owner's 2026-10-06 disposition (D-073,
#180) settles direction: Router is the inference/resource backend only; Kernel/
harness owns workspace, coding-agent lifecycle, tools and repository editing.
Retirement of the working legacy path still requires equivalent accepted
Kernel-controlled functionality and an explicit compatible migration. Kernel,
harness and host own workspace isolation; no second Router sandbox engine.

The administrator issues Kernel/harness clients a grant with
`"inference_only": true` through the existing client-key/configuration surface.
This narrows the same authorization intersection to `server_direct_http`:
Router invokes its HTTP inference adapter, never a native coding agent or a
project workspace. Both normal routing and exact pins refuse other channels as
`unauthorized_channel`; a pin or request metadata cannot override the grant.
Dispatch rechecks the live grant, and suspended native continuations use the
same current authorization check. This is a Router-side authority boundary,
not a claim that a remote provider never uses hosted tools, hidden helper calls
or billable work. Physical identity and usage remain evidence-dependent.

| Current path | Router-side authority | Inference-only grant | Remaining limitations |
| --- | --- | --- | --- |
| Server-direct HTTP | Model requests and client-tool transport; shell/edit/read tool calls return to the harness | Supported, subject to existing capability/authorization gates | Remote provider-side tools, helper identity/cost and billing are not attested by HTTP transport |
| Codex worker | Official app-server with per-attempt scratch, native sandbox controls and client dynamic-tool continuation | Refused: native operations are not proved disabled | Scratch writes and vendor-managed logs/config remain possible; no Kernel project grant or exclusive-editor lease |
| Legacy ZCode worker | Official CLI with fixed `--mode edit` in one administrator-authorized project | Refused, including tool-free prompts and exact pins | Workspace edits/native reads remain enabled; auth unverified, physical/helper identity and cost unknown |

Source evidence cut: Router baseline `1dae1948f372e0f1739896655bb7db97d6b08460`;
official ZCode tag v3.14.3 at
`29628c9acdb81b703bbd4080c207a0e7ce5e276e` (CLI bundle version 0.16.9),
`apps/zcode-cli/packages/cli/src/run.ts` and
`packages/core/src/permission/service.ts` (edit explicitly permits workspace
file edits; plan/build are not tool-free inference replacements); official Codex
schema tag rust-v0.155.1 at `be2951ea34f0d295ed0becf97079f92fa5f6950e`.
These are pinned structural evidence, not current installed/live acceptance.
The observed direct-review CLI 0.159.3 is a development review tool, not proof
of the product adapter's supported runtime or a ZCode installation.

| Negative/side-effect case | Router-phase evidence/boundary | Not proved |
| --- | --- | --- |
| Kernel client requests legacy lane by logical name or pin, or puts paths/authority in metadata | Administrator grant refuses before worker execution; request content cannot renew authority | Kernel consumer/installed replacement acceptance |
| Client shell/edit/read tools over supported HTTP | Returned as client function calls, never run by Router | Harness permission or tool-result correctness |
| Deleted/replaced/symlinked authorized project | Legacy adapter withdraws readiness/refuses; construction-time device/inode and canonical path rechecked after probes | Atomic check-to-spawn filesystem fencing, malicious host replacement during a run |
| Client/worker authority revoked | Existing key/session and continuation checks plus current pre-dispatch inference grant | Undoing previous edits or instantaneous cancellation of every in-flight call |
| Native helpers/hidden calls, exact model/billing demands | Keep existing unknown/unsupported facts and refusal; no inference auth probe or guessed zero cost | Physical/helper identity, provider settlement or total task budget |
| Concurrent editors/lifecycle | Inference-only client cannot dispatch the Router-owned coding lane; legacy worker retains bounded cancellation/single-instance behavior | A lease coordinating arbitrary external editors, Kernel replacement or legacy retirement |

An ordinary legacy grant (field omitted/false) and the existing source/workspace/
binary/service flags retain their behavior; no scratch substitution, yolo mode,
new workspace manager or external-editor mutex is introduced. If a project
directory is deliberately replaced, restarting the configured worker explicitly
renews its construction-time grant; it must not silently adopt a new inode.

### Knowledge, economics and observability

Agreed: consume admitted versioned MI knowledge and retain snapshot and
calibration provenance in decisions. Current local catalog and reviewed
track floors remain the working bootstrap/projection until #176 proves a
replacement; Router must not build another public-facts database/crawler.
MI #9 is a bounded proof, not an approved publication/consumer contract;
[MI #13](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/13)
now owns publication and #15 its production producer. Router #176 owns
consumption, with actual-used provenance retained by Kernel #55. These are
registered obligations, not implemented integration. Historical replay needs
a frozen evidence cut plus evaluation semantics, not just `knowledge(at)`.
Refresh failure alone does not cancel a running Attempt; new decisions obey
approved freshness, and promotions do not extend because refresh failed.
Exact admission/freshness/migration details remain To prove, not new payloads.

Subscription-first includes shared pools, reset windows, remaining capacity,
channel/plan/model/harness-dependent promotions and quota opportunity cost.
PAYG and local inference are supported resource classes, not the product's
definition. No API key or always-cheaper GPU is assumed. Token rate, request
limit and accepted-result budget differ: `SpendingLimit` is currently a rate
ceiling, not total spend. no PAYG is a hard access-mode authorization rule,
never a ranking preference or zero-price proxy. Unknown usage is not zero;
tokens do not automatically convert to quota, balance deltas do not attribute
one task amid other clients, local reservations do not reserve the provider,
and an uncontrollable output cap cannot guarantee cost. Reset/credit/paid
overflow actions need separate authority. #184 owns these source/call proofs;
Kernel aggregates the task path. #177 owns explicit local outcome consumption
and cautious, human-reviewed calibration, not central upload or automatic
learning from raw success percentages.

CLI status usability (#182) does not wait for Console. Snapshot/detail/JSON/
pipe/CI/unknown/no-color behavior must remain coherent. TUI/watch consumes
the same contract. Router-owned state/events (#183) must support freshness,
correlation, deduplication, reconnect and gap detection; traces are not a
settlement ledger. Meaningful-change notifications deduplicate. Reuse/link
the existing admin UI (#140); consumers neither read Router SQLite directly
nor bypass command authorization. #185/#186 own cross-product compatibility
and uncovered public artifact/platform gates, alongside #139/#141.

## Components

### Collectors

Small provider adapters acquire telemetry and translate it into the normalized
capacity model. Acquisition is read-only except for the single bounded
provider-managed OpenAI auth-recovery exception of `docs/decisions.md` D-018. They own provider discovery, subprocess/protocol
handling, response validation and provider-specific error mapping. They do not
rank models or interpret task difficulty.

The supported collector set is exactly OpenAI/Codex and Z.ai Coding Plan.
Adding a later provider requires separate scope and does not change the
selector's provider-independent boundary. The execution-gateway program adds
execution *adapters* (generic HTTP, Ollama, Codex) under the same
provider-edge discipline — see
[Execution-gateway architecture](#execution-gateway-architecture-a0-program)
and [`docs/providers.md`](providers.md); it does not add recommendation
collectors on its own.

Provider drift must stop at this boundary. A broken adapter yields an explicit
status such as `schema_changed` while the rest of the service remains healthy.

### Capacity store/view

Holds the most recent v3 normalized snapshots and their retrieval timestamps.
Freshness evaluation, refresh behavior and durable history are separate concerns;
the initial implementation may be in-memory and refreshed on demand. Secrets and
raw Authorization values never enter the normalized state.

### Catalog

Contains model identities, provider/account relationships, hard properties and
curated multidimensional capabilities. Profiles map stable task names to
requirements. Catalog data is versioned and human-reviewable; it does not read
runtime quota.

Catalog v2 (D-032) adds explicit normalized `reasoning_effort` per calibrated
invocation configuration. Model capability, reasoning intensity and subscription
scarcity are separate inputs: configurations do not inherit vectors from sibling
efforts or invent separate quota buckets. `ModelIdentity` remains the same
provider/model/opaque-variant triple. Selector ranking reads catalog effort
directly after scarcity and capability margin, before preference and identity;
no interface parses variant to infer effort. Null is unconfigured, distinct from
the real `"none"` effort setting. Machine-interface decision/envelope schemas
remain v1 unchanged.

### Policy

Contains default scarcity behavior, reservation rules, preference modes and
explicit overrides. Runtime policy is separate from repository governance and
from the catalog, so a user can change today's preference without changing
source repositories or capability claims. The pure resource-state and
preservation primitives live in `scarcity_router/scarcity.py` (continuous
scarcity penalty, explanatory labels, binding applicability, most-restrictive
aggregation, known/unknown/unavailable assessments) and
`scarcity_router/policy.py` (unknown-capacity modes, scope-targeted
reservations, timezone-aware blackouts, `ReplenishmentState` visibility modes,
the `UserPolicy` container); ranking modes remain the selector's concern.

### Selector

Consumes only normalized capacity, catalog data, task requirements and policy.
It has no credential access, no provider parsing and no client-specific code.
It produces a deterministic decision object with the selected model, ranked
alternatives, exclusions, input provenance and reasons.

The pure modules `scarcity_router/selector.py` (the `balanced` candidate
pipeline, the exact ranking order, `SelectorPolicy` and the monotone
`tighten_requirement` merge) and `scarcity_router/simulation.py` (typed
overrides applied to copies of the inputs, re-running the same `select_model`
core for the baseline and simulated decisions) provide the selection behavior.
The application layer
`scarcity_router/selection_app.py` loads artifacts strictly, resolves the
requirement, obtains one shared evaluation instant through
`collect_status` and renders deterministic output; `scarcity_router/config.py`
resolves and provisions the default user configuration
(`~/.config/scarcity-router`, D-036); `scarcity_router/cli.py`
dispatches `status`, `select`, `simulate` and `install-config`.

### Interfaces

- **CLI** is the primary local operational interface. The module dispatcher
  provides read-only `status`, `select` (with `--explain` and `--json`) and
  `simulate` through one application path; `doctor` provides implemented
  offline/configuration diagnostics (M09), not inference readiness proof.
- **REST** is the language-neutral machine contract. It binds to `127.0.0.1`
  by default and exposes exactly `/healthz`, `/v1/status`, `/v1/select` and
  `/v1/simulate`. `/v1/providers` and `/v1/providers/{provider}` remain
  deferred because `/v1/status` already returns the full snapshot set. The
  loopback-only standard-library adapter in `scarcity_router/server.py`
  (`python -m scarcity_router.server`, default port 8765, serialized
  requests, no runtime dependency) calls the same typed application seam as
  the CLI.
- **MCP** is a thin adapter over local stdio. It exposes
  `scarcity_status`, `scarcity_select` and `scarcity_simulate`, calls the
  application layer directly in-process and never requires the REST server.
  `scarcity_router/mcp.py` uses the official MCP SDK v2 low-level `Server` API,
  structured tool results and the client-owned stdio lifecycle. It shares the
  transport-neutral dependency record in `selection_app.py`, the logical
  parser/envelopes in `scarcity_router/machine_api.py` and the same typed
  application seam; it has no REST runtime dependency.
- **Dashboard** is a small operational view after core contracts exist, not a
  separate frontend product. In the execution-gateway program its realization
  is the M09 web UX served by the server component.

The authoritative machine-interface contract — envelopes, error semantics,
side-effect wording, security/lifecycle boundary, versioning and the
CLI/REST/MCP parity requirement — is [`docs/machine-interfaces.md`](machine-interfaces.md)
(D-028). No interface owns selection or collector business logic. The
OpenAI-compatible execution surface is a separately versioned contract
outside this v1 document (D-045; see
[Execution-gateway architecture](#execution-gateway-architecture-a0-program)).

## Dependency direction

Dependencies point inward:

```text
provider implementations ---> collector contract ---> normalized domain
CLI / REST / MCP -----------> application service ---> selector/domain
data files -----------------> catalog/policy loaders -> selector/domain
```

The domain must not import a provider, web framework, MCP SDK, CLI framework or
Kilo-specific module. Provider adapters may depend on small protocol helpers but
not on the selector.

The MCP SDK is isolated at the `scarcity_router/mcp.py` transport edge. The
shared `ApplicationDependencies` record carries only process-configured
artifact paths plus injectable collectors and clock; clients cannot supply
paths, endpoints or credentials. `machine_api.py` contains logical request
parsing and v1 envelopes, not selection or scarcity logic.

## Domain contracts

The current normalized capacity contract is v3, documented in
`docs/capacity-model.md`:

- `CapacitySnapshot`: schema version, provider/source identifiers, optional safe
  plan, retrieval time, status, windows and safe diagnostics. Account identifiers
  and freshness fields are not in the contract.
- `CapacityWindow`: validated resource and period kind, optional semantic
  capacity-scope identity (`scope_id`; `(provider, scope_id)` identifies one
  scope, `None`/omitted means unknown applicability, opaque exact-match only),
  optional duration, complementary used/remaining percentage pair, optional
  reset time and an allowlisted opaque provider window identifier. Both
  `scope_id` and `window_id` are never parsed by consumers; `window_id` is
  diagnostic only (D-020, D-023).
- `ModelProfile`: stable model identity, variants, provider, hard properties,
  capability assessments and provenance. `ModelCatalogEntry` plus
  `ModelIdentity`, `ModelHardProperties`, `EvidenceRef`, `HumanOverride` and
  the `CapabilityAssessment` vector in `scarcity_router/selection_types.py`
  represent these values: unknown capability is the explicit
  `{"rating": null}` state, a known rating requires complete provenance,
  overrides preserve the source assessment, and `capacity_bindings`
  distinguishes unknown applicability (`None`) from a known non-empty set of
  exact `(provider, scope_id)` references.
- `TaskRequirement`: task level, per-dimension capability minima on the frozen
  `1..5` scale with explicit unknown representation, typed hard constraints and
  single-place profile expansion. Its stored shape is the three-part
  (`task_level`, `capability_minima`, `hard_constraints`) form; profile
  expansion is a construction pathway, not a serialized fourth field.
- `SelectionDecision`: selected candidate, ranked alternatives, exclusions,
  reasons, capacity/catalog versions and degraded/unknown indicators.
  `scarcity_router/selector.py` also provides the per-candidate
  `CandidateEvaluation` record: one primary
  exclusion stage from the closed vocabulary (`policy_blackout`,
  `hard_constraint`, `capability`, `capacity`, `reservation`), structured
  hard/capability failures, normalized reason codes, deterministic
  closest-candidate and recoverable-candidate lists for no-solution
  results, and serialize-only `to_dict()` output. Each serialized candidate
  also carries `reasoning_effort` (D-057) — the configured effort of the
  same catalog entry that participated in ranking, with an explicit `null`
  for unconfigured entries; it is never derived from the opaque identity.
- `ScarcityAssessment`: the per-candidate scarcity result over explicit
  capacity bindings — continuous integer penalty, explanatory label,
  explicit `known`/`unknown`/`unavailable` state and normalized reason
  codes. `scarcity_router/scarcity.py` applies the most restrictive applicable
  window, ignores unrelated scopes, gives explicit exhaustion
  (`remaining_percent == 0`) precedence over incomplete telemetry, and assigns
  no numeric penalty to unknown capacity.

The domain also defines these supporting or forward-looking contracts:

- `CapacityScope`: `(provider, scope_id)` identity for one normalized capacity
  scope. The identity exists in the contract through
  `CapacityWindow.scope_id`, and `CapacityScopeRef` binds catalog entries to
  exact scopes.
- `ReplenishmentState`: minimal safe reset-credit facts — replenishment
  opportunities, never current capacity. `scarcity_router/policy.py` supports
  the `ignore`, `advisory` and `recoverable` visibility modes.
- `ExecutionBudget`: a finite bounded envelope for any future compound
  recommendation. It remains a conceptual product boundary; the service does
  not execute the recommended workflow.

Schemas must distinguish omitted, unknown, unsupported, unavailable and zero. The
capacity contract uses omitted optional fields for unknown values and explicit
`unknown` enum values for unresolved window semantics.

## Configuration boundaries

Separate configuration namespaces are expected:

- static catalog and profile data distributed with the project;
- user policy and temporary preferences;
- collector discovery/configuration without copied secrets;
- service settings such as refresh interval and local bind address.

The root `model-policy.json` currently carries both selector-facing
task-profile data and descriptive development/model-role metadata. Separating
those concerns before a public configuration API is a deferred design choice;
this cleanup does not split or redesign the artifact.

Do not place secrets in ordinary configuration. Do not make users edit
`AGENTS.md`, repository policy or the capability catalog for daily choices.

## Change isolation and reliability

Provider adapters are expected to change more frequently than the normalized
domain. Interface adapters may evolve independently. Public serialized
contracts require explicit versioning once released.

Partial failure is normal: one provider may be unknown while others remain
usable. The selector never fabricates telemetry to hide that failure and the
explanation must identify degraded inputs.

## Execution-gateway architecture (A0 program)

This section is the A0 architecture specification for the optional execution
gateway (issue #85; decisions D-040 through D-045). It defines module
boundaries, responsibilities and contracts precisely enough that each module
issue can proceed independently without inventing incompatible assumptions.
A0 itself implements nothing; every runtime behavior below lands in a module
issue.

### Program map

| Planning id | Forgejo issue | Scope |
| --- | --- | --- |
| A0 | #85 | Architecture, decisions, contracts (this section) |
| M01 | #86 | Resource registry, state, and collectors for executable resources |
| M02 | #87 | Routing core, policy, and client-requirement binding for executable targets |
| M03 | #88 | OpenAI-compatible gateway and execution coordinator |
| M04 | #89 | Generic OpenAI-compatible HTTP adapter and Ollama integration |
| M05 | #90 | Native worker, pairing, and execution transport |
| M06 | #91 | Codex adapter: CLI/App Server with Desktop and VS Code installations |
| M07 | #92 | ZCode adapter — Stage 2 implemented (D-061 reopen; D-063 amended 2026-09-29): official CLI executes coding work in an administrator-authorized project workspace under `--mode edit`; the plan-managed lane routes through the normal selector with no physical-model claim |
| M08 | #93 | MCP, REST, and CLI compatibility; optional remote mode |
| M09 | #94 | Configuration, web UX, and diagnostics |
| M10 | #95 | Distribution, installation, update, and end-to-end acceptance |

Dependency direction (blocked-by; cycle-free): M01→A0; M02→A0; M03→A0,M01,M02;
M04→A0,M03; M05→A0,M03; M06→A0,M05 (Stage 2 only; Stage 1 evidence is exempt);
M07→A0,M05 (Stage 2 only; Stage 1 evidence is exempt; Stage 2 reopened by
D-061); M08→A0; M09→A0,M03;
M10→A0,M01,M02,M03,M04,M05. The first useful execution vertical slice is
M01/M02 → M03/M04 with the minimum M09/M10 support each slice needs; a user
must be able to use real API/Ollama resources through one OpenAI-compatible
endpoint before all local CLI adapters are complete. M07 was an independent
research track that never blocked the program: Stage 1 produced
[`docs/zcode-adapter-stage1-evidence.md`](zcode-adapter-stage1-evidence.md),
Stage 2 was closed by owner decision (D-047) and reopened by D-061
(2026-09-28) after the official CLI verified the interface condition and the
re-evaluated terms gate passed under the recorded single-owner scope.

### Operating modes and deployment topologies

1. **Recommendation-only (default, unchanged).** CLI, loopback REST v1 and
   stdio MCP over the existing core; no server component, worker or Docker.
   This is what exists today and remains installable and supported exactly as
   is (D-040, guarded by M08/M10).
2. **Server-only execution.** The server component serves
   `/v1/models` + `/v1/chat/completions` (plus control API and UI) and
   executes through server-direct HTTP providers (including a
   network-accessible Ollama). No worker is required; API-only operation
   must work without one (M04).
3. **Server plus workers.** One server, one or more native workers on the
   user's other machines (LAN/VPN). Workers bridge localhost-only Ollama
   instances and local Codex entitlements to the server over outbound
   TLS/WSS; no inbound worker port, firewall rule or manual IP configuration
   (M05).
4. **Remote recommendation bridge (optional, M08).** An MCP/control client
   explicitly configured against the server instead of local operation:
   explicit configuration, explicit failure, never a silent local fallback.

Not provided: internet-facing execution without a separate explicit owner
decision, multi-tenant SaaS, or any topology that requires a deployed
database, cache or message-queue service (D-041).

### Module map and responsibility boundaries

The boundaries below are **logical modules inside one Python package, not
microservices** (D-041). One server component and one native worker component
exist at runtime; no module boundary introduces a new container or deployed
service.

| Module | Issue | Owns | Main inputs | Main outputs | Never does |
| --- | --- | --- | --- | --- | --- |
| Resource state, registry and collectors | M01 (#86) | Identity/health/freshness/cost/pool observation of every resource; versioned state snapshots; bounded polling/cache (resolves U-003 for the server state store) | Provider telemetry, worker state reports, admin resource configuration | Versioned resource-state snapshots; eligibility reports (D-039) | Rank, route, execute, or let telemetry change capability ratings |
| Routing core | M02 (#87) | Pure deterministic selection of an authorized executable target; policy; requirement binding; decision provenance | State snapshots, catalog/policy artifacts, request requirements, client identity/profile, admin constraints | Route decisions (with target, provenance, exclusions) | I/O, provider parsing, credential access, execution |
| Execution coordinator + gateway | M03 (#88) | Ingress request validation, admission, concurrency reservation, dispatch, streaming lifecycle, cancellation, usage/accounting, audit writing | Route decisions (or pinned targets), execution adapters, limits | Streams/responses to clients; usage and audit records | Routing decisions, provider parsing, executing client tools |
| HTTP execution adapters | M04 (#89) | One generic OpenAI-compatible HTTP adapter with evidence-based provider presets; Ollama (direct + worker-bridged transport) | Coordinator dispatch, admin-configured origins/credentials | Provider requests/responses, capability reports | Per-provider gateways; sourcing configuration from client requests |
| Native worker + transport | M05 (#90) | Outbound TLS/WSS connection, pairing identity, heartbeat, state reporting, local adapter invocation within allowlists, local diagnostics | Server worker-protocol messages, local resources | State reports, streams, usage reports | Routing decisions; generic shell/ssh; expanding its allowlist on request |
| Codex adapter | M06 (#91) | Codex execution through the official CLI/App Server mechanisms on Desktop/CLI/VS Code installations (worker-side or server-reachable) | Stage-1 evidence, D-039 eligibility, worker isolation | Compatibility matrix entries, execution | GUI automation; token extraction; adopting user projects/plugins |
| ZCode adapter | M07 (#92) | Implemented (D-061 reopen; D-063 amended 2026-09-29): one headless official-CLI run per dispatched call behind the D-053 source architecture — `zcode:<source_id>` instance, `zcode_subscription` kind (provider `zai`), explicit `--mode edit` (least-authority mode evidenced to edit workspace files), administrator-authorized project workspace (realpath-canonical, per-run re-validated, request-proof), honest unverified auth, plan-managed lane routed through the normal selector with no physical-model claim | Stage-2 evidence, vendor terms (D-061), worker isolation, D-063 design record (amended), `model-tracks.json` zai/plan artifact | Official CLI subprocess only; explicit safe mode; single-owner scope | Third-party or multi-user exposure; credential extraction; direct coding-endpoint calls; quota pooling; model-identity claims |
| Interface compatibility | M08 (#93) | Frozen v1 guardrail suite; parity; optional remote bridge | Every module's changes | Green parity suite | Semantic drift in frozen surfaces |
| Configuration, web UX, diagnostics | M09 (#94) | Admin onboarding, provider configuration, pairing UI, client keys, routing profiles, diagnostics/doctor | Admin actions, server state | Control API + UI; copyable client configuration | A second selector; exporting secrets; author-private defaults |
| Distribution and acceptance | M10 (#95) | Server container, worker packaging, update path, E2E and security acceptance | All modules | Installable artifacts; honest acceptance matrix | Fictional artifacts; depending on private infrastructure |

### Responsibility-separation invariants

- **Resource State observes; it never decides.** Registry/state/collectors
  answer what exists, whether it is reachable, how fresh the observation is,
  what it costs and which quota pools apply. Quota never changes a capability
  rating (D-002), and telemetry never reorders ranking.
- **The Routing Core decides; it never touches the world.** It consumes only
  normalized state, catalog/policy artifacts, requirements, identity and
  constraints, and produces a deterministic route decision with provenance. It
  performs no network, subprocess or credential operation — the existing
  import-cleanliness of `selector.py`/`scarcity.py`/`policy.py` extends to
  every execution-era input.
- **Execution coordinates; it never chooses and never parses.** The
  coordinator runs a request's lifecycle against a decision the core produced
  (or a pinned target), enforces limits, dispatches through adapters, streams,
  cancels and accounts. Provider wire formats stop at adapter edges (D-003);
  the coordinator never parses provider payloads and never re-ranks
  candidates.
- **Adapters translate; they never policy.** Execution adapters own provider
  differences exactly like collectors do: validate schema, map errors, report
  capabilities honestly (compatibility matrix), fail closed on drift.

### Server and native-worker responsibilities

**Server component** (one process; may be containerized per M10):

- terminates all public surfaces: the OpenAI-compatible execution surface,
  the authenticated control API, the lightweight web UI (M09) and the worker
  protocol endpoint;
- holds server-side credentials under the D-044 storage decision
  (administrator-configured provider endpoints/keys, client API keys, worker
  identities);
- aggregates resource state (its own collectors plus worker reports through
  one shared normalization — never two collector implementations for the same
  provider/account, M01/M05), runs the routing core, coordinates execution,
  enforces limits and writes the bounded audit trail.

**Native worker** (one package per platform; installed only where needed):

- originates one outbound TLS/WSS connection to the configured server URL;
  no inbound listening port for normal operation;
- holds its per-device pairing credential and local adapter allowlist; local
  application credentials stay on the worker host whenever possible (Codex
  auth remains provider-managed, D-018 unchanged);
- reports safe normalized resource state, executes dispatched requests
  through allowlisted local adapters (localhost Ollama, Codex),
  streams results and usage, and enforces its allowlist even if the server
  requests more (M05);
- performs no routing decisions.

### Contract map

Every contract below is versioned, additive-first and owned by exactly one
module; the field-level serialized shapes are defined by the owning module
issue under these frozen semantic rules. Contracts never share versions
across families (D-045).

**Request contract (execution ingress — M03).** `GET /v1/models` and
`POST /v1/chat/completions` with SSE streaming, authenticated as an inference
client (D-044). Requests are validated for capability before any inference:
a request feature (tools, structured output, reasoning controls, context
size) is a compatibility requirement; unsupported capabilities are rejected
before inference or routed only to a backend that actually supports and is
authorized for them. The `model` field carries either an
administrator-defined routing-profile alias (which resolves to the existing
task/profile requirement model — never a second scoring system, D-042) or a
pinned executable-target reference. The Responses API is a later explicit
sub-scope with a documented supported subset; a fake `/v1/responses` that
silently drops semantics is forbidden.

**Route-decision contract (M02, semantics frozen by D-042).** A gateway-era
decision separates: the physical model/variant (`ModelIdentity`); the
execution channel/surface (server-direct HTTP adapter, worker-bridged
adapter, local CLI/app adapter); the entitlement in use
(subscription-included, promotional, PAYG metered, prepaid credits,
local/ungated); the confirmed quota pool(s) the entitlement draws from; and
the client routing profile under which the decision was made. The decision
carries `decision_id`, provenance, alternatives and exclusions exactly like
the existing `SelectionDecision` discipline, and its serialized extension of
selection output flows only under the machine-interface additive rules
(D-028) on the control side and under execution-surface versioning (D-045)
on the gateway side. Implemented by #87 as
`scarcity_router/routing_core.py` (`schema_version = 1`): the pure
`route_request` core layers requirement binding, authorization and
execution-surface gates on top of the unmodified balanced selector, and
`admit_pinned_target` performs the recommendation-to-execution admission
without re-ranking. The pinned executable-target reference is EXACT:
`PinnedTarget` carries the selected target's `resource_id` plus its exact
`ModelIdentity` (provider, model, variant), converted from the decision's
selected target with `PinnedTarget.from_route_target` (or
`from_route_target_dict` over the serialized decision), so no target
dimension is ever reconstructed from outside the decision. Admission
verifies the pinned identity is one of the identities the named resource
currently binds and approves exactly that resource and variant; a
no-longer-bound identity is an explicit typed rejection
(`pinned_model_not_bound`), never a substitution of another bound variant
or a canonically-first fallback.

**Resource-state contract (M01).** A new versioned contract family,
sibling to capacity v3 (which is preserved; extensions only through explicit
versioning per the D-023/D-028-compatible discipline; implemented by #86 as
`scarcity_router/resource_state.py`, `schema_version = 1`), split into
three explicit records:

- `ResourceStateSnapshot` is a **pure observation** of one executable
  resource: identity, `observed_at`, health (the capacity v3 status
  vocabulary plus its diagnostics), every quota fact as an unchanged
  `CapacityWindow` paired with its observation class
  (`direct_observation`, `provider_telemetry`, `estimate`, `local_limit`,
  `unknown` — see [`docs/capacity-model.md`](capacity-model.md)), and
  promotions as separate observations (source, observation time,
  execution-channel scope, model scope, plan scope, validity period,
  timezone). It deliberately carries no freshness/polling policy, no
  capability fields and no cost fields.
- `ResourceRegistration` is the **administrator-owned canonical
  configuration and policy**: the freshness TTL, the polling cadence, the
  configured execution-capability facts and the configured cost facts. It
  is authoritative on the server; observations — including worker reports
  — can neither redefine it nor silently override it.
- `ResourceRegistryEntry` is the **evaluated, self-contained server read
  model**: the authoritative registration state combined with the latest
  observation and the derived freshness/refresh-due state (the U-003
  resolution scope, evaluated against an explicit injectable instant with
  future-dated observations failing closed).

Across the three records, M01 as a whole owns identity, health,
freshness, cost, quota-pool and bounded polling state for every executable
resource — the ownership is split across the family, not duplicated into
the observation document. No record of this family ever contains secrets,
account identifiers or raw provider payloads.

**Execution contract (M03, semantics frozen by D-043).** Admission →
bounded concurrency reservation → dispatch → stream → completion/cancellation
→ usage accounting. Frozen rules: cancellation propagates to the selected
backend where supported; the selected target is never silently replaced,
before or after a response stream has started — a dispatch failure fails
closed with an explicit error and automatic cross-target failover does not
exist (the prompt-destination invariant of [`docs/security.md`](security.md)
holds from admission, not from the first stream byte); retries after
ambiguous execution state must not blindly duplicate inference consumption
(exactly-once is not promised);
one external request may cause multiple internal provider calls and
usage/accounting represents this honestly; client-supplied tools return to
the client as `tool_calls` and the router never executes them locally; an
execution mode with undefined or unbounded start time never silently
replaces a synchronous HTTP request.

**Worker protocol contract (M05, semantics frozen by D-043/D-044).** One
versioned message protocol over a single outbound TLS/WSS connection;
handshake performs protocol-version negotiation (incompatible versions fail
safely), per-device authentication and heartbeat; message classes cover
state reports, execute, stream chunks, cancellation and usage; reconnect is
bounded with backoff and network loss never automatically duplicates an
already-started request. No generic shell/ssh/arbitrary-command message
exists in the protocol vocabulary. Implemented as worker protocol v1 —
transport, framing, message vocabulary, pairing and reconnect semantics are
specified in [`docs/worker-protocol.md`](worker-protocol.md).

**Entitlement and quota-pool model (M01/M02, frozen by D-042).** The same
model name never implies the same resource, entitlement, quota pool, cost
model or promotional eligibility. Resources sharing one confirmed quota
pool are not independent capacity; unconfirmed sharing is never assumed in
either direction (one subscription discovered through Desktop, CLI, Windows
and WSL is not four pools, nor is pool sharing presumed without
verification). A promotion-based routing preference is distinct from proof
that an execution qualifies (D-039 remains the proof path). The router never
assumes it observes account usage happening outside it.

**OpenAI compatibility matrix (M03 with M04/M06 evidence).** Keyed by
(adapter, adapter version, model/backend) across: roles and conversation
history, streaming, `tool_calls`, tool results, structured output, reasoning
controls, context limits, error semantics, usage reporting, cancellation.
Cell values are `PASS`, `PARTIAL`, `UNSUPPORTED` or `UNKNOWN`, each with
dated evidence and tested version. Admission requires the needed
capabilities to be `PASS` or `PARTIAL` with a documented semantic mapping;
`UNKNOWN` and `UNSUPPORTED` fail closed. An agentic CLI backend is not
automatically an OpenAI-compatible backend; concatenating messages into a
text prompt is not sufficient compatibility.

**Audit-metadata contract (frozen by D-043).** Per executed request: request
id, decision id, client/profile identity, routing-policy version, state
snapshot identity/version, selected target, actually-executed target,
adapter version, start/end time, result status, provider-reported usage,
estimated usage where applicable. No prompt or response contents in the
default audit trail; bounded retention; redacted diagnostics everywhere.

### Authorization precedence

Frozen by D-042 and restated here as the routing-core's evaluation order:

```text
administrator constraints
  > client authorization
    > request requirements
      > configured routing profile
        > explicitly selected target/model
          > optimization preferences
```

Each layer may only narrow the space allowed by stronger layers. A client
override may narrow permissions and must never expand authorization,
provider access or spending limits. An explicitly requested target is pinned
and never silently replaced: if it violates a stronger layer (unauthorized,
incompatible, blocked), the request fails with an explicit error rather than
being re-routed. Administrator-configured identities, profiles/aliases and
limits are never sourced from client request content.

### Recommendation-to-execution binding

The internal `RouteDecision` can carry an executable-target reference and
decision id. Current public `select` (MCP, REST, CLI and authenticated control)
returns `SelectionDecision`, which lacks a resource binding: do not claim
it already supplies an executable pin. #174 owns the missing public
producer-consumer proof. Given a legitimate exact target reference, the
client may pin it in an execution request with decision-id audit provenance.
The gateway then performs **admission only** —
authorization, limits, availability, compatibility — and dispatches; it does
not re-run competitive ranking, so no unexpected second routing decision
occurs. Frozen alongside (D-042): a recommendation is not automatically a
reservation, a capacity guarantee or an execution guarantee; nothing is
reserved between select and execution, and admission may still reject the
pinned target explicitly.

### Durable state

Per D-041: the server keeps one embedded durable store (SQLite-class
single-file store in its data directory) for configuration state,
identities, usage accounting and the bounded audit trail; its schema is
server-internal, migrations are explicit and tested, and no external
database, cache or message-queue service is introduced. Slices that need no
durable state may run in memory. The worker keeps only its identity file and
local configuration. Recommendation-only mode keeps today's zero-durable-
state behavior.

### Coexistence with frozen interfaces

Machine-interface v1 (loopback REST `/healthz`, `/v1/status`, `/v1/select`,
`/v1/simulate`; stdio MCP tools; CLI) is preserved with frozen semantics and
boundaries. The OpenAI-compatible execution surface is a separately
versioned contract whose `/v1/` prefix is the OpenAI client convention, not
machine-interface v1; path sets are disjoint so one listener may serve both
in server deployments. The loopback REST adapter is never converted into the
execution server (D-044/D-045). Versioning rules, error-vocabulary
separation and the extended parity requirement are
[`docs/machine-interfaces.md`](machine-interfaces.md).

### Remote bridge client (M08, #93)

The remote recommendation bridge (topology 4 above) is a configured CLIENT
mode. M08 ships the client, `scarcity_router/remote.py`; the server-side
authenticated control API it calls is owned by M03/M09 and did not exist
when the client landed. The smallest interface-side expectation is
therefore frozen in the client module and exercised against a synthetic
in-process server in the M08 guardrail suite (`make guardrails`):

- **Endpoints:** the machine-interface v1 logical contract on the
  configured server origin — `GET /v1/status`, `POST /v1/select`,
  `POST /v1/simulate`. No `/healthz`: liveness is a local-surface concern.
- **Envelopes:** exactly the machine-interface v1 envelopes (outer
  `schema_version` integer `1` plus `snapshots` / `decision` / `result`),
  so equivalent state and policy produce responses semantically equal to
  the local surfaces (the D-045 parity rule). Responses are parsed with
  the shared strict JSON parser.
- **Authentication:** `Authorization: Bearer <client API key>` — the D-044
  client identity class. The key is transient client input: header-only,
  never in a URL, never logged, redacted from the configuration repr.
- **Transport:** verified TLS for every non-loopback origin; no
  verification bypass option exists. Plain HTTP is accepted only for
  explicit loopback origins (the bounded D-044 exception). Redirects are
  never followed.
- **Failure:** every transport, authentication, status, envelope or
  validation failure raises the typed `RemoteBridgeError`. The client
  holds no collectors, artifacts or policy, so a silent fallback to local
  state or local policy is structurally impossible. Structural request
  violations are rejected client-side through the same shared
  `machine_api` parsers the frozen adapters use; catalog-dependent
  validation (for example an unknown profile id) belongs to the remote
  server and surfaces as an explicit error.

If M03/M09 land different control-API paths or envelope versioning, the
reconciliation is confined to `scarcity_router/remote.py` (endpoint
constants plus envelope validation) and the synthetic-server tests.

### Execution sources, model inventory and dynamic resources (D-053, #116)

The execution-source layer sits between administrator configuration and the
existing routing core; it changes nothing about how a decision is made, only
where executable resources come from:

```text
ExecutionSource (administrator configuration, per-source identity)
      |  worker-run discovery on the source's own controlled runtime
      v
ModelInventory (typed, bounded, versioned worker-protocol state)
      |  classification against the reviewed track registry
      v
adoption policy (DISCOVERED -> CLASSIFIED -> ROUTABLE, conservative floors)
      v
derived exact resources (deterministic per-source materialization)
      v
existing Routing Core (unchanged) -> exact pinned execution target
```

Contracts:

- **ExecutionSource.** One configured, independently authenticated source of
  executable model capacity — `source_id`, `kind` (`codex_subscription`
  first), `label`, owning `worker_id`, `entitlement`, optional explicit
  `quota_pool_id`, adoption policy. Lives in the additive `sources` domain of
  the administrator configuration document (config schema version 2; a v1
  document remains valid). The user configures the source; the source
  discovers models; physical models remain the exact execution targets.
- **ModelInventory.** The worker-protocol v2 state report carries an
  optional bounded `inventories` section: per source — `source_id`,
  `adapter_id` (`codex:<source_id>`), `observed_at`, closed-vocabulary auth
  state, runtime name/version, and per-model slug plus runtime-reported
  reasoning efforts. Bounded entries, no credentials, no account metadata,
  no raw provider payloads. A v1 peer pair behaves exactly as before.
- **Adapter instances.** Adapter KIND (`codex`) is separate from adapter
  INSTANCE (`codex:<source_id>`); one worker process serves any number of
  sources, each with an isolated controlled CODEX home
  (`state_dir/codex-sources/<source_id>/`), auth state, inventory and
  execution identity. No credential or home is shared between sources.
- **Tracks and adoption.** `model-tracks.json` (reviewed, versioned) maps
  slug structure to stable capability families (`openai/luna`,
  `openai/sol`, `openai/astra`, restricted `daybreak`). A discovered model
  becomes routable only through the conservative floor policy: known track,
  safely parsed naming, approved track capability floor, runtime-advertised
  effort, source/auth/health gates, compatibility evidence. Unclassified
  models stay discovered/not-routable; restricted tracks (Daybreak Blue) are
  never materialized as routable resources.
- **Derived resources.** Deterministic per-source materialization
  (`resource_id = <source_id>:<slug>`), exact slug and efforts,
  source-owned lifecycle: absence from an authenticated inventory →
  `unavailable`; absent for three consecutive authenticated inventories →
  retired (audit history and pins remain interpretable; reappearance
  re-materializes). Derived registrations are in-memory derived state — the
  source, not a second durable store, is their origin. D-049's ownership
  clause is amended at source granularity: the administrator grants
  ownership by configuring the source; inventory expands what that grant
  covers, subject to adoption policy.

### Harness-independent execution backend (D-056, #146)

This section records the accepted architecture that generalizes program
#132's representative-client work beyond any single harness. It is
architecture, not implementation status; the current/deferred split is at
the end of the section.

#### Responsibility boundary

| Layer | Owns | Never does |
| --- | --- | --- |
| HARNESS (ZCode, Kilo, Cline, scripts, first-party algorithms) | user interaction; conversation UX; the workspace; tools and tool permissions; local shell/editor/browser/file actions; presentation of intermediate actions; confirmation policy | logical model resolution, resource selection or capability admission (the router's ownership); model execution (the backend's ownership) |
| SCARCITY ROUTER | logical model resolution; resource selection; scarcity/capacity policy; authorization; exact model and reasoning-effort preservation; execution-source capability admission; transport of model semantics; evidence; usage accounting; audit of selected vs executed target; typed failure semantics | execute a client-owned tool; own the conversation, workspace or confirmation policy |
| EXECUTION SOURCE / BACKEND | actual model execution; only the backend capabilities it explicitly evidences | redefine capability, eligibility or policy from runtime listing |

A client-owned tool never becomes a worker-owned tool merely because the
backend has its own internal tool system; backend-native tools are a
different capability domain and are never presented to a client as if they
were client tool calls.

#### Semantic execution model and protocol adapters

Two distinct relations must not be conflated:

- **One selection/routing core.** Every surface — the frozen
  recommendation interfaces (CLI, loopback REST, MCP), the authenticated
  machine/control surfaces and the execution gateway — consumes the same
  selection/routing core (the D-007/D-028 parity rule extended to the
  execution era). Recommendation and machine/control surfaces are
  consumers of that core; they are NOT adapters of the semantic chat/tool
  execution contract and own no selection policy of their own.
- **Execution-protocol adapters around the semantic execution model.**
  The OpenAI Chat Completions surface (implemented, execution surface v1,
  [`docs/execution-surface.md`](execution-surface.md)), a future explicit
  OpenAI Responses surface (not implemented, never faked through Chat
  Completions per D-043; its later addition must reuse the same
  resolution/selection/admission core without duplication), and internal
  Kernel/harness execution consumers are adapters around the internal
  semantic execution contract; none duplicates routing or selection
  policy into its own HTTP API.

The contract a harness may rely on is semantic — model discovery, logical
model selection, reasoning controls, role history, streaming, client-owned
tool declarations, tool calls returned to the client, tool-result
continuation, structured output, max output semantics, context capability,
cancellation, usage, typed errors, authentication and transport security —
never a list of client-specific payload quirks. A client's harmless
additional syntax may be normalized at a protocol edge only when its
semantics are unambiguous and bounded (the #135 reasoning-dialect
precedent); the routing core never branches on client identity
(`if zcode`/`if kilo`/`if cline` behavior is forbidden).

#### Model and execution identity

A request for a `(model, reasoning_effort)` pair may select among multiple
eligible resources providing exactly that logical provider/model/effort
identity (D-055); it may never silently substitute another model, family,
effort, restricted model, or a resource that cannot satisfy the request
semantics. D-054 max-only semantics are untouched. `sr-pin:` remains the
explicit exact-resource escape hatch; logical model ids are the normal
harness path.

#### Effective capability

Four distinct capability layers feed admission:

1. logical model hard capabilities (catalog, provenance-bearing);
2. execution-source/channel capabilities — what the route evidences it can
   carry (context ceiling, output ceiling, tool round trip, streaming,
   cancellation);
3. administrator policy/limits — authoritative ceilings that only narrow;
4. client-request requirements — which may only narrow, never expand
   (D-042).

```text
effective capability = model capability
                     ∩ execution-channel capability
                     ∩ administrator allowance
```

A powerful model reached through a weaker execution channel exposes the
weaker effective capability for that route. UNKNOWN stays UNKNOWN: a
model-catalog maximum is never evidence that every execution source provides
it, and an unknown input yields an unknown effective value — `null` in
metadata, fail-closed in admission — never a guessed number. A request
routes only to a source whose evidenced capability satisfies the full
semantic request. #136 implements the limits dimensions of this
intersection and also settles how multiple routes with differing per-route
effective ceilings aggregate into one advertised model-level number; this
section fixes only the per-route rule.

#### Client-owned tool lifecycle

```text
harness advertises tools
  -> Scarcity Router transports tool definitions
  -> backend/model requests a client tool
  -> Scarcity Router returns a tool_call to the harness
  -> harness executes it under the HARNESS permission model
  -> harness sends the tool result
  -> Scarcity Router transports the continuation
  -> backend/model continues
```

Scarcity Router and its worker never execute a client-owned tool. An
execution source that cannot implement this lifecycle is ineligible for
tool-requiring requests — a limitation of that source recorded in the
compatibility matrix (admission fails closed), never a limitation of the
architecture. #137 investigates the Codex source under exactly this rule.

#### Transport tiers

Plain HTTP is a SUPPORTED LOCAL TRANSPORT, not an insecure-debug escape
hatch; HTTPS/TLS remains required for traffic crossing a host trust
boundary. The authoritative tier policy — loopback (supported, including
host-network containers), same-host container transport under deployment
isolation (architecturally permitted, implemented under #139, a property
of the entire composed HTTP listener rather than a per-surface mix, never
a global non-loopback plaintext listener), cross-host/LAN/VPN/remote (TLS
required), remote TLS UX (public CA normal, private CA advanced), and the
unchanged worker transport — is [`docs/security.md`](security.md) (D-056).

#### Representative-harness acceptance

Tier 1: protocol-level generic clients — deterministic OpenAI-compatible
request fixtures (#133) and SDK-level smoke tests where practical. Tier 2:
one representative coding harness — ZCode, the primary demanding fixture
because a real request capture exists. Tier 3: broader harness evidence —
Kilo, Cline and additional harnesses as practical. Compatibility with a
harness is never claimed without evidence; adding a harness validates the
common contract and never adds a client-specific adapter.

#### Current state versus accepted architecture

Implemented today: execution surface v1 (models discovery with logical
models per D-055, reasoning-effort exactness with bounded dialect
normalization, streaming, typed errors, bearer authentication), the
client-owned tool round trip on server-direct channels whose presets
evidence it (`tool_calls`/`tool_results` PASS/PARTIAL cells; fail-closed
admission elsewhere), loopback plain HTTP (native loopback and containers
sharing the host network namespace), TLS-required non-loopback binds, and
D-055's `effective_context_limit_tokens` metadata intersection. Accepted
here and not yet implemented: the effective-limits intersection and its
model-level aggregation (#136), the client tool round trip on the Codex
worker source (#137), the same-host container plaintext tier including
bridge loopback-publish support (implemented under #139), a Responses
adapter, and Tier 3 harness evidence.

### Migration plan

- No rewrite: the existing package, language and module structure are
  preserved; gateway and worker code are new modules beside the application
  layer (see the file map below).
- Machine-interface v1 changes are additive-only (D-028); the execution
  surface starts at its own v1 and evolves additively within major versions
  (D-045); the worker protocol negotiates versions independently (D-043).
- Capacity contract v3 is preserved; executable-resource state extends
  through the new resource-state contract (M01), not through a forced v4.
- Catalog/policy artifacts evolve additively per the D-032 discipline.
- Recommendation-only users migrate nothing: zero-configuration continuity
  is an M08/M10 acceptance criterion, not an aspiration.

### Existing files and modules

Mapping of the current tree to module ownership (no new directory structure
is imposed by A0; new modules land beside these as their issues require):

| Existing file/module | Primary owner going forward |
| --- | --- |
| `capacity.py`, `eligibility.py`, `status.py`, `resource_state.py` (M01 resource-state contract, added by #86), `providers/*` (collectors) | M01 (+#86); execution adapters in `providers/` per M04/M06 |
| `selector.py`, `policy.py`, `scarcity.py`, `simulation.py`, `selection_types.py`, `selection_app.py`, `routing_core.py` (M02 route-decision contract, added by #87) | M02 (+#87) |
| `server.py` | Stays the frozen loopback REST v1 adapter (M08 guard); never becomes the execution server |
| `machine_api.py` | M08 (+#93); gateway request parsing is new M03-owned code, not a v1 change |
| `mcp.py`, `cli.py` | M08 (+#93); `doctor` realization and config UX extend via M09 |
| `config.py` | M09 (+#94) |
| `model-catalog.json`, `model-policy.json`, `examples/*` | M01/M02/M09 additive evolution |
| New: gateway/coordinator modules, worker package, server control API/UI | M03, M05, M09 respectively |

## Deferred architecture

Runtime failure feedback, central team quota pools, multi-user policy,
signed catalog releases and commercial services remain future possibilities,
not foundations of the current service. Persistent server-side state beyond
D-041's minimal store, and any multi-user authorization model, require new
explicit decisions.
