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
| Update path | built/documented | `uv tool upgrade scarcity-router` / container tag pull + `docker compose up -d` / reinstall the worker package; no background auto-update, no remote code execution |
| Version surface | built/tested | `scarcity-router --version`; server startup banner; `get_version()` single-source literal |

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
3. `scarcity-router-worker pair --server srws://HOST:8790 --code CODE` (redeemed over verified TLS; the Windows package asks for these in its first-run dialog)
4. `scarcity-router-worker run --allow-ollama --resource my-ollama` (or start the packaged executable / systemd unit)
5. Administrator: add a `worker_bridged` resource bound to that device

**Measured friction: 5 steps.** The Windows package additionally accepts
`--server-ui-url` so its tray action opens the right control UI when the
server does not use default ports.

## What M10 verified end to end

Sixty acceptance tests (`tests/test_e2e_acceptance.py`,
`tests/test_e2e_execution.py`, `tests/test_tls_acceptance.py`,
`tests/test_security_acceptance.py`) cover the sixteen mission
scenarios — recommendation-only surface, server startup/liveness,
onboarding, client keys, execution ingress (streaming and
non-streaming), generic HTTP provider, direct Ollama preset, worker
pairing over verified TLS, worker-bridged Ollama execution, exact
resource ownership, cancellation propagation, ambiguous-disconnect
no-duplicate semantics, restart/persistence with schema discipline,
remote M08 bridge, diagnostics/doctor, and secret-free export — plus
the TLS and security matrices in
[`docs/m10-security-acceptance.md`](m10-security-acceptance.md). All
deterministic, synthetic, loopback-only, quota-free; no paid provider
test exists and none is added (explicit opt-in remains the only path).

Notable fail-closed behaviors verified along the way (documents reality;
no code was changed to soften them):

- a streaming request against a deployment whose compatibility matrix
  carries no evidence is refused with `compatibility_unknown` before any
  inference (D-043); the composed deployment registers no cells by
  default, so streaming requires evidenced configuration;
- a pinned reference naming a provider outside the model catalog fails
  closed (the gateway answers a structural error, never routes);
- recording compatibility-matrix evidence through the administration
  surface is NOT implemented — deployments that need streaming on the
  composed path need that capability; tracked as Forgejo issue
  BioMedical-IT/scarcity-router#106, not silently assumed by M10.

## Validation

```bash
uv sync --only-dev
make check          # unittest suite (auto-discovers the acceptance suites) + basedpyright
uv run basedpyright
git diff --check
make package-check
```

Full-suite runtime at time of writing: 1756 tests, ~6 minutes
wall clock on the development host; the acceptance suites add ~140 s.
