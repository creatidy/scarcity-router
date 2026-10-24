"""Synthetic offline Claude projection, never a live/account acceptance test."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from scarcity_router.capacity import CapacitySnapshot
from scarcity_router.providers.claude_statusline import parse_claude_statusline
from scarcity_router.selection_types import SUPPORTED_PROVIDERS
from scarcity_router.status import StatusCollectors

T0 = "2026-10-24T12:00:00.000Z"
CANARY = "SYNTHETIC-CLAUDE-SECRET-NOT-A-CREDENTIAL"
FIXTURE = Path(__file__).parent / "fixtures/claude-statusline/known-windows.json"


class ClaudeStatuslineTests(unittest.TestCase):
    def fixture(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(FIXTURE.read_text()))

    def parse(self, payload: object, *, observed_at: str | None = T0) -> CapacitySnapshot:
        return parse_claude_statusline(payload, retrieved_at=T0, observed_at=observed_at, max_age_seconds=60)

    def window(self, **changes: object) -> dict[str, object]:
        return {"rate_limits": {"five_hour": {"used_percentage": 25, "resets_at": 1792846800, **changes}}}

    def test_evidenced_partial_fields_never_become_healthy_capacity(self) -> None:
        snapshot = self.parse(self.fixture())
        self.assertEqual("unknown", snapshot.status)
        self.assertEqual("anthropic", snapshot.provider)
        self.assertEqual("claude_code_statusline", snapshot.source)
        self.assertEqual(T0, snapshot.retrieved_at)
        self.assertEqual(["five_hour", "weekly"], [window.kind for window in snapshot.windows])
        self.assertEqual([18_000, 604_800], [window.duration_seconds for window in snapshot.windows])
        self.assertEqual([25, 80], [window.used_percent for window in snapshot.windows])
        self.assertEqual([75, 20], [window.remaining_percent for window in snapshot.windows])
        self.assertEqual("2026-10-24T13:00:00.000Z", snapshot.windows[0].resets_at)
        self.assertEqual(["unknown", "unknown"], [window.resource for window in snapshot.windows])
        self.assertTrue(all(window.scope_id is None for window in snapshot.windows))
        self.assertIsNone(snapshot.plan)
        self.assertEqual(snapshot, CapacitySnapshot.from_dict(snapshot.to_dict()))

    def test_default_has_no_quota_observation_provenance(self) -> None:
        snapshot = parse_claude_statusline(self.fixture(), retrieved_at=T0, max_age_seconds=60)
        self.assertEqual("unknown", snapshot.status)
        self.assertTrue(all(window.used_percent is None for window in snapshot.windows))

    def test_invalid_stale_future_and_noncanonical_observations_do_not_refresh_cache(self) -> None:
        for observed in (None, CANARY, "2026-10-24T11:58:59.000Z", "2026-10-24T12:00:00.001Z", "2026-10-24T12:00:00+00:00"):
            with self.subTest(observed=observed):
                snapshot = self.parse(self.fixture(), observed_at=observed)
                self.assertEqual("unknown", snapshot.status)
                self.assertTrue(all(window.used_percent is None for window in snapshot.windows))
                self.assertNotIn(CANARY, json.dumps(snapshot.to_dict()))

    def test_whole_percentages_preserve_zero_and_exhaustion_without_entitlement(self) -> None:
        for used in (0, 0.0, 17, 17.0, 100, 100.0):
            with self.subTest(used=used):
                snapshot = self.parse(self.window(used_percentage=used))
                self.assertEqual("unknown", snapshot.status)
                self.assertEqual(int(used), snapshot.windows[0].used_percent)
                self.assertEqual(100 - int(used), snapshot.windows[0].remaining_percent)

    def test_fractional_or_invalid_percentages_are_not_rounded_or_guessed(self) -> None:
        for used in (True, False, -1, 101, 10**100, 25.5, float("nan"), float("inf"), "25", None, CANARY):
            with self.subTest(used=used):
                snapshot = self.parse(self.window(used_percentage=used))
                self.assertIsNone(snapshot.windows[0].used_percent)
                self.assertIsNone(snapshot.windows[0].remaining_percent)
                self.assertNotIn(CANARY, json.dumps(snapshot.to_dict()))

    def test_missing_invalid_and_millisecond_resets_do_not_reuse_percentages(self) -> None:
        for reset in (None, False, True, 0, -1, 1792846800.0, 1792846800000, 10**100, "1792846800", CANARY):
            with self.subTest(reset=reset):
                window = self.parse(self.window(resets_at=reset)).windows[0]
                self.assertIsNone(window.resets_at)
                self.assertIsNone(window.used_percent)

    def test_reset_crossing_keeps_original_instant_not_old_percentage(self) -> None:
        window = self.parse(self.window(resets_at=1792843199)).windows[0]
        self.assertEqual("2026-10-24T11:59:59.000Z", window.resets_at)
        self.assertIsNone(window.used_percent)

    def test_implausible_future_reset_is_not_reinterpreted(self) -> None:
        window = self.parse(self.window(resets_at=1793275200)).windows[0]
        self.assertIsNone(window.resets_at)
        self.assertIsNone(window.used_percent)

    def test_empty_or_missing_windows_are_unknown_not_full(self) -> None:
        cases: tuple[object, ...] = ({}, {"rate_limits": None}, {"rate_limits": {}}, {"rate_limits": {"spend_limit": {"used": 0, "total": 100}}})
        for payload in cases:
            with self.subTest(payload=payload):
                snapshot = self.parse(payload)
                self.assertEqual("unknown", snapshot.status)
                self.assertEqual((), snapshot.windows)

    def test_shape_drift_and_new_buckets_fail_atomically_without_raw_echo(self) -> None:
        cases: tuple[object, ...] = (None, [], {"rate_limits": []}, {"rate_limits": CANARY}, {"rate_limits": {"future_bucket": CANARY}}, {"rate_limits": {"five_hour": []}}, self.window(future_field=CANARY))
        for payload in cases:
            with self.subTest(payload=payload):
                snapshot = self.parse(payload)
                self.assertEqual("schema_changed", snapshot.status)
                self.assertEqual((), snapshot.windows)
                self.assertNotIn(CANARY, json.dumps(snapshot.to_dict()))

    def test_auth_failure_is_not_guessed_from_an_unevidenced_error_envelope(self) -> None:
        snapshot = self.parse({"error": {"type": "authentication_error", "message": CANARY}})
        self.assertEqual("unknown", snapshot.status)
        self.assertEqual((), snapshot.windows)
        self.assertNotIn(CANARY, json.dumps(snapshot.to_dict()))

    def test_account_auth_cost_session_and_filesystem_data_never_enter_output(self) -> None:
        payload = self.fixture()
        payload.update({"account": CANARY, "session_id": CANARY, "cwd": CANARY, "transcript_path": CANARY, "authMethod": "claude.ai", "loggedIn": True, "subscriptionType": "max", "cost": {"total_cost_usd": 0}, "apiKey": CANARY})
        limits = cast(dict[str, object], payload["rate_limits"])
        limits["spend_limit"] = {"used": CANARY, "total": CANARY}
        snapshot = self.parse(payload)
        self.assertEqual("unknown", snapshot.status)
        self.assertIsNone(snapshot.plan)
        self.assertNotIn(CANARY, json.dumps(snapshot.to_dict()))
        self.assertNotIn("max", json.dumps(snapshot.to_dict()))

    def test_parsing_does_not_mutate_or_launch_a_collector_or_model(self) -> None:
        payload = self.fixture()
        before = copy.deepcopy(payload)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no process")), mock.patch("socket.socket", side_effect=AssertionError("no network")):
            _ = self.parse(payload)
        self.assertEqual(before, payload)
        self.assertNotIn("anthropic", SUPPORTED_PROVIDERS)
        self.assertEqual({"openai", "zai"}, set(StatusCollectors.__dataclass_fields__))

    def test_freshness_policy_is_explicit_and_positive(self) -> None:
        for age in (0, -1, True):
            with self.subTest(age=age), self.assertRaisesRegex(ValueError, "positive integer"):
                _ = parse_claude_statusline(self.fixture(), retrieved_at=T0, max_age_seconds=age)


if __name__ == "__main__":
    _ = unittest.main()
