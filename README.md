# Scarcity Router

> Choose the least scarce model that is capable enough for the task.

Scarcity Router is a local decision service for people using multiple
subscription-backed AI models. It combines current capacity, curated
capabilities, task requirements and user policy, then returns a recommendation
with alternatives and an explanation.

It is the resource-selection and inference component of the local-first
Creatidy system. Kernel owns work intent, authority, harness/workspace
lifecycle and accepted-result evidence; Model Intelligence owns versioned
public knowledge; Console consumes each product's authorized state. The
goal is an accepted result with responsible use of money, subscription
quota, time, review and owner attention, not the cheapest token. This is
target ownership, not a claim that the integrations already work; see
[`architecture.md`](docs/architecture.md#creatidy-system-alignment) and the
[G01-G13 coverage matrix](docs/roadmap.md#creatidy-requirement-coverage).

## When To Use It

**Use it when** you consume several subscription-backed AI models (for
example an OpenAI/Codex plan and a Z.ai Coding Plan), quota is split across
rolling windows, and you want a reproducible answer to "which capable model
should this task consume now?" — instead of checking provider dashboards
manually or maintaining a static model table that goes stale.

**Do not use it when** you need:

- a generic OpenAI-compatible reverse proxy for arbitrary LLM traffic — the
  optional execution gateway below serves only explicitly configured,
  authorized resources under its own deployment and security model;
- an autonomous coding agent or orchestrator. Recommendation-only mode does
  not edit repositories; ordinary gateway client tools remain client-owned.
  The integrated ZCode coding lane is the explicit workspace-editing exception
  and unresolved ownership/migration conflict described below, not a general
  task executor or Kernel integration;
- hosted multi-user quota pooling — this is a local, single-owner service
  that never shares or resells subscription quota.

Current maturity: pre-release (v0.1.0). Integrated implementations and
deterministic tests do not establish complete cross-product or live
acceptance. Known gaps are registered in
[`docs/roadmap.md`](docs/roadmap.md); historical live and external acceptance
evidence is tracked separately in
[`docs/m10-acceptance.md`](docs/m10-acceptance.md); not yet published to a
package index or container registry (see
[Quick Start](#quick-start) for the checkout-based install).

## What It Does

The broker answers **which capable model should this task consume now?** It
considers quota windows, capability, constraints and reservations, then prefers
the least scarce sufficient candidate.

Scarcity Router is two-mode. By default it is a local recommendation service,
not a model gateway: it does not receive prompts, proxy model traffic, read
source code or repositories, execute model calls, dispatch fallbacks, or
replace an orchestrator such as Kilo, Codex or Claude Code. Optionally — as a
separate, explicitly deployed server component — an execution gateway
(decisions D-040 through D-049, implemented; see
[`docs/roadmap.md`](docs/roadmap.md)) lets OpenAI-compatible clients send
authorized requests to one endpoint and have them served from the best
available resource under the same discipline. The gateway's authenticated
OpenAI-compatible execution surface is implemented
(`python -m scarcity_router.control_server`, the one composed server;
contract:
[`docs/execution-surface.md`](docs/execution-surface.md)); native workers
bridge localhost-only resources to it over outbound TLS
(`python -m scarcity_router.worker_client pair|run`; contract:
[`docs/worker-protocol.md`](docs/worker-protocol.md)); the server's
administration surface (control API, web UI, diagnostics) and the
OpenAI-compatible provider adapters are implemented, with configuration,
adapters and the worker transport composed in one process
([`docs/control-surface.md`](docs/control-surface.md)); public distribution,
platform and integrated acceptance still have the explicit gates recorded in
[`docs/m10-acceptance.md`](docs/m10-acceptance.md). This README
documents the recommendation-only product, whose behavior is unchanged by
the gateway.

The integrated ZCode plan-managed backend is a special legacy coding lane
that can edit an explicitly authorized workspace (D-063), not an inference
route proven compatible with Kernel-owned workspaces. Its authority boundary
and migration remain open under
[#180](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/180).

## Quick Start

Requirements:

- Python 3.12 or newer;
- `uv`.

Provider access is described in [Supported Providers](#supported-providers).

The package is not published to any index yet (the public install path that
activates with the first PyPI release is contracted in
[`docs/release-engineering.md`](docs/release-engineering.md)); install it
locally from a checkout (or a built wheel) into an isolated `uv` tool
environment:

```bash
uv tool install /path/to/scarcity-router-checkout
# or, after `uv build --no-sources`:
uv tool install dist/scarcity_router-0.1.0-py3-none-any.whl
# or, from a repository checkout (idempotent install-or-upgrade):
make install
```

After the first PyPI publication, the supported path becomes
`uv tool install scarcity-router` (`pipx install scarcity-router` as the
conventional alternative) — it is deliberately not documented as working
before that release exists.

This exposes exactly four commands:

- `scarcity-router` — the `status` / `select` / `simulate` CLI (plus
  `install-config` and the offline `doctor`);
- `scarcity-router-mcp` — the stdio MCP adapter;
- `scarcity-router-server` — the loopback REST adapter;
- `scarcity-router-worker` — the native worker of the optional execution
  gateway (`pair` / `run` / `service`). The standalone Windows package
  does not contain these console scripts: it ships one executable,
  `scarcity-worker.exe`, which opens a first-run setup dialog on first
  launch and provides the same `pair` / `run` commands directly (see
  the worker section below).

The installed commands load the packaged default catalog and model policy
as package resources, so they work without a
repository checkout and independent of the current directory; explicit
`--catalog` and `--model-policy` flags always override the defaults. The
default user selector policy is packaged the same way and provisioned into
`~/.config/scarcity-router/` (see
[Default configuration location](#default-configuration-location)).

From a checkout instead, sync the repository-managed tools and run the
module CLI:

```bash
uv sync --only-dev
uv run python -m scarcity_router --help
```

In a checkout the defaults resolve to the repository-root
`model-catalog.json` and `model-policy.json`.

## Check Capacity

Show one fresh normalized status snapshot for each supported provider:

```bash
uv run python -m scarcity_router status
uv run python -m scarcity_router status --json
```

Provider failures and unknown windows remain explicit status data; missing
telemetry is never turned into zero or full capacity. Status collection does not
issue a model prompt or model request.

When an installed worker service has exactly one Codex source, OpenAI
telemetry automatically uses that source's existing isolated session: the
same one refreshed by `make codex-login`. An ambient `CODEX_HOME` does not
override this discovered source. No extra login, token copying or worker
restart is required. This selection is shared by CLI, MCP and REST.

Without an installed worker service, or when it configures no Codex source,
ordinary local Codex collection is unchanged. To explicitly use the local
session even with a worker installed:

```bash
SCARCITY_ROUTER_CODEX_SOURCE=@local scarcity-router status
```

With multiple worker Codex sources, choose one of the configured source ids
using `SCARCITY_ROUTER_CODEX_SOURCE=SOURCE_ID`. Ambiguous, invalid or unreadable
source configuration fails with an actionable configuration error rather
than silently reading another account. The existing `SCARCITY_ROUTER_CODEX_BIN`
binary override takes precedence over the service's `--codex-bin`; otherwise
worker-source telemetry uses the same binary discovery as worker login.

## Select A Model

Use a calibrated task profile or provide a complete requirement document:

```bash
uv run python -m scarcity_router select --profile routine_coding --explain
uv run python -m scarcity_router select --profile deep_coding --json
uv run python -m scarcity_router select --requirement task.json
```

The result includes the selected candidate, alternatives, exclusions, capacity
evidence, policy reasons and degraded/unknown indicators. A valid no-solution
result is explicit. `simulate` applies typed overrides and runs the same
selector:

```bash
uv run python -m scarcity_router simulate \
  --profile routine_coding --overrides simulation.json
```

### Default configuration location

All user configuration lives in one place: `~/.config/scarcity-router/`
(honoring `XDG_CONFIG_HOME`). It holds one optional file today,
`selector-policy.json`. Install or reset it explicitly:

```bash
scarcity-router install-config          # creates it from the example, never overwrites
scarcity-router install-config --force  # replaces it with the shipped defaults
```

Every surface provisions the file from the checked-in
[`examples/selector-policy.json`](examples/selector-policy.json) on first
use when it is missing — the run that creates it prints a one-line note to
stderr — and then uses it automatically:

- CLI: an explicit `--selector-policy FILE` wins, then the default config
  file, then the documented neutral policy; `--neutral-policy` ignores the
  config for one run;
- MCP and REST: a request that supplies `selector_policy` wins; a request
  that omits it runs under the default config loaded at process start.

No flags need to be added to the MCP command. The config file contains only
selector policy data — never credentials.

### Provider Availability Policy

Availability windows such as personal peak-hour blackouts and campaign
happy hours are user policy, not telemetry. The checked-in owner policy
[`examples/selector-policy.json`](examples/selector-policy.json) contains
the permanent baseline rules plus the currently documented temporary Z.ai
campaigns (official sources: the
[Plan Update Announcement](https://docs.z.ai/devpack/notice/usage-revision.md)
and the
[GLM-5.3-Flash Usage Campaign](https://docs.z.ai/devpack/notice/event-glm-5.3-flash.md);
billing calendar Asia/Singapore, UTC+8):

- a standing **blackout** pair blocks all Z.ai models (GLM-5.3,
  GLM-5.3-Flash) Monday–Friday 14:00–18:00 Asia/Singapore to preserve the
  plan for off-peak use — split into two dated halves around the vendor's
  all-day off-peak campaign: `zai-peak-hours-sgt` ends 2026-09-24
  (inclusive) and `zai-peak-hours-sgt-post-campaign` resumes the identical
  window from 2026-10-08 (inclusive), so during the vendor's "September 25
  to October 7, 2026" all-day off-peak-rate period an otherwise eligible
  Z.ai request is not excluded as `preserve_zai_offpeak`, and ordinary
  peak behavior resumes automatically afterwards;
- a **happy hour** strongly prefers GLM-5.3-Flash daily 23:00–09:00
  Asia/Singapore between 2026-09-03 and 2026-10-07, matching the vendor's
  extended usage-campaign window, so cheap-quota work is absorbed by the
  campaign model and paid plans are conserved. The vendor's execution-method
  split (zero quota via ZCode ≥ 3.10/AutoClaw, doubled quota via other
  agents, weekly participation cap) is not modeled as selection fact — the
  selector has no authoritative per-request execution-source identity — so
  the rule stays a ranking preference and never fabricates availability.

Use it with the CLI:

```bash
uv run python -m scarcity_router select \
  --profile routine_coding --selector-policy examples/selector-policy.json
```

or install it once with `scarcity-router install-config` and omit the flag
entirely (see [Default configuration location](#default-configuration-location)).
REST/MCP callers pass the same document inline as the optional
`selector_policy` object (see
[`docs/machine-interfaces.md`](docs/machine-interfaces.md)). During a
matching blackout Z.ai candidates are excluded with a `policy_blocked`
result — the explanation says the provider is policy-blocked, never
unavailable or incapable, and capacity telemetry is untouched. During a
matching happy hour the targeted candidate is marked as quota-preferred in
the explanation; the preference reorders ranking only and never bypasses
capability or capacity eligibility.

To confirm either window fires without waiting for it, `simulate` moves
the evaluation instant through a typed override. With
`{"evaluated_at": "2026-09-14T15:00:00+08:00"}` as `overrides.json` (a Monday
inside the blackout window):

```bash
uv run python -m scarcity_router simulate \
  --profile routine_coding \
  --selector-policy examples/selector-policy.json \
  --overrides overrides.json --explain
```

both GLM models appear under the exclusion stage `policy_blackout`, each
naming the rule: `blackout rule zai-peak-hours-sgt (preserve_zai_offpeak)`.
With `{"evaluated_at": "2026-09-15T02:00:00+08:00"}` (inside the campaign
window) GLM-5.3-Flash wins despite scarcer quota and names
`happy hour rule zai-flash-campaign-night-sgt`.

The mechanism is documented in
[`docs/selection-policy.md`](docs/selection-policy.md); the example file is
opt-in configuration, so a missing config file keeps the neutral policy.

## MCP Integration

MCP is the primary machine-integration path for local orchestrators. Start
the stdio adapter through the installed command:

```bash
scarcity-router-mcp
```

or, from a checkout:

```bash
uv run python -m scarcity_router.mcp
```

The installed process configuration recipe is
[`examples/mcp-stdio.json`](examples/mcp-stdio.json). It needs no flags or
inline policy: the MCP process picks up
`~/.config/scarcity-router/selector-policy.json` automatically (see
[Default configuration location](#default-configuration-location)). It
advertises exactly:

- `scarcity_status` — current normalized provider capacity;
- `scarcity_select` — a recommendation for a profile or explicit requirement;
- `scarcity_simulate` — a recommendation with typed simulation overrides.

The [agent routing prompt](examples/agent-routing-prompt.md) explains how to
use Scarcity Router for dynamic model assignment: the MCP recipe answers how to
connect it, while the prompt answers when and how an agent should use it.

The tools call the same application/core as the CLI and REST interface. They
use stdio, accept no credentials or provider endpoints, and never execute
inference. Envelopes are defined in
[`docs/machine-interfaces.md`](docs/machine-interfaces.md).

### Global Kilo deployment note

To use Scarcity Router globally in Kilo, add the
[`examples/mcp-stdio.json`](examples/mcp-stdio.json) recipe (a
`type: local` MCP server launching the installed `scarcity-router-mcp`
command) to the `mcp` section of your Kilo configuration. This repository
never edits your global Kilo configuration itself; the copy-paste is a
manual owner action, and it requires the package to be installed as a `uv`
tool (or the recipe adjusted to the checkout-based module command).

## Supported Providers

- **OpenAI through Codex app-server** — reads subscription capacity from the
  locally available app-server.
- **Z.ai Coding Plan** — reads the existing Kilo `zai-coding-plan` credential
  and the provider's normalized usage endpoint.

These adapters report subscription capacity, not generic API pricing. Local
inference is not part of the recommendation surfaces; it returns as an
execution resource of the optional execution-gateway program (decision D-040;
module issues #86–#95). See
[`docs/providers.md`](docs/providers.md) for discovery, normalization and
provider-change behavior, and
[`docs/architecture.md`](docs/architecture.md) for the gateway architecture.

## Deploying The Optional Execution Gateway

Do not point an inference client at `scarcity-router-server` (local
recommendation REST, default port 8765). Execution uses the composed
`python -m scarcity_router.control_server` below (default HTTP port 8787,
`/v1` client base URL, client authentication). The shared `/v1/` spelling
does not make these servers interchangeable. Chat Completions is implemented;
Responses and Anthropic Messages are not supported ingress protocols today.
Compatibility is specific to the harness/protocol/adapter/source version,
not guaranteed by the phrase OpenAI-compatible.

The recommendation-only product above needs none of this. If you want one
OpenAI-compatible endpoint served from your own heterogeneous resources
(subscription plans, APIs, local inference), the execution gateway is a
separate, explicitly deployed server plus — where local resources need
bridging — a small worker. The honest acceptance record, platform support
table and measured first-run steps live in
[`docs/m10-acceptance.md`](docs/m10-acceptance.md); the security matrix is
[`docs/m10-security-acceptance.md`](docs/m10-security-acceptance.md).

### Server (one container)

```bash
docker build -t scarcity-router .
docker compose up -d          # examples/docker-compose.yml, loopback default
# open http://127.0.0.1:8787/admin and complete first-run onboarding:
# create the administrator password -> add a provider endpoint ->
# add a resource -> issue a client key (shown exactly once)
```

Defaults are loopback-only. LAN/VPN exposure requires verified TLS
certificates (`--host 0.0.0.0 --tls-certfile ... --tls-keyfile ...`) — the
server refuses a non-loopback plaintext bind by design (D-044). The compose
example documents the real flag surface, volume and update discipline;
nothing is invented. The image is not published to any registry yet (the
GHCR contract is recorded in
[`docs/release-engineering.md`](docs/release-engineering.md)).

### Worker (bridges localhost-only resources)

One-time pairing: the administrator issues a short-lived one-time code in
the web UI (Workers page); the worker redeems it over verified TLS:

```bash
scarcity-router-worker pair --server srws://SERVER-HOST:8790 --code CODE
```

The worker connects outbound only (no inbound port), holds no provider
credentials and enforces its local adapter allowlist even against server
requests.

#### Linux/WSL: run it as a service (the normal deployment)

A paired worker installs itself as a systemd **user** service — no root,
no hand-edited unit files, no secrets in the unit. Requires a systemd
user manager (WSL2: `systemd=true` under `[boot]` in `/etc/wsl.conf`).
The generated unit
comes from the ACTUAL installed executable and the ACTUAL state
directory, and preserves the adapter selection you pass (here: one Codex
execution source):

```bash
# 1. pair once (above), then:
scarcity-router-worker service install --codex-source precision-codex-live
#    (idempotent; re-run with new flags to change the selection, then
#     `service restart` to apply)
# 2. operate it with normal systemd verbs — all no-root:
scarcity-router-worker service status      # linger state + systemctl --user status
scarcity-router-worker service restart     # after a configuration change
scarcity-router-worker service uninstall   # stops/disables and removes the
                                           # generated unit; pairing, identity
                                           # and worker state are KEPT
journalctl --user -u scarcity-router-worker -f   # follow the worker's logs
```

Refreshing the Codex credentials needs no source id and no restart — the
login targets exactly the source (and controlled home) the installed
service already runs, on any host:

```bash
make codex-login
# equivalent explicit form: scarcity-router-worker codex-login
```

The official `codex login --device-auth` flow prints a URL and one-time
code; complete it in any browser (SSH-safe: no localhost callback). The
running worker picks the fresh credentials up on its next eligibility
check. With no (or several) configured Codex sources the command refuses
with the exact remediation instead of guessing. A unit written by hand
(the retired copied-example shape, with `%h`-anchored paths) still works:
its `--codex-source`/`--state-dir` are read off the command line it runs,
and the output names the one-time `service install` migration to the
generated unit.

Install enables `loginctl linger` for your user (headless operation
without an active login session) and reports the outcome; when your
distribution requires confirmation for that, it prints the exact
`sudo loginctl enable-linger` command to run once. `systemctl --user
enable/disable/start/stop/restart scarcity-router-worker` work as usual.
One service per user account: a second worker on the same host is a
second user account with its own paired state. A foreground process can
never run next to the service — every worker holds a lifetime lock on
its state directory and the second process fails with a message naming
the holder. Service mode deliberately narrows the filesystem contract
(D-065): the worker state directory AND the systemd user-unit directory
must lie beneath your home directory. The default state location
`~/.local/share/scarcity-router/worker` and the normal
`~/.config/systemd/user` unit location just work; an explicit custom
state directory under `$HOME` is supported when it is private
(owner-owned, not group/world-writable), and `XDG_CONFIG_HOME` must
point beneath `$HOME` for install. `/`, your home itself and every
location outside home are refused for the service — with the exact path
and reason named — rather than silently relocated; a dedicated private
state directory outside home remains supported for foreground `run`.
Unsafe directories are never silently repaired, and no group/world-
writable non-sticky ancestor or symlinked unit-directory chain is
accepted.
The normal missing default state/config trees are provisioned privately
through trusted parents. Custom CA trust is not configured through the unit (see the
TLS/certificate lifecycle when that ships; unit files never carry
`SSL_CERT_FILE` workarounds).

#### Foreground run (debugging)

`run` is the same runtime in a foreground shell — for development and
debugging, not for keeping a worker alive:

```bash
scarcity-router-worker run --codex-source precision-codex-live   # debugging
scarcity-router-worker run --allow-ollama --resource my-ollama   # debugging
```

**Windows (standalone package, no Python required).** The release ZIP
(`scarcity-worker-X.Y.Z-windows-x64.zip`) contains a single executable,
`scarcity-worker.exe`:

1. Unzip and double-click `scarcity-worker.exe`. An unpaired worker opens
   the compact **first-run setup dialog**: enter the worker server origin
   (`srws://SERVER:8790`) and the one-time code (a button opens the
   server web UI where codes are issued), and optionally tick
   "Enable local Ollama" (loopback only; a resource id is required).
   Pairing stores the identity locally and the tray starts.
2. Later launches go straight to the tray. Pair-only is valid: a local
   adapter can be enabled or changed any time via the tray's
   **"Worker settings..."** action (a save restarts the worker loop with
   the new allowlist).
3. PowerShell/cmd automation uses the same executable — no Python
   needed:

```powershell
scarcity-worker.exe pair --server srws://SERVER-HOST:8790 --code CODE
scarcity-worker.exe run --allow-ollama --resource my-ollama
```

The dialog and the `pair` command share one pairing implementation; the
pairing code is never stored. Local settings persist only non-secret
worker configuration (control-UI origin, enabled loopback Ollama
resource) in `%LOCALAPPDATA%\scarcity-router` alongside the identity;
the server can never widen the local allowlist remotely. Windows-native
Codex execution is not offered on this path (unevidenced for v0.1.0 —
Codex worker execution requires the Linux/WSL CLI path). Live Windows
acceptance is a recorded external gate — see the acceptance document for
exactly what is and is not verified.

### Updates and uninstall

- Before publication: reinstall from the updated checkout with `make install`
  (or `uv tool install --force /path/to/checkout`) or from the new built
  wheel. After PyPI publication: `uv tool upgrade scarcity-router` (or
  `pipx upgrade scarcity-router`);
  then `scarcity-router-worker service restart` (the generated unit points
  at the resolved executable path, so an upgrade needs the restart to run
  the new code). Server/container: restart any server processes.
- Container: rebuild the local image before publication; after registry
  publication, pull the new image tag, then `docker compose up -d`; the named
  volume keeps identities, configuration and keys (store schema migrations
  are explicit and refuse future versions — never downgrade across one).
- Windows worker: reinstall the new release package; restart the worker.
- Uninstall: `scarcity-router-worker service uninstall` removes the worker
  service first (identity and state are kept); `uv tool uninstall
  scarcity-router` (or remove the container and volume) removes the
  program; user state lives in `~/.config/scarcity-router/` (user
  policy), `~/.local/share/scarcity-router/` (server store + worker state,
  `%LOCALAPPDATA%\scarcity-router` on Windows) — delete it only when you
  mean to lose keys and configuration.

There is deliberately no background auto-update and no remote code
execution in any update path.

## Privacy / Product Boundary

Credentials stay in their existing local provider-managed source whenever
possible. Collectors use them transiently, never return or persist their
values, and only emit normalized safe status. The REST adapter binds to
`127.0.0.1` by default; MCP uses local stdio.

In the default recommendation-only mode, the service does not inspect prompts,
source code, repository contents or browser sessions. It does not proxy
requests, call models, redeem reset credits or automatically execute a
fallback. The optional execution-gateway program extends this boundary only
through its recorded decisions D-040 through
D-049, with its own security architecture in
[`docs/security.md`](docs/security.md). Read
[`docs/security.md`](docs/security.md) for the complete credential and network
boundary.

## Documentation

A normal adopter can stop after **USE IT**. Technical contracts, development
rules and historical evidence remain available without being part of onboarding.

### USE IT

- This README — quick start, capacity checks, selection and MCP.
- [`examples/mcp-stdio.json`](examples/mcp-stdio.json) — installed MCP process
  configuration recipe.
- [`examples/agent-routing-prompt.md`](examples/agent-routing-prompt.md) —
  copy-paste instructions for dynamic agent model routing.

### UNDERSTAND IT

- [`docs/product.md`](docs/product.md) — purpose and boundaries.
- [`docs/architecture.md`](docs/architecture.md) — components and dependencies.
- [`docs/capacity-model.md`](docs/capacity-model.md) — normalized capacity.
- [`docs/capability-model.md`](docs/capability-model.md) — requirements and capabilities.
- [`docs/selection-policy.md`](docs/selection-policy.md) — eligibility and ranking.
- [`docs/machine-interfaces.md`](docs/machine-interfaces.md) — REST and MCP.
- [`docs/execution-surface.md`](docs/execution-surface.md) — OpenAI-compatible execution surface.
- [`docs/control-surface.md`](docs/control-surface.md) — server control API, web UI and diagnostics.
- [`docs/providers.md`](docs/providers.md) — provider adapters.
- [`docs/security.md`](docs/security.md) — secrets and network boundaries.
- [`docs/worker-protocol.md`](docs/worker-protocol.md) — the versioned
  native-worker transport, pairing and execution-bridging contract of the
  optional execution gateway.
- [`docs/m10-acceptance.md`](docs/m10-acceptance.md) — distribution and
  end-to-end acceptance record: platform support table, measured first-run
  steps, external gates.
- [`docs/m10-security-acceptance.md`](docs/m10-security-acceptance.md) —
  the security acceptance matrix mapped to the D-044 threat model.

### DEVELOP IT

- [`AGENTS.md`](AGENTS.md) — durable repository and agent rules.
- [`docs/llm-operating-policy.md`](docs/llm-operating-policy.md) — bounded
  multi-model execution and review governance.
- [`docs/release-engineering.md`](docs/release-engineering.md) — CI, release
  integrity and public distribution contracts.
- [`Makefile`](Makefile) — the reproducible test and type-check gate.

### AUDIT / HISTORY

These documents preserve decisions, evidence, calibration, acceptance records,
competitive context and roadmap history. They are provenance, not required
onboarding:

- [`docs/decisions.md`](docs/decisions.md)
- [`docs/roadmap.md`](docs/roadmap.md)
- [`docs/history/roadmap.md`](docs/history/roadmap.md) — detailed milestone record.
- [`docs/poc-evidence.md`](docs/poc-evidence.md)
- [`docs/m3-acceptance.md`](docs/m3-acceptance.md)
- [`docs/model-calibration.md`](docs/model-calibration.md)
- [`docs/competitive-landscape.md`](docs/competitive-landscape.md)
- [`docs/codex-adapter-stage1-evidence.md`](docs/codex-adapter-stage1-evidence.md)
  — dated official-interface/compatibility evidence, not fresh live acceptance.
- [`docs/zcode-adapter-stage1-evidence.md`](docs/zcode-adapter-stage1-evidence.md)
  — historical D-047 closeout, superseded through its reopen procedure.
- [`docs/zcode-adapter-stage2-evidence.md`](docs/zcode-adapter-stage2-evidence.md)
  — dated reopen evidence and subsequent current-state qualification.
- [`docs/history/daybreak-review-segmented-2026-09-25.md`](docs/history/daybreak-review-segmented-2026-09-25.md)
  — historical bounded review evidence.

## Development

Repository-local commands are `/implement-issue`, `/review-pr`, `/finish-pr` and
`/loop`; see [`AGENTS.md`](AGENTS.md) and [loop.md](.kilo/command/loop.md).
Standalone implementation requires an owner-selected issue, review is read-only,
and finish performs bounded independent review/remediation without merging.
Only an explicit owner `/loop` authorizes autonomous canonical issue selection,
exact-approved Forgejo PR merge into `develop`, verified acceptance/issue closure,
and continuation. One designated delivery checkout/mutator, bounded temporary
worktrees or evidence checkouts for safe recovery, no external controller, and no
Scarcity Router use for operating this development workflow. Kilo may require a
workspace reload to discover new local commands/agents; missing native reviewer
support requires bounded authorized independent reviewer failover before a finite
blocker, not permission for implementation self-review. Technical blockers are
self-remediated under [the shared recovery rule](.kilo/rules/35-technical-recovery.md);
only genuine owner decisions require owner attention, with no weakened review or security gates.

Run the repository gate from a checkout:

```bash
uv sync --only-dev
make check
uv run basedpyright
git diff --check
```

Package artifact and isolated-install checks:

```bash
make package-check
```

Development CI runs the same gate plus `make package-check` on every pull
request to `develop` and every push to `develop` (workflow `ci`, job
`check` — a stable check name for owner-configured branch protection). Public
releases are deliberate SemVer tags on stable `main`, published through the
tag-driven GitHub workflow. The CI/release authority split, trust model and
owner-action checklist live in
[`docs/release-engineering.md`](docs/release-engineering.md).

Forgejo is canonical for issues, branches, pull requests and reviews; `develop`
is the integration branch. GitHub is an automatic read-only mirror for public
visibility and integrations that require GitHub.

The project is licensed under the Apache License 2.0.
