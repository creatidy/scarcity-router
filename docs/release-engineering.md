# Release engineering

This document is the authority for CI, release integrity and public
distribution of Scarcity Router (issue #97, decision D-046). It defines who
runs what, what must hold before anything is published, and which artifacts
exist today versus which are recorded future contracts. Issue #95
(distribution, installation, update and end-to-end acceptance) builds on this
foundation; it does not re-decide it.

The single most important trust boundary is:

```text
untrusted development input  !=  release authority
```

No path from an ordinary pull request reaches publication authority or any
provider or private-infrastructure secret.

## Creatidy Contract and Public Delivery Coverage

Shared-system alignment does not make unpublished artifacts or untested
platforms available. #185 owns Router's compatibility/migration proof with
Kernel executable consumers and MI artifacts; each producer retains its
own versions/state, with no global ontology/version or common database.
#186 owns uncovered GHCR/Windows artifact and public-platform acceptance;
#224 (D-083 successor of excluded #139) retains the transport/TLS lifecycle
and #227 (D-083 successor of excluded #141) composed install/client
execution proof — successors, not #186 prerequisites.
These are known required deliveries, including external decision/access
gates, not a reason to call distribution complete.

Public installation must work without private `creatidy-onprem`; public-module
dependencies are permitted. Clean installed-user evidence must cover runtime,
authentication/TLS, filesystem, supervisor/service lifecycle and server-first
upgrade ordering where relevant. Current checkout/wheel and local-image paths
remain supported; index/registry upgrade commands below are future contracts
until publication. A new recurring manual step needs evidenced justification.

The statuses below describe artifact availability, not D-068's evidence/
decision statuses. A configured workflow is not a successful run, publication
or live platform proof. Owner-controlled release/tag/signing/deploy authority
is unchanged; ordinary agents open Forgejo PRs, and only the owner's authorized
`/loop` may integrate accepted current PRs into `develop`. It never publishes.

## Status legend

Every capability in this document carries exactly one status:

- **Implemented now** — exists in the repository and works without further
  external setup (where its runner/environment exists).
- **Configured externally** — repository side is done; an owner action
  outside Git must activate it (see the owner-action checklist below).
- **Future contract** — the contract and insertion point are frozen here; the
  artifact does not exist yet and nothing may pretend otherwise.
- **Not yet supported** — explicitly out of scope; no partial or fake support
  is claimed.

## Forgejo versus GitHub

| Responsibility | Authority | Status |
| --- | --- | --- |
| Issues, branches, pull requests, reviews | Forgejo ([canonical](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router)) | Implemented now |
| Development CI (validation gate + package check) | Forgejo Actions, `.forgejo/workflows/ci.yml` | Implemented now (requires an owner-configured runner) |
| `develop` → `main` promotion | Human-controlled, deliberate | Implemented now (policy; never automated) |
| Release tags (`vX.Y.Z`) | Human-controlled, created on stable `main` | Implemented now (policy; none exist yet) |
| Public releases, downloadable artifacts | GitHub Releases via the mirror | Configured externally (workflow is implemented; requires owner actions to fire) |
| PyPI publication | Trusted Publishing (OIDC) via the release workflow | Configured externally |
| Build provenance | Sigstore-backed GitHub artifact attestations | Implemented now |
| Server OCI images on GHCR | `build-server-image` + `publish-ghcr` jobs in `.github/workflows/release.yml` (image built from the committed `Dockerfile`, offline-verified, pushed only on the tag event; `linux/amd64`) | Implemented now (workflow; live GHCR publication is a recorded gate — no image has ever been pushed) |
| Windows worker package (portable ZIP) | `build-windows-worker` job in `.github/workflows/release.yml` | Implemented now (workflow; live runner acceptance is a recorded gate) |
| Windows worker MSIX (signed) | MSIX seam in the same job | External gate (`WINDOWS_CODE_SIGNING`) |
| Custom Linux package repositories (apt/RPM/pacman), second CI platform, other registries | — | Not yet supported (deliberately) |

GitHub ([`creatidy/scarcity-router`](https://github.com/creatidy/scarcity-router))
is a read-only mirror used for public distribution. It hosts exactly one
workflow, `.github/workflows/release.yml`, triggered only by `v*.*.*` tags.
GitHub adds no development CI: no pull-request workflow exists there by
design, and the release workflow never runs from a pull-request event.

## Development CI (Forgejo Actions)

**Implemented now** (the workflow; the runner itself is **configured
externally**).

`.forgejo/workflows/ci.yml` — workflow name `ci`, job name `check` — runs on:

- every pull request targeting `develop`;
- every push to `develop`.

The job runs the repository's authoritative validation gate plus the
authoritative package check, and keeps no test list of its own:

```bash
uv sync --only-dev
make check            # unittest suite + repo-managed basedpyright
uv run basedpyright   # documented gate command
git diff --check
make package-check    # builds wheel + sdist, inspects contents/metadata,
                      # smoke-installs the wheel into a throwaway uv tool
                      # environment (console scripts, packaged resources, MCP
                      # discovery, loopback REST healthz), seeds synthetic
                      # operator state through the installed APIs, replaces
                      # the wheel in place (uv tool install --force) with
                      # retention proof, then uninstalls the tool and proves
                      # the operator state is retained
```

`make package-check` (`tools/package_check.py`) validates the distributable
package, not only the source checkout: missing packaged files, invalid
metadata, broken entry points, undeclared runtime dependencies and accidental
dependencies on repository-relative files all fail the job. Its isolated
lifecycle (issue #186) proves a fresh install, a same-version local-wheel
replacement (reinstall of the exact same wheel — explicitly NOT an
old-release→new-release upgrade, of which none can exist before the first
release) with synthetic user selector policy and server-store configuration
record retained, and an uninstall that removes the tool while the operator
state is deliberately retained (no purge). It proves nothing about Windows
execution, services, global trust stores, signatures or publication. Build
and install subprocesses run on a deliberate environment allowlist, never a
wholesale ambient environment. It contacts no provider and never publishes
anything.

Determinism and supply-chain discipline:

- the CI interpreter is pinned (Python 3.12 via `uv`);
- runner caching is disabled so every run resolves from the lockfile;
- the only third-party actions are `actions/checkout` and
  `astral-sh/setup-uv`, each pinned to a full commit SHA with its version
  recorded in a comment;
- job-level `permissions` are `contents: read` and nothing else;
- superseded runs are cancelled through a per-ref `concurrency` group;
- no provider credentials, paid inference, personal subscriptions or private
  infrastructure are required.

### Stable check name for branch protection

The required-status-check contract for `develop` is:

```text
workflow: ci
job (status check name): check
```

The job name is deliberately identical to the `Makefile` gate target, so
branch protection does not depend on an accidental label. If the workflow's
job name must ever change, that is a contract change to record here first.

### Runner boundary (owner-configured)

The workflow requests the conventional `ubuntu-latest` runner label. The
minimum safe boundary for any runner that executes pull-request code:

- one ephemeral, isolated environment per job (container-isolated or
  freshly provisioned and destroyed afterwards); no state reused between
  jobs or runs;
- no Docker socket mounted into the job, no arbitrary host mounts, no
  privileged containers, no host network access to private Creatidy
  infrastructure;
- no provider, publishing or deployment secrets injected into PR jobs — the
  workflow needs none, so none must be present at the runner level either;
- outbound internet access (PyPI, the Forgejo instance) is sufficient; broad
  private-network reachability is not.

## Development CI security contract (frozen)

- **PR CI is potentially hostile.** Pull-request code runs with `contents:
  read` only and in the runner boundary above.
- **PR jobs receive no provider or publishing secrets** — there is no secret
  in the development-CI path at all.
- **Publishing never runs from pull-request events.** The only publish-capable
  workflow is tag-triggered on the mirror.
- **Public contribution code has no privileged path** into private
  infrastructure; the runner boundary forbids the paths that could create one.
- **Release authority is separated from development CI**: a green `check` alone
  grants no merge or publishing authority. Development PR merge requires a human
  or D-069's explicit owner `/loop` with exact independent approval/currentness
  and acceptance gates; publication authority remains exclusively in the
  human-created release-tag path. `/loop` never releases or deploys.
- **Ordinary deterministic tests require no secrets**, so secret presence can
  never become a test requirement.

## Branch-protection contract for `develop`

**Owner action required — not claimed configured here.**

Required setting on the Forgejo repository:

- branch `develop`;
- require a pull request for merges (no direct pushes);
- require the status check **`check`** (from workflow `ci`) to pass before
  merge;
- disallow force pushes and history rewriting on `develop`.

Until an owner applies this, merging remains protected only by the repository
convention that every PR shows a green `check` run. `main` promotion policy is
deliberately out of scope for this document's automation: promotion is a
manual, human-controlled action.

## Final Project Security Audit (D-081)

Before the first production release, the release owner requires one comprehensive
independent Daybreak audit of the complete integrated product at one frozen
release-candidate SHA, under [the final-audit contract](llm-operating-policy.md#final-release-candidate-audit).
Audit findings must be corrected and verified on the final candidate before release;
a changed candidate needs current audit acceptance. Verify the then-available model,
program, account/project approval and execution mechanism from current official evidence,
not a deprecated alias, Codex subscription or Router's bounded Responses support.

This is a documented human-controlled release acceptance gate, not enforced by the existing release workflow.
It is not an implementation or develop integration prerequisite. Ordinary PR security
review and material-defect merge blockers remain mandatory; a future audit never
waives them. No Daybreak run, credentials, provisioning, main promotion, release or
deployment is authorized by this policy correction.

## Release and versioning contract

**Implemented now** (the contract and its enforcement). Releases are rare,
deliberate, human-controlled events:

```text
develop  ->  human promotion to main  ->  human tag v X.Y.Z  ->  GitHub mirror
                                                                       -> release workflow
```

Invariants enforced fail-closed by `.github/workflows/release.yml`
(`verify` job). The release stops unless **all** of the following hold:

1. the ref is a supported SemVer release tag `vX.Y.Z` (no pre-release,
   build or loose forms);
2. the package version equals the tag version, read from the single
   authoritative literal `__version__` in `scarcity_router/__init__.py`
   (there is no second committed version copy);
3. the tagged commit is reachable from stable `main`
   (`git merge-base --is-ancestor`);
4. the artifacts are built from the exact tagged commit — every build job
   of the run checks out that same commit, and later stages receive bytes
   only through this run's artifact uploads (they perform no checkout);
5. the artifacts pass their applicable verification (`make package-check`)
   before any publication job starts.

Additionally frozen:

- versions are never bumped automatically;
- `develop` is never promoted to `main` automatically;
- arbitrary development commits are never released;
- tags are intentional human actions on `main` only.

Release procedure (human): satisfy D-081's final audit on the exact candidate before
the first production release, then promote the reviewed `develop` state to `main`,
set `__version__` through the normal PR flow beforehand, then create the
annotated tag `vX.Y.Z` on the intended `main` commit and push it. Everything
after that is the workflow's job — or a fail-closed refusal.

## Release pipeline mechanics

**Implemented now**; activation requires the owner actions below.

`.github/workflows/release.yml` on the GitHub mirror supports exactly two
run modes in one workflow:

- **Real public release** — a deliberate `vX.Y.Z` tag push on stable `main`
  (the only publishing mode);
- **Manual release-candidate preflight** — `workflow_dispatch` from a
  selected branch/commit: it builds and verifies EVERY public artifact
  (wheel, sdist, the Windows worker ZIP on a real Windows runner, the
  linux/amd64 server image with its isolated smoke, the finalized attested
  bundle, `SHA256SUMS`) and records the candidate
  identity (ref, exact SHA, package version) — and publishes NOTHING: no
  PyPI upload, no GitHub Release, no GHCR push, no tag, no push. The first
  real execution of the Windows build or the image build therefore never
  has to be the irreversible release
  run. The strict tag/main/version contract is not relaxed for candidates;
  it simply does not apply to a run that never claims to be a release (a
  dispatched run from a tag ref is refused).

Publication ORDERING is fail-safe by construction — every public artifact,
including the Windows ZIP and the server image, is built and verified
BEFORE any publication; the GitHub Release and the GHCR image push are both
tag-event-only and both strictly downstream of the finalized bundle AND the
verified image; the irreversible
PyPI publication runs strictly LAST:

```text
verify-release-contract        (tag invariants | candidate identity)
        |
        v
      build
      /   \
     v     v
build-windows-worker    build-server-image
        |                    |
        v                    |
  finalize-release-bundle    |
        |                    |
        +---------+----------+
                  v
  [publish-github-release, publish-ghcr]     (tag event only)
                  |
                  v
             publish-pypi               (tag event only, strictly LAST)
```

Each publication job requires BOTH the finalized bundle AND the verified
server image (`build-server-image` joins the two publications directly;
`finalize-release-bundle` needs only the Python and Windows builds). All
three publication jobs carry `if: github.event_name == 'push'`, so a manual
candidate run stops after the builds, finalization and attestation. Eight
jobs:

| Job | Needs | Permissions | Does |
| --- | --- | --- | --- |
| `verify` | — | `contents: read` | tag event: checks out the tag with full history and enforces invariants 1–3; dispatch: records the candidate identity (ref, SHA, version) and refuses a tag ref |
| `build` | verify | `contents: read`, `id-token: write`, `attestations: write` | builds and smoke-installs the artifacts from the exact commit under run (`make package-check`), writes the PARTIAL `SHA256SUMS` (wheel and sdist entries), verifies the payload set, attests the wheel and the sdist (artifacts whose bytes are final at this stage), and uploads two structurally separated artifacts: `pypi-packages` (only `*.whl`/`*.tar.gz`) and `release-bundle` (distributions plus the partial `SHA256SUMS`) |
| `build-windows-worker` | build | `contents: read` | on a Windows runner, from the exact commit under run: builds the one-dir PyInstaller worker (`packaging/windows/worker.spec`, a committed source file), stages `scarcity-worker-X.Y.Z-windows-x64.zip` and uploads the `windows-worker` artifact. The MSIX direction is a fail-closed seam behind the owner's signing secret (`EXTERNAL_RELEASE_GATE: WINDOWS_CODE_SIGNING`) |
| `build-server-image` | build | `contents: read` (no write permission — the publication identity exists only in `publish-ghcr`) | from the exact commit under run: builds the committed `Dockerfile` as one `linux/amd64` image with `org.opencontainers.image.version`/`revision`/`created` labels, verifies the built platform and identity labels offline, smoke-runs the composed server in an isolated container (read-only root filesystem, tmpfs `/tmp`, an ephemeral IN-MEMORY tmpfs `/data` owned by the container's non-root uid/gid 10001 with mode 0700 — a host bind would be runner-owned and unwritable for the server, dropped capabilities, no privilege, no published ports; the only request ever made is `GET /healthz`), then saves the tested image with its SHA-256 checksum and a provenance record (version, revision, platform, image id) into the `server-image` artifact — a same-run intermediate that never enters the GitHub Release asset set or the PyPI payload |
| `finalize-release-bundle` | build, build-windows-worker | `contents: read`, `id-token: write`, `attestations: write` | the only stage that knows every public payload: completes `SHA256SUMS` (the worker zip entry joins the wheel and the sdist), verifies every entry against the actual bytes (`sha256sum -c`), enforces the exact final asset set, attests the worker zip and the FINAL `SHA256SUMS`, and uploads `release-bundle-final` — the exact asset set the GitHub Release attaches |
| `publish-github-release` | finalize-release-bundle, build-server-image | `contents: write` | tag event ONLY: downloads `release-bundle-final` and creates the GitHub Release, attaching the wheel, the sdist, the worker zip and `SHA256SUMS` through an explicit asset list |
| `publish-ghcr` | build-server-image, finalize-release-bundle | `contents: read`, `packages: write` (the ONLY `packages: write` in the workflow) | tag event ONLY, and strictly before PyPI; serialized across releases by a fixed repository-wide job concurrency group (`ghcr-publication`, `cancel-in-progress: false`) so alias decisions never race another release's publisher. Re-checks the event/tag gate inside the step (event is `push`, ref is `refs/tags/vX.Y.Z`), verifies the downloaded image artifact (exactly one tarball, checksum, provenance version/revision/platform against the tag and the commit under run), loads the image and re-verifies its id, identity labels and `linux/amd64` platform — all before any registry contact; then logs in with the run's short-lived token through a dedicated temporary `DOCKER_CONFIG` that is EXPORTED for docker and removed by a trap on success and failure (`--password-stdin`, no credential in logs). ALL remote reads happen before ANY push: absence is accepted ONLY for the exact docker-CLI missing-manifest diagnostic of the exact reference (`no such manifest: <ref>`) — anything else (auth, transport, TLS/certificate noise, an embedded 404 in a URL, a wrong-reference diagnostic) fails closed. Tag semantics: the immutable version tag `X.Y.Z` present with different content REFUSES to retarget a released version and is never re-pushed when identical; each moving alias (`X.Y`, `latest`) is advanced only when missing or when a read-only authenticated pull proves its immutable `org.opencontainers.image.version` label is an OLDER-or-equal strict stable SemVer (equal repairs the alias to the exact tested image); a NEWER stable alias is preserved, so an older-release retry can never regress `X.Y` or `latest`; a cross-line minor alias, a missing/non-stable label, or any pull/auth/network failure fails closed before any push. A green `publish-ghcr` therefore means: immutable version verified against the tested bytes, moving aliases never behind this release — and PyPI never follows either an incomplete or a regressed tag set |
| `publish-pypi` | publish-github-release, publish-ghcr | `id-token: write` (environment `pypi`) | tag event ONLY, and strictly LAST: downloads `pypi-packages`, re-verifies with a guard step that the directory contains only `*.whl`/`*.tar.gz`, and publishes exactly those distributions to PyPI via Trusted Publishing (OIDC). Because it is downstream of both other publications, the Windows ZIP, the finalized checksums, the GitHub Release and the pushed server image must all exist before anything reaches PyPI — a Windows-build or image failure can never leave a half-published release behind |

Every action is pinned to a full commit SHA with its version in a comment.
The publication paths use only short-lived identities — the run's own
GitHub token (Release creation, GHCR push) and OIDC (PyPI Trusted
Publishing, attestations): **no long-lived publication credential — no PyPI
token, no registry credential, no static publishing secret — exists in the
release-publication path or may be introduced**. The workflow's only
configured secrets remain the deliberately optional Windows code-signing
inputs (`WINDOWS_CODE_SIGNING_PFX`/`WINDOWS_CODE_SIGNING_PASSWORD`), which
belong to the Windows MSIX seam and its own external gate
(`EXTERNAL_RELEASE_GATE: WINDOWS_CODE_SIGNING`), not to any publication
path. The GHCR login uses the run's token via `--password-stdin` inside a
dedicated temporary `DOCKER_CONFIG` (exported for docker, mode 0700) that a
trap removes together with the stored credential on success and failure;
the token is never expanded into a command line, log line or URL.

Publication-recovery honesty: the ordering above makes BUILD and
VERIFICATION failures safe by construction — nothing is published unless
every artifact built and verified. It does not make publication itself
atomic: a publication job that fails AFTER a predecessor succeeded (registry
outage, rejected OIDC claim, transient API error) leaves the
already-published artifacts in place while the rest is missing. Recovery is
deliberate and safe, never fabricated: inspect which publication steps
completed before retrying only the failed job with the same verified
artifacts. GitHub Release creation and PyPI uploads are not promised to be
idempotent: an existing release or already-uploaded distribution may reject
a retry and must not be overwritten. The GHCR publisher never re-pushes a
matching immutable version while repairing only missing or stale moving
aliases (newer stable aliases are never regressed). A green run means every public
artifact exists and matches the tag, and the image aliases are
current-or-newer stable.

The artifact separation is the structural boundary that keeps release
metadata out of the PyPI publication input: `pypa/gh-action-pypi-publish`
receives `pypi-packages/` — a directory populated exclusively by the
`*.whl`/`*.tar.gz` upload globs and re-checked by the guard step before
twine runs — so checksums, attestations and any future release-metadata
files cannot reach it without a deliberate change to both the upload globs
and the guard. The `server-image` artifact follows the same discipline in
its own lane: it is consumed only by `publish-ghcr`, carries its own
checksum and provenance record, and can never enter the GitHub Release
asset list (an explicit attachment list) or the PyPI payload (whitelisted
upload globs). The public release `SHA256SUMS` stays exactly three entries
(wheel, sdist, worker zip); the image tarball's checksum is a separate
file inside the `server-image` artifact and is never attached to a
release.

Integrity and provenance (**implemented now**):

- `SHA256SUMS` — containing SHA-256 entries for every public release
  payload that needs checksum verification: the wheel, the sdist and the
  Windows worker zip. It is generated in `build` (wheel and sdist),
  completed in `finalize-release-bundle` (zip entry appended), and
  verified there against the actual bytes with `sha256sum -c` before
  anything is attested or attached. It does not contain a hash of
  itself; a downloaded release is verified with `sha256sum -c
  SHA256SUMS` in the directory holding the assets.
- each public GitHub Release file receives exactly one Sigstore-backed GitHub artifact
  attestation (`actions/attest-build-provenance`) signed with the
  workflow run's short-lived OIDC identity, covering its FINAL bytes:
  `build` attests the wheel and the sdist; `finalize-release-bundle`
  attests the worker zip and the finalized `SHA256SUMS` (so the
  attested digest is exactly the file attached to the release — the
  checksum file is never modified after it is attested). Consumers can
  verify provenance with `gh attestation verify`. No bespoke signing
  infrastructure exists.

## Artifact inventory

### Implemented now

| Artifact | Producer | Where |
| --- | --- | --- |
| `scarcity_router-X.Y.Z-py3-none-any.whl` | release workflow (`make package-check`) | GitHub Release, PyPI |
| `scarcity_router-X.Y.Z.tar.gz` | release workflow | GitHub Release, PyPI |
| `scarcity-worker-X.Y.Z-windows-x64.zip` | release workflow (`build-windows-worker` + `finalize-release-bundle`) | GitHub Release |
| `SHA256SUMS` (wheel, sdist, worker zip) | release workflow (`build` + `finalize-release-bundle`) | GitHub Release |
| Sigstore build attestations | release workflow (`build`: wheel + sdist; `finalize-release-bundle`: worker zip + final `SHA256SUMS`) | GitHub attestation store |
| `ghcr.io/creatidy/scarcity-router:X.Y.Z` server OCI image (`linux/amd64`, tags `X.Y.Z`/`X.Y`/`latest`) | release workflow (`build-server-image` + `publish-ghcr`) | GHCR — never pushed yet; publication awaits the first real release (`EXTERNAL_RELEASE_GATE: LIVE_GHCR_PUBLICATION` in [`docs/m10-acceptance.md`](m10-acceptance.md)) |

No release exists yet: creating this foundation publishes nothing. The first
actual release is a separate, human decision. The `server-image` artifact
(image tarball + checksum + provenance record) is a same-run intermediate
consumed only by `publish-ghcr` — it is not a release asset and never
reaches PyPI.

### Configured externally (owner gates before the first release)

1. **GitHub Actions enablement on the mirror** (verified missing
   2026-09-21: the Actions API lists zero registered workflows even
   though `release.yml` is on `develop` and Actions reads `enabled`).
   Open <https://github.com/creatidy/scarcity-router/actions> once with
   the owner account and enable workflows; until then neither the
   candidate preflight nor a tag can start the release pipeline
   (dispatch API answers 404). One-time, repository-level.
2. **Forgejo runner** with the `ubuntu-latest` label satisfying the runner
   boundary above.
3. **`develop` branch protection** requiring the `check` status (exact
   setting above).
4. **PyPI project** `scarcity-router` registered, with a **Trusted
   Publisher** entry: owner `creatidy`, repository `scarcity-router`,
   workflow `release.yml`, environment `pypi`. This lives on PyPI and cannot
   be represented in Git.
5. **GitHub `pypi` environment** with required reviewers, so the OIDC
   publication is also human-gated at release time (verified absent
   2026-09-21: the repository currently defines zero environments —
   pre-create `pypi` BEFORE the first release, otherwise the first
   tag run auto-creates it without reviewers).
6. **GHCR package activation**: `publish-ghcr` pushes with the run's own
   short-lived token (`packages: write`), so no registry secret is
   configured anywhere — but the first real release must confirm the
   organization permits the workflow token to create the
   `ghcr.io/creatidy/scarcity-router` package, and the owner decides the
   package's public visibility after the first successful push. Record the
   pushed digest as evidence under `EXTERNAL_RELEASE_GATE:
   LIVE_GHCR_PUBLICATION` in [`docs/m10-acceptance.md`](m10-acceptance.md).

Until 4 is configured, `publish-pypi` fails closed: PyPI rejects the OIDC
claim and nothing reaches PyPI — and by the ordering above this happens
AFTER the GitHub Release and the GHCR push exist, so the earlier
publications stay in place. Recovery is simply fixing the Trusted Publisher
entry and re-running the failed `publish-pypi` job (the same final,
checksummed bytes are re-published; nothing partially reached PyPI). That
is the designed behavior, not an error to work around.

### GHCR server image

The execution-gateway server's image definition is in-tree (M10, issue
#95): the committed `Dockerfile` builds one production OCI image from the
repository and was built and smoke-verified locally. The release-workflow
implementation now exists as code — `build-server-image` builds,
offline-verifies and smoke-runs the image from the exact commit under run,
and the tag-gated `publish-ghcr` job pushes it to GHCR — but **no workflow
run has ever executed these jobs and no image has ever been pushed**:
publication requires a real `vX.Y.Z` tag release on the GitHub mirror,
which is a human action that has not happened. The contract stays exactly
as frozen:

- namespace `ghcr.io/creatidy/scarcity-router`;
- tags `X.Y.Z`, `X.Y`, and `latest` — where `latest` means the latest stable
  release and never refers to a `develop` commit (only tag runs reach the
  publisher);
- `linux/amd64` now; `linux/arm64` only when the real image supports and
  justifies multi-architecture builds — no arm64 capability is claimed or
  built today;
- publication uses the workflow run's short-lived GitHub token
  (`packages: write` only in `publish-ghcr`, `--password-stdin` into an
  exported temporary `DOCKER_CONFIG` removed by a trap); no permanent
  registry credential;
- the image is built from the exact tagged commit and passes the same
  verify-before-publish discipline as the Python artifacts: the built
  identity labels (version, revision) and platform are verified offline,
  the composed server is smoke-run isolated (read-only root filesystem,
  ephemeral in-memory `/data` tmpfs owned by the container's non-root
  uid/gid 10001, mode 0700, dropped capabilities, no published ports, only
  `GET /healthz`), and the publisher re-verifies the artifact checksum,
  provenance and loaded image identity before any registry contact;
- absence is accepted only for the exact docker-CLI missing-manifest
  diagnostic of the exact reference; auth, transport, TLS and any other
  registry failure fails closed (never read as absence);
- retry semantics, serialized by a repository-wide publication lock (the
  `ghcr-publication` job concurrency group, `cancel-in-progress: false`):
  the immutable version tag `X.Y.Z` is pushed only when absent — present
  with different content refuses to retarget a released version, present
  with identical content is never re-pushed. Each moving alias (`X.Y`,
  `latest`) is advanced only when missing, or when a read-only
  authenticated pull proves its immutable `org.opencontainers.image.version`
  label is an older-or-equal strict stable SemVer than this release (equal
  repairs the alias to the exact tested image). A NEWER stable alias is
  preserved: an older-release retry can never regress `X.Y` or `latest`.
  A cross-line minor alias (version outside `X.Y.*`), a missing or
  non-stable label, or any pull/auth/network failure fails closed BEFORE
  any push. A fully current state is a plain no-op; green status means the
  immutable version is verified against the tested bytes and the moving
  aliases are current-or-newer stable — never regressed.

Job order in the release pipeline (all verifications upstream of every
publication; PyPI irreversible and last):
`verify → build → {build-windows-worker, build-server-image}`;
`build-windows-worker → finalize-release-bundle`; each publication job
(`publish-github-release`, `publish-ghcr`) requires BOTH
`finalize-release-bundle` AND `build-server-image`; `publish-pypi` requires
both other publications. See the job table above for the exact needs.

The structural invariants and the guard's runtime behavior are pinned by
`tests/test_release_workflow.py` and, behaviorally (offline fake-docker
execution of the extracted publish script), by
`tests/test_ghcr_publish_guard.py`.

Live status: `EXTERNAL_RELEASE_GATE: LIVE_GHCR_PUBLICATION`
([`docs/m10-acceptance.md`](m10-acceptance.md)) — the first real
publication happens only through the owner's first release tag and its
evidence is recorded there. This unit delivered workflow code and offline
tests only: no release was invoked, nothing was signed, and no live
platform acceptance is claimed.

### Windows worker packages

The native worker (M05, #90) is packaged by the release workflow's
`build-windows-worker` job (added by M10, issue #95):

- `scarcity-worker-X.Y.Z-windows-x64.zip` — the portable one-dir
  PyInstaller build (bundled interpreter; no Python install required for
  the normal Windows worker path), built on a Windows runner from the
  exact tagged commit, checksummed in `SHA256SUMS`, attested (with the
  finalized `SHA256SUMS`) by `finalize-release-bundle`, and attached to
  the GitHub Release;
- `scarcity-worker-X.Y.Z-windows-x64.msix` — the preferred main
  installation direction, structured as a fail-closed seam: the job's
  MSIX step activates only when the owner configures a code-signing
  secret AND refuses to produce a package until the MSIX manifest/assets
  work is implemented with dated evidence. No unsigned MSIX is ever
  published and no signature is fabricated.

Live end-user acceptance of the produced package is recorded as
`EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE` in
[`docs/m10-acceptance.md`](m10-acceptance.md); the packaging shape itself
(spec structure, tray module, launcher) is verified by the deterministic
`tests/test_windows_packaging.py` / `tests/test_windows_tray.py` suites.

## Installation contract (Linux/WSL, Python component)

**Future contract** — activates with the first PyPI publication:

```bash
uv tool install scarcity-router
```

with the conventional alternative:

```bash
pipx install scarcity-router
```

Today the package is not on any index; the working installation paths are the
checkout-based ones in the README. Deliberate boundaries:

- no custom apt, RPM or pacman repositories (**not yet supported**);
- a Homebrew tap may be considered later, but is not part of this contract;
- Python-package distribution is distinct from the eventual native
  Linux-worker packaging — one does not substitute for the other.

## How #95 builds on this foundation

Issue #95 owns the user-facing distribution work — first-run friction,
update/uninstall flows, clean-environment install verification, server
container publication, worker packaging and end-to-end acceptance — and
consumes, rather than re-decides:

- the CI job (`check`) as the regression gate for every packaging change;
- `make package-check` as the reusable build/verify/smoke harness;
- the release workflow's job pattern for adding the GHCR and Windows
  artifact jobs under the same invariants;
- the SemVer/main-ancestry/exact-commit provenance contract for every public
  artifact it introduces.

## Validation

```bash
uv sync --only-dev
make check
uv run basedpyright
uv build
git diff --check
make package-check
```

Workflow YAML is not executable locally; changes to either workflow must be
checked with a deterministic parser/linter (for example `actionlint` for the
GitHub workflow) and the triggers, permissions and job graph re-read
manually, as done for issue #97. The release graph, event gates and
publication ordering are additionally pinned by the structural suite
`tests/test_release_workflow.py` (runs in the normal gate; parses the
workflow YAML and fails on any change that could let a candidate run
publish, a PR event publish, or PyPI publish before the finalized bundle,
the GitHub Release and the GHCR push). The GHCR publish guard's runtime
behavior is additionally executed offline — against a deterministic fake
`docker` and a synthetic token, with no registry or network — by
`tests/test_ghcr_publish_guard.py`, which proves the refusal ordering
(every identity/artifact mismatch refuses before login), the stdin-only
token delivery, the released-version conflict/idempotency gate and the
exact pushed tag set.
