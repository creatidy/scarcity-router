# Security model

## Security objective

Scarcity Router handles access to subscription telemetry, not model traffic. Its
small scope is a security feature: it has no reason to receive prompts, source
code or repository contents. The main assets are existing provider credentials,
account metadata, quota state and the integrity of routing policy.

## Trust boundaries

- Existing local credential/tool stores are outside the broker and remain the
  source of truth.
- Provider collectors are the only components allowed to use credentials.
- Normalized capacity, selector, catalog and interfaces are credential-free.
- CLI/REST/MCP consumers are untrusted with respect to secrets; they receive
  only normalized safe output.
- Remote provider endpoints are trusted only after scheme and exact-host
  validation.

## Absolute invariants

Never:

- print, log, serialize, return or expose an auth token, cookie or secret;
- include real credentials in fixtures, snapshots, telemetry, analytics,
  exceptions, command arguments, process listings or commits;
- send credentials, prompts, source code or repository data to another model;
- ask an LLM or agent to inspect a credential value;
- copy unrelated browser profile or authentication contents;
- create another long-lived credential store by default;
- attach a credential to an arbitrary user-provided URL;
- issue model prompts as part of quota collection;
- mutate provider quota/account state from a collector.

Credential files must be read as narrowly as possible. Prefer an authenticated
local protocol such as Codex app-server over extracting browser state.

## Network controls

- REST binds to `127.0.0.1` by default, never `0.0.0.0` implicitly.
- Stdio MCP is preferred for local agent integration.
- Authorization-bearing requests require HTTPS and an exact approved provider
  hostname. Validate before constructing/sending the authenticated request.
- Redirects must not carry Authorization to an unapproved origin. The safest
  initial behavior is to reject cross-origin redirects.
- Endpoint overrides are disabled or allowlisted strictly; a syntactically
  valid URL is insufficient.
- Remote exposure, CORS relaxation or LAN binding is a future explicit decision
  requiring authentication and threat analysis.

## Secret handling

Use secrets transiently and keep them in the narrowest scope. Do not cache token
values. Avoid passing them in command-line arguments or environment variables
when a safer in-process read/header is available. Do not include raw response
headers in logs.

Logging and diagnostics use allowlisted structured fields and stable reason
codes. Redaction is defense in depth, not permission to log arbitrary payloads.
Captured subprocess output must be reviewed because an upstream tool may emit
sensitive data unexpectedly.

If explicit credential configuration becomes necessary, prefer OS-native secure
storage. A permissioned file is a considered fallback, never a world-readable
default. Its format and migration require a recorded decision.

## Minimal filesystem access

OpenAI collection should interact with the chosen Codex process rather than
browser profiles. Z.ai discovery should parse only the configured Kilo auth file
and select only `zai-coding-plan`; it must not display or return other entries.
Ollama health should use local runtime interfaces and not inspect unrelated
model or user files.

## Public-interface data

REST, MCP, CLI and dashboard may expose:

- provider identifier and plan type;
- normalized capacity windows and reset times;
- health/status and safe diagnostic reason;
- selected model, alternatives and explanation;
- catalog/policy version.

They must not expose credential paths by default, raw Authorization material,
unredacted raw responses or unrelated account data. A verbose/debug mode does
not waive these rules.

## Testing requirements

- Use synthetic or manually redacted fixtures with conspicuous fake secrets.
- Assert fake secret markers do not appear in normalized output, logs, errors or
  interface responses.
- Test malicious endpoint overrides, HTTP downgrade, redirect to another host,
  malformed auth files, unexpected subprocess output and schema drift.
- Run secret scanning before release and ensure examples contain no real values.
- Verify local-only default binding.

## Incident behavior

If a secret may have been printed, stored, committed or sent to an unapproved
host, stop the collector, preserve only non-secret diagnostic evidence, notify
the user and recommend provider-appropriate revocation/rotation. Do not repeat
the suspected value while reporting the incident.

If a schema changes, fail closed for that collector: return `schema_changed` or
`unknown`; do not guess a healthy quota. Other collectors and the core continue.

## Out of scope for M1

Multi-user authentication, remote service exposure, centralized credential
management, analytics and organization-wide audit are not needed for the local
collector milestone.

