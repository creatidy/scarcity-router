# Normalized capacity model

## Purpose

Capacity describes whether an account or local runtime can accept work now and
how scarce that ability is. It does not describe model quality. The normalized
model must preserve provider evidence without leaking provider-specific parsing
into selection.

## Snapshot

A capacity snapshot conceptually contains:

| Field | Meaning |
| --- | --- |
| `provider` | Stable provider/source identifier. |
| `account` | Non-secret account identifier if safely available; optional. |
| `plan` | Plan name/type if reported. |
| `source` | Collection mechanism, for example Codex app-server. |
| `retrieved_at` | UTC collection timestamp. |
| `freshness` | Age or expiry information; never inferred as current forever. |
| `status` | `ok`, `unavailable`, `auth_required`, `unsupported`, `schema_changed` or `unknown`. |
| `windows` | Arbitrary list of normalized quota windows. |
| `local_runtime` | Availability/configuration facts for non-quota local sources. |
| `diagnostics` | Safe, non-secret reason codes/messages. |

The serialized schema must distinguish a missing field from a known zero.

## Quota windows

A normalized window should be able to represent:

```json
{
  "kind": "weekly",
  "duration_minutes": 10080,
  "used_percent": 98,
  "remaining_percent": 2,
  "resets_at": 1788748064,
  "provider_metadata": {
    "semantic_status": "validated"
  }
}
```

This is a conceptual example, not the published M1 schema.

Requirements:

- accept an arbitrary number of windows;
- preserve duration and reset time when supplied;
- derive `remaining_percent = 100 - used_percent` only after validating a
  numeric percentage in `[0, 100]`;
- classify a window using validated duration/metadata, never array order or a
  permanent assumption that names such as `primary` have fixed semantics;
- retain non-secret provider metadata needed to diagnose new/unknown windows;
- explicitly mark semantics unknown rather than forcing a known kind.

The most optimistic window must never determine effective capacity. For initial
selection, effective subscription headroom is the lowest remaining percentage
among all relevant, validated, active limiting windows. Unknown relevant window
semantics make confidence degraded and invoke the unknown policy; they are not
discarded merely because known windows look healthy.

## Local capacity

Local Ollama models do not have a subscription percentage. Represent facts such
as:

- runtime available/unavailable;
- configured model present/missing;
- context and configured output ceilings;
- health or load state if it can be measured safely and reliably.

Local capacity may have `scarcity = none`, but it can still be unavailable.
Never serialize “unlimited” as a fake 100% quota. User-facing output may say
“local” or “no subscription quota” while machine fields remain typed honestly.

## Scarcity

Human-readable states are a presentation of a continuous penalty, not the
selection mechanism itself. The initial proposed scale for known subscription
headroom `r` expressed from 0 to 1 is:

```text
scarcity_penalty(r) = (1 - r)^2
```

The selector takes the maximum penalty across relevant active windows,
equivalent to using the minimum remaining headroom. This yields a gradual
increase: 80% remaining is low penalty, 50% is moderate, 20% is meaningful,
10% is large and 2% is extreme.

Proposed explanatory labels are:

| Effective remaining | Label |
| --- | --- |
| 75–100% | `PLENTIFUL` |
| 40–<75% | `NORMAL` |
| 10–<40% | `SCARCE` |
| >0–<10% | `CRITICAL` |
| Explicitly exhausted/unreachable | `UNAVAILABLE` |
| Insufficient or uninterpretable telemetry | `UNKNOWN` |

These thresholds are initial policy defaults and require scenario tests before
M2 acceptance. `UNAVAILABLE` is not simply a label for every numerical zero;
provider status and reset/reached metadata must be considered.

Reset proximity is preserved and explained but does not alter the initial
penalty formula. A later evidence-backed policy may account for a near reset.

## Freshness and failure

Telemetry is a timestamped observation, not timeless state. M1 must define and
test refresh/freshness behavior before publishing the CLI contract. Stale data
must be labeled; it must not be silently presented as current.

Unknown is first-class:

- `schema_changed` means the adapter recognized an incompatible response;
- `unsupported` means the mechanism or semantics are intentionally not handled;
- `auth_required` means safe credentials were not available or accepted;
- `unknown` covers insufficient evidence without pretending certainty.

Known healthy sufficient candidates outrank capacity-unknown candidates under
the default balanced policy. Unknown is not automatically equivalent to
unavailable: if it is the only capable option, the broker may recommend it with
a prominent degraded-confidence warning, subject to user policy.

## Provenance

Every snapshot records its source and retrieval time. Raw responses may be used
ephemerally for parsing, but persistence must be allowlist-based, redacted and
justified. No credential or Authorization material belongs in snapshots,
diagnostics, caches, fixtures or explanations.

