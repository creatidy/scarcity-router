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
- Docker Desktop on Windows/macOS does not share host loopback, so the
  loopback default requires the TLS variant there.
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
  composed path need that capability; tracked as follow-up work, not
  silently assumed by M10.

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
