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
| Server OCI images on GHCR | Future job in `.github/workflows/release.yml` | Future contract |
| Windows worker packages (MSIX/ZIP) | Future job in `.github/workflows/release.yml` | Future contract |
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
                      # discovery, loopback REST healthz)
```

`make package-check` (`tools/package_check.py`) validates the distributable
package, not only the source checkout: missing packaged files, invalid
metadata, broken entry points, undeclared runtime dependencies and accidental
dependencies on repository-relative files all fail the job. It contacts no
provider and never publishes anything.

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
- **Release authority is separated from development CI**: a green `check` on
  `develop` authorizes nothing except a human merge; publication authority
  lives exclusively in the human-created release-tag path.
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
4. the artifacts are built from the exact tagged commit — every later job of
   the run checks out that same commit, and artifacts travel to the publish
   jobs only through this run's upload;
5. the artifacts pass their applicable verification (`make package-check`)
   before any publication job starts.

Additionally frozen:

- versions are never bumped automatically;
- `develop` is never promoted to `main` automatically;
- arbitrary development commits are never released;
- tags are intentional human actions on `main` only.

Release procedure (human): promote the reviewed `develop` state to `main`,
set `__version__` through the normal PR flow beforehand, then create the
annotated tag `vX.Y.Z` on the intended `main` commit and push it. Everything
after that is the workflow's job — or a fail-closed refusal.

## Release pipeline mechanics

**Implemented now**; activation requires the owner actions below.

`.github/workflows/release.yml` on the GitHub mirror, four jobs:

| Job | Needs | Permissions | Does |
| --- | --- | --- | --- |
| `verify-release-contract` | — | `contents: read` | checks out the tag with full history and enforces invariants 1–3 |
| `build-and-verify-artifacts` | verify | `contents: read`, `id-token: write`, `attestations: write` | builds and smoke-installs the artifacts from the exact tagged commit (`make package-check`), writes `SHA256SUMS`, attests build provenance, uploads artifacts |
| `publish-github-release` | build | `contents: write` | creates the GitHub Release and attaches the wheel, sdist and `SHA256SUMS` |
| `publish-pypi` | build | `id-token: write` (environment `pypi`) | publishes the verified artifacts to PyPI via Trusted Publishing (OIDC) |

Every action is pinned to a full commit SHA with its version in a comment.
The only credentials used are the run's own short-lived GitHub token and
OIDC identities — **no long-lived PyPI token exists or may be introduced**.

Integrity and provenance (**implemented now**):

- `SHA256SUMS` over the wheel, the sdist and itself is attached to every
  GitHub Release;
- each artifact plus `SHA256SUMS` receives a Sigstore-backed GitHub artifact
  attestation (`actions/attest-build-provenance`) signed with the workflow
  run's short-lived OIDC identity. Consumers can verify provenance with
  `gh attestation verify`. No bespoke signing infrastructure exists.

## Artifact inventory

### Implemented now

| Artifact | Producer | Where |
| --- | --- | --- |
| `scarcity_router-X.Y.Z-py3-none-any.whl` | release workflow (`make package-check`) | GitHub Release, PyPI |
| `scarcity_router-X.Y.Z.tar.gz` | release workflow | GitHub Release, PyPI |
| `SHA256SUMS` | release workflow | GitHub Release |
| Sigstore build attestations | release workflow | GitHub attestation store |

No release exists yet: creating this foundation publishes nothing. The first
actual release is a separate, human decision.

### Configured externally (owner gates before the first release)

1. **Forgejo runner** with the `ubuntu-latest` label satisfying the runner
   boundary above.
2. **`develop` branch protection** requiring the `check` status (exact
   setting above).
3. **PyPI project** `scarcity-router` registered, with a **Trusted
   Publisher** entry: owner `creatidy`, repository `scarcity-router`,
   workflow `release.yml`, environment `pypi`. This lives on PyPI and cannot
   be represented in Git.
4. **GitHub `pypi` environment** with required reviewers, so the OIDC
   publication is also human-gated at release time.

Until 3 is configured, `publish-pypi` fails closed: PyPI rejects the OIDC
claim, nothing is uploaded, and the GitHub Release job is unaffected. That is
the designed behavior, not an error to work around.

### Future contract: GHCR server image

The execution-gateway server (M03, #88) is expected to produce one
production OCI image eventually. Its release contract is frozen now; **no
such image exists today and no image job is present in the workflow**:

- namespace `ghcr.io/creatidy/scarcity-router`;
- tags `X.Y.Z`, `X.Y`, and `latest` — where `latest` means the latest stable
  release and never refers to a `develop` commit;
- `linux/amd64` and `linux/arm64` when the real image supports and justifies
  multi-architecture builds;
- publication uses the workflow run's short-lived GitHub token / OIDC
  identity; no permanent registry credential;
- the image is built from the exact tagged commit and passes the same
  verify-before-publish discipline as the Python artifacts.

Insertion point: one additional publish job in `.github/workflows/release.yml`
(`needs: build`, gated on the same verified artifacts), added by the issue
that actually introduces the supported server image. Adding the image without
extending this contract first is a contract violation.

### Future contract: Windows worker packages

The native worker (M05, #90) does not exist yet. Its eventual public
artifact contract is recorded only:

- `scarcity-worker-X.Y.Z-windows-x64.msix` — the preferred main installation
  direction, subject to implementation evidence;
- `scarcity-worker-X.Y.Z-windows-x64.zip` — optional portable artifact for
  testing and diagnostics;
- built on Windows runners, from the exact tagged commit;
- code signing is part of reaching professional public-release quality and
  will need its own short-lived-identity or documented owner decision;
- installer/update/uninstall UX belongs to #95, not to this foundation.

Insertion point: additional jobs in `.github/workflows/release.yml`, added by
the issue that produces a real, supported worker artifact. No placeholder
binaries, no claimed MSIX support and no simulated publish job may exist
before then.

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
manually, as done for issue #97.
