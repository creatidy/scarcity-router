# Normalized capacity contract v1

## Purpose and boundary

Capacity describes a provider or local runtime observation. It does not describe
model capability, task requirements, user policy or scarcity. Provider adapters
own wire-format parsing and emit this provider-independent record; consumers must
not parse provider responses.

This is the internal serialized contract between M1 read-only adapters and the
core. It is not a public REST, MCP or CLI contract. Those interfaces, including
their versioning, are later decisions. The record is an observation at one point
in time, not a promise that the source remains available.

## Versioning

Every snapshot has the required top-level field `schema_version` with the integer
value `1`. This version covers field names, types, enum values, timestamp format,
omission rules and field semantics. It is a capacity-contract version, not a
provider API, adapter implementation or interface version.

The v1 top-level fields are exactly:

```json
{
  "schema_version": 1,
  "provider": "openai",
  "source": "codex_app_server",
  "plan": "plus",
  "retrieved_at": "2026-09-01T22:49:51.000Z",
  "status": "ok",
  "windows": [],
  "diagnostics": []
}
```

`plan` and `local_runtime` are optional. All other fields are required. Optional
values are omitted, never represented as `null`; empty `windows` and
`diagnostics` arrays are valid.

The safe identifier fields `provider`, `source`, `plan`, `model_name` and
`window_id` are non-empty ASCII strings of at most 64 characters matching
`[a-z0-9][a-z0-9._:-]*` after adapter normalization. If a value cannot be
safely normalized to that form, the adapter omits the optional value or uses an
appropriate unknown status; it does not emit the original value.

A backwards-incompatible change is any removal, rename or retyping of a field;
change to an existing enum or allowlist value or meaning; addition of a value to
a closed v1 enum or allowlist; change to timestamp, percentage or omission
semantics; or change that makes a previously optional field required.
Such a change requires the next integer schema version and an explicit decision.
An implementation change that preserves v1 semantics does not change the
version. A new optional field is outside v1 until it is documented and must be
safe for an older reader to ignore. No public-interface compatibility promise is
made by this internal contract.

## Snapshot fields

| Field | Required | v1 semantics |
| --- | --- | --- |
| `schema_version` | Yes | Integer `1`. |
| `provider` | Yes | Stable, short ASCII provider identifier. Current identifiers are `openai`, `zai` and `ollama`; the value is not an auth-entry name. |
| `source` | Yes | Stable, short ASCII collection-mechanism identifier, such as `codex_app_server`, `zai_usage_endpoint` or `ollama_local`. It is not a URL, filesystem path, version string or raw provider field. |
| `plan` | No | A short, safe plan label normalized by the adapter, such as `plus` or `pro`. It is informational and must not contain account identity or raw response data. |
| `retrieved_at` | Yes | UTC time at which the adapter assembled this result, in the canonical format in [Time](#time). This is present for failures as well as successes. |
| `status` | Yes | One of the six values in [Provider status](#provider-status). |
| `windows` | Yes | An unordered array of normalized quota or limit windows. It may be empty for failures and for local sources with no subscription quota. |
| `local_runtime` | No | Local reachability, model-presence and validated context facts, as defined in [Local runtime](#local-runtime). |
| `diagnostics` | Yes | An unordered array of allowlisted safe diagnostic records, as defined in [Diagnostics and metadata](#diagnostics-and-metadata). |

There is deliberately no `account` field in v1. Account identifiers are not
needed to interpret a single local snapshot and increase correlation and
disclosure risk. They must not be smuggled into `provider`, `source`, `plan`,
diagnostics or metadata. Adding safe account correlation would require a later
explicit decision and contract revision.

## Provider status

`status` describes the collection/source result, not a scarcity label and not a
percentage. A numeric zero in a valid window remains `status: "ok"` when the
source was successfully read.

| Status | Meaning | Window rule |
| --- | --- | --- |
| `ok` | The source response or local observation was collected and validated enough to normalize without guessing. Individual windows or optional values may still be unknown. | Validated windows may be present; `windows: []` is valid for a local source. |
| `unavailable` | The source, runtime or configured local resource was definitely unreachable or unavailable at collection time. | `windows` must be empty; local facts may show `reachable: false` or a missing configured model. |
| `auth_required` | No safe usable credential was available, or the provider rejected authentication. | `windows` must be empty; no quota window or percentage is synthesized. |
| `unsupported` | The source or required collection mechanism is intentionally not supported or enabled. | `windows` must be empty; no quota window is synthesized. |
| `schema_changed` | A provider response was received, but its required shape or semantics are incompatible with the adapter's validated mapping. | Do not partially decode it into windows; use an empty array and safe diagnostics. |
| `unknown` | The attempt produced insufficient evidence for a more specific result without meeting the conditions for another status. | Validated partial windows or local facts may remain; unknown is not zero or full capacity. |

The six values are the complete v1 status vocabulary. A provider-specific error
must map to one of these values rather than adding a status. The status-specific
diagnostic code is required for every non-`ok` snapshot: `source_unavailable`,
`auth_required`, `unsupported_source`, `schema_changed` or `telemetry_unknown`,
respectively. Additional allowlisted codes may explain partial facts.

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

`retrieved_at` is the only freshness-related field in v1. Age calculation,
staleness thresholds, caching, refresh cadence and timeout policy remain
`U-003`; v1 does not claim that a snapshot is fresh merely because it has a
timestamp.

## Quota windows

`windows` contains one entry for each provider-reported quota or limit that the
adapter can normalize, including a limit whose semantics remain unknown. The
array has no positional meaning, and duplicate entries must not be merged merely
because they occupy similar positions. A provider window that is absent from the
array is not a zero-valued window and must not be synthesized.

Each entry has the following exact fields:

| Field | Required | v1 semantics |
| --- | --- | --- |
| `resource` | Yes | Validated limited resource: `tokens`, `time` or `unknown`. `time` is a distinct non-token resource; it must not be folded into a token window. |
| `kind` | Yes | Validated period kind: `five_hour`, `weekly` or `unknown`. `unknown` is required when the period cannot be established from evidence. |
| `duration_seconds` | No | Positive integer duration of the limiting interval, not time remaining until reset. Required for `five_hour` (`18000`) and `weekly` (`604800`); optional for `unknown`. |
| `used_percent` | No | Integer from `0` through `100`, present only as part of a validated percentage pair. It is the normalized used share of this window's resource. |
| `remaining_percent` | No | Integer from `0` through `100`, present only with `used_percent`; it is exactly `100 - used_percent`. |
| `resets_at` | No | Canonical UTC reset instant from [Time](#time), when validated. |
| `provider_metadata` | No | When present, an object containing exactly the safe `window_id` field described below. |

`resource` and `kind` are independent. For example, a validated non-token limit
can use `resource: "time"` and `kind: "unknown"`; a token limit with an
unrecognized period can use `resource: "tokens"` and `kind: "unknown"`. A
provider with a validated one-hour window that is not in the v1 period vocabulary
uses `kind: "unknown"` and may still preserve `duration_seconds: 3600`.

Known period kinds have fixed durations in v1. An entry with `kind: "five_hour"`
must have `duration_seconds: 18000`; an entry with `kind: "weekly"` must have
`duration_seconds: 604800`. An adapter must not select a known kind from array
position or from an unvalidated provider label.

### Percentage invariant

`used_percent` and `remaining_percent` are a pair: both are present or both are
omitted. The adapter validates the provider value and its orientation, chooses
the normalized used value, and derives the complement. It must not independently
copy two provider fields and create a contradictory pair. Values outside
`0..100`, non-integers, booleans, nulls and unvalidated values produce an omitted
pair plus a `percentage_unknown` diagnostic; they are not rounded or defaulted.

If a provider reports a remaining-oriented value, the adapter may normalize it
to the pair only after validating that orientation. If it cannot establish
whether a value means used or remaining, it must omit both normalized percentage
fields even when the provider value is numeric. The normalized contract has no
provider `percentage` field and no orientation flag.

Thus `used_percent: 0` with `remaining_percent: 100` is a known empty-used
window, while `used_percent: 100` with `remaining_percent: 0` is a known
exhausted window. An omitted pair is unknown. All three states are distinct.

The current Z.ai evidence supports the adapter treating that provider's observed
`percentage` as used-oriented: the observed `TIME_LIMIT` counter relationship
was consistent with `percentage: 0` meaning zero used and full remaining, and the
historical reading was consistent with the same orientation. This is evidence
about a validated Z.ai schema, not a normalized contract rule. The adapter must
revalidate the envelope and orientation; if that evidence is unavailable or
changes, it must omit the pair and report a safe diagnostic or status rather than
invert or guess. The remaining Z.ai mapping and reset-field evolution stay under
`U-004`.

### Provider metadata

When a safe provider window identifier exists, `provider_metadata` contains only:

```json
{
  "window_id": "safe-opaque-window-id"
}
```

`window_id` is an adapter-allowlisted, non-secret identifier useful for
correlating an unknown window with a provider observation. It is diagnostic data
only; consumers must not use it as a semantic kind or rely on its format. It may
encode a provider's safe window key, but must not contain credentials, headers,
raw response fragments, URLs, filesystem paths, arbitrary error text or other
sensitive data. If no safe identifier exists, omit `provider_metadata`; no other
provider metadata key is part of v1.

An unknown window remains in `windows` with `resource` and/or `kind` set to
`unknown`, even when no safe `window_id` exists. Unknown semantics are not
silently discarded because known windows look healthy.

## Local runtime

`local_runtime` is for facts about a local runtime, not a subscription quota. Its
fields are:

| Field | Required when object is present | v1 semantics |
| --- | --- | --- |
| `reachable` | Yes | Boolean result of the local runtime reachability check. |
| `model_presence` | Yes | `present`, `missing` or `unknown` for the configured target model. |
| `model_name` | Required when `model_presence` is `present` or `missing`; otherwise optional | Safe configured model identifier, never a filesystem path or raw runtime output. |
| `configured_context_tokens` | No | Positive integer known from configuration. Omission means not known or not safe to retain. |
| `effective_context_tokens` | No | Positive integer validated as the runtime's effective context. It must not be inferred from the configured value. |

If the runtime cannot be reached, `model_presence` must be `unknown` even when a
configured target name is known; context fields may be retained only when their
source is independently validated. `missing` means the runtime explicitly
confirmed that the named configured model is absent, not merely that a check
failed. Context fields are independent, so a known configured context may coexist
with an omitted effective context.

An Ollama snapshot with a reachable runtime and present model uses
`windows: []` and this object; it does not need or receive percentage sentinels.
There is no `unlimited`, `scarcity`, load score or output-cap field in v1.

## Diagnostics and metadata

`diagnostics` is an array of records with exactly one required key, `code`, and
one optional key, `window_id`. `code` must come from this v1 allowlist:

| Code | Use |
| --- | --- |
| `source_unavailable` | Source or runtime could not be reached or was explicitly unavailable. |
| `auth_required` | Safe credential was missing or rejected. |
| `unsupported_source` | The configured source/mechanism is not supported. |
| `schema_changed` | Required provider shape or semantics no longer map safely. |
| `telemetry_unknown` | The overall observation lacks enough evidence for a more specific status. |
| `window_semantics_unknown` | A window's resource or period remains unknown. |
| `percentage_unknown` | A usable percentage pair could not be established. |
| `reset_unknown` | A reset value was missing, invalid or ambiguous. |
| `runtime_unreachable` | The local runtime reachability check failed. |
| `model_missing` | The named configured local model was explicitly absent. |
| `model_presence_unknown` | Local model presence could not be validated. |
| `configured_context_unknown` | Configured context was not safely known. |
| `effective_context_unknown` | Effective runtime context was not validated. |

For v1, only `window_semantics_unknown`, `percentage_unknown` and
`reset_unknown` are window-scoped and may carry `window_id`; all other codes must
omit it. The value must match the corresponding safe
`provider_metadata.window_id`. Diagnostic order has no meaning. There is no
free-form `message`, `detail`, exception, stderr, response body or
credential-bearing field. Interfaces may turn these stable codes into
human-readable text later.

Provider metadata and diagnostics are allowlisted output, not a redaction layer
around arbitrary data. They must never contain credentials, Authorization
material, raw provider responses, arbitrary stderr, sensitive local paths,
account identifiers or endpoint URLs.

## Explicitly outside v1

This contract does not define freshness thresholds, caching, refresh behavior,
timeouts, effective headroom, scarcity formulas or labels, reservations,
selection, capability ratings, model identity/catalog data, history, audit
storage, REST, MCP or CLI versioning. `U-003` remains responsible for refresh and
staleness policy. M2 decisions remain responsible for scarcity and selection.

## Scenario validation

| Scenario | v1 representation and invariant |
| --- | --- |
| A - OpenAI healthy | `status: "ok"` with two unordered windows whose validated periods are `five_hour` and `weekly`, with durations `18000` and `604800`, canonical reset strings and complementary percentage pairs. Codex `primary` and `secondary` are not contract values. |
| B - Z.ai healthy | `status: "ok"` with the known five-hour and weekly token windows plus every additional observed window. An unrecognized token period remains `resource: "tokens", kind: "unknown"` with optional safe metadata; it is not dropped or classified by position. A non-token limit remains `resource: "time"`, not a token window. |
| C - Schema drift | `status: "schema_changed"`, `windows: []`, and a `schema_changed` diagnostic. The successful transport response does not justify guessed or partial quota. |
| D - Authentication unavailable | `status: "auth_required"`, `windows: []`, and an `auth_required` diagnostic. Missing credentials never become zero or full quota. |
| E - Ollama local | `status: "ok"`, `windows: []`, and `local_runtime` with `reachable: true`, `model_presence: "present"`, a safe model name and whichever configured/effective context values were validated. No fake percentage is required. |
| F - Missing versus zero | A known empty-used window has `(used_percent, remaining_percent) = (0, 100)`; an exhausted window has `(100, 0)`; an unknown percentage omits both fields. An absent window and `windows: []` are also distinct from either known pair. |

These cases are contract checks for future adapter tests. They do not define
selection or scarcity outcomes.
