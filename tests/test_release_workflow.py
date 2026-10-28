"""Structural release-workflow tests (release-readiness program).

`.github/workflows/release.yml` is the single public-release pipeline, and
its DEPENDENCY GRAPH and EVENT GATES are release-integrity contracts: they
decide what can be published and in which order. These tests parse the
workflow as YAML (no execution) and pin the mandatory invariants:

- the manual release-candidate preflight (workflow_dispatch) exists and can
  reach the Windows worker build AND the server image build, but can never
  reach a publishing job;
- the real tag path stays fail-closed (SemVer, package-version equality,
  stable-main ancestry, exact-commit builds);
- the server image is built from the exact commit with no write permission
  in its build job, verified offline (labels, platform, isolated /healthz
  smoke) and published only by the tag-gated `publish-ghcr` job, which is
  the only job carrying `packages: write` or running docker login/push;
- PyPI publication is strictly downstream of the finalized release bundle,
  the GitHub Release AND the GHCR image push — an irreversible publication
  can never precede any public artifact, especially the Windows ZIP;
- no pull-request event can publish; no long-lived publishing token exists;
  every `uses:` stays an allowlisted action pinned to a full commit SHA.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import cast, override

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO / ".github" / "workflows" / "release.yml"

PUBLISH_JOBS = ("publish-github-release", "publish-ghcr", "publish-pypi")
IMAGE_BUILD_JOB = "build-server-image"
GHCR_REPOSITORY = "ghcr.io/creatidy/scarcity-router"
GHCR_PUBLISH_STEP_NAME = "Verify the tested image and publish to GHCR (fail closed)"

# The complete allowlist of third-party actions this workflow may use, each
# pinned to a full commit SHA. Any new action needs a deliberate edit here.
ALLOWED_ACTIONS = (
    "actions/attest-build-provenance",
    "actions/checkout",
    "actions/download-artifact",
    "actions/setup-python",
    "actions/upload-artifact",
    "astral-sh/setup-uv",
    "pypa/gh-action-pypi-publish",
)


def _load_workflow() -> dict[str, object]:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    # Strip YAML 1.1 boolean implicit resolution so the bare `on:` key stays
    # the string "on" (the standard workaround for PyYAML's True-key quirk).
    loader = yaml.SafeLoader
    resolvers = cast(
        "dict[str, list[tuple[str, str]]]", loader.yaml_implicit_resolvers
    )
    loader.yaml_implicit_resolvers = {
        prefix: [
            resolver
            for resolver in resolver_list
            if resolver[0] != "tag:yaml.org,2002:bool"
        ]
        for prefix, resolver_list in resolvers.items()
    }
    return cast("dict[str, object]", yaml.load(text, Loader=loader))


def _triggers(document: dict[str, object]) -> dict[str, object]:
    triggers = document.get("on")
    assert isinstance(triggers, dict)
    return cast("dict[str, object]", triggers)


def _jobs(document: dict[str, object]) -> dict[str, dict[str, object]]:
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    result: dict[str, dict[str, object]] = {}
    for name, job in cast("dict[object, object]", jobs).items():
        assert isinstance(name, str)
        assert isinstance(job, dict)
        result[name] = cast("dict[str, object]", job)
    return result


def _needs_list(job: dict[str, object]) -> list[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    assert isinstance(needs, list)
    return [cast("str", entry) for entry in cast("list[object]", needs)]


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    result: list[dict[str, object]] = []
    for step in cast("list[object]", steps):
        assert isinstance(step, dict)
        result.append(cast("dict[str, object]", step))
    return result


def _step_texts(job: dict[str, object]) -> list[str]:
    """Run scripts plus step-level `if` conditions (both are contracts)."""
    texts: list[str] = []
    for step_document in _steps(job):
        condition = step_document.get("if")
        if isinstance(condition, str):
            texts.append(condition)
        run = step_document.get("run")
        if isinstance(run, str):
            texts.append(run)
    return texts


def _step_uses(job: dict[str, object]) -> list[str]:
    return [
        action
        for step_document in _steps(job)
        if isinstance((action := step_document.get("uses")), str)
    ]


def _needs_closure(jobs: dict[str, dict[str, object]], job: str) -> set[str]:
    """Every job transitively required by `job` (including itself)."""
    seen: set[str] = set()
    stack = [job]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(_needs_list(jobs[current]))
    return seen


class TriggerTests(unittest.TestCase):
    def test_manual_release_candidate_dispatch_exists(self) -> None:
        triggers = _triggers(_load_workflow())
        self.assertIn("workflow_dispatch", triggers)

    def test_no_pull_request_event_can_trigger_the_workflow(self) -> None:
        triggers = _triggers(_load_workflow())
        self.assertNotIn("pull_request", triggers)
        self.assertNotIn("pull_request_target", triggers)

    def test_real_release_triggers_only_on_semver_tags(self) -> None:
        triggers = _triggers(_load_workflow())
        push = triggers.get("push")
        assert isinstance(push, dict)
        push_document = cast("dict[str, object]", push)
        tags = push_document.get("tags")
        assert isinstance(tags, list)
        self.assertEqual(["v*.*.*"], tags)


class ManualCandidateModeTests(unittest.TestCase):
    """The candidate preflight builds and verifies; it can never publish."""

    jobs: dict[str, dict[str, object]]

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.jobs = {}

    @override
    def setUp(self) -> None:
        self.jobs = _jobs(_load_workflow())

    def test_server_image_jobs_exist(self) -> None:
        jobs = _jobs(_load_workflow())
        self.assertIn(IMAGE_BUILD_JOB, jobs)
        self.assertIn("publish-ghcr", jobs)

    def test_publishing_jobs_are_gated_to_the_tag_push_event(self) -> None:
        for job in PUBLISH_JOBS:
            condition = self.jobs[job].get("if")
            self.assertEqual("github.event_name == 'push'", condition, job)

    def test_build_jobs_carry_no_event_condition(self) -> None:
        # The candidate preflight must reach the full build chain: none of
        # the build/finalize jobs may skip on workflow_dispatch.
        for job in (
            "verify",
            "build",
            "build-windows-worker",
            IMAGE_BUILD_JOB,
            "finalize-release-bundle",
        ):
            self.assertNotIn("if", self.jobs[job], job)

    def test_candidate_records_identity_and_refuses_tag_refs(self) -> None:
        steps = "\n".join(_step_texts(self.jobs["verify"]))
        self.assertIn("github.event_name == 'workflow_dispatch'", steps)
        self.assertIn("refs/tags/*", steps)  # refuses a tag ref explicitly
        self.assertIn("CANDIDATE_SHA", steps)
        self.assertIn("CANDIDATE_VERSION", steps)

    def test_candidate_mode_reaches_the_windows_build(self) -> None:
        # Graph reachability: dispatch -> verify -> build -> windows -> finalize.
        closure = _needs_closure(self.jobs, "finalize-release-bundle")
        self.assertLessEqual(
            {"verify", "build", "build-windows-worker", "finalize-release-bundle"},
            closure,
        )

    def test_candidate_mode_reaches_the_image_build(self) -> None:
        # The candidate preflight verifies the server image too: it builds
        # and smoke-runs the image but must never push it anywhere.
        closure = _needs_closure(self.jobs, IMAGE_BUILD_JOB)
        self.assertLessEqual({"verify", "build", IMAGE_BUILD_JOB}, closure)
        image_steps = "\n".join(_step_texts(self.jobs[IMAGE_BUILD_JOB]))
        self.assertIn("docker run", image_steps)
        self.assertIn("healthz", image_steps)

    def test_candidate_mode_cannot_reach_publishing_jobs(self) -> None:
        # No publishing job is reachable from the finalize or image-build
        # chain: publication requires BOTH the needs edges AND the tag-push
        # event gate.
        for root in ("finalize-release-bundle", IMAGE_BUILD_JOB):
            closure = _needs_closure(self.jobs, root)
            for job in PUBLISH_JOBS:
                self.assertNotIn(job, closure, f"{root} -> {job}")


class TagReleaseContractTests(unittest.TestCase):
    """The real tag path keeps every fail-closed invariant."""

    verify_steps: str

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.verify_steps = ""

    @override
    def setUp(self) -> None:
        jobs = _jobs(_load_workflow())
        self.verify_steps = "\n".join(_step_texts(jobs["verify"]))

    def test_semver_invariant_is_enforced(self) -> None:
        self.assertIn("github.event_name == 'push'", self.verify_steps)
        self.assertRegex(self.verify_steps, r"v\(0\|\[1-9\]\[0-9\]\*\)")

    def test_package_version_equality_is_enforced(self) -> None:
        self.assertIn("__version__", self.verify_steps)
        self.assertIn("scarcity_router/__init__.py", self.verify_steps)

    def test_main_ancestry_is_enforced(self) -> None:
        self.assertIn("git merge-base --is-ancestor", self.verify_steps)
        self.assertIn("origin/main", self.verify_steps)

    def test_package_check_runs_before_any_publication(self) -> None:
        jobs = _jobs(_load_workflow())
        closure = _needs_closure(jobs, "publish-pypi")
        self.assertIn("build", closure)
        build_steps = "\n".join(_step_texts(jobs["build"]))
        self.assertIn("make package-check", build_steps)


class PublicationOrderingTests(unittest.TestCase):
    def test_pypi_is_downstream_of_everything_including_the_github_release(
        self,
    ) -> None:
        jobs = _jobs(_load_workflow())
        closure = _needs_closure(jobs, "publish-pypi")
        # Irreversible publication runs LAST: the finalized bundle (which
        # includes the Windows ZIP), the verified server image, the GitHub
        # Release and the GHCR push must all be ancestors of publish-pypi.
        self.assertIn("publish-github-release", closure)
        self.assertIn("publish-ghcr", closure)
        self.assertIn("finalize-release-bundle", closure)
        self.assertIn("build-windows-worker", closure)
        self.assertIn(IMAGE_BUILD_JOB, closure)
        self.assertIn("build", closure)

    def test_every_publication_is_downstream_of_all_verification_jobs(self) -> None:
        # A failure anywhere in the build/finalize/image chain skips EVERY
        # publishing job: no publication can start on a partially verified
        # release.
        jobs = _jobs(_load_workflow())
        for job in PUBLISH_JOBS:
            closure = _needs_closure(jobs, job)
            for required in (
                "build",
                "build-windows-worker",
                IMAGE_BUILD_JOB,
                "finalize-release-bundle",
            ):
                self.assertIn(required, closure, f"{job} is missing {required}")

    def test_finalize_needs_both_the_python_and_windows_builds(self) -> None:
        jobs = _jobs(_load_workflow())
        needs = sorted(_needs_list(jobs["finalize-release-bundle"]))
        self.assertEqual(["build", "build-windows-worker"], needs)

    def test_github_release_needs_the_finalized_bundle_and_the_image(self) -> None:
        jobs = _jobs(_load_workflow())
        needs = sorted(_needs_list(jobs["publish-github-release"]))
        self.assertEqual([IMAGE_BUILD_JOB, "finalize-release-bundle"], needs)

    def test_ghcr_publisher_needs_the_image_and_the_finalized_bundle(self) -> None:
        jobs = _jobs(_load_workflow())
        needs = sorted(_needs_list(jobs["publish-ghcr"]))
        self.assertEqual([IMAGE_BUILD_JOB, "finalize-release-bundle"], needs)

    def test_pypi_needs_both_other_publications(self) -> None:
        jobs = _jobs(_load_workflow())
        needs = sorted(_needs_list(jobs["publish-pypi"]))
        self.assertEqual(["publish-ghcr", "publish-github-release"], needs)


class ImagePublicationContractTests(unittest.TestCase):
    """The server image is built without write access and published by one
    tag-gated job only (docs/release-engineering.md GHCR contract)."""

    def test_image_build_checks_out_the_exact_commit_without_credentials(self) -> None:
        job = _jobs(_load_workflow())[IMAGE_BUILD_JOB]
        checkouts = [
            step
            for step in _steps(job)
            if isinstance((uses := step.get("uses")), str)
            and uses.startswith("actions/checkout@")
        ]
        self.assertEqual(1, len(checkouts))
        checkout = checkouts[0]
        with_document = cast("dict[str, object]", checkout.get("with", {}))
        # The test loader deliberately keeps YAML booleans as strings.
        self.assertIn(with_document.get("persist-credentials"), (False, "false"))
        texts = "\n".join(_step_texts(job))
        self.assertIn("GITHUB_SHA", texts)  # labels pin the commit under run

    def test_image_build_has_no_write_permission(self) -> None:
        job = _jobs(_load_workflow())[IMAGE_BUILD_JOB]
        permissions = cast("dict[str, object]", job.get("permissions"))
        self.assertEqual({"contents": "read"}, permissions)

    def test_image_build_pins_platform_and_identity_labels(self) -> None:
        texts = "\n".join(_step_texts(_jobs(_load_workflow())[IMAGE_BUILD_JOB]))
        self.assertIn("--platform linux/amd64", texts)
        self.assertIn("org.opencontainers.image.version", texts)
        self.assertIn("org.opencontainers.image.revision", texts)
        # The built image is verified offline against the expected identity.
        self.assertIn("linux/amd64", texts)
        self.assertIn("Config.Labels", texts)

    def test_image_smoke_is_isolated_and_healthz_only(self) -> None:
        texts = "\n".join(_step_texts(_jobs(_load_workflow())[IMAGE_BUILD_JOB]))
        self.assertIn("docker run", texts)
        self.assertIn("--read-only", texts)
        self.assertIn("--cap-drop ALL", texts)
        self.assertIn("no-new-privileges", texts)
        self.assertIn("/healthz", texts)
        # The data directory is an ephemeral in-memory tmpfs owned by the
        # container's non-root identity (a host bind from mktemp would be
        # runner-owned 0700 and unwritable for uid 10001).
        self.assertIn(
            "--tmpfs /data:rw,noexec,nosuid,nodev,uid=10001,gid=10001,mode=0700",
            texts,
        )
        # No port is ever published: the smoke talks to the container's own
        # loopback through docker exec/inspect only. The check is scoped to
        # the actual docker run command (its continuation lines included),
        # so ordinary shell flags like `mkdir -p` do not blur it.
        run_block: list[str] = []
        in_run = False
        for line in texts.splitlines():
            stripped = line.strip()
            if "docker run " in stripped:
                in_run = True
            if in_run:
                run_block.append(stripped)
                if stripped.startswith('"${IMAGE_REF}"'):
                    in_run = False
        run_command = "\n".join(run_block)
        self.assertTrue(run_command, "docker run command not found")
        self.assertNotRegex(run_command, r"(^|\s)-p(\s|=)")
        self.assertNotIn("--publish", run_command)
        self.assertNotIn("-v ", run_command)  # no host bind into the smoke

    def test_image_artifact_is_a_same_run_intermediate_never_a_release_asset(self) -> None:
        jobs = _jobs(_load_workflow())
        upload_paths: list[str] = []
        for step in _steps(jobs[IMAGE_BUILD_JOB]):
            uses = step.get("uses")
            if not isinstance(uses, str) or not uses.startswith("actions/upload-artifact@"):
                continue
            with_document = cast("dict[str, object]", step.get("with", {}))
            path = with_document.get("path")
            assert isinstance(path, str)
            upload_paths.append(path)
        self.assertTrue(
            any("server-image/" in path for path in upload_paths),
            "the tested image artifact upload is missing",
        )
        for job in ("publish-github-release", "publish-pypi"):
            self.assertNotIn("server-image", "\n".join(_step_texts(jobs[job])), job)
        # The GitHub Release attachment list stays the exact four assets.
        release_texts = "\n".join(_step_texts(jobs["publish-github-release"]))
        self.assertIn("dist/scarcity_router-*.whl", release_texts)
        self.assertIn("dist/scarcity_router-*.tar.gz", release_texts)
        self.assertIn("dist/scarcity-worker-*-windows-x64.zip", release_texts)
        self.assertIn("dist/SHA256SUMS", release_texts)

    def test_ghcr_publisher_is_the_only_packages_writer(self) -> None:
        jobs = _jobs(_load_workflow())
        job = jobs["publish-ghcr"]
        permissions = cast("dict[str, object]", job.get("permissions"))
        self.assertEqual({"contents": "read", "packages": "write"}, permissions)
        writers = [
            name
            for name, other in jobs.items()
            if cast("dict[str, object]", other.get("permissions", {})).get("packages")
            == "write"
        ]
        self.assertEqual(["publish-ghcr"], writers)

    def test_login_and_push_run_only_in_the_ghcr_publisher(self) -> None:
        jobs = _jobs(_load_workflow())
        for name, job in jobs.items():
            texts = "\n".join(_step_texts(job))
            if name == "publish-ghcr":
                self.assertIn("docker login", texts)
                self.assertIn("--password-stdin", texts)
                self.assertIn("docker push", texts)
            else:
                self.assertNotIn("docker login", texts, name)
                self.assertNotIn("docker push", texts, name)

    def test_ghcr_publisher_targets_the_frozen_contract(self) -> None:
        job = _jobs(_load_workflow())["publish-ghcr"]
        texts = "\n".join(_step_texts(job))
        self.assertIn(GHCR_REPOSITORY, texts)
        # Moving tags are derived from the VALIDATED version (never raw
        # event context): minor tag from the semver tail, stable `latest`
        # last, all pushed only in the publisher.
        self.assertIn("${EXPECTED_VERSION%.*}", texts)
        self.assertIn('"${IMAGE_REPOSITORY}:latest"', texts)
        self.assertIn(GHCR_PUBLISH_STEP_NAME, [
            cast("str", step.get("name", "")) for step in _steps(job)
        ])

    def test_ghcr_publisher_verifies_artifact_before_identity_use(self) -> None:
        texts = "\n".join(_step_texts(_jobs(_load_workflow())["publish-ghcr"]))
        self.assertIn("sha256sum -c", texts)
        self.assertIn("org.opencontainers.image.version", texts)
        self.assertIn("org.opencontainers.image.revision", texts)
        self.assertIn("linux/amd64", texts)
        # Fail-closed runtime re-checks behind the workflow-level gate.
        self.assertIn("GITHUB_EVENT_NAME", texts)
        self.assertIn("refs/tags/*", texts)
        # Absence is accepted only for the exact missing-manifest diagnostic
        # of the exact reference; the earlier broad substring regex (which
        # misread any 404/'not found' in transport or TLS noise as absence)
        # is gone.
        self.assertNotRegex(texts, r"no such manifest\|")
        self.assertNotRegex(texts, r"\bnot found\|")
        self.assertIn("no such manifest: ${manifest_ref}", texts)
        self.assertIn("unrecognized diagnostic", texts)
        # Registry inspection happens before any push decision.
        self.assertIn("manifest_state ", texts)
        self.assertIn("refusing", texts)

    def test_ghcr_retry_repairs_moving_tags_without_retargeting_version(self) -> None:
        texts = "\n".join(_step_texts(_jobs(_load_workflow())["publish-ghcr"]))
        # The immutable version is pushed only when it is absent; the moving
        # aliases are decided from their immutable version label: advance
        # when missing or older-or-equal stable, PRESERVE when newer stable
        # (an older-release retry never regresses X.Y/latest), and fail
        # closed on unprovable metadata — all before any push.
        self.assertLess(
            texts.index("PUSH_VERSION=0"), texts.index("docker push"), "version decision must precede pushes"
        )
        self.assertIn("[ \"${PUSH_VERSION}\" -eq 1 ]", texts)
        self.assertIn("[ \"${PUSH_MINOR}\" -eq 1 ]", texts)
        self.assertIn("[ \"${PUSH_LATEST}\" -eq 1 ]", texts)
        self.assertIn("idempotent no-op", texts)
        self.assertIn("docker pull", texts)
        self.assertIn("preserve", texts)
        self.assertIn("major.minor scope", texts)
        self.assertIn("unusable metadata", texts)
        self.assertIn("org.opencontainers.image.version", texts)
        # DOCKER_CONFIG is exported so docker really uses the trap-cleaned
        # temporary directory.
        self.assertIn("export DOCKER_CONFIG", texts)

    def test_publish_ghcr_is_serialized_across_releases(self) -> None:
        # The alias advance/preserve decision must never race another
        # release's publisher: one fixed repository-wide group without
        # canceling the running publisher (pending jobs may be replaced).
        job = _jobs(_load_workflow())["publish-ghcr"]
        concurrency = cast("dict[str, object]", job.get("concurrency"))
        self.assertEqual("ghcr-publication", concurrency.get("group"))
        self.assertIn(concurrency.get("cancel-in-progress"), (False, "false"))
        # The lock belongs to the publisher only.
        for name, other in _jobs(_load_workflow()).items():
            if name != "publish-ghcr":
                self.assertIsNone(other.get("concurrency"), name)


class PublicationIdentityTests(unittest.TestCase):
    def test_no_long_lived_publishing_token_exists(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("password:", text)
        self.assertNotIn("api_token:", text)
        self.assertNotIn("__token__", text)
        self.assertNotIn("PYPI_API_TOKEN", text)

    def test_every_action_is_allowlisted_and_sha_pinned(self) -> None:
        jobs = _jobs(_load_workflow())
        seen: set[str] = set()
        for job in jobs.values():
            for action in _step_uses(job):
                match = re.fullmatch(r"(?P<repo>[^@\s]+)@(?P<sha>[0-9a-f]{40})", action)
                self.assertIsNotNone(match, f"unpinned action: {action}")
                assert match is not None
                repo = match.group("repo")
                self.assertIn(repo, ALLOWED_ACTIONS, f"action outside the allowlist: {repo}")
                seen.add(repo)
        self.assertEqual(set(ALLOWED_ACTIONS), seen)

    def test_pypi_publishes_via_oidc_trusted_publishing(self) -> None:
        jobs = _jobs(_load_workflow())
        job = jobs["publish-pypi"]
        permissions = cast("dict[str, object]", job.get("permissions"))
        self.assertEqual("write", permissions.get("id-token"))
        self.assertEqual("pypi", job.get("environment"))
        uses: list[str] = []
        steps = job.get("steps", [])
        assert isinstance(steps, list)
        for step in cast("list[object]", steps):
            assert isinstance(step, dict)
            step_document = cast("dict[str, object]", step)
            action = step_document.get("uses")
            if isinstance(action, str):
                uses.append(action)
        self.assertTrue(
            any("pypa/gh-action-pypi-publish" in action for action in uses)
        )

    def test_github_release_publication_uses_the_run_token_only(self) -> None:
        jobs = _jobs(_load_workflow())
        job = jobs["publish-github-release"]
        permissions = cast("dict[str, object]", job.get("permissions"))
        self.assertEqual({"contents": "write"}, permissions)
        steps = job.get("steps", [])
        assert isinstance(steps, list)
        for step in cast("list[object]", steps):
            assert isinstance(step, dict)
            step_document = cast("dict[str, object]", step)
            env = step_document.get("env")
            if isinstance(env, dict):
                env_document = cast("dict[str, object]", env)
                self.assertIn("GH_TOKEN", env_document)


if __name__ == "__main__":
    _ = unittest.main()
