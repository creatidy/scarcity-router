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
    "ratelimits-additional-window-present.json",
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
    """A GetAccountRateLimitsResponse envelope for tests.

    ``omit`` removes optional members to build absent-state inputs.
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
    """The exact tagged success response, including the mirrored entry."""

    def test_complete_shape_with_mirrored_codex_is_healthy(self) -> None:
        snap = _parse(_load("ratelimits-full-shape-ok.json"))
        self.assertEqual(snap.status, "ok")
        self.assertEqual(snap.plan, "pro")
        # The mirror must not be treated as an additional bucket or emit
        # duplicate windows: exactly the two main windows.
        self.assertEqual(len(snap.windows), 2)
        self.assertEqual(_codes(snap), set())

    def test_diverging_mirror_fails_closed(self) -> None:
        payload = _load("ratelimits-full-shape-ok.json")
        buckets = cast(
            "dict[str, dict[str, object]]", payload["rateLimitsByLimitId"]
        )
        mirror = buckets["codex"]
        mirror["primary"] = _window(99, 300)
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})

    def test_invalid_mirror_fails_closed(self) -> None:
        payload = _load("ratelimits-full-shape-ok.json")
        buckets = cast(
            "dict[str, dict[str, object]]", payload["rateLimitsByLimitId"]
        )
        # The mirror must carry the codex identity like any full snapshot.
        buckets["codex"]["limitId"] = "other"
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_metadata_members_never_reach_output(self) -> None:
        snap = _parse(_load("ratelimits-credits-present.json"))
        text = _canonical_json(snap)
        for forbidden in (
            "hasCredits", "balance", "remainingPercent", "availableCount",
            "limitName", "individualLimit", "credits", "creditId",
        ):
            self.assertNotIn(forbidden, text)


class EnvelopeRequirements(unittest.TestCase):
    """Per the exact tagged generated schema only rateLimits is required."""

    def test_missing_rate_limits_fails_closed(self) -> None:
        payload = _result(_snapshot(dict(_BOTH_SLOTS)), omit=("rateLimits",))
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(_codes(snap), {"schema_changed"})

    def test_optional_members_may_be_absent_or_null(self) -> None:
        for omit in (
            ("rateLimitsByLimitId",),
            ("rateLimitResetCredits",),
            ("rateLimitsByLimitId", "rateLimitResetCredits"),
        ):
            with self.subTest(omit=omit):
                payload = _result(_snapshot(dict(_BOTH_SLOTS)), omit=omit)
                snap = _parse(payload)
                self.assertEqual(snap.status, "ok")
                self.assertEqual(_codes(snap), set())

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
            reset_credits={
                "availableCount": 2,
                "credits": [
                    {
                        "id": "synthetic-1",
                        "resetType": "codexRateLimits",
                        "status": "available",
                        "grantedAt": 1788000000,
                        "expiresAt": None,
                        "title": None,
                        "description": None,
                    },
                    {
                        "id": "synthetic-2",
                        "resetType": "unknown",
                        "status": "redeeming",
                        "grantedAt": 1788000000,
                        "expiresAt": 1788748064,
                        "title": "synthetic",
                        "description": "synthetic",
                    },
                ],
            },
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

    def test_malformed_credits_fail_closed(self) -> None:
        malformed: tuple[object, ...] = (
            {},  # missing required hasCredits/unlimited
            {"hasCredits": True},  # missing required unlimited/balance
            {"unlimited": False},  # missing required hasCredits/balance
            {"hasCredits": "yes", "unlimited": False},
            {"hasCredits": True, "unlimited": 1},
            {"hasCredits": True, "unlimited": False, "balance": 1000},
            {"hasCredits": True, "unlimited": False, "extra": {"n": 1}},
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
            {},  # all four members required
            {"limit": "10", "used": "1", "remainingPercent": 60},  # resetsAt
            {"limit": "10", "used": "1", "resetsAt": 1788306212},
            {"limit": 10, "used": "1", "remainingPercent": 60, "resetsAt": None},
            {"limit": "10", "used": 1, "remainingPercent": 60, "resetsAt": None},
            {"limit": "10", "used": "1", "remainingPercent": "60", "resetsAt": 1},
            {"limit": "10", "used": "1", "remainingPercent": True, "resetsAt": 1},
            {"limit": "10", "used": "1", "remainingPercent": None, "resetsAt": 1},
            {"limit": "10", "used": "1", "remainingPercent": 60, "extra": {"x": 1}},
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
            cast(object, {}),  # availableCount required
            cast(object, {"availableCount": "2"}),
            cast(object, {"availableCount": 2, "credits": {"a": 1}}),
            cast(object, {"availableCount": 2, "credits": [{}]}),
            cast(object, {"availableCount": 2, "credits": [{"id": "x"}]}),
            cast(object, {"availableCount": 2, "credits": [{"id": 5, "resetType": "unknown", "status": "available", "grantedAt": 1, "expiresAt": None, "title": None, "description": None}]}),
            cast(object, {"availableCount": 2, "credits": [{"id": "x", "resetType": "unknown", "status": "later", "grantedAt": 1, "expiresAt": None, "title": None, "description": None}]}),
            cast(object, {"availableCount": 2, "credits": [{"id": "x", "resetType": "unknown", "status": "available", "grantedAt": 1, "expiresAt": None, "title": None, "description": 5}]}),
            cast(object, {"availableCount": 2, "credits": ["row"]}),
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

    def test_out_of_width_integer_fields_fail_closed(self) -> None:
        valid_individual = {
            "limit": "100.00",
            "used": "40.00",
            "remainingPercent": 40,
            "resetsAt": 1788306212,
        }
        reset_row = {
            "id": "synthetic-1",
            "resetType": "codexRateLimits",
            "status": "available",
            "grantedAt": 1788000000,
        }
        cases: tuple[object, ...] = (
            {"individualLimit": {**valid_individual, "remainingPercent": 2**31}},
            {"individualLimit": {**valid_individual, "resetsAt": 2**63}},
            {
                "reset_credits": {
                    "availableCount": 2**63,
                    "credits": [],
                }
            },
            {
                "reset_credits": {
                    "availableCount": 1,
                    "credits": [{**reset_row, "grantedAt": 2**63}],
                }
            },
            {
                "reset_credits": {
                    "availableCount": 1,
                    "credits": [{**reset_row, "expiresAt": 2**63}],
                }
            },
        )
        for case in cases:
            with self.subTest(case=case):
                extra = cast(dict[str, object], case)
                individual = extra.get("individualLimit")
                if individual is not None:
                    individual = cast(dict[str, object], individual)
                else:
                    individual = None
                snapshot_extra: dict[str, object] | None = (
                    {"individualLimit": individual}
                    if individual is not None
                    else None
                )
                payload = _result(
                    _snapshot(dict(_BOTH_SLOTS), extra=snapshot_extra),
                    reset_credits=extra.get("reset_credits"),
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed")
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

    def test_non_nullable_individual_limit_members_are_required(self) -> None:
        payload = _result(
            _snapshot(
                dict(_BOTH_SLOTS),
                extra={
                    "individualLimit": {
                        "limit": "100.00",
                        "used": "40.00",
                        "remainingPercent": None,
                        "resetsAt": None,
                    }
                },
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())

    def test_explicit_null_credit_balance_is_valid(self) -> None:
        payload = _result(
            _snapshot(
                dict(_BOTH_SLOTS),
                extra={
                    "credits": {
                        "hasCredits": True,
                        "unlimited": False,
                        "balance": None,
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
            "spendControlReached": spend,
        }
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

    def test_additional_window_fixture_emits_bucket_window(self) -> None:
        snap = _parse(_load("ratelimits-additional-window-present.json"))
        self.assertEqual(snap.status, "unknown")
        self.assertIn("telemetry_unknown", _codes(snap))
        # Both main windows keep their validated pairs...
        by_id = {w.window_id: w for w in snap.windows}
        self.assertEqual(by_id["primary"].used_percent, 6)
        self.assertEqual(by_id["secondary"].used_percent, 52)
        # ...and the additional bucket's window is emitted with a safe,
        # distinct identity (no merge with the main five-hour window).
        self.assertEqual(len(snap.windows), 3)
        reserve = by_id["gpt-reserve:primary"]
        self.assertEqual(reserve.used_percent, 30)
        self.assertEqual(reserve.kind, "five_hour")
        self.assertEqual(reserve.duration_seconds, 18_000)
        self.assertNotIn("percentage_unknown", _codes(snap))

    def test_bucket_windows_ordered_deterministically(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={
                "zeta-limit": self._bucket(limit_id="zeta-limit"),
                "alpha-limit": self._bucket(limit_id="alpha-limit"),
            },
        )
        snap = _parse(payload)
        self.assertEqual(
            [w.window_id for w in snap.windows],
            [
                "primary",
                "secondary",
                "alpha-limit:primary",
                "zeta-limit:primary",
            ],
        )

    def test_every_present_bucket_slot_requires_a_safe_identity(self) -> None:
        limit_id = "a" * 55
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={
                limit_id: self._bucket(
                    limit_id=limit_id,
                    secondary=_window(10, 10080, 1788748064),
                )
            },
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())

    def test_unsafe_bucket_key_fails_closed(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={"GPT Reserve!": self._bucket(limit_id="GPT Reserve!")},
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertNotIn("GPT Reserve!", _canonical_json(snap))

    def test_bucket_identity_mismatch_fails_closed(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={"gpt-reserve": self._bucket(limit_id="other-limit")},
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())

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
        bucket["primary"] = {"usedPercent": 5, "windowDurationMins": "300"}
        payload = _result(_snapshot(dict(_BOTH_SLOTS)), buckets={"gpt-reserve": bucket})
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_bucket_structured_drift_fails_closed(self) -> None:
        bucket = self._bucket()
        bucket["unknownObject"] = {"x": 1}
        payload = _result(_snapshot(dict(_BOTH_SLOTS)), buckets={"gpt-reserve": bucket})
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")

    def test_bucket_exhaustion_withholds_all_pairs(self) -> None:
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

    def test_bucket_spend_control_blocker_withholds_all_pairs(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={"gpt-reserve": self._bucket(spend=True)},
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
        self.assertIn("percentage_unknown", _codes(snap))

    def test_bucket_exhausted_individual_limit_withholds_all_pairs(self) -> None:
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)),
            buckets={
                "gpt-reserve": self._bucket(
                    individual={
                        "limit": "10.00",
                        "used": "10.00",
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

    def test_exhausted_bucket_fixture(self) -> None:
        snap = _parse(_load("ratelimits-additional-bucket-exhausted.json"))
        self.assertEqual(snap.status, "unknown")
        self.assertEqual(snap.plan, "pro")
        self.assertEqual(len(snap.windows), 3)  # bucket window emitted too
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
        self.assertIn("telemetry_unknown", _codes(snap))
        # The bucket's window identity is a safe composed id; unsafe raw
        # keys never leak (checked in the no-leak sweep).

    def test_null_duration_bucket_window_accepted(self) -> None:
        bucket = self._bucket()
        bucket["primary"] = {
            "usedPercent": 5,
            "windowDurationMins": None,
            "resetsAt": 1788306212,
        }
        payload = _result(
            _snapshot(dict(_BOTH_SLOTS)), buckets={"gpt-reserve": bucket}
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")  # additional metering present
        reserve = snap.windows[2]
        self.assertEqual(reserve.kind, "unknown")
        self.assertIsNone(reserve.duration_seconds)
        self.assertEqual(reserve.window_id, "gpt-reserve:primary")


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
            _snapshot(
                dict(_BOTH_SLOTS), extra={"rateLimitReachedType": "rate_limit_reached"}
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        for window in snap.windows:
            self.assertIsNone(window.used_percent)
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

    def test_non_snake_or_unknown_reached_values_are_drift(self) -> None:
        for bad in (
            "rateLimitReached",  # camelCase is not the evidenced wire form
            "workspaceMemberCreditsDepleted",
            "brand-new-type",
            "",
            5,
            True,
            {"x": 1},
            ["x"],
        ):
            with self.subTest(bad=bad):
                payload = _result(
                    _snapshot(
                        dict(_BOTH_SLOTS), extra={"rateLimitReachedType": bad}
                    )
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))
                self.assertEqual(snap.windows, ())

    def test_known_zero_usage_is_not_missing(self) -> None:
        snap = _parse(_load("ratelimits-zero-usage.json"))
        self.assertEqual(snap.status, "ok")
        for window in snap.windows:
            self.assertEqual(
                (window.used_percent, window.remaining_percent), (0, 100)
            )
        self.assertNotIn("percentage_unknown", _codes(snap))


class DegradedFixture(unittest.TestCase):
    def test_valid_percentage_survives_other_degraded_facts(self) -> None:
        snap = _parse(_load("ratelimits-degraded.json"))
        five_hour = _windows(snap, resource="tokens", kind="five_hour")
        self.assertEqual(len(five_hour), 1)
        self.assertEqual(five_hour[0].used_percent, 6)
        self.assertEqual(five_hour[0].remaining_percent, 94)

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


# ══════════════════════════ focused parser behavior ══════════════════════════


class PercentageNormalization(unittest.TestCase):
    def test_malformed_percentages_omit_pair(self) -> None:
        for bad in (-1, 101):
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
                self.assertIn("percentage_unknown", _codes(snap))

    def test_non_i32_percentage_is_schema_drift(self) -> None:
        for bad in ("6", 6.5, True, None, [6], {"v": 6}, 2**31, -(2**31) - 1):
            with self.subTest(bad=bad):
                primary = {
                    "usedPercent": bad,
                    "windowDurationMins": 300,
                    "resetsAt": 1788306212,
                }
                payload = _result(
                    _snapshot(
                        {
                            "primary": primary,
                            "secondary": _window(10, 10080, 1788748064),
                        }
                    )
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))
                self.assertEqual(snap.windows, ())

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
        bad_values: tuple[object, ...] = (
            None,
            0,
            -5,
            1788306212.0,
            True,
            "1788306212",
            1788306212000,  # epoch milliseconds must not be misread
            999_999_999,
            10_000_000_000,
        )
        for bad in bad_values:
            with self.subTest(bad=bad):
                snap = self._snap_with(bad)
                self.assertIsNone(snap.windows[0].resets_at, msg=repr(bad))
                self.assertIn("reset_unknown", _codes(snap))

    def test_out_of_width_reset_value_is_schema_drift(self) -> None:
        bad_values: tuple[object, ...] = (2**63, -(2**63) - 1, 10**30)
        for bad in bad_values:
            with self.subTest(bad=bad):
                snap = self._snap_with(bad)
                self.assertEqual(snap.status, "schema_changed")
                self.assertEqual(snap.windows, ())

    def test_epoch_milliseconds_never_become_1970(self) -> None:
        snap = self._snap_with(1788306212000)
        self.assertNotIn("1970", _canonical_json(snap))


class WindowStructureDrift(unittest.TestCase):
    def test_used_percent_is_required_and_i32(self) -> None:
        for primary in (
            {"windowDurationMins": 300, "resetsAt": 1788306212},
            {"usedPercent": None, "windowDurationMins": 300, "resetsAt": 1788306212},
            {"usedPercent": 2**31, "windowDurationMins": 300, "resetsAt": 1788306212},
        ):
            with self.subTest(primary=primary):
                payload = _result(
                    _snapshot(
                        {
                            "primary": primary,
                            "secondary": _window(52, 10080, 1788748064),
                        }
                    )
                )
                snap = _parse(payload)
                self.assertEqual(snap.status, "schema_changed")
                self.assertEqual(snap.windows, ())

    def test_absent_duration_is_a_valid_unknown_window(self) -> None:
        payload = _result(
            _snapshot(
                {
                    "primary": {"usedPercent": 6, "resetsAt": 1788306212},
                    "secondary": _window(52, 10080, 1788748064),
                }
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")  # five-hour constraint unknown
        unknown = _windows(snap, resource="tokens", kind="unknown")
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].used_percent, 6)
        self.assertIsNone(unknown[0].duration_seconds)
        self.assertIn("window_semantics_unknown", _codes(snap))

    def test_null_duration_is_a_valid_unknown_window(self) -> None:
        payload = _result(
            _snapshot(
                {
                    "primary": {
                        "usedPercent": 6,
                        "windowDurationMins": None,
                        "resetsAt": 1788306212,
                    },
                    "secondary": _window(52, 10080, 1788748064),
                }
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "unknown")
        unknown = _windows(snap, resource="tokens", kind="unknown")
        self.assertEqual(len(unknown), 1)
        self.assertIsNone(unknown[0].duration_seconds)
        self.assertEqual(unknown[0].resets_at, RESET_1788306212)

    def test_object_without_any_window_member_is_drift(self) -> None:
        payload = _result(
            _snapshot(
                {
                    "primary": {"foo": "bar"},
                    "secondary": _window(52, 10080, 1788748064),
                }
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())

    def test_non_positive_or_non_integer_duration_fails_closed(self) -> None:
        for bad in (0, -300, 300.0, True, "300"):
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
                {
                    "primary": {"windowDurationMins": {"nested": True}},
                    "secondary": _window(52, 10080, 1788748064),
                }
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

    def test_out_of_width_duration_is_schema_drift(self) -> None:
        payload = _result(
            _snapshot(
                {
                    "primary": _window(10, 2**63),
                    "secondary": _window(20, 10080, 1788748064),
                }
            )
        )
        snap = _parse(payload)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())


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
            # Hybrids carrying a `method` key of ANY type together with
            # response fields are drift, never requests or responses.
            {"method": "x", "result": {}},
            {"method": "x", "error": {"code": -1, "message": "y"}},
            {"id": 2, "method": "x", "result": {"rateLimits": {}}},
            {"method": None, "result": {}},
            {"method": 42, "id": 2, "result": {}},
            {"method": True, "id": 2, "error": {}},
            # Notifications whose `id` key is present but malformed are
            # drift, never silently ignorable notifications.
            {"method": "x", "id": "7"},
            {"method": "x", "id": True},
            {"method": "x", "id": None},
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
                    "hasCredits", "balance", "availableCount", "GPT Reserve",
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
