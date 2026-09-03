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


def _load(name: str) -> dict[str, object]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return cast("dict[str, object]", json.load(handle))


def _rate_limits(
    slots: dict[str, object],
    *,
    plan_type: object = "plus",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    rate_limits: dict[str, object] = {"limitId": "codex", **slots}
    if plan_type is not None:
        rate_limits["planType"] = plan_type
    if extra is not None:
        rate_limits.update(extra)
    return {"rateLimits": rate_limits}


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
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-ok-plus.json"), retrieved_at=RETRIEVED_AT
        )
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
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-slots-swapped.json"), retrieved_at=RETRIEVED_AT
        )
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


class UnknownDurationFixture(unittest.TestCase):
    def test_unknown_window_preserved_without_guessing(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-unknown-duration.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        unknown = _windows(snap, resource="tokens", kind="unknown")
        self.assertEqual(len(unknown), 1)
        uw = unknown[0]
        self.assertEqual(uw.used_percent, 41)
        self.assertEqual(uw.remaining_percent, 59)
        self.assertEqual(uw.duration_seconds, 3_600)  # validated duration kept
        self.assertEqual(uw.resets_at, RESET_1788000000)
        self.assertEqual(uw.window_id, "primary")
        scoped = {
            d.window_id
            for d in snap.diagnostics
            if d.code == "window_semantics_unknown"
        }
        self.assertEqual(scoped, {"primary"})

    def test_known_sibling_survives(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-unknown-duration.json"), retrieved_at=RETRIEVED_AT
        )
        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0].duration_seconds, 604_800)
        self.assertEqual(weekly[0].used_percent, 72)


class ExhaustedAndZeroFixtures(unittest.TestCase):
    def test_known_exhaustion_is_not_a_failure(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-exhausted-reached.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        self.assertEqual(len(snap.windows), 2)
        for window in snap.windows:
            self.assertEqual(
                (window.used_percent, window.remaining_percent), (100, 0)
            )

    def test_known_zero_usage_is_not_missing(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-zero-usage.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        for window in snap.windows:
            self.assertEqual(
                (window.used_percent, window.remaining_percent), (0, 100)
            )
        self.assertNotIn("percentage_unknown", _codes(snap))


class DegradedFixture(unittest.TestCase):
    def test_string_percentage_omits_pair(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-degraded.json"), retrieved_at=RETRIEVED_AT
        )
        five_hour = _windows(snap, resource="tokens", kind="five_hour")
        self.assertEqual(len(five_hour), 1)
        self.assertIsNone(five_hour[0].used_percent)
        self.assertIsNone(five_hour[0].remaining_percent)
        self.assertIn("percentage_unknown", _codes(snap))

    def test_missing_reset_omits_resets_at(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-degraded.json"), retrieved_at=RETRIEVED_AT
        )
        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(len(weekly), 1)
        self.assertIsNone(weekly[0].resets_at)
        self.assertIn("reset_unknown", _codes(snap))

    def test_unevidenced_plan_label_is_omitted(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-degraded.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "ok")
        self.assertIsNone(snap.plan)
        self.assertNotIn('"plan"', _canonical_json(snap))

    def test_degradation_diagnostics_are_window_scoped(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-degraded.json"), retrieved_at=RETRIEVED_AT
        )
        scoped = {(d.code, d.window_id) for d in snap.diagnostics}
        self.assertIn(("percentage_unknown", "primary"), scoped)
        self.assertIn(("reset_unknown", "secondary"), scoped)

    def test_degraded_snapshot_validates_through_v1(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-degraded.json"), retrieved_at=RETRIEVED_AT
        )
        reparsed = CapacitySnapshot.from_dict(snap.to_dict())
        self.assertEqual(reparsed, snap)


class SchemaChangedFixture(unittest.TestCase):
    def test_fails_closed_without_partial_decoding(self) -> None:
        snap = parse_codex_rate_limits_result(
            _load("ratelimits-schema-changed.json"), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})
        self.assertIsNone(snap.plan)
        text = _canonical_json(snap)
        for forbidden in (
            "consumedPercent", "resetTimeUtc", "planTier", "five_hour",
        ):
            self.assertNotIn(forbidden, text)


# ══════════════════════════ focused parser behavior ══════════════════════════


class PercentageNormalization(unittest.TestCase):
    def test_malformed_percentages_omit_pair(self) -> None:
        for bad in ("6", 6.5, True, -1, 101, None, [6], {"v": 6}):
            with self.subTest(bad=bad):
                payload = _rate_limits(
                    {"primary": _window(bad, 300), "secondary": _window(10, 10080)}
                )
                snap = parse_codex_rate_limits_result(
                    payload, retrieved_at=RETRIEVED_AT
                )
                self.assertEqual(snap.status, "ok", msg=repr(bad))
                primary = _windows(snap, resource="tokens", kind="five_hour")
                self.assertEqual(len(primary), 1)
                self.assertIsNone(primary[0].used_percent, msg=repr(bad))
                self.assertIsNone(primary[0].remaining_percent, msg=repr(bad))
                self.assertIn("percentage_unknown", _codes(snap))

    def test_used_orientation_boundary_values(self) -> None:
        for used, remaining in ((0, 100), (50, 50), (100, 0)):
            with self.subTest(used=used):
                payload = _rate_limits({"primary": _window(used, 300)})
                snap = parse_codex_rate_limits_result(
                    payload, retrieved_at=RETRIEVED_AT
                )
                window = snap.windows[0]
                self.assertEqual(
                    (window.used_percent, window.remaining_percent),
                    (used, remaining),
                )


class ResetNormalization(unittest.TestCase):
    def test_valid_epoch_seconds_preserved(self) -> None:
        payload = _rate_limits({"primary": _window(10, 300, 1788306212)})
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
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
                payload = _rate_limits({"primary": _window(10, 300, bad)})
                snap = parse_codex_rate_limits_result(
                    payload, retrieved_at=RETRIEVED_AT
                )
                self.assertIsNone(snap.windows[0].resets_at, msg=repr(bad))
                self.assertIn("reset_unknown", _codes(snap))

    def test_epoch_milliseconds_never_become_1970(self) -> None:
        payload = _rate_limits({"primary": _window(10, 300, 1788306212000)})
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertNotIn("1970", _canonical_json(snap))

    def test_missing_reset_key_omits_resets_at(self) -> None:
        payload = _rate_limits({"primary": _window(10, 300, None)})
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertIsNone(snap.windows[0].resets_at)


class WindowStructureDrift(unittest.TestCase):
    def test_object_without_duration_discriminator_fails_closed(self) -> None:
        payload = _rate_limits(
            {"primary": {"usedPercent": 6, "resetsAt": 1788306212}}
        )
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})

    def test_non_positive_or_non_integer_duration_fails_closed(self) -> None:
        for bad in (0, -300, 300.0, True, "300", None):
            with self.subTest(bad=bad):
                payload = _rate_limits(
                    {"primary": _window(6, bad), "secondary": _window(52, 10080)}
                )
                snap = parse_codex_rate_limits_result(
                    payload, retrieved_at=RETRIEVED_AT
                )
                self.assertEqual(snap.status, "schema_changed", msg=repr(bad))
                self.assertEqual(snap.windows, ())

    def test_healthy_sibling_does_not_survive_drift(self) -> None:
        payload = _rate_limits(
            {"primary": {"foo": "bar"}, "secondary": _window(52, 10080)}
        )
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertIsNone(snap.plan)

    def test_additive_scalar_fields_are_tolerated(self) -> None:
        payload = _rate_limits(
            {"primary": _window(6, 300)},
            extra={"newScalarField": 7, "another": "text"},
        )
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "ok")
        self.assertEqual(len(snap.windows), 1)
        self.assertEqual(_codes(snap), set())

    def test_window_with_extra_object_fields_is_tolerated(self) -> None:
        entry: dict[str, object] = {
            **_window(6, 300),
            "experimental": {"nested": True},
        }
        payload = _rate_limits({"primary": entry})
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "ok")
        self.assertEqual(len(snap.windows), 1)

    def test_windows_under_arbitrary_safe_keys_are_position_independent(self) -> None:
        payload = _rate_limits(
            {"a": _window(10, 300), "zz-9": _window(20, 10080)}
        )
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "ok")
        five_hour = _windows(snap, resource="tokens", kind="five_hour")
        weekly = _windows(snap, resource="tokens", kind="weekly")
        self.assertEqual(five_hour[0].window_id, "a")
        self.assertEqual(weekly[0].window_id, "zz-9")

    def test_unsafe_slot_key_omits_window_id(self) -> None:
        payload = _rate_limits({"Primary Window!": _window(10, 300)})
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "ok")
        window = snap.windows[0]
        self.assertEqual(window.kind, "five_hour")
        self.assertIsNone(window.window_id)
        self.assertNotIn("Primary Window!", _canonical_json(snap))

    def test_duplicate_durations_are_not_merged(self) -> None:
        payload = _rate_limits(
            {"primary": _window(10, 300), "secondary": _window(90, 300)}
        )
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "ok")
        five_hour = _windows(snap, resource="tokens", kind="five_hour")
        self.assertEqual(len(five_hour), 2)

    def test_huge_duration_degrades_to_unknown_without_raising(self) -> None:
        payload = _rate_limits({"primary": _window(10, 10**60)})
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "ok")
        window = snap.windows[0]
        self.assertEqual((window.resource, window.kind), ("tokens", "unknown"))
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
                payload: dict[str, object] = {"rateLimits": rate_limits}
                snap = parse_codex_rate_limits_result(
                    payload, retrieved_at=RETRIEVED_AT
                )
                self.assertEqual(snap.status, "schema_changed", msg=repr(rate_limits))
                self.assertEqual(snap.windows, ())

    def test_absent_rate_limits_key_fails_closed(self) -> None:
        snap = parse_codex_rate_limits_result({}, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "schema_changed")

    def test_windowless_rate_limits_fails_closed(self) -> None:
        payload = _rate_limits({})
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "schema_changed")
        self.assertEqual(snap.windows, ())
        self.assertEqual(_codes(snap), {"schema_changed"})


class PlanNormalization(unittest.TestCase):
    def test_safe_evidenced_plan_preserved(self) -> None:
        payload = _rate_limits({"primary": _window(6, 300)}, plan_type="plus")
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.plan, "plus")

    def test_unevidenced_or_unsafe_labels_are_omitted(self) -> None:
        for level in ("pro", "free", "Plus", "plus!", "", 5, None, ["plus"]):
            with self.subTest(level=level):
                payload = _rate_limits(
                    {"primary": _window(6, 300)}, plan_type=level
                )
                snap = parse_codex_rate_limits_result(
                    payload, retrieved_at=RETRIEVED_AT
                )
                self.assertEqual(snap.status, "ok", msg=repr(level))
                self.assertIsNone(snap.plan, msg=repr(level))
                self.assertNotIn('"plan"', _canonical_json(snap))

    def test_absent_plan_type_is_tolerated(self) -> None:
        payload = _rate_limits({"primary": _window(6, 300)}, plan_type=None)
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "ok")
        self.assertIsNone(snap.plan)

    def test_reached_flag_is_dropped_without_guessing(self) -> None:
        for reached in ("primary", "secondary", 5):
            with self.subTest(reached=reached):
                payload = _rate_limits(
                    {"a": _window(6, 300)},
                    extra={"rateLimitReachedType": reached},
                )
                snap = parse_codex_rate_limits_result(
                    payload, retrieved_at=RETRIEVED_AT
                )
                self.assertEqual(snap.status, "ok", msg=repr(reached))
                self.assertEqual(len(snap.windows), 1)
        # An unrecognized reached value must not leak into any output.
        payload = _rate_limits(
            {"a": _window(6, 300)},
            extra={"rateLimitReachedType": "weird-new-type"},
        )
        snap = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        self.assertEqual(snap.status, "ok")
        self.assertNotIn("weird-new-type", _canonical_json(snap))


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
        a = parse_codex_rate_limits_result(payload, retrieved_at=RETRIEVED_AT)
        b = parse_codex_rate_limits_result(reparsed, retrieved_at=RETRIEVED_AT)
        self.assertEqual(a, b)
        self.assertEqual(_canonical_json(a), _canonical_json(b))

    def test_invalid_retrieved_at_raises_typed_error(self) -> None:
        payload = _rate_limits({"primary": _window(6, 300)})
        with self.assertRaises(CapacityError):
            _ = parse_codex_rate_limits_result(
                payload, retrieved_at="2026-09-03T20:00:00Z"
            )

    def test_all_fixtures_round_trip_through_v1(self) -> None:
        for name in (
            "ratelimits-ok-plus.json",
            "ratelimits-slots-swapped.json",
            "ratelimits-unknown-duration.json",
            "ratelimits-exhausted-reached.json",
            "ratelimits-zero-usage.json",
            "ratelimits-degraded.json",
            "ratelimits-schema-changed.json",
        ):
            with self.subTest(name=name):
                snap = parse_codex_rate_limits_result(
                    _load(name), retrieved_at=RETRIEVED_AT
                )
                reparsed = CapacitySnapshot.from_dict(snap.to_dict())
                self.assertEqual(reparsed, snap)

    def test_serialized_output_carries_no_provider_material(self) -> None:
        for name in (
            "ratelimits-ok-plus.json",
            "ratelimits-slots-swapped.json",
            "ratelimits-unknown-duration.json",
            "ratelimits-exhausted-reached.json",
            "ratelimits-zero-usage.json",
            "ratelimits-degraded.json",
            "ratelimits-schema-changed.json",
        ):
            with self.subTest(name=name):
                snap = parse_codex_rate_limits_result(
                    _load(name), retrieved_at=RETRIEVED_AT
                )
                text = _canonical_json(snap)
                for forbidden in (
                    "windowDurationMins", "usedPercent", "resetsAt",
                    "planType", "rateLimitReachedType", "limitId",
                    "rateLimits", "codexHome", "userAgent", "auth.json",
                    ".codex", "codex-cli", "Authorization", "Bearer",
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
