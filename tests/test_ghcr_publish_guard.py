"""Deterministic behavioral execution of the release workflow's GHCR publish
guard and image smoke (issue #186).

`tests/test_release_workflow.py` pins the release workflow's structure; this
suite executes the `publish-ghcr` job's actual guard script AND the
`build-server-image` job's actual smoke script — extracted from
`.github/workflows/release.yml` at test time — against a deterministic fake
`docker` command, so the security-relevant BEHAVIOR is exercised offline
instead of only claimed by substring matches:

- every artifact/identity mismatch (candidate event, non-tag ref, malformed
  or mismatched tag, checksum, provenance version/revision/platform, image
  id, labels) refuses BEFORE `docker login` ever runs;
- only a fully validated path logs in — with the synthetic token delivered
  through stdin (`--password-stdin`) into an EXPORTED temporary
  `DOCKER_CONFIG` (mode 0700) that the trap removes on success and failure;
  the synthetic HOME is untouched and the token is never printed;
- absence is accepted only for the exact missing-manifest diagnostic of the
  exact reference; auth failures, transport errors, TLS/certificate noise,
  URL-embedded 404s and wrong-reference diagnostics all fail closed;
- the immutable version tag is pushed only when absent (present with
  different content refuses to retarget a released version); each moving
  alias (X.Y, latest) is advanced only when missing or provably holding an
  older-or-equal stable version label (equal repairs to the exact tested
  image), a NEWER stable alias is preserved — an older-release retry never
  regresses the moving aliases — and unprovable metadata (missing,
  non-stable, cross-line-minor label, pull/auth/network failure) fails
  closed before any push;
- the image smoke runs the container with a non-root-writable ephemeral
  tmpfs /data (uid/gid 10001, mode 0700), a read-only root filesystem,
  dropped capabilities, no-new-privileges and no published ports.

No real registry, no Docker daemon and no network is involved: `docker` is
a deterministic fake on PATH, the token is synthetic, and every fixture is
generated in a cleaned OS temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

GHCR_PUBLISH_STEP_NAME = "Verify the tested image and publish to GHCR (fail closed)"
SMOKE_STEP_NAME = "Smoke the composed server in the built image (isolated, /healthz only)"

SYNTHETIC_TOKEN = "sr-synthetic-publish-token-0123456789abcdef"
SYNTHETIC_SHA = "f" * 40
SYNTHETIC_VERSION = "0.1.0"
SYNTHETIC_TAG = "v0.1.0"
SYNTHETIC_IMAGE_ID = "sha256:" + "3" * 64
SYNTHETIC_PLATFORM = "linux/amd64"
GHCR_REPOSITORY = "ghcr.io/creatidy/scarcity-router"
EXPECTED_PUSHES = (
    f"{GHCR_REPOSITORY}:{SYNTHETIC_VERSION}",
    f"{GHCR_REPOSITORY}:0.1",
    f"{GHCR_REPOSITORY}:latest",
)


def _workflow_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def _extract_step_script(step_name: str) -> str:
    """Extract a named step's shell from the workflow at test time."""
    from typing import cast

    import yaml

    document = cast(
        "dict[object, object]", yaml.safe_load(_workflow_path().read_text(encoding="utf-8"))
    )
    jobs = cast("dict[object, object]", document.get("jobs"))
    for job in jobs.values():
        job_document = cast("dict[object, object]", job)
        steps = job_document.get("steps")
        assert isinstance(steps, list)
        for step in cast("list[object]", steps):
            assert isinstance(step, dict)
            step_document = cast("dict[object, object]", step)
            if step_document.get("name") == step_name:
                run = step_document.get("run")
                assert isinstance(run, str)
                return run
    raise AssertionError(f"workflow step {step_name!r} not found")


class FakeDocker:
    """Configuration for the deterministic fake `docker` used by the guard.

    `manifest_mode` drives the remote state of the immutable version tag;
    `minor_mode`/`latest_mode` default to it when unset, so each moving
    alias can be scripted independently (absent, same, diff, http404, cert,
    unauthorized, dial, absent-other). When a moving alias exists with
    different content, the guard pulls it and reads its immutable version
    label; `alias_minor_labels`/`alias_latest_labels` provide that pulled
    image's labels (default: the same release version as the tested image).
    """

    def __init__(
        self,
        *,
        manifest_mode: str = "absent",
        minor_mode: str | None = None,
        latest_mode: str | None = None,
        image_id: str = SYNTHETIC_IMAGE_ID,
        platform: str = SYNTHETIC_PLATFORM,
        labels: dict[str, str] | None = None,
        alias_minor_labels: dict[str, str] | None = None,
        alias_latest_labels: dict[str, str] | None = None,
    ) -> None:
        self.manifest_mode: str = manifest_mode
        self.minor_mode: str = minor_mode if minor_mode is not None else manifest_mode
        self.latest_mode: str = latest_mode if latest_mode is not None else manifest_mode
        self.image_id: str = image_id
        self.platform: str = platform
        if labels is None:
            labels = {
                "org.opencontainers.image.title": "scarcity-router",
                "org.opencontainers.image.version": SYNTHETIC_VERSION,
                "org.opencontainers.image.revision": SYNTHETIC_SHA,
            }
        self.labels: dict[str, str] = labels
        if alias_minor_labels is None:
            alias_minor_labels = {
                "org.opencontainers.image.title": "scarcity-router",
                "org.opencontainers.image.version": SYNTHETIC_VERSION,
            }
        self.alias_minor_labels: dict[str, str] = alias_minor_labels
        if alias_latest_labels is None:
            alias_latest_labels = {
                "org.opencontainers.image.title": "scarcity-router",
                "org.opencontainers.image.version": SYNTHETIC_VERSION,
            }
        self.alias_latest_labels: dict[str, str] = alias_latest_labels

    def script(self) -> str:
        same_digest = self.image_id
        diff_digest = "sha256:" + "2" * 64
        labels_json = shlex.quote(json.dumps(self.labels, sort_keys=True))
        minor_labels_json = shlex.quote(json.dumps(self.alias_minor_labels, sort_keys=True))
        latest_labels_json = shlex.quote(json.dumps(self.alias_latest_labels, sort_keys=True))
        return f"""#!/usr/bin/env bash
set -u
LOG_DIR="$FAKE_DOCKER_LOG"
mkdir -p "$LOG_DIR"
{{
  printf 'ARGS:'
  for arg in "$@"; do printf ' %q' "$arg"; done
  printf '\\n'
}} >> "$LOG_DIR/calls.log"
case "${{1:-}}" in
  load)
    exit 0
    ;;
  pull)
    if [ "${{FAKE_PULL_FAILS:-0}}" = "1" ]; then
      printf 'Get "https://ghcr.io/v2/": dial tcp: connection refused\\n' >&2
      exit 1
    fi
    printf '%s\\n' "$2" >> "$LOG_DIR/pulls.txt"
    exit 0
    ;;
  image)
    fmt="${{4:-}}"
    ref="${{5:-}}"
    tag="${{ref##*:}}"
    case "$fmt" in
      '{{{{.Id}}}}')
        printf '%s\\n' {shlex.quote(self.image_id)}
        ;;
      '{{{{.Os}}}}/{{{{.Architecture}}}}')
        printf '%s\\n' {shlex.quote(self.platform)}
        ;;
      '{{{{json .Config.Labels}}}}')
        case "$tag" in
          latest)
            printf '%s\\n' {latest_labels_json}
            ;;
          *.*.*)
            printf '%s\\n' {labels_json}
            ;;
          *.*)
            printf '%s\\n' {minor_labels_json}
            ;;
        esac
        ;;
      *RepoDigests*)
        printf '%s\\n' {shlex.quote(self.image_id)}
        ;;
      *)
        printf 'fake docker: unexpected image inspect format: %s\\n' "$fmt" >&2
        exit 64
        ;;
    esac
    exit 0
    ;;
  manifest)
    ref="${{4:-}}"
    tag="${{ref##*:}}"
    case "$tag" in
      latest) mode="${{FAKE_MANIFEST_LATEST_MODE:-absent}}" ;;
      *.*.*) mode="${{FAKE_MANIFEST_VERSION_MODE:-absent}}" ;;
      *.*) mode="${{FAKE_MANIFEST_MINOR_MODE:-absent}}" ;;
      *) mode="absent" ;;
    esac
    case "$mode" in
      absent)
        printf 'no such manifest: %s\\n' "$ref" >&2
        exit 1
        ;;
      absent-other)
        printf 'no such manifest: {GHCR_REPOSITORY}:0.1.9\\n' >&2
        exit 1
        ;;
      http404)
        printf 'unexpected status: 404 Not Found; Get "https://ghcr.io/v2/creatidy/scarcity-router/manifests/0.1.0": EOF\\n' >&2
        exit 1
        ;;
      cert)
        printf 'error: certificate file not found: /etc/ssl/certs/ca.crt\\n' >&2
        exit 1
        ;;
      unauthorized)
        printf 'unexpected status: 401 Unauthorized\\n' >&2
        exit 1
        ;;
      dial)
        printf 'Get "https://ghcr.io/v2/": dial tcp: connection refused\\n' >&2
        exit 1
        ;;
      same)
        printf '{{"Ref":"%s","Descriptor":{{"mediaType":"application/vnd.docker.distribution.manifest.v2+json","digest":"sha256:aaaaaaaa","size":528}},"SchemaV2Manifest":{{"schemaVersion":2,"config":{{"mediaType":"application/vnd.docker.container.image.v1+json","size":1234,"digest":"{same_digest}"}},"layers":[]}}}}\\n' "$ref"
        ;;
      diff)
        printf '{{"Ref":"%s","Descriptor":{{"mediaType":"application/vnd.docker.distribution.manifest.v2+json","digest":"sha256:aaaaaaaa","size":528}},"SchemaV2Manifest":{{"schemaVersion":2,"config":{{"mediaType":"application/vnd.docker.container.image.v1+json","size":1234,"digest":"{diff_digest}"}},"layers":[]}}}}\\n' "$ref"
        ;;
      *)
        printf 'fake docker: unknown manifest mode\\n' >&2
        exit 64
        ;;
    esac
    exit 0
    ;;
  login)
    if [ -z "${{DOCKER_CONFIG:-}}" ]; then
      printf 'fake docker: DOCKER_CONFIG is not exported\\n' >&2
      exit 1
    fi
    config_mode="$(stat -c %a "${{DOCKER_CONFIG}}")"
    printf 'DOCKER_CONFIG=%s mode=%s\\n' "${{DOCKER_CONFIG}}" "${{config_mode}}" >> "$LOG_DIR/docker-config.log"
    mkdir -p "${{DOCKER_CONFIG}}"
    printf '{{"auths":{{"ghcr.io":{{"auth":"c3ludGhldGljLXVzZXI6c3ludGhldGljLXRva2Vu"}}}}}}\\n' > "${{DOCKER_CONFIG}}/config.json"
    printf 'config-written\\n' >> "$LOG_DIR/docker-config.log"
    cat > "$LOG_DIR/login-stdin.txt"
    exit 0
    ;;
  tag)
    exit 0
    ;;
  push)
    printf '%s\\n' "$2" >> "$LOG_DIR/pushes.txt"
    exit 0
    ;;
  *)
    printf 'fake docker: unsupported invocation: %s\\n' "$*" >&2
    exit 64
    ;;
esac
"""


class SmokeFakeDocker:
    """Deterministic fake `docker` for the image smoke script."""

    def script(self) -> str:
        return """#!/usr/bin/env bash
set -u
LOG_DIR="$FAKE_DOCKER_LOG"
mkdir -p "$LOG_DIR"
{
  printf 'ARGS:'
  for arg in "$@"; do printf ' %q' "$arg"; done
  printf '\\n'
} >> "$LOG_DIR/calls.log"
case "${1:-}" in
  run)
    printf '%s\\n' "container-id-synthetic"
    exit 0
    ;;
  inspect)
    printf '%s\\n' "${FAKE_HEALTH:-healthy}"
    exit 0
    ;;
  exec)
    printf '%s\\n' "${FAKE_HEALTHZ_CODE:-200}"
    exit 0
    ;;
  logs)
    printf 'synthetic server log\\n'
    exit 0
    ;;
  rm)
    printf '%s\\n' "$*" >> "$LOG_DIR/cleanup.log"
    exit 0
    ;;
  *)
    printf 'fake docker: unsupported invocation: %s\\n' "$*" >&2
    exit 64
    ;;
esac
"""


class GhcrPublishGuardTests(unittest.TestCase):
    """Execute the extracted publish guard offline against a fake docker."""

    root: Path
    log_dir: Path
    home: Path

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.root = Path("/nonexistent")
        self.log_dir = Path("/nonexistent")
        self.home = Path("/nonexistent")

    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sr-ghcr-guard-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log_dir = self.root / "fake-docker-log"
        _ = self.log_dir.mkdir()
        # Synthetic HOME with a canary: the guard must never write outside
        # its own temporary DOCKER_CONFIG (no inheritance from the owner).
        self.home = self.root / "home"
        _ = self.home.mkdir()
        _ = (self.home / "canary.txt").write_text("synthetic home canary\n", encoding="utf-8")

    def _write_fake_docker(self, fake: FakeDocker | SmokeFakeDocker) -> Path:
        bindir = self.root / "bin"
        _ = bindir.mkdir(exist_ok=True)
        docker = bindir / "docker"
        _ = docker.write_text(fake.script(), encoding="utf-8")
        _ = docker.chmod(docker.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return bindir

    def _write_artifact(
        self,
        *,
        version: str = SYNTHETIC_VERSION,
        revision: str = SYNTHETIC_SHA,
        image_id: str = SYNTHETIC_IMAGE_ID,
        platform: str = SYNTHETIC_PLATFORM,
        corrupt: bool = False,
    ) -> None:
        image_dir = self.root / "server-image"
        _ = image_dir.mkdir()
        tar_name = f"scarcity-router-image-{version}-linux-amd64.tar"
        tar_path = image_dir / tar_name
        _ = tar_path.write_bytes(b"synthetic offline image bytes\n")
        # The recorded checksum covers the ORIGINAL bytes; corrupting the
        # tarball afterwards simulates a damaged artifact.
        digest = hashlib.sha256(tar_path.read_bytes()).hexdigest()
        if corrupt:
            _ = tar_path.write_bytes(b"tampered\n")
        _ = (image_dir / f"{tar_name}.sha256").write_text(
            f"{digest}  {tar_name}\n", encoding="utf-8"
        )
        provenance = {
            "image": GHCR_REPOSITORY,
            "image_id": image_id,
            "platform": platform,
            "revision": revision,
            "version": version,
        }
        _ = (image_dir / "server-image-provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _run_guard(
        self,
        fake: FakeDocker,
        *,
        env_overrides: dict[str, str] | None = None,
        artifact_version: str = SYNTHETIC_VERSION,
        artifact_revision: str = SYNTHETIC_SHA,
        artifact_image_id: str = SYNTHETIC_IMAGE_ID,
        artifact_platform: str = SYNTHETIC_PLATFORM,
        corrupt: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        _ = self._write_fake_docker(fake)
        self._write_artifact(
            version=artifact_version,
            revision=artifact_revision,
            image_id=artifact_image_id,
            platform=artifact_platform,
            corrupt=corrupt,
        )
        script_path = self.root / "guard.sh"
        _ = script_path.write_text(_extract_step_script(GHCR_PUBLISH_STEP_NAME), encoding="utf-8")
        env = {
            "PATH": f"{self.root / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            # Keep the guard's own mktemp -d inside the test root so the
            # containment/removal assertions below are meaningful.
            "TMPDIR": str(self.root),
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": f"refs/tags/{SYNTHETIC_TAG}",
            "GITHUB_REF_NAME": SYNTHETIC_TAG,
            "GITHUB_SHA": SYNTHETIC_SHA,
            "GITHUB_ACTOR": "sr-synthetic-actor",
            "GHCR_TOKEN": SYNTHETIC_TOKEN,
            "GITHUB_STEP_SUMMARY": str(self.root / "step-summary.txt"),
            "FAKE_DOCKER_LOG": str(self.log_dir),
            "FAKE_MANIFEST_VERSION_MODE": fake.manifest_mode,
            "FAKE_MANIFEST_MINOR_MODE": fake.minor_mode,
            "FAKE_MANIFEST_LATEST_MODE": fake.latest_mode,
        }
        env.update(env_overrides or {})
        return subprocess.run(
            ["bash", str(script_path)],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def _calls(self) -> str:
        calls = self.log_dir / "calls.log"
        if not calls.exists():
            return ""
        return calls.read_text(encoding="utf-8")

    def _pushed_refs(self) -> list[str]:
        pushes = self.log_dir / "pushes.txt"
        if not pushes.exists():
            return []
        return [
            line
            for line in pushes.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def _pulled_refs(self) -> list[str]:
        pulls = self.log_dir / "pulls.txt"
        if not pulls.exists():
            return []
        return [
            line
            for line in pulls.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def _docker_config_record(self) -> tuple[str, str] | None:
        log = self.log_dir / "docker-config.log"
        if not log.exists():
            return None
        first = log.read_text(encoding="utf-8").splitlines()[0]
        tail = first[len("DOCKER_CONFIG="):]
        path, _, mode = tail.rpartition(" mode=")
        return path, mode

    def _assert_refused_before_login(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("FAIL:", result.stderr)
        self.assertNotIn("login", self._calls())
        self.assertIsNone(self._docker_config_record())

    def _assert_config_lifecycle_clean(self) -> None:
        record = self._docker_config_record()
        assert record is not None, "fake docker never observed an exported DOCKER_CONFIG"
        path, mode = record
        self.assertEqual("700", mode, "temporary DOCKER_CONFIG must be private")
        self.assertTrue(path.startswith(str(self.root)), "DOCKER_CONFIG escaped the test root")
        self.assertNotEqual(path, str(self.home), "DOCKER_CONFIG must not be the synthetic HOME")
        self.assertFalse(Path(path).exists(), "temporary DOCKER_CONFIG survived the script")
        self.assertTrue((self.home / "canary.txt").exists(), "synthetic HOME was modified")
        self.assertFalse((self.home / "config.json").exists(), "credential written to HOME")
        self.assertFalse((self.home / ".docker").exists(), "credential directory written to HOME")

    def _assert_token_never_printed(self, result: subprocess.CompletedProcess[str]) -> None:
        recorded = "\n".join(
            text
            for text in (
                result.stdout,
                result.stderr,
                self._calls(),
                (self.log_dir / "docker-config.log").read_text(encoding="utf-8")
                if (self.log_dir / "docker-config.log").exists()
                else "",
                (self.log_dir / "pushes.txt").read_text(encoding="utf-8")
                if (self.log_dir / "pushes.txt").exists()
                else "",
            )
        )
        self.assertNotIn(SYNTHETIC_TOKEN, recorded)

    def test_validated_path_pushes_the_frozen_tag_set_with_the_token_via_stdin(self) -> None:
        result = self._run_guard(FakeDocker())
        self.assertEqual(0, result.returncode, result.stderr)
        calls = self._calls()
        self.assertEqual(1, calls.count(" login "), calls)
        login_stdin = (self.log_dir / "login-stdin.txt").read_text(encoding="utf-8")
        # The token reaches docker through stdin, exactly once, unmodified.
        self.assertEqual(SYNTHETIC_TOKEN, login_stdin)
        self.assertEqual(list(EXPECTED_PUSHES), self._pushed_refs())
        self._assert_token_never_printed(result)
        self._assert_config_lifecycle_clean()
        summary = (self.root / "step-summary.txt").read_text(encoding="utf-8")
        self.assertIn("pushed ghcr image identity", summary)
        self.assertIn(f"{GHCR_REPOSITORY}:latest", summary)

    def test_candidate_event_refuses_before_login(self) -> None:
        result = self._run_guard(
            FakeDocker(), env_overrides={"GITHUB_EVENT_NAME": "workflow_dispatch"}
        )
        self._assert_refused_before_login(result)

    def test_branch_ref_refuses_before_login(self) -> None:
        result = self._run_guard(
            FakeDocker(),
            env_overrides={"GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main"},
        )
        self._assert_refused_before_login(result)

    def test_non_semver_tag_refuses_before_login(self) -> None:
        result = self._run_guard(
            FakeDocker(),
            env_overrides={"GITHUB_REF": "refs/tags/v1.2", "GITHUB_REF_NAME": "v1.2"},
        )
        self._assert_refused_before_login(result)

    def test_version_mismatch_refuses_before_login(self) -> None:
        result = self._run_guard(FakeDocker(), artifact_version="0.2.0")
        self._assert_refused_before_login(result)
        self.assertIn("version", result.stderr)

    def test_revision_mismatch_refuses_before_login(self) -> None:
        result = self._run_guard(FakeDocker(), artifact_revision="a" * 40)
        self._assert_refused_before_login(result)
        self.assertIn("revision", result.stderr)

    def test_provenance_platform_mismatch_refuses_before_login(self) -> None:
        result = self._run_guard(FakeDocker(), artifact_platform="linux/arm64")
        self._assert_refused_before_login(result)
        self.assertIn("provenance platform", result.stderr)

    def test_checksum_mismatch_refuses_before_login(self) -> None:
        result = self._run_guard(FakeDocker(), corrupt=True)
        self._assert_refused_before_login(result)
        self.assertIn("checksum", result.stderr)

    def test_label_mismatch_refuses_before_login(self) -> None:
        labels = {
            "org.opencontainers.image.title": "scarcity-router",
            "org.opencontainers.image.version": SYNTHETIC_VERSION,
            "org.opencontainers.image.revision": "b" * 40,
        }
        result = self._run_guard(FakeDocker(labels=labels))
        self._assert_refused_before_login(result)
        self.assertIn("labels", result.stderr)

    def test_platform_mismatch_refuses_before_login(self) -> None:
        result = self._run_guard(FakeDocker(platform="linux/arm64"))
        self._assert_refused_before_login(result)
        self.assertIn("platform", result.stderr)

    def test_image_id_mismatch_refuses_before_login(self) -> None:
        result = self._run_guard(
            FakeDocker(image_id="sha256:" + "9" * 64), artifact_image_id=SYNTHETIC_IMAGE_ID
        )
        self._assert_refused_before_login(result)
        self.assertIn("image id", result.stderr)

    def test_released_version_with_different_content_refuses_before_push(self) -> None:
        result = self._run_guard(FakeDocker(manifest_mode="diff"))
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("refusing", result.stderr)
        self.assertIn("login", self._calls())  # the conflict gate needs the session
        self.assertEqual([], self._pushed_refs())
        self._assert_config_lifecycle_clean()  # failure still removes the credential

    def test_exact_missing_diagnostic_is_the_only_accepted_absence(self) -> None:
        # The real known CLI shape for the exact reference: absence.
        result = self._run_guard(FakeDocker(manifest_mode="absent"))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(list(EXPECTED_PUSHES), self._pushed_refs())

    def test_missing_diagnostic_for_wrong_ref_is_not_absence(self) -> None:
        result = self._run_guard(FakeDocker(manifest_mode="absent-other"))
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("unrecognized diagnostic", result.stderr)
        self.assertEqual([], self._pushed_refs())

    def test_http_404_in_registry_error_is_not_absence(self) -> None:
        result = self._run_guard(FakeDocker(manifest_mode="http404"))
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("unrecognized diagnostic", result.stderr)
        self.assertEqual([], self._pushed_refs())

    def test_certificate_not_found_is_not_absence(self) -> None:
        result = self._run_guard(FakeDocker(manifest_mode="cert"))
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("unrecognized diagnostic", result.stderr)
        self.assertEqual([], self._pushed_refs())

    def test_unauthorized_is_not_absence(self) -> None:
        result = self._run_guard(FakeDocker(manifest_mode="unauthorized"))
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("unrecognized diagnostic", result.stderr)
        self.assertEqual([], self._pushed_refs())

    def test_moving_tag_transport_failure_fails_closed_before_any_push(self) -> None:
        result = self._run_guard(
            FakeDocker(manifest_mode="absent", minor_mode="absent", latest_mode="dial")
        )
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("unrecognized diagnostic", result.stderr)
        self.assertEqual([], self._pushed_refs())

    def test_identical_released_content_everywhere_is_an_idempotent_no_op(self) -> None:
        result = self._run_guard(
            FakeDocker(manifest_mode="same", minor_mode="same", latest_mode="same")
        )
        self.assertEqual(0, result.returncode, result.stderr)
        calls = self._calls()
        self.assertNotIn(" push ", calls)
        self.assertNotIn(" tag ", calls)
        # Alias content matches exactly: no proof pull is even needed.
        self.assertNotIn(" pull ", calls)
        summary = (self.root / "step-summary.txt").read_text(encoding="utf-8")
        self.assertIn("idempotent no-op", summary)
        self._assert_config_lifecycle_clean()

    def test_older_release_retry_preserves_newer_stable_aliases(self) -> None:
        # A retry of an older release's publish job (its immutable version
        # already present with identical bytes) must NEVER regress the
        # moving aliases when they hold newer stable releases.
        result = self._run_guard(
            FakeDocker(
                manifest_mode="same",
                minor_mode="diff",
                latest_mode="diff",
                alias_minor_labels={"org.opencontainers.image.version": "0.1.5"},
                alias_latest_labels={"org.opencontainers.image.version": "1.4.2"},
            )
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self._pushed_refs())
        # Both aliases were pulled read-only for the version proof.
        self.assertEqual(
            [f"{GHCR_REPOSITORY}:0.1", f"{GHCR_REPOSITORY}:latest"], self._pulled_refs()
        )
        summary = (self.root / "step-summary.txt").read_text(encoding="utf-8")
        self.assertIn("idempotent no-op", summary)
        self.assertIn("current-or-newer stable", summary)
        self._assert_config_lifecycle_clean()

    def test_newer_release_advances_older_aliases(self) -> None:
        # A patch release advances its IN-SCOPE minor alias (0.2.x holding
        # the older 0.2.0) and `latest`; the first run of a new minor line
        # simply finds `X.Y` absent. Cross-line labels stay fail-closed
        # (covered by the scope test below).
        result = self._run_guard(
            FakeDocker(
                manifest_mode="absent",
                minor_mode="diff",
                latest_mode="diff",
                labels={
                    "org.opencontainers.image.title": "scarcity-router",
                    "org.opencontainers.image.version": "0.2.1",
                    "org.opencontainers.image.revision": SYNTHETIC_SHA,
                },
                alias_minor_labels={"org.opencontainers.image.version": "0.2.0"},
                alias_latest_labels={"org.opencontainers.image.version": "0.2.0"},
            ),
            env_overrides={
                "GITHUB_REF": "refs/tags/v0.2.1",
                "GITHUB_REF_NAME": "v0.2.1",
            },
            artifact_version="0.2.1",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [
                f"{GHCR_REPOSITORY}:0.2.1",
                f"{GHCR_REPOSITORY}:0.2",
                f"{GHCR_REPOSITORY}:latest",
            ],
            self._pushed_refs(),
        )
        self.assertEqual(
            [f"{GHCR_REPOSITORY}:0.2", f"{GHCR_REPOSITORY}:latest"],
            self._pulled_refs(),
        )
        self._assert_config_lifecycle_clean()

    def test_same_version_divergent_alias_is_repaired_to_tested_image(self) -> None:
        # The alias provably holds THIS release's version but different
        # bytes (e.g. a partially failed earlier attempt): repair it to the
        # exact tested immutable image without re-pushing the version tag.
        result = self._run_guard(
            FakeDocker(
                manifest_mode="same",
                minor_mode="diff",
                latest_mode="absent",
                alias_minor_labels={"org.opencontainers.image.version": SYNTHETIC_VERSION},
            )
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [f"{GHCR_REPOSITORY}:0.1", f"{GHCR_REPOSITORY}:latest"], self._pushed_refs()
        )
        self.assertNotIn(
            f"push {GHCR_REPOSITORY}:{SYNTHETIC_VERSION}", self._calls()
        )
        self._assert_config_lifecycle_clean()

    def test_invalid_alias_version_label_fails_closed_before_any_push(self) -> None:
        result = self._run_guard(
            FakeDocker(
                manifest_mode="absent",
                minor_mode="diff",
                alias_minor_labels={"org.opencontainers.image.version": "v0.1-beta"},
            )
        )
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("unusable metadata", result.stderr)
        self.assertEqual([], self._pushed_refs())

    def test_missing_alias_version_label_fails_closed(self) -> None:
        result = self._run_guard(
            FakeDocker(
                manifest_mode="absent",
                minor_mode="diff",
                alias_minor_labels={"org.opencontainers.image.title": "scarcity-router"},
            )
        )
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("unusable metadata", result.stderr)
        self.assertEqual([], self._pushed_refs())

    def test_minor_alias_outside_its_major_minor_scope_fails_closed(self) -> None:
        # The X.Y alias provably points at another minor line: that is an
        # unprovable/corrupt state, never silently repaired or preserved.
        result = self._run_guard(
            FakeDocker(
                manifest_mode="absent",
                minor_mode="diff",
                alias_minor_labels={"org.opencontainers.image.version": "0.2.1"},
            )
        )
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("major.minor scope", result.stderr)
        self.assertEqual([], self._pushed_refs())

    def test_alias_pull_failure_fails_closed_before_any_push(self) -> None:
        result = self._run_guard(
            FakeDocker(manifest_mode="absent", minor_mode="diff"),
            env_overrides={"FAKE_PULL_FAILS": "1"},
        )
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("pull failed", result.stderr)
        self.assertEqual([], self._pushed_refs())

    def test_partial_publication_retry_repairs_missing_moving_tags(self) -> None:
        # First attempt died after pushing the immutable version: the retry
        # must push ONLY the missing moving tags, never re-push or move the
        # immutable version.
        result = self._run_guard(
            FakeDocker(manifest_mode="same", minor_mode="absent", latest_mode="absent")
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [f"{GHCR_REPOSITORY}:0.1", f"{GHCR_REPOSITORY}:latest"], self._pushed_refs()
        )
        calls = self._calls()
        version_tag_destinations = [
            line
            for line in calls.splitlines()
            if line.startswith("ARGS: tag ")
            and line.rstrip().endswith(f"{GHCR_REPOSITORY}:{SYNTHETIC_VERSION}")
        ]
        self.assertEqual(
            [], version_tag_destinations, "immutable version tag must not be a retag destination"
        )
        self.assertNotIn(
            f"push {GHCR_REPOSITORY}:{SYNTHETIC_VERSION}", calls, "immutable version tag must not be re-pushed"
        )
        summary = (self.root / "step-summary.txt").read_text(encoding="utf-8")
        self.assertIn("pushed ghcr image identity", summary)
        self._assert_config_lifecycle_clean()

    def test_divergent_moving_tag_is_repointed_on_retry(self) -> None:
        result = self._run_guard(
            FakeDocker(manifest_mode="same", minor_mode="same", latest_mode="diff")
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([f"{GHCR_REPOSITORY}:latest"], self._pushed_refs())
        self._assert_config_lifecycle_clean()

    def test_docker_config_must_be_exported_for_login(self) -> None:
        # Negative control for the export contract: strip the export line
        # and the fake login (a child process, like the real docker) can no
        # longer see DOCKER_CONFIG — the guard must fail closed instead of
        # silently writing credentials to docker's default location.
        script = _extract_step_script(GHCR_PUBLISH_STEP_NAME)
        self.assertIn("export DOCKER_CONFIG", script)
        mutated = script.replace("export DOCKER_CONFIG\n", "", 1)
        self.assertNotIn("export DOCKER_CONFIG", mutated)
        script_path = self.root / "guard.sh"
        _ = script_path.write_text(mutated, encoding="utf-8")
        _ = self._write_fake_docker(FakeDocker())
        self._write_artifact()
        env = {
            "PATH": f"{self.root / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": f"refs/tags/{SYNTHETIC_TAG}",
            "GITHUB_REF_NAME": SYNTHETIC_TAG,
            "GITHUB_SHA": SYNTHETIC_SHA,
            "GITHUB_ACTOR": "sr-synthetic-actor",
            "GHCR_TOKEN": SYNTHETIC_TOKEN,
            "GITHUB_STEP_SUMMARY": str(self.root / "step-summary.txt"),
            "FAKE_DOCKER_LOG": str(self.log_dir),
            "FAKE_MANIFEST_VERSION_MODE": "absent",
            "FAKE_MANIFEST_MINOR_MODE": "absent",
            "FAKE_MANIFEST_LATEST_MODE": "absent",
        }
        result = subprocess.run(
            ["bash", str(script_path)],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertIn("DOCKER_CONFIG is not exported", result.stderr)
        self.assertIn("GHCR login failed", result.stderr)
        self.assertIsNone(self._docker_config_record())


class GhcrImageSmokeTests(unittest.TestCase):
    """Execute the extracted image smoke script offline against a fake docker."""

    root: Path
    log_dir: Path
    home: Path

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.root = Path("/nonexistent")
        self.log_dir = Path("/nonexistent")
        self.home = Path("/nonexistent")

    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sr-ghcr-smoke-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log_dir = self.root / "fake-docker-log"
        _ = self.log_dir.mkdir()
        self.home = self.root / "home"
        _ = self.home.mkdir()

    def _run_smoke(self, *, health: str = "healthy", healthz_code: str = "200") -> subprocess.CompletedProcess[str]:
        _ = self._write_fake_docker()
        script_path = self.root / "smoke.sh"
        _ = script_path.write_text(_extract_step_script(SMOKE_STEP_NAME), encoding="utf-8")
        env = {
            "PATH": f"{self.root / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "IMAGE_REF": f"{GHCR_REPOSITORY}:{SYNTHETIC_VERSION}",
            "FAKE_DOCKER_LOG": str(self.log_dir),
            "FAKE_HEALTH": health,
            "FAKE_HEALTHZ_CODE": healthz_code,
        }
        return subprocess.run(
            ["bash", str(script_path)],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def _write_fake_docker(self) -> None:
        bindir = self.root / "bin"
        _ = bindir.mkdir(exist_ok=True)
        docker = bindir / "docker"
        _ = docker.write_text(SmokeFakeDocker().script(), encoding="utf-8")
        _ = docker.chmod(docker.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _calls(self) -> str:
        calls = self.log_dir / "calls.log"
        if not calls.exists():
            return ""
        return calls.read_text(encoding="utf-8")

    def _run_line(self) -> str:
        for line in self._calls().splitlines():
            if " run -d " in line:
                return line
        self.fail("docker run invocation not recorded")

    def _cleanup_count(self) -> int:
        cleanup = self.log_dir / "cleanup.log"
        if not cleanup.exists():
            return 0
        return len([line for line in cleanup.read_text(encoding="utf-8").splitlines() if line])

    def test_smoke_runs_isolated_with_nonroot_writable_ephemeral_data(self) -> None:
        result = self._run_smoke()
        self.assertEqual(0, result.returncode, result.stderr)
        # The fake logs shell-%q-quoted argv; the recorded args carry no
        # literal backslashes, so unescaping is a plain strip for matching.
        run_line = self._run_line().replace("\\", "")
        # The data directory is an ephemeral IN-MEMORY tmpfs owned by the
        # container's non-root identity — a host bind from mktemp would be
        # runner-owned 0700 and unwritable for uid 10001.
        self.assertIn("--tmpfs /data:rw,noexec,nosuid,nodev,uid=10001,gid=10001,mode=0700", run_line)
        self.assertIn("--read-only", run_line)
        self.assertIn("--cap-drop", run_line)
        self.assertIn("ALL", run_line)
        self.assertIn("no-new-privileges", run_line)
        self.assertNotRegex(run_line, r"(^|\s)-p(\s|=)")
        self.assertNotIn("--publish", run_line)
        self.assertNotIn("-v ", run_line)
        self.assertIn("--tmpfs /tmp:", run_line)
        # The readiness probe runs inside the container and the container is
        # always cleaned up, exactly once.
        self.assertIn(" exec ", self._calls())
        self.assertEqual(1, self._cleanup_count())

    def test_unhealthy_image_fails_and_cleans_up(self) -> None:
        result = self._run_smoke(health="unhealthy")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("FAIL:", result.stderr)
        self.assertIn(" logs ", self._calls())
        self.assertEqual(1, self._cleanup_count())

    def test_failed_healthz_fails_and_cleans_up(self) -> None:
        result = self._run_smoke(healthz_code="500")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("FAIL:", result.stderr)
        self.assertIn("healthz answered 500", result.stderr)
        self.assertEqual(1, self._cleanup_count())


if __name__ == "__main__":
    _ = unittest.main()
