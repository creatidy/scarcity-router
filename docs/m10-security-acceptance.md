# M10 security acceptance matrix

Issue #95, mapped to the D-044 threat model
([`docs/security.md`](security.md), execution-gateway section). Security
failures here are program blockers. Every row names the M10 test that
re-proves the property through a real surface (composed server, real
worker endpoint, real TLS) and, where one exists, the earlier module
suite that owns the deepest coverage.

Status legend: **pass** — automated, deterministic, run on every
`make check`/`make test`; **gate** — an external gate recorded in
[`docs/m10-acceptance.md`](m10-acceptance.md) that this repository
cannot honestly run.

| # | Scenario | M10 evidence | Prior coverage |
| --- | --- | --- | --- |
| 1 | Unauthenticated administrator/control access rejected | `AuthBoundaryTests.test_unauthenticated_control_and_admin_are_rejected` (`tests/test_security_acceptance.py`) — 401 on every `/control/**` read; UI redirects unauthenticated browsers | `tests/test_m09_server.py`, `tests/test_server_ui.py` |
| 2 | First-run onboarding cannot be replayed (no default credential; bootstrap refused after first use) | `AuthBoundaryTests.test_second_bootstrap_is_refused_forever` (409) | `tests/test_control_api.py` |
| 3 | Inference-client key cannot administer | `AuthBoundaryTests.test_inference_client_key_cannot_administer` — bearer key on `/control/**` is 401 | `tests/test_control_api.py` |
| 4 | Administrator session cannot execute inference (identity classes never collapsed) | `AuthBoundaryTests.test_admin_session_cannot_execute_inference` — session cookie on `/v1/**` is 401 | `tests/test_control_api.py` |
| 5 | Administrator session mutations require CSRF | `AuthBoundaryTests.test_mutations_without_csrf_are_refused` (403 without `X-Scarcity-CSRF`) | `tests/test_control_api.py`, `tests/test_server_ui.py` |
| 6 | Wrong administrator password rejected (PBKDF2 verifier, no default password) | `AuthBoundaryTests.test_wrong_admin_password_is_rejected` (401) | `tests/test_control_api.py` |
| 7 | Revoked inference key rejected immediately, no redeployment | `RevocationTests.test_revoked_inference_key_is_rejected_immediately` | `tests/test_control_api.py` |
| 8 | Revoked worker credential rejected on next connection | `RevocationTests.test_revoked_worker_credential_is_rejected_on_next_connection` — typed `credential_revoked` error over a real socket | `tests/test_worker_endpoint.py` |
| 9 | Verified TLS for non-loopback; worker verifies the server; wrong CA / wrong hostname / expired certificate fail; no `verify=false` anywhere | `tests/test_tls_acceptance.py` — `ComposedServerTlsTests` (right CA serves, wrong CA/expired/wrong-hostname refused, plaintext refused), `WorkerTlsVerificationTests` (verifying default context, wrong CA refused on the real socket), `NoBypassSourceTests` (AST scan: no `verify=False`, no `CERT_NONE`, no hostname-check disabling, no unverified-context factory in product code) | `tests/test_worker_client.py` (origin discipline), `tests/test_openai_http_adapter.py` (origin binding) |
| 10 | Redirect credential-exfiltration refused (credentials bound to configured origins) | `NetworkDisciplineTests.test_cross_origin_redirect_is_refused_and_never_followed` — 302 to another host: exactly one outbound request, credential never follows | `tests/test_openai_http_adapter.py::test_redirects_are_refused_and_never_followed` |
| 11 | SSRF: clients cannot supply provider URLs or credentials | `NetworkDisciplineTests.test_client_requests_carry_no_provider_url_or_credentials` — unknown request fields refused (`unknown_parameter`); dispatch parameters come only from administrator configuration | `tests/test_gateway_server.py` |
| 12 | Router-loop protection (own origin / gateway marker refused in both directions) | `NetworkDisciplineTests.test_router_loop_marker_is_refused_at_ingress`, `NetworkDisciplineTests.test_configuring_the_router_as_its_own_provider_is_refused` | `tests/test_gateway_server.py`, `tests/test_control_api.py` |
| 13 | Plain HTTP only for explicit loopback origins; credentials never travel in URLs | `NetworkDisciplineTests.test_plain_http_non_loopback_provider_is_refused`, `NetworkDisciplineTests.test_credentials_in_urls_are_refused` | `tests/test_server_config.py` |
| 14 | Exact resource ownership (D-049): a worker can neither report nor execute another worker's resource | `tests/test_e2e_execution.py::WorkerBridgedExecutionTests.test_scenario_10_ownership_is_exact` (hijacked report rejected; dispatch reaches only the owner); `CancellationTests.test_scenario_11_cancel_reaches_exactly_the_owning_worker` (a second session never receives anything) | `tests/test_worker_endpoint.py`, `tests/test_worker_ownership.py` |
| 15 | No generic shell/ssh/command surface (closed protocol vocabulary) | `ProtocolVocabularyTests.test_worker_protocol_rejects_arbitrary_command_messages` (unknown `shell` message is a typed rejection), `ProtocolVocabularyTests.test_no_shell_ssh_endpoint_exists_on_any_http_surface` (no `/shell`, `/ssh`, `/exec` route anywhere) | `tests/test_worker_endpoint.py::test_unknown_arbitrary_command_message_is_rejected` |
| 16 | Admission limits: oversized request body refused before dispatch | `AdmissionBoundTests.test_oversized_request_body_is_refused` (413 `request_too_large`) | `tests/test_gateway_server.py` |
| 17 | Malformed/deep input fails safely (no traceback, safe structural error) | `AdmissionBoundTests.test_deeply_nested_json_is_refused`, `AdmissionBoundTests.test_malformed_json_is_refused_without_traceback` (400 `invalid_json`); depth bombs on the worker protocol: `tests/test_worker_endpoint.py::test_json_depth_bomb_is_rejected_without_leaking_the_session` | `tests/test_gateway_server.py` |
| 18 | Prompt/response content absent from audit, export and diagnostics (server side) after real execution | `tests/test_e2e_acceptance.py::DiagnosticsAndExportTests.test_scenario_16_export_audit_and_diagnostics_are_secret_free` — a real dispatch carrying conspicuous markers leaves none in the audit table, `/control/export` or `/control/diagnostics` | `tests/test_control_api.py`, `tests/test_gateway_audit.py` |
| 19 | Secret-free worker diagnostics (local runtimes accounted for, not only server logs) | `tests/test_worker_client.py::RuntimeTests::test_diagnostics_redact_the_credential` (credential never in worker diagnostics); the M10 e2e worker flows reuse the same redacted `diagnostics()` surface for the bounded worker log (`scarcity_router/windows_tray.py::append_worker_log` writes only those lines) | `tests/test_worker_client.py` |
| 20 | Provider credentials never exported, never listed, never returned | `tests/test_e2e_acceptance.py::DiagnosticsAndExportTests.test_scenario_16_...` (export/diagnostics redaction); export is secret-free by construction (`server_config.py` carries no credentials) | `tests/test_control_api.py` |
| 21 | Malformed/future configuration fails closed | `StoreDisciplineTests.test_future_configuration_schema_fails_closed`; store with a future schema version refuses to open: `tests/test_e2e_acceptance.py::RestartPersistenceTests.test_scenario_13_...` | `tests/test_server_store.py` |
| 22 | Unsafe file permissions detected/refused (`0o600`/`0o700` discipline) | `StoreDisciplineTests.test_store_permissions_are_enforced` (store forced 0600/dir 0700), `StoreDisciplineTests.test_loose_client_keys_file_is_refused` (0644 keys file refused); `tests/test_e2e_acceptance.py::RestartPersistenceTests.test_client_keys_file_permissions_are_enforced` | `tests/test_gateway_server.py`, `tests/test_server_store.py` |
| 23 | Worker local store rejects unknown schema (fail closed) | `StoreDisciplineTests.test_worker_store_rejects_unknown_schema` | `tests/test_worker_local_store.py` |
| 24 | Stale/future store schema migration refused | `tests/test_e2e_acceptance.py::RestartPersistenceTests.test_scenario_13_...` (`require_current_schema` refuses version > current) | `tests/test_server_store.py` |
| 25 | Session/token storage discipline (hashes only, constant-time compare, HttpOnly/SameSite cookies) | `tests/test_e2e_acceptance.py::OnboardingTests.test_scenario_03_...` (cookie flags, logout invalidation, client key stored as 64-hex hash only) | `tests/test_control_api.py`, `tests/test_server_store.py` |
| 26 | Bearer secrets never in URLs; key redacted from client representations | `tests/test_interfaces_guardrails.py` (RemoteServerConfig repr redaction, verified TLS only) — re-proven implicitly by every M10 flow that authenticates via the `Authorization` header only | `tests/test_interfaces_guardrails.py` |

## Gates (cannot be honestly run in this repository)

| Gate | Label | Why it cannot run here | Where recorded |
| --- | --- | --- | --- |
| Windows code signing | `EXTERNAL_RELEASE_GATE: WINDOWS_CODE_SIGNING` | Signing requires an owner-held certificate; the release workflow's MSIX step stays fail-closed until the certificate AND implementation evidence exist. No signature is ever fabricated. | `.github/workflows/release.yml` (`build-windows-worker` job), `docs/m10-acceptance.md` |
| Live Windows acceptance | `EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE` | No Windows host in this environment; the PyInstaller artifact, tray icon and packaged worker UX are verified statically (spec shape, lazy imports, state machine, assembly) and must be exercised on a real Windows 11 host. | `docs/m10-acceptance.md`, `tests/test_windows_packaging.py` |

## Verdict

All rows above marked **pass** were green at the time of writing under
`uv run python -m unittest tests.test_e2e_acceptance
tests.test_e2e_execution tests.test_tls_acceptance
tests.test_security_acceptance` (plus the full `make test` gate), on a
deterministic, synthetic, loopback-only, quota-free setup. No paid
provider test exists; none is desired (opt-in remains the only path, and
M10 adds none).
