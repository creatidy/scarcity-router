# M10 acceptance record

Issue #95 — distribution, installation, update path and end-to-end
acceptance (M10-A scope: everything independent of the final Codex
adapter; M10-B adds Codex-dependent acceptance on the integration
branch). This document is the honest record: what is built and tested,
what is a recorded seam, and what this repository cannot claim. Platform
honesty everywhere — nothing below claims a capability that was not
built or tested.

## Platform support table

| Capability | Status | Evidence |
| --- | --- | --- |
| Wheel + sdist distribution (D-034) | built/tested | `make package-check` (build, contents/metadata inspection, isolated `uv tool` install, console-script help, packaged resources, MCP discovery, loopback REST healthz) — green |
| `scarcity-router`, `scarcity-router-mcp`, `scarcity-router-server`, `scarcity-router-worker` console scripts | built/tested | `tools/package_check.py` verifies exactly these four entry points and their `--help` |
| Recommendation-only installation without server/worker/Docker | built/tested | unchanged D-034 path; `tests/test_e2e_acceptance.py::RecommendationOnlySurfaceTests` |
| Linux/WSL Python install: `uv tool install scarcity-router` (post-PyPI), `pipx` alternative | contracted, not published | future contract in `docs/release-engineering.md`; today's working path is checkout/wheel install (`make install`) |
| Linux worker background operation (systemd user service) | built/documented | `examples/scarcity-router-worker.service` with start/stop/status/logs recipes and `loginctl enable-linger`; commands verified against the real worker CLI |
| Server container image | built/tested locally | `Dockerfile` (python:3.12-slim, multi-stage wheel build, non-root uid/gid 10001, `/data` volume, `HEALTHCHECK` on `/healthz`); verified live on this host (Docker 29.7.2): build, `/healthz` 200, healthy status, single process, restart persistence (second bootstrap 409), non-root user, verified-TLS bind 200 / wrong CA refused / plaintext refused |
| Compose example | built/documented | `examples/docker-compose.yml` — loopback host-networking default, TLS/LAN variant using only real server flags |
| GHCR image publication (`ghcr.io/creatidy/scarcity-router`, tags X.Y.Z/X.Y/latest) | future contract | `docs/release-engineering.md` — no image job, no registry push, nothing published |
| Windows worker package (portable zip) | workflow seam | `.github/workflows/release.yml` `build-windows-worker` (windows-latest, exact tagged commit, PyInstaller one-dir, `scarcity-worker-X.Y.Z-windows-x64.zip`, checksummed in `SHA256SUMS`, attached to the release); spec shape verified statically (`tests/test_windows_packaging.py`) |
| Windows worker MSIX (signed) | external gate | `EXTERNAL_RELEASE_GATE: WINDOWS_CODE_SIGNING` — fail-closed seam; no signature ever fabricated |
| Windows tray UX | built/tested (logic), adapter Windows-gated | state machine/run-loop/log unit-tested everywhere (`tests/test_windows_tray.py`); pystray adapter lazy-imports and refuses off-Windows (`tests/test_windows_packaging.py::TrayViewAdapterTests`); `EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE` |
| Windows first-run onboarding (setup dialog, packaged `pair` command, local settings — issue #113, D-051) | built/tested (logic), adapter Windows-gated | entry routing, cancel/partial-failure semantics, real pairing path, code non-persistence, settings strictness/precedence/restart, loopback-only Ollama, no-codex advertising all unit-tested on every platform (`tests/test_worker_first_run.py`); tkinter dialog adapter lazy-imports (`tests/test_windows_packaging.py::SetupViewAdapterTests`); packaged-CLI output path (`_attach_parent_console`) and the live double-click flow are covered by `EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE` |
| Update path | built/documented | `uv tool upgrade scarcity-router` / container tag pull + `docker compose up -d` / reinstall the worker package; no background auto-update, no remote code execution |
| Version surface | built/tested | `scarcity-router --version`; server startup banner; `get_version()` single-source literal |
| Codex execution path (worker-local adapter, subscription-included) | built/tested (deterministic e2e; live subscription gated) | `tests/test_e2e_codex_acceptance.py` + `tests/test_codex_real_binary_probe.py` (M10-B section below); `EXTERNAL_ACCEPTANCE_GATE: LIVE_CODEX_SUBSCRIPTION`; Windows-native Codex unevidenced (`platform_not_evidenced` by design) |

## Issue #95 acceptance-criteria mapping

Every scenario in the issue's acceptance list, with its honest
disposition. **covered** — automated test(s) named; **documented** —
implemented and documented, with no meaningful automated assertion to
make; **gate** — requires an environment this repository does not have;
**deferred** — explicitly moved to a tracked follow-up.

| # | Scenario | Disposition | Evidence |
| --- | --- | --- | --- |
| 1 | Installation from README verified on clean environments | covered (scoped) | `make package-check` installs the built wheel into a throwaway `uv tool` environment from a clean HOME and exercises the installed surface; the committed-tree proof extracts `git archive HEAD` and runs the packaging suites in that fresh checkout. What remains outside automation: a human following the README prose on brand-new OS installs (recorded under the gates below) |
| 2 | API-only server with no worker | covered | `tests/test_e2e_acceptance.py` scenarios 2–5 (startup/liveness, onboarding, client keys, execution ingress); honest-empty default: `tests/test_program_integration.py::HonestEmptyDefaultTests` |
| 3 | Server and worker on one machine | covered | `tests/test_e2e_execution.py` scenarios 8–10 (pairing, worker-bridged execution, ownership) — server and worker share the loopback host |
| 4 | Server and worker on different hosts | gate | the transport between server and worker is the same verified-TLS outbound connection in both cases, and non-loopback TLS + worker origin discipline are tested (`tests/test_tls_acceptance.py`, `tests/test_worker_client.py::WorkerOriginTests`); a run across physically separate hosts is not automatable here — record it with `EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE`'s host-diversity follow-up or M10-B |
| 5 | Client access through LAN/VPN | gate | LAN exposure path is exactly the verified-TLS bind (`tests/test_tls_acceptance.py`) plus bearer authentication (`tests/test_security_acceptance.py`); a real LAN/VPN client run requires a second machine — same gates as #4 |
| 6 | Windows 11 + WSL | gate | `EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE`; per-platform worker state-dir resolution is unit-tested (`tests/test_worker_local_store.py`) |
| 7 | Linux accessed through SSH | documented | SSH is how a user reaches the machine, never a router protocol; the Linux worker background path is the systemd user unit (`examples/scarcity-router-worker.service`) with `loginctl enable-linger` for headless operation — documented commands verified against the real CLI; no server-side SSH surface exists to test (asserted: `tests/test_security_acceptance.py::ProtocolVocabularyTests`) |
| 8 | Server restart | covered | `tests/test_e2e_acceptance.py::RestartPersistenceTests.test_scenario_13_...` (store reopen, identities/keys/config/credential survive, migration idempotent, future schema refused); container restart with a persistent volume verified live during M10 |
| 9 | Worker restart | covered | `tests/test_worker_client.py::RunLoopTests::test_reconnect_budget_is_bounded` (bounded reconnect with backoff), `tests/test_worker_client.py::RotationPersistenceTests::test_rotated_credential_is_stored_and_used_on_reconnect` (identity survives, reconnect re-authenticates), `tests/test_windows_tray.py::RunTrayWorkerTests::test_restart_spawns_a_fresh_runtime_without_overlap` (tray restart), `tests/test_e2e_execution.py::AmbiguousDisconnectTests` (restart after ambiguity never duplicates execution) |
| 10 | Worker offline | covered | dispatch refuses an offline owner: `tests/test_worker_bridged_adapter.py::DispatchTests::test_worker_offline_is_permanent_before_dispatch`; real connection state in diagnostics: `tests/test_program_integration.py::DiagnosticsRealStateTests::test_diagnostics_show_worker_connection_state` |
| 11 | Revoked credential | covered | `tests/test_security_acceptance.py::RevocationTests` (client key + worker credential, immediate effect over real surfaces) |
| 12 | Exhausted quota | covered | recommendation level: `tests/test_selector.py::test_exhausted_zai_does_not_bypass_capacity_for_any_effort` (`capacity_exhausted`), `tests/test_resource_policy.py::test_scenario_f_exhausted_openai_with_reset_credit`; execution admission: eligibility gates refuse (`tests/test_selector_eligibility.py` `included_window_exhausted`) |
| 13 | Invalid certificate/identity | covered | `tests/test_tls_acceptance.py` (wrong CA, wrong hostname, expired certificate, revoked worker credential) |
| 14 | Version upgrade | covered (scoped) | store-schema upgrade discipline is explicit and tested (`tests/test_e2e_acceptance.py::RestartPersistenceTests`, migration v1→v2 in `tests/test_program_integration.py::MigrationFromSchemaOneTests`); the package update command is documented (`uv tool upgrade`); a real old-release→new-release upgrade cannot exist before the first release — the first actual upgrade exercise is post-first-release/M10-B |
| 15 | Recommendation-only installable without server/worker/Docker | covered | `tests/test_e2e_acceptance.py::RecommendationOnlySurfaceTests`; `make package-check` isolated install needs none of the three |
| 16 | First-run friction measured and reported | covered | `docs/m10-acceptance.md` first-run sequences (2 / 8 / 5 steps) with friction notes |
| 17 | Paid-provider tests are explicit opt-in only | covered | no paid-provider test exists in the suite; every M10 test is synthetic/loopback (asserted by review of `tests/m10_fixtures.py` and suites; nothing contacts a provider) |
| 18 | Security acceptance scenarios pass | covered | `docs/m10-security-acceptance.md` matrix (26 rows, each mapped to its test) |
| 19 | Full validation gate green; CI covers build, package and deterministic suites | covered | `make check` + `make package-check` green on the committed state; Forgejo `ci` job `check` runs exactly these; the windows-worker release job adds the tag-driven build path |

## External gates

| Label | Meaning | Owner action |
| --- | --- | --- |
| `EXTERNAL_RELEASE_GATE: WINDOWS_CODE_SIGNING` | MSIX packaging/signing stays a fail-closed seam in the release workflow until a certificate and implementation evidence exist | configure the signing secret only together with the evidenced MSIX work |
| `EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE` | the produced Windows worker package (installer UX, tray icon, autostart, uninstall) has NOT been exercised on a real Windows 11 host | run the release-built package on Windows 11 and record evidence |
| `EXTERNAL_ACCEPTANCE_GATE: LIVE_CODEX_SUBSCRIPTION` | live, turn-level Codex execution against a provider-managed login in a dedicated controlled `CODEX_HOME` has NOT been run (see the M10-B section) | run one official `codex login` against the controlled home, then re-run the live smoke and record the evidence here |
| `PYPI_TRUSTED_PUBLISHER` | PyPI publication stays workflow-gated and NOT executed; the owner actions (PyPI Trusted Publisher entry, protected `pypi` environment, Forgejo runner, `develop` branch protection) are recorded in `docs/release-engineering.md` — never claimed configured | complete the owner checklist before the first release; until then `publish-pypi` fails closed by design |

## First-run sequences (measured, matching the implemented commands)

### Recommendation-only (default product; no server, worker or Docker)

1. `uv tool install scarcity-router` *(after the first PyPI release; today: `uv tool install /path/to/checkout` or `make install`)*
2. `scarcity-router status` — done.

**Measured friction: 2 commands.** Optional: `scarcity-router install-config`
installs the default user policy; `scarcity-router-mcp` needs no flags.

### Server (container)

1. `docker build -t scarcity-router .` (pre-release; post-release the image comes from GHCR by tag)
2. `docker compose up -d` (loopback default)
3. Open `http://127.0.0.1:8787/admin`
4. Create the administrator password (forced onboarding form)
5. Add a provider endpoint (+ credential) — Providers page
6. Add a resource bound to that endpoint — Resources page
7. Issue a client key — Clients page (revealed exactly once)
8. Point an OpenAI-compatible client at the server (base URL, key, alias
   or `sr-pin:` reference — copyable from the UI)

**Measured friction: 8 steps** (3 commands/forms are copy-paste from the
docs; the UI is server-rendered HTML with no JavaScript requirement).

Friction notes (measured, deliberate):

- LAN/VPN exposure requires TLS certificates mounted into the container
  and `--host 0.0.0.0` (D-044: no plaintext LAN). The mounted key must
  be readable by the container's uid 10001 — the compose example
  documents this; it is the single most likely first-run stumble.
- Any Docker Desktop-backed engine — including Docker Desktop's WSL2
  integration — does not share the host loopback, so the loopback default
  requires the TLS variant there. On a WSL2 distro backed by Docker
  Desktop, `network_mode: host` behaves like Docker Desktop (the server
  did NOT surface on the WSL distro's own loopback; verified with
  `curl` returning 000 on `127.0.0.1:8787`), not like a native Linux
  engine where host networking shares the host namespace.
- Changing the container's internal port also requires overriding the
  baked `HEALTHCHECK` (documented in the Dockerfile).

### Worker (Windows or Linux)

1. Administrator: Workers page → issue one-time pairing code
2. Worker host: install (`uv tool install`/`pipx`; Windows: unzip the release package)
3. Pair — Linux/WSL: `scarcity-router-worker pair --server srws://HOST:8790 --code CODE` (redeemed over verified TLS); **Windows: double-click `scarcity-worker.exe` and enter the server origin + code in the first-run setup dialog** (or, from PowerShell/cmd: `scarcity-worker.exe pair --server srws://HOST:8790 --code CODE` — the packaged executable speaks the same pairing protocol path; no Python required)
4. Enable local resources — Linux: `scarcity-router-worker run --allow-ollama --resource my-ollama`; **Windows: tick "Enable local Ollama" in the same dialog (loopback only) or later via the tray's "Worker settings..." action**, then start the executable
5. Administrator: add a `worker_bridged` resource bound to that device

**Measured friction: 5 steps** (unchanged; on Windows steps 3–4 collapse
into one dialog). The Windows package additionally accepts
`--server-ui-url` so its tray action opens the right control UI when the
server does not use default ports; the first-run dialog can also store
that origin (persisted, non-secret, in the worker's local settings).

Windows package specifics (issue #113): the ZIP contains only
`scarcity-worker.exe`. A no-arg launch of an unpaired worker opens the
first-run setup dialog; a paired worker starts the tray directly
(pair-only is valid — a local adapter can be enabled later through the
tray). Cancelling the dialog before pairing persists nothing. The
dialog and the packaged `pair` command share one pairing
implementation (`WorkerRuntime.pair`); local settings are typed,
versioned and non-secret; a malformed settings document fails closed
into a recoverable settings state. Windows-native Codex execution is
not offered in the dialog (platform_not_evidenced for v0.1.0; the
evidenced path is Linux/WSL via the CLI flags). Record decision:
D-051 (docs/decisions.md).

## What M10 verified end to end

Sixty acceptance tests plus the M10-B Codex suites
(`tests/test_e2e_codex_acceptance.py`,
`tests/test_codex_real_binary_probe.py`;
`tests/test_e2e_acceptance.py`,
`tests/test_e2e_execution.py`, `tests/test_tls_acceptance.py`,
`tests/test_security_acceptance.py`) cover the sixteen mission
scenarios — recommendation-only surface, server startup/liveness,
onboarding, client keys, execution ingress (streaming and
non-streaming), generic HTTP provider, direct Ollama preset, worker
pairing over verified TLS, worker-bridged Ollama execution, exact
resource ownership, cancellation propagation, ambiguous-disconnect
no-duplicate semantics, restart/persistence with schema discipline,
remote M08 bridge, diagnostics/doctor, and secret-free export — plus
the Codex end-to-end layer (packaged-style worker path, Codex resource
composition and ownership, auth-verdict eligibility, Codex execution
streaming and non-streaming, disconnect→`turn/interrupt`, effort and
structured-output binding, pre-dispatch tool rejection, mid-execution
worker-loss ambiguity, usage honesty, leakage and isolation asserts,
and the structure-only real-binary probe) and the TLS and security
matrices in
[`docs/m10-security-acceptance.md`](m10-security-acceptance.md). All
deterministic, synthetic, loopback-only, quota-free; no paid provider
test exists and none is added (explicit opt-in remains the only path).

Notable fail-closed behaviors verified along the way (documents reality;
no code was changed to soften them):

- a streaming request against a backend whose compatibility cells carry
  no evidence is refused with `compatibility_unknown` before any
  inference (D-043); the composed deployment builds its matrix from
  configuration through the existing evidence modules (M04 preset
  evidence; the reviewed M06 Codex evidence cells), so an un-evidenced
  preset (e.g. `generic-openai`, all-UNKNOWN) still fails closed;
- a pinned reference naming a physical model the configured resource
  does not represent fails closed — Codex resources bind the SHIPPED
  catalog through their physical model identity
  (`openai`/`gpt-5.6-sol` with the calibrated variants; `codex` is the
  execution surface, never a model), so a composed Codex resource is
  admittable for every calibrated model in the shipped catalog with NO
  administrator catalog invention. Curated capability calibration for
  NEW Codex models remains D-025 governance; the earlier gap tracked as
  Forgejo issue BioMedical-IT/scarcity-router#107 (an `(openai,
  "codex")` catalog entry) is resolved by this physical-identity
  binding rather than by inventing entries;
- recording administrator compatibility-matrix evidence through the
  control surface (narrowing or elevating a built-in evidenced cell
  with the administrator's own dated evidence) is NOT implemented —
  built-in adapter evidence is the default ceiling on the composed path
  (M04 preset cells and the reviewed M06 Codex cells are wired in
  `server_composition`). The composed-matrix wiring itself was
  delivered and closed under Forgejo issue
  BioMedical-IT/scarcity-router#106 (2026-09-21, with the authority
  model recorded: nothing in configuration can elevate above built-in
  evidence); administrator-supplied elevation above built-in evidence
  remains a possible future enhancement, not needed for any currently
  evidenced adapter.

## M10-B: Codex end-to-end acceptance

M10-B adds the Codex-dependent acceptance layer on the integration
branch (M06 Codex worker-local adapter + M10-A distribution/acceptance
both merged). It exercises Codex through the FULL composed stacks —
OpenAI-compatible client → server coordinator → real worker protocol →
``CodexLocalAdapter`` → fake App Server — deterministically and without
any live Codex backend. The fake App Server
(`tests/codex_fake_appserver.py`) remains the ONLY Codex backend in
CI-reachable tests; every scripted string is synthetic.

Integration-blocker dispositions recorded here (2026-09-21):

- **Physical model identity (D-042).** The Codex resource binds the
  SHIPPED catalog by its physical model: the worker requires
  `--codex-model <slug>` with `--allow-codex`, the registration names
  the same slug, and the adapter rejects any selected model outside its
  configured resource BEFORE any process spawn. The fake App Server
  advertises the real slug `gpt-5.6-sol` with the shipped calibrated
  efforts (`medium`/`high`), and every suite uses the SHIPPED
  `model-catalog.json` — the synthetic `(openai, "codex")` catalog entry
  and the `model="codex"` resource identity are gone (issue #107
  resolved by binding, not by catalog invention).
- **Production compatibility matrix.** The composed server builds its
  matrix from administrator configuration through
  `server_composition`: M04 `default_cells_for` for each bound
  server-direct resource, and the reviewed M06 Codex evidence cells
  (`scarcity_router/codex_worker_evidence.py`, Stage-2 values, dated
  2026-09-20, adapter `codex-worker-local` `1.0.0`) for each configured
  Codex resource, keyed to its physical model. Streaming on the Codex
  composed path now works on BUILT-IN evidence (streaming cell
  `PARTIAL`), tools still fail closed (`UNSUPPORTED`), and no synthetic
  cell injection exists in any test. Built-in evidence is the ceiling;
  administrator narrowing/elevation above it was recorded when issue
  #106 closed (2026-09-21) as a possible future enhancement, not
  needed for any currently evidenced adapter.

Deterministic suites added (all green, `tests/test_e2e_codex_acceptance.py`,
`tests/test_codex_real_binary_probe.py`):

| # | Area | Test |
| --- | --- | --- |
| 1 | Packaged-style worker path: console-script pair/run `--allow-codex --codex-model <physical-slug>`, fake `codex` discovered on a tmpdir PATH, real verified TLS (trustme CA via `SSL_CERT_FILE`), resource lands in server registry/state with its physical-model identity, executes | `PackagedWorkerPathTests.test_console_script_worker_discovers_codex_pairs_and_serves` |
| 2 | Codex resource composition (`worker_bridged` + `local_adapter_id: "codex"`); exact configured ownership (worker B can neither report nor serve worker A's Codex resource) | `CodexCompositionTests.test_exact_configured_ownership_for_the_codex_resource` |
| 3 | Honest auth verdicts through the full snapshot path (`auth_unverified` → `unknown`/`telemetry_unknown` → admission refuses; `chatgpt` → `ok` → executes) | `CodexCompositionTests.test_auth_verdicts_drive_eligibility_through_snapshots` |
| 4 | Full execution round trips, streaming AND non-streaming, through the ACTUAL composed server — the production compatibility matrix (M04 preset cells + the reviewed M06 Codex cells built from configuration in `server_composition`) serves the gates; no synthetic cell injection anywhere | `CodexExecutionTests.test_nonstreaming_execution_reports_provider_usage_honestly`, `CodexStreamingExecutionTests.test_streaming_roles_and_history_end_to_end`, `ProductionCompatibilityMatrixTests` |
| 5 | Real installed Codex smoke probe (local-only, skip-safe): discovery + `--version` + `initialize` handshake with controlled-home adoption + `account/read` verdict; NO turn, NO quota read, no user-home contact | `tests/test_codex_real_binary_probe.py` (see the probe record below) |
| 6 | Cancellation through the full stack: client FIN → gateway EOF check → worker cancel → `turn/interrupt` → interrupted; never completed-after-cancel; exactly one turn, no second session | `CodexStreamingExecutionTests.test_client_disconnect_interrupts_the_codex_turn` |
| 7 | Reasoning-effort binding: pinned model + `reasoning_effort` forwarded verbatim; a surface-valid effort the runtime listing lacks is rejected before any thread/turn exists (the binding verdict needs the runtime's own `model/list`) | `CodexStreamingExecutionTests.test_reasoning_effort_forwarded_verbatim`, `..._rejected_before_the_turn` |
| 8 | Structured output: `response_format` `json_schema` → `outputSchema` forwarded verbatim through the stack | `CodexStreamingExecutionTests.test_structured_output_schema_forwarded` |
| 9 | Unsupported client tools: a tools-bearing request is refused 400 `compatibility_unsupported` at admission (the REAL M06 evidence cell records the stable-surface `UNSUPPORTED`) — no execute message, no dispatch session | `CodexStreamingExecutionTests.test_tools_bearing_request_rejected_before_dispatch` |
| 10 | Mid-execution worker loss through the real protocol: 500 `ambiguous_execution_state`, audited, worker-side attempt cancelled (`turn/interrupt` traced), reconnect never re-dispatches (exactly one turn ever) | `CodexWorkerLossTests.test_midexecution_worker_loss_is_ambiguous_and_never_retried` |
| 11 | Usage honesty: fake-reported usage lands in the audit trail as provider-reported (`{11, 7}`); absent usage stays absent (no zeros fabricated) | `CodexExecutionTests.test_nonstreaming_..._honestly`, `test_absent_provider_usage_stays_absent` |
| 12 | No token extraction / no leakage: closed wire surface (reviewed allowlist, no forbidden methods), minimal child environment (`PATH`/`HOME`/`CODEX_HOME`), no `auth.json` in the controlled home, prompt/response content and client key absent from audit, export, diagnostics and worker diagnostics | `CodexExecutionTests.test_stack_holds_the_leakage_line` |
| 13 | Isolation visible end to end: controlled `CODEX_HOME` (0o700, generated config without `mcp_servers`, handshake-verified adoption), scratch cwd under the worker state dir, `workspaceWrite` + `networkAccess: false` + `approvalPolicy: never`, ephemeral thread, roles mapped (`baseInstructions`/`developerInstructions`/`inject_items`), scratch removed after the call | `CodexStreamingExecutionTests.test_isolation_profile_end_to_end` |

Recorded composed behavior worth knowing (documents reality; no code was
changed to alter it): for a streaming request served by the Codex
adapter, the SSE stream is the role frame, the text-delta frames and
`data: [DONE]` — the explicit `finish_reason` frame appears only when the
adapter itself emits a finish chunk, and the Codex adapter does not.
OpenAI-compatible clients treat `[DONE]` as the terminator; the shape is
well-formed but has no terminal finish frame on this path.

### Local real-binary probe record (supplementary, opt-in)

- **Date:** 2026-09-20 (UTC), host WSL2 Linux x86_64 (the Stage-1/2
  evidence host).
- **Binary:** the U-001 VS Code ChatGPT extension layout
  (`openai.chatgpt-26.908.40401-linux-x64`,
  `bin/linux-x86_64/codex`), located read-only.
- **Version:** `codex --version` parses to `0.154.0`
  (`codex-cli 0.154.0-alpha.6.2`; the pre-release segment after the
  numeric triple is tolerated by the adapter's version contract).
- **Observed verdict (structure only, sanitized):** the REAL runtime's
  `initialize` handshake SUCCEEDED against the adapter's controlled home
  under a tmpdir state dir — the runtime echoed the controlled
  `CODEX_HOME`, so the adoption check passed against the real binary —
  and the official `account/read` on the fresh, UNSIGNED-IN controlled
  home failed CLOSED: verdict `auth_unverified`, snapshot status
  `unknown` with the closed `telemetry_unknown` diagnostic. Honestly
  ineligible, exactly as designed. NO turn was started, NO quota read was
  sent, no user-home `~/.codex` content was touched, and no credential
  material existed in the controlled home.
- The probe skips when the binary is absent, so CI stays deterministic;
  the deterministic layer never depends on it.

### External gate

`EXTERNAL_ACCEPTANCE_GATE: LIVE_CODEX_SUBSCRIPTION`

- **What is missing:** a live, turn-level execution against a
  provider-managed login inside a dedicated controlled `CODEX_HOME`. That
  login can be obtained only by an interactive official
  `codex login` (browser or device code) performed by the owner against
  the controlled home. The adapter's isolation design deliberately
  forbids pointing it at the user's real `~/.codex` (D-044: the execution
  runtime gets its own home), and manual token extraction or
  `auth.json` copying is forbidden (issue #91 acceptance criteria;
  AGENTS.md security invariants) — so this gate cannot be completed
  autonomously by this repository's agents.
- **What is already implemented and tested:** deterministic full-stack
  execution against the fake App Server (all areas above), and
  structure-only real-binary probes (handshake, controlled-home
  adoption, honest unsigned-in verdict).
- **Owner action later:** create/sign in once against the dedicated
  controlled home (`CODEX_HOME=<controlled home> codex login`), then
  re-run the live smoke — the bounded eligibility probe
  (`resource_snapshots`) must report the `chatgpt` verdict (status `ok`)
  and one real pinned execution must complete end to end. Record the
  date, binary version and observed quota scope here. The deterministic
  layer must never depend on it.

Platform honesty: Windows-native Codex remains UNEVIDENCED — the
adapter reports `platform_not_evidenced` there by design, no Windows
host exists in this environment, and no Windows evidence is claimed.
The live-subscription confirmation above is the single recorded gate for
the Codex path; every deterministic cell value below stays conservative
until it closes.

## Validation

```bash
uv sync --only-dev
make check          # unittest suite (auto-discovers the acceptance suites) + basedpyright
uv run basedpyright
git diff --check
make package-check
```

Full-suite runtime at time of writing (M10-B included): ~1772 tests,
~9 minutes wall clock on the development host; the M10-B Codex
acceptance suites add ~150 s (of which the packaged-style subprocess
worker path is ~35 s).
