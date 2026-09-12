"""M3b REST adapter tests (D-028/D-030).

Deterministic contract and integration tests for the loopback-only REST
server: synthetic collectors, a fixed clock and the repository artifacts.
No live provider quota and no model requests. Covers the frozen surface
(``/healthz``, ``/v1/status``, ``/v1/select``, ``/v1/simulate``), the strict
request-body rules, the frozen error semantics, transport routing
(404/405), security/runtime invariants and direct-application parity.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import cast

from scarcity_router import (
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
    SimulationOverrides,
    TaskRequirement,
)
from scarcity_router.cli import main as cli_main
from scarcity_router.selection_app import (
    load_configured_artifacts,
    select_from_inputs,
    simulate_from_inputs,
)
from scarcity_router.server import (
    MAX_REQUEST_BODY_BYTES,
    RestApplication,
    RestHTTPServer,
    build_parser,
    make_server,
)
from scarcity_router.status import Clock, StatusCollectors, collect_status

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"
POLICY_PATH = REPO / "model-policy.json"
FIXED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

# D-036 isolation: CLI runs in this module must never read or provision the
# host's ~/.config/scarcity-router. XDG_CONFIG_HOME points at a throwaway
# directory whose user config is the documented neutral policy, so every
# expectation keeps its pre-D-036 neutral behavior; default-config
# resolution has dedicated tests in tests/test_config.py.
_TMP_CONFIG_HOME = Path(tempfile.mkdtemp(prefix="scarcity-router-tests-"))
os.environ["XDG_CONFIG_HOME"] = str(_TMP_CONFIG_HOME)
_ = (_TMP_CONFIG_HOME / "scarcity-router").mkdir(mode=0o700)
_ = (_TMP_CONFIG_HOME / "scarcity-router" / "selector-policy.json").write_text(
    json.dumps(
        {
            "mode": "balanced",
            "resource_policy": {
                "policy_version": 1,
                "unknown_capacity_mode": "degraded",
                "replenishment_mode": "advisory",
                "reservations": [],
                "blackouts": [],
            },
        }
    ),
    encoding="utf-8",
)

_REQUIREMENT_L3 = {
    "task_level": "L3",
    "capability_minima": {"reasoning": 4, "coding": 5, "tool_use": 4},
    "hard_constraints": {
        "requires_tool_use": True,
        "requires_reasoning_mode": True,
    },
}
_REPLENISHMENT_STATE = {
    "provider": "openai",
    "kind": "rate_limit_reset",
    "available_count": 2,
    "details_known": True,
    "earliest_expiry": "2026-09-07T12:00:00.000Z",
    "retrieved_at": "2026-09-06T12:00:00.000Z",
}
_ZAI_WEEKLY_2 = {
    "capacity_percentages": [
        {
            "provider": "zai",
            "scope_id": "coding_plan",
            "resource": "tokens",
            "kind": "weekly",
            "remaining_percent": 2,
        }
    ]
}


def _snap(provider: str, five: int, weekly: int) -> CapacitySnapshot:
    scope = "codex" if provider == "openai" else "coding_plan"
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at="2026-09-06T12:00:00.000Z",
        status="ok",
        windows=(
            CapacityWindow(
                resource="tokens",
                kind="five_hour",
                scope_id=scope,
                duration_seconds=18_000,
                used_percent=100 - five,
                remaining_percent=five,
                window_id=f"{provider}-five",
            ),
            CapacityWindow(
                resource="tokens",
                kind="weekly",
                scope_id=scope,
                duration_seconds=604_800,
                used_percent=100 - weekly,
                remaining_percent=weekly,
                window_id=f"{provider}-weekly",
            ),
        ),
        diagnostics=(),
    )


def _unknown_snap(provider: str) -> CapacitySnapshot:
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at="2026-09-06T12:00:00.000Z",
        status="unknown",
        windows=(),
        diagnostics=(CapacityDiagnostic(code="telemetry_unknown"),),
    )


def _clock() -> datetime:
    return FIXED_AT


def _collectors(
    *,
    openai_five: int = 40,
    openai_weekly: int = 40,
    zai_five: int = 80,
    zai_weekly: int = 80,
) -> StatusCollectors:
    def openai(*, retrieved_at: str) -> CapacitySnapshot:
        _ = retrieved_at
        return _snap("openai", openai_five, openai_weekly)

    def zai(*, retrieved_at: str) -> CapacitySnapshot:
        _ = retrieved_at
        return _snap("zai", zai_five, zai_weekly)

    return StatusCollectors(openai=openai, zai=zai)


def _application(
    *,
    collectors: StatusCollectors | None = None,
    clock: Clock | None = _clock,
    catalog_path: Path | None = None,
    model_policy_path: Path | None = None,
) -> RestApplication:
    return RestApplication(
        catalog_path=catalog_path if catalog_path is not None else CATALOG_PATH,
        model_policy_path=(
            model_policy_path if model_policy_path is not None else POLICY_PATH
        ),
        collectors=collectors if collectors is not None else _collectors(),
        clock=clock,
    )


class _ServerHarness:
    """One loopback server on a kernel-assigned port for one test."""

    def __init__(self, application: RestApplication) -> None:
        self.server: RestHTTPServer = make_server(application, port=0)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        thread = self.thread
        if thread is not None:
            thread.join(timeout=5)

    @property
    def port(self) -> int:
        return cast("tuple[str, int]", self.server.server_address)[1]


def _request(
    harness: _ServerHarness,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One client request over a fresh loopback HTTP connection."""
    conn = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=10)
    try:
        conn.request(
            method, path, body=body, headers=dict(headers) if headers else {}
        )
        response = conn.getresponse()
        payload = response.read()
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        status = response.status
    finally:
        conn.close()
    return status, response_headers, payload


def _post_json(
    harness: _ServerHarness, path: str, payload: object
) -> tuple[int, dict[str, str], object]:
    status, headers, body = _request(
        harness,
        "POST",
        path,
        body=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return status, headers, json.loads(body)


def _select_on(harness: _ServerHarness, payload: object) -> dict[str, object]:
    status, _, response = _post_json(harness, "/v1/select", payload)
    if status != 200:
        raise AssertionError(f"expected 200, got {status}: {response!r}")
    envelope = cast("dict[str, object]", response)
    return cast("dict[str, object]", envelope["decision"])


def _error_payload(response: object) -> dict[str, object]:
    envelope = cast("dict[str, object]", response)
    return cast("dict[str, object]", envelope["error"])


def _error_code(response: object) -> object:
    return _error_payload(response)["code"]


def _parse(body: bytes) -> dict[str, object]:
    """Parse a response body; the frozen responses are always JSON objects."""
    return cast("dict[str, object]", json.loads(body))


def _raw_exchange(port: int, raw: bytes) -> tuple[int, bytes]:
    """Send pre-serialized request bytes and read the whole response.

    Used for transport rules http.client cannot express (duplicate or
    malformed Content-Length, Transfer-Encoding, missing Content-Length).
    """
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            response += chunk
    if not response:
        raise AssertionError("server closed the connection without a response")
    return int(response.split(b" ", 2)[1]), response


class ServerTestCase(unittest.TestCase):
    """Shared harness: one fresh serialized server per test."""

    def _serve(self, application: RestApplication | None = None) -> _ServerHarness:
        harness = _ServerHarness(
            application if application is not None else _application()
        )
        self.addCleanup(harness.close)
        harness.start()
        return harness

    def _get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        return _request(self._serve(), "GET", path)

    def _post(
        self, path: str, payload: object
    ) -> tuple[int, dict[str, str], object]:
        return _post_json(self._serve(), path, payload)

    def _select(self, payload: object) -> dict[str, object]:
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(200, status)
        envelope = cast("dict[str, object]", response)
        self.assertEqual({"schema_version", "decision"}, set(envelope))
        self.assertEqual(1, envelope["schema_version"])
        return cast("dict[str, object]", envelope["decision"])


class HealthTests(ServerTestCase):
    """``GET /healthz`` is process liveness only (D-028)."""

    def test_healthz_exact_shape(self) -> None:
        status, headers, body = self._get("/healthz")
        self.assertEqual(200, status)
        self.assertEqual("application/json; charset=utf-8", headers["content-type"])
        self.assertEqual({"status": "ok"}, json.loads(body))

    def test_healthz_invokes_no_collector_and_loads_no_artifact(self) -> None:
        def failing_openai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            raise AssertionError("healthz must not invoke collectors")

        def failing_zai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            raise AssertionError("healthz must not invoke collectors")

        application = _application(
            collectors=StatusCollectors(openai=failing_openai, zai=failing_zai),
            catalog_path=Path("/nonexistent/model-catalog.json"),
            model_policy_path=Path("/nonexistent/model-policy.json"),
        )
        status, _, body = _request(self._serve(application), "GET", "/healthz")
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, json.loads(body))


class StatusTests(ServerTestCase):
    """``GET /v1/status`` reuses the CLI collection semantics."""

    def test_status_envelope_with_both_snapshots(self) -> None:
        status, _, body = self._get("/v1/status")
        self.assertEqual(200, status)
        envelope = cast("dict[str, object]", json.loads(body))
        self.assertEqual({"schema_version", "snapshots"}, set(envelope))
        self.assertEqual(1, envelope["schema_version"])
        snapshots = cast("list[object]", envelope["snapshots"])
        self.assertEqual(2, len(snapshots))
        providers = [
            cast("dict[str, object]", snapshot)["provider"] for snapshot in snapshots
        ]
        self.assertEqual(["openai", "zai"], providers)
        for snapshot in snapshots:
            document = cast("dict[str, object]", snapshot)
            # The envelope version (1) and the capacity contract version (3)
            # are separate concepts and both appear as frozen (D-028).
            self.assertEqual(3, document["schema_version"])

    def test_status_provider_degradation_remains_data(self) -> None:
        def unknown_openai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            return _unknown_snap("openai")

        collectors = StatusCollectors(openai=unknown_openai, zai=_collectors().zai)
        harness = self._serve(_application(collectors=collectors))
        status, _, body = _request(harness, "GET", "/v1/status")
        self.assertEqual(200, status)
        envelope = cast("dict[str, object]", json.loads(body))
        snapshots = cast("list[dict[str, object]]", envelope["snapshots"])
        self.assertEqual(["openai", "zai"], [s["provider"] for s in snapshots])
        self.assertEqual("unknown", snapshots[0]["status"])
        self.assertEqual("ok", snapshots[1]["status"])

    def test_status_canonical_provider_ordering(self) -> None:
        def zai_first(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            return _snap("zai", 80, 80)

        def openai_second(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            return _snap("openai", 40, 40)

        collectors = StatusCollectors(openai=openai_second, zai=zai_first)
        harness = self._serve(_application(collectors=collectors))
        _, _, body = _request(harness, "GET", "/v1/status")
        envelope = cast("dict[str, object]", json.loads(body))
        snapshots = cast("list[dict[str, object]]", envelope["snapshots"])
        self.assertEqual(["openai", "zai"], [s["provider"] for s in snapshots])


class SelectTests(ServerTestCase):
    """``POST /v1/select`` frozen request semantics."""

    def test_profile_path(self) -> None:
        decision = self._select({"profile_id": "routine_coding"})
        self.assertEqual("routine_coding", decision["profile_id"])
        self.assertEqual(6, decision["profile_policy_version"])
        self.assertEqual("balanced", decision["selector_mode"])
        selected = cast("dict[str, object]", decision["selected"])
        identity = cast("dict[str, object]", selected["identity"])
        self.assertEqual("glm-5.3-flash", identity["model"])

    def test_explicit_requirement_path(self) -> None:
        decision = self._select({"requirement": _REQUIREMENT_L3})
        # The explicit path carries no profile provenance, and the frozen
        # serialization omits the absent profile id entirely.
        self.assertNotIn("profile_id", decision)
        selected = cast("dict[str, object]", decision["selected"])
        identity = cast("dict[str, object]", selected["identity"])
        self.assertEqual("glm-5.3", identity["model"])

    def test_valid_no_solution_is_http_200(self) -> None:
        payload = {
            "requirement": {
                "task_level": "L1",
                "capability_minima": {},
                "hard_constraints": {"minimum_output_tokens": 999_999},
            }
        }
        decision = self._select(payload)
        self.assertIsNone(decision["selected"])
        codes = cast("list[str]", decision["reason_codes"])
        self.assertIn("no_eligible_candidate", codes)

    def test_profile_with_tightening(self) -> None:
        decision = self._select(
            {
                "profile_id": "deep_coding",
                "tightening": {
                    "task_level": "L3",
                    "capability_minima": {},
                    "hard_constraints": {"requires_vision": True},
                },
            }
        )
        selected = cast("dict[str, object]", decision["selected"])
        identity = cast("dict[str, object]", selected["identity"])
        self.assertEqual("gpt-5.6-terra", identity["model"])
        self.assertEqual("medium", identity["variant"])

    def test_unknown_profile_is_invalid_request(self) -> None:
        status, _, response = self._post("/v1/select", {"profile_id": "no_such"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_both_sources_rejected(self) -> None:
        payload = {"profile_id": "routine_coding", "requirement": _REQUIREMENT_L3}
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_neither_source_rejected(self) -> None:
        status, _, response = self._post("/v1/select", {})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_selector_policy_null_is_neutral(self) -> None:
        harness = self._serve()
        without = _select_on(harness, {"profile_id": "routine_coding"})
        with_null = _select_on(
            harness, {"profile_id": "routine_coding", "selector_policy": None}
        )
        self.assertEqual(without, with_null)
        self.assertEqual("balanced", with_null["selector_mode"])

    def test_replenishment_missing_and_empty_mean_none(self) -> None:
        harness = self._serve()
        baseline = _select_on(harness, {"profile_id": "routine_coding"})
        empty = _select_on(
            harness, {"profile_id": "routine_coding", "replenishment_states": []}
        )
        self.assertEqual(baseline, empty)

    def test_replenishment_states_null_is_invalid_request(self) -> None:
        payload = {"profile_id": "routine_coding", "replenishment_states": None}
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_replenishment_entries_are_parsed(self) -> None:
        payload = {
            "profile_id": "routine_coding",
            "replenishment_states": [_REPLENISHMENT_STATE],
        }
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(200, status)
        decision = cast(
            "dict[str, object]", cast("dict[str, object]", response)["decision"]
        )
        # The parsed observation reaches the selector core and is visible as
        # replenishment provenance on the openai-scope candidates.
        self.assertIn("rate_limit_reset", json.dumps(decision))

    def test_invalid_replenishment_entry_rejected(self) -> None:
        broken = {**_REPLENISHMENT_STATE, "available_count": -1}
        payload = {
            "profile_id": "routine_coding",
            "replenishment_states": [broken],
        }
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_duplicate_replenishment_states_rejected(self) -> None:
        payload = {
            "profile_id": "routine_coding",
            "replenishment_states": [_REPLENISHMENT_STATE, _REPLENISHMENT_STATE],
        }
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_unknown_top_level_key_rejected(self) -> None:
        payload = {"profile_id": "routine_coding", "bogus": 1}
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_schema_invalid_requirement_rejected(self) -> None:
        payload: dict[str, object] = {
            "requirement": {
                "task_level": "L9",
                "capability_minima": {},
                "hard_constraints": {},
            }
        }
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_tightening_with_explicit_requirement_rejected(self) -> None:
        payload = {"requirement": _REQUIREMENT_L3, "tightening": _REQUIREMENT_L3}
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_non_monotone_tightening_rejected(self) -> None:
        payload: dict[str, object] = {
            "profile_id": "deep_coding",
            "tightening": {
                "task_level": "L1",
                "capability_minima": {},
                "hard_constraints": {},
            },
        }
        status, _, response = self._post("/v1/select", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))


class StrictJsonTests(ServerTestCase):
    """Frozen strict request parsing (D-028) and transport body rules."""

    def _post_raw(
        self, raw: bytes, *, content_type: str | None = "application/json"
    ) -> tuple[int, dict[str, str], bytes]:
        headers: dict[str, str] = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return _request(
            self._serve(), "POST", "/v1/select", body=raw, headers=headers
        )

    def test_duplicate_object_key_rejected(self) -> None:
        raw = b'{"profile_id": "routine_coding", "profile_id": "x"}'
        status, _, response = self._post_raw(raw)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_nan_rejected(self) -> None:
        status, _, response = self._post_raw(b'{"profile_id": NaN}')
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_infinity_rejected(self) -> None:
        status, _, response = self._post_raw(b'{"profile_id": Infinity}')
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_negative_infinity_rejected(self) -> None:
        status, _, response = self._post_raw(b'{"profile_id": -Infinity}')
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_malformed_json_rejected(self) -> None:
        status, _, response = self._post_raw(b'{"profile_id": "x"')
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_non_object_top_level_rejected(self) -> None:
        for raw in (b"[]", b'"text"', b"5", b"null"):
            status, _, response = self._post_raw(raw)
            self.assertEqual(400, status)
            self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_invalid_utf8_rejected(self) -> None:
        status, _, response = self._post_raw(b'{"profile_id": "\xff\xfe"}')
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_wrong_content_type_rejected(self) -> None:
        status, _, response = self._post_raw(b"{}", content_type="text/plain")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_missing_content_type_rejected(self) -> None:
        status, _, response = self._post_raw(b"{}", content_type=None)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_non_utf8_charset_rejected(self) -> None:
        status, _, response = self._post_raw(
            b"{}", content_type="application/json; charset=utf-16"
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_utf8_charset_accepted(self) -> None:
        status, _, _ = self._post_raw(
            b'{"profile_id": "routine_coding"}',
            content_type="application/json; charset=utf-8",
        )
        self.assertEqual(200, status)

    def test_oversized_body_rejected(self) -> None:
        body = b"x" * (MAX_REQUEST_BODY_BYTES + 1)
        status, _, response = self._post_raw(body)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(_parse(response)))

    def test_oversized_declared_body_rejected_without_full_body(self) -> None:
        harness = self._serve()
        head = (
            "POST /v1/select HTTP/1.0\r\n"
            + "Host: 127.0.0.1\r\n"
            + "Content-Type: application/json\r\n"
            + f"Content-Length: {MAX_REQUEST_BODY_BYTES + 1}\r\n\r\n"
        ).encode("ascii")
        code, _ = _raw_exchange(harness.port, head + b'{"partial": true}')
        self.assertEqual(400, code)

    def _raw_post(self, head_headers: str) -> int:
        harness = self._serve()
        head = f"POST /v1/select HTTP/1.0\r\nHost: 127.0.0.1\r\n{head_headers}\r\n"
        code, _ = _raw_exchange(harness.port, head.encode("ascii"))
        return code

    def test_duplicate_content_length_rejected(self) -> None:
        code = self._raw_post(
            "Content-Type: application/json\r\n"
            + "Content-Length: 2\r\nContent-Length: 2\r\n"
        )
        self.assertEqual(400, code)

    def test_malformed_content_length_rejected(self) -> None:
        code = self._raw_post(
            "Content-Type: application/json\r\nContent-Length: abc\r\n"
        )
        self.assertEqual(400, code)

    def test_pathological_content_length_rejected_before_integer_conversion(self) -> None:
        harness = self._serve()
        head = (
            "POST /v1/select HTTP/1.0\r\n"
            + "Host: 127.0.0.1\r\n"
            + "Content-Type: application/json\r\n"
            + "Content-Length: "
            + ("9" * 5000)
            + "\r\n\r\n"
        ).encode("ascii")
        code, response = _raw_exchange(harness.port, head)
        self.assertEqual(400, code)
        self.assertNotIn(b"9" * 100, response)

    def test_negative_content_length_rejected(self) -> None:
        code = self._raw_post(
            "Content-Type: application/json\r\nContent-Length: -5\r\n"
        )
        self.assertEqual(400, code)

    def test_missing_content_length_rejected(self) -> None:
        code = self._raw_post("Content-Type: application/json\r\n")
        self.assertEqual(400, code)

    def test_transfer_encoding_rejected(self) -> None:
        code = self._raw_post(
            "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
        )
        self.assertEqual(400, code)


class SimulationTests(ServerTestCase):
    """``POST /v1/simulate`` frozen request semantics."""

    def test_empty_overrides_is_valid(self) -> None:
        status, _, response = self._post(
            "/v1/simulate", {"profile_id": "deep_coding", "overrides": {}}
        )
        self.assertEqual(200, status)
        envelope = cast("dict[str, object]", response)
        self.assertEqual({"schema_version", "result"}, set(envelope))
        self.assertEqual(1, envelope["schema_version"])
        result = cast("dict[str, object]", envelope["result"])
        self.assertEqual({"baseline", "simulated", "applied_overrides"}, set(result))
        self.assertEqual(result["baseline"], result["simulated"])

    def test_overrides_required(self) -> None:
        status, _, response = self._post(
            "/v1/simulate", {"profile_id": "deep_coding"}
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))
        status, _, response = self._post(
            "/v1/simulate", {"profile_id": "deep_coding", "overrides": None}
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_nested_replenishment_null_retains_baseline(self) -> None:
        payload = {
            "profile_id": "routine_coding",
            "replenishment_states": [_REPLENISHMENT_STATE],
            "overrides": {"replenishment_states": None},
        }
        status, _, response = self._post("/v1/simulate", payload)
        self.assertEqual(200, status)
        result = cast(
            "dict[str, object]", cast("dict[str, object]", response)["result"]
        )
        applied = cast("dict[str, object]", result["applied_overrides"])
        self.assertNotIn("replenishment_states", applied)

    def test_nested_replenishment_empty_clears(self) -> None:
        payload: dict[str, object] = {
            "profile_id": "routine_coding",
            "replenishment_states": [_REPLENISHMENT_STATE],
            "overrides": {"replenishment_states": []},
        }
        status, _, response = self._post("/v1/simulate", payload)
        self.assertEqual(200, status)
        result = cast(
            "dict[str, object]", cast("dict[str, object]", response)["result"]
        )
        applied = cast("dict[str, object]", result["applied_overrides"])
        self.assertEqual([], applied["replenishment_states"])

    def test_invalid_override_rejected(self) -> None:
        broken_override: dict[str, object] = {
            "provider": "zai",
            "scope_id": "coding_plan",
            "resource": "tokens",
            "kind": "weekly",
            "remaining_percent": 101,
        }
        broken = {"capacity_percentages": [broken_override]}
        payload = {"profile_id": "deep_coding", "overrides": broken}
        status, _, response = self._post("/v1/simulate", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_semantically_missing_override_target_is_invalid_request(self) -> None:
        payload = {
            "profile_id": "deep_coding",
            "overrides": {
                "capacity_percentages": [
                    {
                        "provider": "zai",
                        "scope_id": "missing_plan",
                        "resource": "tokens",
                        "kind": "weekly",
                        "remaining_percent": 2,
                    }
                ]
            },
        }
        status, _, response = self._post("/v1/simulate", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_non_ok_override_target_is_invalid_request(self) -> None:
        def unknown_zai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            return _unknown_snap("zai")

        collectors = StatusCollectors(openai=_collectors().openai, zai=unknown_zai)
        harness = self._serve(_application(collectors=collectors))
        payload = {
            "profile_id": "deep_coding",
            "overrides": _ZAI_WEEKLY_2,
        }
        status, _, response = _post_json(harness, "/v1/simulate", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_unknown_top_level_key_rejected(self) -> None:
        payload: dict[str, object] = {"profile_id": "deep_coding", "overrides": {}, "bogus": 1}
        status, _, response = self._post("/v1/simulate", payload)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))

    def test_requirement_source_rules_apply(self) -> None:
        status, _, response = self._post("/v1/simulate", {"overrides": {}})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))
        both: dict[str, object] = {
            "profile_id": "deep_coding",
            "requirement": _REQUIREMENT_L3,
            "overrides": {},
        }
        status, _, response = self._post("/v1/simulate", both)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", _error_code(response))


class ErrorBoundaryTests(ServerTestCase):
    """500 semantics, 404/405 routing and the safe error envelope."""

    def test_malformed_catalog_is_internal_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "catalog.json"
            _ = broken.write_text("{not json", encoding="utf-8")
            harness = self._serve(_application(catalog_path=broken))
            status, _, body = _request(
                harness,
                "POST",
                "/v1/select",
                body=json.dumps({"profile_id": "routine_coding"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(500, status)
            error = _error_payload(_parse(body))
            self.assertEqual("internal_error", error["code"])
            self.assertEqual("internal server error", error["message"])
            # No local path, no traceback, no raw exception detail.
            rendered = body.decode("utf-8")
            self.assertNotIn(str(tmp), rendered)
            self.assertNotIn("Traceback", rendered)
            self.assertNotIn("not json", rendered)

    def test_unexpected_internal_failure_is_internal_error(self) -> None:
        def failing_openai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            raise RuntimeError("synthetic collector failure")

        collectors = StatusCollectors(openai=failing_openai, zai=_collectors().zai)
        harness = self._serve(_application(collectors=collectors))
        status, _, body = _request(harness, "GET", "/v1/status")
        self.assertEqual(500, status)
        error = _error_payload(_parse(body))
        self.assertEqual("internal_error", error["code"])
        self.assertNotIn("synthetic collector failure", json.dumps(error))

    def test_simulation_collector_failure_is_internal_error(self) -> None:
        def failing_openai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            raise RuntimeError("synthetic simulation collector failure")

        collectors = StatusCollectors(openai=failing_openai, zai=_collectors().zai)
        harness = self._serve(_application(collectors=collectors))
        status, _, response = _post_json(
            harness,
            "/v1/simulate",
            {"profile_id": "routine_coding", "overrides": {}},
        )
        self.assertEqual(500, status)
        error = _error_payload(response)
        self.assertEqual("internal_error", error["code"])
        self.assertNotIn("synthetic simulation collector failure", json.dumps(error))

    def test_unknown_routes_are_404(self) -> None:
        harness = self._serve()
        for path in ("/nope", "/v1/unknown", "/healthz?probe=1", "/v1/status?x=1"):
            status, _, body = _request(harness, "GET", path)
            self.assertEqual(404, status, path)
            self.assertEqual(b"", body)

    def test_unsupported_methods_are_405_not_501(self) -> None:
        harness = self._serve()
        cases: list[tuple[str, str, str]] = [
            ("HEAD", "/healthz", "GET"),
            ("PUT", "/healthz", "GET"),
            ("PATCH", "/v1/status", "GET"),
            ("DELETE", "/v1/select", "POST"),
            ("OPTIONS", "/v1/simulate", "POST"),
            ("POST", "/healthz", "GET"),
            ("GET", "/v1/select", "POST"),
        ]
        for method, path, allow in cases:
            status, headers, body = _request(harness, method, path)
            self.assertEqual(405, status, f"{method} {path}")
            self.assertEqual(allow, headers.get("allow"), f"{method} {path}")
            self.assertEqual(b"", body)

    def test_unknown_method_gets_no_html_body(self) -> None:
        harness = self._serve()
        code, response = _raw_exchange(
            harness.port,
            b"PROPFIND /v1/status HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n",
        )
        self.assertEqual(501, code)
        self.assertNotIn(b"<html", response.lower())


class SecurityRuntimeTests(ServerTestCase):
    """Loopback binding, serialization and no-leak invariants."""

    def test_binds_loopback_only(self) -> None:
        harness = self._serve()
        host = cast("tuple[str, int]", harness.server.server_address)[0]
        self.assertEqual("127.0.0.1", host)

    def test_accepts_bare_loopback_host(self) -> None:
        harness = self._serve()
        status, _, body = _request(
            harness,
            "GET",
            "/healthz",
            headers={"Host": "127.0.0.1"},
        )
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, json.loads(body))

    def test_accepts_bound_port_loopback_host(self) -> None:
        harness = self._serve()
        status, _, body = _request(
            harness,
            "GET",
            "/healthz",
            headers={"Host": f"127.0.0.1:{harness.port}"},
        )
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok"}, json.loads(body))

    def test_rejects_missing_foreign_and_duplicate_hosts(self) -> None:
        harness = self._serve()
        requests = (
            b"GET /healthz HTTP/1.0\r\n\r\n",
            (
                "GET /healthz HTTP/1.0\r\n"
                + f"Host: attacker.example:{harness.port}\r\n\r\n"
            ).encode("ascii"),
            (
                "GET /healthz HTTP/1.0\r\n"
                + "Host: 127.0.0.1\r\nHost: 127.0.0.1\r\n\r\n"
            ).encode("ascii"),
        )
        for raw in requests:
            code, response = _raw_exchange(harness.port, raw)
            self.assertEqual(400, code)
            body = response.split(b"\r\n\r\n", 1)[1]
            self.assertEqual("invalid_request", _error_code(_parse(body)))
            self.assertNotIn(b"attacker.example", response)

    def test_host_validation_precedes_each_endpoint(self) -> None:
        def failing_openai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            raise RuntimeError("endpoint must not be dispatched")

        collectors = StatusCollectors(openai=failing_openai, zai=_collectors().zai)
        harness = self._serve(_application(collectors=collectors))
        requests = (
            b"GET /healthz HTTP/1.0\r\nHost: attacker.example\r\n\r\n",
            b"GET /v1/status HTTP/1.0\r\nHost: attacker.example\r\n\r\n",
            (
                "POST /v1/select HTTP/1.0\r\n"
                + "Host: attacker.example\r\n"
                + "Content-Type: application/json\r\n"
                + "Content-Length: 31\r\n\r\n"
                + '{"profile_id":"routine_coding"}'
            ).encode("ascii"),
            (
                "POST /v1/simulate HTTP/1.0\r\n"
                + "Host: attacker.example\r\n"
                + "Content-Type: application/json\r\n"
                + "Content-Length: 46\r\n\r\n"
                + '{"profile_id":"routine_coding","overrides":{}}'
            ).encode("ascii"),
        )
        for raw in requests:
            code, response = _raw_exchange(harness.port, raw)
            self.assertEqual(400, code)
            body = response.split(b"\r\n\r\n", 1)[1]
            self.assertEqual("invalid_request", _error_code(_parse(body)))

    def test_no_bind_address_option_is_exposed(self) -> None:
        help_text = build_parser().format_help()
        for forbidden in ("--host", "--bind", "--listen-address", "0.0.0.0"):
            self.assertNotIn(forbidden, help_text)

    def test_request_body_is_never_logged(self) -> None:
        harness = self._serve()
        captured = io.StringIO()
        marker = "SECRET-BODY-MARKER-7f3a"
        payload = json.dumps({"profile_id": marker}).encode("utf-8")
        with contextlib.redirect_stderr(captured):
            # A valid request and a rejected request must both stay silent.
            _ = _request(
                harness,
                "POST",
                "/v1/select",
                body=payload,
                headers={"Content-Type": "application/json"},
            )
            _ = _request(
                harness,
                "POST",
                "/v1/select",
                body=b"{broken",
                headers={"Content-Type": "application/json"},
            )
        self.assertNotIn(marker, captured.getvalue())
        self.assertNotIn("{broken", captured.getvalue())

    def test_single_threaded_serialization_by_construction(self) -> None:
        harness = self._serve()
        self.assertNotIsInstance(harness.server, ThreadingHTTPServer)
        self.assertFalse(
            issubclass(type(harness.server), ThreadingMixIn),
            "server must not be threaded by construction",
        )
        self.assertIsInstance(harness.server, HTTPServer)


class ParityTests(ServerTestCase):
    """REST results equal direct typed-application results (D-028 parity)."""

    def test_rest_select_equals_direct_application(self) -> None:
        collectors = _collectors()
        harness = self._serve(_application(collectors=collectors))
        _, _, response = _post_json(harness, "/v1/select", {"profile_id": "deep_coding"})
        envelope = cast("dict[str, object]", response)
        catalog, profiles, version = load_configured_artifacts(
            CATALOG_PATH, POLICY_PATH
        )
        direct = select_from_inputs(
            catalog=catalog,
            profiles=profiles,
            profile_policy_version=version,
            profile_id="deep_coding",
            requirement=None,
            tightening=None,
            policy=None,
            replenishment_states=(),
            collectors=collectors,
            clock=_clock,
        )
        self.assertEqual(direct.to_dict(), envelope["decision"])

    def test_rest_simulate_equals_direct_application(self) -> None:
        collectors = _collectors()
        harness = self._serve(_application(collectors=collectors))
        payload = {"profile_id": "deep_coding", "overrides": _ZAI_WEEKLY_2}
        _, _, response = _post_json(harness, "/v1/simulate", payload)
        envelope = cast("dict[str, object]", response)
        catalog, profiles, version = load_configured_artifacts(
            CATALOG_PATH, POLICY_PATH
        )
        direct = simulate_from_inputs(
            catalog=catalog,
            profiles=profiles,
            profile_policy_version=version,
            profile_id="deep_coding",
            requirement=None,
            tightening=None,
            policy=None,
            replenishment_states=(),
            overrides=SimulationOverrides.from_dict(_ZAI_WEEKLY_2),
            collectors=collectors,
            clock=_clock,
        )
        self.assertEqual(direct.to_dict(), envelope["result"])

    def test_rest_status_equals_direct_collection(self) -> None:
        collectors = _collectors()
        harness = self._serve(_application(collectors=collectors))
        _, _, body = _request(harness, "GET", "/v1/status")
        envelope = cast("dict[str, object]", json.loads(body))
        snapshots = collect_status(collectors=collectors, clock=_clock)
        self.assertEqual(
            [snapshot.to_dict() for snapshot in snapshots],
            envelope["snapshots"],
        )

    def test_file_based_cli_matches_typed_seam(self) -> None:
        """PHASE 17 regression: file CLI and typed seam share one decision."""
        with tempfile.TemporaryDirectory() as tmp:
            requirement_path = Path(tmp) / "task.json"
            _ = requirement_path.write_text(
                json.dumps(_REQUIREMENT_L3), encoding="utf-8"
            )
            out = io.StringIO()
            code = cli_main(
                [
                    "select",
                    "--requirement",
                    str(requirement_path),
                    "--catalog",
                    str(CATALOG_PATH),
                    "--model-policy",
                    str(POLICY_PATH),
                    "--json",
                ],
                stdout=out,
                collectors=_collectors(),
                clock=_clock,
            )
            self.assertEqual(0, code)
            cli_decision = cast("dict[str, object]", json.loads(out.getvalue()))
            catalog, profiles, version = load_configured_artifacts(
                CATALOG_PATH, POLICY_PATH
            )
            direct = select_from_inputs(
                catalog=catalog,
                profiles=profiles,
                profile_policy_version=version,
                profile_id=None,
                requirement=TaskRequirement.from_dict(_REQUIREMENT_L3),
                tightening=None,
                policy=None,
                replenishment_states=(),
                collectors=_collectors(),
                clock=_clock,
            )
            self.assertEqual(direct.to_dict(), cli_decision)


if __name__ == "__main__":
    _ = unittest.main()
