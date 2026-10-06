"""Application tests for the unified OpenAI and Z.ai status surface."""

from __future__ import annotations

import io
import json
import os
import unittest
from dataclasses import replace
from contextlib import AbstractContextManager
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
from typing import cast, override
from unittest import mock

from scarcity_router import CapacityDiagnostic, CapacitySnapshot, CapacityWindow
from scarcity_router import status, worker_service
from scarcity_router import cli
from scarcity_router.eligibility import ExecutionEligibility
from scarcity_router.codex_home import ControlledCodexHome
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
    render_terminal,
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


class OpenAISourceSelectionTests(unittest.TestCase):
    """Actual unit parsing/home validation; only the provider boundary is fake."""

    root: Path = Path("/")
    unit: Path = Path("/")
    state: Path = Path("/")
    calls: list[tuple[str, Path | None, Path | None]] = []

    @override
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.unit = self.root / "worker.service"
        self.state = self.root / "custom state"
        environment = cast(
            AbstractContextManager[object],
            mock.patch.dict(os.environ, {"CODEX_HOME": "/ordinary/codex"}, clear=True),
        )
        _ = self.enterContext(environment)
        location = mock.patch.object(worker_service, "unit_install_path", return_value=self.unit)
        _ = location.start()
        self.addCleanup(location.stop)
        self.calls = []

        def collect(
            *, retrieved_at: str, binary_path: Path | None, codex_home: Path | None,
        ) -> OpenAICodexObservation:
            self.calls.append((retrieved_at, binary_path, codex_home))
            return paired_observation(_healthy_snapshots()[0])

        provider = mock.patch(
            "scarcity_router.status.collect_openai_codex_capacity",
            new=collect,
        )
        _ = provider.start()
        self.addCleanup(provider.stop)

    def _install(
        self, sources: tuple[str, ...] = ("test-source",), *, binary: str | None = None,
        generated: bool = True,
    ) -> None:
        text = worker_service.render_unit(
            executable=self.root / "scarcity-router-worker",
            state_dir=self.state,
            selection=worker_service.ServiceSelection(codex_sources=sources, codex_bin=binary),
        )
        if not generated:
            text = text.removeprefix(worker_service.UNIT_MARKER_LINE + "\n")
        _ = self.unit.write_text(text, encoding="utf-8")
        for source in sources:
            ControlledCodexHome(self.state, name=f"codex-sources/{source}").ensure()

    def _collect(self) -> OpenAICodexObservation:
        return StatusCollectors().openai(retrieved_at=RETRIEVED_AT)

    def test_single_source_uses_custom_home_for_generated_and_handwritten_units(self) -> None:
        for generated in (True, False):
            with self.subTest(generated=generated):
                self._install(generated=generated)
                with mock.patch.object(
                    ControlledCodexHome, "ensure",
                    side_effect=AssertionError("telemetry must not provision a home"),
                ):
                    result = self._collect()
                self.assertEqual(
                    self.calls[-1],
                    (RETRIEVED_AT, None, self.state / "codex-sources/test-source/codex-home"),
                )
                self.assertEqual(os.environ["CODEX_HOME"], "/ordinary/codex")
                self.assertEqual(result.snapshot.status, "ok")
                self.assertNotIn(str(self.state), render_json((result.snapshot,)))

    def test_missing_service_preserves_local_collection(self) -> None:
        _ = self._collect()
        self.assertEqual(self.calls, [(RETRIEVED_AT, None, None)])

    def test_service_without_codex_source_preserves_local_collection(self) -> None:
        self._install(())
        _ = self._collect()
        self.assertIsNone(self.calls[-1][2])

    def test_binary_override_wins_over_service_pin(self) -> None:
        binary = self.root / "codex"
        _ = binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        self._install(binary=str(binary))
        _ = self._collect()
        self.assertEqual(self.calls[-1][1], binary)
        os.environ[status.CODEX_BINARY_PATH_ENV] = "/explicit/codex"
        _ = self._collect()
        self.assertEqual(self.calls[-1][1], Path("/explicit/codex"))

    def test_service_binary_symlink_is_refused_but_explicit_override_keeps_old_semantics(self) -> None:
        binary = self.root / "real-codex"
        _ = binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        link = self.root / "codex-link"
        link.symlink_to(binary)
        self._install(binary=str(link))
        with self.assertRaisesRegex(ValueError, "non-symlink.*SCARCITY_ROUTER_CODEX_BIN"):
            _ = self._collect()
        self.assertEqual(self.calls, [])
        os.environ[status.CODEX_BINARY_PATH_ENV] = str(link)
        _ = self._collect()
        self.assertEqual(self.calls[-1][1], link)

    def test_handwritten_equals_flags_preserve_source_state_and_pin(self) -> None:
        binary = self.root / "codex"
        _ = binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        self._install()
        _ = self.unit.write_text(
            "ExecStart=/bin/worker run --codex-source=test-source "
            + f'"--state-dir={self.state}" --codex-bin={binary}\n', encoding="utf-8",
        )
        _ = self._collect()
        self.assertEqual(
            self.calls[-1],
            (RETRIEVED_AT, binary, self.state / "codex-sources/test-source/codex-home"),
        )

    def test_empty_configured_sources_never_look_like_no_source(self) -> None:
        for generated, flag in (
            (True, '--codex-source ""'),
            (False, '--codex-source ""'),
            (False, "--codex-source="),
            (False, "--codex-sour=test-source"),
        ):
            with self.subTest(generated=generated, flag=flag):
                prefix = worker_service.UNIT_MARKER_LINE + "\n" if generated else ""
                _ = self.unit.write_text(
                    prefix + f"ExecStart=/bin/worker run --state-dir {self.root} {flag}\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "repair.*@local"):
                    _ = self._collect()
        self.assertEqual(self.calls, [])

    def test_multiple_sources_require_choice_and_explicit_source_selects_exact_home(self) -> None:
        self._install(("source-one", "source-two"))
        with self.assertRaisesRegex(ValueError, "multiple.*SCARCITY_ROUTER_CODEX_SOURCE"):
            _ = self._collect()
        self.assertEqual(self.calls, [])
        os.environ[status.CODEX_SOURCE_ENV] = "source-two"
        _ = self._collect()
        self.assertEqual(
            self.calls[-1][2],
            self.state / "codex-sources/source-two/codex-home",
        )

    def test_multisource_custom_state_and_binary_recovery_describes_the_same_home_without_paths(self) -> None:
        binary = self.root / "codex"
        _ = binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        self._install(("source-one", "source-two"), binary=str(binary))
        os.environ[status.CODEX_SOURCE_ENV] = "source-two"
        collected = self._collect()
        self.assertEqual(self.calls[-1][1], binary)
        self.assertEqual(self.calls[-1][2], self.state / "codex-sources/source-two/codex-home")
        snapshot = replace(collected.snapshot, status="auth_required", windows=(),
                           diagnostics=(CapacityDiagnostic("auth_required"),))
        screen = " ".join(render_terminal(StatusObservation((snapshot,), ()),
            collected_at=RETRIEVED_AT).split())
        self.assertIn("--source SOURCE_ID --state-dir STATE_DIR --codex-bin CODEX_BIN", screen)
        self.assertIn("same source as SCARCITY_ROUTER_CODEX_SOURCE", screen)
        self.assertIn("explicit --source does not discover those settings", screen)
        self.assertNotIn(str(self.root), screen)
        self.assertNotIn("source-two", screen)
        self.assertNotIn("source-one", screen)

    def test_local_opt_out_skips_even_malformed_worker_configuration(self) -> None:
        _ = self.unit.write_text("malformed unit", encoding="utf-8")
        os.environ[status.CODEX_SOURCE_ENV] = "@local"
        _ = self._collect()
        self.assertIsNone(self.calls[-1][2])
        self.assertEqual(os.environ["CODEX_HOME"], "/ordinary/codex")

    def test_unknown_explicit_source_never_falls_back(self) -> None:
        for installed in (False, True):
            with self.subTest(installed=installed):
                if installed:
                    self._install()
                os.environ[status.CODEX_SOURCE_ENV] = "SYNTHETIC_SECRET_DO_NOT_ECHO"
                with self.assertRaises(ValueError) as raised:
                    _ = self._collect()
                self.assertIn("SCARCITY_ROUTER_CODEX_SOURCE", str(raised.exception))
                self.assertNotIn("SYNTHETIC_SECRET_DO_NOT_ECHO", str(raised.exception))
        self.assertEqual(self.calls, [])

    def test_malformed_or_unreadable_service_never_falls_back_or_echoes_contents(self) -> None:
        _ = self.unit.write_text("ExecStart=SYNTHETIC_SECRET_DO_NOT_ECHO\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "repair.*@local") as raised:
            _ = self._collect()
        self.assertNotIn("SYNTHETIC_SECRET_DO_NOT_ECHO", str(raised.exception))
        self.unit.unlink()
        self.unit.mkdir()
        with self.assertRaisesRegex(ValueError, "repair.*@local"):
            _ = self._collect()
        self.assertEqual(self.calls, [])

    def test_dangling_service_symlink_does_not_look_like_no_service(self) -> None:
        self.unit.symlink_to(self.root / "missing-unit")
        with self.assertRaisesRegex(ValueError, "repair.*@local"):
            _ = self._collect()
        self.assertEqual(self.calls, [])

    def test_ambiguous_source_cli_error_is_actionable_without_traceback(self) -> None:
        from scarcity_router import cli

        self._install(("source-one", "source-two"))
        stderr = io.StringIO()
        stdout = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = cli.main(["status"], stdout=stdout)
        self.assertEqual(code, 1)
        self.assertIn("SCARCITY_ROUTER_CODEX_SOURCE", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn(str(self.state), stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_missing_home_is_not_provisioned(self) -> None:
        self._install()
        home = self.state / "codex-sources/test-source/codex-home"
        (home / "config.toml").unlink()
        home.rmdir()
        with self.assertRaisesRegex(ValueError, "make codex-login"):
            _ = self._collect()
        self.assertFalse(home.exists())
        self.assertEqual(self.calls, [])

    def test_symlinked_source_ancestor_is_refused(self) -> None:
        self._install()
        sources = self.state / "codex-sources"
        relocated = self.state / "relocated"
        _ = sources.rename(relocated)
        sources.symlink_to(relocated, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            _ = self._collect()
        self.assertEqual(self.calls, [])

    def test_unsafe_source_ids_are_refused_before_interpolating_paths(self) -> None:
        self._install()
        for source in ("../outside", "x" * 21):
            with self.subTest(source=source):
                _ = self.unit.write_text(
                    f"ExecStart=/bin/worker run --codex-source {source} "
                    + f"--state-dir {self.root}\n", encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "source id"):
                    _ = self._collect()
        self.assertEqual(self.calls, [])


class StatusRenderingTests(unittest.TestCase):
    def test_terminal_overview_has_a_stable_healthy_content_golden(self) -> None:
        text = render_terminal(StatusObservation(_healthy_snapshots(), ()), collected_at=RETRIEVED_AT, width=120)
        overview = text.split("Overview\n", 1)[1].split("\n\nDetails", 1)[0]
        self.assertEqual(overview, "\n".join((
            "openai: collection ok; lowest reported 65% (5-hour tokens); policy not reported (unknown)",
            "zai: collection ok; lowest reported 98% (5-hour tokens); policy not reported (unknown)",
        )))

    def test_terminal_overview_reports_limiting_weekly_and_short_windows_without_policy_ranking(self) -> None:
        for kind in ("weekly", "five_hour"):
            with self.subTest(kind=kind):
                other = "five_hour" if kind == "weekly" else "weekly"
                snapshot = _snapshot("openai", windows=(
                    _window(other, used=10, remaining=90),
                    _window(kind, used=95, remaining=5),
                ))
                observed = StatusObservation((snapshot,), ())
                text = render_terminal(observed, collected_at=RETRIEVED_AT)
                self.assertIn("Overview", text)
                self.assertIn("lowest reported 5%", text)
                self.assertIn("Details", text)
                self.assertIn("policy not reported (unknown)", " ".join(text.split()))
                self.assertIn("not execution readiness", " ".join(text.split()))
                self.assertIn("never sum scopes", text)
                self.assertNotIn("eligible", text)

    def test_terminal_unknown_absent_and_exhausted_are_not_conflated(self) -> None:
        unknown = _window("unknown", used=None, remaining=None, reset=None)
        exhausted = _window("weekly", used=100, remaining=0, reset=None)
        observed = StatusObservation((_snapshot("openai", windows=(unknown, exhausted)),), ())
        text = render_terminal(observed, collected_at=RETRIEVED_AT)
        normalized = " ".join(text.split())
        self.assertIn("lowest reported 0%", text)
        self.assertIn("unknown/unclassified window(s)", normalized)
        self.assertIn("remaining unknown, used unknown", text)
        self.assertIn("unknown (not immediate)", text)
        self.assertIn("This reported window is exhausted", text)
        absent = _snapshot("openai", "unavailable", diagnostics=(CapacityDiagnostic("source_unavailable"),))
        text = render_terminal(StatusObservation((absent,), ()), collected_at=RETRIEVED_AT)
        self.assertIn("quota unknown, not exhausted or full", text)
        self.assertNotIn("lowest reported 0%", text)
        self.assertNotIn("remaining 100%", text)

    def test_terminal_collection_failures_have_safe_existing_recovery_without_inference(self) -> None:
        for state, code in (
            ("auth_required", "auth_required"), ("schema_changed", "schema_changed"),
            ("unsupported", "unsupported_source"), ("unknown", "telemetry_unknown"),
        ):
            with self.subTest(state=state):
                snapshot = _snapshot("openai", state, diagnostics=(CapacityDiagnostic(code),))
                text = render_terminal(StatusObservation((snapshot,), ()), collected_at=RETRIEVED_AT)
                self.assertIn(state, text)
                self.assertIn("unknown", text)
                self.assertIn("Recovery:", text)
                self.assertNotIn("/home/", text)
                self.assertNotIn("Authorization", text)
                self.assertNotIn("remaining 0%", text)
                if state == "auth_required":
                    self.assertIn("scarcity-router-worker codex-login", " ".join(text.split()))
                    self.assertIn("Do not probe with inference", " ".join(text.split()))

    def test_terminal_fetch_age_and_policy_block_are_separate_from_quota(self) -> None:
        healthy = _healthy_snapshots()[0]
        snapshot = replace(healthy, retrieved_at="2026-09-04T09:00:00.123Z", windows=tuple(
            replace(window, resets_at="2026-09-04T12:00:00.000Z") for window in healthy.windows
        ))
        report = ExecutionEligibility(schema_version=1, provider="openai", source=snapshot.source,
            retrieved_at=snapshot.retrieved_at, state="policy_blocked", reason_codes=("purchased_credits_present",))
        text = render_terminal(StatusObservation((snapshot,), (report,)), collected_at=RETRIEVED_AT)
        normalized = " ".join(text.split())
        self.assertIn("1d since fetch", text)
        self.assertIn("not a freshness verdict", normalized)
        self.assertIn("policy policy_blocked", normalized)
        self.assertIn("purchased_credits_present", text)
        self.assertIn("remaining 65%", text)
        self.assertIn("do not use purchased credits", normalized)
        self.assertIn("reported time passed; refresh to confirm", normalized)

    def test_terminal_does_not_claim_offline_doctor_checks_source_or_provider_access(self) -> None:
        for state, code in (("unavailable", "source_unavailable"), ("unknown", "telemetry_unknown")):
            with self.subTest(state=state):
                snapshot = _snapshot("openai", state, diagnostics=(CapacityDiagnostic(code),))
                text = " ".join(render_terminal(StatusObservation((snapshot,), ()),
                    collected_at=RETRIEVED_AT).split())
                self.assertIn("checks artifacts/configuration only, not provider/source access", text)
                self.assertIn("configured binary", text)
                self.assertIn("scarcity-router-worker service status", text)
                self.assertIn("Retry", text)
                self.assertNotIn("Check access with scarcity-router doctor", text)
                self.assertNotIn("Check source access with scarcity-router doctor", text)

    def test_terminal_ambiguous_windows_identified_without_assuming_shared_pool(self) -> None:
        first = replace(_window("weekly", window_id="pool-a"), scope_id="codex")
        second = replace(first, window_id="pool-b", remaining_percent=20, used_percent=80)
        observation = StatusObservation((_snapshot("openai", windows=(first, second)),), ())
        text = render_terminal(observation, collected_at=RETRIEVED_AT)
        self.assertIn("[pool-a]", text)
        self.assertIn("[pool-b]", text)
        self.assertIn("scope: codex", text)
        self.assertIn("never sum scopes or assume a shared/account pool", " ".join(text.split()))
        reversed_observation = replace(observation, snapshots=(replace(observation.snapshots[0], windows=(second, first)),))
        self.assertEqual(text, render_terminal(reversed_observation, collected_at=RETRIEVED_AT))

    def test_terminal_does_not_promote_an_unpaired_old_policy_report(self) -> None:
        snapshot = _healthy_snapshots()[0]
        report = ExecutionEligibility(schema_version=1, provider="openai", source=snapshot.source,
            retrieved_at="2026-09-04T09:00:00.123Z", state="eligible", reason_codes=())
        text = render_terminal(StatusObservation((snapshot,), (report,)), collected_at=RETRIEVED_AT)
        self.assertIn("policy not reported (unknown)", " ".join(text.split()))
        self.assertNotIn("policy: eligible", text)

    def test_both_cli_entrypoints_use_one_clock_and_keep_json_and_pipes_byte_compatible(self) -> None:
        class Terminal(io.StringIO):
            @override
            def isatty(self) -> bool:
                return True

        snapshots = _healthy_snapshots()
        for entrypoint in (main, cli.main):
            for tty, json_output in ((True, False), (False, False), (True, True), (False, True)):
                with self.subTest(entrypoint=entrypoint.__module__, tty=tty, json=json_output):
                    collectors, fakes = _collector_set(snapshots)
                    output = Terminal() if tty else io.StringIO()
                    calls = 0

                    def clock() -> datetime:
                        nonlocal calls
                        calls += 1
                        return datetime.fromisoformat(RETRIEVED_AT.replace("Z", "+00:00"))

                    with TemporaryDirectory() as home, mock.patch.dict(os.environ, {
                        "HOME": home, "XDG_CONFIG_HOME": str(Path(home) / "config"),
                        "COLUMNS": "32", "NO_COLOR": "1", "FORCE_COLOR": "1",
                    }, clear=True):
                        result = entrypoint(["status", *( ["--json"] if json_output else [])],
                            stdout=output, collectors=collectors, clock=clock)
                    self.assertEqual(result, 0)
                    self.assertEqual(calls, 1)
                    self.assertEqual([item[0] for item in fakes.calls], ["openai", "zai"])
                    if json_output:
                        self.assertEqual(output.getvalue(), render_json(snapshots))
                    elif tty:
                        screen = output.getvalue()
                        self.assertIn("Capacity snapshot", screen)
                        self.assertLessEqual(max(map(len, screen.splitlines())), 32)
                        self.assertTrue(all(character == "\n" or 32 <= ord(character) < 127 for character in screen))
                        self.assertNotIn("\x1b", screen)
                    else:
                        self.assertEqual(output.getvalue(), render_human(snapshots))

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
