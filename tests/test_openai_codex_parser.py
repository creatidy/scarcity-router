"""Parser and contract tests for the OpenAI Codex app-server adapter.

Every fixture under ``tests/fixtures/openai-codex-appserver/`` participates
here. The parser under test is pure: it receives an already decoded
``account/rateLimits/read`` result and a caller-supplied retrieval
timestamp, and performs no clock, filesystem, environment, network or
subprocess access.

All tests are deterministic and self-contained; the only file access is
reading the synthetic, redacted fixture inputs.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from scarcity_router import CapacityError, CapacitySnapshot, CapacityWindow
from scarcity_router.providers.openai_codex import (
    REACHED_TYPES,
    classify_app_server_message,
    parse_codex_rate_limits_result,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "openai-codex-appserver"
RETRIEVED_AT = "2026-09-03T20:00:00.000Z"

# Independently precomputed canonical UTC renderings of the fixture
# epoch-second values (they must not be recomputed via the parser).
RESET_1788306212 = "2026-09-01T23:43:32.000Z"
RESET_1788748064 = "2026-09-07T02:27:44.000Z"
RESET_1788000000 = "2026-08-29T10:40:00.000Z"

ALL_FIXTURES = (
    "ratelimits-ok-plus.json",
    "ratelimits-full-shape-ok.json",
    "ratelimits-credits-present.json",
    "ratelimits-spend-control-exhausted.json",
    "ratelimits-credits-malformed.json",
    "ratelimits-additional-bucket-exhausted.json",
    "ratelimits-slots-swapped.json",
    "ratelimits-unknown-duration.json",
    "ratelimits-exhausted-reached.json",
    "ratelimits-zero-usage.json",
    "ratelimits-degraded.json",
    "ratelimits-schema-changed.json",
)


def _load(name: str) -> dict[str, object]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return cast("dict[str, object]", json.load(handle))


def _window(
    used_percent: object,
    duration_mins: object,
    resets_at: object = 1788306212,
) -> dict[str, object]:
    window: dict[str, object] = {}
    if used_percent is not None:
        window["usedPercent"] = used_percent
    if duration_mins is not None:
        window["windowDurationMins"] = duration_mins
    if resets_at is not None:
        window["resetsAt"] = resets_at
    return window


_BOTH_SLOTS: dict[str, object] = {
    "primary": _window(6, 300),
    "secondary": _window(52, 10080, 1788748064),
}


def _snapshot(
    slots: dict[str, object],
    *,
    plan_type: object = "plus",
    limit_id: object = "codex",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    rate_limits: dict[str, object] = {"limitId": limit_id, **slots}
    if plan_type is not None:
        rate_limits["planType"] = plan_type
    if extra is not None:
        rate_limits.update(extra)
    return rate_limits


def _result(
    rate_limits: dict[str, object],
    *,
    buckets: object = None,
    reset_credits: object = None,
    envelope_extra: dict[str, object] | None = None,
    omit: tuple[str, ...] = (),
) -> dict[str, object]:
    """A full GetAccountRateLimitsResponse envelope for tests.

    ``omit`` removes required members to build drift inputs.
    """
    result: dict[str, object] = {
        "rateLimits": rate_limits,
        "rateLimitsByLimitId": buckets,
        "rateLimitResetCredits": reset_credits,
    }
    for member in omit:
        _ = result.pop(member, None)
    if envelope_extra is not None:
        result.update(envelope_extra)
    return result


def _parse(payload: dict[str, object]) -> CapacitySnapshot:
    return parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)


def _windows(
    snap: CapacitySnapshot,
    *,
    resource: str,
    kind: str,
) -> list[CapacityWindow]:
    return [w for w in snap.windows if w.resource == resource and w.kind == kind]


def _codes(snap: CapacitySnapshot) -> set[str]:
    return {d.code for d in snap.diagnostics}


def _canonical_json(snap: CapacitySnapshot) -> str:
    return json.dumps(snap.to_dict(), sort_keys=True)


# ══════════════════════════ fixture-driven tests ═════════════════════════════


class KnownWindowsFixture(unittest.TestCase):
    def test_full_normalization(self) -> None:
        snap = _parse(_load("ratelimits-ok-plus.json"))
        self.assertEqual(snap.status, "ok")
        self.assertEqual(snap.schema_version, 1)
        self.assertEqual(snap.provider, "openai")
        self.assertEqual(snap.source, "codex_app_server")
        self.assertEqual(snap.plan, "plus")
        self.assertEqual(snap.retrieved_at, RETRIEVED_AT)
        self.assertEqual(len(snap.windows), 2)
        self.assertEqual(_codes(snap), set())

        five_hour = _windows(snap, resource="tokens", kind="five_hour")
        self.assertEqual(len(five_hour), 1)
        fh = five_hour[0]
        self.assertEqual(fh.duration_seconds, 18_000)
        self.assertEqual(fh.used_percent, 6)
        self.assertEqual(fh.remaining_percent, 94)
        self.assertEqual(fh.resets_at, RESET_1788306212)
        self.assertEqual(fh.window_id, "primary")

        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(len(weekly), 1)
        wk = weekly[0]
        self.assertEqual(wk.duration_seconds, 604_800)
        self.assertEqual(wk.used_percent, 52)
        self.assertEqual(wk.remaining_percent, 48)
        self.assertEqual(wk.resets_at, RESET_1788748064)
        self.assertEqual(wk.window_id, "secondary")

    def test_slot_names_carry_no_period_semantics(self) -> None:
        snap = _parse(_load("ratelimits-slots-swapped.json"))
        self.assertEqual(snap.status, "ok")
        # The weekly window sits under `primary`; classification must follow
        # the validated windowDurationMins, never the slot position.
        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0].window_id, "primary")
        self.assertEqual(weekly[0].used_percent, 80)
        five_hour = _windows(snap, resource="tokens", kind="five_hour")
        self.assertEqual(len(five_hour), 1)
        self.assertEqual(five_hour[0].window_id, "secondary")
        self.assertEqual(five_hour[0].used_percent, 10)


class FullShapeFixture(unittest.TestCase):
    """The exact evidenced envelope and snapshot shapes, metadata null."""

    def test_complete_shape_is_healthy(self) -> None:
        snap = _parse(_load("ratelimits-full-shape-ok.json"))
        self.assertEqual(snap.status, "ok")
        self.assertEqual(snap.plan, "pro")
        self.assertEqual(len(snap.windows), 2)
        self.assertEqual(_codes(snap), set())

    def test_metadata_members_never_reach_output(self) -> None:
        snap = _parse(_load("ratelimits-credits-present.json"))
        text = _canonical_json(snap)
        for forbidden in (
            "hasCredits", "balance", "remainingPercent", "enforcementMode",
            "availableCount", "limitName", "individualLimit", "credits",
        ):
            self.assertNotIn(forbidden, text)


class CreditsAndSpendControl(unittest.TestCase):
    """Typed CreditsSnapshot / SpendControlLimitSnapshot semantics."""

    def test_present_valid_credits_degrade_with_withheld_pairs(self) -> None:
        snap = _parse(_load("ratelimits-credits-present.json"))
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
            self.assertIsNone(window.remaining_percent)
        self.assertIn("percentage_unknown", _codes(snap))

    def test_valid_reset_credit_summary_degrades(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            reset_credits={"availableCount": 2, "credits": []},
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))
        for window in snap.windows:
            self.assertIsNone(window.used_percent)

    def test_exhausted_individual_limit_is_a_blocker(self) -> None:
        snap = _parse(_load("ratelimits-spend-control-exhausted.json"))
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
            self.assertIsNone(window.remaining_percent)
        self.assertIn("percentage_unknown", _codes(snap))

    def test_exhausted_by_used_over_limit_is_a_blocker(self) -> None:
        payload = _result(
            _snapshot(
                dict(_BOTH_SLOTS),
                extra={
                    "individualLimit": {
                        "limit": 10,
                        "used": 11,
                        "remainingPercent": None,
                        "resetsAt": 1788306212,
                    }
                },
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        for window in snap.windows:
            self.assertIsNone(window.used_percent)

    def test_malformed_credits_fail_closed(self) -> None:
        malformed: tuple[object, ...] = (
            {"hasCredits": "yes", "unlimited": False, "balance": "1.00"},
            {"hasCredits": True, "unlimited": 1, "balance": None},
            {"hasCredits": True, "unlimited": False, "balance": 1000},
            {"hasCredits": True, "unlimited": False, "balance": None,
             "extra": {"nested": 1}},
            "many",
            5,
            ["x"],
        )
        for bad in malformed:
            with self.subTest(bad=bad):
                payload = _result(_snapshot(dict(_BOTH_SLOTS), extra={"credits": bad}))
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))
                self.assertEqual(snap.windows, ())

    def test_malformed_individual_limit_fails_closed(self) -> None:
        malformed: tuple[object, ...] = (
            {"limit": "10", "used": "1", "remainingPercent": "60"},
            {"limit": 10, "used": 1, "remainingPercent": True},
            {"limit": 10, "used": 1, "resetsAt": "soon"},
            {"limit": 10, "used": 1, "extra": {"x": 1}},
            "unlimited",
            7,
            ["x"],
        )
        for bad in malformed:
            with self.subTest(bad=bad):
                payload = _result(
                    _snapshot(dict(_BOTH_SLOTS), extra={"individualLimit": bad})
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))
                self.assertEqual(snap.windows, ())

    def test_malformed_reset_credits_fail_closed(self) -> None:
        malformed: tuple[object, ...] = (
            {"availableCount": "2"},
            {"availableCount": 2, "credits": {"a": 1}, "extra": [1]},
            "available",
            2,
            True,
            [],
        )
        for bad in malformed:
            with self.subTest(bad=bad):
                payload = _result(_snapshot(dict(_BOTH_SLOTS)), reset_credits=bad)
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))
                self.assertEqual(snap.windows, ())

    def test_null_and_absent_states_are_clear(self) -> None:
        extra_cases: tuple[dict[str, object], ...] = (
            {"credits": None, "individualLimit": None, "spendControlReached": None},
            {},  # option-typed members may be missing entirely
        )
        for extra in extra_cases:
            with self.subTest(extra=extra):
                payload = _result(_snapshot(dict(_BOTH_SLOTS), extra=extra))
                snap = _parse(payload)
                self.assertEqual(snap.status, "ok", msg=repr(extra))
                self.assertEqual(_codes(snap), set())

    def test_string_amounts_accepted_structurally_as_unrepresentable(self) -> None:
        payload = _result(
            _snapshot(
                dict(_BOTH_SLOTS),
                extra={
                    "individualLimit": {
                        "limit": "100.00",
                        "used": "40.00",
                        "remainingPercent": 60,
                        "resetsAt": 1788306212,
                    }
                },
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))

    def test_boolean_spend_control_blocker_never_reports_healthy(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS), extra={"spendControlReached": True})
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
        self.assertIn("percentage_unknown", _codes(snap))

    def test_non_boolean_spend_control_value_is_drift(self) -> None:
        for bad in ("yes", 1, ["blocked"], {"blocked": True}):
            with self.subTest(bad=bad):
                payload = _result(
                    _snapshot(
                        dict(_BOTH_SLOTS), extra={"spendControlReached": bad}
                    )
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))


class EnvelopeRequirements(unittest.TestCase):
    """The exact tagged schema requires all three envelope members."""

    def test_missing_required_member_fails_closed(self) -> None:
        for member in ("rateLimits", "rateLimitsByLimitId", "rateLimitResetCredits"):
            with self.subTest(member=member):
                payload = _result(_snapshot(dict(_BOTH_SLOTS)), omit=(member,))
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed")
                self.assertEqual(snap.windows, ())
                self.assertEqual(_codes(snap), {"schema_changed"})

    def test_explicit_null_members_are_valid_absent_states(self) -> None:
        payload = _result(_snapshot(dict(_BOTH_SLOTS)))
        snap = _parse(payload)
        self.assertEqual(snap.status, "ok")

    def test_non_mapping_envelope_member_is_drift(self) -> None:
        bad_members: tuple[tuple[str, object], ...] = (
            ("rateLimits", []),
            ("rateLimitsByLimitId", "codex"),
            ("rateLimitResetCredits", 5),
        )
        for member, bad in bad_members:
            with self.subTest(member=member):
                payload: dict[str, object] = _result(_snapshot(dict(_BOTH_SLOTS)))
                payload[member] = bad
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))

    def test_additive_envelope_structured_member_is_drift(self) -> None:
        extra_cases: tuple[dict[str, object], ...] = (
            {"newLimits": {"x": 1}},
            {"extraList": [{"x": 1}]},
        )
        for extra in extra_cases:
            with self.subTest(extra=extra):
                payload = _result(
                    _snapshot(dict(_BOTH_SLOTS)), envelope_extra=extra
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(extra))

    def test_additive_envelope_scalar_member_tolerated(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)), envelope_extra={"newScalar": True}
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "ok")


class AdditionalBuckets(unittest.TestCase):
    """Every additional bucket validates as a full quota snapshot."""

    def _bucket(
        self,
        *,
        limit_id: object = "gpt-reserve",
        used_percent: object = 5,
        duration: object = 300,
        secondary: object = None,
        reached: object = None,
        spend: object = None,
        individual: object = None,
    ) -> dict[str, object]:
        bucket: dict[str, object] = {
            "limitId": limit_id,
            "primary": _window(used_percent, duration),
            "secondary": secondary,
            "rateLimitReachedType": reached,
        }
        if spend is not None:
            bucket["spendControlReached"] = spend
        if individual is not None:
            bucket["individualLimit"] = individual
        return bucket

    def test_null_and_empty_bucket_maps_are_healthy(self) -> None:
        bucket_maps: tuple[object, ...] = (None, {})
        for buckets in bucket_maps:
            with self.subTest(buckets=buckets):
                payload = _result(_snapshot(dict(_BOTH_SLOTS)), buckets=buckets)
                snap = _parse(payload)
                self.assertEqual(snap.status, "ok")
                self.assertEqual(_codes(snap), set())

    def test_present_bucket_degrades_to_unknown_with_pairs(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={"gpt-reserve": self._bucket()},
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))
        for window in snap.windows:
            self.assertIsNotNone(window.used_percent)
        self.assertNotIn("percentage_unknown", _codes(snap))

    def test_bucket_identity_mismatch_fails_closed(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={"gpt-reserve": self._bucket(limit_id="other-limit")},
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())

    def test_bucket_key_shadowing_main_identity_fails_closed(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={"codex": self._bucket(limit_id="codex")},
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_bucket_missing_identity_fails_closed(self) -> None:
        bucket = self._bucket()
        _ = bucket.pop("limitId")
        payload = _result(_snapshot(dict(_BOTH_SLOTS)), buckets={"gpt-reserve": bucket})
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_bucket_duplicate_periods_fail_closed(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={
                "gpt-reserve": self._bucket(
                    secondary=_window(10, 300, 1788748064)
                )
            },
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_bucket_malformed_window_fails_closed(self) -> None:
        bucket = self._bucket()
        bucket["primary"] = {"usedPercent": 5}  # no duration discriminator
        payload = _result(_snapshot(dict(_BOTH_SLOTS)), buckets={"gpt-reserve": bucket})
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_bucket_structured_drift_fails_closed(self) -> None:
        bucket = self._bucket()
        bucket["unknownObject"] = {"x": 1}
        payload = _result(_snapshot(dict(_BOTH_SLOTS)), buckets={"gpt-reserve": bucket})
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_bucket_exhaustion_withholds_main_pairs(self) -> None:
        for bucket in (
            self._bucket(used_percent=100),
            self._bucket(reached="rate_limit_reached"),
        ):
            with self.subTest(bucket=bucket):
                payload = _result(
                    _snapshot(dict(_BOTH_SLOTS)),
                    buckets={"gpt-reserve": bucket},
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "unknown")
                for window in snap.windows:
                    self.assertIsNone(window.used_percent)
                self.assertIn("percentage_unknown", _codes(snap))
                self.assertIn("telemetry_unknown", _codes(snap))

    def test_bucket_spend_control_blocker_withholds_main_pairs(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={"gpt-reserve": self._bucket(spend=True)},
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
        self.assertIn("percentage_unknown", _codes(snap))

    def test_bucket_exhausted_individual_limit_withholds_main_pairs(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={
                "gpt-reserve": self._bucket(
                    individual={
                        "limit": 10,
                        "used": 10,
                        "remainingPercent": 0,
                        "resetsAt": 1788306212,
                    }
                )
            },
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        for window in snap.windows:
            self.assertIsNone(window.used_percent)

    def test_bucket_unrepresentable_state_withholds_main_pairs(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={
                "gpt-reserve": self._bucket(
                    individual={
                        "limit": 10,
                        "used": 1,
                        "remainingPercent": 90,
                        "resetsAt": 1788306212,
                    }
                )
            },
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        for window in snap.windows:
            self.assertIsNone(window.used_percent)

    def test_exhausted_bucket_fixture(self) -> None:
        snap = _parse(_load("ratelimits-additional-bucket-exhausted.json"))
        self.assertEqual(snap.status, "unknown")
        self.assertEqual(snap.plan, "pro")
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
        self.assertIn("telemetry_unknown", _codes(snap))
        self.assertNotIn("gpt-reserve", _canonical_json(snap))


class UnknownDurationFixture(unittest.TestCase):
    def test_unknown_window_preserved_without_guessing(self) -> None:
        snap = _parse(_load("ratelimits-unknown-duration.json"))
        # The weekly sibling is validated, but the five-hour constraint is
        # missing, so the snapshot must not appear healthy.
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))
        unknown = _windows(snap, resource="tokens", kind="unknown")
        self.assertEqual(len(unknown), 1)
        uw = unknown[0]
        self.assertEqual(uw.used_percent, 41)
        self.assertEqual(uw.remaining_percent, 59)
        self.assertEqual(uw.duration_seconds, 3_600)  # validated duration kept
        self.assertEqual(uw.resets_at, RESET_1788000000)
        self.assertEqual(uw.window_id, "primary")

    def test_known_sibling_survives_with_its_pair(self) -> None:
        snap = _parse(_load("ratelimits-unknown-duration.json"))
        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0].duration_seconds, 604_800)
        self.assertEqual(weekly[0].used_percent, 72)


class ExhaustedAndZeroFixtures(unittest.TestCase):
    def test_backend_reached_degrades_to_unknown_without_pairs(self) -> None:
        snap = _parse(_load("ratelimits-exhausted-reached.json"))
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))
        self.assertEqual(len(snap.windows), 2)
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
            self.assertIsNone(window.remaining_percent)
            self.assertIsNotNone(window.kind)
            self.assertIsNotNone(window.resets_at)
        scoped = {(d.code, d.window_id) for d in snap.diagnostics}
        self.assertIn(("percentage_unknown", "primary"), scoped)
        self.assertIn(("percentage_unknown", "secondary"), scoped)

    def test_known_exhaustion_without_blockers_is_ok(self) -> None:
        payload = _result(
            _snapshot(
                {
                    "primary": _window(100, 300),
                    "secondary": _window(100, 10080, 1788748064),
                }
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "ok")
        for window in snap.windows:
            self.assertEqual(
                (window.used_percent, window.remaining_percent), (100, 0)
            )

    def test_reached_with_midrange_percentages_still_withholds_pairs(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS), extra={"rateLimitReachedType": "rate_limit_reached"})
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
            self.assertIsNone(window.remaining_percent)
        self.assertIn("telemetry_unknown", _codes(snap))
        self.assertIn("percentage_unknown", _codes(snap))

    def test_every_evidenced_reached_enum_member_degrades(self) -> None:
        for reached in sorted(REACHED_TYPES):
            with self.subTest(reached=reached):
                payload = _result(
                    _snapshot(
                        dict(_BOTH_SLOTS),
                        extra={"rateLimitReachedType": reached},
                    )
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "unknown")
                self.assertIsNone(snap.windows[0].used_percent)
                self.assertNotIn(reached, _canonical_json(snap))

    def test_unknown_reached_value_degrades_not_healthy(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS), extra={"rateLimitReachedType": "brand-new-type"})
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))
        self.assertIsNone(snap.windows[0].used_percent)

    def test_non_string_reached_value_is_drift(self) -> None:
        for bad in (5, True, {"x": 1}, ["x"]):
            with self.subTest(bad=bad):
                payload = _result(
                    _snapshot(
                        dict(_BOTH_SLOTS), extra={"rateLimitReachedType": bad}
                    )
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))

    def test_known_zero_usage_is_not_missing(self) -> None:
        snap = _parse(_load("ratelimits-zero-usage.json"))
        self.assertEqual(snap.status, "ok")
        for window in snap.windows:
            self.assertEqual(
                (window.used_percent, window.remaining_percent), (0, 100)
            )
        self.assertNotIn("percentage_unknown", _codes(snap))


class DegradedFixture(unittest.TestCase):
    def test_string_percentage_omits_pair(self) -> None:
        snap = _parse(_load("ratelimits-degraded.json"))
        five_hour = _windows(snap, resource="tokens", kind="five_hour")
        self.assertEqual(len(five_hour), 1)
        self.assertIsNone(five_hour[0].used_percent)
        self.assertIsNone(five_hour[0].remaining_percent)
        self.assertIn("percentage_unknown", _codes(snap))

    def test_missing_reset_omits_resets_at(self) -> None:
        snap = _parse(_load("ratelimits-degraded.json"))
        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(len(weekly), 1)
        self.assertIsNone(weekly[0].resets_at)
        self.assertIn("reset_unknown", _codes(snap))

    def test_unevidenced_plan_label_is_omitted(self) -> None:
        snap = _parse(_load("ratelimits-degraded.json"))
        self.assertEqual(snap.status, "ok")
        self.assertIsNone(snap.plan)
        self.assertNotIn('"plan"', _canonical_json(snap))

    def test_degraded_snapshot_validates_through_v1(self) -> None:
        snap = _parse(_load("ratelimits-degraded.json"))
        reparsed = CapacitySnapshot.from_dict(snap.to_dict())
        self.assertEqual(reparsed, snap)


class SchemaChangedFixture(unittest.TestCase):
    def test_fails_closed_without_partial_decoding(self) -> None:
        snap = _parse(_load("ratelimits-schema-changed.json"))
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})
        self.assertIsNone(snap.plan)
        text = _canonical_json(snap)
        for forbidden in (
            "consumedPercent", "resetTimeUtc", "planTier", "five_hour",
        ):
            self.assertNotIn(forbidden, text)

    def test_malformed_credits_fixture(self) -> None:
        snap = _parse(_load("ratelimits-credits-malformed.json"))
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})


# ═════════════════════ coverage and identity validation ══════════════════════


class CoverageValidation(unittest.TestCase):
    """A response missing an expected window constraint never looks healthy."""

    def test_missing_weekly_degrades_to_unknown(self) -> None:
        for slots in (
            {"primary": _window(6, 300), "secondary": None},
            {"primary": _window(6, 300)},  # key absent entirely
        ):
            with self.subTest(slots=sorted(slots)):
                payload = _result(_snapshot(dict(slots)))
                snap = _parse(payload)
                self.assertEqual(snap.status, "unknown")
                telemetry = [
                    d.code for d in snap.diagnostics
                    if d.code == "telemetry_unknown"
                ]
                self.assertEqual(telemetry, ["telemetry_unknown"])
                self.assertEqual(len(snap.windows), 1)
                five_hour = snap.windows[0]
                self.assertEqual(five_hour.kind, "five_hour")
                self.assertEqual(five_hour.used_percent, 6)  # validated fact kept

    def test_missing_five_hour_degrades_to_unknown(self) -> None:
        payload = _result(
            _snapshot({"secondary": _window(52, 10080, 1788748064)})
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        self.assertEqual(len(snap.windows), 1)
        self.assertEqual(snap.windows[0].kind, "weekly")

    def test_no_windows_at_all_is_unknown_never_ok(self) -> None:
        payload = _result(_snapshot({}))
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"telemetry_unknown"})

    def test_duplicate_known_period_fails_closed(self) -> None:
        for mins in (300, 10080):
            with self.subTest(mins=mins):
                payload = _result(
                    _snapshot(
                        {
                            "primary": _window(6, mins),
                            "secondary": _window(52, mins, 1788748064),
                        }
                    )
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed")
                self.assertEqual(snap.windows, ())
                self.assertEqual(_codes(snap), {"schema_changed"})

    def test_wrong_quota_identity_fails_closed(self) -> None:
        for limit_id in ("gpt_reserve", "codex-xl", None, 5, "CODEX"):
            with self.subTest(limit_id=limit_id):
                payload = _result(_snapshot(dict(_BOTH_SLOTS), limit_id=limit_id))
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(limit_id))
                self.assertEqual(snap.windows, ())
                self.assertEqual(_codes(snap), {"schema_changed"})

    def test_additive_structured_snapshot_member_fails_closed(self) -> None:
        extra_cases: tuple[dict[str, object], ...] = (
            {"tertiary": {"usedPercent": 5, "windowDurationMins": 30}},
            {"extraWindows": [{"kind": "five_hour"}]},
        )
        for extra in extra_cases:
            with self.subTest(extra=extra):
                payload = _result(_snapshot(dict(_BOTH_SLOTS), extra=extra))
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(extra))
                self.assertEqual(snap.windows, ())
                self.assertEqual(_codes(snap), {"schema_changed"})


# ══════════════════════════ focused parser behavior ══════════════════════════


class PercentageNormalization(unittest.TestCase):
    def test_malformed_percentages_omit_pair(self) -> None:
        for bad in ("6", 6.5, True, -1, 101, None, [6], {"v": 6}):
            with self.subTest(bad=bad):
                payload = _result(
                    _snapshot(
                        {
                            "primary": _window(bad, 300),
                            "secondary": _window(10, 10080, 1788748064),
                        }
                    )
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "ok", msg=repr(bad))
                primary = _windows(snap, resource="tokens", kind="five_hour")
                self.assertEqual(len(primary), 1)
                self.assertIsNone(primary[0].used_percent, msg=repr(bad))
                self.assertIsNone(primary[0].remaining_percent, msg=repr(bad))
                self.assertIn("percentage_unknown", _codes(snap))

    def test_used_orientation_boundary_values(self) -> None:
        for used, remaining in ((0, 100), (50, 50), (100, 0)):
            with self.subTest(used=used):
                payload = _result(
                    _snapshot(
                        {
                            "primary": _window(used, 300),
                            "secondary": _window(10, 10080, 1788748064),
                        }
                    )
                )
                snap = _parse(payload)
                window = _windows(snap, resource="tokens", kind="five_hour")[0]
                self.assertEqual(
                    (window.used_percent, window.remaining_percent),
                    (used, remaining),
                )


class ResetNormalization(unittest.TestCase):
    def _snap_with(self, resets_at: object) -> CapacitySnapshot:
        payload = _result(
            _snapshot(
                {
                    "primary": _window(10, 300, resets_at),
                    "secondary": _window(10, 10080, 1788748064),
                }
            )
        )
        return _parse(payload)

    def test_valid_epoch_seconds_preserved(self) -> None:
        snap = self._snap_with(1788306212)
        self.assertEqual(snap.windows[0].resets_at, RESET_1788306212)
        self.assertNotIn("reset_unknown", _codes(snap))

    def test_malformed_reset_values_omit_resets_at(self) -> None:
        for bad in (
            None,
            0,
            -5,
            1788306212.0,
            True,
            "1788306212",
            1788306212000,  # epoch milliseconds must not be misread
            999_999_999,
            10_000_000_000,
            10**30,
        ):
            with self.subTest(bad=bad):
                snap = self._snap_with(bad)
                self.assertIsNone(snap.windows[0].resets_at, msg=repr(bad))
                self.assertIn("reset_unknown", _codes(snap))

    def test_epoch_milliseconds_never_become_1970(self) -> None:
        snap = self._snap_with(1788306212000)
        self.assertNotIn("1970", _canonical_json(snap))


class WindowStructureDrift(unittest.TestCase):
    def test_object_without_duration_discriminator_fails_closed(self) -> None:
        payload = _result(
            _snapshot(
                {
                    "primary": {"usedPercent": 6, "resetsAt": 1788306212},
                    "secondary": _window(52, 10080, 1788748064),
                }
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())

    def test_non_positive_or_non_integer_duration_fails_closed(self) -> None:
        for bad in (0, -300, 300.0, True, "300", None):
            with self.subTest(bad=bad):
                payload = _result(
                    _snapshot(
                        {
                            "primary": _window(6, bad),
                            "secondary": _window(52, 10080, 1788748064),
                        }
                    )
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))
                self.assertEqual(snap.windows, ())

    def test_scalar_window_slot_fails_closed(self) -> None:
        payload = _result(
            _snapshot(
                {"primary": "unlimited", "secondary": _window(52, 10080, 1788748064)}
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_healthy_sibling_does_not_survive_drift(self) -> None:
        payload = _result(
            _snapshot(
                {"primary": {"foo": "bar"}, "secondary": _window(52, 10080, 1788748064)}
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertIsNone(snap.plan)

    def test_additive_scalar_fields_are_tolerated(self) -> None:
        payload = _result(
            _snapshot(
                dict(_BOTH_SLOTS), extra={"newScalarField": 7, "another": "text"}
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "ok")
        self.assertEqual(len(snap.windows), 2)
        self.assertEqual(_codes(snap), set())

    def test_window_structured_additive_member_is_drift(self) -> None:
        # Consistent conservative posture at every level: an additive
        # structured member inside a window object is drift.
        entry: dict[str, object] = {
            **_window(6, 300),
            "experimental": {"nested": True},
        }
        payload = _result(
            _snapshot(
                {"primary": entry, "secondary": _window(52, 10080, 1788748064)}
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())

    def test_window_additive_scalar_member_is_tolerated(self) -> None:
        entry: dict[str, object] = {**_window(6, 300), "newScalar": 7}
        payload = _result(
            _snapshot(
                {"primary": entry, "secondary": _window(52, 10080, 1788748064)}
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "ok")
        self.assertEqual(len(snap.windows), 2)

    def test_unknown_durations_on_both_slots_stay_unknown(self) -> None:
        payload = _result(
            _snapshot(
                {
                    "primary": _window(10, 60, 1788000000),
                    "secondary": _window(20, 90, 1788748064),
                }
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        self.assertEqual(len(snap.windows), 2)
        for window in snap.windows:
            self.assertEqual(window.kind, "unknown")
            self.assertIsNotNone(window.duration_seconds)
        self.assertIn("window_semantics_unknown", _codes(snap))

    def test_huge_duration_degrades_to_unknown_without_raising(self) -> None:
        payload = _result(
            _snapshot(
                {
                    "primary": _window(10, 10**60),
                    "secondary": _window(20, 10080, 1788748064),
                }
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        window = _windows(snap, resource="tokens", kind="unknown")[0]
        self.assertEqual(window.duration_seconds, 10**60 * 60)


class EnvelopeAndStructure(unittest.TestCase):
    def test_non_mapping_results_fail_closed(self) -> None:
        bad_payloads: tuple[object, ...] = ([], "result", None, 42, True)
        for bad in bad_payloads:
            with self.subTest(bad=bad):
                snap = parse_codex_rate_limits_result(bad, retrieved_at=RETRIEVED_AT)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))
                self.assertEqual(snap.windows, ())
                self.assertEqual(_codes(snap), {"schema_changed"})

    def test_missing_or_non_mapping_rate_limits_fails_closed(self) -> None:
        bad_containers: tuple[object, ...] = (None, [], "codex", 0)
        for rate_limits in bad_containers:
            with self.subTest(rate_limits=rate_limits):
                payload: dict[str, object] = {
                    "rateLimits": rate_limits,
                    "rateLimitsByLimitId": None,
                    "rateLimitResetCredits": None,
                }
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(rate_limits))
                self.assertEqual(snap.windows, ())

    def test_non_string_limit_name_is_drift(self) -> None:
        payload = _result(_snapshot(dict(_BOTH_SLOTS), extra={"limitName": 5}))
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_non_string_plan_type_is_drift(self) -> None:
        payload = _result(_snapshot(dict(_BOTH_SLOTS), plan_type=5))
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")


class PlanNormalization(unittest.TestCase):
    def test_evidenced_plan_labels_are_retained(self) -> None:
        for plan in (
            "free", "go", "plus", "pro", "prolite", "team", "business",
            "edu", "edu_plus", "edu_pro", "enterprise", "ent26",
            "enterprise_cbp_automation", "enterprise_cbp_usage_based",
            "self_serve_business_prolite", "self_serve_business_usage_based",
            "unknown",
        ):
            with self.subTest(plan=plan):
                payload = _result(_snapshot(dict(_BOTH_SLOTS), plan_type=plan))
                snap = _parse(payload)
                self.assertEqual(snap.status, "ok")
                self.assertEqual(snap.plan, plan)

    def test_unevidenced_or_unsafe_labels_are_omitted(self) -> None:
        for level in (
            "luna", "edu-pro", "selfServeBusinessProlite", "Plus", "plus!",
            "run", "", None,
        ):
            with self.subTest(level=level):
                payload = _result(_snapshot(dict(_BOTH_SLOTS), plan_type=level))
                snap = _parse(payload)
                self.assertEqual(snap.status, "ok", msg=repr(level))
                self.assertIsNone(snap.plan, msg=repr(level))
                self.assertNotIn('"plan"', _canonical_json(snap))

    def test_absent_plan_type_is_tolerated(self) -> None:
        payload = _result(_snapshot(dict(_BOTH_SLOTS), plan_type=None))
        snap = _parse(payload)
        self.assertEqual(snap.status, "ok")
        self.assertIsNone(snap.plan)

    def test_evidenced_members_preserved_verbatim(self) -> None:
        for level in ("edu_plus", "enterprise_cbp_automation"):
            with self.subTest(level=level):
                payload = _result(_snapshot(dict(_BOTH_SLOTS), plan_type=level))
                snap = _parse(payload)
                self.assertEqual(snap.plan, level)
                self.assertIn(f'"plan": "{level}"', _canonical_json(snap))


# ══════════════════════════ message classification ═══════════════════════════


class MessageClassification(unittest.TestCase):
    def test_response_shapes(self) -> None:
        messages: tuple[object, ...] = (
            {"id": 2, "result": {"rateLimits": {}}},
            {"jsonrpc": "2.0", "id": 2, "result": None},
            {"id": 2, "error": {"code": -32603, "message": "x"}},
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(
                    classify_app_server_message(message), "response"
                )

    def test_notification_shapes(self) -> None:
        messages: tuple[object, ...] = (
            {"method": "notifications/initialized"},
            {
                "method": "account/rateLimits/updated",
                "params": {"status": "syncing"},
                "emittedAtMs": 1788306212999,
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            # A string id is not an integer request identity: the message can
            # never match one of our responses, so it is safely ignorable.
            {"method": "x", "id": "7"},
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(
                    classify_app_server_message(message), "notification"
                )

    def test_request_shapes(self) -> None:
        messages: tuple[object, ...] = (
            {"id": 7, "method": "elicitation/create", "params": {}},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(classify_app_server_message(message), "request")

    def test_invalid_shapes(self) -> None:
        messages: tuple[object, ...] = (
            None,
            [],
            "message",
            42,
            {},
            {"id": 2},  # neither result nor error
            {"id": 2, "result": {}, "error": {}},  # both
            {"id": "2", "result": {}},  # string id
            {"id": True, "result": {}},  # boolean id
            {"method": 42},  # non-string method
            # Hybrids carrying method together with response fields are
            # neither well-formed requests nor responses: drift.
            {"method": "x", "result": {}},
            {"method": "x", "error": {"code": -1, "message": "y"}},
            {"id": 2, "method": "x", "result": {"rateLimits": {}}},
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(
                    classify_app_server_message(message), "invalid"
                )


class PurityAndDeterminism(unittest.TestCase):
    def test_repeated_parse_is_deterministic(self) -> None:
        payload = _load("ratelimits-ok-plus.json")
        reparsed = cast("dict[str, object]", json.loads(json.dumps(payload)))
        a = _parse(payload)
        b = _parse(reparsed)
        self.assertEqual(a, b)
        self.assertEqual(_canonical_json(a), _canonical_json(b))

    def test_invalid_retrieved_at_raises_typed_error(self) -> None:
        payload = _result(_snapshot(dict(_BOTH_SLOTS)))
        with self.assertRaises(CapacityError):
            _ = parse_codex_rate_limits_result(
                payload, retrieved_at="2026-09-03T20:00:00Z"
            )

    def test_all_fixtures_round_trip_through_v1(self) -> None:
        for name in ALL_FIXTURES:
            with self.subTest(name=name):
                snap = _parse(_load(name))
                reparsed = CapacitySnapshot.from_dict(snap.to_dict())
                self.assertEqual(reparsed, snap)

    def test_serialized_output_carries_no_provider_material(self) -> None:
        for name in ALL_FIXTURES:
            with self.subTest(name=name):
                snap = _parse(_load(name))
                text = _canonical_json(snap)
                for forbidden in (
                    "windowDurationMins", "usedPercent", "resetsAt",
                    "planType", "rateLimitReachedType", "limitId",
                    "rateLimits", "rateLimitsByLimitId", "codexHome",
                    "userAgent", "auth.json", ".codex", "codex-cli",
                    "Authorization", "Bearer", "credits", "individualLimit",
                    "spendControlReached", "rate_limit_reached", "workspace",
                    "hasCredits", "balance", "availableCount", "gpt-reserve",
                ):
                    self.assertNotIn(forbidden, text, msg=f"{name}: {forbidden!r}")

    def test_parser_module_has_no_io_surface(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "scarcity_router"
            / "providers"
            / "openai_codex.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "import os", "import sys", "import subprocess", "import time",
            "import socket", "import ssl", "import urllib", "import http",
            "import requests", "getpass", "netrc", "tempfile", "shutil",
            "os.environ", "utcnow", "now(", "open(", "time.time", "Popen",
            ".run(",
        ):
            self.assertNotIn(forbidden, source, msg=f"forbidden: {forbidden!r}")


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
