# OpenAI Codex app-server fixtures

These fixtures are **structurally representative, synthetic redacted** inputs for
the OpenAI Codex collector's parser and contract tests. They carry no
credentials, no `Authorization` material, no account identifiers, no raw
subprocess output and no local paths.

Source of the shape: the successful PoC JSONL interaction recorded in
`docs/poc-evidence.md` ("OpenAI/Codex subscription capacity") plus the
2026-09-03 collector reconnaissance recorded in the same document
(including the serde string-table/generated-schema facts for tag
`rust-v0.151.0-alpha.7.2`). Each file below is the decoded JSON-RPC
`result` object of one `account/rateLimits/read` response — a complete
`GetAccountRateLimitsResponse` envelope. The exact tagged schema requires
`rateLimits`; the nullable `rateLimitsByLimitId` and
`rateLimitResetCredits` members may be absent or explicitly null. Values are
synthetic and chosen to exercise the required parsing paths rather than
replay a live reading.

## Files

- `ratelimits-ok-plus.json` — success with both known windows
  (`windowDurationMins` 300 and 10080) under the PoC `primary`/`secondary`
  slots. The parser must classify semantics from validated
  `windowDurationMins`, not the slot names, derive
  `remaining = 100 - used`, and preserve the plan label `plus`.
- `ratelimits-full-shape-ok.json` — the exact evidenced envelope with all
  three required members (null absent states) and the nine-member snapshot
  with every typed member present (`credits: null`, `individualLimit:
  null`, `spendControlReached: false`, `limitName: null`). Healthy.
- `ratelimits-credits-present.json` — valid typed credit/spend/reset-credit
  states (`CreditsSnapshot` with required boolean fields and optional
  string-or-null `balance`, `SpendControlLimitSnapshot` with four required fields and string
  `limit`/`used`, reset-credit summary with `availableCount` and fully typed
  rows). Valid but
  v1-unrepresentable: degrades to `unknown` with percentage pairs
  withheld.
- `ratelimits-spend-control-exhausted.json` — `individualLimit` at
  `remainingPercent: 0` (also `used >= limit`): a backend blocker, never
  healthy, pairs withheld.
- `ratelimits-credits-malformed.json` — `credits.balance` as a JSON number
  instead of the evidenced string-or-null: `schema_changed`.
- `ratelimits-additional-window-present.json` — an additional metered bucket
  window is emitted with a distinct safe identity rather than merged with a
  main window; the snapshot is `unknown` because v1 cannot represent the
  cross-bucket metering.
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
- `ratelimits-degraded.json` — one `usedPercent` is a string (unusable), one
  window omits `resetsAt`, and `planType` is an unevidenced label (`luna`).
  The parser must omit the affected values with explicit diagnostics and omit
  the plan rather than leaking arbitrary provider text.
- `ratelimits-schema-changed.json` — a plausibly evolved envelope
  (`rateLimits.windows[].{kind, consumedPercent, resetTimeUtc}`) that is not
  the observed shape. The parser must fail closed to `schema_changed` with no
  partial windows.

## Assertions expected of the collector

- success yields all present windows; slot names carry no period semantics;
- only `rateLimits` must be present (the map/reset members may be absent or
  null), and typed credit/spend/reset members
  validate strictly, never surfacing values in output;
- missing window coverage, duplicate known periods and a non-`codex`
  `limitId` never yield a healthy snapshot;
- backend blockers — a non-null `rateLimitReachedType`,
  `spendControlReached: true`, an exhausted `individualLimit`, or a
  blocked/exhausted additional bucket — degrade to `unknown` with
  percentage pairs withheld; valid v1-unrepresentable credit/spend/reset
  states do too; known exhaustion without any blocker stays `ok` as
  `(100, 0)`;
- unknown / missing values remain unknown, never defaulted to 0 or 100;
- schema change maps to `schema_changed` and no synthesized windows;
- no credential- or authorization-shaped string, subprocess output or local
  path ever appears in normalized output, diagnostics or the snapshot.
