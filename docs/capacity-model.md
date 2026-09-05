# Normalized Capacity Contract v2

## Purpose And Boundary

Capacity describes an observation from a supported subscription provider. It does
not describe model capability, task requirements, user policy or scarcity.
Provider adapters own wire-format parsing and emit this provider-independent
record; consumers must not parse provider responses.

This is the internal serialized contract between M1 read-only adapters and the
core. It is not a public REST, MCP or CLI contract. Those interfaces, including
their versioning, are later decisions. The record is an observation at one point
in time, not a promise that the source remains available.

## Versioning

Every snapshot has the required top-level field `schema_version` with the integer
value `2`. This version covers field names, types, enum values, timestamp format,
omission rules and field semantics. It is a capacity-contract version, not a
provider API, adapter implementation or interface version.

Schema v2 removes the optional v1 `local_runtime` field and the diagnostics that
only described local runtime state. The removal is intentional and is recorded in
decision D-017. No compatibility reader is provided for the unreleased internal
v1 contract.

The v2 top-level fields are exactly:

```json
{
  "schema_version": 2,
  "provider": "openai",
  "source": "codex_app_server",
  "plan": "plus",
  "retrieved_at": "2026-09-01T22:49:51.000Z",
  "status": "ok",
  "windows": [],
  "diagnostics": []
}
```

`plan` is optional. All other fields are required. Optional values are omitted,
never represented as `null`; empty `windows` and `diagnostics` arrays are valid.

The safe identifier fields `provider`, `source`, `plan` and `window_id` are
non-empty ASCII strings of at most 64 characters matching
`[a-z0-9][a-z0-9._:-]*` after adapter normalization. If a value cannot be
safely normalized to that form, the adapter omits the optional value or uses an
appropriate unknown status; it does not emit the original value.

A backwards-incompatible change is any removal, rename or retyping of a field;
change to an existing enum or allowlist value or meaning; addition of a value to
a closed enum or allowlist; change to timestamp, percentage or omission
semantics; or change that makes a previously optional field required. Such a
change requires the next integer schema version and an explicit decision. An
implementation change that preserves v2 semantics does not change the version.
No public-interface compatibility promise is made by this internal contract.

## Snapshot Fields

| Field | Required | v2 semantics |
| --- | --- | --- |
| `schema_version` | Yes | Integer `2`. |
| `provider` | Yes | Stable, short ASCII provider identifier. Current identifiers are `openai` and `zai`; later providers require separate scope. |
| `source` | Yes | Stable, short ASCII collection-mechanism identifier, such as `codex_app_server` or `zai_usage_endpoint`. It is not a URL, filesystem path, version string or raw provider field. |
| `plan` | No | A short, safe plan label normalized by the adapter, such as `plus` or `pro`. It is informational and must not contain account identity or raw response data. |
| `retrieved_at` | Yes | UTC time at which the adapter assembled this result, in the canonical format in [Time](#time). This is present for failures as well as successes. |
| `status` | Yes | One of the six values in [Provider status](#provider-status). |
| `windows` | Yes | An unordered array of normalized quota or limit windows. It may be empty for failures. |
| `diagnostics` | Yes | An unordered array of allowlisted safe diagnostic records, as defined in [Diagnostics and metadata](#diagnostics-and-metadata). |

There is deliberately no `account` field. Account identifiers are not needed to
interpret one snapshot and increase correlation and disclosure risk. They must
not be smuggled into `provider`, `source`, `plan`, diagnostics or metadata.

## Provider Status

`status` describes the collection/source result, not a scarcity label and not a
percentage. A numeric zero in a valid window remains `status: "ok"` when the
source was successfully read.

| Status | Meaning | Window rule |
| --- | --- | --- |
| `ok` | The source response was collected and validated enough to normalize without guessing. Individual windows may still be unknown. | Validated windows may be present; `windows: []` is valid. |
| `unavailable` | The provider source was definitely unreachable or unavailable at collection time. | `windows` must be empty. |
| `auth_required` | No safe usable credential was available, or the provider rejected authentication. | `windows` must be empty; no quota window or percentage is synthesized. |
| `unsupported` | The source or required collection mechanism is intentionally not supported or enabled. | `windows` must be empty; no quota window is synthesized. |
| `schema_changed` | A provider response was received, but its required shape or semantics are incompatible with the adapter's validated mapping. | Do not partially decode it into windows; use an empty array and safe diagnostics. |
| `unknown` | The attempt produced insufficient evidence for a more specific result without meeting the conditions for another status. | Validated partial windows may remain; unknown is not zero or full capacity. |

The six values are the complete status vocabulary. A provider-specific error must
map to one of these values rather than adding a status. The status-specific
diagnostic code is required for every non-`ok` snapshot:
`source_unavailable`, `auth_required`, `unsupported_source`, `schema_changed`
or `telemetry_unknown`, respectively.

## Time

`retrieved_at` and every `resets_at` use the same canonical representation:
RFC 3339 UTC with a literal `Z` and exactly three fractional-second digits:

```text
YYYY-MM-DDTHH:MM:SS.sssZ
```

For example, `2026-09-01T22:49:51.000Z` is valid. Offsets other than `Z`, local
times and provider epoch units are not normalized representations. A source
timestamp with second precision becomes `.000Z`; a source timestamp with
millisecond precision preserves those milliseconds. An invalid, ambiguous or
missing reset is represented by omitting `resets_at`, not by a sentinel or a
guessed value.

`retrieved_at` is the only freshness-related field in v2. Age calculation,
staleness thresholds, caching, refresh cadence and timeout policy remain U-003;
v2 does not claim that a snapshot is fresh merely because it has a timestamp.

## Quota Windows

`windows` contains one entry for each provider-reported quota or limit that the
adapter can normalize, including a limit whose semantics remain unknown. The
array has no positional meaning, and duplicate entries must not be merged merely
because they occupy similar positions. A provider window absent from the array
is not a zero-valued window and must not be synthesized.

Each entry has the following exact fields:

| Field | Required | v2 semantics |
| --- | --- | --- |
| `resource` | Yes | Validated limited resource: `tokens`, `time` or `unknown`. `time` is distinct from a token resource. |
| `kind` | Yes | Validated period kind: `five_hour`, `weekly` or `unknown`. `unknown` is required when the period cannot be established from evidence. |
| `duration_seconds` | No | Positive integer duration of the limiting interval, not time remaining until reset. Required for `five_hour` (`18000`) and `weekly` (`604800`); optional for `unknown`. |
| `used_percent` | No | Integer from `0` through `100`, present only as part of a validated percentage pair. |
| `remaining_percent` | No | Integer from `0` through `100`, present only with `used_percent`; it is exactly `100 - used_percent`. |
| `resets_at` | No | Canonical UTC reset instant from [Time](#time), when validated. |
| `provider_metadata` | No | When present, an object containing exactly the safe `window_id` field described below. |

`resource` and `kind` are independent. An unrecognized period uses
`kind: "unknown"` and may preserve a validated duration. Known period kinds
have fixed durations: `five_hour` requires `18000`, and `weekly` requires
`604800`. An adapter must not select a known kind from array position or an
unvalidated provider label.

### Percentage Invariant

`used_percent` and `remaining_percent` are a pair: both are present or both are
omitted. The adapter validates the provider value and its orientation, chooses
the normalized used value, and derives the complement. Values outside `0..100`,
non-integers, booleans, nulls and unvalidated values produce an omitted pair plus
`percentage_unknown`; they are not rounded or defaulted.

If a provider reports a remaining-oriented value, the adapter may normalize it
only after validating that orientation. If it cannot establish whether a value
means used or remaining, it must omit both fields even when the provider value
is numeric. The normalized contract has no provider `percentage` field and no
orientation flag.

Thus `(0, 100)` is known empty-used capacity, `(100, 0)` is known exhausted
capacity, and an omitted pair is unknown. All three states are distinct.

### Provider Metadata

When a safe provider window identifier exists, `provider_metadata` contains only:

```json
{
  "window_id": "safe-opaque-window-id"
}
```

`window_id` is diagnostic data only. It must not contain credentials, headers,
raw response fragments, URLs, filesystem paths, arbitrary error text or other
sensitive data. If no safe identifier exists, omit `provider_metadata`.

An unknown window remains in `windows` with `resource` and/or `kind` set to
`unknown`, even when no safe `window_id` exists. Unknown semantics are not
silently discarded because known windows look healthy.

## Diagnostics And Metadata

`diagnostics` is an array of records with exactly one required key, `code`, and
one optional key, `window_id`. `code` must come from this v2 allowlist:

| Code | Use |
| --- | --- |
| `source_unavailable` | Provider source could not be reached or was explicitly unavailable. |
| `auth_required` | Safe credential was missing or rejected. |
| `unsupported_source` | The configured source/mechanism is not supported. |
| `schema_changed` | Required provider shape or semantics no longer map safely. |
| `telemetry_unknown` | The overall observation lacks enough evidence for a more specific status. |
| `window_semantics_unknown` | A window's resource or period remains unknown. |
| `percentage_unknown` | A usable percentage pair could not be established. |
| `reset_unknown` | A reset value was missing, invalid or ambiguous. |

Only `window_semantics_unknown`, `percentage_unknown` and `reset_unknown` are
window-scoped and may carry `window_id`; all other codes must omit it. There is
no free-form `message`, `detail`, exception, stderr, response body or
credential-bearing field.

Provider metadata and diagnostics are allowlisted output, not a redaction layer
around arbitrary data. They must never contain credentials, Authorization
material, raw provider responses, arbitrary stderr, sensitive paths, account
identifiers or endpoint URLs.

## Explicitly Outside V2

This contract does not define freshness thresholds, caching, refresh behavior,
timeouts, effective headroom, scarcity formulas or labels, reservations,
selection, capability ratings, model identity/catalog data, history, audit
storage, REST, MCP or CLI versioning. U-003 remains responsible for refresh and
staleness policy. M2 decisions remain responsible for scarcity and selection.

## Scenario Validation

| Scenario | v2 representation and invariant |
| --- | --- |
| A - OpenAI healthy | `status: "ok"` with two unordered windows whose validated periods are `five_hour` and `weekly`, durations `18000` and `604800`, canonical reset strings and complementary percentage pairs. |
| B - Z.ai healthy | Known five-hour and weekly token windows plus every additional observed window. Unknown periods remain `kind: "unknown"`; non-token limits remain `resource: "time"`. |
| C - Schema drift | `status: "schema_changed"`, `windows: []`, and a `schema_changed` diagnostic. The successful transport response does not justify guessed or partial quota. |
| D - Authentication unavailable | `status: "auth_required"`, `windows: []`, and an `auth_required` diagnostic. Missing credentials never become zero or full quota. |
| E - Missing versus zero | A known empty-used window has `(used_percent, remaining_percent) = (0, 100)`; an exhausted window has `(100, 0)`; an unknown percentage omits both fields. An absent window and `windows: []` are also distinct from either known pair. |

These cases are contract checks for provider adapter tests. They do not define
selection or scarcity outcomes.
