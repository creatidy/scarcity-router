# Roadmap

This page summarizes current direction. It is development planning, not
required onboarding. The detailed completed-milestone record is preserved in
[`docs/history/roadmap.md`](history/roadmap.md), and live acceptance evidence
is in [`docs/m3-acceptance.md`](m3-acceptance.md).

## Current Status

The Creatidy alignment audit is tracked by
[#173](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/173).
Its evidence baseline is canonical `develop` at
`8d9d4b04bcb23fe19ff702b6209fbcb1537cdf1b`, fetched on 2026-10-04.
Integrated code, a proposed PR, deployment and live acceptance are separate
states. In particular, open PR #170 is not integrated evidence; the newer
telemetry correction #171 / merged PR #172 is in this baseline.

## Creatidy Requirement Coverage

Audit matrix for the owner's shared architecture v1.0 (2026-10-04),
source sections 1-5, 7-13 and 14-18. G identifiers are analysis identifiers,
not issue numbers. The source file was not found in the workspace; the
owner-supplied repository-specific brief is the authority for this audit.
Exact source provenance and status vocabulary belong in
[`architecture.md`](architecture.md). This matrix records coverage, not an
alternative operational task queue; Forgejo issues own execution and closure.

| Gap / requirement | Owner of this part | Current baseline evidence | Documents to align | Coverage / remaining work |
| --- | --- | --- | --- | --- |
| G01 Harness adapters | Kernel; Router supplies inference compatibility | [Kernel #11](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/11) proves recommendation consumption, not gateway execution | Architecture, execution contract | [Kernel #47](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/47) separate control/backend proofs, [#53](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/53) adapter implementation; Router [#174](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/174)/[#181](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/181) own producer/compatibility, not harness control |
| G02 Ordinary task intake | Kernel | Router has no TaskSpec intake; machine v1 accepts requirements, not tasks | Product, agent context | [Kernel #49](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/49) owns authorized ordinary-issue specification; no local implementation because intake is outside Router ownership |
| G03 Review/remediation | Kernel | Router governance is a development workflow, not a product review loop | Product, LLM operating policy | [Kernel #54](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/54) owns durable independent product review/remediation; no Router loop implementation |
| G04 Executable recommendation/admission/pin | Router producer; Kernel/harness consumer | `SelectionDecision` vs `RouteDecision`; exact `admit_pinned_target`/gateway `sr-pin:`; core resource-pin defect reproduced | Architecture, machine/execution/control contracts | #87/#88 delivered primitives, not public consumer contract: [#174](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/174); independent core defect [#178](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/178) |
| G05 Rich requirements and harness compatibility | Kernel producer; Router interpretation | `TaskRequirement`, nine `HardConstraints`, `tighten_requirement`; unprofiled execution uses L0/no minima; #136 effective limits integrated | Capability and selection contracts | Existing monotonic core covered by #87/#136; missing rich producer-consumer mapping [#175](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/175), no new quality scale |
| G06 MI consumption | MI producer; Router consumer | `selection_app.py` loads catalog/policy; reviewed track floors; no MI consumer; MI #9 is proof only | Architecture, capability, selection | [#176](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/176) adoption/provenance/bootstrap migration; [MI #13](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/13) publication, [#15](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/15) producer; [Kernel #55](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/55) retains actual knowledge provenance |
| G07 Outcome feedback | Kernel exports outcome; Router local analysis consumer | `UsageAccounting`, `AuditRecord` are request-level, not accepted-task exports | Selection, capacity, architecture | [#177](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/177) explicit local import/correlation/calibration; [Kernel #58](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/58) export producer; [MI #17](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/17) optional sharing decision only, not upload service |
| G08 Status/state/events | Router producer; Console/CLI consumers | `status.render_human` raw fields; control/API/admin views already exist | README, control contract | [#182](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/182) CLI; [#183](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/183) authorized reconnectable state/events; reuse [#140](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/140) admin UX; Console counterpart not yet found |
| G09 Adapter/tools boundary | Router backend permissions; Kernel/harness/host isolation | D-063 edits an authorized project; fake-CLI tests plus historical live PR #161 evidence, not current target conformance | Security, architecture, ADRs, agents | U-014 / [#180](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/180) Router authority/migration proof; [Kernel #50](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/50) workspace isolation and [#52](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/52) affected-lane gate, no second sandbox |
| G10 Identity/protocols | Router route facts; harness observed identity | Recommendation effort is catalog-sourced; gateway effort uses variant; plan lane/dispatch audit cannot prove physical model; worker code v4 | Execution, worker, architecture | [#179](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/179) identity/effort defect; [#181](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/181) real compatibility/Responses subset proof; #148/#137/#158 already supply effort/continuation/reasoning primitives |
| G11 Multiple tasks/concurrency | Kernel scheduler; Router request admission | `gateway_coordinator._ConcurrencyReservation`, `GatewayLimits`; worker single-instance lifecycle; cancellation paths #165 | Architecture, capacity | [Kernel #59](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/59) tasks/workspaces/manual edits, [#61](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/61) cache locking; no second local scheduler or distinct evidenced Router concurrency defect. [#165](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/165) owns concrete cancel defects |
| G12 End-to-end economics | Router source/call facts; Kernel task aggregation | Confirmed pools/entitlements, rate-only `SpendingLimit`, `UsageAccounting`; not task-total enforcement | Capacity, selection, execution | [#184](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/184) access-mode/pools/hidden-call enforcement proof; [#177](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/177) local accepted-result analysis; no token-to-quota fiction |
| G13 Versions/distribution/acceptance | Each producer owns contracts; Router its artifacts/lifecycle | Independent contract versions, release/M10 records; unpublished index/image, external platform gates | Release, README, roadmap | [#185](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/185) contract/artifact migrations; [#186](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/186) uncovered public artifact/platform gates; reuse [#139](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/139) TLS and [#141](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/141) composed acceptance |

No row implies READY_FOR_LIVE_TASK. Known work remains tracked even when
implementation must wait for a producer contract, feasibility evidence or
separate owner authorization.

### Cross-Product Contract Links

The single closeout reread found [Kernel alignment #46](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/46)
and [MI alignment #11](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/11)
with registered children. All were OPEN at the reread: obligations, not
implementation receipts. Router only modifies its own issues/documents.

| Contract | Producer / consumer responsibility and acceptance order |
| --- | --- |
| Requirements | [Kernel #49](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/49) authorizes specs; [#51](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/51) transmits them after Router #175 mapping; [MI #14](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/14) agrees evidence/calibration identity, not a new quality scale |
| Exact execution | Router #174/#178/#179 produce/fix route semantics; [Kernel #47](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/47) proves actual control/backend combination; [#52](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/52) persists/consumes the Attempt binding. Affected coding lanes first require accepted Router #180 disposition; no copied ranker |
| MI publication | [MI #13](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/13) publication contract and [#15](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/15) production acquisition -> Router #176 admitted consumption -> [Kernel #55](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/55) actual-used provenance; freeze evidence cut plus evaluation semantics, not merely a timestamp |
| Economics/outcomes | Router #184 call/access/pool facts -> [Kernel #56](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/56) whole-task budgets -> [#58](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/58) explicit private local export -> Router #177 analysis; [MI #17](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/17) only a separate optional sharing decision |
| State/events | Router #183, [Kernel #57](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/57), [MI #16](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/16) independently own their producer state/events. Console rendering/delegation consumer counterpart was not found; no Console implementation or shared DB is assigned here |
| Migration/installed receipt | Router #185/#186, [MI #18](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/18), [Kernel #62](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/62) own their artifact/state migrations; [MI #19](https://forgejo.creatidy.com/Creatidy/model-intelligence/issues/19), [Kernel #60](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/60) and Router #141 receive composed installed proof after component delivery, not instead of it |

### Verified Local Foundation

At the recorded audit baseline, Scarcity Router provides:

- normalized subscription-capacity status for OpenAI/Codex and Z.ai Coding
  Plan (with the D-039 execution-eligibility reports);
- deterministic, explainable model selection and typed simulation;
- a loopback-only REST interface and a local stdio MCP adapter over the same
  application/core;
- a provenance-bearing catalog and data-driven task profiles;
- an installable local package (D-034) with a default user configuration
  (D-036);
- the implemented optional execution gateway (D-040 through D-049): one
  composed server with an authenticated OpenAI-compatible execution surface,
  control API and web UI, generic OpenAI-compatible HTTP/Ollama adapters,
  a native outbound-TLS worker, and the Codex worker-local adapter — with
  distribution and acceptance work retaining the explicit external
  gates recorded in
  [`docs/m10-acceptance.md`](m10-acceptance.md).

Recommendation-only operation is the default and remains fully supported
throughout everything below.

## Execution-Gateway Program (A0, issues #85–#95) — Historical Delivery

The owner-approved optional execution gateway (D-040, 2026-09-19) is
specified (A0, #85) and its original modules integrated. This historical
delivery is not completion of the shared-system gaps above. State per module,
with the honest evidence and remaining external gates:

| Planning id | Issue | Scope | Completion state |
| --- | --- | --- | --- |
| A0 | #85 | Architecture, decisions, contracts | Complete |
| M01 | #86 | Resource registry, state, and collectors for executable resources | Complete |
| M02 | #87 | Routing core, policy, and client-requirement binding for executable targets | Complete |
| M03 | #88 | OpenAI-compatible gateway and execution coordinator | Complete |
| M04 | #89 | Generic OpenAI-compatible HTTP adapter and Ollama integration | Complete |
| M05 | #90 | Native worker, pairing, and execution transport | Complete |
| M06 | #91 | Codex adapter (Stage 1 evidence + Stage 2 implementation) | Complete subject to the recorded `EXTERNAL_ACCEPTANCE_GATE: LIVE_CODEX_SUBSCRIPTION` live-acceptance gate |
| M07 | #92 | ZCode adapter (Stage 1 evidence + Stage 2 implementation) | Stage 1/2 integrated (D-061/D-063); older evidence was TLS-blocked; merged PR #161 records a later historical real edit at `a89975f`, not a fresh deploy check. Target ownership/migration remains U-014 / #180 |
| M08 | #93 | MCP, REST, and CLI compatibility; optional remote mode | Complete |
| M09 | #94 | Configuration, web UX, and diagnostics | Complete |
| M10 | #95 | Distribution, installation, update, and end-to-end acceptance | Implementation and deterministic acceptance complete subject to the explicit external gates in [`docs/m10-acceptance.md`](m10-acceptance.md) |

The acceptance records are
[`docs/m10-acceptance.md`](m10-acceptance.md) (platform support table,
sixteen-mission-scenario E2E matrix, measured first-run friction,
external gates) and
[`docs/m10-security-acceptance.md`](m10-security-acceptance.md) (the
26-row security matrix mapped to the D-044 threat model). The remaining
external gates — live Codex subscription acceptance, live Windows
acceptance, Windows code signing and PyPI Trusted Publisher
configuration — are owner actions recorded there and in
[`docs/release-engineering.md`](release-engineering.md). #186 now owns the
uncovered public artifact/platform delivery and proof; #141 retains composed
program acceptance and #139 TLS lifecycle. External authority is not a
reason to drop known work or claim the public product complete.

M07 ran as an independent research track and never blocked the
program: its Stage-1 feasibility evidence is complete
([`docs/zcode-adapter-stage1-evidence.md`](zcode-adapter-stage1-evidence.md))
and Stage 2 was initially cancelled by D-047. D-061 reopened it and D-063
authorized the now-integrated coding lane. Preserve its proven operational
properties until #180 resolves the shared-system ownership conflict. Z.ai
Coding Plan's generic HTTP adapter remains a different access path; neither
path's terms, quota or identity guarantees may be borrowed by the other.

The CI and public release-engineering foundation (#97,
[`docs/release-engineering.md`](release-engineering.md), D-046) is in
place: Forgejo development CI runs the authoritative gate plus the
package check on every `develop` push and PR, and the tag-driven public
release pipeline is implemented with its owner-side activation steps
recorded.

## Priorities and Acceptance Order

This is a dependency/risk explanation, not a new P0 taxonomy or permission
to implement unselected tasks. Formal dependencies are recorded in Forgejo.

1. **Exact route and identity correctness:** #178's reproduced core pin
   substitution and #179's effort/variant conflation must be corrected before
   #174's producer-consumer acceptance. Gateway admission itself is exact;
   the required closure is exact core target assertions and discovery/ingress
   tests for opaque variants and the effort-less plan lane, not a new ranker.
2. **Quality and executable selection:** #175 maps Kernel's real requirements
   monotonically; #174 proves resource-aware authorized output, preservation
   through the harness and explained admission/refusal. A v1 recommendation
   or L0 unprofiled route is not that proof. Generic clients retain their
   shipped behavior unless a reviewed migration authorizes change.
3. **Authority and economic guarantees:** #180 resolves D-063 without silent
   removal of a working path or two workspace editors. #184 proves no-PAYG
   access, pools, output/hidden-call limitations and unknown usage, rather
   than presenting configured entitlement or rate ceiling as total budget.
   These are prerequisites for the corresponding claimed guarantees.
4. **Independent operator improvements:** #182's readable CLI snapshot can
   ship without Console; #140 owns existing admin UX. #165 owns evidenced
   cancellation gaps, not a Router multi-task scheduler.
5. **Full remaining scope:** #185 contract/artifact migrations precede #176's
   admitted MI replacement; #177 consumes explicit local outcomes after route
   identity/economics; #181 proves supported protocol combinations/Responses
   subset; #183 supplies authorized state/events; #186 plus #139/#141 close
   public distribution/platform/integrated acceptance. Proof-dependent work
   stays registered, not erased by a minimal-delivery rationale.

New endpoints/payloads/CLI flags/protocol versions, MI admission/calibration
policy, lossy semantic mapping, native-agent migration and unsupported hard
cost guarantees require evidence and the relevant owner-reviewed decision
before implementation. Those are not routine documentation-review defects.
Live inference/effects, signing/publishing/service access and release/deploy
remain separately authorized operations; this alignment grants none.

## Other Evidence-Gated Direction

- **Provider evaluation:** add another subscription provider only after its
  telemetry, authentication, security and maintenance boundary is evidenced.
- **Distribution and feedback:** known requirements are already covered by
  #177/#185/#186 and existing #139/#141, not discretionary future work.
- **Additional extensions:** team policy and additional providers need a
  concrete use case and evidence. Compound work/review orchestration belongs
  to Kernel; Router may supply choices without owning task lifecycle.

The active artifact is catalog v5 / policy v9, with reviewed track-floor
Astra Low present since D-053; its lower-confidence floor is not the older
conditional stronger calibration. Historical v2-era absence is not current
eligibility. See the dated records in
[`model-calibration.md`](model-calibration.md). This task changes no ratings,
catalog entries, profile minima or selector behavior.

## Scope Guard

The product is two-mode (D-040): recommendation-only by default, plus the
optional execution gateway exactly as authorized by D-040 through D-045. It
does not own repositories, become a generic LLM gateway or agent
framework, orchestrate issues/PRs, schedule general background tasks, or execute
client-supplied tools. New work starts from an explicitly selected Forgejo
issue and must preserve the authoritative contracts and security boundaries.
The D-063 workspace-editing lane is the explicit current exception/conflict
(U-014/#180), not permission for new repository access or target conformance.
