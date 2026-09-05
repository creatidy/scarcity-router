# Provider Collectors

## Shared Adapter Contract

Each supported provider adapter performs one small read-only acquisition and maps
it to a normalized capacity snapshot. It must:

- discover/configure its source without copying secrets where practical;
- validate authentication source, endpoint scheme and exact host before use;
- parse into the normalized model without leaking raw provider types inward;
- preserve all relevant quota windows and reset times;
- return explicit health/failure status and safe diagnostics;
- carry retrieval time, safe plan metadata when known and source mechanism;
  account identifiers are not part of the v2 snapshot;
- have redacted fixtures, parser tests and contract tests;
- treat new fields tolerantly and changed required semantics conservatively;
- never print, return or persist credentials.

Collector failures are isolated. `status` should still report a healthy source
when another source is `auth_required`, `schema_changed` or `unknown`.

## OpenAI Through Codex App-Server

The proven mechanism launches a locally available `codex app-server`, speaks
JSONL, sends `initialize`, sends the `initialized` notification, and calls
`account/rateLimits/read`. It obtains subscription rate limits without a model
prompt. The generated tag's notification method is exactly `initialized`, and
the read request omits `params` because its generated option is empty.

Implementation requirements:

- discover a compatible Codex binary explicitly and report the selected binary
  and protocol/version safely;
- supervise subprocess lifetime, timeouts, malformed JSONL and stderr without
  leaking sensitive content;
- identify windows using `windowDurationMins` and validated fields, not the
  `primary`/`secondary` position alone;
- validate `usedPercent`, preserve `resetsAt`, `planType` and reached status;
- require explicit current-backend `ordinaryUsageAllowed: true` before
  reporting usable ordinary quota pairs; false, null or missing permission is
  unknown, while a missing/null `spendControlReached` is clear only under that
  explicit permission;
- recognize `accountId`, `rateLimitUpsell` and `normalModelSlug` at the
  provider boundary without exposing them; a non-null upsell remains an
  upstream recovery blocker, but its presentation content is not part of v2;
- never fall back to browser-cookie scraping as an incidental convenience;
- fail with a clear unsupported/schema status if protocol behavior changes.

Status (M1): the collector is implemented and fixture-tested in
`scarcity_router/providers/openai_codex.py` (pure parser
`parse_codex_rate_limits_result` and the JSONL message classifier) and
`scarcity_router/providers/openai_codex_acquisition.py`
(`collect_openai_codex_capacity`). It performs deterministic read-only discovery
of the supported Codex installation, bounded app-server supervision, strict
JSONL validation, safe failure mapping and terminate/kill cleanup. The parser
validates the complete evidenced response envelope, typed credit and spend
states, reset-credit summaries, additional metered buckets and backend blockers.
Unrepresentable states degrade to `unknown` without inventing quota.

The current upstream app-server schema also defines `ordinaryUsageAllowed`,
`accountId`, `rateLimitUpsell` and `normalModelSlug`; these fields are
explicitly handled at the adapter edge without expanding v2 or weakening the
unknown-structured-field rule. Current upstream semantics are recorded in
`docs/decisions.md` U-011. The installed supported Codex binary remains an
older schema generation, and the 2026-09-05 live read returned a protocol
error before a quota result, so live OpenAI usability remains an M1 blocker.

Supported discovery is exactly the VS Code ChatGPT extension layout documented
in `docs/decisions.md` (U-001): non-symlink `openai.chatgpt-*` directories under
`~/.vscode/extensions` or `~/.vscode-server/extensions`, on currently supported
Linux x86-64 hosts, with a validated `codex-package.json` and executable. No
installation maps to `unavailable`; an installation whose layout cannot be
validated maps to `unsupported`.

The provisional `uv run python -m scarcity_router status` command composes this
collector with the Z.ai normalized snapshot without exposing discovery paths or
versions. The automated suite contains no live-account test and all transport
tests use synthetic JSONL process fakes and fixtures under
`tests/fixtures/openai-codex-appserver/`.

## Z.ai Coding Plan

The proven mechanism reads the existing Kilo provider entry identified as
`zai-coding-plan` (credential type `api`) and makes:

```text
GET https://api.z.ai/api/monitor/usage/quota/limit
Authorization: <existing credential>
```

The credential is transient input only. Before attaching it, the adapter must
require HTTPS and the exact expected host. Endpoint overrides must pass an
equally strict allowlist; arbitrary URLs are forbidden.

The observed response contains a plan level and a list of typed limits. In the
observed/current schema:

```text
unit=3, number=5 -> five-hour token window
unit=6, number=1 -> weekly token window
```

These are validated adapter mappings, not universal constants. Unknown units or
combinations must preserve safe metadata and yield unknown semantics rather than
being guessed. Array position is not semantic.

Status (M1): the response parser is implemented and fixture-tested in
`scarcity_router/providers/zai.py` (`parse_zai_quota_response`), and the secure
production acquisition shell is implemented and unit-tested in
`scarcity_router/providers/zai_acquisition.py` (`collect_zai_capacity`). It
performs strict credential discovery, fixed HTTPS destination validation, one
redirect-free bounded GET and safe failure mapping to the v2 statuses.

Each observed window carries `nextResetTime`, a 13-digit epoch-millisecond value,
and `percentage` is the used percentage. Both are provider evidence, not a
permanent contract; the authoritative record and fail-safe fixtures live in
`docs/poc-evidence.md` and `tests/fixtures/zai-coding-plan/`.

The currently observed Kilo auth location is `~/.local/share/kilo/auth.json`.
Discovery reads only the bounded auth file and selects only the
`zai-coding-plan` entry with the evidenced `type == "api"` and non-empty `key`
shape. It must never dump the file or return other entries. No second long-lived
token store is created.

The provisional status command composes this collector with the OpenAI
normalized snapshot. The automated suite contains no live credential-dependent
integration test; transport tests use mocked fakes and synthetic secrets.

## Later Providers

Claude subscription is the highest-priority later collector because it would
materially expand usefulness. It must not delay the initial two-provider scope
and must be preceded by an auth, security and maintenance evaluation. Other
providers are added based on user demand, stable telemetry, maintenance burden
and routing value, not a provider-count target.

## Contract-Test Expectations

For each adapter, freeze minimal redacted provider-shaped fixtures and assert:

- successful normalization;
- all active windows survive normalization;
- window semantics are classified only from validated evidence;
- missing/unknown semantics remain unknown;
- used/remaining validation and reset preservation;
- error/status mapping;
- no credential-shaped value appears in output, diagnostics or snapshots;
- provider changes do not crash the core;
- failures from one provider do not suppress the other provider's snapshot.

Live integration checks may complement fixtures, but tests must not require or
record the owner's credentials.
