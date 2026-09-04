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
- never fall back to browser-cookie scraping as an incidental convenience;
- fail with a clear unsupported/schema status if protocol behavior changes.

The PoC used a binary included with a VS Code ChatGPT extension; production
binary discovery is narrowly resolved for M1 in `docs/decisions.md` (U-001),
and the minimum compatible version remains unresolved there.

Status (M1): the collector is implemented and fixture-tested in
`scarcity_router/providers/openai_codex.py` (pure parser
`parse_codex_rate_limits_result` and the JSONL message classifier) and
`scarcity_router/providers/openai_codex_acquisition.py`
(`collect_openai_codex_capacity`): deterministic read-only discovery of a
supported Codex installation, a bounded supervised `codex app-server`
subprocess with discarded stderr, the proven
initialize/initialized/`account/rateLimits/read` exchange with responses
matched by request identity and message structure (never timing), bounded
line/total output budgets, ambiguity-safe JSONL decoding (duplicate keys,
NaN/Infinity constants, non-finite exponents such as `1e10000` and
adversarial deep nesting all rejected as drift), bounded finite-positive
wait-supported startup/session
timeouts, and terminate→(bounded wait)→kill cleanup on every path —
including a reader startup failure before the session begins. Cleanup reports
`unavailable` whenever a bounded wait cannot prove that the child was reaped
or the reader thread was stopped; it never claims successful collection on
unproven cleanup.
The parser validates the complete evidenced response envelope (U-010):
only the `GetAccountRateLimitsResponse.rateLimits` member is required for input
deserialization; `rateLimitsByLimitId` and `rateLimitResetCredits` are nullable
optional members, so missing and explicit null states are accepted. Rust's
tagged serializer does not skip these `Option` fields and the TypeScript shape
requires both keys, so this input tolerance is not a claim about emitted wire
success shape. `primary`/`secondary` are the
only window slots; a present window requires i32 `usedPercent`, while nullable
i64 `windowDurationMins` and `resetsAt` are validated and duration drives
classification, never slot position. Typed states are validated: `credits` (`CreditsSnapshot` with
required booleans and optional string-or-null `balance`), `individualLimit`
(`SpendControlLimitSnapshot` with four required fields, string `limit`/`used`
and integer `remainingPercent`/`resetsAt`) and `rateLimitResetCredits`
(`availableCount` plus typed optional reset-credit rows; rows require `id`,
`resetType`, `status` and `grantedAt`, with nullable optional detail fields);
malformed shapes fail closed, and
valid credit or spend-control states — being v1-unrepresentable — degrade to
`unknown` with percentage pairs withheld. A valid reset-credit summary is
supplemental telemetry: its presence or `availableCount` does not block or
withhold current quota pairs. Additional `rateLimitsByLimitId` buckets
validate as full quota snapshots; a present map must contain an exact
`"codex"` mirror of top-level `rateLimits`, and non-mirror keys must equal
their bucket `limitId` and never be `"codex"`; every emitted bucket window gets a distinct safe
`<limitId>:<slot>` identity with no equal-period merging; same
window/duplicate/nested rules). Backend
blockers — a non-null `rateLimitReachedType`, an unavailable or true
`spendControlReached`, an exhausted `individualLimit`, or a blocked/exhausted additional
bucket — never yield a healthy snapshot and withhold the percentage pairs;
a present-but-unblocked bucket degrades to `unknown` with its validated
pairs. Plan labels accept every exact tagged `PlanType` member the v1
safe-ID grammar permits as-is (underscores included, e.g. `edu_plus`,
`enterprise_cbp_automation`, and the `unknown` catch-all), verbatim and
never rewritten; a present nonmember is `schema_changed`, never silently
omitted.

Supported discovery (U-001, evidence in `docs/poc-evidence.md`):
`openai.chatgpt-*` extension directories under `~/.vscode/extensions` or
`~/.vscode-server/extensions` (the remote-server layout is the directly
evidenced PoC environment), on currently supported Linux x86-64 hosts, highest
extension version first, validated by a
non-symlink candidate plus `bin/<platform>` directory, a regular non-symlink
executable `bin/<platform>/codex` and a
`codex-package.json` with
`layoutVersion` 1 and `variant` `codex` (duplicate-key rejecting; this pins
the installation filesystem layout, not protocol compatibility — protocol
drift is caught at runtime by strict shape validation failing closed). No
installation maps to `unavailable`; an installation whose layout cannot be
validated maps to `unsupported`.

Known compatibility limits: other installation sources (a `codex` on PATH,
npm installs, other editors) and a minimum/maximum codex version policy are
unsupported and unresolved (U-001 residual; Darwin is intentionally
unsupported until descriptor execution is evidenced); error-response text is never
parsed, so protocol error responses map to `unknown` until failure shapes
are captured as evidence; the selected binary path and versions are
validated but not surfaced, because v1 has no field for them (future
`doctor`/`status` work reports them); the reached enum uses the exact
snake_case generated-schema values, so camelCase and arbitrary strings are
rejected as drift; amount values inside credits/spend-control/reset-credit
members are validated structurally but never interpreted (documented
residual); live CLI/status integration
is not implemented, and the automated suite contains no live-account test —
all transport tests use synthetic JSONL process fakes and fixtures under
`tests/fixtures/openai-codex-appserver/`.

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
`scarcity_router/providers/zai.py` (`parse_zai_quota_response`), and the
secure production acquisition shell is implemented and unit-tested in
`scarcity_router/providers/zai_acquisition.py`
(`collect_zai_capacity`): strict discovery of the `zai-coding-plan` entry
(`type == "api"`, non-empty string `key`, sent as-is), destination validation
against the fixed endpoint before Authorization is attached, exactly one
redirect-free bounded GET, and safe failure mapping to the v1 statuses.
Live CLI/status integration is still not implemented, and the automated
suite contains no live credential-dependent integration test; all transport
tests use mocked fakes and synthetic secrets.

Each observed window carries `nextResetTime`, a 13-digit epoch-**millisecond**
value, and `percentage` is the **used** percentage (remaining is the complement).
Both are current provider evidence, not a permanent contract; the authoritative
record and the required fail-safe fixtures live in
`poc-evidence.md` and `tests/fixtures/zai-coding-plan/`.

The adapter needs fixtures for known windows, unknown windows, missing values,
invalid percentages, authentication failure and schema change.

The currently observed Kilo auth location is `~/.local/share/kilo/auth.json`.
Discovery reads only the bounded auth file and selects only the
`zai-coding-plan` entry with the evidenced `type == "api"`, non-empty `key`
shape; it must never dump the file or return other entries. A credential
value that cannot be represented safely as an HTTP header value (anything
outside printable ASCII) and any duplicate object key in the document
(ambiguous credential definitions) also fail closed to `auth_required`
before any request is made. An explicit safe path parameter exists for
deterministic tests and controlled local configuration; no second
long-lived token store is created.

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

Status (M1): the collector is implemented and fixture-tested in
`scarcity_router/providers/ollama.py` (pure parsers for the version probe,
the model listing and the loaded-model listing) and
`scarcity_router/providers/ollama_acquisition.py`
(`collect_ollama_capacity`): strict pre-I/O canonicalization of the
explicit local endpoint (plain `http` on exactly the numeric loopback
hosts `127.0.0.1` or `::1` only — `localhost` and every other name are
rejected, so no name resolution exists to race or escape through; the
omitted port canonically defaults to 11434, never an implicit socket
default; empty query/fragment delimiters, whitespace and control
characters are rejected; no LAN scanning, no internet access, no proxy
routing, no redirects), a required explicit target model identity matching
the v1 safe-ID grammar as a full string with no control characters (never
a hard-coded default), and at most three fixed read-only GETs against the
single canonical endpoint — two when the validated listing proves the
configured model absent: `/api/version` (the reachability probe),
`/api/tags` (exact-name model presence, with the listing's validated
`sha256` digest as identity evidence) and `/api/ps` (effective context,
only from a validated positive integer `context_length` on the configured
model's loaded entry whose validated digest agrees with the listing's — a
missing, invalid or mismatched digest preserves the validated
reachability/presence facts but degrades the telemetry to `unknown` and
omits the effective context). Response bodies decode under a strict JSON
contract (duplicate object keys at any depth, NaN/Infinity constants,
non-finite floats such as `1e10000`, integers outside the validated
signed 64-bit band, and recursion-limit nesting all map to
`schema_changed`), every transport result is narrowly protocol-validated
before use (integer HTTP status plus callable `read`; body chunks must be
`bytes`; a malformed response object or contract-violating chunk degrades
safely instead of raising), and one monotonic collection deadline is
enforced end to end and **cancellably**: each read executes inside a
bounded worker that the collector abandons at the deadline, cancelling it
by closing its connection (close unblocks every in-flight operation on a
socket), then reclaims the worker with a bounded join — so control
returns on time even when a single blocking call would run forever, and
no thread, socket or file descriptor is left behind. Response-operation
failures of any kind — including provider-controlled exception text —
normalize to safe outcomes and are never propagated or logged; an
unexpected internal worker error is re-raised rather than swallowed.
Healthy local runtimes report `windows: []` with no quota
semantics. The configured context is accepted only as an explicit
configuration parameter and is never inferred; the effective context is
never inferred from the configured value or from the listing's model-file
metadata. Duplicate model names, non-object listing entries, malformed
bodies and drifted envelopes fail closed
(`schema_changed`/`unknown`/`unavailable` per the failure mapping in the
module), `model_presence` stays `unknown` on runtime/listing failures,
`/api/ps` supplemental failures preserve the tags-derived presence while
omitting the effective context, and a runtime that explicitly lists its
models without the configured target reports `missing`. Evidence and
limitations: `docs/poc-evidence.md`
("2026-09-04 M1 Ollama local runtime reconnaissance") — the populated
effective-context path is synthetic-fixture-tested only until a naturally
loaded model is observed, and the cross-version stability of
`context_length` is explicitly unvalidated; live CLI/status integration
is not implemented.

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
