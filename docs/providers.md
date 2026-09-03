# Provider collectors

## Shared adapter contract

Each provider adapter performs one small read-only acquisition and maps it to a
normalized capacity snapshot. It must:

- discover/configure its source without copying secrets where practical;
- validate authentication source, endpoint scheme and exact host before use;
- parse into the normalized model without leaking raw provider types inward;
- preserve all relevant quota windows and reset times;
- return explicit health/failure status and safe diagnostics;
- carry retrieval time, safe plan metadata when known and source mechanism;
  account identifiers are not part of the v1 snapshot;
- have redacted fixtures, parser tests and contract tests;
- treat new fields tolerantly and changed required semantics conservatively;
- never print, return or persist credentials.

Collector failures are isolated. `status` should still report healthy sources
when another source is `auth_required`, `schema_changed` or `unknown`.

## OpenAI through Codex app-server

The proven mechanism launches a locally available `codex app-server`, speaks
JSONL, sends `initialize`, sends the `initialized` notification, and calls
`account/rateLimits/read`. It obtains subscription rate limits without a model
prompt.

Implementation requirements:

- discover a compatible Codex binary explicitly and report the selected binary
  and protocol/version safely;
- supervise subprocess lifetime, timeouts, malformed JSONL and stderr without
  leaking sensitive content;
- identify windows using `windowDurationMins` and validated fields, not the
  `primary`/`secondary` position alone;
- validate `usedPercent`, preserve `resetsAt`, `planType` and reached status;
- never fall back to browser-cookie scraping as an incidental convenience;
- fail with a clear unsupported/schema status if protocol behavior changes.

The PoC used a binary included with a VS Code ChatGPT extension, but production
binary discovery order and minimum compatible version are unresolved.

## Z.ai Coding Plan

The proven mechanism reads the existing Kilo provider entry identified as
`zai-coding-plan` (credential type `api`) and makes:

```text
GET https://api.z.ai/api/monitor/usage/quota/limit
Authorization: <existing credential>
```

The credential is transient input only. Before attaching it, the adapter must
require HTTPS and the exact expected host. An endpoint override must pass an
equally strict allowlist; arbitrary URLs are forbidden.

The observed response contains a plan level and a list of typed limits. In the
observed/current schema:

```text
unit=3, number=5 -> five-hour token window
unit=6, number=1 -> weekly token window
```

These are validated adapter mappings, not universal constants. Unknown units or
combinations must preserve safe metadata and yield unknown semantics rather
than being guessed. Array position is not semantic.

Status (M1): the response parser is implemented and fixture-tested in
`scarcity_router/providers/zai.py` (`parse_zai_quota_response`); live
acquisition (credential discovery and the authenticated request) is not
implemented.

Each observed window carries `nextResetTime`, a 13-digit epoch-**millisecond**
value, and `percentage` is the **used** percentage (remaining is the complement).
Both are current provider evidence, not a permanent contract; the authoritative
record and the required fail-safe fixtures live in
`poc-evidence.md` and `tests/fixtures/zai-coding-plan/`.

The adapter needs fixtures for known windows, unknown windows, missing values,
invalid percentages, authentication failure and schema change.

The currently observed Kilo auth location is `~/.local/share/kilo/auth.json`.
Discovery must read only the necessary provider entry, must never dump the file,
and should support an explicit safe path/configuration later without creating a
second long-lived token store.

## Local Ollama

The first local model is `qwen3.8:27b-3090-q4km-160k`. Known configuration at
design time includes context 163840, output ceiling 49152, reasoning enabled,
vision disabled, q8_0 KV cache, Flash Attention, no MTP/speculative decoding and
full GPU fit on an RTX 3090.

M1 only needs defensible local facts:

- is the Ollama runtime reachable locally;
- is the configured model present/available;
- what configured model and context limits are known;
- optional health/load state only if reliably measurable.

Do not invent quota. Do not assume the configured context is the effective
context without validating what the runtime exposes. The exact health and model
inspection calls must be selected from supported Ollama behavior during M1 and
recorded in PoC evidence before being treated as confirmed.

## Later providers

Claude subscription is the highest-priority later collector because it would
materially expand usefulness. It must not delay v0.1 and must be preceded by an
auth, security and maintenance evaluation. Other providers are added based on
user demand, stable telemetry, maintenance burden and routing value—not a
provider-count target.

## Contract-test expectations

For each adapter, freeze minimal redacted provider-shaped fixtures and assert:

- successful normalization;
- all active windows survive normalization;
- window semantics are classified only from validated evidence;
- missing/unknown semantics remain unknown;
- used/remaining validation and reset preservation;
- error/status mapping;
- no credential-shaped value appears in output, diagnostics or snapshots;
- provider changes do not crash the core.

Live integration checks may complement fixtures, but tests must not require or
record the owner's credentials.
