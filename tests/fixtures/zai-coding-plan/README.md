# Z.ai Coding Plan fixtures

These fixtures are **structurally representative, synthetic redacted** inputs for the
future Z.ai adapter's parser and contract tests. They carry no credentials, no
`Authorization` material, no account identifiers and no raw provider content
beyond the allowlisted response envelope.

Source of the shape: live read-only reconnaissance of
`GET https://api.z.ai/api/monitor/usage/quota/limit` on **2026-09-01T22:49:51Z**
(recorded in `docs/poc-evidence.md`). See the "2026-09-01 M1 reconnaissance"
section there for the exact observed envelope.

Values in each fixture (percentages, reset times) are synthetic and chosen so the
fixtures exercise the required parsing paths rather than replay a live reading.

## Files

- `quota-200-known-windows.json` — success with both known windows
  (`(unit=3, number=5)` and `(unit=6, number=1)`). The parser must classify
  semantics by validated `(unit, number)`, not array position, and derive
  `remaining = 100 - used` after numeric validation.
- `quota-200-unknown-window.json` — one window with an **unobserved** unit
  (`unit=7`). Parser must report the window as semantics-`unknown`, not guess
  and not drop the other, known window.
- `quota-200-weekly-missing.json` — weekly window absent. Parser must not
  synthesize a `weekly` value; effective headroom uses only the windows present.
- `quota-200-degraded-values.json` — the five-hour entry has `percentage` but a
  `null` `nextResetTime`; the weekly entry omits both `percentage` and
  `nextResetTime`. Parser must distinguish "known zero" from "missing/absent",
  treat a null reset as unknown, and leave the absent `percentage` `unknown`
  rather than defaulting to 0 or 100.
- `quota-schema-changed.json` — a plausibly evolved envelope
  (`data.result.windows[].{kind, period, consumedPercent, resetTimeUtc}`) that
  is **not** the observed shape. Parser must map this to `schema_changed`, not
  partially decode or guess semantics.
- `quota-401-auth-failed.json` — auth-failure envelope. Parser maps to
  `auth_required`; must not emit any window or percentage.

## Assertions expected of the parser

- success path yields all present windows; array position carries no meaning;
- unknown / missing fields remain `unknown`, never defaulted to 0 or 100;
- schema change maps to a `schema_changed` status and no synthesized windows;
- auth failure maps to `auth_required`;
- no credential- or authorization-shaped string ever appears in normalized
  output, diagnostics or the snapshot.
