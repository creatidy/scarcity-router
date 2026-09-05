"""Contract tests for the small synchronous Ollama acquisition adapter."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from collections.abc import Mapping
from typing import final
from unittest import mock

from scarcity_router import CapacitySnapshot, LocalRuntime
from scarcity_router.providers import ollama_acquisition

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ollama-local"
MODEL = "test-model:latest"
OTHER_MODEL = "other-model:1b"
DIGEST_ZERO = "sha256:" + "0" * 64
RETRIEVED_AT = "2026-09-04T12:00:00.000Z"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _runtime(snapshot: CapacitySnapshot) -> LocalRuntime:
    runtime = snapshot.local_runtime
    assert runtime is not None
    return runtime


@final
class _FakeResponse:
    def __init__(
        self,
        status: object,
        body: object = b"",
        read_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.read_error = read_error
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> object:
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        if isinstance(self.body, bytes) and size >= 0:
            return self.body[:size]
        return self.body


@final
class _FakeConnection:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[tuple[str, str, Mapping[str, str]]] = []
        self.close_calls = 0

    def request(
        self, method: str, path: str, /, *, headers: Mapping[str, str]
    ) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> object:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        self.close_calls += 1


@final
class _FakeHTTPConnection:
    def __init__(self, responses: Mapping[str, object]) -> None:
        self.responses = responses
        self.connections: list[_FakeConnection] = []
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, host: str, port: int, *, timeout: float) -> _FakeConnection:
        self.calls.append((host, port, timeout))
        response = tuple(self.responses.values())[len(self.connections)]
        connection = _FakeConnection(response)
        self.connections.append(connection)
        return connection


class AcquisitionContract(unittest.TestCase):
    def _collect(
        self,
        transport: _FakeHTTPConnection,
        *,
        model_name: str = MODEL,
        endpoint: str = ollama_acquisition.DEFAULT_ENDPOINT,
        configured_context_tokens: int | None = None,
    ) -> CapacitySnapshot:
        with mock.patch.object(ollama_acquisition, "HTTPConnection", transport):
            return ollama_acquisition.collect_ollama_capacity(
                retrieved_at=RETRIEVED_AT,
                model_name=model_name,
                endpoint=endpoint,
                configured_context_tokens=configured_context_tokens,
            )

    def test_healthy_sequence_timeout_cleanup_and_normalization(self) -> None:
        transport = _FakeHTTPConnection(
            {
                "/api/version": _FakeResponse(200, _fixture("version-ok.json")),
                "/api/tags": _FakeResponse(200, _fixture("tags-present.json")),
                "/api/ps": _FakeResponse(200, _fixture("ps-loaded.json")),
            }
        )
        snapshot = self._collect(transport, configured_context_tokens=8192)
        self.assertEqual(snapshot.status, "ok")
        runtime = _runtime(snapshot)
        self.assertTrue(runtime.reachable)
        self.assertEqual(runtime.model_presence, "present")
        self.assertEqual(runtime.configured_context_tokens, 8192)
        self.assertEqual(runtime.effective_context_tokens, 16384)
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(
            [(method, path) for connection in transport.connections for method, path, _ in connection.requests],
            [("GET", "/api/version"), ("GET", "/api/tags"), ("GET", "/api/ps")],
        )
        self.assertEqual(
            transport.calls,
            [("127.0.0.1", 11434, ollama_acquisition.TIMEOUT_SECONDS)] * 3,
        )
        self.assertEqual([c.close_calls for c in transport.connections], [1, 1, 1])

    def test_missing_model_is_distinct_and_skips_ps(self) -> None:
        transport = _FakeHTTPConnection(
            {
                "/api/version": _FakeResponse(200, _fixture("version-ok.json")),
                "/api/tags": _FakeResponse(200, _fixture("tags-missing.json")),
            }
        )
        snapshot = self._collect(transport)
        self.assertEqual(snapshot.status, "unavailable")
        runtime = _runtime(snapshot)
        self.assertEqual(runtime.model_presence, "missing")
        self.assertEqual(len(transport.connections), 2)
        self.assertEqual(getattr(transport.connections[1], "close_calls"), 1)

    def test_endpoint_policy_rejects_unsafe_overrides_before_io(self) -> None:
        self.assertEqual(
            ollama_acquisition.canonical_local_endpoint("http://[::1]/"),
            "http://[::1]:11434",
        )
        for endpoint in (
            "http://localhost:11434",
            "https://127.0.0.1:11434",
            "http://192.168.1.10:11434",
            "http://127.0.0.1:11434/api",
            "http://127.0.0.1:",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertFalse(ollama_acquisition.is_approved_local_endpoint(endpoint))

        transport = _FakeHTTPConnection({})
        with self.assertRaises(ValueError):
            _ = self._collect(transport, endpoint="http://localhost:11434")
        self.assertEqual(transport.calls, [])

    def test_probe_status_and_transport_failures_are_safe(self) -> None:
        for response in (
            _FakeResponse(503, b"SECRET_ERROR_BODY"),
            OSError("SECRET_TRANSPORT_ERROR"),
        ):
            with self.subTest(response=type(response).__name__):
                transport = _FakeHTTPConnection({"/api/version": response})
                snapshot = self._collect(transport)
                expected = "unknown" if isinstance(response, _FakeResponse) else "unavailable"
                self.assertEqual(snapshot.status, expected)
                self.assertEqual(_runtime(snapshot).model_presence, "unknown")
                if isinstance(response, _FakeResponse):
                    self.assertEqual(response.read_sizes, [])
                self.assertNotIn("SECRET", json.dumps(snapshot.to_dict()))

    def test_tags_transport_failure_preserves_reachability_but_not_presence(self) -> None:
        transport = _FakeHTTPConnection(
            {
                "/api/version": _FakeResponse(200, _fixture("version-ok.json")),
                "/api/tags": OSError("SECRET_TAGS_ERROR"),
            }
        )
        snapshot = self._collect(transport)
        self.assertEqual(snapshot.status, "unknown")
        self.assertTrue(_runtime(snapshot).reachable)
        self.assertEqual(_runtime(snapshot).model_presence, "unknown")

    def test_body_bound_and_strict_json_fail_closed(self) -> None:
        cases = (
            _FakeResponse(200, b"x" * (ollama_acquisition.MAX_BODY_BYTES + 1)),
            _FakeResponse(200, b'{"version":"v","version":"w"}'),
            _FakeResponse(200, b'{"version":"v","future":1e10000}'),
            _FakeResponse(200, "not bytes"),
        )
        for response in cases:
            with self.subTest(response=response.body):
                transport = _FakeHTTPConnection({"/api/version": response})
                snapshot = self._collect(transport)
                expected = "schema_changed" if isinstance(response.body, bytes) and len(response.body) <= ollama_acquisition.MAX_BODY_BYTES else "unknown"
                self.assertEqual(snapshot.status, expected)
                if isinstance(response.body, bytes):
                    self.assertEqual(response.read_sizes, [ollama_acquisition.MAX_BODY_BYTES + 1])

    def test_digest_agreement_controls_effective_context(self) -> None:
        transport = _FakeHTTPConnection(
            {
                "/api/version": _FakeResponse(200, _fixture("version-ok.json")),
                "/api/tags": _FakeResponse(200, _fixture("tags-present.json")),
                "/api/ps": _FakeResponse(200, _fixture("ps-digest-mismatch.json")),
            }
        )
        snapshot = self._collect(transport, configured_context_tokens=163840)
        self.assertEqual(snapshot.status, "unknown")
        self.assertEqual(_runtime(snapshot).configured_context_tokens, 163840)
        self.assertIsNone(_runtime(snapshot).effective_context_tokens)

    def test_ps_transport_failure_keeps_present_model_without_effective_context(self) -> None:
        transport = _FakeHTTPConnection(
            {
                "/api/version": _FakeResponse(200, _fixture("version-ok.json")),
                "/api/tags": _FakeResponse(200, _fixture("tags-present.json")),
                "/api/ps": OSError("SECRET_PS_ERROR"),
            }
        )
        snapshot = self._collect(transport)
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(_runtime(snapshot).model_presence, "present")
        self.assertIsNone(_runtime(snapshot).effective_context_tokens)

    def test_configuration_validation_does_not_restrict_consumed_parser_values(self) -> None:
        self.assertTrue(
            ollama_acquisition.is_approved_local_endpoint("http://127.0.0.1:11435")
        )
        transport = _FakeHTTPConnection({})
        with self.assertRaises(ValueError):
            _ = self._collect(transport, configured_context_tokens=0)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    _ = unittest.main()
