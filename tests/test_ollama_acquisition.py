"""Security, integration and contract tests for the Ollama acquisition layer.

The transport seam (``open_response``) is replaced with a fake local runtime
dispatching per URL path; no test in this module contacts a runtime, a
network or the filesystem beyond the synthetic fixtures. Every failure class
asserts that conspicuous synthetic markers (fake secrets, fake paths, the
endpoint URL, digests) appear in no serialized snapshot, repr, diagnostic or
captured stdout/stderr output, and that adversarial bodies (duplicate keys,
non-finite values, deep nesting, huge integers, trickling reads) are handled
by the strict decode/deadline boundaries.

Contract tests assert the normalized snapshot/local-runtime shape, context
independence and deterministic normalization per docs/capacity-model.md.
"""

from __future__ import annotations

import contextlib
import io
import json
import time
import unittest
import urllib.error
import urllib.request
from http.client import HTTPException, HTTPMessage
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
DIGEST_OTHER = "sha256:" + "f" * 64
HUGE_INT = 10**500
HUGE_DIGITS = str(HUGE_INT).encode()

# Conspicuous synthetic-only markers; never realistic production shapes.
FAKE_SECRET = "TEST_ONLY_FAKE_OLLAMA_SECRET_NEVER_REAL"
FAKE_PATH = "/home/test/.fake-models/TEST_ONLY_PATH"
FAKE_RAW_FRAGMENT = "TEST_ONLY_RAW_RESPONSE_FRAGMENT"
FAKE_REDIRECT_TARGET = "TEST_ONLY_REDIRECT_TARGET"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _serialized(snapshot: CapacitySnapshot) -> str:
    return json.dumps(snapshot.to_dict(), sort_keys=True) + repr(snapshot)


class _FakeResponse:
    """Minimal successful-response fake for the transport seam.

    ``read(size)`` honors the requested bound and ends with an empty chunk
    at EOF, like a real streamed body, so the collector's chunked
    deadline-bounded read loop is exercised faithfully.
    """

    _body: bytes
    _offset: int
    _read_error: Exception | None
    closed: bool

    def __init__(self, body: bytes, read_error: Exception | None = None) -> None:
        self._body = body
        self._offset = 0
        self._read_error = read_error
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        if size < 0:
            chunk = self._body[self._offset :]
            self._offset = len(self._body)
            return chunk
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True


class _TricklingResponse:
    """A response that trickles bytes forever, one small chunk per read."""

    _delay: float
    closed: bool

    def __init__(self, delay: float = 0.02) -> None:
        self._delay = delay
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        _ = size
        time.sleep(self._delay)
        return b"x" * 256

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_TricklingResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        ENDPOINT, code, "synthetic", HTTPMessage(), io.BytesIO(b"")
    )


class _TrackingBytesIO(io.BytesIO):
    """BytesIO that records its read position when closed, never after."""

    close_count: int
    position_at_close: int

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.close_count = 0
        self.position_at_close = -1

    @override
    def close(self) -> None:
        self.close_count += 1
        self.position_at_close = self.tell()
        super().close()


def _http_error_with_fp(code: int, fp: io.BytesIO) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(ENDPOINT, code, "synthetic", HTTPMessage(), fp)


class _FakeRuntime:
    """Fake local runtime: dispatches one fixed outcome per URL path."""

    _outcomes: dict[str, bytes | Exception | _FakeResponse | _TricklingResponse]

    def __init__(
        self,
        outcomes: dict[str, bytes | Exception | _FakeResponse | _TricklingResponse],
    ) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[str, float]] = []

    def __call__(
        self, request: urllib.request.Request, timeout: float
    ) -> _FakeResponse | _TricklingResponse:
        url = request.full_url
        self.calls.append((url, timeout))
        path = url.removeprefix(BASE)
        outcome = self._outcomes.get(path)
        if outcome is None:
            raise AssertionError(f"unexpected request path: {path!r}")
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, (bytes, bytearray)):
            return _FakeResponse(bytes(outcome))
        return outcome

    def requested_paths(self) -> list[str]:
        return [url.removeprefix(BASE) for url, _timeout in self.calls]


def _healthy_runtime(
    *,
    tags: bytes | Exception | _FakeResponse | _TricklingResponse | None = None,
    ps: bytes | Exception | _FakeResponse | _TricklingResponse | None = None,
    version: bytes | Exception | _FakeResponse | _TricklingResponse | None = None,
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

    @override
    def setUp(self) -> None:
        self.runtime = None
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def _install(
        self,
        runtime: _FakeRuntime,
    ) -> _FakeRuntime:
        self.runtime = runtime
        patcher = mock.patch.object(ollama_acquisition, "open_response", runtime)
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
        self.assertEqual(runtime.calls, [])
        self._assert_no_output()

    def test_omitted_port_contacts_canonical_default(self) -> None:
        runtime = self._install(_healthy_runtime())
        _ = self._collect(endpoint="http://127.0.0.1")
        self.assertEqual(
            [url for url, _timeout in runtime.calls],
            [
                "http://127.0.0.1:11434/api/version",
                "http://127.0.0.1:11434/api/tags",
                "http://127.0.0.1:11434/api/ps",
            ],
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
                self.assertEqual(runtime.calls, [])
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
                self.assertEqual(runtime.calls, [])

    def test_control_characters_in_model_name_rejected(self) -> None:
        runtime = self._install(_healthy_runtime())
        for bad in ("\x01test-model:latest", "test-model:latest\x7f", "a\tb"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    _ = self._collect(model_name=bad)
                self.assertEqual(runtime.calls, [])

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
        self.assertEqual(len(runtime.calls), 2)

    def test_invalid_configured_context_raises_before_any_io(self) -> None:
        runtime = self._install(_healthy_runtime())
        for bad in (0, -1, True, "16384", 16.5, [16384]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _ = self._collect(configured_context_tokens=cast(int, bad))
                self.assertEqual(runtime.calls, [])
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
        runtime = _healthy_runtime(version=_FakeResponse(body))
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
        runtime = _healthy_runtime(tags=_FakeResponse(body))
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
        runtime = _healthy_runtime(ps=_FakeResponse(body))
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
        self.assertEqual(len(runtime.calls), 3)
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

    def test_very_large_integers_never_crash_or_leak(self) -> None:
        # Tolerated additive fields carrying huge integers parse fine
        # (Python ints are unbounded) and must never leak into output.
        huge_version = b'{"version": "x", "big": ' + HUGE_DIGITS + b"}"
        _ = self._install(_healthy_runtime(version=huge_version))
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "ok")
        self.assertNotIn(HUGE_DIGITS[:32].decode(), _serialized(snapshot))

        huge_tags = (
            b'{"models": [{"name": "test-model:latest", "digest": "'
            + DIGEST_ZERO.encode()
            + b'", "future_size": '
            + HUGE_DIGITS
            + b"}]}"
        )
        _ = self._install(_healthy_runtime(tags=huge_tags))
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "ok")  # model present by name
        self.assertNotIn(HUGE_DIGITS[:32].decode(), _serialized(snapshot))

        # On ps the huge integer occupies the validated context_length
        # slot: it is a positive integer, so it is deterministic evidence
        # (the value itself is the normalized fact, not a leak).
        huge_ps = (
            b'{"models": [{"name": "test-model:latest", "digest": "'
            + DIGEST_ZERO.encode()
            + b'", "context_length": '
            + HUGE_DIGITS
            + b"}]}"
        )
        _ = self._install(_healthy_runtime(ps=huge_ps))
        snapshot = self._collect(configured_context_tokens=8192)
        self.assertEqual(snapshot.status, "ok")
        local = snapshot.local_runtime
        assert local is not None
        self.assertEqual(local.effective_context_tokens, HUGE_INT)


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
            _http_error(500),
            _FakeResponse(b"A" * (ollama_acquisition.MAX_BODY_BYTES + 1)),
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
            urllib.error.URLError("name resolution failed"),
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
                runtime = self._install(
                    _healthy_runtime(version=_http_error(code))
                )
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

    def test_oversized_probe_maps_to_unknown(self) -> None:
        _ = self._install(
            _healthy_runtime(
                version=_FakeResponse(
                    b"A" * (ollama_acquisition.MAX_BODY_BYTES + 1)
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
            b"",
        ):
            with self.subTest(body=body[:20]):
                runtime = self._install(_healthy_runtime(version=body))
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
        _ = self._install(_healthy_runtime(tags=_http_error(503)))
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


class CollectionDeadline(_AcquisitionCase):
    def test_trickling_probe_aborts_at_deadline(self) -> None:
        # A peer that trickles bytes forever cannot extend the collection:
        # the monotonic deadline aborts the read and normalizes the outcome.
        trickling = _TricklingResponse()
        runtime = self._install(_healthy_runtime(version=trickling))
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", 0.3
        ):
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
        self.assertEqual(runtime.requested_paths(), ["/api/version"])
        self.assertTrue(trickling.closed)
        self._assert_safe_serialization(snapshot)

    def test_trickling_tags_read_preserves_reachability(self) -> None:
        trickling = _TricklingResponse()
        _ = self._install(_healthy_runtime(tags=trickling))
        with mock.patch.object(
            ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", 0.3
        ):
            snapshot = self._collect()
        self.assertEqual(snapshot.status, "unavailable")
        local = snapshot.local_runtime
        assert local is not None
        self.assertTrue(local.reachable)  # validated by the probe
        self.assertEqual(local.model_presence, "unknown")
        self.assertTrue(trickling.closed)

    def test_expired_deadline_makes_no_transport_calls(self) -> None:
        runtime = self._install(_healthy_runtime())
        with mock.patch.object(ollama_acquisition, "COLLECTION_DEADLINE_SECONDS", 0.0):
            snapshot = self._collect()
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual(runtime.calls, [])
        self._assert_no_output()

    def test_http_error_response_closed_without_reading_body(self) -> None:
        # The error body carries a sentinel; the position recorded at the
        # deterministic close proves it was never read, and exactly one
        # close happened.
        fp = _TrackingBytesIO(b"TEST_ONLY_ERROR_BODY_MUST_NOT_BE_READ")
        _ = self._install(
            _healthy_runtime(version=_http_error_with_fp(500, fp))
        )
        snapshot = self._collect()
        self.assertEqual(snapshot.status, "unknown")
        self.assertEqual(fp.close_count, 1)
        self.assertEqual(fp.position_at_close, 0)
        self._assert_no_output()


# ═══════════════════════ transport mechanism ═════════════════════════════════


class _OpenerRecorder:
    """Typed fake opener recording exactly one ``open`` call."""

    sentinel: object
    opened: list[tuple[urllib.request.Request, float]]

    def __init__(self, sentinel: object) -> None:
        self.sentinel = sentinel
        self.opened = []

    def open(self, request: urllib.request.Request, timeout: float) -> object:
        self.opened.append((request, timeout))
        return self.sentinel


class TransportMechanism(unittest.TestCase):
    def test_open_response_disables_proxies_and_redirects(self) -> None:
        request = urllib.request.Request(ENDPOINT + "/api/version")
        recorder = _OpenerRecorder(sentinel=object())

        with mock.patch.object(
            urllib.request, "build_opener", return_value=recorder
        ) as build_opener:
            returned = ollama_acquisition.open_response(
                request, ollama_acquisition.TIMEOUT_SECONDS
            )

        self.assertIs(returned, recorder.sentinel)
        build_opener.assert_called_once()
        handlers = cast("tuple[object, ...]", build_opener.call_args.args)
        kinds = [type(handler).__name__ for handler in handlers]
        self.assertIn("NoRedirect", kinds)
        self.assertIn("ProxyHandler", kinds)
        proxy_handler = next(
            handler
            for handler in handlers
            if type(handler).__name__ == "ProxyHandler"
        )
        # typeshed exposes no annotated ``proxies`` member; assert through
        # a bounded getattr instead of skipping the security-relevant check.
        proxies = cast("dict[str, object]", getattr(proxy_handler, "proxies"))
        self.assertEqual(proxies, {})
        # Exactly one call, with the original request object.
        self.assertEqual(
            recorder.opened, [(request, ollama_acquisition.TIMEOUT_SECONDS)]
        )
        self.assertIs(recorder.opened[0][0], request)

    def test_no_redirect_declines_and_constructs_nothing(self) -> None:
        handler = ollama_acquisition.NoRedirect()
        original = urllib.request.Request(ENDPOINT + "/api/version")
        headers = HTTPMessage()
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.assertIsNone(
                    handler.redirect_request(
                        original,
                        io.BytesIO(b""),
                        code,
                        "Redirect",
                        headers,
                        "http://TEST_ONLY_REDIRECT_TARGET/x",
                    )
                )


class RequestShape(_AcquisitionCase):
    def test_requests_target_canonical_paths_with_bounded_timeout(self) -> None:
        runtime = self._install(_healthy_runtime())
        _ = self._collect(configured_context_tokens=8192)

        self.assertEqual(len(runtime.calls), 3)
        for url, timeout in runtime.calls:
            self.assertIn(
                url,
                (
                    BASE + "/api/version",
                    BASE + "/api/tags",
                    BASE + "/api/ps",
                ),
            )
            self.assertEqual(timeout, ollama_acquisition.TIMEOUT_SECONDS)
            self.assertGreater(timeout, 0)

    def test_headers_carried_by_requests(self) -> None:
        recorded: list[urllib.request.Request] = []

        def spy(
            request: urllib.request.Request, timeout: float
        ) -> _FakeResponse:
            _ = timeout
            recorded.append(request)
            return _FakeResponse(b"{}")

        patcher = mock.patch.object(ollama_acquisition, "open_response", spy)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        _ = self._collect()

        self.assertEqual(len(recorded), 1)
        request = recorded[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertIsNone(request.get_header("Authorization"))


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
            "probe-http-error": _healthy_runtime(version=_http_error(500)),
            "probe-oversized": _healthy_runtime(
                version=_FakeResponse(
                    b"A" * (ollama_acquisition.MAX_BODY_BYTES + 1)
                )
            ),
            "probe-malformed": _healthy_runtime(version=b"not json"),
            "tags-drift": _healthy_runtime(tags=_fixture("tags-schema-changed.json")),
            "tags-duplicates": _healthy_runtime(
                tags=_fixture("tags-duplicate-names.json")
            ),
            "model-missing": _healthy_runtime(tags=_fixture("tags-missing.json")),
            "ps-drift": _healthy_runtime(ps=_fixture("ps-schema-changed.json")),
            "ps-digest-mismatch": _healthy_runtime(
                ps=_fixture("ps-digest-mismatch.json")
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
