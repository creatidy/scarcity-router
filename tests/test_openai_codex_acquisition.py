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
import platform
import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast, override
from unittest import mock

from scarcity_router import CapacitySnapshot, CapacityValidationError
from scarcity_router.providers import openai_codex_acquisition as acq
from scarcity_router.providers.openai_codex import (
    classify_app_server_message,
    parse_codex_rate_limits_result,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "openai-codex-appserver"
RETRIEVED_AT = "2026-09-03T20:00:00.000Z"
INVALID_RETRIEVED_AT = "2026-09-03T20:00:00Z"  # missing milliseconds

# Conspicuous synthetic-only secrets; never realistic production shapes.
SECRET = "TEST_ONLY_OPENAI_SECRET_NEVER_REAL"
ALL_FAKE_SECRETS: tuple[str, ...] = (SECRET,)

INIT_RESPONSE = b'{"id":1,"result":{"userAgent":"x","codexHome":"synthetic-home","platformFamily":"unix","platformOs":"linux"}}\n'


def _fixture_result(name: str) -> object:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return cast("object", json.load(handle))


def _read_response(result: object) -> bytes:
    return json.dumps({"id": 2, "result": result}).encode() + b"\n"


class _FakeStdin:
    """Records every write; close() mirrors a real pipe closing."""

    writes: list[bytes]
    closed: bool
    on_write: Callable[[bytes], None] | None

    def __init__(self, on_write: Callable[[bytes], None] | None = None) -> None:
        self.writes = []
        self.closed = False
        self.on_write = on_write

    def write(self, data: bytes) -> int:
        if self.closed:
            raise ValueError("write to closed file")
        self.writes.append(bytes(data))
        if self.on_write is not None:
            self.on_write(bytes(data))
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
    _retained_write_fd: int | None
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
        retain_write_fd: bool = False,
    ) -> None:
        self.stdin = _FakeStdin()
        self.events = []
        self._lines = tuple(lines)
        self._block = block
        self._stubborn = stubborn
        self._exit_code = exit_code
        self._read_fd, self._write_fd = os.pipe()
        self._retained_write_fd = (
            os.dup(self._write_fd) if retain_write_fd else None
        )
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
        except (OSError, ValueError):
            pass
        if self._retained_write_fd is not None:
            try:
                os.close(self._retained_write_fd)
            except OSError:
                pass
            self._retained_write_fd = None

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


class _ReactiveAppServer(_FakeAppServer):
    """Synthetic server that validates each client frame before responding."""

    _request_index: int
    protocol_error: str | None
    responses: list[bytes]
    _result: object
    stdin: _FakeStdin

    def __init__(self, result: object) -> None:
        super().__init__(block=True)
        self._request_index = 0
        self.protocol_error = None
        self.responses = []
        self._result = result
        self.stdin = _FakeStdin(self._on_write)

    def _on_write(self, data: bytes) -> None:
        try:
            message = cast("dict[str, object]", json.loads(data.decode()))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.protocol_error = "invalid_json"
            return
        expected: tuple[dict[str, object], ...] = (
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "scarcity-router",
                        "title": "Scarcity Router",
                        "version": "0.0.0",
                    },
                    "capabilities": {},
                },
            },
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read"},
        )
        if (
            self._request_index >= len(expected)
            or message != expected[self._request_index]
        ):
            self.protocol_error = "invalid_frame"
            return
        self._request_index += 1
        if self._request_index == 1:
            self._send(INIT_RESPONSE)
        elif self._request_index == 3:
            self._send(_read_response(self._result))

    @property
    def request_count(self) -> int:
        return self._request_index

    def receive(self, data: bytes) -> None:
        self._on_write(data)

    def _send(self, data: bytes) -> None:
        self.responses.append(data)
        try:
            _ = os.write(self._write_fd, data)
        except OSError:
            self.protocol_error = "response_write_failed"


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
        retain_write_fd: bool = False,
    ) -> _FakeAppServer:
        fake = _FakeAppServer(
            lines,
            block=block,
            stubborn=stubborn,
            retain_write_fd=retain_write_fd,
        )
        self.addCleanup(fake.finalize)
        self.fake = fake

        def fake_spawn(
            argv: Sequence[str], *, executable_fd: int | None = None
        ) -> "subprocess.Popen[bytes]":
            self.spawn_calls.append(list(argv))
            _ = executable_fd
            if error is not None:
                raise error
            return cast("subprocess.Popen[bytes]", cast("object", fake))

        patcher = mock.patch.object(acq, "spawn_app_server", fake_spawn)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _install_reactive(self, result: object) -> _ReactiveAppServer:
        fake = _ReactiveAppServer(result)
        self.addCleanup(fake.finalize)
        self.fake = fake

        def fake_spawn(
            argv: Sequence[str], *, executable_fd: int | None = None
        ) -> "subprocess.Popen[bytes]":
            self.spawn_calls.append(list(argv))
            _ = executable_fd
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
            "ratelimits-full-shape-ok.json",
            "ratelimits-credits-present.json",
            "ratelimits-spend-control-exhausted.json",
            "ratelimits-credits-malformed.json",
            "ratelimits-additional-window-present.json",
            "ratelimits-additional-bucket-exhausted.json",
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
        self.assertEqual(initialized["method"], "initialized")
        self.assertNotIn("id", initialized)
        self.assertNotIn("params", initialized)
        read = messages[2]
        self.assertEqual(read["method"], "account/rateLimits/read")
        self.assertEqual(read["id"], 2)
        self.assertNotIn("params", read)

    def test_reactive_server_validates_exact_generated_frames(self) -> None:
        fake = self._install_reactive(_fixture_result("ratelimits-ok-plus.json"))
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(fake.request_count, 3)
        self.assertIsNone(fake.protocol_error)
        self.assertEqual(len(fake.responses), 2)

    def test_reactive_server_rejects_old_frame_before_responding(self) -> None:
        fake = _ReactiveAppServer(_fixture_result("ratelimits-ok-plus.json"))
        self.addCleanup(fake.finalize)
        fake.receive(
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        )
        self.assertEqual(fake.protocol_error, "invalid_frame")
        self.assertEqual(fake.responses, [])

    def test_blank_separator_lines_tolerated(self) -> None:
        _ = self._install_fake(
            [b"\n", INIT_RESPONSE, b"   \n", _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(len(snapshot.windows), 2)

    def test_missing_required_used_percent_maps_to_schema_changed(self) -> None:
        payload = cast("dict[str, object]", _fixture_result("ratelimits-ok-plus.json"))
        rate_limits = cast("dict[str, object]", payload["rateLimits"])
        primary = cast("dict[str, object]", rate_limits["primary"])
        _ = primary.pop("usedPercent")
        _ = self._install_fake([INIT_RESPONSE, _read_response(payload)])
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertEqual(snapshot.windows, ())

    def test_inherited_stdout_writer_does_not_leak_reader(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, _read_response(_fixture_result("ratelimits-ok-plus.json"))],
            retain_write_fd=True,
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "ok")
        self.assertFalse(
            any(
                thread.name == "codex-app-server-stdout"
                for thread in threading.enumerate()
            )
        )

    def test_unproven_reader_cleanup_degrades_collection(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        roots = self._make_installation()
        with mock.patch.object(acq.BoundedLineReader, "stopped", return_value=False):
            snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual(snapshot.windows, ())

    def test_high_fd_reader_uses_safe_polling(self) -> None:
        read_fd, write_fd = os.pipe()
        held = [read_fd]
        while held[-1] < 1024:
            held.append(os.dup(read_fd))
        high_fd = held[-1]
        stream = os.fdopen(high_fd, "rb", closefd=True)
        try:
            _ = os.write(write_fd, b"{}\n")
            os.close(write_fd)
            reader = acq.BoundedLineReader(
                stream,
                max_line_bytes=acq.MAX_LINE_BYTES,
                max_total_bytes=acq.MAX_TOTAL_BYTES,
            )
            reader.start()
            kind, chunk = reader.get(1.0)
            self.assertEqual((kind, chunk), ("line", b"{}\n"))
            reader.close()
            reader.join(1.0)
            self.assertTrue(reader.stopped())
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            for fd in held[:-1]:
                try:
                    os.close(fd)
                except OSError:
                    pass


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

    def test_string_id_response_is_valid_but_never_matches_numeric_request(self) -> None:
        _ = self._install_fake(
            [
                INIT_RESPONSE,
                b'{"id":"other","result":{"ignored":true}}\n',
                _read_response(_fixture_result("ratelimits-ok-plus.json")),
            ]
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "ok")

    def test_string_and_signed_i64_request_ids_are_structurally_valid(self) -> None:
        self.assertEqual(
            classify_app_server_message({"id": "string-id", "result": {}}),
            "response",
        )
        self.assertEqual(
            classify_app_server_message(
                {"id": "string-id", "method": "server/request", "params": {}}
            ),
            "request",
        )
        values: tuple[int, ...] = (-2**63, 2**63 - 1)
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(
                    classify_app_server_message({"id": value, "result": {}}),
                    "response",
                )

    def test_integer_request_ids_outside_i64_are_invalid(self) -> None:
        values: tuple[int, ...] = (-(2**63) - 1, 2**63)
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(
                    classify_app_server_message({"id": value, "result": {}}),
                    "invalid",
                )

    def test_hybrid_method_response_message_rejected_as_drift(self) -> None:
        # A message carrying a `method` key — whatever its value type —
        # together with `result`/`error` is neither a well-formed request
        # nor a response: it must fail closed instead of being silently
        # ignored ahead of the real matching response.
        for hybrid in (
            b'{"id":2,"method":"account/rateLimits/updated","result":{"x":1}}\n',
            b'{"id":2,"method":null,"result":{"x":1}}\n',
            b'{"id":2,"method":42,"result":{"x":1}}\n',
            b'{"id":2,"method":true,"error":{"code":-1}}\n',
            b'{"method":"x","result":{"x":1}}\n',
        ):
            with self.subTest(hybrid=hybrid[:40]):
                _ = self._install_fake(
                    [INIT_RESPONSE, hybrid, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
                )
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")
                self.assertEqual(
                    [d.code for d in snapshot.diagnostics], ["schema_changed"]
                )

    def test_notification_with_malformed_id_rejected_as_drift(self) -> None:
        for bad in (
            b'{"method":"x","id":true,"params":{}}\n',
            b'{"method":"x","id":null,"params":{}}\n',
        ):
            with self.subTest(bad=bad[:40]):
                _ = self._install_fake(
                    [INIT_RESPONSE, bad, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
                )
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")

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

    def test_initialize_response_requires_tagged_fields(self) -> None:
        for result in (
            {},
            {
                "userAgent": "x",
                "codexHome": "synthetic-home",
                "platformFamily": "unix",
            },
            {
                "userAgent": "x",
                "codexHome": "synthetic-home",
                "platformFamily": "unix",
                "platformOs": 7,
            },
        ):
            with self.subTest(result=result):
                init = json.dumps({"id": 1, "result": result}).encode() + b"\n"
                _ = self._install_fake([init])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")
                self.assertEqual(snapshot.windows, ())

    def test_null_error_field_is_malformed_protocol(self) -> None:
        _ = self._install_fake(
            [INIT_RESPONSE, b'{"id":2,"error":null}\n']
        )
        roots = self._make_installation()
        snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertEqual(snapshot.windows, ())

    def test_error_code_signed_i64_boundaries_are_valid(self) -> None:
        values: tuple[int, ...] = (-2**63, 2**63 - 1)
        for code in values:
            with self.subTest(code=code):
                error = json.dumps(
                    {"id": 1, "error": {"code": code, "message": "synthetic"}}
                ).encode() + b"\n"
                _ = self._install_fake([error])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "unknown")
                self.assertEqual(snapshot.windows, ())

    def test_error_code_outside_signed_i64_is_schema_drift(self) -> None:
        values: tuple[int, ...] = (-(2**63) - 1, 2**63)
        for code in values:
            with self.subTest(code=code):
                error = json.dumps(
                    {"id": 1, "error": {"code": code, "message": "synthetic"}}
                ).encode() + b"\n"
                _ = self._install_fake([error])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")
                self.assertEqual(snapshot.windows, ())

    def test_read_error_code_signed_i64_boundaries_are_valid(self) -> None:
        values: tuple[int, ...] = (-2**63, 2**63 - 1)
        for code in values:
            with self.subTest(code=code):
                error = json.dumps(
                    {"id": 2, "error": {"code": code, "message": "synthetic"}}
                ).encode() + b"\n"
                _ = self._install_fake([INIT_RESPONSE, error])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "unknown")
                self.assertEqual(snapshot.windows, ())

    def test_read_error_code_outside_signed_i64_is_schema_drift(self) -> None:
        values: tuple[int, ...] = (-(2**63) - 1, 2**63)
        for code in values:
            with self.subTest(code=code):
                error = json.dumps(
                    {"id": 2, "error": {"code": code, "message": "synthetic"}}
                ).encode() + b"\n"
                _ = self._install_fake([INIT_RESPONSE, error])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")
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

    def test_duplicate_object_keys_rejected_at_every_depth(self) -> None:
        # One deliberate interpretation per message: duplicate keys in the
        # message identity, the result envelope, or a nested window object
        # all fail closed as protocol drift.
        bad_lines: tuple[bytes, ...] = (
            b'{"id":1,"id":2,"result":{}}\n',
            b'{"id":2,"result":{"rateLimits":{}},"result":{}}\n',
            b'{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":1,'
            + b'"usedPercent":2,"windowDurationMins":300},"secondary":{'
            + b'"usedPercent":3,"windowDurationMins":10080}}}}\n',
        )
        for bad in bad_lines:
            with self.subTest(bad=bad[:40]):
                _ = self._install_fake([INIT_RESPONSE, bad])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")
                self.assertEqual(
                    [d.code for d in snapshot.diagnostics], ["schema_changed"]
                )

    def test_non_finite_exponent_values_rejected(self) -> None:
        # Valid JSON numeric syntax that parses to infinity must fail closed
        # exactly like the literal NaN/Infinity constants.
        bad_lines: tuple[bytes, ...] = (
            b'{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":1e10000,'
            + b'"windowDurationMins":300},"secondary":{"usedPercent":3,'
            + b'"windowDurationMins":10080}}}}\n',
            b'{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":6,'
            + b'"windowDurationMins":1e9999},"secondary":{"usedPercent":3,'
            + b'"windowDurationMins":10080}}}}\n',
            b'{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":6,'
            + b'"windowDurationMins":300},"secondary":{"usedPercent":-1e10000,'
            + b'"windowDurationMins":10080}}}}\n',
        )
        for bad in bad_lines:
            with self.subTest(bad=bad[:40]):
                _ = self._install_fake([INIT_RESPONSE, bad])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")
                self.assertEqual(
                    [d.code for d in snapshot.diagnostics], ["schema_changed"]
                )

    def test_deeply_nested_payloads_rejected_as_drift(self) -> None:
        # Adversarial nesting within the line budget must surface as a safe
        # schema_changed status, never as an uncaught RecursionError.
        bad_lines: tuple[bytes, ...] = (
            b'{"id":2,"result":{"rateLimits":' + b'{"a":' * 5000 + b"1" + b"}" * 5000 + b"}}\n",
            b'{"id":2,"result":' + b"[" * 10_000 + b"]" * 10_000 + b"}\n",
        )
        for bad in bad_lines:
            with self.subTest(bad=bad[:24]):
                self.assertLessEqual(len(bad), acq.MAX_LINE_BYTES + 1)
                _ = self._install_fake([INIT_RESPONSE, bad])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")
                self.assertEqual(
                    [d.code for d in snapshot.diagnostics], ["schema_changed"]
                )

    def test_non_standard_json_constants_rejected(self) -> None:
        bad_lines: tuple[bytes, ...] = (
            b'{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":NaN,'
            + b'"windowDurationMins":300},"secondary":{"usedPercent":3,'
            + b'"windowDurationMins":10080}}}}\n',
            b'{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":6,'
            + b'"windowDurationMins":Infinity},"secondary":{"usedPercent":3,'
            + b'"windowDurationMins":10080}}}}\n',
            b'{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":-Infinity,'
            + b'"windowDurationMins":300},"secondary":{"usedPercent":3,'
            + b'"windowDurationMins":10080}}}}\n',
        )
        for bad in bad_lines:
            with self.subTest(bad=bad[:40]):
                _ = self._install_fake([INIT_RESPONSE, bad])
                roots = self._make_installation()
                snapshot = self._collect(discovery_roots=[roots])
                self.assertEqual(snapshot.status, "schema_changed")
                self.assertEqual(
                    [d.code for d in snapshot.diagnostics], ["schema_changed"]
                )

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

    def test_reader_startup_failure_terminates_child(self) -> None:
        # Regression: a reader-start failure before the session begins must
        # still attempt termination and return a safe snapshot — never claim
        # a successful collection when reaping cannot be proven.
        fake = self._install_fake(
            [
                INIT_RESPONSE,
                _read_response(_fixture_result("ratelimits-ok-plus.json")),
            ]
        )
        roots = self._make_installation()
        with mock.patch.object(
            acq.BoundedLineReader,
            "start",
            side_effect=RuntimeError("cannot start new thread"),
        ):
            snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual(
            [d.code for d in snapshot.diagnostics], ["source_unavailable"]
        )
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(fake.events, ["terminate"])
        self.assertTrue(fake.stdin.closed)
        self.assertEqual(fake.written_messages(), [])  # no session ran
        self._assert_no_output()

    def test_reader_startup_failure_kills_stubborn_child(self) -> None:
        fake = self._install_fake(
            [
                INIT_RESPONSE,
                _read_response(_fixture_result("ratelimits-ok-plus.json")),
            ],
            stubborn=True,
        )
        roots = self._make_installation()
        with mock.patch.object(
            acq.BoundedLineReader,
            "start",
            side_effect=RuntimeError("cannot start new thread"),
        ):
            snapshot = self._collect(discovery_roots=[roots])
        self.assertEqual(snapshot.status, "unavailable")
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

        def fake_spawn(
            argv: Sequence[str], *, executable_fd: int | None = None
        ) -> "subprocess.Popen[bytes]":
            _ = argv
            _ = executable_fd
            return cast("subprocess.Popen[bytes]", cast("object", fake))

        with mock.patch.object(acq, "spawn_app_server", fake_spawn):
            with mock.patch.object(
                acq,
                "_run_session",
                return_value=parse_codex_rate_limits_result(
                    _fixture_result("ratelimits-ok-plus.json"),
                    retrieved_at=RETRIEVED_AT,
                ),
            ):
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

    def test_deeply_nested_package_maps_to_unsupported(self) -> None:
        # Regression: an adversarially nested codex-package.json below the
        # byte limit must make the candidate unusable (unsupported), never
        # crash discovery with an uncaught RecursionError.
        platform_dir = acq.platform_directory()
        assert platform_dir is not None
        root = self.tmp / "extensions"
        extension = root / "openai.chatgpt-26.825.51511-linux-x64"
        binary_dir = extension / "bin" / platform_dir
        _ = binary_dir.mkdir(parents=True, exist_ok=True)
        binary = binary_dir / "codex"
        _ = binary.write_bytes(b"#!/bin/sh\nexit 0\n")
        _ = binary.chmod(0o755)
        package = (
            '{"layoutVersion":1,"variant":"codex","version":"0.1","x":'
            + "[" * 10_000 + "]" * 10_000 + "}"
        )
        self.assertLess(len(package), acq.MAX_PACKAGE_BYTES)
        _ = (binary_dir / "codex-package.json").write_text(package, encoding="utf-8")

        installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    def test_non_finite_package_numbers_map_to_unsupported(self) -> None:
        platform_dir = acq.platform_directory()
        assert platform_dir is not None
        root = self.tmp / "extensions"
        extension = root / "openai.chatgpt-26.825.51511-linux-x64"
        binary_dir = extension / "bin" / platform_dir
        _ = binary_dir.mkdir(parents=True, exist_ok=True)
        binary = binary_dir / "codex"
        _ = binary.write_bytes(b"#!/bin/sh\nexit 0\n")
        _ = binary.chmod(0o755)
        for package in (
            '{"layoutVersion":1,"variant":"codex","version":"0.1","x":NaN}',
            '{"layoutVersion":1,"variant":"codex","version":"0.1","x":1e10000}',
        ):
            with self.subTest(package=package[-12:]):
                _ = (binary_dir / "codex-package.json").write_text(
                    package, encoding="utf-8"
                )
                installation, outcome = acq.discover_codex_installation([root])
                self.assertIsNone(installation)
                self.assertEqual(outcome, "unsupported_installation")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "platform lacks FIFOs")
    def test_fifo_package_maps_to_unsupported_without_blocking(self) -> None:
        # Regression: a FIFO (or socket) at the package path must never
        # block discovery — the file is opened no-follow and the descriptor
        # is verified to be a regular file before reading.
        platform_dir = acq.platform_directory()
        assert platform_dir is not None
        root = self.tmp / "extensions"
        extension = root / "openai.chatgpt-26.825.51511-linux-x64"
        binary_dir = extension / "bin" / platform_dir
        _ = binary_dir.mkdir(parents=True, exist_ok=True)
        binary = binary_dir / "codex"
        _ = binary.write_bytes(b"#!/bin/sh\nexit 0\n")
        _ = binary.chmod(0o755)
        os.mkfifo(str(binary_dir / "codex-package.json"))

        installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    @unittest.skipUnless(hasattr(os, "symlink"), "platform lacks symlinks")
    def test_symlinked_binary_maps_to_unsupported(self) -> None:
        platform_dir = acq.platform_directory()
        assert platform_dir is not None
        root = self.tmp / "extensions"
        extension = root / "openai.chatgpt-26.825.51511-linux-x64"
        binary_dir = extension / "bin" / platform_dir
        _ = binary_dir.mkdir(parents=True, exist_ok=True)
        target = self.tmp / "real-codex"
        _ = target.write_bytes(b"#!/bin/sh\nexit 0\n")
        _ = target.chmod(0o755)
        (binary_dir / "codex").symlink_to(target)
        _ = (binary_dir / "codex-package.json").write_text(
            json.dumps(
                {
                    "layoutVersion": 1,
                    "variant": "codex",
                    "version": "0.151.0-alpha.7.2",
                }
            ),
            encoding="utf-8",
        )

        installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    def test_execution_remains_bound_to_validated_binary_inode(self) -> None:
        root = self._make(suffix="validated")
        installation, outcome = acq.discover_codex_installation([root])
        self.assertEqual(outcome, "found")
        assert installation is not None
        if not sys.platform.startswith("linux"):
            installation.close()
            self.skipTest("platform lacks descriptor execution path")
        escaped = self.tmp / "escaped-codex"
        _ = escaped.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        _ = escaped.chmod(0o755)
        installation.binary.unlink()
        installation.binary.symlink_to(escaped)
        proc = acq.spawn_app_server(
            [str(installation.binary), "app-server"],
            executable_fd=installation.binary_fd,
        )
        installation.close()
        try:
            self.assertEqual(proc.wait(timeout=2.0), 0)
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            if proc.stdout is not None:
                proc.stdout.close()

    def test_symlinked_candidate_directory_cannot_escape_root(self) -> None:
        outside = self._make(suffix="outside")
        root = self.tmp / "configured-extensions"
        _ = root.mkdir()
        outside_candidate = outside / "openai.chatgpt-26.825.51511-linux-x64"
        (root / outside_candidate.name).symlink_to(
            outside_candidate, target_is_directory=True
        )

        installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    def test_candidate_replacement_between_listing_and_open_is_rejected(self) -> None:
        root = self._make(suffix="candidate-race")
        outside = self._make(suffix="candidate-race-outside")
        name = "openai.chatgpt-26.825.51511-linux-x64"
        candidate = root / name
        outside_candidate = outside / name
        backup = self.tmp / "candidate-race-backup"
        real_open = cast(
            Callable[[int, str], int | None], getattr(acq, "_open_directory_at")
        )

        def replace_before_open(parent_fd: int, entry_name: str) -> int | None:
            if entry_name == name and candidate.exists():
                _ = candidate.rename(backup)
                candidate.symlink_to(outside_candidate, target_is_directory=True)
            return real_open(parent_fd, entry_name)

        with mock.patch.object(
            acq, "_open_directory_at", side_effect=replace_before_open
        ):
            installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    def test_bin_replacement_after_candidate_open_is_rejected(self) -> None:
        root = self._make(suffix="bin-race")
        outside = self._make(suffix="bin-race-outside")
        name = "openai.chatgpt-26.825.51511-linux-x64"
        candidate = root / name
        outside_candidate = outside / name
        bin_path = candidate / "bin"
        backup = self.tmp / "bin-race-backup"
        real_open = cast(
            Callable[[int, str], int | None], getattr(acq, "_open_directory_at")
        )

        def replace_bin(parent_fd: int, entry_name: str) -> int | None:
            if entry_name == "bin" and bin_path.exists():
                _ = bin_path.rename(backup)
                bin_path.symlink_to(
                    outside_candidate / "bin", target_is_directory=True
                )
            return real_open(parent_fd, entry_name)

        with mock.patch.object(acq, "_open_directory_at", side_effect=replace_bin):
            installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    def test_symlinked_intermediate_directory_cannot_escape_root(self) -> None:
        outside = self._make(suffix="outside")
        root = self.tmp / "configured-extensions"
        extension = root / "openai.chatgpt-26.825.51511-linux-x64"
        _ = extension.mkdir(parents=True)
        outside_candidate = outside / "openai.chatgpt-26.825.51511-linux-x64"
        (extension / "bin").symlink_to(
            outside_candidate / "bin", target_is_directory=True
        )

        installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    def test_darwin_descriptor_execution_is_unsupported(self) -> None:
        root = self._make(suffix="darwin")
        with mock.patch.object(platform, "system", return_value="Darwin"):
            with mock.patch.object(platform, "machine", return_value="arm64"):
                installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    def test_linux_arm64_discovery_is_unsupported_without_evidence(self) -> None:
        root = self._make(suffix="arm64")
        with mock.patch.object(platform, "system", return_value="Linux"):
            with mock.patch.object(platform, "machine", return_value="aarch64"):
                installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

    def test_unicode_version_directory_is_skipped_deterministically(self) -> None:
        # Regression: Unicode digit-lookalikes (superscript two) must not
        # raise ValueError in version parsing; the malformed candidate is
        # skipped and, when nothing usable remains, reported unsupported.
        platform_dir = acq.platform_directory()
        assert platform_dir is not None
        root = self.tmp / "extensions"

        def make(name: str) -> None:
            extension = root / name
            binary_dir = extension / "bin" / platform_dir
            _ = binary_dir.mkdir(parents=True, exist_ok=True)
            binary = binary_dir / "codex"
            _ = binary.write_bytes(b"#!/bin/sh\nexit 0\n")
            _ = binary.chmod(0o755)
            _ = (binary_dir / "codex-package.json").write_text(
                json.dumps(
                    {
                        "layoutVersion": 1,
                        "variant": "codex",
                        "version": "0.151.0-alpha.7.2",
                    }
                ),
                encoding="utf-8",
            )

        make("openai.chatgpt-26.825.51511\u00b2-linux-x64")
        installation, outcome = acq.discover_codex_installation([root])
        self.assertIsNone(installation)
        self.assertEqual(outcome, "unsupported_installation")

        make("openai.chatgpt-26.825.51511-linux-x64")
        installation, outcome = acq.discover_codex_installation([root])
        self.assertEqual(outcome, "found")
        assert installation is not None
        self.assertEqual(installation.extension_version, "26.825.51511")

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

    def test_deeply_nested_package_maps_collect_to_unsupported(self) -> None:
        # The nested package makes the only installation unusable, so the
        # collector reports unsupported without ever spawning a process.
        platform_dir = acq.platform_directory()
        assert platform_dir is not None
        root = self.tmp / "extensions"
        extension = root / "openai.chatgpt-26.825.51511-linux-x64"
        binary_dir = extension / "bin" / platform_dir
        _ = binary_dir.mkdir(parents=True, exist_ok=True)
        binary = binary_dir / "codex"
        _ = binary.write_bytes(b"#!/bin/sh\nexit 0\n")
        _ = binary.chmod(0o755)
        package = (
            '{"layoutVersion":1,"variant":"codex","version":"0.1","x":'
            + "[" * 10_000 + "]" * 10_000 + "}"
        )
        _ = (binary_dir / "codex-package.json").write_text(package, encoding="utf-8")

        _ = self._install_fake(
            [INIT_RESPONSE, _read_response(_fixture_result("ratelimits-ok-plus.json"))]
        )
        snapshot = self._collect(discovery_roots=[root])
        self.assertEqual(snapshot.status, "unsupported")
        self.assertEqual([d.code for d in snapshot.diagnostics], ["unsupported_source"])
        self.assertEqual(self.spawn_calls, [])  # never spawned
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


class InvalidTimeouts(_AcquisitionCase):
    """Non-finite or non-positive timeouts are rejected before any spawn."""

    def test_invalid_timeouts_raise_before_spawning(self) -> None:
        for bad in (
            float("nan"),
            float("inf"),
            float("-inf"),
            1e308,
            10**1000,
            0.0,
            -1.0,
            True,
        ):
            for parameter in ("startup_timeout", "session_timeout"):
                with self.subTest(bad=bad, parameter=parameter):
                    fake = self._install_fake(
                        [
                            INIT_RESPONSE,
                            _read_response(
                                _fixture_result("ratelimits-ok-plus.json")
                            ),
                        ],
                        block=True,
                    )
                    roots = self._make_installation()
                    with self.assertRaises(ValueError):
                        if parameter == "startup_timeout":
                            _ = self._collect(
                                discovery_roots=[roots],
                                startup_timeout=cast(float, bad),
                            )
                        else:
                            _ = self._collect(
                                discovery_roots=[roots],
                                session_timeout=cast(float, bad),
                            )
                    # Nothing was ever spawned, so no child can leak and no
                    # deadline arithmetic ran with an unusable bound.
                    self.assertEqual(self.spawn_calls, [])
                    self.assertEqual(fake.events, [])

    def test_valid_timeouts_still_work(self) -> None:
        _ = self._install_fake(
            [
                INIT_RESPONSE,
                _read_response(_fixture_result("ratelimits-ok-plus.json")),
            ]
        )
        roots = self._make_installation()
        snapshot = self._collect(
            discovery_roots=[roots], startup_timeout=5.0, session_timeout=5.0
        )
        self.assertEqual(snapshot.status, "ok")


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
