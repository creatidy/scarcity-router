"""Worker systemd service lifecycle tests (issue #138).

Deterministic and CI-safe: no live systemd daemon is ever contacted.
The ``systemctl``/``loginctl`` seams are recording fakes, the unit is
written to a test-provided path, and the worker executable is a fake
script in a temporary directory. The suite pins the #138 contract:

- unit generation is deterministic (idempotent install), carries the
  ACTUAL executable, the ACTUAL state directory and the preserved
  adapter selection, and embeds NO credential material (negative scan);
- install/update/uninstall discipline: marker-gated replacement, an
  unrelated unit is never overwritten or removed, repeats are safe,
  failures are visible and honestly exited;
- linger is handled deliberately and visibly, never fatally.

The single-instance lock has its own suite next to the store tests
(``tests/test_worker_local_store.py``) and the CLI wiring (dispatch
without a store, run-path lock, SIGTERM wiring) lives in
``tests/test_worker_client.py``.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override

from scarcity_router import worker_service
from scarcity_router.worker_client import (
    build_registry,
    open_worker_store,
)
from scarcity_router.worker_client import WorkerConfigError
from scarcity_router.worker_local_store import WorkerLocalIdentity

SYNTHETIC_CREDENTIAL = "SYNTHETIC-PAIRING-CREDENTIAL-138-NEVER-LEAK"
SYNTHETIC_PAIRING_CODE = "SYNTHETIC-ONE-TIME-CODE-138"
SYNTHETIC_ORIGIN = "srws://gateway.local:8790"


def selection(
    *,
    codex_sources: tuple[str, ...] = ("precision-codex-live",),
    codex_bin: str | None = None,
    zcode_sources: tuple[str, ...] = (),
    zcode_bin: str | None = None,
    zcode_workspace: str | None = None,
    allow_ollama: bool = False,
    resource: str | None = None,
    ollama_host: str | None = None,
    ollama_port: int | None = None,
    allow_codex: bool = False,
    codex_model: str | None = None,
    codex_resource: str | None = None,
) -> worker_service.ServiceSelection:
    """A typed ``ServiceSelection`` builder (defaults = one codex source)."""
    return worker_service.ServiceSelection(
        codex_sources=codex_sources,
        codex_bin=codex_bin,
        zcode_sources=zcode_sources,
        zcode_bin=zcode_bin,
        zcode_workspace=zcode_workspace,
        allow_ollama=allow_ollama,
        resource=resource,
        ollama_host=ollama_host,
        ollama_port=ollama_port,
        allow_codex=allow_codex,
        codex_model=codex_model,
        codex_resource=codex_resource,
    )


class ServiceWorld:
    """One test world: paired store, fake executable, recording tools."""

    tmp: Path
    state_dir: Path
    executable: Path
    unit_path: Path
    linger_value: str

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.state_dir = tmp / "state"
        store = open_worker_store(str(self.state_dir))
        try:
            store.save_identity(
                WorkerLocalIdentity(
                    worker_id="worker-138",
                    credential=SYNTHETIC_CREDENTIAL,
                    server_origin=SYNTHETIC_ORIGIN,
                    device_label=None,
                )
            )
        finally:
            store.close()
        bin_dir = tmp / "bin"
        _ = bin_dir.mkdir()
        self.executable = bin_dir / "scarcity-router-worker"
        _ = self.executable.write_text("#!/bin/sh\nexit 0\n")
        _ = self.executable.chmod(0o755)
        self.unit_path = (
            tmp / "config" / "systemd" / "user" / worker_service.SERVICE_UNIT_NAME
        )
        self.systemctl_calls: list[list[str]] = []
        self.loginctl_calls: list[list[str]] = []
        self.linger_value = "no"
        self.systemctl_failures: dict[str, int] = {}
        self.loginctl_failures: dict[str, int] = {}

    def tools(self) -> worker_service.ServiceTools:
        world = self

        def systemctl(arguments: list[str]) -> worker_service.ServiceToolResult:
            _ = world.systemctl_calls.append(list(arguments))
            code = world.systemctl_failures.get(arguments[0], 0)
            stderr = "simulated systemctl failure\n" if code else ""
            return worker_service.ServiceToolResult(
                command=("systemctl", *arguments),
                returncode=code,
                stdout=f"out:{arguments[0]}\n",
                stderr=stderr,
            )

        def loginctl(arguments: list[str]) -> worker_service.ServiceToolResult:
            _ = world.loginctl_calls.append(list(arguments))
            code = world.loginctl_failures.get(arguments[0], 0)
            if arguments[0] == "show-user":
                return worker_service.ServiceToolResult(
                    command=("loginctl", *arguments),
                    returncode=code,
                    stdout=world.linger_value + "\n",
                    stderr="simulated loginctl failure\n" if code else "",
                )
            return worker_service.ServiceToolResult(
                command=("loginctl", *arguments),
                returncode=code,
                stdout="",
                stderr="simulated loginctl failure\n" if code else "",
            )

        return worker_service.ServiceTools(
            systemctl=systemctl, loginctl=loginctl
        )

    def install_arguments(
        self, **overrides: object
    ) -> dict[str, object]:
        arguments: dict[str, object] = {
            "service_command": "install",
            "state_dir": str(self.state_dir),
            "allow_ollama": False,
            "resource": None,
            "ollama_host": "127.0.0.1",
            "ollama_port": 11434,
            "allow_codex": False,
            "codex_model": None,
            "codex_resource": None,
            "codex_sources": ["precision-codex-live"],
            "codex_bin": None,
            "zcode_sources": [],
            "zcode_bin": None,
            "zcode_workspace": None,
        }
        arguments.update(overrides)
        return arguments

    def install(self, **overrides: object) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = worker_service.run_service_command(
                self.install_arguments(**overrides),
                argv0=str(self.executable),
                tools=self.tools(),
                unit_path=self.unit_path,
                open_store=open_worker_store,
                build_registry=build_registry,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def uninstall(self) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = worker_service.run_service_command(
                {"service_command": "uninstall"},
                tools=self.tools(),
                unit_path=self.unit_path,
                open_store=open_worker_store,
                build_registry=build_registry,
            )
        return code, stdout.getvalue(), stderr.getvalue()


class UnitRenderingTests(unittest.TestCase):
    def render(self, selected: worker_service.ServiceSelection) -> str:
        return worker_service.render_unit(
            executable=Path("/opt/tools/scarcity-router-worker"),
            state_dir=Path("/home/u/.local/share/scarcity-router/worker"),
            selection=selected,
        )

    def test_render_is_deterministic_and_marked(self) -> None:
        first = self.render(selection())
        second = self.render(selection())
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(worker_service.UNIT_MARKER_LINE))

    def test_exec_start_carries_actual_executable_state_dir_and_sources(self) -> None:
        unit = self.render(selection())
        self.assertIn(
            "ExecStart=/opt/tools/scarcity-router-worker run "
            + "--state-dir /home/u/.local/share/scarcity-router/worker "
            + "--codex-source precision-codex-live",
            unit,
        )

    def test_selection_flags_are_preserved(self) -> None:
        unit = self.render(
            selection(
                codex_sources=("src-a", "src-b"),
                codex_bin="/opt/codex",
                zcode_sources=("zsrc",),
                zcode_bin="/opt/zcode",
                zcode_workspace="/home/u/project",
                allow_ollama=True,
                resource="my-ollama",
                ollama_host=None,
                ollama_port=11500,
            )
        )
        self.assertIn("--codex-source src-a --codex-source src-b", unit)
        self.assertIn("--codex-bin /opt/codex", unit)
        self.assertIn("--zcode-source zsrc", unit)
        self.assertIn("--zcode-bin /opt/zcode", unit)
        self.assertIn("--zcode-workspace /home/u/project", unit)
        self.assertIn(
            "--allow-ollama --resource my-ollama --ollama-port 11500", unit
        )
        self.assertNotIn("--ollama-host", unit)
        self.assertNotIn("--allow-codex", unit)

    def test_read_write_paths_covers_state_dir_and_zcode_workspace(self) -> None:
        unit = self.render(selection(zcode_workspace="/home/u/project"))
        self.assertIn(
            "ReadWritePaths=/home/u/.local/share/scarcity-router/worker "
            + "/home/u/project",
            unit,
        )

    def test_service_hardening_and_restart_policy_present(self) -> None:
        unit = self.render(selection())
        for expected in (
            "Restart=on-failure",
            "RestartSec=5s",
            "TimeoutStopSec=30s",
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "WantedBy=default.target",
        ):
            self.assertIn(expected, unit)

    def test_paths_with_spaces_are_quoted_and_cannot_break_out(self) -> None:
        unit = worker_service.render_unit(
            executable=Path("/opt/my tools/scarcity-router-worker"),
            state_dir=Path("/home/u/my state"),
            selection=selection(zcode_workspace='/home/u/we"ird'),
        )
        self.assertIn(
            'ExecStart="/opt/my tools/scarcity-router-worker" run', unit
        )
        self.assertIn('--state-dir "/home/u/my state"', unit)
        self.assertIn('--zcode-workspace "/home/u/we\\"ird"', unit)
        self.assertIn(
            'ReadWritePaths="/home/u/my state" "/home/u/we\\"ird"', unit
        )

    def test_systemd_quote_leaves_safe_words_unquoted(self) -> None:
        self.assertEqual("plain", worker_service.systemd_quote("plain"))
        self.assertEqual(
            "/opt/tool-v2", worker_service.systemd_quote("/opt/tool-v2")
        )
        self.assertEqual('""', worker_service.systemd_quote(""))
        self.assertEqual('"a b"', worker_service.systemd_quote("a b"))
        self.assertEqual('"a\\"b"', worker_service.systemd_quote('a"b'))
        self.assertEqual('"a\\\\b"', worker_service.systemd_quote("a\\b"))

    def test_ollama_without_resource_cannot_render(self) -> None:
        with self.assertRaises(ValueError):
            _ = worker_service.ServiceSelection(
                allow_ollama=True
            ).exec_start_arguments()

    def test_from_arguments_normalizes_defaults_for_idempotency(self) -> None:
        # Defaults never reach the unit: an equivalent re-install renders
        # the identical unit regardless of explicit default flags.
        explicit = worker_service.ServiceSelection.from_arguments(
            {
                "allow_ollama": True,
                "resource": "my-ollama",
                "ollama_host": "127.0.0.1",
                "ollama_port": 11434,
                "codex_sources": ["s"],
            }
        )
        omitted = worker_service.ServiceSelection.from_arguments(
            {
                "allow_ollama": True,
                "resource": "my-ollama",
                "codex_sources": ["s"],
            }
        )
        self.assertEqual(explicit, omitted)
        self.assertEqual(
            [
                "--codex-source",
                "s",
                "--allow-ollama",
                "--resource",
                "my-ollama",
            ],
            explicit.exec_start_arguments(),
        )


class ExecutableResolutionTests(unittest.TestCase):
    def test_console_script_argv0_is_used_directly(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            exe = Path(tmp) / "scarcity-router-worker"
            _ = exe.write_text("#!/bin/sh\n")
            _ = exe.chmod(0o755)
            resolved = worker_service.resolve_worker_executable(str(exe))
            self.assertEqual(exe.resolve(), resolved)

    def test_packaged_binary_name_is_used_directly(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            exe = Path(tmp) / "scarcity-worker"
            _ = exe.write_text("#!/bin/sh\n")
            _ = exe.chmod(0o755)
            resolved = worker_service.resolve_worker_executable(str(exe))
            self.assertEqual(exe.resolve(), resolved)

    def test_python_m_invocation_falls_back_to_path_lookup(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            exe = Path(tmp) / "scarcity-router-worker"
            _ = exe.write_text("#!/bin/sh\n")
            _ = exe.chmod(0o755)
            resolved = worker_service.resolve_worker_executable(
                "/usr/bin/python3",
                path_lookup=lambda name: str(exe)
                if name == "scarcity-router-worker"
                else None,
            )
            self.assertEqual(exe.resolve(), resolved)

    def test_missing_executable_fails_with_install_guidance(self) -> None:
        with self.assertRaises(worker_service.ServiceExecutableError) as caught:
            _ = worker_service.resolve_worker_executable(
                "/usr/bin/python3", path_lookup=lambda name: None
            )
        self.assertIn("install the package", str(caught.exception))

    def test_non_executable_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            exe = Path(tmp) / "scarcity-router-worker"
            _ = exe.write_text("not executable\n")
            with self.assertRaises(worker_service.ServiceExecutableError):
                _ = worker_service.resolve_worker_executable(str(exe))


class InstallLifecycleTests(unittest.TestCase):
    _tmp: tempfile.TemporaryDirectory[str]
    world: ServiceWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        # Placeholders; setUp replaces them before each test body runs.
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.world = cast("ServiceWorld", object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.world = ServiceWorld(Path(self._tmp.name))

    def test_install_writes_unit_then_reloads_enables_and_starts(self) -> None:
        code, out, _err = self.world.install()
        self.assertEqual(0, code)
        self.assertTrue(self.world.unit_path.is_file())
        self.assertEqual(
            [
                ["daemon-reload"],
                ["enable", "--now", worker_service.SERVICE_UNIT_NAME],
            ],
            self.world.systemctl_calls,
        )
        self.assertIn("unit written", out)
        self.assertIn("enabled and started", out)
        # Linger was handled visibly: queried, then enabled for this user.
        self.assertEqual(
            self.world.loginctl_calls,
            [
                ["show-user", str(os.getuid()), "--property=Linger", "--value"],
                ["enable-linger", str(os.getuid())],
            ],
        )
        self.assertIn("linger enabled", out)
        # The unit pins the ACTUAL executable and state directory.
        unit_text = self.world.unit_path.read_text(encoding="utf-8")
        self.assertIn(f"--state-dir {self.world.state_dir}", unit_text)
        self.assertIn(str(self.world.executable), unit_text)
        self.assertEqual(0o644, os.stat(self.world.unit_path).st_mode & 0o777)

    def test_install_output_and_unit_never_leak_credential_material(self) -> None:
        code, out, err = self.world.install()
        self.assertEqual(0, code)
        for surface in (
            out,
            err,
            self.world.unit_path.read_text(encoding="utf-8"),
        ):
            self.assertNotIn(SYNTHETIC_CREDENTIAL, surface)
            self.assertNotIn(SYNTHETIC_PAIRING_CODE, surface)
            # A pairing-code flag would be a credential on a command line
            # (--codex-source legitimately shares the "--code" prefix).
            self.assertNotRegex(surface, r"--code[ =]")
            self.assertNotIn("credential=", surface)

    def test_repeated_install_is_idempotent(self) -> None:
        first, _, _ = self.world.install()
        before = self.world.unit_path.read_bytes()
        mtime = self.world.unit_path.stat().st_mtime_ns
        second, out, _err = self.world.install()
        self.assertEqual(0, first)
        self.assertEqual(0, second)
        self.assertIn("already installed (unchanged)", out)
        self.assertEqual(before, self.world.unit_path.read_bytes())
        self.assertEqual(mtime, self.world.unit_path.stat().st_mtime_ns)
        # The lifecycle steps stay correct (reload + enable are idempotent).
        self.assertEqual(
            [
                ["daemon-reload"],
                ["enable", "--now", worker_service.SERVICE_UNIT_NAME],
            ],
            self.world.systemctl_calls[-2:],
        )

    def test_reinstall_with_new_selection_replaces_the_generated_unit(self) -> None:
        _ = self.world.install()
        code, out, _err = self.world.install(
            codex_sources=["precision-codex-live", "second-source"]
        )
        self.assertEqual(0, code)
        self.assertIn("unit written", out)
        unit_text = self.world.unit_path.read_text(encoding="utf-8")
        self.assertIn("--codex-source precision-codex-live", unit_text)
        self.assertIn("--codex-source second-source", unit_text)

    def test_install_refuses_to_overwrite_an_unrelated_unit(self) -> None:
        _ = self.world.unit_path.parent.mkdir(parents=True)
        foreign = (
            "# somebody's own hand-written unit\n[Service]\nExecStart=/bin/true\n"
        )
        _ = self.world.unit_path.write_text(foreign, encoding="utf-8")
        code, _out, err = self.world.install()
        self.assertEqual(2, code)
        self.assertIn("refusing to overwrite", err)
        self.assertIn("scarcity-router-worker.service", err)
        # The unrelated unit is untouched, byte for byte.
        self.assertEqual(
            foreign, self.world.unit_path.read_text(encoding="utf-8")
        )
        self.assertNotIn("ExecStart=/bin/true run", err)
        self.assertEqual([], self.world.systemctl_calls)

    def test_unpaired_worker_refuses_install(self) -> None:
        store = open_worker_store(str(self.world.state_dir))
        try:
            store.clear_identity()
        finally:
            store.close()
        code, _out, err = self.world.install()
        self.assertEqual(2, code)
        self.assertIn("not paired", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_invalid_selection_is_refused_before_anything_is_written(self) -> None:
        # Validation runs through the real `run` registry builder, so the
        # typed configuration error propagates to the CLI's handler
        # (main renders it as exit 2; asserted end-to-end in
        # test_worker_client.py). Nothing is written or executed here.
        with self.assertRaises(WorkerConfigError) as caught:
            _ = self.world.install(allow_ollama=True, codex_sources=[])
        self.assertIn("--resource is required", str(caught.exception))
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_enable_failure_is_visible_and_exits_two(self) -> None:
        self.world.systemctl_failures["enable"] = 1
        code, _out, err = self.world.install()
        self.assertEqual(2, code)
        self.assertIn("systemctl failed", err)
        self.assertTrue(self.world.unit_path.is_file())

    def test_daemon_reload_failure_is_visible_and_exits_two(self) -> None:
        self.world.systemctl_failures["daemon-reload"] = 1
        code, _out, err = self.world.install()
        self.assertEqual(2, code)
        self.assertIn("systemctl failed", err)

    def test_linger_failure_warns_but_installs(self) -> None:
        self.world.loginctl_failures["enable-linger"] = 1
        code, out, err = self.world.install()
        self.assertEqual(0, code)
        self.assertIn("linger is NOT enabled", err)
        self.assertIn("sudo loginctl enable-linger", err)
        self.assertIn("enabled and started", out)

    def test_linger_already_enabled_is_reported_without_a_write(self) -> None:
        self.world.linger_value = "yes"
        code, out, _err = self.world.install()
        self.assertEqual(0, code)
        self.assertIn("linger is enabled", out)
        self.assertEqual(
            [["show-user", str(os.getuid()), "--property=Linger", "--value"]],
            self.world.loginctl_calls,
        )

    def test_missing_systemctl_refuses_install(self) -> None:
        def broken(_arguments: list[str]) -> worker_service.ServiceToolResult:
            raise worker_service.ServiceToolError("systemctl unavailable")

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = worker_service.run_service_command(
                self.world.install_arguments(),
                argv0=str(self.world.executable),
                tools=worker_service.ServiceTools(
                    systemctl=broken,
                    loginctl=broken,
                ),
                unit_path=self.world.unit_path,
                open_store=open_worker_store,
                build_registry=build_registry,
            )
        self.assertEqual(2, code)
        self.assertIn("systemctl unavailable", stderr.getvalue())


class StatusRestartUninstallTests(unittest.TestCase):
    _tmp: tempfile.TemporaryDirectory[str]
    world: ServiceWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        # Placeholders; setUp replaces them before each test body runs.
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.world = cast("ServiceWorld", object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.world = ServiceWorld(Path(self._tmp.name))

    def _run(self, subcommand: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = worker_service.run_service_command(
                {"service_command": subcommand},
                tools=self.world.tools(),
                open_store=open_worker_store,
                build_registry=build_registry,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_prints_linger_state_and_passes_systemctl_through(self) -> None:
        _ = self.world.install()
        self.world.systemctl_calls.clear()
        self.world.linger_value = "yes"
        code, out, _err = self._run("status")
        self.assertEqual(0, code)
        self.assertIn("linger: enabled", out)
        self.assertIn("out:status", out)
        self.assertEqual(
            [["status", "--no-pager", worker_service.SERVICE_UNIT_NAME]],
            self.world.systemctl_calls,
        )

    def test_status_passes_systemctl_exit_code_through(self) -> None:
        self.world.systemctl_failures["status"] = 3
        code, _out, _err = self._run("status")
        self.assertEqual(3, code)

    def test_restart_passes_through_and_reports(self) -> None:
        _ = self.world.install()
        self.world.systemctl_calls.clear()
        code, out, _err = self._run("restart")
        self.assertEqual(0, code)
        self.assertIn("restarted", out)
        self.assertEqual(
            [["restart", worker_service.SERVICE_UNIT_NAME]],
            self.world.systemctl_calls,
        )
        self.world.systemctl_failures["restart"] = 1
        code, _out, err = self._run("restart")
        self.assertEqual(1, code)
        self.assertIn("systemctl failed", err)

    def test_uninstall_stops_disables_removes_and_keeps_state(self) -> None:
        _ = self.world.install()
        state_marker = self.world.state_dir / "worker-state.db"
        self.assertTrue(state_marker.is_file())
        self.world.systemctl_calls.clear()
        code, out, _err = self.world.uninstall()
        self.assertEqual(0, code)
        self.assertIn("uninstalled", out)
        self.assertIn("identity", out)
        self.assertIn("linger was left unchanged", out)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual(
            [
                ["disable", "--now", worker_service.SERVICE_UNIT_NAME],
                ["daemon-reload"],
            ],
            self.world.systemctl_calls,
        )
        # Unpairing never happens as a side effect of uninstalling.
        self.assertTrue(state_marker.is_file())

    def test_repeated_uninstall_is_idempotent(self) -> None:
        _ = self.world.install()
        first, _out, _err = self.world.uninstall()
        second, out2, _err2 = self.world.uninstall()
        self.assertEqual(0, first)
        self.assertEqual(0, second)
        self.assertIn("already uninstalled", out2)

    def test_uninstall_refuses_a_unit_it_did_not_generate(self) -> None:
        _ = self.world.unit_path.parent.mkdir(parents=True)
        _ = self.world.unit_path.write_text(
            "# somebody else's unit\n", encoding="utf-8"
        )
        code, _out, err = self.world.uninstall()
        self.assertEqual(2, code)
        self.assertIn("refusing to remove", err)
        self.assertTrue(self.world.unit_path.exists())

    def test_uninstall_survives_a_disable_failure_visibly(self) -> None:
        _ = self.world.install()
        self.world.systemctl_failures["disable"] = 1
        code, _out, err = self.world.uninstall()
        self.assertEqual(0, code)
        self.assertIn("disable/stop reported a problem", err)
        self.assertFalse(self.world.unit_path.exists())

    def test_unknown_service_subcommand_is_refused(self) -> None:
        code, _out, err = self._run("teleport")
        self.assertEqual(2, code)
        self.assertIn("unknown service subcommand", err)


if __name__ == "__main__":
    _ = unittest.main()
