"""Application tests for the unified read-only status surface."""

from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone
from typing import cast
from unittest import mock

from scarcity_router import (
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
    LocalRuntime,
)
from scarcity_router.status import (
    OLLAMA_CONTEXT_ENV,
    OLLAMA_MODEL_ENV,
    OllamaConfiguration,
    StatusCollectors,
    collect_status,
    main,
    render_human,
    render_json,
    resolve_ollama_configuration,
)

RETRIEVED_AT = "2026-09-05T09:00:00.123Z"
MODEL = "test-model:latest"


def _window(
    kind: str,
    *,
    resource: str = "tokens",
    used: int | None = 35,
    remaining: int | None = 65,
    reset: str | None = "2026-09-05T12:00:00.000Z",
    window_id: str | None = None,
) -> CapacityWindow:
    return CapacityWindow(
        resource=resource,
        kind=kind,
        duration_seconds={"five_hour": 18_000, "weekly": 604_800}.get(kind),
        used_percent=used,
        remaining_percent=remaining,
        resets_at=reset,
        window_id=window_id,
    )


def _snapshot(
    provider: str,
    status: str = "ok",
    *,
    windows: tuple[CapacityWindow, ...] = (),
    diagnostics: tuple[CapacityDiagnostic, ...] = (),
    plan: str | None = None,
    local_runtime: LocalRuntime | None = None,
) -> CapacitySnapshot:
    return CapacitySnapshot(
        schema_version=1,
        provider=provider,
        source={
            "openai": "codex_app_server",
            "zai": "zai_usage_endpoint",
            "ollama": "ollama_local",
        }[provider],
        retrieved_at=RETRIEVED_AT,
        status=status,
        windows=windows,
        diagnostics=diagnostics,
        plan=plan,
        local_runtime=local_runtime,
    )


def _healthy_snapshots() -> tuple[CapacitySnapshot, CapacitySnapshot, CapacitySnapshot]:
    return (
        _snapshot(
            "openai",
            windows=(_window("five_hour"), _window("weekly")),
            plan="plus",
        ),
        _snapshot(
            "zai",
            windows=(_window("five_hour", used=2, remaining=98, window_id="tokens_limit-3-5"),),
            plan="pro",
        ),
        _snapshot(
            "ollama",
            local_runtime=LocalRuntime(
                reachable=True,
                model_presence="present",
                model_name=MODEL,
                configured_context_tokens=8192,
                effective_context_tokens=16384,
            ),
        ),
    )


class _FakeCollectors:
    def __init__(self, snapshots: tuple[CapacitySnapshot, ...]) -> None:
        self.snapshots: dict[str, CapacitySnapshot] = {
            snapshot.provider: snapshot for snapshot in snapshots
        }
        self.calls: list[tuple[str, str, str | None, int | None]] = []

    def openai(self, *, retrieved_at: str) -> CapacitySnapshot:
        self.calls.append(("openai", retrieved_at, None, None))
        return self.snapshots["openai"]

    def zai(self, *, retrieved_at: str) -> CapacitySnapshot:
        self.calls.append(("zai", retrieved_at, None, None))
        return self.snapshots["zai"]

    def ollama(
        self,
        *,
        retrieved_at: str,
        model_name: str,
        endpoint: str,
        configured_context_tokens: int | None,
    ) -> CapacitySnapshot:
        self.calls.append(
            ("ollama", retrieved_at, model_name, configured_context_tokens)
        )
        self.assert_no_model_request(endpoint)
        return self.snapshots["ollama"]

    @staticmethod
    def assert_no_model_request(endpoint: str) -> None:
        if endpoint != "http://127.0.0.1:11434":
            raise AssertionError("unexpected endpoint")


def _collector_set(
    snapshots: tuple[CapacitySnapshot, ...],
) -> tuple[StatusCollectors, _FakeCollectors]:
    fakes = _FakeCollectors(snapshots)
    return (
        StatusCollectors(
            openai=fakes.openai,
            zai=fakes.zai,
            ollama=fakes.ollama,
        ),
        fakes,
    )


class StatusApplicationTests(unittest.TestCase):
    def test_all_providers_are_collected_in_order_with_one_timestamp(self) -> None:
        snapshots = _healthy_snapshots()
        collectors, fakes = _collector_set(snapshots)
        clock_calls = 0

        def clock() -> datetime:
            nonlocal clock_calls
            clock_calls += 1
            return datetime(2026, 9, 5, 9, 0, 0, 123456, tzinfo=timezone.utc)

        result = collect_status(
            ollama=OllamaConfiguration(model_name=MODEL),
            collectors=collectors,
            clock=clock,
        )

        self.assertEqual(result, snapshots)
        self.assertEqual(clock_calls, 1)
        self.assertEqual([call[0] for call in fakes.calls], ["openai", "zai", "ollama"])
        self.assertEqual({call[1] for call in fakes.calls}, {RETRIEVED_AT})
        self.assertEqual(fakes.calls[-1][2:], (MODEL, None))

    def test_openai_unavailable_does_not_suppress_other_providers(self) -> None:
        healthy = _healthy_snapshots()
        snapshots = (
            _snapshot(
                "openai",
                "unavailable",
                diagnostics=(CapacityDiagnostic("source_unavailable"),),
            ),
            healthy[1],
            healthy[2],
        )
        collectors, fakes = _collector_set(snapshots)
        result = collect_status(
            ollama=OllamaConfiguration(model_name=MODEL), collectors=collectors
        )
        self.assertEqual([snapshot.status for snapshot in result], ["unavailable", "ok", "ok"])
        self.assertEqual(len(fakes.calls), 3)

    def test_zai_auth_required_does_not_suppress_other_providers(self) -> None:
        healthy = _healthy_snapshots()
        snapshots = (
            healthy[0],
            _snapshot(
                "zai",
                "auth_required",
                diagnostics=(CapacityDiagnostic("auth_required"),),
            ),
            healthy[2],
        )
        collectors, _ = _collector_set(snapshots)
        result = collect_status(
            ollama=OllamaConfiguration(model_name=MODEL), collectors=collectors
        )
        self.assertEqual([snapshot.status for snapshot in result], ["ok", "auth_required", "ok"])

    def test_ollama_unavailable_does_not_suppress_cloud_providers(self) -> None:
        healthy = _healthy_snapshots()
        snapshots = (
            healthy[0],
            healthy[1],
            _snapshot(
                "ollama",
                "unavailable",
                diagnostics=(
                    CapacityDiagnostic("source_unavailable"),
                    CapacityDiagnostic("runtime_unreachable"),
                ),
                local_runtime=LocalRuntime(
                    reachable=False,
                    model_presence="unknown",
                    model_name=MODEL,
                ),
            ),
        )
        collectors, _ = _collector_set(snapshots)
        result = collect_status(
            ollama=OllamaConfiguration(model_name=MODEL), collectors=collectors
        )
        self.assertEqual([snapshot.status for snapshot in result], ["ok", "ok", "unavailable"])

    def test_multiple_degraded_providers_remain_data(self) -> None:
        snapshots = (
            _snapshot(
                "openai",
                "schema_changed",
                diagnostics=(CapacityDiagnostic("schema_changed"),),
            ),
            _snapshot(
                "zai",
                "unknown",
                diagnostics=(CapacityDiagnostic("telemetry_unknown"),),
            ),
            _snapshot(
                "ollama",
                "unknown",
                diagnostics=(
                    CapacityDiagnostic("telemetry_unknown"),
                    CapacityDiagnostic("effective_context_unknown"),
                ),
                local_runtime=LocalRuntime(
                    reachable=True,
                    model_presence="present",
                    model_name=MODEL,
                    configured_context_tokens=8192,
                ),
            ),
        )
        collectors, _ = _collector_set(snapshots)
        result = collect_status(
            ollama=OllamaConfiguration(model_name=MODEL), collectors=collectors
        )
        self.assertEqual(
            [snapshot.status for snapshot in result],
            ["schema_changed", "unknown", "unknown"],
        )

    def test_ollama_configuration_resolution_is_cli_over_environment(self) -> None:
        config = resolve_ollama_configuration(
            model_name="cli-model:latest",
            endpoint=None,
            configured_context_tokens=None,
            environment={
                OLLAMA_MODEL_ENV: "env-model:latest",
                OLLAMA_CONTEXT_ENV: "4096",
            },
        )
        self.assertEqual(config.model_name, "cli-model:latest")
        self.assertEqual(config.configured_context_tokens, 4096)
        self.assertEqual(config.endpoint, "http://127.0.0.1:11434")

    def test_invalid_ollama_configuration_is_rejected_before_collection(self) -> None:
        with self.assertRaisesRegex(ValueError, "model is not configured"):
            _ = resolve_ollama_configuration(
                model_name=None,
                endpoint=None,
                configured_context_tokens=None,
                environment={},
            )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            _ = resolve_ollama_configuration(
                model_name=MODEL,
                endpoint=None,
                configured_context_tokens=None,
                environment={OLLAMA_CONTEXT_ENV: "not-a-number"},
            )

    def test_missing_and_present_ollama_states_show_no_fake_quota(self) -> None:
        present = _healthy_snapshots()[2]
        missing = _snapshot(
            "ollama",
            "unavailable",
            diagnostics=(
                CapacityDiagnostic("source_unavailable"),
                CapacityDiagnostic("model_missing"),
            ),
            local_runtime=LocalRuntime(
                reachable=True,
                model_presence="missing",
                model_name=MODEL,
                configured_context_tokens=8192,
            ),
        )
        for snapshot, expected in ((present, "presence=present"), (missing, "presence=missing")):
            with self.subTest(snapshot=snapshot):
                text = render_human((snapshot,))
                self.assertIn(expected, text)
                self.assertNotIn("remaining=", text)
                self.assertNotIn("quota", text.lower())

    def test_effective_context_unknown_is_explicit(self) -> None:
        snapshot = _snapshot(
            "ollama",
            "ok",
            diagnostics=(CapacityDiagnostic("effective_context_unknown"),),
            local_runtime=LocalRuntime(
                reachable=True,
                model_presence="present",
                model_name=MODEL,
                configured_context_tokens=163840,
            ),
        )
        text = render_human((snapshot,))
        self.assertIn("configured_context=163840", text)
        self.assertIn("effective_context=unknown", text)
        self.assertIn("diagnostics=effective_context_unknown", text)


class StatusRenderingTests(unittest.TestCase):
    def test_exhausted_and_unknown_windows_are_honest_and_deterministic(self) -> None:
        exhausted = _window(
            "weekly",
            used=100,
            remaining=0,
            window_id="weekly-window",
        )
        unknown = _window(
            "unknown",
            used=None,
            remaining=None,
            reset=None,
            window_id="unknown-window",
        )
        snapshot = _snapshot("openai", windows=(exhausted, unknown))
        first = render_human((snapshot,))
        second = render_human(
            (_snapshot("openai", windows=(unknown, exhausted)),)
        )
        self.assertEqual(first, second)
        self.assertIn("kind=weekly resource=tokens used=100% remaining=0%", first)
        self.assertIn("kind=unknown resource=tokens used=unknown remaining=unknown reset=unknown", first)

    def test_diagnostics_and_cloud_plan_are_displayed_without_raw_data(self) -> None:
        snapshot = _snapshot(
            "zai",
            "auth_required",
            diagnostics=(CapacityDiagnostic("auth_required"),),
            plan="pro",
        )
        text = render_human((snapshot,))
        self.assertIn("Provider zai status=auth_required plan=pro", text)
        self.assertIn("diagnostics=auth_required", text)
        for forbidden in (
            "TEST_ONLY_SECRET",
            "Authorization",
            "/home/private",
            "provider response body",
        ):
            self.assertNotIn(forbidden, text)

    def test_json_is_a_deterministic_list_of_existing_snapshot_serializations(self) -> None:
        snapshots = _healthy_snapshots()
        encoded = render_json((snapshots[2], snapshots[1], snapshots[0]))
        parsed = cast(list[dict[str, object]], json.loads(encoded))
        self.assertEqual(parsed, [snapshot.to_dict() for snapshot in snapshots])
        self.assertEqual(encoded, render_json((snapshots[0], snapshots[1], snapshots[2])))

    def test_json_canonicalizes_zai_like_unordered_windows_and_diagnostics(self) -> None:
        weekly = _window(
            "weekly",
            used=40,
            remaining=60,
            window_id="tokens_limit-6-1",
        )
        unknown = _window(
            "unknown",
            used=None,
            remaining=None,
            reset=None,
            window_id="tokens_limit-7-1",
        )
        diagnostics = (
            CapacityDiagnostic("percentage_unknown", window_id="tokens_limit-7-1"),
            CapacityDiagnostic("reset_unknown", window_id="tokens_limit-7-1"),
            CapacityDiagnostic(
                "window_semantics_unknown", window_id="tokens_limit-7-1"
            ),
        )
        first = _snapshot(
            "zai",
            windows=(weekly, unknown),
            diagnostics=diagnostics,
            plan="pro",
        )
        second = _snapshot(
            "zai",
            windows=(unknown, weekly),
            diagnostics=tuple(reversed(diagnostics)),
            plan="pro",
        )

        self.assertEqual(render_json((first,)), render_json((second,)))

    def test_degraded_provider_exit_code_is_zero(self) -> None:
        snapshots = (
            _snapshot(
                "openai",
                "unavailable",
                diagnostics=(CapacityDiagnostic("source_unavailable"),),
            ),
            _snapshot(
                "zai",
                "auth_required",
                diagnostics=(CapacityDiagnostic("auth_required"),),
            ),
            _snapshot(
                "ollama",
                "unknown",
                diagnostics=(CapacityDiagnostic("telemetry_unknown"),),
                local_runtime=LocalRuntime(
                    reachable=True,
                    model_presence="unknown",
                    model_name=MODEL,
                ),
            ),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "scarcity_router.status.collect_status", return_value=snapshots
        ):
            exit_code = main(
                ["status", "--ollama-model", MODEL],
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("status=auth_required", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_invalid_cli_configuration_is_nonzero_and_collects_nothing(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("scarcity_router.status.collect_status") as collect:
            exit_code = main(
                ["status"],
                environment={},
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 2)
        collect.assert_not_called()
        self.assertIn("configuration error:", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    _ = unittest.main()
