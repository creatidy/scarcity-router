# Selection policy

Scarcity Router chooses among authorized executable resources using the least
scarce candidate that satisfies the caller's requirements. D-084 supersedes
the former repository-versioned catalog, task-profile policy, family floors
and capability-margin ranking. Historical decisions and their original
provenance remain in [decisions.md](decisions.md).

## Inputs And Authority

- The caller provides a typed requirement and may impose an exact provider,
  model and invocation variant. Existing explicit local routing policy can
  narrow, but cannot silently relax, a requirement.
- Runtime inventory reports exact model identities and reported effort options
  from authenticated configured execution sources. An administrator grants
  exact models on a source. A separately configured execution resource can also
  provide its exact identity and capabilities. Discovery, authorization,
  adapter support, account entitlement and quota are distinct facts.
- An administrator may explicitly bind provider telemetry scopes to all
  granted models of a source and account. Router uses only provider telemetry
  reported in that resource's own fresh worker observation; server-local
  provider telemetry and account eligibility are not attributed to a different
  worker. A missing binding or observation is unknown, not free, unlimited or
  shared capacity. Each resource is evaluated separately, including when
  several resources name the same model. Source health and strict non-paid
  execution-assurance gates still apply to the exact resource.
- Provider-only replenishment observations cannot identify a worker account;
  the composed runtime interface explicitly refuses them until a source-scoped
  contract exists. No reset or benefit is redeemed automatically.
- An administrator may attest an exact source/model/effort context bound using a
  non-secret `local:` evidence handle, the observed runtime version and a
  bounded verification interval. Router records that it is administrator
  attested, not independently verified; it rejects use before verification,
  at or after expiry, or when the runtime version differs. A plan-lane bound
  applies to the lane and makes no physical-model claim.
- Model Intelligence supplies external evidence when explicitly trusted,
  applicable and current. Router checks exact references, scope, freshness,
  conflicts and withdrawal. MI does not grant private authorization, prove
  account entitlement or change quota. An installation can operate without MI
  using independently supported local facts.
- Local administrator and client authorization, resource policy, compatibility,
  health and execution-adapter evidence remain mandatory at their boundaries.
  A source or public model name does not establish these properties.

## Eligibility

Resolve the actual resource and exact identity before ranking. Apply hard
requirements and protocol compatibility, administrator and client restrictions,
freshness, adapter availability and applicable capacity or reservation policy.
Unknown capability does not satisfy an explicit minimum; unknown context or
output bounds cannot satisfy a request requiring those bounds. An exact model
request is not rejected solely for missing comparative quality ratings.

Effort is accepted only when evidenced for the exact resource. An effort
reported by one source is not attributed to a different source or a separately
configured resource. An unqualified configured resource is represented by the
`unconfigured` invocation qualifier, not a fabricated effort. The plan-managed
ZCode lane has no attested physical model or effort and is not represented as
one; its existing workspace authority and migration constraints remain in force.

No configured or authorized resource means no candidate, not a recommendation
from hardcoded models. Unknown capacity remains explicit. The configured
unknown-capacity mode determines whether an unknown candidate is denied or
considered degraded; it is never treated as a zero-cost or unlimited resource.

## Ordering And Admission

For each eligible physical resource, evaluate its applicable capacity scopes,
blackouts, reservations, replenishment observations and local policy. The
selector orders known capacity ahead of degraded unknown capacity, then applies
active policy preferences, scarcity penalty, reported effort, explicit
preference order and a stable identity tie-break. A capability margin is not
calculated or used to rank candidates. The legacy scoring helper explicitly
refuses calls rather than manufacturing a replacement score.

The selected model and resource are bound together. Alternative resources for
the same model are ranked independently; an exhausted source is not selected
because another source of the same model has quota. Exact admission rechecks
authorization, compatible execution properties, policy and capacity for that
resource. An exact pin is never silently redirected to another physical model,
resource or effort. The gateway rechecks current authority at dispatch; a
recommendation is not a reservation.

## Explanations And Provenance

Decisions expose eligibility and truthful exclusions, known versus unknown
capacity, applicable windows and policy reasons. Runtime-inventory selection
uses schema 2 and identifies its input as runtime inventory, with null legacy
catalog version and date. Resource-bound target exclusions include their exact
identity and capacity/policy reason. Historical catalog-backed decisions keep
their original version and meaning; old ratings are not converted into new
evidence.

When MI contributes, the pinned MI utilization contract's schema-1 receipt is
retained intact within the schema-2 response. Decision and context bindings
remain immutable. A missing or invalid required publication cannot be treated as
a successful MI attestation, and a failed refresh cannot revive a withdrawal.

## Migration And Simulation

Legacy `--catalog`, `--model-policy` and globally defined profile inputs are
explicitly refused. Existing persisted source `allowed_tracks` grants remain
stored but cannot authorize adoption; an administrator must replace them with
exact `allowed_models` through the source model-policy operation. User state is
not automatically deleted or converted to invented evidence.

The authenticated server evaluates simulation overrides against a frozen copy
of source state through the same routing core. A percentage override must match
exactly one existing, known and authorized resource window; ambiguous source
matches fail rather than changing multiple accounts. A hypothetical evaluation
time recomputes resource freshness without mutating the live registry. Standalone
CLI, REST and MCP processes without execution-source configuration return an
honest empty inventory. See [machine-interfaces.md](machine-interfaces.md) for
versioned wire contracts and [control-surface.md](control-surface.md) for
administrator operations.
