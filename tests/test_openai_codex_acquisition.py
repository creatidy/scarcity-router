"""Security, protocol and lifecycle tests for the OpenAI Codex acquisition.

The subprocess seam (``spawn_app_server``) is replaced with a fake app-server
built on a real ``os.pipe``; no test in this module executes a real binary,
contacts any account or reads any credential. The fake's scripted stdout
lines are synthetic JSONL protocol messages derived only from the redacted
shapes in ``docs/poc-evidence.md`` and the fixture fixtures under
``tests/fixtures/openai-codex-appserver/``.

Every failure class asserts that synthetic secret material appears in no
serialized snapshot, repr, diagnostic or captured stdout/stderr output.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import threading
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast, override
from unittest import mock

from scarcity_router import CapacitySnapshot, CapacityValidationError
from scarcity_router.providers import openai_codex_acquisition as acq
from scarcity_router.providers.openai_codex import parse_codex_rate_limits_result

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "openai-codex-appserver"
RETRIEVED_AT = "2026-09-03T20:00:00.000Z"
INVALID_RETRIEVED_AT = "2026-09-03T20:00:00Z"  # missing milliseconds

# Conspicuous synthetic-only secrets; never realistic production shapes.
SECRET = "TEST_ONLY_OPENAI_SECRET_NEVER_REAL"
ALL_FAKE_SECRETS: tuple[str, ...] = (SECRET,)

INIT_RESPONSE = b'{"id":1,"result":{"userAgent":"x","platformOs":"linux"}}\n'


def _fixture_result(name: str) -> object:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return cast("object", json.load(handle))


def _read_response(result: object) -> bytes:
    return json.dumps({"id": 2, "result": result}).encode() + b"\n"


class _FakeStdin:
    """Records every write; close() mirrors a real pipe closing."""

    writes: list[bytes]
    closed: bool

    def __init__(self) -> None:
        self.writes = []
        self.closed = False

    def write(self, data: bytes) -> int:
        if self.closed:
            raise ValueError("write to closed file")
        self.writes.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeAppServer:
    """Fake Codex app-server process on a real pipe.

    Scripted lines are fed to the production reader thread exactly as a real
    child would feed them (blocking pipe reads). ``block`` keeps the pipe
    open forever after the scripted lines (a stalled child). ``stubborn``
    makes the first bounded ``wait`` time out so the kill path is exercised.

    The feeder writes through a non-blocking descriptor and parks on a stop
    event, and the write end is closed only after the feeder thread has
    joined: a blocked ``os.write`` must never have its descriptor closed
    underneath it, or the write could land on a recycled fd number.
    """

    stdin: _FakeStdin
    stdout: io.BufferedReader
    events: list[str]
    _lines: tuple[bytes, ...]
    _block: bool
    _stubborn: bool
    _exit_code: int
    _write_fd: int
    _read_fd: int
    _write_closed: bool
    _stop: threading.Event
    _feeder: threading.Thread
    _lock: threading.Lock

    def __init__(
        self,
        lines: Sequence[bytes] = (),
        *,
        block: bool = False,
        stubborn: bool = False,
        exit_code: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self.events = []
        self._lines = tuple(lines)
        self._block = block
        self._stubborn = stubborn
        self._exit_code = exit_code
        self._read_fd, self._write_fd = os.pipe()
        self._write_closed = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.stdout = io.BufferedReader(io.FileIO(self._read_fd, "rb"))
        self._feeder = threading.Thread(target=self._feed, daemon=True)
        self._feeder.start()

    def _feed(self) -> None:
        os.set_blocking(self._write_fd, False)
        remaining = b"".join(self._lines)
        while remaining:
            if self._stop.is_set():
                return
            try:
                written = os.write(self._write_fd, remaining)
            except BlockingIOError:
                time.sleep(0.002)
                continue
            remaining = remaining[written:]
        if self._block:
            _ = self._stop.wait()
            return
        self._close_write()

    def _close_write(self) -> None:
        """Stop the feeder, then close the write end exactly once.

        Safe to call from the feeder thread itself (the non-block completion
        path), in which case no join happens: the caller is already done.
        """
        self._stop.set()
        if threading.current_thread() is not self._feeder:
            self._feeder.join(timeout=5.0)
        with self._lock:
            if not self._write_closed:
                self._write_closed = True
                try:
                    os.close(self._write_fd)
                except OSError:
                    pass

    def finalize(self) -> None:
        """Test cleanup: release the pipe regardless of protocol outcome."""
        self._close_write()
        try:
            self.stdout.close()
        except OSError:
            pass

    def written_messages(self) -> "list[dict[str, object]]":
        decoded: list[dict[str, object]] = []
        for chunk in self.stdin.writes:
            value = cast("object", json.loads(chunk.decode("utf-8")))
            decoded.append(cast("dict[str, object]", value))
        return decoded

    def terminate(self) -> None:
        self.events.append("terminate")
        self._close_write()

    def kill(self) -> None:
        self.events.append("kill")
        self._close_write()

    def wait(self, timeout: float | None = None) -> int:
        if self._stubborn and "kill" not in self.events:
            raise subprocess.TimeoutExpired(cmd="fake-codex", timeout=timeout or 0.0)
        return self._exit_code

    def poll(self) -> int | None:
        if "terminate" in self.events or "kill" in self.events:
            return self._exit_code
        return None


class _AcquisitionCase(unittest.TestCase):
    """Shared harness: fake spawn seam plus output capture."""

    tmp: Path = Path("/")
    fake: _FakeAppServer | None = None
    spawn_calls: list[list[str]] = []
    stdout: io.StringIO = io.StringIO()
    stderr: io.StringIO = io.StringIO()

    @override
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fake = None
        self.spawn_calls = []
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def _install_fake(
        self,
        lines: Sequence[bytes] = (),
        *,
        block: bool = False,
        stubborn: bool = False,
        error: Exception | None = None,
    ) -> _FakeAppServer:
        fake = _FakeAppServer(lines, block=block, stubborn=stubborn)
        self.addCleanup(fake.finalize)
        self.fake = fake

        def fake_spawn(argv: Sequence[str]) -> "subprocess.Popen[bytes]":
            self.spawn_calls.append(list(argv))
            if error is not None:
                raise error
            return cast("subprocess.Popen[bytes]", cast("object", fake))

        patcher = mock.patch.object(acq, "spawn_app_server", fake_spawn)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _make_installation(self, *, layout: int = 1) -> Path:
        """A synthetic supported installation tree for the current host."""
        platform_dir = acq.platform_directory()
        assert platform_dir is not None
        root = self.tmp / "extensions"
        extension = root / "openai.chatgpt-26.825.51511-linux-x64"
        binary_dir = extension / "bin" / platform_dir
        _ = binary_dir.mkdir(parents=True, exist_ok=True)
        binary = binary_dir / "codex"
        _ = binary.write_bytes(b"#!/bin/sh\nexit 0\n")  # never executed
        _ = binary.chmod(0o755)
        package: dict[str, object] = {
            "layoutVersion": layout,
            "version": "0.151.0-alpha.7.2",
            "target": "synthetic",
            "variant": "codex",
            "entrypoint": "bin/codex",
        }
        _ = (binary_dir / "codex-package.json").write_text(
            json.dumps(package), encoding="utf-8"
        )
        return root

    def _collect(
        self,
        *,
        discovery_roots: Sequence[Path] | None,
        startup_timeout: float | None = 5.0,
        session_timeout: float | None = 5.0,
        retrieved_at: str = RETRIEVED_AT,
    ) -> CapacitySnapshot:
        assert startup_timeout is not None and session_timeout is not None
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return acq.collect_openai_codex_capacity(
                retrieved_at=retrieved_at,
                discovery_roots=discovery_roots,
                startup_timeout=startup_timeout,
                session_timeout=session_timeout,
            )

    def _assert_no_output(self) -> None:
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "")


def _serialized(snapshot: CapacitySnapshot) -> str:
    return json.dumps(snapshot.to_dict(), sort_keys=True) + repr(snapshot)


# ═════════════════════════ successful session ════════════════════════════════


class SuccessfulSession(_AcquisitionCase):
    def _happy_lines(self, fixture: str) -> list[bytes]:
        return [
            INIT_RESPONSE,
            b'{"method":"app/installed","params":{"status":"ok"},"emittedAtMs":1788306212999}\n',
            _read_response(_fixture_result(fixture)),
        ]

    def test_fixture_matches_pure_parser_exactly(self) -> None:
        for fixture in (
            "ratelimits-ok-plus.json",
            "ratelimits-slots-swapped.json",
            "ratelimits-unknown-duration.json",
            "ratelimits-exhausted-reached.json",
            "ratelimits-zero-usage.json",
            "ratelimits-degraded.json",
            "ratelimits-schema-changed.json",
        ):
            with self.subTest(fixture=fixture):
                fake = self._install_fake(self._happy_lines(fixture))
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                expected = parse_codex_rate_limits_result(
                    _fixture_result(fixture), retrieved_at=RETRIEVED_AT
                )
                self.assertEqual(snapshot, expected)
                self.assertEqual(snapshot.provider, "openai")
                self.assertEqual(snapshot.source, "codex_app_server")
                self._assert_no_output()
                _ = fake

    def test_protocol_exchange_shape_and_order(self) -> None:
        fake = self._install_fake(self._happy_lines("ratelimits-ok-plus.json"))
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])

        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(len(self.spawn_calls), 1)
        argv = self.spawn_calls[0]
        self.assertEqual(argv[1:], ["app-server"])
        self.assertEqual(Path(argv[0]).name, "codex")

        messages = fake.written_messages()
        self.assertEqual(len(messages), 3)  # exactly three bounded writes
        initialize = messages[0]
        self.assertEqual(initialize["method"], "initialize")
        self.assertEqual(initialize["id"], 1)
        client_info = cast("dict[str, object]", initialize["params"])
        self.assertIn("clientInfo", client_info)
        initialized = messages[1]
        self.assertEqual(initialized["method"], "notifications/initialized")
        self.assertNotIn("id", initialized)
        read = messages[2]
        self.assertEqual(read["method"], "account/rateLimits/read")
        self.assertEqual(read["id"], 2)

    def test_blank_separator_lines_tolerated(self) -> None:
        _ = self._install_fake(
            [b"\n", INIT_RESPONSE, b"   \n", _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(len(snapshot.windows), 2)


# ═════════════════════════ response matching ═════════════════════════════════


class ResponseMatching(_AcquisitionCase):
    def test_unexpected_notifications_do_not_confuse_matching(self) -> None:
        noise = [
            b'{"method":"account/rateLimits/updated","params":{"x":1},"emittedAtMs":1}\n',
            b'{"method":"some/other","params":{}}\n',
            b'{"method":"third","params":{"deep":{"x":[1,2]}},"emittedAtMs":2}\n',
        ]
        _ = self._install_fake(
            [INIT_RESPONSE, *noise, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(len(snapshot.windows), 2)

    def test_wrong_request_id_and_method_never_match(self) -> None:
        wrong = [
            b'{"id":99,"result":{"rateLimits":{"limitId":"codex"}}}\n',
            b'{"id":2,"method":"account/rateLimits/updated","params":{}}\n',
            b'{"id":1,"result":{}}\n',  # stale initialize response
            b'{"id":7,"method":"elicitation/create","params":{}}\n',  # server request
        ]
        _ = self._install_fake(
            [INIT_RESPONSE, *wrong, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(len(snapshot.windows), 2)

    def test_string_id_response_fails_closed_as_drift(self) -> None:
        # A response-shaped message with a string id is structurally invalid
        # under the observed protocol (integer ids only); it must fail closed
        # rather than be treated as our answer or silently skipped.
        _ = self._install_fake(
            [
                INIT_RESPONSE,
                b'{"id":"2","result":{"rateLimits":{"primary":{"usedPercent":1,"windowDurationMins":300}}}}\n',
            ]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["schema_changed"])

    def test_read_error_response_maps_to_unknown(self) -> None:
        error_line = json.dumps(
            {"id": 2, "error": {"code": -32603, "message": f"auth token {SECRET}"}}
        ).encode() + b"\n"
        _ = self._install_fake([INIT_RESPONSE, error_line])
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unknown")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["telemetry_unknown"])
        self.assertEqual(snapshot.windows, ())
        self.assertNotIn(SECRET, _serialized(snapshot))
        self._assert_no_output()

    def test_initialize_error_response_maps_to_unknown(self) -> None:
        _ = self._install_fake(
            [b'{"id":1,"error":{"code":-32603,"message":"x"}}\n']
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unknown")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["telemetry_unknown"])
        self.assertEqual(snapshot.windows, ())

    def test_initialize_result_non_object_maps_to_schema_changed(self) -> None:
        _ = self._install_fake([b'{"id":1,"result":"ok"}\n'])
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["schema_changed"])
        self.assertEqual(snapshot.windows, ())


# ═════════════════════ process failures and bounds ═══════════════════════════


class ProcessFailures(_AcquisitionCase):
    def test_spawn_failure_maps_to_unavailable(self) -> None:
        _ = self._install_fake(error=FileNotFoundError("no such binary"))
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["source_unavailable"])
        self.assertEqual(snapshot.windows, ())
        self._assert_no_output()

    def test_process_exit_before_initialize_maps_to_unavailable(self) -> None:
        _ = self._install_fake([])  # immediate EOF
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["source_unavailable"])

    def test_process_exit_before_read_maps_to_unavailable(self) -> None:
        _ = self._install_fake([INIT_RESPONSE])
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["source_unavailable"])

    def test_stalled_child_times_out_to_unavailable(self) -> None:
        _ = self._install_fake([INIT_RESPONSE], block=True)
        roots = self._make_installation()
        snapshot = self._collect(
            discovery_roots=[roots], startup_timeout=5.0, session_timeout=0.2
        )
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["source_unavailable"])
        self.assertEqual(snapshot.windows, ())
        self._assert_no_output()

    def test_startup_timeout_maps_to_unavailable(self) -> None:
        _ = self._install_fake([], block=True)
        roots = self._make_installation()
        snapshot = self._collect(
            discovery_roots=[roots], startup_timeout=0.2, session_timeout=5.0
        )
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["source_unavailable"])


class MalformedAndBoundedOutput(_AcquisitionCase):
    def test_malformed_jsonl_maps_to_schema_changed(self) -> None:
        for bad in (b'{"id": 2, \n', b"not json at all\n", b"[1, 2, 3]\n", b'"string"\n'):
            with self.subTest(bad=bad):
                _ = self._install_fake([INIT_RESPONSE, bad])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")
                self.assertEqual([d.code for d in snapshot.diagnostics], ["schema_changed"])
                self.assertEqual(snapshot.windows, ())

    def test_invalid_utf8_maps_to_schema_changed(self) -> None:
        _ = self._install_fake([INIT_RESPONSE, b'{"id":2,"result":{"x":"\xff\xfe"}}\n'])
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "schema_changed")

    def test_structurally_invalid_message_maps_to_schema_changed(self) -> None:
        for bad in (
            b'{"id":2,"result":{},"error":{}}\n',  # result and error
            b'{"id":2}\n',  # neither
            b'{"id":true,"result":{}}\n',  # boolean id
        ):
            with self.subTest(bad=bad):
                _ = self._install_fake([INIT_RESPONSE, bad])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")

    def test_oversized_line_maps_to_schema_changed(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, b"X" * (acq.MAX_LINE_BYTES + 64) + b"\n"]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["schema_changed"])

    def test_total_output_budget_maps_to_schema_changed(self) -> None:
        noise = b'{"method":"noise/notification","params":{"pad":"pppppppppp"}}\n'
        flood = [INIT_RESPONSE] + [noise] * 32 + [
            _read_response(_fixture_result("ratelimits-ok-plus.json"))
        ]
        _ = self._install_fake(flood)
        roots = self._make_installation()
        with mock.patch.object(acq, "MAX_TOTAL_BYTES", len(INIT_RESPONSE) + 4 * len(noise)):
            snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["schema_changed"])

    def test_oversized_line_with_secret_never_leaks(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, (SECRET.encode() + b"-") * 4096 + b"\n"]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertNotIn(SECRET, _serialized(snapshot))
        self._assert_no_output()

    def test_secret_in_error_and_noise_never_leaks(self) -> None:
        lines = [
            INIT_RESPONSE,
            f'{{"method":"noise","params":{{"token":"{SECRET}"}}}}'.encode() + b"\n",
            json.dumps(
                {"id": 2, "error": {"code": -32000, "message": SECRET}}
            ).encode()
            + b"\n",
        ]
        _ = self._install_fake(lines)
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unknown")
        text = _serialized(snapshot)
        for secret in ALL_FAKE_SECRETS:
            self.assertNotIn(secret, text)
        self._assert_no_output()


# ═════════════════════════ safe termination ══════════════════════════════════


class SafeTermination(_AcquisitionCase):
    def test_success_path_terminates_without_kill(self) -> None:
        fake = self._install_fake(
            [
                INIT_RESPONSE,
                _read_response(_fixture_result("ratelimits-ok-plus.json")),
            ]
        )
        roots = self._make_installation()
        _ = self._collect(discovery_roots=[roots])
        self.assertEqual(fake.events, ["terminate"])
        self.assertTrue(fake.stdin.closed)

    def test_failure_paths_terminate_without_kill(self) -> None:
        for lines, block in (
            ([], False),  # immediate exit
            ([INIT_RESPONSE], False),  # exit before read
        ):
            with self.subTest(lines=lines):
                fake = self._install_fake(lines, block=block)
                roots = self._make_installation()
                _ = self._collect(discovery_roots=[roots])
                self.assertEqual(fake.events, ["terminate"])
                self.assertTrue(fake.stdin.closed)

    def test_timeout_path_terminates_and_never_leaks(self) -> None:
        fake = self._install_fake([INIT_RESPONSE], block=True)
        roots = self._make_installation()
        snapshot = self._collect(
            discovery_roots=[roots], session_timeout=0.2
        )
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual(fake.events, ["terminate"])
        self.assertTrue(fake.stdin.closed)

    def test_stubborn_child_is_killed_after_bounded_wait(self) -> None:
        fake = self._install_fake(
            [
                INIT_RESPONSE,
                _read_response(_fixture_result("ratelimits-ok-plus.json")),
            ],
            stubborn=True,
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(fake.events, ["terminate", "kill"])
        self.assertTrue(fake.stdin.closed)

    def test_shutdown_never_raises_through_collect(self) -> None:
        class _Rude:
            stdin: _FakeStdin = _FakeStdin()
            stdout: io.BytesIO = io.BytesIO(b"")  # immediate EOF

            def terminate(self) -> None:
                raise OSError("terminate failed")

            def kill(self) -> None:
                raise OSError("kill failed")

            def wait(self, timeout: float | None = None) -> int:
                _ = timeout
                raise OSError("wait failed")

        fake = _Rude()
        roots = self._make_installation()

        def fake_spawn(argv: Sequence[str]) -> "subprocess.Popen[bytes]":
            _ = argv
            return cast("subprocess.Popen[bytes]", cast("object", fake))

        with mock.patch.object(acq, "spawn_app_server", fake_spawn):
            snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unavailable")


# ═════════════════════════ discovery ═════════════════════════════════════════


class Discovery(unittest.TestCase):
    tmp: Path = Path("/")

    @override
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _make(
        self,
        extension_version: str = "26.825.51511",
        *,
        layout: int | None = 1,
        variant: str = "codex",
        executable: bool = True,
        package: str | None = None,
        platform_dir: str | None = None,
        suffix: str = "",
    ) -> Path:
        root = self.tmp / f"extensions-{extension_version}-{layout}-{variant}-{suffix}"
        if platform_dir is None:
            platform_dir = acq.platform_directory() or "linux-x86_64"
        extension = root / f"openai.chatgpt-{extension_version}-linux-x64"
        binary_dir = extension / "bin" / platform_dir
        _ = binary_dir.mkdir(parents=True)
        binary = binary_dir / "codex"
        _ = binary.write_bytes(b"#!/bin/sh\nexit 0\n")
        _ = binary.chmod(0o755 if executable else 0o644)
        if package is not None:
            _ = (binary_dir / "codex-package.json").write_text(
                package, encoding="utf-8"
            )
        elif layout is not None:
            _ = (binary_dir / "codex-package.json").write_text(
                json.dumps(
                    {
                        "layoutVersion": layout,
                        "variant": variant,
                        "version": "0.151.0-alpha.7.2",
                    }
                ),
                encoding="utf-8",
            )
        return root

    def test_found_reports_versions(self) -> None:
        root = self._make()
        installation, outcome = acq.discover_codex_installation([root])
        self.assertEqual(outcome, "found")
        assert installation is not None
        self.assertEqual(installation.extension_version, "26.825.51511")
        self.assertEqual(installation.codex_version, "0.151.0-alpha.7.2")
        self.assertEqual(installation.binary.name, "codex")

    def test_highest_extension_version_wins(self) -> None:
        older = self._make("25.1.1")
        newer = self._make("27.0.1")
        installation, outcome = acq.discover_codex_installation([older, newer])
        self.assertEqual(outcome, "found")
        assert installation is not None
        self.assertEqual(installation.extension_version, "27.0.1")

    def test_newest_unsupported_layout_falls_back_to_older_supported(self) -> None:
        older = self._make("25.1.1")
        newer_drifted = self._make("27.0.1", layout=2)
        installation, outcome = acq.discover_codex_installation(
            [older, newer_drifted]
        )
        self.assertEqual(outcome, "found")
        assert installation is not None
        self.assertEqual(installation.extension_version, "25.1.1")

    def test_missing_roots_map_to_not_installed(self) -> None:
        installation, outcome = acq.discover_codex_installation(
            [self.tmp / "does-not-exist"]
        )
        self.assertIsNone(installation)
        self.assertEqual(outcome, "not_installed")

    def test_non_chatgpt_extensions_are_ignored(self) -> None:
        root = self.tmp / "extensions"
        _ = (root / "ms-python.python-2026.0.0").mkdir(parents=True)
        installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "not_installed")

    def test_unusable_installations_map_to_unsupported(self) -> None:
        cases: dict[str, Path] = {
            "layout-2": self._make(layout=2, suffix="a"),
            "wrong-variant": self._make(variant="chatgpt", suffix="b"),
            "no-package": self._make(layout=None, suffix="c"),
            "not-executable": self._make(executable=False, suffix="d"),
            "duplicate-package-keys": self._make(
                suffix="e",
                package='{"layoutVersion":1,"layoutVersion":1,"variant":"codex","version":"0.151.0"}',
            ),
            "malformed-package": self._make(suffix="f", package="{not json"),
            "empty-version": self._make(
                suffix="g",
                package='{"layoutVersion":1,"variant":"codex","version":""}',
            ),
        }
        for name, root in cases.items():
            with self.subTest(case=name):
                installation, outcome = acq.discover_codex_installation([root])
                self.assertIsNone(installation, msg=name)
                self.assertEqual(outcome, "unsupported_installation", msg=name)

    def test_missing_platform_directory_maps_to_unsupported(self) -> None:
        root = self._make(platform_dir="other-platform")
        installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    def test_discovery_never_uses_default_roots_when_given(self) -> None:
        root = self._make()
        with mock.patch.object(
            acq, "DEFAULT_DISCOVERY_ROOTS", (self.tmp / "absent",)
        ) as _patched:
            installation, outcome = acq.discover_codex_installation([root])
        self.assertEqual(outcome, "found")
        assert installation is not None


# ═════════════════════════ discovery integration ═════════════════════════════


class DiscoveryIntegration(_AcquisitionCase):
    def test_not_installed_maps_to_unavailable_without_spawn(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        snapshot = self._collect(discovery_roots=[self.tmp / "absent"])
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["source_unavailable"])
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(self.spawn_calls, [])
        self._assert_no_output()

    def test_unsupported_installation_maps_to_unsupported_without_spawn(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        root = self._make_installation(layout=2)
        snapshot = self._collect(discovery_roots=[root])
        self.assertEqual(snapshot.status, "unsupported")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["unsupported_source"])
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(self.spawn_calls, [])
        self._assert_no_output()

    def test_snapshots_never_contain_local_paths_or_versions(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "ok")
        text = _serialized(snapshot)
        for forbidden in (
            str(roots),
            str(self.tmp),
            ".vscode",
            "codex-package.json",
            "0.151.0-alpha.7.2",
            "26.825.51511",
            "app-server",
            "userAgent",
            "codexHome",
            "auth.json",
            ".codex",
        ):
            self.assertNotIn(forbidden, text, msg=forbidden)
        self._assert_no_output()

    def test_invalid_retrieved_at_fails_through_contract_validation(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        roots = self._make_installation()
        with self.assertRaises(CapacityValidationError):
            _ = self._collect(discovery_roots=[roots], retrieved_at=INVALID_RETRIEVED_AT)
        self._assert_no_output()

    def test_snapshot_round_trips_through_v1(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, _read_response(_fixture_result("ratelimits-degraded.json"))]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        reparsed = CapacitySnapshot.from_dict(snapshot.to_dict())
        self.assertEqual(reparsed, snapshot)


# ═════════════════════════ subprocess surface ════════════════════════════════


class SubprocessSurface(unittest.TestCase):
    """The spawn seam's own argument surface, without executing anything."""

    def test_stderr_is_discarded_never_piped(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "scarcity_router"
            / "providers"
            / "openai_codex_acquisition.py"
        ).read_text(encoding="utf-8")
        self.assertIn("stderr=subprocess.DEVNULL", source)
        self.assertNotIn("stderr=subprocess.PIPE", source)
        self.assertNotIn("stderr=None", source)

    def test_no_shell_and_bounded_writes(self) -> None:
        # Requests are exactly three fixed JSON documents; there is no retry
        # loop and no shell anywhere in the module.
        source = (
            Path(__file__).resolve().parents[1]
            / "scarcity_router"
            / "providers"
            / "openai_codex_acquisition.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        self.assertNotIn("while True:\n            _send", source)


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
