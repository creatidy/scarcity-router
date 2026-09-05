# OpenAI Codex app-server fixtures

These fixtures are **structurally representative, synthetic redacted** inputs for
the OpenAI Codex collector's parser and contract tests. They carry no
credentials, no `Authorization` material, no account identifiers, no raw
subprocess output and no local paths.

Source of the shape: the successful PoC JSONL interaction recorded in
`docs/poc-evidence.md` ("OpenAI/Codex subscription capacity") plus the
2026-09-03 collector reconnaissance and the 2026-09-05 current-schema
compatibility evidence recorded in the same document (including the installed
generated schema for `codex-cli 0.151.0-alpha.7.2` and current upstream commit
`a7a4321593c77933c18f84ba9bd28eba095759d8`). Each file below is the decoded JSON-RPC
`result` object of one `account/rateLimits/read` response — a complete
`GetAccountRateLimitsResponse` envelope. For deserialization input, the JSON
Schema requires `rateLimits`; the nullable `rateLimitsByLimitId` and
`rateLimitResetCredits` members may be absent or explicitly null. The tagged
Rust serializer does not skip these `Option` fields and the TypeScript shape
requires both keys, so this input tolerance is not a claim about exact server
serialization. Values are synthetic and chosen to exercise the required
parsing paths rather than replay a live reading.

## Files

- `ratelimits-ok-plus.json` — success with both known windows
  (`windowDurationMins` 300 and 10080) under the PoC `primary`/`secondary`
  slots. The parser must classify semantics from validated
  `windowDurationMins`, not the slot names, derive
  `remaining = 100 - used`, and preserve the plan label `plus`.
- `ratelimits-full-shape-ok.json` — a current-compatible envelope with the
  ten-member snapshot
  with every typed member present (`credits: null`, `individualLimit:
  null`, `spendControlReached: false`, `normalModelSlug: null`,
  `limitName: null`). `ordinaryUsageAllowed` is explicitly true. Healthy.
- `ratelimits-credits-present.json` — valid typed credit/spend/reset-credit
  states (`CreditsSnapshot` with required boolean fields and optional
  string-or-null `balance`, `SpendControlLimitSnapshot` with four required fields and string
  `limit`/`used`, reset-credit summary with `availableCount` and fully typed
  rows). The credit and spend states are valid but v2-unrepresentable and
  degrade to `unknown` with percentage pairs withheld.
- Valid reset-credit summaries, including `{availableCount: 0, credits: []}`,
  are supplemental telemetry and do not block or withhold current quota
  percentages.
- `ratelimits-spend-control-exhausted.json` — `individualLimit` at
  `remainingPercent: 0`: a backend blocker, never healthy, pairs withheld.
- `ratelimits-credits-malformed.json` — `credits.balance` as a JSON number
  instead of the evidenced string-or-null: `schema_changed`.
- `ratelimits-additional-window-present.json` — an additional metered bucket
  plus the required matching `codex` mirror. Its window is emitted with a
  distinct safe identity rather than merged with a main window; the snapshot
  is `unknown` because v2 cannot represent the cross-bucket metering.
- `ratelimits-additional-bucket-exhausted.json` — an additional metered
  bucket under `rateLimitsByLimitId` (key matching its `limitId`) with an
  exhausted window (`usedPercent` 100): blocker, main pairs withheld.
- `ratelimits-slots-swapped.json` — the weekly window sits under `primary`
  and the five-hour window under `secondary`. Classification must follow
  the validated duration, never the slot position.
- `ratelimits-unknown-duration.json` — one window with an **unvalidated**
  duration (60 minutes). The parser must preserve it with `kind: "unknown"`
  and `duration_seconds: 3600`, never guess a known period, keep the
  healthy weekly sibling's pair, and degrade the overall snapshot to
  `unknown` because the five-hour constraint is missing.
- `ratelimits-exhausted-reached.json` — both windows report 100% used and a
  schema-backed `rateLimitReachedType` (`rate_limit_reached`). A non-null
  backend reached state must not yield a healthy snapshot with inferred
  remaining capacity: the snapshot degrades to `unknown` with the
  percentage pairs withheld.
- `ratelimits-zero-usage.json` — both windows report 0% used. Known zero
  usage normalizes to `(0, 100)`, distinct from an unknown pair.
- `ratelimits-degraded.json` — one window omits `resetsAt` and uses the exact
  `unknown` plan member. The parser must omit the affected reset value with an
  explicit diagnostic while preserving the safe plan. A missing, null or
  non-i32 `usedPercent` is schema drift.
- `ratelimits-schema-changed.json` — a plausibly evolved envelope
  (`rateLimits.windows[].{kind, consumedPercent, resetTimeUtc}`) that is not
  the observed shape. The parser must fail closed to `schema_changed` with no
  partial windows.

## Assertions expected of the collector

- success yields all present windows; slot names carry no period semantics;
- only `rateLimits` must be present for input deserialization (the map/reset
  members may be absent or null); when `rateLimitsByLimitId` is a present object it must include the
  matching `codex` mirror, and typed credit/spend/reset members
  validate strictly, never surfacing values in output;
- current envelope metadata is explicit: `ordinaryUsageAllowed` must be a
  boolean or null when present, `accountId` is validated as string-or-null but
  never emitted, and `rateLimitUpsell` is recognized as opaque presentation
  data. A true ordinary-usage permission is required for usable pairs; false,
  null or absence remains unknown. A non-null upsell is a current upstream
  recovery blocker, but its content is never inspected or emitted. The
  `normalModelSlug` snapshot member is validated as string-or-null and omitted
  from v2 output;
- missing window coverage, duplicate known periods and a non-`codex`
  `limitId` never yield a healthy snapshot;
- backend blockers — a non-null `rateLimitReachedType`,
  `spendControlReached: true`, an exhausted `individualLimit`, or a
  blocked/exhausted additional bucket — degrade to `unknown` with
  percentage pairs withheld; valid v2-unrepresentable credit/spend states do
  too; reset-credit summaries are supplemental and do not block; known
  exhaustion without any blocker stays `ok` as
  `(100, 0)`;
- unknown / missing values remain unknown, never defaulted to 0 or 100;
- schema change maps to `schema_changed` and no synthesized windows;
- no credential- or authorization-shaped string, subprocess output or local
  path ever appears in normalized output, diagnostics or the snapshot.
