"""Worker systemd service lifecycle tests (issue #138).

Deterministic and CI-safe: no live systemd daemon is ever contacted.
The ``systemctl``/``loginctl`` seams are recording fakes, the unit is
written to a test-provided path, and the worker executable is a fake
script in a temporary directory. The suite pins the #138 contract:

- unit generation is deterministic (idempotent install), carries the
  ACTUAL executable, the ACTUAL state directory and the preserved
  adapter selection, and embeds NO credential material (negative scan);
- systemd substitution safety (review remediation): every literal ``%``
  is doubled where specifier expansion applies (``ExecStart`` AND
  ``ReadWritePaths``, quotes never suppress it) and the ``ExecStart``
  line suppresses ``$``-variable substitution with the documented ``:``
  prefix — rendered words round-trip to the ORIGINAL literal paths,
  proven by a parsing oracle and, where available, the host's real
  ``systemd-analyze verify``;
- a ZCode-source unit excepts exactly the resolved ZCode CLI state home
  (``$HOME/.zcode``) in ``ReadWritePaths`` (service mode must not be
  stricter than a foreground run), canonicalized against symlink or
  non-canonical input, refused at install when missing, and absent from
  every non-ZCode unit;
- install/update/uninstall discipline: marker-gated replacement, an
  unrelated unit is never overwritten or removed, repeats are safe,
  failures are visible and honestly exited;
- the narrowed service-mode filesystem contract (D-065): the worker
  state directory AND the systemd user-unit directory must lie strictly
  beneath the invoking user's canonical home — ``/``, the home itself,
  outside-home locations and symlink escapes are refused before anything
  is written, and ``XDG_CONFIG_HOME`` outside home is refused for
  install — while outside-home state directories remain a foreground
  ``run`` capability with the conservative root-to-leaf checks;
- linger is handled deliberately and visibly, never fatally.

The single-instance lock has its own suite next to the store tests
(``tests/test_worker_local_store.py``) and the CLI wiring (dispatch
without a store, run-path lock, SIGTERM wiring) lives in
``tests/test_worker_client.py``.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import Mock, patch

from scarcity_router import worker_service
from scarcity_router.worker_client import (
    build_registry,
    open_worker_store,
)
from scarcity_router.worker_client import WorkerConfigError
from scarcity_router.worker_local_store import WorkerLocalIdentity, WorkerLocalStore
from scarcity_router.worker_zcode_adapter import zcode_state_home

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
        # The narrowed D-065 service contract: the worker state directory
        # and the systemd user-unit directory live UNDER the user's home
        # (the world's $HOME is a fake home inside tmp).
        self.home_dir: Path = tmp / "home"
        _ = (self.home_dir / ".zcode").mkdir(parents=True)
        self.state_dir = (
            self.home_dir / ".local" / "share" / "scarcity-router" / "worker"
        )
        self.workspace_dir: Path = tmp / "workspace"
        _ = self.workspace_dir.mkdir()
        self.environment: dict[str, str] = {"HOME": str(self.home_dir)}
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
            self.home_dir
            / ".config"
            / "systemd"
            / "user"
            / worker_service.SERVICE_UNIT_NAME
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

    def pair_state_dir(self, state_dir: Path) -> None:
        """Pair a NON-default state directory (the world pairs only its own)."""
        store = open_worker_store(str(state_dir))
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
                env=self.environment,
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


def systemd_words(value: str) -> list[str]:
    """Tokenize one rendered directive value the way systemd parses it.

    Reverses the renderer's escaping in systemd's own documented
    order: specifier resolution first on the raw text (a single
    left-to-right pass in which ``%%`` yields one literal ``%``),
    then command-line unquoting (surrounding double quotes removed,
    the C-style escapes the renderer emits — ``\\\\`` and ``\\"`` —
    decoded). A rendered word that does NOT round-trip to its
    original input means systemd would address a DIFFERENT path than
    the one the administrator configured.
    """
    words: list[str] = []
    index = 0
    total = len(value)
    while index < total:
        while index < total and value[index] == " ":
            index += 1
        if index >= total:
            break
        characters: list[str] = []
        quoted = value[index] == '"'
        if quoted:
            index += 1
        while index < total:
            character = value[index]
            if character == "%" and index + 1 < total and value[index + 1] == "%":
                characters.append("%")
                index += 2
                continue
            if (
                quoted
                and character == "\\"
                and index + 1 < total
                and value[index + 1] in ('"', "\\")
            ):
                characters.append(value[index + 1])
                index += 2
                continue
            if quoted and character == '"':
                index += 1
                break
            if not quoted and character == " ":
                index += 1
                break
            characters.append(character)
            index += 1
        words.append("".join(characters))
    return words


class UnitRenderingTests(unittest.TestCase):
    def render(
        self,
        selected: worker_service.ServiceSelection,
        zcode_state_home: Path | None = None,
    ) -> str:
        return worker_service.render_unit(
            executable=Path("/opt/tools/scarcity-router-worker"),
            state_dir=Path("/home/u/.local/share/scarcity-router/worker"),
            selection=selected,
            zcode_state_home=zcode_state_home,
        )

    def test_render_is_deterministic_and_marked(self) -> None:
        first = self.render(selection())
        second = self.render(selection())
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(worker_service.UNIT_MARKER_LINE))

    def test_exec_start_carries_actual_executable_state_dir_and_sources(self) -> None:
        unit = self.render(selection())
        # The ':' executable prefix (systemd.service(5)) suppresses
        # $-variable substitution for the whole command line: every word
        # below stays the literal path/value it was rendered from.
        self.assertIn(
            "ExecStart=:/opt/tools/scarcity-router-worker run "
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
            ),
            zcode_state_home=Path("/home/u/.zcode"),
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
            'ExecStart=:"/opt/my tools/scarcity-router-worker" run', unit
        )
        self.assertIn('--state-dir "/home/u/my state"', unit)
        self.assertIn('--zcode-workspace "/home/u/we\\"ird"', unit)
        self.assertIn(
            'ReadWritePaths="/home/u/my state" "/home/u/we\\"ird"', unit
        )

    def test_paths_with_spaces_are_quoted_under_the_colon_prefix(self) -> None:
        # The ':' prefix attaches outside the quoting: a quoted executable
        # path is still parsed as the command (verified against systemd
        # 255's own parser by SystemdAnalyzeTests below).
        unit = worker_service.render_unit(
            executable=Path("/opt/my tools/scarcity-router-worker"),
            state_dir=Path("/home/u/my state"),
            selection=selection(),
        )
        self.assertIn(
            'ExecStart=:"/opt/my tools/scarcity-router-worker" run', unit
        )

    # ── systemd substitution round-trip oracle ────────────────────────

    def _directive(self, unit: str, name: str) -> list[str]:
        line = next(
            line
            for line in unit.splitlines()
            if line.startswith(f"{name}=")
        )
        value = line.removeprefix(f"{name}=")
        if name == "ExecStart":
            value = value.removeprefix(":")
        return systemd_words(value)

    def test_literal_percent_is_doubled_and_round_trips(self) -> None:
        unit = worker_service.render_unit(
            executable=Path("/opt/tools/scarcity-router-worker"),
            state_dir=Path("/home/u/st%20ate"),
            selection=selection(
                zcode_sources=("zsrc",),
                zcode_workspace="/home/u/proj%20ect",
            ),
            zcode_state_home=Path("/home/u/%h/.zcode"),
        )
        # Every '%' is doubled wherever specifier expansion applies —
        # quoted or not (quotes never suppress systemd specifier
        # expansion); a specifier-looking segment can never survive raw.
        for directive in ("ExecStart", "ReadWritePaths"):
            line = next(
                line
                for line in unit.splitlines()
                if line.startswith(f"{directive}=")
            )
            self.assertNotRegex(
                line, r"(^|[^%])%[0-9a-zA-Z]", directive
            )
        self.assertIn("--state-dir /home/u/st%%20ate", unit)
        self.assertIn("--zcode-workspace /home/u/proj%%20ect", unit)
        self.assertIn(
            "ReadWritePaths=/home/u/st%%20ate /home/u/%%h/.zcode "
            + "/home/u/proj%%20ect",
            unit,
        )
        # Round trip: after systemd's OWN processing the unit addresses
        # the original literal paths — '%h' is NOT silently substituted
        # by a (different) home directory.
        self.assertIn("/home/u/st%20ate", self._directive(unit, "ExecStart"))
        self.assertIn(
            "/home/u/proj%20ect", self._directive(unit, "ExecStart")
        )
        self.assertEqual(
            [
                "/home/u/st%20ate",
                "/home/u/%h/.zcode",
                "/home/u/proj%20ect",
            ],
            self._directive(unit, "ReadWritePaths"),
        )

    def test_literal_dollar_stays_literal_in_exec_start(self) -> None:
        unit = worker_service.render_unit(
            executable=Path("/opt/tools/scarcity-router-worker"),
            state_dir=Path("/home/u/$st ate"),
            selection=selection(
                zcode_sources=("zsrc",), zcode_workspace="/home/u/w$orks"
            ),
            zcode_state_home=Path("/home/u/.zcode"),
        )
        exec_line = next(
            line for line in unit.splitlines() if line.startswith("ExecStart=")
        )
        # The ':' executable prefix is systemd's documented switch that
        # suppresses environment-variable substitution for the whole
        # command line — so a literal '$' needs no doubling anywhere.
        self.assertTrue(exec_line.startswith("ExecStart=:"))
        self.assertNotIn("$$", unit)
        self.assertIn('--state-dir "/home/u/$st ate"', unit)
        self.assertIn('--zcode-workspace "/home/u/w$orks"', unit)
        # ReadWritePaths does NOT do variable substitution at all: '$'
        # renders literally there, unmodified.
        self.assertEqual(
            [
                "/home/u/$st ate",
                "/home/u/.zcode",
                "/home/u/w$orks",
            ],
            self._directive(unit, "ReadWritePaths"),
        )
        # And the ExecStart words round-trip to the literal '$' paths.
        exec_words = self._directive(unit, "ExecStart")
        self.assertIn("/home/u/$st ate", exec_words)
        self.assertIn("/home/u/w$orks", exec_words)

    def test_a_zcode_unit_without_the_state_home_cannot_render(self) -> None:
        with self.assertRaises(ValueError):
            _ = self.render(selection(zcode_sources=("zsrc",)))

    def test_installation_normalizes_paths_before_rendering(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            base = Path(tmp).resolve()
            real_home = base / "real-home"
            _ = (real_home / ".zcode").mkdir(parents=True)
            linked_home = base / "linked-home"
            _ = linked_home.symlink_to(real_home, target_is_directory=True)
            real_workspace = base / "real-project"
            _ = real_workspace.mkdir()
            # A symlinked state home and a non-canonical workspace
            # spelling: the unit must grant exactly the REAL directory
            # ZCode itself resolves to — never a second, differently
            # spelled (or symlink-named) entry.
            unit = worker_service.render_unit(
                executable=Path("/opt/tools/scarcity-router-worker"),
                state_dir=base / "state",
                selection=worker_service.normalize_selection_paths(selection(
                    zcode_sources=("zsrc",),
                    zcode_workspace=str(linked_home / ".." / "real-project"),
                )),
                zcode_state_home=worker_service.resolve_zcode_state_home(
                    {"HOME": str(linked_home)}
                ),
            )
            read_write_line = next(
                line
                for line in unit.splitlines()
                if line.startswith("ReadWritePaths=")
            )
            self.assertIn(str(real_home / ".zcode"), read_write_line)
            self.assertIn(str(real_workspace), read_write_line)
            self.assertNotIn("linked-home", read_write_line)
            self.assertNotIn("..", read_write_line)

    def test_renderer_serializes_validated_paths_after_pathname_replacement(self) -> None:
        for changed_role in ("workspace", "zcode-home"):
            with self.subTest(role=changed_role), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                workspace = base / "workspace"
                workspace.mkdir()
                home = base / "home"
                (home / ".zcode").mkdir(parents=True)
                chosen = worker_service.normalize_selection_paths(selection(
                    zcode_sources=("zsrc",), zcode_workspace=str(workspace)
                ))
                zhome = worker_service.resolve_zcode_state_home({"HOME": str(home)})
                changed = workspace if changed_role == "workspace" else zhome
                original = changed.with_name(changed.name + "-original")
                replacement = base / "replacement"
                replacement.mkdir()
                _ = changed.rename(original)
                changed.symlink_to(replacement, target_is_directory=True)
                self.assertNotEqual(changed, changed.resolve())
                unit = worker_service.render_unit(
                    executable=Path("/opt/scarcity-router-worker"),
                    state_dir=base / "state", selection=chosen, zcode_state_home=zhome,
                )
                grants = systemd_words(next(
                    line.removeprefix("ReadWritePaths=") for line in unit.splitlines()
                    if line.startswith("ReadWritePaths=")
                ))
                self.assertEqual([str(base / "state"), str(zhome), str(workspace)], grants)
                self.assertNotIn(str(replacement), unit)

    def test_renderer_does_not_consult_filesystem_or_path_lookup(self) -> None:
        chosen = selection(
            codex_bin="/tools/codex", zcode_bin="/tools/zcode",
            zcode_sources=("zsrc",), zcode_workspace="/work/project",
        )
        with patch.object(Path, "resolve", side_effect=AssertionError("resolve")), \
                patch.object(Path, "absolute", side_effect=AssertionError("absolute")), \
                patch.object(os.path, "realpath", side_effect=AssertionError("realpath")), \
                patch.object(shutil, "which", side_effect=AssertionError("PATH")):
            unit = worker_service.render_unit(
                executable=Path("/tools/worker"), state_dir=Path("/data/worker"),
                selection=chosen, zcode_state_home=Path("/home/u/.zcode"),
            )
        for value in ("/tools/worker", "/data/worker", "/tools/codex", "/tools/zcode", "/work/project", "/home/u/.zcode"):
            self.assertIn(value, unit)

    def test_codex_only_unit_never_renders_a_zcode_state_home(self) -> None:
        unit = self.render(selection())
        self.assertNotIn(".zcode", unit)
        self.assertIn(
            "ReadWritePaths=/home/u/.local/share/scarcity-router/worker",
            unit,
        )

    def test_state_home_with_no_zcode_source_is_refused(self) -> None:
        # The write grant can never leak into a Codex-only unit by a
        # confused call site: the combination is a hard error.
        with self.assertRaises(ValueError):
            _ = self.render(
                selection(), zcode_state_home=Path("/home/u/.zcode")
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
        # Literal '%' is doubled BEFORE the quoting decision, so a
        # specifier-looking word can never reach systemd un-escaped.
        self.assertEqual("a%%b", worker_service.systemd_quote("a%b"))
        self.assertEqual("%%h", worker_service.systemd_quote("%h"))
        self.assertEqual('"/a %%b c"', worker_service.systemd_quote("/a %b c"))


class SystemdAnalyzeTests(unittest.TestCase):
    """The rendered unit must LOAD under the host's real systemd parser.

    Skipped where systemd-analyze is unavailable (CI); where present it
    is the authoritative oracle: an un-escaped '%20' or a raw specifier
    fails the unit to load ("Invalid specifier"), exactly the failure
    mode the review proved against the pre-remediation renderer.
    """

    @unittest.skipUnless(
        shutil.which("systemd-analyze"), "systemd-analyze unavailable"
    )
    def test_hostile_rendered_unit_loads(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            base = Path(tmp).resolve()
            exe = base / "scarcity-router-worker"
            _ = exe.write_text("#!/bin/sh\nexit 0\n")
            _ = exe.chmod(0o755)
            hostile_state = base / "st%20ate dir"
            hostile_home = base / "%h zcode"
            hostile_workspace = base / "proj$ect x"
            for directory in (hostile_state, hostile_home, hostile_workspace):
                _ = directory.mkdir()
            unit = worker_service.render_unit(
                executable=exe,
                state_dir=hostile_state,
                selection=selection(
                    zcode_sources=("zsrc",),
                    zcode_workspace=str(hostile_workspace),
                ),
                zcode_state_home=hostile_home,
            )
            unit_file = base / "hostile-probe.service"
            _ = unit_file.write_text(unit, encoding="utf-8")
            completed = subprocess.run(
                ["systemd-analyze", "verify", str(unit_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertNotIn("specifier", completed.stderr)
            self.assertNotIn("bad unit file setting", completed.stderr)

    @unittest.skipUnless(
        shutil.which("systemd-analyze"), "systemd-analyze unavailable"
    )
    def test_unescaped_percent_would_fail_to_load(self) -> None:
        # The control: systemd's own parser rejects the raw '%20' the
        # renderer is required to escape — the oracle is discriminating.
        with tempfile.TemporaryDirectory[str]() as tmp:
            unit_file = Path(tmp) / "control.service"
            _ = unit_file.write_text(
                "[Unit]\nDescription=control\n[Service]\nType=oneshot\n"
                + "ExecStart=/bin/true /home/u/st%20ate\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["systemd-analyze", "verify", str(unit_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("specifier", completed.stderr)


class ZCodeStateHomeTests(unittest.TestCase):
    """The ZCode CLI state home the service unit must allow (blocker 2).

    Service mode runs with ``ProtectHome=read-only``, and normal ZCode
    CLI execution writes session/runtime/log/rollout and database state
    under its supported state home — so a ZCode-source unit must except
    exactly that resolved directory, and nothing wider.
    """

    def test_location_follows_the_adapter_home_contract(self) -> None:
        self.assertEqual(
            Path("/home/u/.zcode"), zcode_state_home({"HOME": "/home/u"})
        )
        # HOME unset falls back to the invoking user's home.
        fallback = zcode_state_home({})
        self.assertTrue(str(fallback).endswith("/.zcode"))

    def test_resolver_returns_the_realpath_canonical_directory(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            base = Path(tmp).resolve()
            real_home = base / "real-home"
            _ = (real_home / ".zcode").mkdir(parents=True)
            linked_home = base / "linked-home"
            _ = linked_home.symlink_to(real_home, target_is_directory=True)
            resolved = worker_service.resolve_zcode_state_home(
                {"HOME": str(linked_home)}
            )
            self.assertEqual(real_home / ".zcode", resolved)

    def test_missing_state_home_is_refused_with_remediation(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            with self.assertRaises(
                worker_service.ServiceZCodeStateHomeError
            ) as caught:
                _ = worker_service.resolve_zcode_state_home({"HOME": str(tmp)})
        message = str(caught.exception)
        self.assertIn("does not exist", message)
        # The remediation names the owner's own supported sign-in path;
        # the tooling never creates ZCode's state home itself.
        self.assertIn("zcode login zai", message)

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

    def test_zcode_install_grants_exactly_the_resolved_state_home(self) -> None:
        code, _out, err = self.world.install(
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_workspace=str(self.world.workspace_dir),
        )
        self.assertEqual(0, code, err)
        unit_text = self.world.unit_path.read_text(encoding="utf-8")
        expected_home = (self.world.home_dir / ".zcode").resolve()
        # The full ReadWritePaths value: worker state, the EXACT resolved
        # ZCode CLI state home, the authorized workspace — nothing wider
        # (in particular never the whole $HOME the state home sits in).
        self.assertEqual(
            [
                f"ReadWritePaths={self.world.state_dir} {expected_home} "
                + f"{self.world.workspace_dir}",
            ],
            [
                line
                for line in unit_text.splitlines()
                if line.startswith("ReadWritePaths=")
            ],
        )
        self.assertIn("ProtectHome=read-only", unit_text)
        # No credential material on the ZCode path either.
        self.assertNotIn(SYNTHETIC_CREDENTIAL, unit_text)
        self.assertNotRegex(unit_text, r"--code[ =]")

    def test_install_refuses_when_zcode_state_home_is_missing(self) -> None:
        # The required directory must already exist (the owner's own
        # interactive ZCode usage creates it): install refuses instead of
        # writing a unit whose ReadWritePaths names an impossible path —
        # and it never creates ZCode's state home itself.
        alt_home = self.world.tmp / "home-without-zcode"
        alt_state = alt_home / ".local" / "share" / "scarcity-router" / "worker"
        self.world.pair_state_dir(alt_state)
        self.world.environment = {"HOME": str(alt_home)}
        code, _out, err = self.world.install(
            state_dir=str(alt_state),
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_workspace=str(self.world.workspace_dir),
        )
        self.assertEqual(2, code)
        self.assertIn("ZCode CLI state home", err)
        self.assertIn("zcode login zai", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)
        self.assertFalse((alt_home / ".zcode").exists())

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
                env=self.world.environment,
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


class PathNormalizationTests(unittest.TestCase):
    """Daybreak blocker 1: resolve once at install, persist canonical paths.

    A user manager resolves the service's relative paths against its own
    working-directory context (not the installer's shell), so every
    filesystem-valued argument must enter the unit as the exact canonical
    absolute path that was validated at install time. Each test here
    passes a RELATIVE input that would resolve differently under systemd
    (a relative workspace from a cwd that is not ``$HOME`` would become
    ``$HOME`` after reboot) and pins the canonical outcome.
    """

    _tmp: tempfile.TemporaryDirectory[str]
    world: ServiceWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.world = cast("ServiceWorld", object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.world = ServiceWorld(Path(self._tmp.name))

    def _chdir(self, target: Path) -> None:
        previous = os.getcwd()
        os.chdir(target)
        self.addCleanup(os.chdir, previous)

    def _pair(self, state_dir: Path) -> None:
        return self.world.pair_state_dir(state_dir)

    def _exec_start_words(self) -> list[str]:
        unit = self.world.unit_path.read_text(encoding="utf-8")
        line = next(
            l for l in unit.splitlines() if l.startswith("ExecStart=")
        )
        value = line.removeprefix("ExecStart=").removeprefix(":")
        return systemd_words(value)

    def test_relative_workspace_dot_resolves_to_the_install_time_directory(
        self,
    ) -> None:
        # Installed from INSIDE the workspace with '.': the unit must pin
        # the installation-time directory, never '.' — under systemd '.'
        # would be the user manager's working directory ($HOME), a
        # DIFFERENT workspace after reboot.
        workspace = self.world.workspace_dir
        self._chdir(workspace)
        code, _out, err = self.world.install(
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_workspace=".",
        )
        self.assertEqual(0, code, err)
        words = self._exec_start_words()
        flag_index = words.index("--zcode-workspace")
        self.assertEqual(str(workspace.resolve()), words[flag_index + 1])
        self.assertNotIn(".", words)
        read_write = next(
            l
            for l in self.world.unit_path.read_text(encoding="utf-8").splitlines()
            if l.startswith("ReadWritePaths=")
        )
        self.assertIn(str(workspace.resolve()), read_write)

    def test_relative_pinned_binary_resolves_to_the_exact_validated_executable(
        self,
    ) -> None:
        # Installed with a cwd-relative --zcode-bin that goes through a
        # symlink: the unit carries the canonical real executable that was
        # validated, never the relative spelling.
        bin_dir = self.world.tmp / "bin"
        real = bin_dir / "zcode-real"
        _ = real.write_text("#!/bin/sh\nexit 0\n")
        _ = real.chmod(0o755)
        alias = bin_dir / "zcode-link"
        os.symlink(str(real), str(alias))
        self._chdir(self.world.tmp)
        code, _out, err = self.world.install(
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_bin="bin/zcode-link",
            zcode_workspace="workspace",
        )
        self.assertEqual(0, code, err)
        words = self._exec_start_words()
        flag_index = words.index("--zcode-bin")
        self.assertEqual(str(real.resolve()), words[flag_index + 1])
        self.assertNotIn("bin/zcode-link", words)

    def test_relative_codex_pinned_binary_resolves_the_same_way(self) -> None:
        bin_dir = self.world.tmp / "bin"
        _ = bin_dir.mkdir(exist_ok=True)
        real = bin_dir / "codex-real"
        _ = real.write_text("#!/bin/sh\nexit 0\n")
        _ = real.chmod(0o755)
        self._chdir(self.world.tmp)
        code, _out, err = self.world.install(codex_bin="bin/codex-real")
        self.assertEqual(0, code, err)
        words = self._exec_start_words()
        flag_index = words.index("--codex-bin")
        self.assertEqual(str(real.resolve()), words[flag_index + 1])

    def test_relative_state_dir_resolves_to_the_install_time_directory(
        self,
    ) -> None:
        self._chdir(self.world.home_dir)
        self._pair(self.world.home_dir / "state-rel")
        code, _out, err = self.world.install(state_dir="state-rel")
        self.assertEqual(0, code, err)
        words = self._exec_start_words()
        flag_index = words.index("--state-dir")
        self.assertEqual(
            str((self.world.home_dir / "state-rel").resolve()),
            words[flag_index + 1],
        )

    def test_generated_unit_contains_no_relative_filesystem_arguments(
        self,
    ) -> None:
        self._chdir(self.world.home_dir)
        _ = (self.world.home_dir / "workspace").mkdir()
        bin_dir = self.world.home_dir / "bin"
        _ = bin_dir.mkdir(exist_ok=True)
        zbin = bin_dir / "zcode"
        _ = zbin.write_text("#!/bin/sh\nexit 0\n")
        _ = zbin.chmod(0o755)
        self._pair(self.world.home_dir / "state-rel")
        code, _out, err = self.world.install(
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_bin="bin/zcode",
            zcode_workspace="workspace",
            state_dir="state-rel",
        )
        self.assertEqual(0, code, err)
        words = self._exec_start_words()
        for flag in (
            "--state-dir",
            "--zcode-workspace",
            "--zcode-bin",
            "--codex-bin",
        ):
            if flag in words:
                self.assertTrue(
                    os.path.isabs(words[words.index(flag) + 1]),
                    f"{flag} persisted a relative value: {words}",
                )

    def test_bare_command_name_pinned_binary_is_refused_not_path_resolved(
        self,
    ) -> None:
        # A bare name would make the unit depend on whatever PATH the
        # installer happened to have; it is refused, never resolved.
        code, _out, err = self.world.install(
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_bin="zcode",
            zcode_workspace=str(self.world.workspace_dir),
        )
        self.assertEqual(2, code)
        self.assertIn("--zcode-bin", err)
        self.assertIn("not an existing regular executable", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_non_executable_pinned_binary_is_refused(self) -> None:
        not_exec = self.world.tmp / "not-executable"
        _ = not_exec.write_text("data")
        code, _out, err = self.world.install(
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_bin=str(not_exec),
            zcode_workspace=str(self.world.workspace_dir),
        )
        self.assertEqual(2, code)
        self.assertIn("not an existing regular executable", err)
        self.assertFalse(self.world.unit_path.exists())

    def test_missing_relative_workspace_is_refused_before_anything_is_written(
        self,
    ) -> None:
        self._chdir(self.world.tmp)
        code, _out, err = self.world.install(
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_workspace="does-not-exist",
        )
        self.assertEqual(2, code)
        self.assertIn("does-not-exist", err)
        self.assertIn("not an existing directory", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)


class WritableGrantPolicyTests(unittest.TestCase):
    """Daybreak blocker 2: broad writable grants are refused at install.

    No ``ReadWritePaths`` role may be the filesystem root or the home
    directory; the ZCode state home must stay the dedicated ZCode subtree
    (a ``~/.zcode`` symlink to ``$HOME`` or ``/`` fails installation);
    a workspace must never contain a protected boundary. Every refusal
    happens BEFORE the unit is written, before ``daemon-reload`` and
    before any ``systemctl`` call.
    """

    _tmp: tempfile.TemporaryDirectory[str]
    world: ServiceWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.world = cast("ServiceWorld", object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.world = ServiceWorld(Path(self._tmp.name))

    def _pair(self, state_dir: Path) -> None:
        return self.world.pair_state_dir(state_dir)

    def _zcode_install(self, **overrides: object) -> tuple[int, str, str]:
        arguments: dict[str, object] = {
            "zcode_workspace": str(self.world.workspace_dir)
        }
        arguments.update(overrides)
        return self.world.install(
            codex_sources=[],
            zcode_sources=["zai-plan"],
            **arguments,
        )

    def _replant_zcode_state_home(self, target: Path) -> None:
        planted = self.world.home_dir / ".zcode"
        planted.rmdir()
        os.symlink(str(target), str(planted))

    def test_workspace_filesystem_root_is_refused(self) -> None:
        code, _out, err = self.world.install(
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_workspace="/",
        )
        self.assertEqual(2, code)
        self.assertIn("filesystem root", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_workspace_home_itself_is_refused(self) -> None:
        code, _out, err = self._zcode_install(
            zcode_workspace=str(self.world.home_dir),
        )
        self.assertEqual(2, code)
        self.assertIn("home directory itself", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_workspace_ancestor_of_home_is_refused(self) -> None:
        code, _out, err = self._zcode_install(
            zcode_workspace=str(self.world.tmp),
        )
        self.assertEqual(2, code)
        self.assertIn("broad ancestor grant", err)
        self.assertFalse(self.world.unit_path.exists())

    def test_workspace_containing_the_state_dir_is_refused(self) -> None:
        alt_state = self.world.home_dir / "alt" / "state"
        self._pair(alt_state)
        code, _out, err = self.world.install(
            state_dir=str(alt_state),
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_workspace=str(self.world.home_dir / "alt"),
        )
        self.assertEqual(2, code)
        self.assertIn("contains or equals the worker state directory", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_workspace_equal_to_the_state_dir_is_refused(self) -> None:
        alt_state = self.world.home_dir / "alt" / "state"
        self._pair(alt_state)
        code, _out, err = self.world.install(
            state_dir=str(alt_state),
            codex_sources=[],
            zcode_sources=["zai-plan"],
            zcode_workspace=str(alt_state),
        )
        self.assertEqual(2, code)
        self.assertIn("contains or equals the worker state directory", err)
        self.assertFalse(self.world.unit_path.exists())

    def test_zcode_state_home_symlink_to_home_is_refused(self) -> None:
        self._replant_zcode_state_home(self.world.home_dir)
        code, _out, err = self._zcode_install()
        self.assertEqual(2, code)
        self.assertIn("resolves to the invoking user's home directory", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_zcode_state_home_symlink_to_root_is_refused(self) -> None:
        self._replant_zcode_state_home(Path("/"))
        code, _out, err = self._zcode_install()
        self.assertEqual(2, code)
        self.assertIn("filesystem root", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_zcode_state_home_broader_ancestor_is_refused(self) -> None:
        # ~/.zcode -> the tmp root (an ancestor of $HOME): broader than
        # the dedicated ZCode state subtree, so install refuses.
        self._replant_zcode_state_home(self.world.tmp)
        code, _out, err = self._zcode_install()
        self.assertEqual(2, code)
        self.assertIn(
            "broader than the dedicated ZCode state subtree", err
        )
        self.assertFalse(self.world.unit_path.exists())

    def test_normal_workspace_under_home_and_dedicated_state_dir_accepted(
        self,
    ) -> None:
        workspace = self.world.home_dir / "projects" / "foo"
        _ = workspace.mkdir(parents=True)
        code, _out, _err = self._zcode_install(zcode_workspace=str(workspace))
        self.assertEqual(0, code)
        self.assertTrue(self.world.unit_path.is_file())

    def test_normal_workspace_outside_home_is_accepted(self) -> None:
        # A workspace outside $HOME is legitimate (no arbitrary
        # must-be-under-home rule): only containment-destroying values
        # are refused.
        outside = self.world.tmp / "elsewhere" / "project"
        _ = outside.mkdir(parents=True)
        code, _out, _err = self._zcode_install(zcode_workspace=str(outside))
        self.assertEqual(0, code)
        self.assertTrue(self.world.unit_path.is_file())

    def test_group_writable_state_dir_is_refused_by_install(self) -> None:
        shared = self.world.home_dir / "shared-state"
        _ = shared.mkdir()
        # A REAL paired state directory whose permissions were loosened
        # (or a shared directory an operator pointed the worker at): the
        # policy refuses it as the ReadWritePaths grant and the lock
        # location — it never chmod's it into compliance silently.
        self._pair(shared)
        _ = shared.chmod(0o777)
        code, _out, err = self.world.install(state_dir=str(shared))
        self.assertEqual(2, code)
        self.assertIn("group- or world-writable", err)
        self.assertIn(str(shared), err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)


class ServiceHomeContractTests(unittest.TestCase):
    """The narrowed D-065 service-mode filesystem contract.

    Security-sensitive mutable state for the systemd user service lives
    under the invoking user's canonical home: ``service install``
    accepts the default state directory and an explicit private custom
    state directory beneath ``$HOME``, and refuses ``/``, the home
    itself and every location outside home (including a symlink that
    resolves outside) BEFORE anything is validated, written or enabled.
    The systemd user-unit directory carries the same boundary: the
    normal ``~/.config/systemd/user`` location is supported and an
    ``XDG_CONFIG_HOME`` outside the canonical home is refused for
    install. Dedicated private directories outside home remain a
    FOREGROUND ``run`` capability.
    """

    _tmp: tempfile.TemporaryDirectory[str]
    world: ServiceWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.world = cast("ServiceWorld", object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.world = ServiceWorld(Path(self._tmp.name))

    def _pair(self, state_dir: Path) -> None:
        return self.world.pair_state_dir(state_dir)

    def _install_through_env(
        self, *, unit_path: Path | None
    ) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = worker_service.run_service_command(
                self.world.install_arguments(),
                argv0=str(self.world.executable),
                tools=self.world.tools(),
                unit_path=unit_path,
                env=self.world.environment,
                open_store=open_worker_store,
                build_registry=build_registry,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_state_layout_under_home_installs(self) -> None:
        # The world's own state directory IS the primary supported path:
        # $HOME/.local/share/scarcity-router/worker.
        self.assertEqual(
            self.world.home_dir / ".local" / "share" / "scarcity-router" / "worker",
            self.world.state_dir,
        )
        code, _out, err = self.world.install()
        self.assertEqual(0, code, err)
        self.assertTrue(self.world.unit_path.is_file())

    def test_explicit_private_custom_state_dir_beneath_home_installs(self) -> None:
        custom = self.world.home_dir / "worker-state"
        custom.mkdir(mode=0o700)
        self._pair(custom)
        code, _out, err = self.world.install(state_dir=str(custom))
        self.assertEqual(0, code, err)
        unit_text = self.world.unit_path.read_text(encoding="utf-8")
        self.assertIn(f"--state-dir {custom}", unit_text)

    def test_state_dir_equal_to_home_is_refused(self) -> None:
        code, _out, err = self.world.install(state_dir=str(self.world.home_dir))
        self.assertEqual(2, code)
        self.assertIn("strictly beneath", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_state_dir_at_the_filesystem_root_is_refused(self) -> None:
        code, _out, err = self.world.install(state_dir="/")
        self.assertEqual(2, code)
        self.assertIn("strictly beneath", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_state_dir_outside_home_is_refused(self) -> None:
        outside = self.world.tmp / "srv" / "scarcity-worker"
        outside.mkdir(parents=True, mode=0o700)
        self._pair(outside)
        code, _out, err = self.world.install(state_dir=str(outside))
        self.assertEqual(2, code)
        self.assertIn("strictly beneath", err)
        self.assertIn(str(outside), err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_symlinked_state_dir_resolving_outside_home_is_refused(self) -> None:
        target = self.world.tmp / "elsewhere" / "worker"
        target.mkdir(parents=True, mode=0o700)
        self._pair(target)
        link = self.world.home_dir / "worker-link"
        os.symlink(str(target), str(link))
        code, _out, err = self.world.install(state_dir=str(link))
        self.assertEqual(2, code)
        self.assertIn("strictly beneath", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_unit_directory_through_xdg_outside_home_is_refused(self) -> None:
        outside_config = self.world.tmp / "outside-config"
        outside_config.mkdir()
        with patch.dict(
            os.environ,
            {
                "HOME": str(self.world.home_dir),
                "XDG_CONFIG_HOME": str(outside_config),
            },
        ):
            code, _out, err = self._install_through_env(unit_path=None)
        self.assertEqual(2, code)
        self.assertIn("must lie beneath the current user's home", err)
        self.assertFalse(
            (outside_config / "systemd").exists(),
            "the refused unit directory must not even be provisioned",
        )
        self.assertEqual([], self.world.systemctl_calls)

    def test_unit_directory_through_xdg_beneath_home_is_accepted(self) -> None:
        custom_config = self.world.home_dir / ".config-custom"
        with patch.dict(
            os.environ,
            {
                "HOME": str(self.world.home_dir),
                "XDG_CONFIG_HOME": str(custom_config),
            },
        ):
            code, _out, err = self._install_through_env(unit_path=None)
        self.assertEqual(0, code, err)
        unit = custom_config / "systemd" / "user" / worker_service.SERVICE_UNIT_NAME
        self.assertTrue(unit.is_file())
        self.assertEqual(0o700, os.stat(unit.parent).st_mode & 0o777)


class UnitWriterSecurityTests(unittest.TestCase):
    """Daybreak blocker 3: the unit write is secure and atomic.

    The unit directory is trust-checked (owned by the user, closed to
    group/other writes); the temporary file is a randomized exclusive
    creation inside that directory — a planted predictable temp path or
    a planted unit symlink can neither redirect the write nor clobber an
    unrelated target.
    """

    _tmp: tempfile.TemporaryDirectory[str]
    world: ServiceWorld

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.world = cast("ServiceWorld", object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.world = ServiceWorld(Path(self._tmp.name))

    def _make_unit_dir(self) -> Path:
        unit_dir = self.world.unit_path.parent
        unit_dir.mkdir(parents=True, exist_ok=True)
        return unit_dir

    def test_directory_replacement_cannot_redirect_inspection_or_publication(self) -> None:
        for replace_ancestor in (False, True):
            with self.subTest(ancestor=replace_ancestor), tempfile.TemporaryDirectory() as tmp:
                world = ServiceWorld(Path(tmp))
                unit_dir = world.unit_path.parent
                unit_dir.mkdir(parents=True)
                _ = world.unit_path.write_text(worker_service.UNIT_MARKER_LINE + "\nold\n")
                source = unit_dir.parent.parent if replace_ancestor else unit_dir
                retained = source.with_name(source.name + "-validated")
                def verify(directory: Path, *, env: object = None) -> int:
                    from scarcity_router.worker_identity_store import open_trusted_worker_directory
                    return open_trusted_worker_directory(
                        directory, create=True, env=env  # pyright: ignore[reportArgumentType]
                    )

                def replace_after_verification(directory: Path, *, env: object = None) -> int:
                    fd = verify(directory, env=env)
                    _ = source.rename(retained)
                    unit_dir.mkdir(parents=True)
                    _ = world.unit_path.write_text("UNRELATED UNIT IN REPLACEMENT\n")
                    return fd

                with patch.object(
                    worker_service, "_verify_unit_directory", replace_after_verification
                ):
                    code, _out, err = world.install()
                self.assertEqual(2, code, err)
                self.assertIn("changed during installation", err)
                self.assertEqual("UNRELATED UNIT IN REPLACEMENT\n", world.unit_path.read_text())
                old_unit = retained / world.unit_path.relative_to(source)
                self.assertIn("Restart=on-failure", old_unit.read_text())
                self.assertEqual([], world.systemctl_calls)

    def test_writable_ancestor_refused_before_provisioning(self) -> None:
        parent = self.world.unit_path.parent.parent.parent
        parent.mkdir()
        parent.chmod(0o777)
        code, _out, err = self.world.install()
        self.assertEqual(2, code)
        self.assertIn(str(parent), err)
        self.assertIn("choose a private location", err)
        self.assertFalse((parent / "systemd").exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_implicit_systemd_path_cannot_use_an_alternate_symlink_chain(self) -> None:
        safe = self.world.tmp / "safe-config"
        safe.mkdir()
        shared = self.world.tmp / "shared-config-parent"
        shared.mkdir()
        shared.chmod(0o777)
        alias = shared / "config"
        alias.symlink_to(safe, target_is_directory=True)
        self.world.unit_path = alias / "systemd" / "user" / worker_service.SERVICE_UNIT_NAME
        code, _out, err = self.world.install()
        self.assertEqual(2, code)
        self.assertIn("private canonical configuration location", err)
        self.assertEqual([], list(safe.iterdir()))
        self.assertEqual([], self.world.systemctl_calls)

    def test_unsafe_state_symlink_refused_before_any_store_access(self) -> None:
        victim = self.world.tmp / "victim.db"
        _ = victim.write_bytes(b"UNCHANGED VICTIM")
        victim.chmod(0o644)
        database = self.world.state_dir / "worker-state.db"
        database.unlink()
        database.symlink_to(victim)
        self.world.state_dir.chmod(0o777)
        opener = Mock(wraps=open_worker_store)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = worker_service.run_service_command(
                self.world.install_arguments(), argv0=str(self.world.executable),
                tools=self.world.tools(), unit_path=self.world.unit_path,
                env=self.world.environment, open_store=opener, build_registry=build_registry,
            )
        self.assertEqual(2, code)
        opener.assert_not_called()
        self.assertEqual(b"UNCHANGED VICTIM", victim.read_bytes())
        self.assertEqual(0o644, victim.stat().st_mode & 0o777)
        self.assertTrue(database.is_symlink())
        self.assertEqual([], self.world.systemctl_calls)

    def test_state_ancestor_refused_before_store_access(self) -> None:
        shared = self.world.home_dir / "shared"
        shared.mkdir(mode=0o777)
        shared.chmod(0o777)
        state = shared / "private-leaf"
        state.mkdir(mode=0o700)
        opener = Mock(wraps=open_worker_store)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = worker_service.run_service_command(
                self.world.install_arguments(state_dir=str(state)),
                argv0=str(self.world.executable), tools=self.world.tools(),
                unit_path=self.world.unit_path, env=self.world.environment,
                open_store=opener, build_registry=build_registry,
            )
        self.assertEqual(2, code)
        opener.assert_not_called()
        self.assertEqual([], list(state.iterdir()))

    def test_default_first_run_is_provisioned_before_store_open(self) -> None:
        state = (
            self.world.home_dir / "new-data" / "scarcity-router" / "worker"
        )
        seen: list[str] = []

        def open_verified(path: str) -> WorkerLocalStore:
            self.assertTrue(state.is_dir())
            self.assertEqual(0o700, state.stat().st_mode & 0o777)
            seen.append(path)
            return open_worker_store(path)

        with patch.object(worker_service, "default_worker_state_dir", return_value=str(state)):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                code = worker_service.run_service_command(
                    self.world.install_arguments(state_dir=None),
                    argv0=str(self.world.executable), tools=self.world.tools(),
                    unit_path=self.world.unit_path, env=self.world.environment,
                    open_store=open_verified, build_registry=build_registry,
                )
        self.assertEqual(2, code)  # safely provisioned but not paired yet
        self.assertEqual([str(state)], seen)

    def test_missing_custom_state_is_not_created_by_store(self) -> None:
        state = self.world.home_dir / "custom-missing"
        code, _out, _err = self.world.install(state_dir=str(state))
        self.assertEqual(2, code)
        self.assertFalse(state.exists())

    def test_group_writable_unit_directory_is_refused(self) -> None:
        unit_dir = self._make_unit_dir()
        _ = unit_dir.chmod(0o777)
        code, _out, err = self.world.install()
        self.assertEqual(2, code)
        self.assertIn("group- or world-writable", err)
        self.assertIn(str(unit_dir), err)
        self.assertFalse(self.world.unit_path.exists())
        # Refusal happens BEFORE daemon-reload / enable --now.
        self.assertEqual([], self.world.systemctl_calls)

    def test_preexisting_unit_directory_mode_is_never_silently_changed(
        self,
    ) -> None:
        unit_dir = self._make_unit_dir()
        _ = unit_dir.chmod(0o755)
        code, _out, _err = self.world.install()
        self.assertEqual(0, code)
        self.assertEqual(0o755, os.stat(unit_dir).st_mode & 0o777)
        self.assertTrue(self.world.unit_path.is_file())

    def test_missing_unit_directory_is_created_private(self) -> None:
        code, _out, _err = self.world.install()
        self.assertEqual(0, code)
        self.assertEqual(0o700, os.stat(self.world.unit_path.parent).st_mode & 0o777)

    def test_planted_predictable_temp_paths_cannot_redirect_or_clobber(
        self,
    ) -> None:
        # The previous writer opened a predictable '.tmp-<pid>' path by
        # name: a pre-planted regular file there was clobbered and a
        # planted symlink there was followed. The randomized exclusive
        # temp name can collide with neither.
        unit_dir = self._make_unit_dir()
        decoy = self.world.tmp / "decoy.txt"
        _ = decoy.write_text("DECOY-CONTENT")
        planted_regular = unit_dir / (
            worker_service.SERVICE_UNIT_NAME + f".tmp-{os.getpid()}"
        )
        _ = planted_regular.write_text("PLANTED-REGULAR")
        planted_link = unit_dir / (worker_service.SERVICE_UNIT_NAME + ".tmp-evil")
        os.symlink(str(decoy), str(planted_link))
        code, _out, _err = self.world.install()
        self.assertEqual(0, code)
        self.assertEqual("DECOY-CONTENT", decoy.read_text())
        self.assertEqual("PLANTED-REGULAR", planted_regular.read_text())
        self.assertTrue(os.path.islink(planted_link))
        unit_text = self.world.unit_path.read_text(encoding="utf-8")
        self.assertTrue(unit_text.startswith(worker_service.UNIT_MARKER_LINE))
        self.assertTrue(self.world.unit_path.is_file())
        self.assertFalse(os.path.islink(self.world.unit_path))

    def test_planted_unit_symlink_is_replaced_without_touching_its_target(
        self,
    ) -> None:
        # A marker-carrying symlink AT the unit path: the atomic rename
        # replaces the LINK with a real file; the link's target — which
        # the old write_text-through-the-temp flow could have reached —
        # is never modified.
        target = self.world.tmp / "unrelated-target"
        _ = target.write_text(worker_service.UNIT_MARKER_LINE + "\nold\n")
        _ = self._make_unit_dir()
        os.symlink(str(target), str(self.world.unit_path))
        code, _out, _err = self.world.install()
        self.assertEqual(0, code)
        self.assertEqual(
            worker_service.UNIT_MARKER_LINE + "\nold\n", target.read_text()
        )
        self.assertTrue(os.path.islink(target) is False)
        self.assertFalse(os.path.islink(self.world.unit_path))
        self.assertIn("Restart=on-failure", self.world.unit_path.read_text(encoding="utf-8"))

    def test_successful_write_stays_marker_compatible_and_atomic(self) -> None:
        code, out, _err = self.world.install()
        self.assertEqual(0, code)
        unit_text = self.world.unit_path.read_text(encoding="utf-8")
        self.assertTrue(unit_text.startswith(worker_service.UNIT_MARKER_LINE))
        self.assertIn("unit written", out)
        # The 0o644 unit mode is set on the open descriptor before the
        # rename: the final file never exists with a different mode.
        self.assertEqual(0o644, os.stat(self.world.unit_path).st_mode & 0o777)
        self.assertEqual(
            [], list(self.world.unit_path.parent.glob("*.tmp-*"))
        )


if __name__ == "__main__":
    _ = unittest.main()
