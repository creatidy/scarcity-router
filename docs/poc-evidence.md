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

It returned plan level, typed quota limits, utilization percentages and reset
metadata. No model prompt or model request was required.

Representative redacted response shape observed in the PoC:

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

In the observed/current implementation, `unit=3, number=5` represented the
five-hour token window and `unit=6, number=1` represented the weekly token
window. The numeric mapping is provider-specific evidence that must be guarded
by fixtures and validation, not assumed permanent.

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
- Z.ai's numeric unit mapping and endpoint schema will remain compatible.
- Z.ai reset metadata has stable enough semantics for a normalized reset time;
  the exact raw field mapping must be captured in a fully redacted fixture.
- Kilo auth layout and provider identifier remain stable across releases.
- Local Ollama can expose the needed model presence and effective configuration
  through a stable, safe local interface.
- Provider terms permit this continued read-only personal use; this should be
  checked before public release and maintained as providers evolve.

Assumptions must not be described as supported behavior in user-facing output.

## FUTURE EVIDENCE NEEDED

- Minimal redacted raw fixtures for successful OpenAI and Z.ai responses,
  including exact request/response IDs only where non-secret and necessary.
- Failure captures for auth required, rate limit reached, malformed JSON,
  missing windows, unknown Z.ai units and app-server protocol mismatch.
- Supported Codex binary discovery and compatibility matrix.
- Precise Z.ai reset-field semantics and redirect behavior.
- Ollama health/model-inspection PoC, including effective context reporting.
- Freshness/refresh behavior and latency measurements.
- Security review of provider terms and any public redistribution implications.

