# M3 Live Acceptance

## Scope

- Acceptance date (UTC): 2026-09-07
- Tested integration SHA: `a8e5b3f1600dfcc9a255a7bfde51959690574ae2`
- Acceptance issue: `#54`
- Acceptance branch: `m3-closeout`
- The tested source contained the merged M3c head and the required M3c
  ancestor `89130b2997ef3c96636f9edd5ed6aee9d0ddcfb8`.

## Regression

- Full suite: 885 tests passed.
- `basedpyright`: 0 errors, 0 warnings, 0 notes.
- `git diff --check`: passed.
- `model-catalog.json` and `model-policy.json` were unchanged during
  acceptance.
- No provider, capacity, selector, catalog or policy implementation changes
  were made for closeout.

## Live Providers

- OpenAI: `ok`; normalized CapacitySnapshot schema v3 observed through the
  Codex app-server path.
- Z.ai: `ok`; normalized CapacitySnapshot schema v3 observed through the
  Coding Plan usage path.
- The public semantic scopes `openai/codex` and `zai/coding_plan` were
  observed. Additional quota values, reset times and non-public scope
  identifiers are intentionally not recorded.
- Only normalized, allowlisted output was retained; raw provider responses,
  account identifiers and credential data were not retained.

## CLI

- `status --json` returned valid JSON containing exactly OpenAI and Z.ai
  snapshots with schema v3 and safe normalized fields.
- Live profiles exercised: `routine_coding`, `deep_coding`,
  `scientific_review`, `orchestration` and `translation`.
- Live selections were capability-eligible, had balanced policy provenance and
  reconstructable rankings. Public selected identities were Luna for routine
  coding and orchestration, and Sol for deep coding, scientific review and
  translation.
- Empty/no-change simulation preserved the current decision.
- A 98/2-style synthetic Z.ai weekly capacity override was applied to copied
  simulation inputs; the live baseline remained preserved and no snapshot was
  mutated.

## REST

- `GET /healthz`, `GET /v1/status`, `POST /v1/select` and
  `POST /v1/simulate` succeeded against the default server.
- The server bound to `127.0.0.1:8765`; no LAN bind was used.
- Status returned the v1 envelope containing two v3 snapshots.
- Select returned a v1 envelope with a valid balanced decision; the live deep
  coding result selected public Sol.
- Simulate returned a v1 envelope with baseline and simulated decisions using
  the synthetic Z.ai weekly override.
- A foreign `Host` header was rejected with HTTP 400 and the fixed
  `invalid_request` envelope.
- No live no-solution or degraded-provider result was encountered; their HTTP
  200 domain semantics remain covered by deterministic tests.
- The server emitted no request logging, released port 8765 on shutdown and
  left no process behind.

## MCP

- The official MCP client launched `uv run python -m scarcity_router.mcp` over
  stdio and completed initialization.
- `tools/list` returned exactly `scarcity_status`, `scarcity_select` and
  `scarcity_simulate`; no resources or prompts were advertised.
- All three tools returned successful structured v1 logical envelopes over
  fresh OpenAI and Z.ai telemetry.
- Live deep coding selection selected public Sol.
- Live routine simulation preserved public Luna as both baseline and
  simulated selection while applying the synthetic Z.ai weekly override.
- Structured content was present for each successful result and its single
  deterministic JSON text block matched the structured payload.
- An unknown profile returned `is_error = true` with the fixed safe
  `invalid_request` payload.
- The stdio client closed cleanly; no MCP process or network listener remained.

## Consistency And Boundary

- CLI, REST and MCP exposed the same provider set, machine-interface envelope
  version and capacity schema version, with the same semantic scope vocabulary
  and no provider-specific raw payload leakage.
- Equivalent live deep coding selections all selected public Sol. Equivalent
  routine simulations all preserved public Luna and applied the same typed
  Z.ai weekly override. No unexplained freshness variation was observed; raw
  fresh percentages and timestamps were not compared or recorded.
- Deterministic parity evidence remains authoritative in
  `tests/test_mcp.py`:
  `StatusTests.test_status_matches_rest_and_cli_and_direct`,
  `SelectTests.test_profile_and_explicit_requirement_paths` and
  `SimulationTests.test_empty_and_98_2_simulations_match_direct_cli_rest_and_mcp`.
- Additional direct application parity evidence remains in
  `tests/test_server.py`:
  `ParityTests.test_rest_select_equals_direct_application`,
  `ParityTests.test_rest_simulate_equals_direct_application`,
  `ParityTests.test_rest_status_equals_direct_collection` and
  `ParityTests.test_file_based_cli_matches_typed_seam`.
- Credentials leaked: no.
- Raw provider data leaked: no.
- Model execution observed: no.
- Prompt proxy observed: no.
- Automatic dispatch observed: no.
- Local inference was not used or added.

## Exit Gates

```text
M3-1 Regression = PASS
M3-2 CLI live = PASS
M3-3 REST live = PASS
M3-4 MCP live = PASS
M3-5 Interface consistency = PASS
M3-6 Product boundary = PASS
M3 = PASS
```

The next planned milestone is M4, minimal dashboard and integration recipes.
Astra onboarding issue `#49` remains independent and was not folded into M3.
No M4 implementation or installable-package/release readiness is claimed.
