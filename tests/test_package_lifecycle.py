"""Focused installed-lifecycle unit tests for ``tools/package_check.py``
(issue #186).

Covers the orchestration the tool is responsible for — fresh install,
synthetic operator-state seeding, same-version local-wheel replacement,
retention proof, uninstall with retained-state proof — and the deliberate
subprocess environment allowlist. ``uv`` itself is never executed here:
commands are simulated on a synthetic directory layout, while the embedded
seed/retention python snippets run for real against the checkout package
inside the synthetic HOME (no network, no provider, no model inference).
The real installed E2E proof remains ``make package-check``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr
from pathlib import Path
from typing import Protocol, cast
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
CompletedProcess = subprocess.CompletedProcess[str]
_TOOL_SPEC = importlib.util.spec_from_file_location(
    "package_check_under_test", REPO / "tools" / "package_check.py"
)
assert _TOOL_SPEC is not None and _TOOL_SPEC.loader is not None
_module = importlib.util.module_from_spec(_TOOL_SPEC)
# The documented importlib pattern: dataclass processing resolves module
# annotations through sys.modules, so register before executing.
sys.modules[_TOOL_SPEC.name] = _module
_TOOL_SPEC.loader.exec_module(_module)


class _PackageCheckModule(Protocol):
    """The attributes these tests touch on the loaded tool module."""

    _run: Callable[..., CompletedProcess]
    check_cli_scripts: Callable[..., None]
    check_installed_resources: Callable[..., None]
    check_mcp: Callable[..., None]
    check_rest: Callable[..., None]
    install_env: Callable[[Path, Path, Path], dict[str, str]]
    clean_env: Callable[[Path], dict[str, str]]
    build_artifacts: Callable[[], None]
    run_install_lifecycle: Callable[[Path], None]
    WHEEL_NAME: str
    SDIST_NAME: str


package_check = cast(_PackageCheckModule, cast("object", _module))

SCRIPTS = (
    "scarcity-router",
    "scarcity-router-mcp",
    "scarcity-router-server",
    "scarcity-router-worker",
)
POLICY_MARKER = b"package-check-synthetic-blackout"
SESSION_TTL = 7777


def _fake_tool_layout(root: Path, *, install: bool) -> None:
    """Simulate what uv does to the temporary tool layout."""
    bin_dir = root / "bin"
    tool_dir = root / "tools" / "scarcity-router"
    if install:
        bin_dir.mkdir(exist_ok=True)
        for name in SCRIPTS:
            script = bin_dir / name
            _ = script.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o755)
        python = tool_dir / "bin" / "python"
        python.parent.mkdir(parents=True, exist_ok=True)
        _ = python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o755)
    else:
        for name in SCRIPTS:
            _ = (bin_dir / name).unlink(missing_ok=True)
        shutil.rmtree(tool_dir, ignore_errors=True)


def _seeded_policy_path(root: Path) -> Path:
    return root / "home" / ".config" / "scarcity-router" / "selector-policy.json"


def _seeded_store_path(root: Path) -> Path:
    return root / "home" / ".local" / "share" / "scarcity-router" / "server" / "server-state.sqlite3"


def _stored_record(store: Path) -> object:
    connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        row = cast(
            "tuple[object, ...] | None",
            connection.execute(
                "SELECT document FROM server_configuration WHERE id = 1"
            ).fetchone(),
        )
    finally:
        connection.close()
    assert row is not None
    return cast(object, json.loads(cast(str, row[0])))


class _FakeUv:
    """Records tool/python commands and simulates uv on the fake layout."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.commands: list[list[str]] = []
        self.force_install: Callable[[], CompletedProcess] | None = None
        self.uninstall: Callable[[], CompletedProcess] | None = None
        self.seed_home: Path | None = None

    def __call__(
        self,
        command: Sequence[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 300,
    ) -> CompletedProcess:
        argv = [str(item) for item in command]
        self.commands.append(argv)
        if argv[:3] == ["uv", "tool", "install"]:
            if "--force" in argv and self.force_install is not None:
                return self.force_install()
            _fake_tool_layout(self.root, install=True)
            return CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["uv", "tool", "uninstall"]:
            if self.uninstall is not None:
                return self.uninstall()
            _fake_tool_layout(self.root, install=False)
            return CompletedProcess(argv, 0, "", "")
        if len(argv) == 4 and argv[1:3] == ["-I", "-c"]:
            # Run the real embedded seed/retention code with the checkout
            # interpreter: the package is importable and the code only
            # touches synthetic state under the given synthetic HOME.
            child_env = None if env is None else dict(env)
            if self.seed_home is not None and "default_selector_policy_path" in argv[3]:
                assert child_env is not None
                child_env["HOME"] = str(self.seed_home)
            return subprocess.run(
                [sys.executable, "-I", "-c", argv[3]],
                env=child_env,
                cwd=None if cwd is None else str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        return CompletedProcess(argv, 0, "", "")


class _LifecycleHarness:
    """Run the lifecycle with simulated uv and recorded surface smokes."""

    def __init__(self) -> None:
        self.smoke_calls: int = 0
        self.stderr: io.StringIO = io.StringIO()

    def run(self, root: Path, fake: _FakeUv) -> None:
        smokes = self

        def counted(*_args: object, **_kwargs: object) -> None:
            smokes.smoke_calls += 1

        with (
            mock.patch.object(package_check, "_run", fake),
            mock.patch.object(package_check, "check_cli_scripts", counted),
            mock.patch.object(package_check, "check_installed_resources", counted),
            mock.patch.object(package_check, "check_mcp", counted),
            mock.patch.object(package_check, "check_rest", counted),
            redirect_stderr(self.stderr),
        ):
            package_check.run_install_lifecycle(root)


class LifecycleOrderTests(unittest.TestCase):
    """install → seed → same-version replacement → retention → uninstall."""

    def test_lifecycle_runs_in_order_and_retains_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-check-test-") as directory:
            root = Path(directory)
            fake = _FakeUv(root)
            harness = _LifecycleHarness()
            harness.run(root, fake)

            uv_commands = [command for command in fake.commands if command[:1] == ["uv"]]
            self.assertEqual(3, len(uv_commands))
            self.assertEqual(["uv", "tool", "install"], uv_commands[0][:3])
            self.assertEqual(["uv", "tool", "install", "--force"], uv_commands[1][:4])
            self.assertEqual(
                ["uv", "tool", "uninstall", "scarcity-router"], uv_commands[2]
            )
            python_commands = [
                command
                for command in fake.commands
                if len(command) == 4 and command[1:3] == ["-I", "-c"]
            ]
            self.assertEqual(2, len(python_commands))
            self.assertIn("default_selector_policy_path", python_commands[0][3])
            self.assertIn("load_configuration_document", python_commands[1][3])
            self.assertLess(
                fake.commands.index(python_commands[0]), fake.commands.index(uv_commands[1])
            )
            self.assertGreater(
                fake.commands.index(python_commands[1]), fake.commands.index(uv_commands[1])
            )
            # The four installed-surface smokes run once per installation round.
            self.assertEqual(8, harness.smoke_calls)

            # Retained synthetic state after uninstall, outside the tool env.
            policy = _seeded_policy_path(root)
            self.assertTrue(policy.is_file())
            self.assertIn(POLICY_MARKER, policy.read_bytes())
            store = _seeded_store_path(root)
            self.assertEqual(
                SESSION_TTL,
                cast("dict[str, object]", _stored_record(store))["session_ttl_seconds"],
            )
            self.assertEqual([], [entry.name for entry in (root / "bin").iterdir()])
            self.assertFalse((root / "tools" / "scarcity-router").exists())


class LifecycleFailClosedTests(unittest.TestCase):
    """Replacement, entry-point, uninstall and retention failures stop work."""

    def test_seed_state_in_home_prefix_sibling_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-check-test-") as directory:
            root = Path(directory)
            fake = _FakeUv(root)
            fake.seed_home = root / "home-lookalike"
            harness = _LifecycleHarness()
            with self.assertRaises(SystemExit):
                harness.run(root, fake)
            self.assertIn("escaped the temporary HOME", harness.stderr.getvalue())
            self.assertFalse(any("--force" in command for command in fake.commands))

    def test_failed_replacement_stops_before_uninstall(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-check-test-") as directory:
            root = Path(directory)
            fake = _FakeUv(root)
            fake.force_install = lambda: CompletedProcess(
                ["uv", "tool", "install", "--force", "wheel"], 1, "", "boom"
            )
            with self.assertRaises(SystemExit) as context:
                _LifecycleHarness().run(root, fake)
            self.assertEqual(1, context.exception.code)
            self.assertFalse(
                any(command[:3] == ["uv", "tool", "uninstall"] for command in fake.commands)
            )

    def test_missing_entry_point_after_replacement_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-check-test-") as directory:
            root = Path(directory)

            def force() -> CompletedProcess:
                _fake_tool_layout(root, install=True)
                _ = (root / "bin" / "scarcity-router-mcp").unlink()
                return CompletedProcess(["uv", "tool", "install", "--force", "wheel"], 0, "", "")

            fake = _FakeUv(root)
            fake.force_install = force
            harness = _LifecycleHarness()
            with self.assertRaises(SystemExit):
                harness.run(root, fake)
            self.assertIn("installed scripts", harness.stderr.getvalue())

    def test_replacement_overwriting_user_policy_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-check-test-") as directory:
            root = Path(directory)

            def force() -> CompletedProcess:
                _fake_tool_layout(root, install=True)
                _ = _seeded_policy_path(root).write_text("{}\n", encoding="utf-8")
                return CompletedProcess(["uv", "tool", "install", "--force", "wheel"], 0, "", "")

            fake = _FakeUv(root)
            fake.force_install = force
            harness = _LifecycleHarness()
            with self.assertRaises(SystemExit):
                harness.run(root, fake)
            self.assertIn(
                "modified the seeded user selector policy", harness.stderr.getvalue()
            )

    def test_uninstall_failure_fails_with_state_intact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-check-test-") as directory:
            root = Path(directory)
            fake = _FakeUv(root)
            fake.uninstall = lambda: CompletedProcess(
                ["uv", "tool", "uninstall", "scarcity-router"], 1, "", "refused"
            )
            harness = _LifecycleHarness()
            with self.assertRaises(SystemExit):
                harness.run(root, fake)
            self.assertIn("uv tool uninstall failed", harness.stderr.getvalue())
            self.assertTrue(_seeded_policy_path(root).is_file())
            self.assertTrue(_seeded_store_path(root).is_file())

    def test_uninstall_purging_user_state_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-check-test-") as directory:
            root = Path(directory)

            def uninstall() -> CompletedProcess:
                _fake_tool_layout(root, install=False)
                _ = _seeded_policy_path(root).unlink()
                return CompletedProcess(["uv", "tool", "uninstall", "scarcity-router"], 0, "", "")

            fake = _FakeUv(root)
            fake.uninstall = uninstall
            harness = _LifecycleHarness()
            with self.assertRaises(SystemExit):
                harness.run(root, fake)
            self.assertIn("purged or modified", harness.stderr.getvalue())


class SubprocessEnvironmentTests(unittest.TestCase):
    """Build/install subprocesses never inherit the ambient environment."""

    AMBIENT_MARKERS: dict[str, str] = {
        "PATH": "/usr/bin:/bin",
        "UV_CACHE_DIR": "/synthetic/uv-cache",
        "UV_TOOL_DIR": "/ambient/tool-dir",
        "PYTHONPATH": "/ambient/leak",
        "VIRTUAL_ENV": "/ambient/venv",
        "PYTHONHOME": "/ambient/python-home",
        "PIP_INDEX_URL": "https://ambient.example/simple",
        "HTTP_PROXY": "http://ambient.example:3128",
        "SCARCITY_SYNTHETIC_TOKEN": "synthetic-secret-marker",
    }
    FORBIDDEN_VALUES: tuple[str, ...] = (
        "/ambient/leak",
        "/ambient/venv",
        "/ambient/python-home",
        "https://ambient.example",
        "synthetic-secret-marker",
    )

    def test_install_env_is_a_deliberate_allowlist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-check-test-") as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, self.AMBIENT_MARKERS):
                environment = package_check.install_env(
                    root / "home", root / "tools", root / "bin"
                )
        self.assertEqual(
            {"PATH", "HOME", "UV_CACHE_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR"},
            set(environment),
        )
        self.assertEqual(str(root / "home"), environment["HOME"])
        self.assertEqual(str(root / "tools"), environment["UV_TOOL_DIR"])
        self.assertEqual(str(root / "bin"), environment["UV_TOOL_BIN_DIR"])
        self.assertEqual("/synthetic/uv-cache", environment["UV_CACHE_DIR"])
        rendered = json.dumps(environment)
        for forbidden in self.FORBIDDEN_VALUES:
            self.assertNotIn(forbidden, rendered)

    def test_build_artifacts_uses_the_allowlist(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(
            command: Sequence[str],
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
            timeout: float = 300,
        ) -> CompletedProcess:
            _ = cwd, timeout
            captured["command"] = [str(item) for item in command]
            captured["env"] = dict(env or {})
            return CompletedProcess([str(item) for item in command], 0, "ok", "")

        with tempfile.TemporaryDirectory(prefix="package-check-test-") as directory:
            dist = Path(directory) / "dist"
            _ = dist.mkdir()
            for name in (package_check.WHEEL_NAME, package_check.SDIST_NAME):
                _ = (dist / name).write_bytes(b"")
            with (
                mock.patch.dict(os.environ, self.AMBIENT_MARKERS),
                mock.patch.object(package_check, "_run", fake_run),
                mock.patch.object(package_check, "DIST", dist),
            ):
                package_check.build_artifacts()
        self.assertEqual(["uv", "build", "--no-sources"], captured["command"])
        environment = cast("dict[str, str]", captured["env"])
        self.assertEqual({"PATH", "HOME", "UV_CACHE_DIR"}, set(environment))
        self.assertNotEqual("/ambient/tool-dir", environment.get("UV_TOOL_DIR"))
        rendered = json.dumps(environment)
        for forbidden in self.FORBIDDEN_VALUES:
            self.assertNotIn(forbidden, rendered)

    def test_clean_env_keeps_excluding_interpreter_variables(self) -> None:
        with mock.patch.dict(os.environ, self.AMBIENT_MARKERS):
            environment = package_check.clean_env(Path("/synthetic/home"))
        self.assertEqual({"PATH", "HOME"}, set(environment))
        self.assertEqual("/synthetic/home", environment["HOME"])


if __name__ == "__main__":
    _ = unittest.main()
