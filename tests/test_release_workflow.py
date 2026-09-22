"""Structural release-workflow tests (release-readiness program).

`.github/workflows/release.yml` is the single public-release pipeline, and
its DEPENDENCY GRAPH and EVENT GATES are release-integrity contracts: they
decide what can be published and in which order. These tests parse the
workflow as YAML (no execution) and pin the mandatory invariants:

- the manual release-candidate preflight (workflow_dispatch) exists and can
  reach the Windows worker build, but can never reach a publishing job;
- the real tag path stays fail-closed (SemVer, package-version equality,
  stable-main ancestry, exact-commit builds);
- PyPI publication is strictly downstream of the finalized release bundle
  AND the GitHub Release — an irreversible publication can never precede
  any public artifact, especially the Windows ZIP;
- no pull-request event can publish; no long-lived publishing token exists.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import cast, override

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO / ".github" / "workflows" / "release.yml"

PUBLISH_JOBS = ("publish-github-release", "publish-pypi")


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


def _step_texts(job: dict[str, object]) -> list[str]:
    """Run scripts plus step-level `if` conditions (both are contracts)."""
    texts: list[str] = []
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    for step in cast("list[object]", steps):
        assert isinstance(step, dict)
        step_document = cast("dict[str, object]", step)
        condition = step_document.get("if")
        if isinstance(condition, str):
            texts.append(condition)
        run = step_document.get("run")
        if isinstance(run, str):
            texts.append(run)
    return texts


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

    def test_candidate_mode_cannot_reach_publishing_jobs(self) -> None:
        # No publishing job is reachable from the finalize job: publication
        # requires BOTH the needs edge AND the tag-push event gate.
        closure = _needs_closure(self.jobs, "finalize-release-bundle")
        self.assertNotIn("publish-github-release", closure)
        self.assertNotIn("publish-pypi", closure)


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
        # includes the Windows ZIP) and the GitHub Release must both be
        # ancestors of publish-pypi.
        self.assertIn("publish-github-release", closure)
        self.assertIn("finalize-release-bundle", closure)
        self.assertIn("build-windows-worker", closure)
        self.assertIn("build", closure)

    def test_finalize_needs_both_the_python_and_windows_builds(self) -> None:
        jobs = _jobs(_load_workflow())
        needs = sorted(_needs_list(jobs["finalize-release-bundle"]))
        self.assertEqual(["build", "build-windows-worker"], needs)


class PublicationIdentityTests(unittest.TestCase):
    def test_no_long_lived_publishing_token_exists(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("password:", text)
        self.assertNotIn("api_token:", text)
        self.assertNotIn("__token__", text)
        self.assertNotIn("PYPI_API_TOKEN", text)

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
