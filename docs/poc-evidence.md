# Proof-of-concept evidence

This document distinguishes experimentally established facts from design
assumptions and future work. Historical usage values are evidence that the
mechanisms worked at one point; they are not current configuration or defaults.

## CONFIRMED

### OpenAI/Codex subscription capacity

Test environment:

```text
codex-cli 0.151.0-alpha.7.2
```

A Codex binary available under the VS Code ChatGPT extension was launched as:

```text
codex app-server
```

The successful JSONL interaction was:

```text
initialize
initialized notification
account/rateLimits/read
```

The response included plan type and two quota windows with `usedPercent`,
`windowDurationMins` and `resetsAt`, plus a rate-limit-reached field. The observed
durations were:

```text
300 minutes   -> five-hour window
10080 minutes -> seven-day/weekly window
```

No model prompt or model request was required.

Representative redacted response shape observed in the PoC:

```json
{
  "rateLimits": {
    "limitId": "codex",
    "primary": {
      "usedPercent": 6,
      "windowDurationMins": 300,
      "resetsAt": 1788306212
    },
    "secondary": {
      "usedPercent": 52,
      "windowDurationMins": 10080,
      "resetsAt": 1788748064
    },
    "planType": "plus",
    "rateLimitReachedType": null
  }
}
```

The example timestamps and percentages are historical evidence only.

### Z.ai Coding Plan capacity

PoC environment included Kilo 7.5.6. Kilo reported a `Z.AI Coding Plan`
provider and stored authentication data at:

```text
~/.local/share/kilo/auth.json
```

The relevant provider identifier was `zai-coding-plan` with credential type
`api`. Using that existing credential, this read-only request succeeded:

```text
GET https://api.z.ai/api/monitor/usage/quota/limit
Authorization: <existing Z.ai credential>
```

It returned plan level, typed quota limits and utilization percentages. No model
prompt or model request was required.

The PoC summary recorded a partial shape (see "Historical M0 PoC shape" at the
end of this section). The authoritative current shape, re-verified on
2026-09-01, is below.

### 2026-09-01 M1 reconnaissance

Read-only reconnaissance of `GET https://api.z.ai/api/monitor/usage/quota/limit`
performed at `2026-09-01T22:49:51Z` confirmed the following. The credential was
used only in-process and never printed; the Authorization value was the stored
credential as-is (no `Bearer` prefix was required for a 200).

Envelope:

```json
{
  "code": 200,
  "msg": "Operation successful",
  "success": true,
  "data": {
    "level": "pro",
    "limits": [
      {
        "type": "TIME_LIMIT",
        "unit": 5,
        "number": 1,
        "usage": 1000,
        "currentValue": 0,
        "remaining": 1000,
        "percentage": 0,
        "nextResetTime": <epoch-ms>,
        "usageDetails": [
          {"modelCode": "<feature>", "usage": 0},
          {"modelCode": "<feature>", "usage": 0},
          {"modelCode": "<feature>", "usage": 0}
        ]
      },
      {
        "type": "TOKENS_LIMIT",
        "unit": 3,
        "number": 5,
        "percentage": <int 0-100>,
        "nextResetTime": <epoch-ms>
      },
      {
        "type": "TOKENS_LIMIT",
        "unit": 6,
        "number": 1,
        "percentage": <int 0-100>,
        "nextResetTime": <epoch-ms>
      }
    ]
  }
}
```

(The Kilo auth entry for this provider carries its credential under a `key`
field with `type="api"`; the credential value is intentionally never reproduced.)

Established facts:

- **Envelope.** `code` (int), `msg` (str), `success` (bool), `data` (object).
  `data.level` is the plan tier (`"pro"` observed). `data.limits` is an ordered
  array; array position carries no semantics.
- **Window kinds are carried by `type`.** `TOKENS_LIMIT` entries are
  token-quota windows; `TIME_LIMIT` is a different, non-token limit. The adapter
  must not read the `usageDetails.modelCode` entries (non-token per-feature
  counters) as model token quota.
- **Reset metadata.** Every window observed carries `nextResetTime`, a
  **13-digit epoch-millisecond** value (interpreting it as seconds overflows the
  representable date). The three windows reset at distinct instants: the
  `(unit=3, number=5)` entry ~1h after retrieval (five-hour cadence), the
  `(unit=6, number=1)` entry ~2d after (weekly cadence), and the `TIME_LIMIT`
  entry ~26d after. This independently corroborates the known-window cadences.
- **Percentage orientation — used (evidence-backed).** The `TIME_LIMIT` entry's
  counter triple was observed as `currentValue=0`, `remaining=1000`,
  `usage=1000` alongside `percentage=0`. Reading those as used / remaining /
  cap, `percentage=0` equals the used percentage and the remaining percentage
  is its complement (100%); the used% reading is the only self-consistent one.
  The M0 historical reading (98% "used", 2% remaining) agrees. A two-snapshot
  check (percentage moving with consumption) would fully confirm this and is
  listed under Future evidence.
- **Window discrimination.** A known window is one whose `(unit, number)`
  combination is in the validated set `{(3,5): five-hour tokens, (6,1): weekly
  tokens}`. Any other `(unit, number)`, or a `TOKENS_LIMIT` missing `unit` or
  `number`, is an **unknown window** and must be reported with unknown semantics
  — never guessed, and never defaulted to 0%/100% remaining.
- **Fields that distinguish known from unknown windows:** `type` (kind),
  `unit`, `number`, and `percentage`. `nextResetTime` presence is separate
  evidence and is not a window-identity marker by itself.
- **Fail-safe for unknown `(unit, number)`:** preserve the raw non-secret fields,
  mark the window `unknown` (semantics unresolved), and let the core apply the
  unknown-window policy; do not synthesize a known `kind`.

Historical M0 PoC shape (partial; pre-dates the re-verification):

```json
{
  "data": {
    "limits": [
      {"type": "TIME_LIMIT", "percentage": 0},
      {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 2},
      {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 98}
    ],
    "level": "pro"
  }
}
```

This M0 summary omitted `code`/`msg`/`success`, `nextResetTime`, and the
`TIME_LIMIT` counter fields later observed; treat it as a partial record, not
the current schema.

### Confirmed historical observation

At one test point the normalized interpretation was:

```text
OpenAI:
  five-hour used: 6%       (94% remaining)
  weekly used:   52%       (48% remaining)

Z.ai:
  five-hour used: 2%       (98% remaining)
  weekly used:   98%       (2% remaining)
```

This demonstrated why all active windows matter: the Z.ai short window looked
plentiful while the weekly capacity was critical.

### Security fact

The working mechanisms did not require browser-cookie inspection or a model
prompt. No actual credential value belongs in this repository, its history,
fixtures, logs or documentation.

## ASSUMED / NOT YET VALIDATED

- The OpenAI app-server fields and method will remain compatible with a future
  collector version.
- A robust Codex binary discovery strategy can support installations beyond the
  tested VS Code extension layout.
- Z.ai's `(unit, number)` mapping and endpoint schema will remain compatible.
  The 2026-09-01 reconnaissance confirms the current mapping (`3/5` five-hour,
  `6/1` weekly) and `nextResetTime` (epoch-ms) as currently observed, but it is
  provider-specific evidence, not a permanent contract.
- Z.ai reset metadata semantics are now captured in a fully redacted fixture
  (`tests/fixtures/zai-coding-plan/`); only the current epoch-ms `nextResetTime`
  is treated as a normalized reset time.
- Kilo auth layout and provider identifier remain stable across releases.
- The `TIME_LIMIT` counter triple (`usage`/`currentValue`/`remaining`) is
  stable and its used/remaining reading is correct; only the used-orientation
  reading is relied on.
- Local Ollama can expose the needed model presence and effective configuration
  through a stable, safe local interface.
- Provider terms permit this continued read-only personal use; this should be
  checked before public release and maintained as providers evolve.

Assumptions must not be described as supported behavior in user-facing output.

## FUTURE EVIDENCE NEEDED

- Minimal redacted raw fixture for a successful **OpenAI** response (the
  **Z.ai** success and failure fixtures already exist under
  `tests/fixtures/zai-coding-plan/`).
- Failure captures still needed for **OpenAI**: auth required, rate limit
  reached, malformed JSON, missing windows and app-server protocol mismatch
  (the **Z.ai** auth-failed, missing-window, unknown-unit and schema-changed
  shapes already exist as fixtures).
- A second-snapshot **Z.ai** check that `percentage` moves with consumption,
  fully confirming used-orientation.
- Z.ai redirect behavior under an authorization-bearing response (the 2026-09-01
  reconnaissance observed no redirect; no cross-host redirect with Authorization
  was exercised).
- Supported Codex binary discovery and compatibility matrix.
- Ollama health/model-inspection PoC, including effective context reporting.
- Freshness/refresh behavior and latency measurements.
- Security review of provider terms and any public redistribution implications.

