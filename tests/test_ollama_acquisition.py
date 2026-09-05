"""Security, integration and contract tests for the Ollama acquisition layer.

The transport seam (``open_connection``) is replaced with fake connection
factories dispatching per request path; no test in this module contacts a
runtime, a network or the filesystem beyond the synthetic fixtures. The
fakes honor the transport contract the real ``HTTPConnection`` provides:
the registered raw socket's ``shutdown``/``close`` operations unblock
in-flight operations, which is what makes the bounded worker reclaimable at
the deadline.

Every failure class asserts that conspicuous synthetic markers (fake
secrets, fake paths, the endpoint URL, digests, provider-controlled
exception text) appear in no serialized snapshot, repr, diagnostic or
captured stdout/stderr output, and that adversarial conditions
(permanent blocks, delayed EOF, invalid response objects, non-bytes
chunks, duplicate keys, non-finite values, deep nesting, huge integers)
are reclaimed or fail closed.

Contract tests assert the normalized snapshot/local-runtime shape, context
independence and deterministic normalization per docs/capacity-model.md.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import socket
import threading
import time
import unittest
from collections.abc import Callable, Mapping
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import cast, override
from unittest import mock

from scarcity_router import CapacitySnapshot, CapacityValidationError, LocalRuntime
from scarcity_router.providers import ollama_acquisition
from scarcity_router.providers.ollama import PROVIDER, SOURCE

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ollama-local"
RETRIEVED_AT = "2026-09-04T21:00:00.000Z"
MODEL = "test-model:latest"
OTHER_MODEL = "other-model:1b"
ENDPOINT = ollama_acquisition.DEFAULT_ENDPOINT
BASE = ENDPOINT.rstrip("/")
DIGEST_ZERO = "sha256:" + "0" * 64
DIGEST_ONE = "sha256:" + "1" * 64
HUGE_DIGITS = str(10**500).encode()

# Conspicuous synthetic-only markers; never realistic production shapes.
FAKE_SECRET = "TEST_ONLY_FAKE_OLLAMA_SECRET_NEVER_REAL"
FAKE_PATH = "/home/test/.fake-models/TEST_ONLY_PATH"
FAKE_RAW_FRAGMENT = "TEST_ONLY_RAW_RESPONSE_FRAGMENT"
FAKE_REDIRECT_TARGET = "TEST_ONLY_REDIRECT_TARGET"
FAKE_TRANSPORT_SECRET = "TEST_ONLY_SECRET_FROM_RESPONSE"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _http_response(
    body: bytes,
    *,
    content_length: int | None = None,
    chunked: bool = False,
) -> bytes:
    """Build one synthetic HTTP response with explicit body framing."""
    if chunked:
        framing = (
            f"{len(body):x}\r\n".encode()
            + body
            + b"\r\n0\r\n\r\n"
        )
        return (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
            + b"Connection: close\r\n\r\n"
            + framing
        )
    declared = len(body) if content_length is None else content_length
    return (
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(declared).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )


def _serialized(snapshot: CapacitySnapshot) -> str:
    return json.dumps(snapshot.to_dict(), sort_keys=True) + repr(snapshot)


class _FakeHTTPResponse:
    """Fake HTTP response: integer status + chunked bounded body.

    Honors ``read`` bounds and returns ``b""`` at EOF, like the real
    ``HTTPResponse``. Optional fault injection: a read error, a first-read
    ``str`` chunk (contract violation), a read guard that records any read
    of a body that must never be read, and a trickling mode.
    """

    status: int
    _data: bytes
    _offset: int
    _read_error: Exception | None
    _str_chunk_once: bool
    _chunk_delay: float | None
    _guard_read: bool
    _headers: Mapping[str, object]
    guard_tripped: bool
    closed: bool

    def __init__(
        self,
        status: int = 200,
        data: bytes = b"",
        read_error: Exception | None = None,
        str_chunk_once: bool = False,
        chunk_delay: float | None = None,
        guard_read: bool = False,
        headers: Mapping[str, object] | None = None,
    ) -> None:
        self.status = status
        self._data = data
        self._offset = 0
        self._read_error = read_error
        self._str_chunk_once = str_chunk_once
        self._chunk_delay = chunk_delay
        self.guard_tripped = False
        self._guard_read = guard_read
        self._headers = {} if headers is None else headers
        self.closed = False

    def read(self, size: int = -1) -> object:
        if self._guard_read:
            self.guard_tripped = True
            raise AssertionError("response body must never be read")
        if self._read_error is not None:
            raise self._read_error
        if self._str_chunk_once:
            self._str_chunk_once = False
            return "TEST_ONLY_NON_BYTES_CHUNK"
        if self._chunk_delay is not None:
            time.sleep(self._chunk_delay)
        if size < 0:
            size = self._data.__len__() - self._offset
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def getheader(self, name: str, default: object = None) -> object:
        return self._headers.get(name, default)


class _TricklingBody:
    """A 200 response whose body trickles small chunks forever."""

    status: int = 200
    _delay: float
    closed: bool

    def __init__(self, delay: float = 0.02) -> None:
        self._delay = delay
        self.closed = False

    def read(self, size: int = -1) -> object:
        _ = size
        time.sleep(self._delay)
        return b"x" * 256

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    """Fake ``HTTPConnection``: one fixed response outcome per path."""

    _runtime: "_FakeRuntime"
    requests: list[tuple[str, str, object]]
    closed: bool
    close_count: int

    def __init__(self, runtime: "_FakeRuntime") -> None:
        self._runtime = runtime
        self.requests = []
        self.closed = False
        self.close_count = 0

    def request(
        self, method: str, path: str, /, *, headers: object = None
    ) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> object:
        path = self.requests[-1][1]
        outcome = self._runtime.outcome_for(path)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True
        self.close_count += 1


class _FakeRuntime:
    """Fake connection factory: one fixed outcome per request path."""

    _outcomes: dict[str, object]
    connections: list[_FakeConnection]
    opened: list[tuple[str, int, float]]

    def __init__(self, outcomes: dict[str, object]) -> None:
        self._outcomes = outcomes
        self.connections = []
        self.opened = []

    def __call__(
        self,
        host: str,
        port: int,
        timeout: float,
        register_handle: Callable[[object], None],
    ) -> _FakeConnection:
        self.opened.append((host, port, timeout))
        connection = _FakeConnection(self)
        self.connections.append(connection)
        sock: object | None = getattr(connection, "sock", None)
        release: object | None = (
            getattr(sock, "_release", None) if sock is not None else None
        )
        if type(release) is threading.Event:
            register_handle(release)
        elif type(sock) is socket.socket:
            register_handle(sock)
        return connection

    def outcome_for(self, path: str) -> object:
        if path not in self._outcomes:
            raise AssertionError(f"unexpected request path: {path!r}")
        outcome = self._outcomes[path]
        if isinstance(outcome, bytes):
            return _FakeHTTPResponse(200, outcome)
        if isinstance(outcome, int):
            return _FakeHTTPResponse(outcome)
        return outcome

    def requested_paths(self) -> list[str]:
        return [
            path
            for connection in self.connections
            for _method, path, _headers in connection.requests
        ]


class _FakeSocket:
    """Raw-socket state stand-in paired with an explicit release event.

    The production seam registers the raw socket itself; test factories
    register this object's event so no foreign methods are invoked.
    """

    _release: threading.Event
    shutdown_count: int
    closed: bool

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.shutdown_count = 0
        self.closed = False

    def shutdown(self, how: int) -> None:
        _ = how
        self.shutdown_count += 1
        _ = self._release.set()

    def close(self) -> None:
        self.closed = True
        _ = self._release.set()


class _RecordingCloseConnection:
    """Connection serving healthy canned responses, recording closes."""

    requests: list[tuple[str, str, object]]
    sock: _FakeSocket
    _release: threading.Event
    status: int = 200
    closed: bool
    close_calls: int

    def __init__(self) -> None:
        self.requests = []
        self._release = threading.Event()
        self.sock = _FakeSocket(self._release)
        self.closed = False
        self.close_calls = 0

    def request(
        self, method: str, path: str, /, *, headers: object = None
    ) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> object:
        path = self.requests[-1][1]
        bodies = {
            "/api/version": _fixture("version-ok.json"),
            "/api/tags": _fixture("tags-present.json"),
            "/api/ps": _fixture("ps-loaded.json"),
        }
        return _FakeHTTPResponse(200, bodies[path])

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _healthy_runtime(
    *,
    tags: object = None,
    ps: object = None,
    version: object = None,
) -> _FakeRuntime:
    return _FakeRuntime(
        {
            "/api/version": (
                _fixture("version-ok.json") if version is None else version
            ),
            "/api/tags": _fixture("tags-present.json") if tags is None else tags,
            "/api/ps": _fixture("ps-loaded.json") if ps is None else ps,
        }
    )


class _AcquisitionCase(unittest.TestCase):
    """Shared harness: a patched transport seam and captured output."""

    runtime: _FakeRuntime | None = None
    stdout: io.StringIO = io.StringIO()
    stderr: io.StringIO = io.StringIO()
    _baseline_threads: int = 0

    @override
    def setUp(self) -> None:
        self.runtime = None
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self._baseline_threads = threading.active_count()

    def _install(self, runtime: _FakeRuntime) -> _FakeRuntime:
        self.runtime = runtime
        patcher = mock.patch.object(ollama_acquisition, "open_connection", runtime)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        return runtime

    def _collect(
        self,
        *,
        model_name: str = MODEL,
        endpoint: str = ENDPOINT,
        configured_context_tokens: int | None = None,
        retrieved_at: str = RETRIEVED_AT,
    ) -> CapacitySnapshot:
        with contextlib.redirect_stdout(
            self.stdout
        ), contextlib.redirect_stderr(self.stderr):
            return ollama_acquisition.collect_ollama_capacity(
                retrieved_at=retrieved_at,
                model_name=model_name,
                endpoint=endpoint,
                configured_context_tokens=configured_context_tokens,
            )

    def _assert_no_output(self) -> None:
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "")

    def _assert_safe_serialization(self, snapshot: CapacitySnapshot) -> None:
        text = _serialized(snapshot)
        for marker in (
            FAKE_SECRET,
            FAKE_PATH,
            FAKE_RAW_FRAGMENT,
            FAKE_TRANSPORT_SECRET,
            ENDPOINT,
            "http://",
            "Authorization",
            "sha256:",
        ):
            self.assertNotIn(marker, text)
        self._assert_no_output()

    def _assert_codes(
        self, snapshot: CapacitySnapshot, expected: list[str]
    ) -> None:
        self.assertEqual(
            [diagnostic.code for diagnostic in snapshot.diagnostics], expected
        )

    def _assert_no_collector_threads(self) -> None:
        # The collector's own worker must be reclaimed (poll briefly for
        # thread-system scheduling). Fixture listener hold-threads are
        # irrelevant here; global thread/fd stability is asserted in
        # RealTransportCancellation.tearDown once the fixtures are
        # released.
        wait_until = time.monotonic() + 2.0
        while (
            any(
                thread.name == "scarcity-router-ollama-read"
                for thread in threading.enumerate()
            )
            and time.monotonic() < wait_until
        ):
            time.sleep(0.01)
        self.assertFalse(
            any(
                thread.name == "scarcity-router-ollama-read"
                for thread in threading.enumerate()
            ),
            "collector worker must be reclaimed before returning",
        )


# ═══════════════════════ configuration boundary ══════════════════════════════


class ConnectionSetup(_AcquisitionCase):
    """Connection-setup failures normalize without leaking exception text."""

    BUDGET: float = 0.02
    DELAY: float = 0.05

    def _install_factory(
        self, make: Callable[[Callable[[object], None]], object]
    ) -> None:
        def factory(
            host: str,
            port: int,
            timeout: float,
            register_handle: Callable[[object], None],
        ) -> object:
            _ = host, port, timeout
            return make(register_handle)

        patcher = mock.patch.object(ollama_acquisition, "open_connection", factory)
        _ = patcher.start()
        self.addCleanup(patcher.stop)

    def _collect_bounded(self) -> tuple[CapacitySnapshot, float]:
        started = time.monotonic()
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", self.BUDGET
        ), contextlib.redirect_stdout(
            self.stdout
        ), contextlib.redirect_stderr(self.stderr):
            snapshot = ollama_acquisition.collect_ollama_capacity(
                retrieved_at=RETRIEVED_AT, model_name=MODEL
            )
        return snapshot, time.monotonic() - started

    def test_connect_refusal_with_secret_normalizes(self) -> None:
        # A refused connection (OSError with provider-controlled text from
        # a hostile transport) normalizes to unavailable; the text never
        # escapes into the snapshot, diagnostics or output.
        def make(register: Callable[[object], None]) -> object:
            _ = register  # the hostile transport fails before registering
            raise ConnectionRefusedError(FAKE_TRANSPORT_SECRET)

        self._install_factory(make)
        snapshot, elapsed = self._collect_bounded()
        self.assertEqual(snapshot.status, "unavailable")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self.assertLess(elapsed, 0.5)
        text = _serialized(snapshot)
        self.assertNotIn(FAKE_TRANSPORT_SECRET, text)
        self._assert_no_output()

    def _assert_bounded_connect_cancel(self, wait: float) -> None:
        sockets: list[_ConnectBlockingSocket] = []

        class _ConnectBlockingSocket:
            """Connection whose first transport operation blocks until
            shutdown cancels it (the real seam registers the raw socket
            before any blocking operation, so this models the earliest
            cancellable phase)."""

            requests: list[tuple[str, str, object]]
            _release: threading.Event
            shutdown_count: int
            closed: bool

            def __init__(self, release: threading.Event) -> None:
                self.requests = []
                self._release = release
                self.shutdown_count = 0
                self.closed = False

            def request(
                self, method: str, path: str, /, *, headers: object = None
            ) -> None:
                self.requests.append((method, path, headers))
                if self._release.wait(wait):
                    raise OSError("connection cancelled by collector")
                raise OSError("connect timed out")

            def shutdown(self, how: int) -> None:
                _ = how
                self.shutdown_count += 1
                _ = self._release.set()

            def close(self) -> None:
                self.closed = True

        def make(register: Callable[[object], None]) -> object:
            sock = _ConnectBlockingSocket(threading.Event())
            sockets.append(sock)
            register(cast("object", getattr(sock, "_release")))
            return sock

        self._install_factory(make)
        snapshot, elapsed = self._collect_bounded()
        self.assertEqual(snapshot.status, "unavailable")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self.assertLess(elapsed, self.BUDGET + 0.1)
        # The connect-phase test primitive was registered before the blocking
        # call and cancelled directly: the connect phase cannot outlive the
        # collection bound.
        self.assertTrue(cast("threading.Event", getattr(sockets[0], "_release")).is_set())
        self._assert_no_output()

    def test_delayed_connect_cancels_within_budget(self) -> None:
        self._assert_bounded_connect_cancel(self.DELAY)

    def test_permanent_connect_cancels_within_budget(self) -> None:
        self._assert_bounded_connect_cancel(30.0)


# ═══════════════════════ configuration boundary ══════════════════════════════


class EndpointPolicy(unittest.TestCase):
    def test_canonicalization_pins_default_and_explicit_ports(self) -> None:
        for url, canonical in (
            ("http://127.0.0.1", "http://127.0.0.1:11434"),
            ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
            ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
            ("http://127.0.0.1/", "http://127.0.0.1:11434"),
            ("http://[::1]", "http://[::1]:11434"),
            ("http://[::1]:9100", "http://[::1]:9100"),
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    ollama_acquisition.canonical_local_endpoint(url), canonical
                )

    def test_loopback_endpoints_are_approved(self) -> None:
        for url in ("http://127.0.0.1:11434", "http://[::1]:11434"):
            with self.subTest(url=url):
                self.assertTrue(ollama_acquisition.is_approved_local_endpoint(url))

    def test_localhost_and_names_are_rejected(self) -> None:
        # Name-based endpoints are rejected outright: the collector never
        # resolves names, so DNS/hosts-file/proxy escape paths do not exist.
        for url in (
            "http://localhost:11434",
            "http://localhost",
            "http://ollama.internal:11434",
            "http://example.com:11434",
        ):
            with self.subTest(url=url):
                self.assertFalse(ollama_acquisition.is_approved_local_endpoint(url))

    def test_arbitrary_external_and_lan_hosts_rejected(self) -> None:
        for url in (
            "http://ollama.example.org",
            "https://api.z.ai",
            "http://192.168.1.10:11434",
            "http://10.0.0.5:11434",
            "http://172.16.0.9:11434",
            "http://0.0.0.0:11434",
            "http://169.254.1.1:11434",
            "http://127.0.0.1.evil.test:11434",
            "http://localhost.example.com:11434",
            "http://127.0.0.10:11434",  # different host, not 127.0.0.1
            "http://0127.0.0.1:11434",
        ):
            with self.subTest(url=url):
                self.assertFalse(ollama_acquisition.is_approved_local_endpoint(url))

    def test_non_http_schemes_rejected(self) -> None:
        for url in (
            "https://127.0.0.1:11434",
            "ftp://127.0.0.1:11434",
            "file:///127.0.0.1:11434",
        ):
            with self.subTest(url=url):
                self.assertFalse(ollama_acquisition.is_approved_local_endpoint(url))

    def test_query_fragment_and_empty_delimiters_rejected(self) -> None:
        for url in (
            "http://127.0.0.1:11434?",
            "http://127.0.0.1:11434#",
            "http://127.0.0.1:11434?#",
            "http://127.0.0.1:11434?debug=1",
            "http://127.0.0.1:11434#x",
        ):
            with self.subTest(url=url):
                self.assertFalse(ollama_acquisition.is_approved_local_endpoint(url))

    def test_whitespace_control_and_leading_characters_rejected(self) -> None:
        for url in (
            " http://127.0.0.1:11434",
            "http://127.0.0.1:11434 ",
            "\thttp://127.0.0.1:11434",
            "http://127.0.0.1:11434\n",
            "http://127.0.0.1\x00:11434",
            "http://127.0.0.1:11434/api/ver sion",
        ):
            with self.subTest(url=url):
                self.assertFalse(ollama_acquisition.is_approved_local_endpoint(url))

    def test_userinfo_and_non_root_paths_rejected(self) -> None:
        for url in (
            "http://user@127.0.0.1:11434",
            "http://user:pass@127.0.0.1:11434",
            "http://127.0.0.1:11434/api/base/other",
        ):
            with self.subTest(url=url):
                self.assertFalse(ollama_acquisition.is_approved_local_endpoint(url))

    def test_malformed_urls_and_ports_fail_closed(self) -> None:
        for url in (
            "http://127.0.0.1:not-a-port",
            "http://127.0.0.1:",
            "http://[::1]:",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://[::1:11434",
            "not a url at all",
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(ollama_acquisition.is_approved_local_endpoint(url))

    def test_non_string_rejected(self) -> None:
        self.assertFalse(ollama_acquisition.is_approved_local_endpoint(11434))


class ConfigurationBoundary(_AcquisitionCase):
    def test_unapproved_endpoint_raises_before_any_io(self) -> None:
        runtime = self._install(
            _healthy_runtime()
        )  # any request would fail the test
        host_marker = "TEST_ONLY_EXTERNAL_HOST_EXAMPLE"
        with self.assertRaises(ValueError) as ctx:
            _ = self._collect(endpoint=f"http://{host_marker.lower()}:11434")
        self.assertNotIn(host_marker, str(ctx.exception))
        self.assertNotIn("http://", str(ctx.exception))
        self.assertEqual(runtime.opened, [])
        self._assert_no_output()

    def test_omitted_port_contacts_canonical_default(self) -> None:
        runtime = self._install(_healthy_runtime())
        _ = self._collect(endpoint="http://127.0.0.1")
        self.assertEqual(
            runtime.opened,
            [
                ("127.0.0.1", 11434, ollama_acquisition.TIMEOUT_SECONDS),
                ("127.0.0.1", 11434, ollama_acquisition.TIMEOUT_SECONDS),
                ("127.0.0.1", 11434, ollama_acquisition.TIMEOUT_SECONDS),
            ],
        )
        self.assertEqual(
            runtime.requested_paths(),
            ["/api/version", "/api/tags", "/api/ps"],
        )

    def test_unsafe_model_names_raise_before_any_io(self) -> None:
        runtime = self._install(_healthy_runtime())
        for bad in (
            "Test-Model:latest",  # uppercase
            "../etc/passwd",
            "/usr/local/bin/ollama",
            "has space:latest",
            "",
            "a" * 65,
            7,
            None,
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _ = self._collect(model_name=cast(str, bad))
                self.assertEqual(runtime.opened, [])
        self._assert_no_output()

    def test_trailing_newline_model_name_rejected(self) -> None:
        # re.match with ``$`` would accept a final newline; validation is
        # full-string, so this configuration must be refused before any I/O.
        runtime = self._install(_healthy_runtime())
        for bad in ("test-model:latest\n", "test-model:latest\r\n"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError) as ctx:
                    _ = self._collect(model_name=bad)
                self.assertNotIn("test-model", str(ctx.exception))
                self.assertEqual(runtime.opened, [])

    def test_control_characters_in_model_name_rejected(self) -> None:
        runtime = self._install(_healthy_runtime())
        for bad in ("\x01test-model:latest", "test-model:latest\x7f", "a\tb"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    _ = self._collect(model_name=bad)
                self.assertEqual(runtime.opened, [])

    def test_model_name_length_boundary(self) -> None:
        # A 64-character identifier is a valid v1 identity (the model is
        # then simply missing from the listing); 65 characters are refused
        # before any I/O.
        runtime = self._install(_healthy_runtime())
        snapshot = self._collect(model_name="a" * 64)
        self.assertEqual(runtime.requested_paths(), ["/api/version", "/api/tags"])
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(local.model_name, "a" * 64)
        self.assertEqual(local.model_presence, "missing")
        with self.assertRaises(ValueError):
            _ = self._collect(model_name="a" * 65)
        self.assertEqual(len(runtime.opened), 2)

    def test_invalid_configured_context_raises_before_any_io(self) -> None:
        runtime = self._install(_healthy_runtime())
        for bad in (0, -1, True, "16384", 16.5, [16384]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _ = self._collect(configured_context_tokens=cast(int, bad))
                self.assertEqual(runtime.opened, [])
        self._assert_no_output()

    def test_error_messages_never_echo_configuration_values(self) -> None:
        marker_model = "TEST_ONLY_UNSAFE_MODEL/../../path"
        marker_host = "TEST_ONLY_UNSAFE_HOST.example"
        with self.assertRaises(ValueError) as endpoint_error:
            _ = self._collect(endpoint=f"http://{marker_host.lower()}:1")
        with self.assertRaises(ValueError) as model_error:
            _ = self._collect(model_name=marker_model)
        combined = str(endpoint_error.exception) + str(model_error.exception)
        self.assertNotIn(marker_model, combined)
        self.assertNotIn(marker_host, combined)


# ═══════════════════════ strict JSON decoding ════════════════════════════════


def _deep_nesting(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


class StrictJsonDecoding(_AcquisitionCase):
    """Ambiguous/adversarial bodies never decode or leak; they fail closed."""

    def _assert_probe_drift(self, body: bytes) -> None:
        runtime = _healthy_runtime(version=_FakeHTTPResponse(200, body))
        _ = self._install(runtime)
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "schema_changed")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "schema_changed",
                "runtime_unreachable",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        self.assertEqual(runtime.requested_paths(), ["/api/version"])
        self._assert_safe_serialization(snapshot)

    def _assert_tags_drift(self, body: bytes) -> None:
        runtime = _healthy_runtime(tags=_FakeHTTPResponse(200, body))
        _ = self._install(runtime)
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "schema_changed")
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "schema_changed",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        self.assertEqual(runtime.requested_paths(), ["/api/version", "/api/tags"])
        self._assert_safe_serialization(snapshot)

    def _assert_ps_drift(self, body: bytes) -> None:
        runtime = _healthy_runtime(ps=_FakeHTTPResponse(200, body))
        _ = self._install(runtime)
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "schema_changed")
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)
        self.assertEqual(local.model_presence, "present")
        self.assertIsNone(local.effective_context_tokens)
        self._assert_codes(
            snapshot,
            [
                "schema_changed",
                "configured_context_unknown",
                "effective_context_unknown",
            ],
        )
        self.assertEqual(len(runtime.opened), 3)
        self._assert_safe_serialization(snapshot)

    def _drift(self, endpoint: str, body: bytes) -> None:
        if endpoint == "version":
            self._assert_probe_drift(body)
        elif endpoint == "tags":
            self._assert_tags_drift(body)
        else:
            self._assert_ps_drift(body)

    def test_duplicate_top_level_keys_rejected(self) -> None:
        cases = {
            "version": b'{"version": "x", "version": "y"}',
            "tags": b'{"models": [{"name": "test-model:latest"}], "models": []}',
            "ps": b'{"models": [], "models": [{"name": "test-model:latest"}]}',
        }
        for endpoint, body in cases.items():
            with self.subTest(endpoint=endpoint):
                self._drift(endpoint, body)

    def test_duplicate_nested_keys_rejected(self) -> None:
        cases = {
            "version": b'{"version": "x", "extra": {"a": 1, "a": 2}}',
            "tags": (
                b'{"models": [{"name": "test-model:latest",'
                b' "details": {"family": "s", "family": "t"}}]}'
            ),
            "ps": (
                b'{"models": [{"name": "test-model:latest",'
                b' "details": {"family": "s", "family": "t"}}]}'
            ),
        }
        for endpoint, body in cases.items():
            with self.subTest(endpoint=endpoint):
                self._drift(endpoint, body)

    def test_nonfinite_constants_and_floats_rejected(self) -> None:
        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e10000", b"-1e10000"):
            for endpoint in ("version", "tags", "ps"):
                with self.subTest(endpoint=endpoint, token=token):
                    if endpoint == "version":
                        self._drift(
                            endpoint, b'{"version": "x", "extra": ' + token + b"}"
                        )
                    elif endpoint == "tags":
                        self._drift(
                            endpoint,
                            b'{"models": [{"name": "test-model:latest",'
                            + b' "size": ' + token + b"}]}",
                        )
                    else:
                        self._drift(
                            endpoint,
                            b'{"models": [{"name": "test-model:latest",'
                            + b' "size_vram": ' + token + b"}]}",
                        )

    def test_ten_thousand_level_nesting_rejected(self) -> None:
        for endpoint in ("version", "tags", "ps"):
            with self.subTest(endpoint=endpoint):
                if endpoint == "version":
                    self._drift(
                        endpoint,
                        b'{"version": "x", "extra": ' + _deep_nesting(10_000) + b"}",
                    )
                else:
                    self._drift(
                        endpoint, b'{"models": ' + _deep_nesting(10_000) + b"}"
                    )

    def test_overlarge_integers_rejected_on_all_endpoints(self) -> None:
        # Integers outside the validated signed 64-bit band are a strict
        # decode rejection on every endpoint: never truncated, never
        # emitted, never healthy output. HUGE_DIGITS has 501 digits.
        huge_version = b'{"version": "x", "big": ' + HUGE_DIGITS + b"}"
        self._assert_probe_drift(huge_version)

        huge_tags = (
            b'{"models": [{"name": "test-model:latest", "digest": "'
            + DIGEST_ZERO.encode()
            + b'", "future_size": '
            + HUGE_DIGITS
            + b"}]}"
        )
        self._assert_tags_drift(huge_tags)

        huge_ps = (
            b'{"models": [{"name": "test-model:latest", "digest": "'
            + DIGEST_ZERO.encode()
            + b'", "context_length": '
            + HUGE_DIGITS
            + b"}]}"
        )
        self._assert_ps_drift(huge_ps)

        # No raw payload fragment (the huge digits) may leak anywhere.
        _ = self._install(_healthy_runtime(version=huge_version))
        snapshot = self._collect()
        self.assertNotIn(HUGE_DIGITS[:32].decode(), _serialized(snapshot))

    def test_i64_band_boundary_values(self) -> None:
        # The band edge itself stays valid evidence; one past it drifts.
        at_band = (
            b'{"models": [{"name": "test-model:latest", "model": '
            + b'"test-model:latest", "digest": "'
            + DIGEST_ZERO.encode()
            + b'", "context_length": '
            + str(2**63 - 1).encode()
            + b"}]}"
        )
        _ = self._install(_healthy_runtime(ps=at_band))
        snapshot = self._collect(configured_context_tokens=8192)
        self.assertEqual(snapshot.status, "ok")
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(local.effective_context_tokens, 2**63 - 1)

        past_band = at_band.replace(str(2**63 - 1).encode(), str(2**63).encode())
        self._assert_ps_drift(past_band)

    def test_decoder_memory_error_maps_to_schema_changed_on_all_endpoints(self) -> None:
        # Decoder resource exhaustion is provider input drift, not an internal
        # programming failure. Exercise the failing decode at each read stage.
        original_decode = cast(
            "Callable[[bytes], object]",
            getattr(ollama_acquisition, "_decode_strict"),
        )
        for endpoint, failing_call in ("version", 1), ("tags", 2), ("ps", 3):
            with self.subTest(endpoint=endpoint):
                calls = 0

                def decode(body: bytes) -> object:
                    nonlocal calls
                    calls += 1
                    if calls == failing_call:
                        raise MemoryError(FAKE_TRANSPORT_SECRET)
                    return original_decode(body)

                _ = self._install(_healthy_runtime())
                with mock.patch.object(
                    ollama_acquisition, "_decode_strict", side_effect=decode
                ):
                    snapshot = self._collect()
                self.assertEqual(snapshot.status, "schema_changed")
                local = snapshot.local_runtime
                assert local is not None
                self.assertEqual(
                    local.model_presence,
                    {"version": "unknown", "tags": "unknown", "ps": "present"}[endpoint],
                )
                self.assertNotIn(FAKE_TRANSPORT_SECRET, _serialized(snapshot))
                self._assert_no_collector_threads()

    def test_unexpected_decoder_error_is_reraised(self) -> None:
        _ = self._install(_healthy_runtime())
        with mock.patch.object(
            ollama_acquisition,
            "_decode_strict",
            side_effect=RuntimeError("TEST_ONLY_PROGRAMMING_ERROR"),
        ):
            with self.assertRaisesRegex(RuntimeError, "TEST_ONLY_PROGRAMMING_ERROR"):
                _ = self._collect()
        self._assert_no_collector_threads()


# ═══════════════════════ healthy collection paths ════════════════════════════


class HealthyRuntime(_AcquisitionCase):
    def test_present_with_both_contexts(self) -> None:
        _ = self._install(_healthy_runtime())
        snapshot = self._collect(configured_context_tokens=8192)

        self.assertEqual(snapshot.schema_version, 1)
        self.assertEqual(snapshot.provider, PROVIDER)
        self.assertEqual(snapshot.source, SOURCE)
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(snapshot.windows, ())
        self.assertIsNone(snapshot.plan)
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)
        self.assertEqual(local.model_presence, "present")
        self.assertEqual(local.model_name, MODEL)
        self.assertEqual(local.configured_context_tokens, 8192)
        self.assertEqual(local.effective_context_tokens, 16384)
        self._assert_codes(snapshot, [])
        self._assert_safe_serialization(snapshot)

    def test_exact_three_local_reads_in_order(self) -> None:
        runtime = self._install(_healthy_runtime())
        _ = self._collect(configured_context_tokens=8192)
        self.assertEqual(
            runtime.requested_paths(),
            ["/api/version", "/api/tags", "/api/ps"],
        )
        # One fresh connection per read; the connection close is never
        # part of the collector path (the raw socket handle is released
        # instead).
        self.assertEqual(len(runtime.connections), 3)

    def test_windows_empty_and_no_quota_semantics(self) -> None:
        _ = self._install(_healthy_runtime())
        snapshot = self._collect(configured_context_tokens=8192)
        self._assert_safe_serialization(snapshot)
        payload = snapshot.to_dict()
        self.assertEqual(payload["windows"], [])
        self.assertNotIn("plan", payload)
        text = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "used_percent",
            "remaining_percent",
            "resets_at",
            "unlimited",
            "scarcity",
            "plan",
            "duration_seconds",
        ):
            self.assertNotIn(forbidden, text)

    def test_contexts_independent_when_only_configured_known(self) -> None:
        # tags advertises details.context_length (8192) for the target; the
        # collector must never surface that as configured or effective
        # context, and ps shows the model not loaded (16384 would be wrong
        # too): configured comes only from the boundary, effective only
        # from validated ps evidence.
        _ = self._install(
            _healthy_runtime(ps=_fixture("ps-not-loaded.json")),
        )
        snapshot = self._collect(configured_context_tokens=8192)
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(local.configured_context_tokens, 8192)
        self.assertIsNone(local.effective_context_tokens)
        self._assert_codes(snapshot, ["effective_context_unknown"])
        self._assert_safe_serialization(snapshot)

    def test_contexts_independent_when_only_effective_known(self) -> None:
        _ = self._install(_healthy_runtime())
        snapshot = self._collect()
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(snapshot.status, "ok")
        self.assertIsNone(local.configured_context_tokens)
        self.assertEqual(local.effective_context_tokens, 16384)
        self._assert_codes(snapshot, ["configured_context_unknown"])
        self._assert_safe_serialization(snapshot)

    def test_effective_context_never_taken_from_other_loaded_model(self) -> None:
        _ = self._install(_healthy_runtime(ps=_fixture("ps-other-loaded.json")))
        snapshot = self._collect()
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(snapshot.status, "ok")
        self.assertIsNone(local.effective_context_tokens)
        self._assert_codes(snapshot, ["configured_context_unknown", "effective_context_unknown"])

    def test_effective_context_only_from_validated_evidence(self) -> None:
        # A listed loaded entry without usable context_length is drift, not
        # an unknown-valued effective context.
        _ = self._install(
            _healthy_runtime(ps=_fixture("ps-missing-context-length.json"))
        )
        snapshot = self._collect(configured_context_tokens=8192)
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertTrue(local.reachable)
        self.assertEqual(local.model_presence, "present")
        self.assertIsNone(local.effective_context_tokens)
        # Configured context is supplied here, so only the effective
        # context is unknown.
        self._assert_codes(snapshot, ["schema_changed", "effective_context_unknown"])

    def test_ps_schema_changed_fixture_fails_closed(self) -> None:
        _ = self._install(_healthy_runtime(ps=_fixture("ps-schema-changed.json")))
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "schema_changed")
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(local.model_presence, "present")
        self._assert_codes(
            snapshot,
            ["schema_changed", "configured_context_unknown", "effective_context_unknown"],
        )

    def test_ps_transport_failures_keep_validated_facts(self) -> None:
        for outcome in (
            ConnectionRefusedError(),
            TimeoutError(),
            500,
            _FakeHTTPResponse(200, b"A" * (ollama_acquisition.MAX_BODY_BYTES + 1)),
            HTTPException("truncated"),
        ):
            with self.subTest(outcome=type(outcome).__name__):
                _ = self._install(_healthy_runtime(ps=outcome))
                snapshot = self._collect()
                local = snapshot.local_runtime
                assert local is not None
                self.assertEqual(snapshot.status, "ok")
                self.assertEqual(local.model_presence, "present")
                self.assertIsNone(local.effective_context_tokens)
                self._assert_codes(
                    snapshot,
                    ["configured_context_unknown", "effective_context_unknown"],
                )

    def test_missing_model_is_explicit_not_unknown(self) -> None:
        _ = self._install(_healthy_runtime(tags=_fixture("tags-missing.json")))
        snapshot = self._collect(configured_context_tokens=8192)
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(snapshot.status, "unavailable")
        self.assertTrue(local.reachable)
        self.assertEqual(local.model_presence, "missing")
        self.assertEqual(local.model_name, MODEL)
        self.assertIsNone(local.effective_context_tokens)
        self._assert_codes(snapshot, ["source_unavailable", "model_missing"])
        self._assert_safe_serialization(snapshot)

    def test_missing_model_never_queries_ps(self) -> None:
        # Two reads total when the listing proves the model absent.
        runtime = _healthy_runtime(tags=_fixture("tags-missing.json"))
        _ = self._install(runtime)
        _ = self._collect()
        self.assertEqual(runtime.requested_paths(), ["/api/version", "/api/tags"])


# ═══════════════════════ digest identity pinning ═════════════════════════════


class DigestIdentityAgreement(_AcquisitionCase):
    def _assert_degraded(
        self, snapshot: CapacitySnapshot, *, with_configured: bool
    ) -> None:
        self.assertEqual(snapshot.status, "unknown")
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)
        self.assertEqual(local.model_presence, "present")
        self.assertIsNone(local.effective_context_tokens)
        expected = ["telemetry_unknown"]
        if not with_configured:
            expected.append("configured_context_unknown")
        expected.append("effective_context_unknown")
        self._assert_codes(snapshot, expected)
        # Digests are identity evidence only and never enter output.
        self._assert_safe_serialization(snapshot)

    def test_mismatched_ps_digest_degrades_without_discarding_presence(self) -> None:
        _ = self._install(_healthy_runtime(ps=_fixture("ps-digest-mismatch.json")))
        snapshot = self._collect(configured_context_tokens=8192)
        self._assert_degraded(snapshot, with_configured=True)

    def test_missing_ps_digest_degrades(self) -> None:
        _ = self._install(_healthy_runtime(ps=_fixture("ps-digest-missing.json")))
        snapshot = self._collect()
        self._assert_degraded(snapshot, with_configured=False)

    def test_conflicting_identity_listing_is_drift(self) -> None:
        # A listing whose `model` identity conflicts with its `name` can
        # never ground presence or effective context: it is drift.
        _ = self._install(
            _healthy_runtime(tags=_fixture("tags-conflicting-identity.json"))
        )
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "schema_changed")
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "schema_changed",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        # The conflicting identity never leaks into output.
        text = _serialized(snapshot)
        self.assertNotIn("other-model", text)
        self._assert_safe_serialization(snapshot)

    def test_invalid_tags_digest_degrades_even_with_valid_ps_digest(self) -> None:
        # Without a trustworthy listing digest there is nothing to agree
        # with, so the effective context is withheld.
        _ = self._install(
            _healthy_runtime(
                tags=_fixture("tags-invalid-digest.json"),
                ps=_fixture("ps-loaded.json"),
            )
        )
        snapshot = self._collect()
        self._assert_degraded(snapshot, with_configured=False)


# ═══════════════════════ failure and drift paths ═════════════════════════════


class UnreachableRuntime(_AcquisitionCase):
    def test_connection_failures_map_to_unreachable(self) -> None:
        for outcome in (
            ConnectionRefusedError(),
            ConnectionResetError(),
            TimeoutError(),
            OSError("name resolution failed"),
        ):
            with self.subTest(outcome=type(outcome).__name__):
                runtime = self._install(_healthy_runtime(version=outcome))
                snapshot = self._collect()
                local = snapshot.local_runtime
                assert local is not None
                self.assertEqual(snapshot.status, "unavailable")
                self.assertFalse(local.reachable)
                self.assertEqual(local.model_presence, "unknown")
                self.assertIsNone(local.effective_context_tokens)
                self._assert_codes(
                    snapshot,
                    [
                        "source_unavailable",
                        "runtime_unreachable",
                        "model_presence_unknown",
                        "configured_context_unknown",
                    ],
                )
                # No LAN scanning: exactly one attempt, nothing else.
                self.assertEqual(runtime.requested_paths(), ["/api/version"])
                self._assert_safe_serialization(snapshot)

    def test_unreachable_keeps_independently_configured_context(self) -> None:
        _ = self._install(_healthy_runtime(version=ConnectionRefusedError()))
        snapshot = self._collect(configured_context_tokens=8192)
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.configured_context_tokens, 8192)
        self.assertIsNone(local.effective_context_tokens)
        self._assert_codes(
            snapshot,
            ["source_unavailable", "runtime_unreachable", "model_presence_unknown"],
        )

    def test_unreachable_presence_is_unknown_even_with_known_name(self) -> None:
        _ = self._install(_healthy_runtime(version=TimeoutError()))
        snapshot = self._collect()
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(local.model_presence, "unknown")
        self.assertEqual(local.model_name, MODEL)


class ProbeDrift(_AcquisitionCase):
    def test_http_errors_map_to_unknown(self) -> None:
        for code in (301, 302, 403, 404, 500, 503):
            with self.subTest(code=code):
                response = _FakeHTTPResponse(
                    code, b"TEST_ONLY_ERROR_BODY", guard_read=True
                )
                runtime = self._install(_healthy_runtime(version=response))
                snapshot = self._collect()
                self.assertEqual(snapshot.status, "unknown")
                local = snapshot.local_runtime
                assert local is not None
                self.assertFalse(local.reachable)
                self.assertEqual(local.model_presence, "unknown")
                self._assert_codes(
                    snapshot,
                    [
                        "telemetry_unknown",
                        "runtime_unreachable",
                        "model_presence_unknown",
                        "configured_context_unknown",
                    ],
                )
                # Redirects are never followed: exactly one attempt.
                self.assertEqual(runtime.requested_paths(), ["/api/version"])
                # The non-200 body was never read; the response close is
                # never invoked on the collector path (the raw socket
                # handle is released instead).
                self.assertFalse(response.guard_tripped)
                self.assertFalse(response.closed)
                self.assertFalse(runtime.connections[0].closed)

    def test_oversized_probe_maps_to_unknown(self) -> None:
        _ = self._install(
            _healthy_runtime(
                version=_FakeHTTPResponse(
                    200, b"A" * (ollama_acquisition.MAX_BODY_BYTES + 1)
                )
            )
        )
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "unknown")
        self._assert_codes(
            snapshot,
            [
                "telemetry_unknown",
                "runtime_unreachable",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )

    def test_malformed_probe_maps_to_schema_changed(self) -> None:
        for body in (
            _fixture("version-malformed.json"),
            b"not json at all",
            b'{"version": \xff}',
            b'{"version": "0.0.0\\u0085"}',
            b'{"version": "0.0.0\\ufeff"}',
            b"",
        ):
            with self.subTest(body=body[:20]):
                runtime = self._install(_healthy_runtime(version=_FakeHTTPResponse(200, body)))
                snapshot = self._collect()
                self.assertEqual(snapshot.status, "schema_changed")
                local = snapshot.local_runtime
                assert local is not None
                self.assertFalse(local.reachable)
                self.assertEqual(local.model_presence, "unknown")
                self._assert_codes(
                    snapshot,
                    [
                        "schema_changed",
                        "runtime_unreachable",
                        "model_presence_unknown",
                        "configured_context_unknown",
                    ],
                )
                self.assertEqual(runtime.requested_paths(), ["/api/version"])


class TagsDrift(_AcquisitionCase):
    def test_transport_failures_after_probe_keep_reachability(self) -> None:
        for outcome in (
            ConnectionRefusedError(),
            TimeoutError(),
            HTTPException("truncated"),
        ):
            with self.subTest(outcome=type(outcome).__name__):
                runtime = self._install(_healthy_runtime(tags=outcome))
                snapshot = self._collect()
                self.assertEqual(snapshot.status, "unavailable")
                local = snapshot.local_runtime
                assert local is not None
                self.assertTrue(local.reachable)
                self.assertEqual(local.model_presence, "unknown")
                self._assert_codes(
                    snapshot,
                    [
                        "source_unavailable",
                        "model_presence_unknown",
                        "configured_context_unknown",
                    ],
                )
                self.assertEqual(
                    runtime.requested_paths(), ["/api/version", "/api/tags"]
                )

    def test_http_errors_map_to_unknown_keeping_reachability(self) -> None:
        _ = self._install(_healthy_runtime(tags=503))
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "unknown")
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)
        self._assert_codes(
            snapshot,
            [
                "telemetry_unknown",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )

    def test_malformed_listings_map_to_schema_changed(self) -> None:
        for name in (
            "tags-duplicate-names.json",
            "tags-malformed-entries.json",
            "tags-schema-changed.json",
        ):
            with self.subTest(fixture=name):
                _ = self._install(_healthy_runtime(tags=_fixture(name)))
                snapshot = self._collect()
                self.assertEqual(snapshot.status, "schema_changed")
                local = snapshot.local_runtime
                assert local is not None
                self.assertTrue(local.reachable)
                self.assertEqual(local.model_presence, "unknown")
                self._assert_codes(
                    snapshot,
                    [
                        "schema_changed",
                        "model_presence_unknown",
                        "configured_context_unknown",
                    ],
                )

    def test_malformed_listing_never_reports_presence(self) -> None:
        # A drifted listing must not degrade to "missing": the runtime did
        # not explicitly confirm the model absent.
        _ = self._install(_healthy_runtime(tags=_fixture("tags-malformed-entries.json")))
        snapshot = self._collect()
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(local.model_presence, "unknown")


# ═══════════════════ collection deadline ═════════════════════════════════════


class _BlockingGetResponseConnection:
    """Connection whose ``getresponse`` blocks until cancellation."""

    requests: list[tuple[str, str, object]]
    sock: _FakeSocket
    _release: threading.Event
    closed: bool
    close_count: int

    def __init__(self) -> None:
        self.requests = []
        self._release = threading.Event()
        self.sock = _FakeSocket(self._release)
        self.closed = False
        self.close_count = 0

    def request(
        self, method: str, path: str, /, *, headers: object = None
    ) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> object:
        # Models a permanently blocked read; only cancellation unblocks it.
        _ = self._release.wait(30.0)
        raise OSError("connection closed by collector")

    def close(self) -> None:
        self.closed = True
        self.close_count += 1
        _ = self._release.set()


class _BlockedReaderResponse:
    """200 response whose single ``read`` blocks until cancellation."""

    status: int = 200
    release: threading.Event
    closed: bool

    def __init__(self) -> None:
        self.closed = False
        self.release = threading.Event()

    def read(self, size: int = -1) -> object:
        _ = size
        _ = self.release.wait(30.0)
        raise OSError("connection closed by collector")

    def cancel(self) -> None:
        self.closed = True
        _ = self.release.set()

    def close(self) -> None:
        self.cancel()


class _BlockingReadConnection:
    """Connection serving a body that blocks until cancellation."""

    requests: list[tuple[str, str, object]]
    sock: _FakeSocket
    response: _BlockedReaderResponse
    closed: bool
    close_count: int

    def __init__(self) -> None:
        self.requests = []
        self.response = _BlockedReaderResponse()
        self.sock = _FakeSocket(self.response.release)
        self.closed = False
        self.close_count = 0

    def request(
        self, method: str, path: str, /, *, headers: object = None
    ) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> object:
        return self.response

    def close(self) -> None:
        self.closed = True
        self.close_count += 1
        self.response.cancel()


class _Factory:
    """open_connection seam returning a fresh blocking connection per call."""

    _make: Callable[[], object]
    connections: list[object]

    def __init__(self, make: Callable[[], object]) -> None:
        self._make = make
        self.connections = []

    def __call__(
        self,
        host: str,
        port: int,
        timeout: float,
        register_handle: Callable[[object], None],
    ) -> object:
        _ = host, port, timeout
        connection = self._make()
        self.connections.append(connection)
        sock: object | None = getattr(connection, "sock", None)
        release: object | None = (
            getattr(sock, "_release", None) if sock is not None else None
        )
        if type(release) is threading.Event:
            register_handle(release)
        elif type(sock) is socket.socket:
            register_handle(sock)
        return connection


class CollectionDeadline(_AcquisitionCase):
    def test_trickling_probe_aborts_at_deadline(self) -> None:
        # A peer that trickles bytes forever cannot extend the collection:
        # the monotonic deadline cancels the read and normalizes the outcome.
        trickling = _TricklingBody()
        runtime = self._install(_healthy_runtime(version=trickling))
        started = time.monotonic()
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", 0.3
        ):
            snapshot = self._collect()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.45)
        self.assertEqual(snapshot.status, "unavailable")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "source_unavailable",
                "runtime_unreachable",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        self.assertEqual(runtime.requested_paths(), ["/api/version"])
        # The worker is cancelled through its registered raw socket; no
        # collector worker remains and no response/connection close is invoked.
        self._assert_no_collector_threads()
        self._assert_safe_serialization(snapshot)

    def test_trickling_tags_expiry_degrades_to_unknown(self) -> None:
        # Deadline expiry during the listing read must not be reported as
        # ``unavailable`` (the probe validated reachability) nor as ``ok``:
        # it degrades to ``unknown`` with validated facts preserved.
        trickling = _TricklingBody()
        _ = self._install(_healthy_runtime(tags=trickling))
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", 0.3
        ):
            snapshot = self._collect()
        self.assertEqual(snapshot.status, "unknown")
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)  # validated by the probe
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "telemetry_unknown",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        # The worker is cancelled through its registered raw socket; no
        # collector worker remains and no response/connection close is invoked.
        self._assert_no_collector_threads()

    def test_late_socket_registration_is_cancelled_and_reclaimed(self) -> None:
        # Stage registration after the deadline cancellation pass. The
        # synchronization-aware registry must cancel this handle immediately,
        # allowing the non-daemon worker to terminate before the return.
        initial_release = threading.Event()
        late_release = threading.Event()

        def late_factory(
            host: str,
            port: int,
            timeout: float,
            register_handle: Callable[[object], None],
        ) -> object:
            _ = host, port, timeout
            register_handle(initial_release)
            if not initial_release.wait(1.0):
                raise AssertionError("deadline cancellation did not arrive")
            register_handle(late_release)
            raise OSError(FAKE_TRANSPORT_SECRET)

        patcher = mock.patch.object(
            ollama_acquisition, "open_connection", late_factory
        )
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", 0.02
        ):
            snapshot = self._collect()

        self.assertEqual(snapshot.status, "unavailable")
        self.assertTrue(late_release.is_set())
        # This is intentionally immediate, rather than a polling assertion:
        # _read_call must have completed its final bounded join already.
        self.assertFalse(
            any(
                thread.name == "scarcity-router-ollama-read"
                for thread in threading.enumerate()
            )
        )
        self.assertNotIn(FAKE_TRANSPORT_SECRET, _serialized(snapshot))
        self._assert_no_output()

    def test_expired_deadline_makes_no_transport_calls(self) -> None:
        runtime = self._install(_healthy_runtime())
        with mock.patch.object(ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", 0.0):
            snapshot = self._collect()
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual(runtime.opened, [])
        self._assert_no_output()


class BoundedWorkerDeadline(_AcquisitionCase):
    """One blocking call can never push the collection past the deadline."""

    BUDGET: float = 0.04
    BLOCK: float = 0.15
    ELAPSED_LIMIT: float = 0.12
    GRACE: float = 0.3

    def _collect_bounded(
        self,
        transport: Callable[[str, int, float, Callable[[object], None]], object],
    ) -> None:
        patcher = mock.patch.object(ollama_acquisition, "open_connection", transport)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        started = time.monotonic()
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", self.BUDGET
        ), contextlib.redirect_stdout(
            self.stdout
        ), contextlib.redirect_stderr(self.stderr):
            snapshot = ollama_acquisition.collect_ollama_capacity(
                retrieved_at=RETRIEVED_AT, model_name=MODEL
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, self.ELAPSED_LIMIT)
        self.assertEqual(snapshot.status, "unavailable")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "source_unavailable",
                "runtime_unreachable",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        self._assert_no_output()

    def test_permanently_blocking_getresponse_is_cancelled(self) -> None:
        # The collector must return by the deadline even when one transport
        # call blocks forever: cancellation sets the registered test
        # primitive (the production equivalent is a raw socket), which
        # unblocks the worker, and the
        # worker is reclaimed (no daemon left).
        factory = _Factory(_BlockingGetResponseConnection)
        self._collect_bounded(factory)
        connection = cast("_BlockingGetResponseConnection", factory.connections[0])
        self.assertTrue(
            cast("threading.Event", getattr(connection.sock, "_release")).is_set()
        )
        # The worker was reclaimed, not abandoned.
        time.sleep(self.GRACE)
        self.assertEqual(threading.active_count(), self._baseline_threads)

    def test_permanently_blocking_read_is_cancelled(self) -> None:
        factory = _Factory(_BlockingReadConnection)
        self._collect_bounded(factory)
        connection = cast("_BlockingReadConnection", factory.connections[0])
        self.assertTrue(
            cast("threading.Event", getattr(connection.sock, "_release")).is_set()
        )
        self.assertFalse(connection.response.closed)
        time.sleep(self.GRACE)
        self.assertEqual(threading.active_count(), self._baseline_threads)

    def test_repeated_permanent_blocks_keep_threads_and_fds_stable(self) -> None:
        proc_fd = Path("/proc/self/fd")
        baseline_fds = (
            len(list(proc_fd.iterdir())) if proc_fd.is_dir() else None
        )
        factory = _Factory(_BlockingGetResponseConnection)
        for round_number in range(3):
            with self.subTest(round=round_number):
                self._collect_bounded(factory)
                connection = cast(
                    "_BlockingGetResponseConnection", factory.connections[-1]
                )
                self.assertTrue(
                    cast("threading.Event", getattr(connection.sock, "_release")).is_set()
                )
                self._assert_no_collector_threads()
        if baseline_fds is not None:
            self.assertLessEqual(len(list(proc_fd.iterdir())), baseline_fds + 2)

    def test_permanently_blocked_close_cannot_leak_worker(self) -> None:
        # A transport whose close blocks forever is NEVER invoked on the
        # collector path: the deadline cancel unblocks the read through
        # the registered raw socket, the collection returns on time, and
        # zero collector workers remain alive immediately — even before
        # the hostile close blocker is ever released.
        close_blocker = threading.Event()

        class _PermanentCloseConnection:
            requests: list[tuple[str, str, object]]
            sock: _FakeSocket
            _release: threading.Event
            status: int = 200
            closed: bool
            close_calls: int

            def __init__(self) -> None:
                self.requests = []
                self._release = threading.Event()
                self.sock = _FakeSocket(self._release)
                self.closed = False
                self.close_calls = 0

            def request(
                self, method: str, path: str, /, *, headers: object = None
            ) -> None:
                self.requests.append((method, path, headers))

            def getresponse(self) -> object:
                return self

            def read(self, size: int = -1) -> object:
                _ = size
                if self._release.wait(30.0):
                    raise OSError("connection cancelled by collector")
                return _fixture("version-ok.json")

            def close(self) -> None:
                self.close_calls += 1
                _ = close_blocker.wait(30.0)
                self.closed = True

        connection = _PermanentCloseConnection()
        factory = _Factory(lambda: connection)
        patcher = mock.patch.object(
            ollama_acquisition, "open_connection", factory
        )
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        started = time.monotonic()
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", self.BUDGET
        ), contextlib.redirect_stdout(
            self.stdout
        ), contextlib.redirect_stderr(self.stderr):
            snapshot = ollama_acquisition.collect_ollama_capacity(
                retrieved_at=RETRIEVED_AT, model_name=MODEL
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, self.BUDGET + 0.1)
        self.assertEqual(snapshot.status, "unavailable")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        # The hostile close was never invoked on the collector path.
        self.assertEqual(connection.close_calls, 0)
        # The test cancellation event was set; the foreign close was not used.
        self.assertTrue(
            cast("threading.Event", getattr(connection.sock, "_release")).is_set()
        )
        # Zero collector workers immediately — before the blocker releases.
        self.assertFalse(
            any(
                thread.name == "scarcity-router-ollama-read"
                for thread in threading.enumerate()
            )
        )
        self._assert_no_output()
        # Fixture hygiene: release the blocker so its thread finishes.
        _ = close_blocker.set()
        time.sleep(self.GRACE)
        self.assertFalse(
            any(
                thread.name == "scarcity-router-ollama-read"
                for thread in threading.enumerate()
            )
        )

    def test_foreign_registered_handle_methods_are_never_invoked(self) -> None:
        release = threading.Event()

        class _ForeignHandle:
            shutdown_accesses: int
            close_accesses: int
            fileno_accesses: int

            def __init__(self) -> None:
                self.shutdown_accesses = 0
                self.close_accesses = 0
                self.fileno_accesses = 0

            @property
            def shutdown(self) -> Callable[[int], None]:
                self.shutdown_accesses += 1
                raise AssertionError("foreign shutdown must not be inspected")

            @property
            def close(self) -> Callable[[], None]:
                self.close_accesses += 1
                raise AssertionError("foreign close must not be inspected")

            @property
            def fileno(self) -> Callable[[], int]:
                self.fileno_accesses += 1
                raise AssertionError("foreign fileno must not be inspected")

        foreign = _ForeignHandle()

        class _BlockedConnection:
            def request(
                self, method: str, path: str, /, *, headers: object = None
            ) -> None:
                _ = method, path, headers

            def getresponse(self) -> object:
                _ = release.wait(30.0)
                raise OSError("connection cancelled by collector")

        connection = _BlockedConnection()

        def factory(
            host: str,
            port: int,
            timeout: float,
            register_handle: Callable[[object], None],
        ) -> object:
            _ = host, port, timeout
            register_handle(release)
            register_handle(foreign)
            return connection

        patcher = mock.patch.object(ollama_acquisition, "open_connection", factory)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        with contextlib.redirect_stdout(
            self.stdout
        ), contextlib.redirect_stderr(self.stderr):
            with self.assertRaisesRegex(
                RuntimeError, "unsupported cancellation handle"
            ):
                _ = ollama_acquisition.collect_ollama_capacity(
                    retrieved_at=RETRIEVED_AT, model_name=MODEL
                )
        self.assertEqual(foreign.shutdown_accesses, 0)
        self.assertEqual(foreign.close_accesses, 0)
        self.assertEqual(foreign.fileno_accesses, 0)
        self._assert_no_collector_threads()
        self._assert_no_output()

    def test_close_never_invoked_on_collector_path(self) -> None:
        # Resource release is owned by the registered cancellation primitive
        # (a raw socket in production). Response/connection ``close`` is
        # never part of the collector path — success included — so no
        # hostile close can ever be reached.
        connection = _RecordingCloseConnection()
        factory = _Factory(lambda: connection)
        patcher = mock.patch.object(
            ollama_acquisition, "open_connection", factory
        )
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        snapshot = self._collect(configured_context_tokens=8192)
        self.assertEqual(snapshot.status, "ok")
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(local.effective_context_tokens, 16384)
        self.assertEqual(connection.close_calls, 0)
        self.assertFalse(connection.closed)

    def test_delayed_eof_after_deadline_fails_closed(self) -> None:
        # A fully valid body whose EOF only arrives after the deadline must
        # never be reported ok: the worker re-checks the deadline after EOF
        # and the collector before consuming any result. The read models a
        # real socket: raw-socket cancellation unblocks it with an error
        # instead of delivering late data.
        release = threading.Event()
        block = self.BLOCK

        release = threading.Event()
        block = self.BLOCK

        class _DelayedEofConnection:
            requests: list[tuple[str, str, object]]
            sock: _FakeSocket
            _first: bool
            closed: bool

            def __init__(self) -> None:
                self.requests = []
                self.sock = _FakeSocket(release)
                self.closed = False
                self._first = True

            def request(
                self, method: str, path: str, /, *, headers: object = None
            ) -> None:
                self.requests.append((method, path, headers))

            def getresponse(self) -> object:
                return self

            # The connection doubles as the response object.
            status: int = 200

            def read(self, size: int = -1) -> object:
                _ = size
                if self._first:
                    self._first = False
                    if release.wait(block):
                        raise OSError("connection closed by collector")
                    return _fixture("version-ok.json")
                if release.is_set():
                    raise OSError("connection closed by collector")
                return b""

            def close(self) -> None:
                self.closed = True
                _ = release.set()

        factory = _Factory(_DelayedEofConnection)
        self._collect_bounded(factory)
        time.sleep(self.GRACE)
        self.assertEqual(threading.active_count(), self._baseline_threads)


# ═══════════════════════ malformed transport results ═════════════════════════


class MalformedTransportResult(_AcquisitionCase):
    def _install_single(self, outcome_for_version: object) -> _FakeRuntime:
        runtime = _healthy_runtime(version=outcome_for_version)
        _ = self._install(runtime)
        return runtime

    def test_response_object_without_read_maps_to_unknown(self) -> None:
        # A closable-but-unreadable response object is normalized without
        # invoking its close method, never an uncaught TypeError.
        class _CloseOnlyResponse:
            closed: bool

            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        response = _CloseOnlyResponse()
        runtime = self._install_single(response)
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "unknown")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "telemetry_unknown",
                "runtime_unreachable",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        self.assertFalse(response.closed)
        self.assertFalse(runtime.connections[0].closed)
        self._assert_no_output()

    def test_invalid_response_object_on_probe_maps_to_unknown(self) -> None:
        _ = self._install_single(object())
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "unknown")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "telemetry_unknown",
                "runtime_unreachable",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        self._assert_no_output()

    def test_invalid_response_object_on_ps_keeps_validated_presence(self) -> None:
        runtime = _FakeRuntime(
            {
                "/api/version": _fixture("version-ok.json"),
                "/api/tags": _fixture("tags-present.json"),
                "/api/ps": object(),
            }
        )
        _ = self._install(runtime)
        snapshot = self._collect(configured_context_tokens=8192)
        self.assertEqual(snapshot.status, "ok")
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)
        self.assertEqual(local.model_presence, "present")
        self.assertIsNone(local.effective_context_tokens)
        self._assert_codes(snapshot, ["effective_context_unknown"])
        self._assert_safe_serialization(snapshot)

    def test_non_bytes_chunk_maps_to_invalid_response(self) -> None:
        _ = self._install_single(
            _FakeHTTPResponse(200, b'{"version": "x"}', str_chunk_once=True)
        )
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "unknown")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self._assert_codes(
            snapshot,
            [
                "telemetry_unknown",
                "runtime_unreachable",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        self._assert_no_output()

    def test_secret_bearing_read_exception_is_never_propagated(self) -> None:
        _ = self._install_single(
            _FakeHTTPResponse(
                200,
                b"",
                read_error=RuntimeError(FAKE_TRANSPORT_SECRET),
            )
        )
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "unavailable")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "source_unavailable",
                "runtime_unreachable",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        # The provider-controlled exception text never surfaces anywhere.
        text = _serialized(snapshot)
        self.assertNotIn(FAKE_TRANSPORT_SECRET, text)
        self._assert_no_output()
        # The worker was not killed by the unexpected exception: it is
        # reclaimed deterministically.
        time.sleep(0.2)
        self.assertEqual(threading.active_count(), self._baseline_threads)

    def test_error_response_not_read_without_response_close(self) -> None:
        # The error body carries a read guard; raw-socket cleanup must happen
        # without reading it or invoking response/connection close.
        guarded = _FakeHTTPResponse(500, b"TEST_ONLY_ERROR_BODY", guard_read=True)
        runtime = _healthy_runtime(version=guarded)
        _ = self._install(runtime)
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "unknown")
        self.assertFalse(guarded.guard_tripped)
        self.assertFalse(guarded.closed)
        self.assertFalse(runtime.connections[0].closed)
        self._assert_no_output()

    def test_ambiguous_response_framing_fails_closed(self) -> None:
        for headers in (
            {"Content-Length": "10", "Transfer-Encoding": "chunked"},
            {"Content-Length": "10, 11"},
            {"Transfer-Encoding": "gzip"},
        ):
            with self.subTest(headers=headers):
                response = _FakeHTTPResponse(
                    200, _fixture("version-ok.json"), headers=headers
                )
                _ = self._install(_healthy_runtime(version=response))
                snapshot = self._collect()
                self.assertEqual(snapshot.status, "schema_changed")
                self.assertNotIn(FAKE_TRANSPORT_SECRET, _serialized(snapshot))
                self._assert_no_collector_threads()

    def test_internal_framing_error_is_reraised(self) -> None:
        _ = self._install(_healthy_runtime())
        with mock.patch.object(
            ollama_acquisition,
            "_response_body_framing",
            side_effect=RuntimeError("TEST_ONLY_INTERNAL_FRAMING_ERROR"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "TEST_ONLY_INTERNAL_FRAMING_ERROR"
            ):
                _ = self._collect()
        self._assert_no_collector_threads()


# ═════════════ real-transport cancellation (http.client) ════════════════════


class _OneShotListener:
    """Local TCP listener serving one canned response, then holding open.

    Purely an in-process test fixture (loopback, no provider contact): the
    socketpair-equivalent needed to exercise the real ``HTTPConnection``
    cancellation paths, including the ``Connection: close`` ownership
    transfer where the response object owns the socket file.
    """

    _respond: bytes | None
    _teardown: threading.Event
    accepted: threading.Event
    _listener: socket.socket
    _thread: threading.Thread
    port: int

    def __init__(self, respond: bytes | None) -> None:
        self._respond = respond
        self._teardown = threading.Event()
        self.accepted = threading.Event()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            accepted: tuple[socket.socket, object] = self._listener.accept()
            connection = accepted[0]
        except OSError:
            return
        self.accepted.set()
        with connection:
            respond = self._respond
            if respond is not None:
                connection.sendall(respond)
            # Hold the connection open (no EOF): a reader stays blocked
            # until the collector cancels through the socket or the test
            # tears the listener down.
            _ = self._teardown.wait(30.0)

    def close(self) -> None:
        _ = self._teardown.set()
        self._listener.close()


class _ScriptedListener:
    """Local TCP listener serving one canned response per connection.

    Each accepted connection receives its scripted bytes (or nothing) and
    is then held open without EOF, so the next collector read blocks.
    """

    _responses: list[bytes | None]
    _close_after_send: bool
    _teardown: threading.Event
    _listener: socket.socket
    _thread: threading.Thread
    port: int

    def __init__(
        self, responses: list[bytes | None], close_after_send: bool = False
    ) -> None:
        self._responses = responses
        self._close_after_send = close_after_send
        self._teardown = threading.Event()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        for respond in self._responses:
            try:
                accepted: tuple[socket.socket, object] = self._listener.accept()
                connection = accepted[0]
            except OSError:
                return
            # Serve each connection on its own fixture thread so several
            # sequential collector reads are scripted while every held
            # connection stays open without EOF.
            thread = threading.Thread(
                target=self._hold,
                args=(connection, respond, self._close_after_send),
                daemon=True,
            )
            thread.start()

    def _hold(
        self,
        connection: socket.socket,
        respond: bytes | None,
        close_after_send: bool,
    ) -> None:
        with connection:
            if close_after_send:
                # Drain the request before a graceful close; closing with an
                # unread request can produce a reset instead of a FIN.
                connection.settimeout(1.0)
                request = b""
                try:
                    while b"\r\n\r\n" not in request:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        request += chunk
                except OSError:
                    return
            if respond is not None:
                connection.sendall(respond)
            if close_after_send:
                return
            # Hold open (no EOF) until the test tears the listener down;
            # a mid-transfer reader stays blocked until cancellation.
            _ = self._teardown.wait(30.0)

    def close(self) -> None:
        _ = self._teardown.set()
        self._listener.close()


class RealTransportCancellation(_AcquisitionCase):
    """Cancellation proven against the real ``HTTPConnection``."""

    BUDGET: float = 0.15
    ELAPSED_LIMIT: float = 0.6
    _listeners: list[_OneShotListener | _ScriptedListener]
    _baseline_fds: int | None
    _proc_fd: Path

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._listeners = []
        self._baseline_fds = None
        self._proc_fd = Path("/proc/self/fd")

    @override
    def setUp(self) -> None:
        super().setUp()
        if self._proc_fd.is_dir():
            self._baseline_fds = len(list(self._proc_fd.iterdir()))

    @override
    def tearDown(self) -> None:
        # Release the fixture listeners first: their (daemon) hold-threads
        # exit and their sockets close. Then prove no non-daemon thread —
        # in particular no collector worker — survived, and that file
        # descriptors returned to the baseline. A lingering non-daemon
        # thread fails the test: no worker may survive a collection.
        for listener in self._listeners:
            listener.close()
        wait_until = time.monotonic() + 3.0
        while threading.active_count() > self._baseline_threads and (
            time.monotonic() < wait_until
        ):
            time.sleep(0.01)
        current = threading.current_thread()
        lingering = [
            thread
            for thread in threading.enumerate()
            if thread is not current and not thread.daemon
        ]
        self.assertEqual(lingering, [])
        self.assertFalse(
            any(
                thread.name == "scarcity-router-ollama-read"
                for thread in threading.enumerate()
            ),
            "collector worker must be reclaimed before returning",
        )
        if self._baseline_fds is not None:
            self.assertLessEqual(
                len(list(self._proc_fd.iterdir())), self._baseline_fds + 4
            )
        super().tearDown()

    def _start_listener(self, respond: bytes | None) -> int:
        listener = _OneShotListener(respond)
        self._listeners.append(listener)
        return listener.port

    def _start_scripted_listener(
        self, responses: list[bytes | None], close_after_send: bool = False
    ) -> int:
        listener = _ScriptedListener(responses, close_after_send)
        self._listeners.append(listener)
        return listener.port

    def _assert_reclaimed(self, elapsed: float) -> None:
        self.assertLess(elapsed, self.ELAPSED_LIMIT)
        self._assert_no_collector_threads()
        self._assert_no_output()

    def test_header_phase_block_cancels_and_reclaims(self) -> None:
        # The runtime accepts, then sends nothing and holds the connection
        # open: getresponse genuinely blocks reading the status line until
        # the deadline. Cancellation must reach the connect/header phase
        # socket and reclaim the (non-daemon) worker.
        listener = _OneShotListener(None)
        self._listeners.append(listener)
        started = time.monotonic()
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", self.BUDGET
        ):
            snapshot = self._collect(endpoint=f"http://127.0.0.1:{listener.port}")
        elapsed = time.monotonic() - started
        self.assertTrue(listener.accepted.is_set())
        # Actually blocked: the collection lasted the whole budget.
        self.assertGreaterEqual(elapsed, self.BUDGET * 0.8)
        self.assertEqual(snapshot.status, "unavailable")
        local = snapshot.local_runtime
        assert local is not None
        self.assertFalse(local.reachable)
        self.assertEqual(local.model_presence, "unknown")
        self._assert_reclaimed(elapsed)

    def test_body_phase_block_with_connection_close_cancels(self) -> None:
        # With ``Connection: close`` the socket file ownership transfers to
        # the response object, but the separately retained raw-socket handle
        # still cancels the body read. The probe completes against a scripted
        # valid response; the listing body then blocks mid-transfer.
        version_body = b'{"version": "0.0.0"}'
        complete = (
            b"HTTP/1.1 200 OK\r\n"
            + b"Content-Length: "
            + str(len(version_body)).encode()
            + b"\r\n"
            + b"Connection: close\r\n"
            + b"\r\n"
            + version_body
        )
        partial = (
            b"HTTP/1.1 200 OK\r\n"
            + b"Content-Length: 100\r\n"
            + b"Connection: close\r\n"
            + b"\r\n"
            + b"AB"
        )
        port = self._start_scripted_listener([complete, partial, None])
        started = time.monotonic()
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", self.BUDGET
        ):
            snapshot = self._collect(endpoint=f"http://127.0.0.1:{port}")
        elapsed = time.monotonic() - started
        self.assertEqual(snapshot.status, "unknown")
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)  # validated before the body block
        self.assertEqual(local.model_presence, "unknown")
        self._assert_codes(
            snapshot,
            [
                "telemetry_unknown",
                "model_presence_unknown",
                "configured_context_unknown",
            ],
        )
        self._assert_reclaimed(elapsed)

    def test_truncated_content_length_fails_closed_on_each_endpoint(self) -> None:
        bodies = [
            b'{"version": "0.0.0"}',
            _fixture("tags-present.json"),
            _fixture("ps-loaded.json"),
        ]
        paths = ["version", "tags", "ps"]
        for failing_index, path in enumerate(paths):
            with self.subTest(path=path):
                responses: list[bytes | None] = [
                    _http_response(body)
                    for body in bodies[:failing_index]
                ]
                body = bodies[failing_index]
                responses.append(
                    _http_response(body, content_length=len(body) + 10)
                )
                port = self._start_scripted_listener(
                    responses, close_after_send=True
                )
                snapshot = self._collect(endpoint=f"http://127.0.0.1:{port}")
                self.assertEqual(snapshot.status, "schema_changed")
                local = snapshot.local_runtime
                assert local is not None
                self.assertEqual(
                    local.model_presence,
                    {"version": "unknown", "tags": "unknown", "ps": "present"}[path],
                )
                self.assertNotIn(FAKE_RAW_FRAGMENT, _serialized(snapshot))
                self._assert_reclaimed(0.0)

    def test_complete_and_truncated_chunked_frames_are_distinguished(self) -> None:
        version_body = b'{"version": "0.0.0"}'
        missing_body = _fixture("tags-missing.json")
        complete_port = self._start_scripted_listener(
            [
                _http_response(version_body, chunked=True),
                _http_response(missing_body, chunked=True),
            ],
            close_after_send=True,
        )
        complete = self._collect(endpoint=f"http://127.0.0.1:{complete_port}")
        self.assertEqual(complete.status, "unavailable")
        complete_local = complete.local_runtime
        assert complete_local is not None
        self.assertEqual(complete_local.model_presence, "missing")
        self._assert_reclaimed(0.0)

        truncated = (
            b"HTTP/1.1 200 OK\r\n"
            + b"Transfer-Encoding: chunked\r\n"
            + b"Connection: close\r\n\r\n"
            + f"{len(version_body) + 10:x}\r\n".encode()
            + version_body
            + b"\r\n"
        )
        truncated_port = self._start_scripted_listener(
            [truncated], close_after_send=True
        )
        rejected = self._collect(endpoint=f"http://127.0.0.1:{truncated_port}")
        self.assertEqual(rejected.status, "schema_changed")
        self._assert_reclaimed(0.0)

    def test_trailing_bytes_after_content_length_fail_closed(self) -> None:
        version_body = b'{"version": "0.0.0"}'
        version_with_trailing = _http_response(version_body) + b"EXTRA"
        port = self._start_scripted_listener(
            [
                version_with_trailing,
                _http_response(_fixture("tags-present.json")),
                _http_response(_fixture("ps-loaded.json")),
            ],
            close_after_send=True,
        )
        snapshot = self._collect(endpoint=f"http://127.0.0.1:{port}")
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertNotIn(b"EXTRA".decode(), _serialized(snapshot))
        self._assert_reclaimed(0.0)

    def test_repeated_real_timeouts_keep_threads_stable(self) -> None:
        for round_number in range(3):
            with self.subTest(round=round_number):
                listener = _OneShotListener(None)
                self._listeners.append(listener)
                started = time.monotonic()
                with mock.patch.object(
                    ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", self.BUDGET
                ):
                    snapshot = self._collect(endpoint=f"http://127.0.0.1:{listener.port}")
                elapsed = time.monotonic() - started
                self.assertEqual(snapshot.status, "unavailable")
                self.assertGreaterEqual(elapsed, self.BUDGET * 0.8)
                self.assertLess(elapsed, self.ELAPSED_LIMIT)
                self.assertTrue(listener.accepted.is_set())
                self._assert_no_collector_threads()

    def test_stuck_close_paths_do_not_deadlock_or_leak(self) -> None:
        # Closes that raise (and run twice: cancel + worker cleanup) must
        # neither deadlock the bounded reclaim nor leak provider-controlled
        # exception text.
        release = threading.Event()

        class _RaisingCloseEverything:
            requests: list[tuple[str, str, object]]
            sock: _FakeSocket
            status: int = 200
            close_count: int
            closed: bool

            def __init__(self) -> None:
                self.requests = []
                self.sock = _FakeSocket(release)
                self.close_count = 0
                self.closed = False

            def request(
                self, method: str, path: str, /, *, headers: object = None
            ) -> None:
                self.requests.append((method, path, headers))

            def getresponse(self) -> object:
                return self

            def read(self, size: int = -1) -> object:
                _ = size
                if release.wait(30.0):
                    raise OSError("connection closed by collector")
                return _fixture("version-ok.json")

            @property
            def close(self) -> Callable[[], None]:
                # A hostile descriptor: attribute access performs the
                # unblocking work first (like a real close), then raises
                # with provider-controlled text. Cancellation must still
                # succeed and contain the text.
                self.closed = True
                self.close_count += 1
                _ = release.set()
                raise RuntimeError(FAKE_TRANSPORT_SECRET)

        connection = _RaisingCloseEverything()
        factory = _Factory(lambda: connection)
        patcher = mock.patch.object(
            ollama_acquisition, "open_connection", factory
        )
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        started = time.monotonic()
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", self.BUDGET
        ), contextlib.redirect_stdout(
            self.stdout
        ), contextlib.redirect_stderr(self.stderr):
            snapshot = ollama_acquisition.collect_ollama_capacity(
                retrieved_at=RETRIEVED_AT, model_name=MODEL
            )
        elapsed = time.monotonic() - started
        self.assertEqual(snapshot.status, "unavailable")
        self.assertLess(elapsed, self.ELAPSED_LIMIT)
        text = _serialized(snapshot)
        self.assertNotIn(FAKE_TRANSPORT_SECRET, text)
        self._assert_no_output()
        self._assert_no_collector_threads()
        # The hostile close descriptor is never invoked on the collector
        # path at all: resource release happens through the raw socket.
        self.assertEqual(connection.close_count, 0)


# ═══════════════════════ transport mechanism ═════════════════════════════════


class TransportMechanism(unittest.TestCase):
    def test_seam_returns_connected_connection_with_socket_handle(self) -> None:
        # The real seam pre-connects within the phase timeout and exposes
        # the raw socket as the cancellation handle. Used only against a
        # local in-process listener fixture, never a provider runtime.
        version_body = b'{"version": "0.0.0"}'
        listener = _OneShotListener(
            b"HTTP/1.1 200 OK\r\n"
            + b"Content-Length: "
            + str(len(version_body)).encode()
            + b"\r\n"
            + b"Connection: close\r\n"
            + b"\r\n"
            + version_body
        )
        registered: list[object] = []
        try:
            connection = ollama_acquisition.open_connection(
                "127.0.0.1", listener.port, 2.0, registered.append
            )
            self.assertIsInstance(connection, HTTPConnection)
            real = cast("http.client.HTTPConnection", connection)
            self.assertEqual(real.host, "127.0.0.1")
            self.assertEqual(real.port, listener.port)
            handle = getattr(connection, "sock", None)
            self.assertIsInstance(handle, socket.socket)
            socket_handle = cast("socket.socket", handle)
            peer = cast("object", socket_handle.getpeername())
            self.assertTrue(peer is not None)
            # The cancellation handle was registered before any blocking
            # operation and is the connection's own raw socket.
            self.assertEqual(registered, [socket_handle])
            real.close()
        finally:
            listener.close()




class RequestShape(_AcquisitionCase):
    def test_requests_are_get_with_accept_on_canonical_paths(self) -> None:
        runtime = self._install(_healthy_runtime())
        _ = self._collect(configured_context_tokens=8192)

        self.assertEqual(len(runtime.connections), 3)
        self.assertEqual(
            runtime.requested_paths(),
            ["/api/version", "/api/tags", "/api/ps"],
        )
        for connection in runtime.connections:
            self.assertEqual(len(connection.requests), 1)
            method, _path, headers = connection.requests[0]
            self.assertEqual(method, "GET")
            self.assertEqual(headers, {"Accept": "application/json"})

    def test_timeouts_forwarded_within_bounds(self) -> None:
        runtime = self._install(_healthy_runtime())
        _ = self._collect()
        for _host, _port, timeout in runtime.opened:
            self.assertLessEqual(timeout, ollama_acquisition.TIMEOUT_SECONDS)
            self.assertGreater(timeout, 0)


# ═══════════════════════ determinism and contract ════════════════════════════


class DeterministicNormalization(_AcquisitionCase):
    def _twice(self) -> tuple[CapacitySnapshot, CapacitySnapshot]:
        first = self._collect(configured_context_tokens=8192)
        second = self._collect(configured_context_tokens=8192)
        return first, second

    def test_equal_inputs_normalize_equally(self) -> None:
        _ = self._install(_healthy_runtime())
        first, second = self._twice()
        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_failure_paths_normalize_deterministically(self) -> None:
        for name in ("tags-duplicate-names.json", "tags-schema-changed.json"):
            with self.subTest(fixture=name):
                _ = self._install(_healthy_runtime(tags=_fixture(name)))
                first, second = self._twice()
                self.assertEqual(first, second)


class SnapshotContract(_AcquisitionCase):
    def test_serialized_shape_round_trips(self) -> None:
        _ = self._install(_healthy_runtime())
        snapshot = self._collect(configured_context_tokens=8192)
        payload = snapshot.to_dict()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(set(payload.keys()), {
            "schema_version",
            "provider",
            "source",
            "retrieved_at",
            "status",
            "windows",
            "local_runtime",
            "diagnostics",
        })
        local_payload = cast("dict[str, object]", payload["local_runtime"])
        self.assertEqual(set(local_payload.keys()), {
            "reachable",
            "model_presence",
            "model_name",
            "configured_context_tokens",
            "effective_context_tokens",
        })
        round_tripped = CapacitySnapshot.from_dict(payload)
        self.assertEqual(round_tripped, snapshot)
        self.assertEqual(round_tripped.to_dict(), payload)

    def test_optional_fields_omitted_never_null(self) -> None:
        _ = self._install(_healthy_runtime(ps=_fixture("ps-not-loaded.json")))
        snapshot = self._collect()
        payload = cast(
            "dict[str, object]", json.loads(json.dumps(snapshot.to_dict()))
        )
        local = cast("dict[str, object]", payload["local_runtime"])
        self.assertNotIn("configured_context_tokens", local)
        self.assertNotIn("effective_context_tokens", local)
        self.assertNotIn("plan", payload)
        self.assertNotIn("null", json.dumps(payload))

    def test_local_runtime_context_independence_contract(self) -> None:
        cases = [
            (True, None, None),
            (True, 8192, None),
            (True, None, 16384),
            (True, 8192, 16384),
        ]
        for reachable, configured, effective in cases:
            with self.subTest(
                configured=configured, effective=effective
            ):
                local = LocalRuntime(
                    reachable=reachable,
                    model_presence="present",
                    model_name=MODEL,
                    configured_context_tokens=configured,
                    effective_context_tokens=effective,
                )
                payload = local.to_dict()
                self.assertEqual(
                    LocalRuntime.from_dict(payload), local
                )
                self.assertEqual(
                    "configured_context_tokens" in payload,
                    configured is not None,
                )
                self.assertEqual(
                    "effective_context_tokens" in payload,
                    effective is not None,
                )

    def test_unreachable_runtime_forces_unknown_presence(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = LocalRuntime(
                reachable=False,
                model_presence="present",
                model_name=MODEL,
            )

    def test_invalid_retrieved_at_fails_through_contract_validation(self) -> None:
        _ = self._install(_healthy_runtime())
        with self.assertRaises(CapacityValidationError):
            _ = self._collect(retrieved_at="2026-09-04T21:00:00Z")

    def test_provider_and_source_identifiers_are_fixed(self) -> None:
        self.assertEqual(PROVIDER, "ollama")
        self.assertEqual(SOURCE, "ollama_local")
        _ = self._install(_healthy_runtime(version=ConnectionRefusedError()))
        snapshot = self._collect()
        self.assertEqual(snapshot.provider, "ollama")
        self.assertEqual(snapshot.source, "ollama_local")


# ═══════════════════════ marker non-leak sweep ═══════════════════════════════


class MarkerNonLeak(_AcquisitionCase):
    def _poisoned_runtime(self) -> _FakeRuntime:
        poisoned_tags = (
            b'{"models": [{"name": "'
            + FAKE_SECRET.encode()
            + b':latest", "model": "'
            + FAKE_PATH.encode()
            + b'"}]}'
        )
        return _FakeRuntime(
            {
                "/api/version": (
                    b'{"version": "' + FAKE_RAW_FRAGMENT.encode() + b'"}'
                ),
                "/api/tags": poisoned_tags,
                "/api/ps": _fixture("ps-loaded.json"),
            }
        )

    def _scenarios(self) -> dict[str, _FakeRuntime]:
        return {
            "healthy": _healthy_runtime(),
            "unreachable": _healthy_runtime(version=ConnectionRefusedError()),
            "timeout": _healthy_runtime(version=TimeoutError()),
            "probe-http-error": _healthy_runtime(version=500),
            "probe-oversized": _healthy_runtime(
                version=_FakeHTTPResponse(
                    200, b"A" * (ollama_acquisition.MAX_BODY_BYTES + 1)
                )
            ),
            "probe-malformed": _healthy_runtime(version=_FakeHTTPResponse(200, b"not json")),
            "tags-drift": _healthy_runtime(tags=_fixture("tags-schema-changed.json")),
            "tags-duplicates": _healthy_runtime(
                tags=_fixture("tags-duplicate-names.json")
            ),
            "model-missing": _healthy_runtime(tags=_fixture("tags-missing.json")),
            "ps-drift": _healthy_runtime(ps=_fixture("ps-schema-changed.json")),
            "ps-digest-mismatch": _healthy_runtime(
                ps=_fixture("ps-digest-mismatch.json")
            ),
            "secret-read-error": _healthy_runtime(
                version=_FakeHTTPResponse(
                    200, b"", read_error=RuntimeError(FAKE_TRANSPORT_SECRET)
                )
            ),
            "poisoned-responses": self._poisoned_runtime(),
        }

    def test_no_scenario_leaks_markers_or_endpoint(self) -> None:
        for name, runtime in self._scenarios().items():
            with self.subTest(scenario=name):
                _ = self._install(runtime)
                snapshot = self._collect(configured_context_tokens=8192)
                text = _serialized(snapshot)
                for marker in (
                    FAKE_SECRET,
                    FAKE_PATH,
                    FAKE_RAW_FRAGMENT,
                    FAKE_TRANSPORT_SECRET,
                    FAKE_REDIRECT_TARGET,
                    ENDPOINT,
                    "http://",
                    "Authorization",
                    "sha256:",
                ):
                    self.assertNotIn(marker, text)
                for diagnostic in snapshot.diagnostics:
                    self.assertNotIn(FAKE_SECRET, repr(diagnostic))
                self._assert_no_output()
