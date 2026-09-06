"""M2e application and CLI tests (D-027).

Focused application tests with injected synthetic collectors and an
injected clock: no live provider calls and no model requests. Covers the
top-level dispatcher (``status`` compatibility, ``select``, ``simulate``),
the requirement-resolution paths (profile, explicit, tightening), strict
JSON artifact loading and safe failure for malformed artifacts.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from scarcity_router import CapacityDiagnostic, CapacitySnapshot, CapacityWindow
from scarcity_router.cli import build_parser, main
from scarcity_router.selection_app import load_strict_json
from scarcity_router.status import StatusCollectors

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"
POLICY_PATH = REPO / "model-policy.json"

FIXED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
RETRIEVED_AT = "2026-09-06T12:00:00.000Z"


def _snap(provider: str, five: int, weekly: int) -> CapacitySnapshot:
    scope = "codex" if provider == "openai" else "coding_plan"
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at=RETRIEVED_AT,
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


def _collectors() -> StatusCollectors:
    def openai(*, retrieved_at: str) -> CapacitySnapshot:
        _ = retrieved_at
        return _snap("openai", 40, 40)

    def zai(*, retrieved_at: str) -> CapacitySnapshot:
        _ = retrieved_at
        return _snap("zai", 80, 80)

    return StatusCollectors(openai=openai, zai=zai)


def _clock() -> datetime:
    return FIXED_AT


def _run(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    original_stderr = sys.stderr
    sys.stderr = err
    try:
        code = main(argv, stdout=out, collectors=_collectors(), clock=_clock)
    finally:
        sys.stderr = original_stderr
    return code, out.getvalue(), err.getvalue()


def _select_args(*extra: str) -> list[str]:
    return [
        "select",
        *extra,
        "--catalog",
        str(CATALOG_PATH),
        "--model-policy",
        str(POLICY_PATH),
    ]


class StatusCompatibilityTests(unittest.TestCase):
    """The existing status behavior is preserved through the dispatcher."""

    def test_status_matches_status_module(self) -> None:
        from scarcity_router.status import main as status_main

        code, out, err = _run(["status"])
        self.assertEqual(0, code)
        self.assertEqual("", err)

        direct = io.StringIO()
        status_code = status_main(
            ["status"], stdout=direct, collectors=_collectors(), clock=_clock
        )
        self.assertEqual(0, status_code)
        self.assertEqual(direct.getvalue(), out)

    def test_status_json_matches_status_module(self) -> None:
        from scarcity_router.status import main as status_main

        code, out, _ = _run(["status", "--json"])
        self.assertEqual(0, code)
        direct = io.StringIO()
        status_code = status_main(
            ["status", "--json"],
            stdout=direct,
            collectors=_collectors(),
            clock=_clock,
        )
        self.assertEqual(0, status_code)
        self.assertEqual(direct.getvalue(), out)
        payload = cast("list[object]", json.loads(out))
        self.assertEqual(2, len(payload))


class SelectCommandTests(unittest.TestCase):
    """The ``select`` command paths and output contracts."""

    def test_select_profile_human(self) -> None:
        code, out, err = _run(_select_args("--profile", "routine_coding"))
        self.assertEqual(0, code)
        self.assertEqual("", err)
        self.assertIn("Selected: GLM-5.3-Flash Max (zai/glm-5.3-flash/max)", out)
        self.assertIn("Profile: routine_coding", out)
        self.assertIn("Scarcity: plentiful — 80% remaining, penalty 400", out)
        self.assertIn("Capability margin: 5", out)
        self.assertIn("Policy: balanced", out)

    def test_select_profile_json(self) -> None:
        code, out, _ = _run(_select_args("--profile", "routine_coding", "--json"))
        self.assertEqual(0, code)
        decision = cast("dict[str, object]", json.loads(out))
        self.assertEqual("routine_coding", decision["profile_id"])
        self.assertEqual(4, decision["profile_policy_version"])
        self.assertEqual("balanced", decision["selector_mode"])
        self.assertEqual(["selected_balanced"], decision["reason_codes"])
        selected = cast("dict[str, object]", decision["selected"])
        identity = cast("dict[str, object]", selected["identity"])
        self.assertEqual("glm-5.3-flash", identity["model"])

    def test_select_profile_explain(self) -> None:
        code, out, _ = _run(_select_args("--profile", "deep_coding", "--explain"))
        self.assertEqual(0, code)
        self.assertIn("Resolved requirement:", out)
        self.assertIn("Alternatives (exact ranking order):", out)
        self.assertIn("Excluded candidates:", out)
        self.assertIn("capability:", out)
        self.assertIn("Versions: catalog 1 (2026-09-06)", out)

    def test_select_requirement_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            requirement_path = Path(tmp) / "task.json"
            _ = requirement_path.write_text(
                json.dumps(
                    {
                        "task_level": "L3",
                        "capability_minima": {
                            "reasoning": 4,
                            "coding": 5,
                            "tool_use": 4,
                        },
                        "hard_constraints": {
                            "requires_tool_use": True,
                            "requires_reasoning_mode": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            code, out, _ = _run(
                _select_args("--requirement", str(requirement_path))
            )
            self.assertEqual(0, code)
            self.assertIn("Selected: GLM-5.3 Max (zai/glm-5.3/max)", out)
            self.assertIn("Requirement: explicit", out)

    def test_select_profile_with_tighten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tighten_path = Path(tmp) / "stricter.json"
            _ = tighten_path.write_text(
                json.dumps(
                    {
                        "task_level": "L3",
                        "capability_minima": {},
                        "hard_constraints": {"requires_vision": True},
                    }
                ),
                encoding="utf-8",
            )
            code, out, _ = _run(
                _select_args(
                    "--profile",
                    "deep_coding",
                    "--tighten",
                    str(tighten_path),
                    "--explain",
                )
            )
            self.assertEqual(0, code)
            # GLM-5.3 (vision known false) is excluded; Sol is selected.
            self.assertIn(
                "Selected: GPT-5.6 Sol High (openai/gpt-5.6-sol/high)", out
            )
            self.assertIn("requires_vision unsupported", out)

    def test_tighten_with_requirement_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            requirement_path = Path(tmp) / "task.json"
            _ = requirement_path.write_text(
                json.dumps(
                    {
                        "task_level": "L1",
                        "capability_minima": {},
                        "hard_constraints": {},
                    }
                ),
                encoding="utf-8",
            )
            tighten_path = Path(tmp) / "tighten.json"
            _ = tighten_path.write_text(
                json.dumps(
                    {
                        "task_level": "L1",
                        "capability_minima": {},
                        "hard_constraints": {},
                    }
                ),
                encoding="utf-8",
            )
            code, _, err = _run(
                [
                    "select",
                    "--requirement",
                    str(requirement_path),
                    "--tighten",
                    str(tighten_path),
                ]
            )
            self.assertEqual(1, code)
            self.assertTrue(err.startswith("error:"))

    def test_no_solution_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            requirement_path = Path(tmp) / "task.json"
            _ = requirement_path.write_text(
                json.dumps(
                    {
                        "task_level": "L1",
                        "capability_minima": {},
                        "hard_constraints": {"minimum_output_tokens": 999_999},
                    }
                ),
                encoding="utf-8",
            )
            code, out, _ = _run(
                _select_args("--requirement", str(requirement_path))
            )
            # A valid structured no-solution is a legitimate result.
            self.assertEqual(0, code)
            self.assertIn("No eligible candidate", out)
            self.assertIn("Closest candidates:", out)

    def test_degraded_selection_warns(self) -> None:
        def unknown_openai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            return CapacitySnapshot(
                schema_version=3,
                provider="openai",
                source="synthetic_test",
                retrieved_at=RETRIEVED_AT,
                status="unknown",
                windows=(),
                diagnostics=(CapacityDiagnostic(code="telemetry_unknown"),),
            )

        def unknown_zai(*, retrieved_at: str) -> CapacitySnapshot:
            _ = retrieved_at
            return CapacitySnapshot(
                schema_version=3,
                provider="zai",
                source="synthetic_test",
                retrieved_at=RETRIEVED_AT,
                status="unknown",
                windows=(),
                diagnostics=(CapacityDiagnostic(code="telemetry_unknown"),),
            )

        out = io.StringIO()
        code = main(
            ["select", "--profile", "routine_coding",
             "--catalog", str(CATALOG_PATH), "--model-policy", str(POLICY_PATH)],
            stdout=out,
            collectors=StatusCollectors(openai=unknown_openai, zai=unknown_zai),
            clock=_clock,
        )
        self.assertEqual(0, code)
        rendered = out.getvalue()
        # A degraded selection is explicit, with the normalized unknown
        # reason and no fake percentage.
        self.assertIn("WARNING: selected with unknown capacity", rendered)
        self.assertIn("Unknown reason: unknown_capacity_degraded", rendered)
        self.assertIn("Scarcity: unknown — provider_snapshot_not_ok", rendered)
        self.assertIn("Capability margin: 5", rendered)
        self.assertIn(
            "Reason: selected_balanced,selected_degraded_capacity", rendered
        )


class SimulateCommandTests(unittest.TestCase):
    """The ``simulate`` command paths and output contracts."""

    def _overrides_file(self, tmp: str) -> Path:
        path = Path(tmp) / "overrides.json"
        _ = path.write_text(
            json.dumps(
                {
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
            ),
            encoding="utf-8",
        )
        return path

    def test_simulate_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overrides = self._overrides_file(tmp)
            code, out, err = _run(
                [
                    "simulate",
                    "--profile",
                    "deep_coding",
                    "--overrides",
                    str(overrides),
                    "--catalog",
                    str(CATALOG_PATH),
                    "--model-policy",
                    str(POLICY_PATH),
                ]
            )
            self.assertEqual(0, code)
            self.assertEqual("", err)
            self.assertIn("CURRENT", out)
            self.assertIn("SIMULATED", out)
            self.assertIn("Overrides applied:", out)
            self.assertIn(
                "zai/coding_plan tokens weekly -> 2% remaining", out
            )
            current_block = out.split("SIMULATED", 1)[0]
            simulated_block = out.split("SIMULATED", 1)[1]
            self.assertIn("GLM-5.3 Max", current_block)
            self.assertIn("GPT-5.6 Sol High", simulated_block)

    def test_simulate_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overrides = self._overrides_file(tmp)
            code, out, _ = _run(
                [
                    "simulate",
                    "--profile",
                    "deep_coding",
                    "--overrides",
                    str(overrides),
                    "--json",
                    "--catalog",
                    str(CATALOG_PATH),
                    "--model-policy",
                    str(POLICY_PATH),
                ]
            )
            self.assertEqual(0, code)
            payload = cast("dict[str, object]", json.loads(out))
            self.assertEqual(
                {"baseline", "simulated", "applied_overrides"}, set(payload)
            )
            baseline = cast("dict[str, object]", payload["baseline"])
            baseline_selected = cast("dict[str, object]", baseline["selected"])
            baseline_identity = cast(
                "dict[str, object]", baseline_selected["identity"]
            )
            self.assertEqual("glm-5.3", baseline_identity["model"])
            simulated = cast("dict[str, object]", payload["simulated"])
            simulated_selected = cast("dict[str, object]", simulated["selected"])
            simulated_identity = cast(
                "dict[str, object]", simulated_selected["identity"]
            )
            self.assertEqual("gpt-5.6-sol", simulated_identity["model"])
            applied = cast("dict[str, object]", payload["applied_overrides"])
            percentages = cast("list[object]", applied["capacity_percentages"])
            first_override = cast("dict[str, object]", percentages[0])
            self.assertEqual(2, first_override["remaining_percent"])


class ArtifactFailureTests(unittest.TestCase):
    """Malformed artifacts fail safely with exit 1 and a concise message."""

    def _broken_file(self, tmp: str, name: str, content: str) -> str:
        path = Path(tmp) / name
        _ = path.write_text(content, encoding="utf-8")
        return str(path)

    def test_malformed_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self._broken_file(tmp, "catalog.json", "{not json")
            code, _, err = _run(
                [
                    "select",
                    "--profile",
                    "routine_coding",
                    "--catalog",
                    catalog,
                ]
            )
            self.assertEqual(1, code)
            self.assertTrue(err.startswith("error:"))
            self.assertNotIn("Traceback", err)

    def test_duplicate_json_keys_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self._broken_file(
                tmp,
                "catalog.json",
                (
                    '{"catalog_version": 1, "catalog_version": 2, '
                    + '"updated_on": "2026-09-06", "entries": []}'
                ),
            )
            code, _, err = _run(
                [
                    "select",
                    "--profile",
                    "routine_coding",
                    "--catalog",
                    catalog,
                ]
            )
            self.assertEqual(1, code)
            self.assertIn("duplicate object key", err)

    def test_malformed_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            requirement = self._broken_file(
                tmp, "task.json", '{"task_level": "L9", "capability_minima": {}}'
            )
            code, _, err = _run(
                _select_args("--requirement", requirement)
            )
            self.assertEqual(1, code)
            self.assertTrue(err.startswith("error:"))

    def test_malformed_tightening_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tighten = self._broken_file(tmp, "tighten.json", '{"task_level": "L1"}')
            code, _, err = _run(
                _select_args("--profile", "deep_coding", "--tighten", tighten)
            )
            self.assertEqual(1, code)
            self.assertTrue(err.startswith("error:"))

    def test_malformed_selector_policy_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = self._broken_file(
                tmp, "policy.json", '{"mode": "quality-first"}'
            )
            code, _, err = _run(
                _select_args(
                    "--profile",
                    "routine_coding",
                    "--selector-policy",
                    policy,
                )
            )
            self.assertEqual(1, code)
            self.assertTrue(err.startswith("error:"))

    def test_malformed_replenishment_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            states = self._broken_file(
                tmp,
                "replenishment.json",
                (
                    '[{"provider": "openai", "kind": "rate_limit_reset", '
                    + '"available_count": -1, "details_known": true, '
                    + '"retrieved_at": "2026-09-06T12:00:00.000Z"}]'
                ),
            )
            code, _, err = _run(
                _select_args(
                    "--profile",
                    "routine_coding",
                    "--replenishment",
                    states,
                )
            )
            self.assertEqual(1, code)
            self.assertTrue(err.startswith("error:"))

    def test_malformed_overrides_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overrides = self._broken_file(
                tmp,
                "overrides.json",
                (
                    '{"capacity_percentages": [{"provider": "zai", '
                    + '"scope_id": "coding_plan", "resource": "tokens", '
                    + '"kind": "weekly", "remaining_percent": 101}]}'
                ),
            )
            code, _, err = _run(
                [
                    "simulate",
                    "--profile",
                    "deep_coding",
                    "--overrides",
                    overrides,
                    "--catalog",
                    str(CATALOG_PATH),
                    "--model-policy",
                    str(POLICY_PATH),
                ]
            )
            self.assertEqual(1, code)
            self.assertTrue(err.startswith("error:"))
            self.assertNotIn("Traceback", err)


class StrictJsonAndRenderingTests(unittest.TestCase):
    """Strict JSON loading and deterministic rendering details."""

    def test_strict_json_rejects_nan_and_duplicates(self) -> None:
        with self.assertRaises(ValueError):
            _ = load_strict_json('{"a": NaN}', label="test")
        with self.assertRaises(ValueError):
            _ = load_strict_json('{"a": 1, "a": 2}', label="test")
        with self.assertRaises(ValueError):
            _ = load_strict_json("{oops}", label="test")
        self.assertEqual({"a": 1}, load_strict_json('{"a": 1}', label="test"))

    def test_rendering_is_deterministic(self) -> None:
        collectors = _collectors()
        json_args = _select_args("--profile", "routine_coding", "--json")
        first = io.StringIO()
        second = io.StringIO()
        self.assertEqual(
            0, main(json_args, stdout=first, collectors=collectors, clock=_clock)
        )
        self.assertEqual(
            0, main(json_args, stdout=second, collectors=collectors, clock=_clock)
        )
        self.assertEqual(first.getvalue(), second.getvalue())
        # Sorted-keys deterministic JSON: re-dumping the document reproduces
        # the exact bytes.
        document = cast("dict[str, object]", json.loads(first.getvalue()))
        self.assertEqual(
            json.dumps(document, indent=2, sort_keys=True) + "\n", first.getvalue()
        )
        human_args = _select_args("--profile", "routine_coding")
        human_first = io.StringIO()
        human_second = io.StringIO()
        self.assertEqual(
            0,
            main(human_args, stdout=human_first, collectors=collectors, clock=_clock),
        )
        self.assertEqual(
            0,
            main(human_args, stdout=human_second, collectors=collectors, clock=_clock),
        )
        self.assertEqual(human_first.getvalue(), human_second.getvalue())

    def test_help_surfaces(self) -> None:
        for argv in (
            ["--help"],
            ["status", "--help"],
            ["select", "--help"],
            ["simulate", "--help"],
        ):
            captured = io.StringIO()
            original_stdout = sys.stdout
            sys.stdout = captured
            try:
                with self.assertRaises(SystemExit) as ctx:
                    _ = build_parser().parse_args(argv)
            finally:
                sys.stdout = original_stdout
            self.assertEqual(0, ctx.exception.code)
            self.assertIn("usage:", captured.getvalue())
        help_text = build_parser().format_help()
        for command in ("status", "select", "simulate"):
            self.assertIn(command, help_text)


if __name__ == "__main__":
    _ = unittest.main()
