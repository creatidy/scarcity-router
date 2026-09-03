# OpenAI Codex app-server fixtures

These fixtures are **structurally representative, synthetic redacted** inputs for
the OpenAI Codex collector's parser and contract tests. They carry no
credentials, no `Authorization` material, no account identifiers, no raw
subprocess output and no local paths.

Source of the shape: the successful PoC JSONL interaction recorded in
`docs/poc-evidence.md` ("OpenAI/Codex subscription capacity") plus the
2026-09-03 collector reconnaissance recorded in the same document. Each file
below is the decoded JSON-RPC `result` object of one
`account/rateLimits/read` response. Values are synthetic and chosen to exercise
the required parsing paths rather than replay a live reading.

## Files

- `ratelimits-ok-plus.json` — success with both known windows
  (`windowDurationMins` 300 and 10080) under the PoC `primary`/`secondary`
  slots. The parser must classify semantics from validated
  `windowDurationMins`, not the slot names, derive
  `remaining = 100 - used`, and preserve the plan label `plus`.
- `ratelimits-slots-swapped.json` — the weekly window sits under `primary`
  and the five-hour window under `secondary`. Classification must follow the
  validated duration, never the slot position.
- `ratelimits-unknown-duration.json` — one window with an **unvalidated**
  duration (60 minutes). The parser must preserve it with `kind: "unknown"`
  and `duration_seconds: 3600`, never guess a known period, and keep the
  healthy weekly sibling.
- `ratelimits-exhausted-reached.json` — both windows report 100% used and a
  non-null `rateLimitReachedType`. Known exhaustion must normalize to the
  `(100, 0)` pair, not an error or unknown status.
- `ratelimits-zero-usage.json` — both windows report 0% used. Known zero
  usage normalizes to `(0, 100)`, distinct from an unknown pair.
- `ratelimits-degraded.json` — one `usedPercent` is a string (unusable), one
  window omits `resetsAt`, and `planType` is an unevidenced label (`pro`).
  The parser must omit the affected values with explicit diagnostics and omit
  the plan rather than leaking arbitrary provider text.
- `ratelimits-schema-changed.json` — a plausibly evolved envelope
  (`rateLimits.windows[].{kind, consumedPercent, resetTimeUtc}`) that is not
  the observed shape. The parser must fail closed to `schema_changed` with no
  partial windows.

## Assertions expected of the collector

- success yields all present windows; slot names carry no period semantics;
- unknown / missing values remain unknown, never defaulted to 0 or 100;
- rate-limit reached with 100% used is known exhaustion, not a failure;
- schema change maps to `schema_changed` and no synthesized windows;
- no credential- or authorization-shaped string, subprocess output or local
  path ever appears in normalized output, diagnostics or the snapshot.
