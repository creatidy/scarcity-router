"""Application tests for the unified OpenAI and Z.ai status surface."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest import mock

from scarcity_router import CapacityDiagnostic, CapacitySnapshot, CapacityWindow
from tests.observation import paired_observation
from scarcity_router.providers.openai_codex_acquisition import OpenAICodexObservation
from scarcity_router.status import (
    StatusCollectors,
    StatusObservation,
    build_parser,
    collect_status,
    main,
    render_human,
    render_json,
)

RETRIEVED_AT = "2026-09-05T09:00:00.123Z"


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
) -> CapacitySnapshot:
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source={
            "openai": "codex_app_server",
            "zai": "zai_usage_endpoint",
        }[provider],
        retrieved_at=RETRIEVED_AT,
        status=status,
        windows=windows,
        diagnostics=diagnostics,
        plan=plan,
    )


def _healthy_snapshots() -> tuple[CapacitySnapshot, CapacitySnapshot]:
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
    )


class _FakeCollectors:
    def __init__(self, snapshots: tuple[CapacitySnapshot, ...]) -> None:
        self.snapshots: dict[str, CapacitySnapshot] = {
            snapshot.provider: snapshot for snapshot in snapshots
        }
        self.calls: list[tuple[str, str]] = []

    def openai(self, *, retrieved_at: str) -> OpenAICodexObservation:
        self.calls.append(("openai", retrieved_at))
        return paired_observation(self.snapshots["openai"])

    def zai(self, *, retrieved_at: str) -> CapacitySnapshot:
        self.calls.append(("zai", retrieved_at))
        return self.snapshots["zai"]


def _collector_set(
    snapshots: tuple[CapacitySnapshot, ...],
) -> tuple[StatusCollectors, _FakeCollectors]:
    fakes = _FakeCollectors(snapshots)
    return (
        StatusCollectors(openai=fakes.openai, zai=fakes.zai),
        fakes,
    )


class ControlledHomeCompositionTests(unittest.TestCase):
    """The default OpenAI collector composes the D-066 credential location.

    When the installed worker service runs exactly one Codex source, the
    collector points the app-server at that source's controlled home —
    the same home ``make codex-login`` refreshes — so a login immediately
    feeds provider telemetry. An explicit ``CODEX_HOME``, and every
    ambiguous or absent configuration, keeps the inherited-environment
    behavior instead. Everything is asserted through the PUBLIC
    ``StatusCollectors().openai`` seam with the acquisition patched out
    (no binary is ever spawned); the resolved home is cross-checked
    against the adapter's real ``ControlledCodexHome`` so the spelled
    layout in ``status`` can never drift from its owner.
    """

    def _home_with_unit(self, base: Path, unit_text: str) -> Path:
        home = base / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        _ = (
            unit_dir / "scarcity-router-worker.service"
        ).write_text(unit_text, encoding="utf-8")
        return home

    def _generated_unit(self, state_dir: Path, sources: tuple[str, ...]) -> str:
        from scarcity_router import worker_service

        return worker_service.render_unit(
            executable=Path("/opt/tools/scarcity-router-worker"),
            state_dir=state_dir,
            selection=worker_service.ServiceSelection(
                codex_sources=sources
            ),
        )

    def _environment(self, home: Path) -> dict[str, str]:
        return {"HOME": str(home), "XDG_CONFIG_HOME": "", "PATH": "/usr/bin"}

    def _collected_codex_home(self, home: Path) -> object:
        """Run the default OpenAI collector seam; return captured kwargs."""
        from scarcity_router import status

        captured: dict[str, object] = {}

        def fake_collect(**kwargs: object) -> object:
            captured.update(kwargs)
            return paired_observation(_snapshot("openai"))

        with (
            mock.patch.dict(os.environ, self._environment(home), clear=True),
            mock.patch.object(
                status, "collect_openai_codex_capacity", fake_collect
            ),
        ):
            _ = StatusCollectors().openai(retrieved_at=RETRIEVED_AT)
        return captured.get("codex_home")

    def _controlled_home(self, state_dir: Path, source_id: str) -> str:
        from scarcity_router.worker_codex_adapter import ControlledCodexHome

        return str(
            ControlledCodexHome(state_dir, name=f"codex-sources/{source_id}").path
        )

    def test_one_configured_source_resolves_its_controlled_home(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            state_dir = Path(tmp) / "worker-state"
            home = self._home_with_unit(
                Path(tmp), self._generated_unit(state_dir, ("precision-codex-live",))
            )
            self.assertEqual(
                self._controlled_home(state_dir, "precision-codex-live"),
                self._collected_codex_home(home),
            )

    def test_a_hand_written_unit_resolves_the_same_way(self) -> None:
        # The Precision deployment shape: the targeted flag scan feeds the
        # capacity collector exactly as it feeds codex-login.
        unit = (
            "# hand-written\n[Service]\n"
            + "ExecStart=/usr/bin/env scarcity-router-worker run "
            + "--state-dir /home/u/worker-state "
            + "--codex-source precision-codex-live\n"
        )
        with tempfile.TemporaryDirectory[str]() as tmp:
            home = self._home_with_unit(Path(tmp), unit)
            self.assertEqual(
                self._controlled_home(
                    Path("/home/u/worker-state"), "precision-codex-live"
                ),
                self._collected_codex_home(home),
            )

    def test_an_explicit_codex_home_env_keeps_precedence(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            home = self._home_with_unit(
                Path(tmp),
                self._generated_unit(Path("/s"), ("precision-codex-live",)),
            )
            environment = self._environment(home)
            environment["CODEX_HOME"] = "/operator/override"

            from scarcity_router import status

            captured: dict[str, object] = {}

            def fake_collect(**kwargs: object) -> object:
                captured.update(kwargs)
                return paired_observation(_snapshot("openai"))

            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(
                    status, "collect_openai_codex_capacity", fake_collect
                ),
            ):
                _ = StatusCollectors().openai(retrieved_at=RETRIEVED_AT)
            self.assertIsNone(captured.get("codex_home"))

    def test_zero_multiple_missing_or_invalid_sources_resolve_none(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            base = Path(tmp)
            zero = self._home_with_unit(
                base, self._generated_unit(Path("/s"), ())
            )
            self.assertIsNone(self._collected_codex_home(zero))
            several = self._home_with_unit(
                base / "a",
                self._generated_unit(Path("/s"), ("one", "two")),
            )
            self.assertIsNone(self._collected_codex_home(several))
            missing = base / "b" / "home"
            missing.mkdir(parents=True)
            self.assertIsNone(self._collected_codex_home(missing))
            traversal = self._home_with_unit(
                base / "c",
                "# hand-written\n[Service]\n"
                + "ExecStart=/opt/w run --codex-source ../escape\n",
            )
            self.assertIsNone(self._collected_codex_home(traversal))

    def test_the_collector_passes_the_observation_timestamp_and_binary(self) -> None:
        from scarcity_router import status

        with tempfile.TemporaryDirectory[str]() as tmp:
            state_dir = Path(tmp) / "worker-state"
            home = self._home_with_unit(
                Path(tmp), self._generated_unit(state_dir, ("precision-codex-live",))
            )
            captured: dict[str, object] = {}

            def fake_collect(**kwargs: object) -> object:
                captured.update(kwargs)
                return paired_observation(_snapshot("openai"))

            with (
                mock.patch.dict(os.environ, self._environment(home), clear=True),
                mock.patch.object(
                    status, "collect_openai_codex_capacity", fake_collect
                ),
            ):
                observation = StatusCollectors().openai(
                    retrieved_at=RETRIEVED_AT
                )
            self.assertIsNotNone(observation)
            self.assertEqual(
                self._controlled_home(state_dir, "precision-codex-live"),
                captured.get("codex_home"),
            )
            self.assertEqual(RETRIEVED_AT, captured.get("retrieved_at"))
            self.assertIsNone(captured.get("binary_path"))


class StatusApplicationTests(unittest.TestCase):
    def test_supported_providers_are_collected_in_order_with_one_timestamp(self) -> None:
        snapshots = _healthy_snapshots()
        collectors, fakes = _collector_set(snapshots)
        clock_calls = 0

        def clock() -> datetime:
            nonlocal clock_calls
            clock_calls += 1
            return datetime(2026, 9, 5, 9, 0, 0, 123456, tzinfo=timezone.utc)

        result = collect_status(collectors=collectors, clock=clock)

        self.assertEqual(result.snapshots, snapshots)
        self.assertEqual(
            [report.provider for report in result.eligibility], ["openai"]
        )
        self.assertEqual(result.eligibility[0].state, "eligible")
        self.assertEqual(clock_calls, 1)
        self.assertEqual([call[0] for call in fakes.calls], ["openai", "zai"])
        self.assertEqual({call[1] for call in fakes.calls}, {RETRIEVED_AT})

    def test_openai_failure_does_not_suppress_zai(self) -> None:
        healthy = _healthy_snapshots()
        snapshots = (
            _snapshot(
                "openai",
                "unavailable",
                diagnostics=(CapacityDiagnostic("source_unavailable"),),
            ),
            healthy[1],
        )
        collectors, fakes = _collector_set(snapshots)
        result = collect_status(collectors=collectors)
        self.assertEqual(
            [snapshot.status for snapshot in result.snapshots], ["unavailable", "ok"]
        )
        self.assertEqual(len(fakes.calls), 2)

    def test_zai_failure_does_not_suppress_openai(self) -> None:
        healthy = _healthy_snapshots()
        snapshots = (
            healthy[0],
            _snapshot(
                "zai",
                "auth_required",
                diagnostics=(CapacityDiagnostic("auth_required"),),
            ),
        )
        collectors, _ = _collector_set(snapshots)
        result = collect_status(collectors=collectors)
        self.assertEqual(
            [snapshot.status for snapshot in result.snapshots], ["ok", "auth_required"]
        )

    def test_no_local_provider_can_be_injected_into_status_collectors(self) -> None:
        self.assertEqual(set(StatusCollectors.__dataclass_fields__), {"openai", "zai"})


class StatusRenderingTests(unittest.TestCase):
    def test_human_and_json_contain_exactly_openai_and_zai(self) -> None:
        snapshots = _healthy_snapshots()
        text = render_human((snapshots[1], snapshots[0]))
        encoded = render_json((snapshots[1], snapshots[0]))
        parsed = cast(list[dict[str, object]], json.loads(encoded))

        self.assertEqual(
            [line.split()[1] for line in text.splitlines() if line.startswith("Provider ")],
            ["openai", "zai"],
        )
        self.assertEqual([entry["provider"] for entry in parsed], ["openai", "zai"])
        self.assertEqual({entry["schema_version"] for entry in parsed}, {3})
        self.assertNotIn("ollama", text.lower())
        self.assertNotIn("local", text.lower())
        self.assertNotIn("ollama", encoded.lower())
        self.assertNotIn("local_runtime", encoded)

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
        second = render_human((_snapshot("openai", windows=(unknown, exhausted)),))
        self.assertEqual(first, second)
        self.assertIn("kind=weekly resource=tokens used=100% remaining=0%", first)
        self.assertIn(
            "kind=unknown resource=tokens used=unknown remaining=unknown reset=unknown",
            first,
        )

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
        encoded = render_json((snapshots[1], snapshots[0]))
        parsed = cast(list[dict[str, object]], json.loads(encoded))
        self.assertEqual(parsed, [snapshot.to_dict() for snapshot in snapshots])
        self.assertEqual(encoded, render_json((snapshots[0], snapshots[1])))

    def test_json_canonicalizes_unordered_windows_and_diagnostics(self) -> None:
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

    def test_known_scope_is_displayed_and_unknown_scope_is_omitted(self) -> None:
        with_scope = CapacityWindow(
            resource="tokens",
            kind="five_hour",
            scope_id="codex",
            duration_seconds=18_000,
            used_percent=35,
            remaining_percent=65,
            resets_at="2026-09-05T12:00:00.000Z",
            window_id="primary",
        )
        unscoped = _window("weekly", used=None, remaining=None, reset=None)
        snapshot = _snapshot("openai", windows=(with_scope, unscoped))
        text = render_human((snapshot,))
        self.assertIn("scope=codex", text)
        self.assertLess(text.index("scope=codex"), text.index("id=primary"))
        # Unknown scope stays absent — there is no placeholder scope string.
        self.assertIn("kind=weekly resource=tokens used=unknown", text)
        self.assertNotIn("scope=None", text)

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
        )
        stdout = io.StringIO()
        with mock.patch(
            "scarcity_router.status.collect_status",
            return_value=StatusObservation(
                snapshots=snapshots,
                eligibility=(),
            ),
        ):
            exit_code = main(["status"], stdout=stdout)
        self.assertEqual(exit_code, 0)
        self.assertIn("status=auth_required", stdout.getvalue())

    def test_ollama_cli_options_are_not_accepted(self) -> None:
        for option in (
            "--ollama-model",
            "--ollama-endpoint",
            "--ollama-context-tokens",
        ):
            with self.subTest(option=option):
                with self.assertRaises(SystemExit) as raised:
                    _ = build_parser().parse_args(["status", option, "value"])
                self.assertEqual(raised.exception.code, 2)

    def test_ollama_environment_variables_are_not_read(self) -> None:
        collectors, _ = _collector_set(_healthy_snapshots())
        stdout = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "SCARCITY_ROUTER_OLLAMA_MODEL": "should-not-be-read",
                "SCARCITY_ROUTER_OLLAMA_ENDPOINT": "http://127.0.0.1:11434",
                "SCARCITY_ROUTER_OLLAMA_CONTEXT_TOKENS": "8192",
            },
            clear=False,
        ):
            exit_code = main(
                ["status"],
                collectors=collectors,
                stdout=stdout,
            )
        self.assertEqual(exit_code, 0)
        self.assertNotIn("should-not-be-read", stdout.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
