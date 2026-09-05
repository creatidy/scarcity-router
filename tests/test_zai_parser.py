"""Parser and contract tests for the Z.ai quota-response adapter.

Every fixture under ``tests/fixtures/zai-coding-plan/`` participates here.
The parser under test is pure: it receives an already decoded payload and a
caller-supplied retrieval timestamp, and performs no clock, filesystem,
environment or network access.

All tests are deterministic and self-contained; the only file access is
reading the synthetic, redacted fixture inputs.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from scarcity_router import CapacityError, CapacitySnapshot, CapacityWindow
from scarcity_router.providers.zai import parse_zai_quota_response

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "zai-coding-plan"
RETRIEVED_AT = "2026-09-01T22:49:51.000Z"

# Independently precomputed canonical UTC renderings of the fixture
# epoch-millisecond values (they must not be recomputed via the parser).
RESET_1788000000000 = "2026-08-29T10:40:00.000Z"
RESET_1788500000000 = "2026-09-04T05:33:20.000Z"


def _load(name: str) -> dict[str, object]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return cast("dict[str, object]", json.load(handle))


def _limits_payload(
    limits: list[object],
    level: object = "pro",
    code: int = 200,
    success: bool = True,
) -> dict[str, object]:
    return {
        "code": code,
        "msg": "Operation successful",
        "data": {"limits": limits, "level": level},
        "success": success,
    }


def _token_limit(
    unit: object,
    number: object,
    percentage: object,
    next_reset_time: object = 1788000000000,
) -> dict[str, object]:
    limit: dict[str, object] = {"type": "TOKENS_LIMIT"}
    if unit is not None:
        limit["unit"] = unit
    if number is not None:
        limit["number"] = number
    if percentage is not None:
        limit["percentage"] = percentage
    if next_reset_time is not None:
        limit["nextResetTime"] = next_reset_time
    return limit


def _windows(
    snap: CapacitySnapshot,
    *,
    resource: str,
    kind: str,
) -> list[CapacityWindow]:
    return [
        w
        for w in snap.windows
        if w.resource == resource and w.kind == kind
    ]


def _codes(snap: CapacitySnapshot) -> set[str]:
    return {d.code for d in snap.diagnostics}


def _canonical_json(snap: CapacitySnapshot) -> str:
    return json.dumps(snap.to_dict(), sort_keys=True)


def _sorted_window_json(snap: CapacitySnapshot) -> list[str]:
    return sorted(
        json.dumps(w.to_dict(), sort_keys=True) for w in snap.windows
    )


# ══════════════════════════ fixture-driven tests ═════════════════════════════


class KnownWindowsFixture(unittest.TestCase):
    def test_full_normalization(self) -> None:
        snap = parse_zai_quota_response(
            _load("quota-200-known-windows.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        self.assertEqual(snap.schema_version, 2)
        self.assertEqual(snap.provider, "zai")
        self.assertEqual(snap.source, "zai_usage_endpoint")
        self.assertEqual(snap.plan, "pro")
        self.assertEqual(snap.retrieved_at, RETRIEVED_AT)
        self.assertEqual(len(snap.windows), 2)
        self.assertEqual(_codes(snap), set())

        five_hour = _windows(snap, resource="tokens", kind="five_hour")
        self.assertEqual(len(five_hour), 1)
        fh = five_hour[0]
        self.assertEqual(fh.duration_seconds, 18_000)
        self.assertEqual(fh.used_percent, 35)
        self.assertEqual(fh.remaining_percent, 65)
        self.assertEqual(fh.resets_at, RESET_1788000000000)
        self.assertEqual(fh.window_id, "tokens_limit-3-5")

        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(len(weekly), 1)
        wk = weekly[0]
        self.assertEqual(wk.duration_seconds, 604_800)
        self.assertEqual(wk.used_percent, 72)
        self.assertEqual(wk.remaining_percent, 28)
        self.assertEqual(wk.resets_at, RESET_1788500000000)
        self.assertEqual(wk.window_id, "tokens_limit-6-1")

    def test_order_independent_classification(self) -> None:
        payload = _load("quota-200-known-windows.json")
        direct = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        limits = cast("list[object]", cast("dict[str, object]", payload["data"])["limits"])
        cast("dict[str, object]", payload["data"])["limits"] = list(reversed(limits))
        shuffled = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        # Classification follows validated metadata, never array position.
        self.assertEqual(_sorted_window_json(direct), _sorted_window_json(shuffled))
        self.assertEqual(
            [w.kind for w in sorted(shuffled.windows, key=lambda w: w.window_id or "")],
            ["five_hour", "weekly"],
        )


class UnknownWindowFixture(unittest.TestCase):
    def test_known_sibling_survives(self) -> None:
        snap = parse_zai_quota_response(
            _load("quota-200-unknown-window.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0].duration_seconds, 604_800)
        self.assertEqual(weekly[0].used_percent, 72)
        self.assertEqual(weekly[0].remaining_percent, 28)
        self.assertEqual(weekly[0].window_id, "tokens_limit-6-1")

    def test_unknown_window_preserved_without_guessing(self) -> None:
        snap = parse_zai_quota_response(
            _load("quota-200-unknown-window.json"), retrieved_at=RETRIEVED_AT
        )
        unknown = _windows(snap, resource="tokens", kind="unknown")
        self.assertEqual(len(unknown), 1)
        uw = unknown[0]
        self.assertIsNone(uw.duration_seconds)  # no invented period
        self.assertEqual(uw.used_percent, 41)
        self.assertEqual(uw.remaining_percent, 59)
        self.assertEqual(uw.resets_at, RESET_1788000000000)
        # Deterministic safe identity for the partially identified window.
        self.assertEqual(uw.window_id, "tokens_limit-7-1")
        scoped = {
            d.window_id
            for d in snap.diagnostics
            if d.code == "window_semantics_unknown"
        }
        self.assertEqual(scoped, {"tokens_limit-7-1"})
        # The weekly sibling's missing reset is also reported.
        self.assertIn("reset_unknown", _codes(snap))


class WeeklyMissingFixture(unittest.TestCase):
    def test_weekly_is_not_synthesized(self) -> None:
        snap = parse_zai_quota_response(
            _load("quota-200-weekly-missing.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        self.assertEqual(len(snap.windows), 1)
        self.assertEqual(snap.windows[0].kind, "five_hour")
        self.assertEqual(_codes(snap), set())


class DegradedValuesFixture(unittest.TestCase):
    def test_known_zero_is_not_missing(self) -> None:
        snap = parse_zai_quota_response(
            _load("quota-200-degraded-values.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        five_hour = _windows(snap, resource="tokens", kind="five_hour")
        self.assertEqual(len(five_hour), 1)
        # percentage 0 is known zero usage, not missing telemetry.
        self.assertEqual(five_hour[0].used_percent, 0)
        self.assertEqual(five_hour[0].remaining_percent, 100)

    def test_missing_percentage_and_reset_degrade(self) -> None:
        snap = parse_zai_quota_response(
            _load("quota-200-degraded-values.json"), retrieved_at=RETRIEVED_AT
        )
        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(len(weekly), 1)
        self.assertIsNone(weekly[0].used_percent)
        self.assertIsNone(weekly[0].remaining_percent)
        self.assertIsNone(weekly[0].resets_at)
        # A null nextResetTime also omits resets_at.
        five_hour = _windows(snap, resource="tokens", kind="five_hour")[0]
        self.assertIsNone(five_hour.resets_at)

    def test_degradation_diagnostics_and_validity(self) -> None:
        snap = parse_zai_quota_response(
            _load("quota-200-degraded-values.json"), retrieved_at=RETRIEVED_AT
        )
        scoped = {
            (d.code, d.window_id)
            for d in snap.diagnostics
        }
        self.assertIn(("reset_unknown", "tokens_limit-3-5"), scoped)
        self.assertIn(("percentage_unknown", "tokens_limit-6-1"), scoped)
        self.assertIn(("reset_unknown", "tokens_limit-6-1"), scoped)
        self.assertNotIn(("percentage_unknown", "tokens_limit-3-5"), scoped)
        # The degraded snapshot must still be a valid v2 snapshot.
        reparsed = CapacitySnapshot.from_dict(snap.to_dict())
        self.assertEqual(reparsed, snap)


class SchemaChangedFixture(unittest.TestCase):
    def test_fails_closed_without_partial_decoding(self) -> None:
        snap = parse_zai_quota_response(
            _load("quota-schema-changed.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})
        self.assertIsNone(snap.plan)
        # No provider field names or values leak into the normalized output.
        text = _canonical_json(snap)
        for forbidden in ("period", "consumedPercent", "resetTimeUtc",
                          "planTier", "P5H", "P1W"):
            self.assertNotIn(forbidden, text)


class AuthFailedFixture(unittest.TestCase):
    def test_maps_to_auth_required(self) -> None:
        snap = parse_zai_quota_response(
            _load("quota-401-auth-failed.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "auth_required")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"auth_required"})
        self.assertIsNone(snap.plan)
        text = _canonical_json(snap)
        self.assertNotIn("Authentication failed", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("Bearer", text)


# ══════════════════════════ focused parser behavior ══════════════════════════


class PercentageNormalization(unittest.TestCase):
    def test_100_is_known_exhaustion(self) -> None:
        payload = _limits_payload([_token_limit(3, 5, 100)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        w = snap.windows[0]
        self.assertEqual((w.used_percent, w.remaining_percent), (100, 0))
        self.assertEqual(snap.status, "ok")
        self.assertNotIn("percentage_unknown", _codes(snap))

    def test_negative_percentage_omits_pair(self) -> None:
        payload = _limits_payload([_token_limit(3, 5, -1)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        w = snap.windows[0]
        self.assertIsNone(w.used_percent)
        self.assertIsNone(w.remaining_percent)
        self.assertEqual(w.kind, "five_hour")  # window itself survives
        self.assertIn("percentage_unknown", _codes(snap))

    def test_above_100_percentage_omits_pair(self) -> None:
        payload = _limits_payload([_token_limit(3, 5, 101)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        self.assertIsNone(snap.windows[0].used_percent)
        self.assertIsNone(snap.windows[0].remaining_percent)
        self.assertIn("percentage_unknown", _codes(snap))

    def test_boolean_percentage_is_not_an_integer(self) -> None:
        payload = _limits_payload([_token_limit(3, 5, True)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        self.assertIsNone(snap.windows[0].used_percent)
        self.assertIn("percentage_unknown", _codes(snap))

    def test_string_percentage_is_rejected(self) -> None:
        payload = _limits_payload([_token_limit(3, 5, "35")])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        self.assertIsNone(snap.windows[0].used_percent)
        self.assertIn("percentage_unknown", _codes(snap))


class ResetNormalization(unittest.TestCase):
    def test_malformed_reset_values_omit_resets_at(self) -> None:
        for bad in ("1788000000000", 1788000000000.0, True):
            payload = _limits_payload([_token_limit(3, 5, 35, bad)])
            snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
            self.assertIsNone(snap.windows[0].resets_at, msg=repr(bad))
            self.assertIn("reset_unknown", _codes(snap))

    def test_valid_thirteen_digit_epoch_ms_normalizes(self) -> None:
        payload = _limits_payload([_token_limit(3, 5, 35, 1_788_000_000_000)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.windows[0].resets_at, RESET_1788000000000)
        self.assertNotIn("reset_unknown", _codes(snap))

    def test_epoch_seconds_value_is_not_misread_as_millis(self) -> None:
        # A plausible 10-digit epoch-seconds value must never become a 1970
        # timestamp via a millisecond reading.
        payload = _limits_payload([_token_limit(3, 5, 35, 1_788_000_000)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        self.assertIsNone(snap.windows[0].resets_at)
        self.assertIn("reset_unknown", _codes(snap))
        self.assertNotIn("1970", _canonical_json(snap))

    def test_zero_and_negative_reset_are_rejected(self) -> None:
        for bad in (0, -1_500):
            payload = _limits_payload([_token_limit(3, 5, 35, bad)])
            snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
            self.assertIsNone(snap.windows[0].resets_at, msg=repr(bad))
            self.assertIn("reset_unknown", _codes(snap))

    def test_wrong_digit_counts_are_rejected(self) -> None:
        for bad in (999_999_999_999, 10_000_000_000_000):
            payload = _limits_payload([_token_limit(3, 5, 35, bad)])
            snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
            self.assertIsNone(snap.windows[0].resets_at, msg=repr(bad))
            self.assertIn("reset_unknown", _codes(snap))

    def test_thirteen_digit_lower_bound_normalizes(self) -> None:
        payload = _limits_payload([_token_limit(3, 5, 35, 1_000_000_000_000)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.windows[0].resets_at, "2001-09-09T01:46:40.000Z")

    def test_unrepresentable_reset_omits_resets_at(self) -> None:
        payload = _limits_payload([_token_limit(3, 5, 35, 10**30)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        self.assertIsNone(snap.windows[0].resets_at)
        self.assertIn("reset_unknown", _codes(snap))


class UnknownWindowIdentities(unittest.TestCase):
    def test_unknown_unit_number_combination(self) -> None:
        payload = _limits_payload([_token_limit(7, 1, 10)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        w = snap.windows[0]
        self.assertEqual((w.resource, w.kind), ("tokens", "unknown"))
        self.assertIsNone(w.duration_seconds)
        self.assertEqual(w.window_id, "tokens_limit-7-1")

    def test_missing_unit(self) -> None:
        payload = _limits_payload([_token_limit(None, 5, 10)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        w = snap.windows[0]
        self.assertEqual((w.resource, w.kind), ("tokens", "unknown"))
        self.assertIsNone(w.duration_seconds)
        self.assertEqual(w.window_id, "tokens_limit-x-5")

    def test_missing_number(self) -> None:
        payload = _limits_payload([_token_limit(3, None, 10)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        w = snap.windows[0]
        self.assertEqual((w.resource, w.kind), ("tokens", "unknown"))
        self.assertEqual(w.window_id, "tokens_limit-3-x")

    def test_non_integer_identity_fields(self) -> None:
        payload = _limits_payload([_token_limit("3", 5.0, 10)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        w = snap.windows[0]
        self.assertEqual((w.resource, w.kind), ("tokens", "unknown"))
        self.assertEqual(w.window_id, "tokens_limit-x-x")


class MalformedLimitStructure(unittest.TestCase):
    """Structural drift inside ``data.limits[]`` fails closed.

    An unknown window is still a provider *window object* with unknown
    semantics; a scalar, a list, an object without ``type``, or an object
    with a non-string ``type`` is a structurally incompatible successful
    response and must produce ``schema_changed`` with no partial windows.
    """

    def _snap_with_limits(self, limits: object) -> CapacitySnapshot:
        payload = _limits_payload([])
        payload["data"] = {"limits": limits, "level": "pro"}
        return parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)

    def test_scalar_entry_fails_closed(self) -> None:
        snap = self._snap_with_limits([42])
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})

    def test_list_entry_fails_closed(self) -> None:
        snap = self._snap_with_limits([[1, 2]])
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})

    def test_entry_missing_type_fails_closed(self) -> None:
        snap = self._snap_with_limits([{"unit": 3, "number": 5}])
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})

    def test_entry_non_string_type_fails_closed(self) -> None:
        snap = self._snap_with_limits([{"type": 42, "unit": 3, "number": 5}])
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})

    def test_healthy_sibling_does_not_survive_malformed_structure(self) -> None:
        limits: list[object] = [_token_limit(3, 5, 35), 42, _token_limit(6, 1, 72)]
        snap = self._snap_with_limits(limits)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})
        self.assertIsNone(snap.plan)

    def test_unknown_valid_string_type_is_preserved(self) -> None:
        for limit_type in ("CREDITS_LIMIT", ""):
            with self.subTest(limit_type=limit_type):
                snap = self._snap_with_limits(
                    [{"type": limit_type, "percentage": 5, "nextResetTime": 1788000000000}]
                )
                self.assertEqual(snap.status, "ok")
                self.assertEqual(len(snap.windows), 1)
                w = snap.windows[0]
                # An additive provider window type without evidenced semantics.
                self.assertEqual((w.resource, w.kind), ("unknown", "unknown"))
                self.assertIsNone(w.duration_seconds)
                # Unvalidated percentage/reset semantics are omitted, never
                # guessed from the evidenced-schema rules.
                self.assertIsNone(w.used_percent)
                self.assertIsNone(w.remaining_percent)
                self.assertIsNone(w.resets_at)
                # No window_id is invented from arbitrary type text.
                self.assertIsNone(w.window_id)
                self.assertEqual(
                    _codes(snap),
                    {"window_semantics_unknown", "percentage_unknown", "reset_unknown"},
                )
                for d in snap.diagnostics:
                    self.assertIsNone(d.window_id)
                # The raw provider type string never reaches the output.
                self.assertNotIn("CREDITS_LIMIT", _canonical_json(snap))
                reparsed = CapacitySnapshot.from_dict(snap.to_dict())
                self.assertEqual(reparsed, snap)


class WindowIdentitySafety(unittest.TestCase):
    """Window-ID generation is total: arbitrary identity magnitudes degrade
    deterministically instead of raising or escaping the safe-ID grammar."""

    def _window_for(self, unit: object, number: object) -> CapacitySnapshot:
        payload = _limits_payload([_token_limit(unit, number, 10)])
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "ok")
        reparsed = CapacitySnapshot.from_dict(snap.to_dict())
        self.assertEqual(reparsed, snap)
        return snap

    def test_huge_positive_unit_degrades(self) -> None:
        # Far beyond the int->str conversion limit; must never raise.
        snap = self._window_for(10**5000, 1)
        w = snap.windows[0]
        self.assertEqual((w.resource, w.kind), ("tokens", "unknown"))
        self.assertEqual(w.window_id, "tokens_limit-x-1")

    def test_huge_negative_number_degrades(self) -> None:
        snap = self._window_for(3, -(10**5000))
        w = snap.windows[0]
        self.assertEqual(w.window_id, "tokens_limit-3-x")

    def test_oversized_but_strable_integers_degrade(self) -> None:
        snap = self._window_for(12_345_678_901_234_567_890, -5)
        w = snap.windows[0]
        self.assertEqual((w.resource, w.kind), ("tokens", "unknown"))
        self.assertEqual(w.window_id, "tokens_limit-x-x")

    def test_boundary_identity_values_render(self) -> None:
        snap = self._window_for(99_999, 0)
        self.assertEqual(snap.windows[0].window_id, "tokens_limit-99999-0")

    def test_evidenced_ids_are_unchanged(self) -> None:
        for unit, number, expected in (
            (3, 5, "tokens_limit-3-5"),
            (6, 1, "tokens_limit-6-1"),
        ):
            snap = self._window_for(unit, number)
            self.assertEqual(snap.windows[0].kind, "five_hour" if unit == 3 else "weekly")
            self.assertEqual(snap.windows[0].window_id, expected)


class EnvelopeAndStructure(unittest.TestCase):
    def test_non_mapping_payloads_fail_closed(self) -> None:
        bad_payloads: list[object] = [[], "response", None, 42, True]
        for bad in bad_payloads:
            snap = parse_zai_quota_response(bad, retrieved_at=RETRIEVED_AT)
            self.assertEqual(snap.status, "schema_changed", msg=repr(bad))
            self.assertEqual(snap.windows, ())
            self.assertEqual(_codes(snap), {"schema_changed"})

    def test_missing_or_mistyped_envelope_fields_fail_closed(self) -> None:
        base: dict[str, object] = {
            "code": 200,
            "msg": "Operation successful",
            "data": {"limits": [], "level": "pro"},
            "success": True,
        }
        mutations: list[dict[str, object]] = [
            {},
            {k: v for k, v in base.items() if k != "code"},
            {k: v for k, v in base.items() if k != "msg"},
            {k: v for k, v in base.items() if k != "success"},
            {k: v for k, v in base.items() if k != "data"},
            {**base, "code": "200"},
            {**base, "code": True},
            {**base, "success": "true"},
            {**base, "msg": 5},
        ]
        for payload in mutations:
            snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
            self.assertEqual(snap.status, "schema_changed", msg=repr(payload))
            self.assertEqual(_codes(snap), {"schema_changed"})

    def test_malformed_data_fails_closed(self) -> None:
        bad_data: list[object] = [None, [], "limits", 0]
        for data in bad_data:
            payload = _limits_payload([])
            payload["data"] = data
            snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
            self.assertEqual(snap.status, "schema_changed", msg=repr(data))
            self.assertEqual(_codes(snap), {"schema_changed"})

    def test_invalid_limits_container_fails_closed(self) -> None:
        bad_containers: list[object] = [None, {}, "limits", 42]
        for limits in bad_containers:
            payload = _limits_payload([])
            payload["data"] = {"limits": limits, "level": "pro"}
            snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
            self.assertEqual(snap.status, "schema_changed", msg=repr(limits))
            self.assertEqual(snap.windows, ())

    def test_empty_limits_is_ok_with_no_windows(self) -> None:
        snap = parse_zai_quota_response(
            _limits_payload([]), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), set())

    def test_unrecognized_failure_code_maps_to_unknown(self) -> None:
        for code, success in ((500, False), (200, False), (403, False)):
            payload = _limits_payload([], code=code, success=success)
            snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
            self.assertEqual(snap.status, "unknown", msg=str(code))
            self.assertEqual(snap.windows, ())
            self.assertEqual(_codes(snap), {"telemetry_unknown"})


class PlanNormalization(unittest.TestCase):
    def test_safe_level_becomes_plan(self) -> None:
        snap = parse_zai_quota_response(
            _limits_payload([]), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.plan, "pro")

    def test_unapproved_but_safe_shaped_levels_are_omitted(self) -> None:
        # The adapter evidence allowlist is {"pro"}: syntax alone never makes
        # arbitrary provider text a normalized plan label.
        for level in ("plus", "user-123", "sk-example", "pro2"):
            payload = _limits_payload([], level=level)
            snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
            self.assertEqual(snap.status, "ok", msg=repr(level))
            self.assertIsNone(snap.plan, msg=repr(level))
            self.assertNotIn(level, _canonical_json(snap))

    def test_unsafe_or_null_level_omits_plan(self) -> None:
        for level in ("Pro", "PRO", "pro!", "super plan", "", 5, None, ["pro"]):
            payload = _limits_payload([], level=level)
            snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
            self.assertIsNone(snap.plan, msg=repr(level))
            # The unsafe value is not smuggled into any output field.
            if isinstance(level, str) and level:
                self.assertNotIn(level, _canonical_json(snap))

    def test_absent_level_key_is_structural_drift(self) -> None:
        # A physically removed level key is a disappeared required provider
        # field, not an unknown optional value: it must fail closed.
        payload = _limits_payload([_token_limit(3, 5, 35)])
        data = cast("dict[str, object]", payload["data"])
        _ = data.pop("level")
        snap = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})
        self.assertIsNone(snap.plan)


class TimeLimitNormalization(unittest.TestCase):
    def test_time_limit_is_a_distinct_non_token_window(self) -> None:
        limits: list[object] = [
            {
                "type": "TIME_LIMIT",
                "unit": 5,
                "number": 1,
                "percentage": 0,
                "nextResetTime": 1788300000000,
            },
            _token_limit(3, 5, 2),
            _token_limit(6, 1, 98),
        ]
        snap = parse_zai_quota_response(
            _limits_payload(limits), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        time_windows = _windows(snap, resource="time", kind="unknown")
        self.assertEqual(len(time_windows), 1)
        tw = time_windows[0]
        self.assertNotEqual(tw.resource, "tokens")
        self.assertIsNone(tw.duration_seconds)  # no inferred period
        self.assertEqual((tw.used_percent, tw.remaining_percent), (0, 100))
        self.assertEqual(tw.resets_at, "2026-09-01T22:00:00.000Z")
        self.assertEqual(tw.window_id, "time_limit-5-1")
        # The known token windows coexist with the time window.
        self.assertEqual(len(_windows(snap, resource="tokens", kind="five_hour")), 1)
        self.assertEqual(len(_windows(snap, resource="tokens", kind="weekly")), 1)


class PurityAndDeterminism(unittest.TestCase):
    def test_repeated_parse_is_deterministic(self) -> None:
        payload = _load("quota-200-known-windows.json")
        reparsed = cast("dict[str, object]", json.loads(json.dumps(payload)))
        a = parse_zai_quota_response(payload, retrieved_at=RETRIEVED_AT)
        b = parse_zai_quota_response(reparsed, retrieved_at=RETRIEVED_AT)
        self.assertEqual(a, b)
        self.assertEqual(_canonical_json(a), _canonical_json(b))

    def test_invalid_retrieved_at_raises_typed_error(self) -> None:
        payload = _limits_payload([])
        with self.assertRaises(CapacityError):
            _ = parse_zai_quota_response(
                payload, retrieved_at="2026-09-01T22:49:51Z"
            )

    def test_snapshot_validates_through_v2_contract(self) -> None:
        for name in (
            "quota-200-known-windows.json",
            "quota-200-unknown-window.json",
            "quota-200-weekly-missing.json",
            "quota-200-degraded-values.json",
            "quota-schema-changed.json",
            "quota-401-auth-failed.json",
        ):
            snap = parse_zai_quota_response(_load(name), retrieved_at=RETRIEVED_AT)
            reparsed = CapacitySnapshot.from_dict(snap.to_dict())
            self.assertEqual(reparsed, snap, msg=name)

    def test_serialized_output_carries_no_provider_material(self) -> None:
        for name in (
            "quota-200-known-windows.json",
            "quota-200-unknown-window.json",
            "quota-200-weekly-missing.json",
            "quota-200-degraded-values.json",
            "quota-schema-changed.json",
            "quota-401-auth-failed.json",
        ):
            snap = parse_zai_quota_response(_load(name), retrieved_at=RETRIEVED_AT)
            text = _canonical_json(snap)
            for forbidden in (
                "TOKENS_LIMIT", "TIME_LIMIT", "usageDetails", "modelCode",
                "Operation successful", "Authentication failed",
                "Authorization", "Bearer", "sk-", "api.z.ai", "auth.json",
                '"nextResetTime"', '"percentage"', '"limits"', '"level"',
                '"unit"', '"number"', '"msg"', '"success"',
            ):
                self.assertNotIn(forbidden, text, msg=f"{name}: {forbidden!r}")

    def test_parser_module_has_no_io_surface(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "scarcity_router"
            / "providers"
            / "zai.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "import os", "import sys", "socket", "ssl", "urllib", "http",
            "requests", "subprocess", "getpass", "netrc", "tempfile",
            "shutil", "os.environ", "utcnow", "now(", "open(", "time.time",
        ):
            self.assertNotIn(forbidden, source, msg=f"forbidden: {forbidden!r}")


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
