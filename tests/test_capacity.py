"""Contract tests for the pure v1 normalized capacity model.

These tests construct normalized v1 objects directly (the contract in
``docs/capacity-model.md``) and assert the invariants the implementation must
enforce. They do NOT parse provider fixtures; the Z.ai fixtures under
``tests/fixtures/zai-coding-plan`` are evidence inputs for the later adapter task.

Run with either:

    python -m unittest discover -s tests -v
    python -m unittest tests.test_capacity -v

All tests are deterministic and self-contained; no network, subprocess, or
credential access occurs.
"""

from __future__ import annotations

import json
import unittest
from typing import cast

from scarcity_router import (
    CapacityDiagnostic,
    CapacityError,
    CapacitySnapshot,
    CapacityValidationError,
    CapacityWindow,
    LocalRuntime,
)


def _clone(payload: dict[str, object]) -> dict[str, object]:
    """Deep-copy a payload (JSON round-trip keeps the exact JSON shape)."""
    return cast("dict[str, object]", json.loads(json.dumps(payload)))


def _windows(payload: dict[str, object]) -> list[dict[str, object]]:
    """Typed view of the serialized ``windows`` array for mutation tests."""
    return cast("list[dict[str, object]]", payload["windows"])


def _diagnostics(payload: dict[str, object]) -> list[dict[str, object]]:
    """Typed view of the serialized ``diagnostics`` array for shape tests."""
    return cast("list[dict[str, object]]", payload["diagnostics"])


# ── payload builders ──────────────────────────────────────────────────────────

def _window(
    resource: str = "tokens",
    kind: str = "five_hour",
    duration_seconds: int | None = 18_000,
    used: int | None = 6,
    remaining: int | None = 94,
    resets_at: str | None = "2026-09-02T04:00:00.000Z",
    window_id: str | None = None,
) -> dict[str, object]:
    d: dict[str, object] = {"resource": resource, "kind": kind}
    if duration_seconds is not None:
        d["duration_seconds"] = duration_seconds
    if used is not None:
        d["used_percent"] = used
    if remaining is not None:
        d["remaining_percent"] = remaining
    if resets_at is not None:
        d["resets_at"] = resets_at
    if window_id is not None:
        d["provider_metadata"] = {"window_id": window_id}
    return d


def openai_healthy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider": "openai",
        "source": "codex_app_server",
        "plan": "plus",
        "retrieved_at": "2026-09-01T22:49:51.000Z",
        "status": "ok",
        "windows": [
            _window(kind="five_hour", duration_seconds=18_000, used=6, remaining=94),
            _window(kind="weekly", duration_seconds=604_800, used=52, remaining=48),
        ],
        "diagnostics": [],
    }


def zai_healthy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider": "zai",
        "source": "zai_usage_endpoint",
        "plan": "pro",
        "retrieved_at": "2026-09-01T22:49:51.000Z",
        "status": "ok",
        "windows": [
            _window(kind="five_hour", duration_seconds=18_000, used=2, remaining=98),
            _window(kind="weekly", duration_seconds=604_800, used=98, remaining=2),
            _window(
                resource="tokens",
                kind="unknown",
                duration_seconds=None,
                used=None,
                remaining=None,
                resets_at=None,
                window_id="unknown-window-a",
            ),
            _window(resource="time", kind="unknown", used=None, remaining=None, resets_at=None),
        ],
        "diagnostics": [{"code": "window_semantics_unknown"}],
    }


def ollama_healthy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider": "ollama",
        "source": "ollama_local",
        "retrieved_at": "2026-09-01T22:49:51.000Z",
        "status": "ok",
        "windows": [],
        "local_runtime": {
            "reachable": True,
            "model_presence": "present",
            "model_name": "qwen3.8:27b",
            "configured_context_tokens": 163840,
        },
        "diagnostics": [],
    }


# ══════════════════════════════════ VALID ═════════════════════════════════════


class ValidSnapshots(unittest.TestCase):
    def test_1_openai_healthy(self) -> None:
        snap = CapacitySnapshot.from_dict(_clone(openai_healthy()))
        self.assertEqual(snap.status, "ok")
        self.assertEqual(len(snap.windows), 2)
        self.assertEqual(snap.windows[0].kind, "five_hour")
        self.assertEqual(snap.windows[0].duration_seconds, 18_000)

    def test_2_zai_healthy_with_unknown_window(self) -> None:
        snap = CapacitySnapshot.from_dict(_clone(zai_healthy()))
        self.assertEqual(len(snap.windows), 4)
        unknown_token = [w for w in snap.windows if w.resource == "tokens" and w.kind == "unknown"]
        self.assertEqual(len(unknown_token), 1)
        self.assertEqual(unknown_token[0].window_id, "unknown-window-a")
        time_window = [w for w in snap.windows if w.resource == "time"]
        self.assertEqual(len(time_window), 1)
        self.assertEqual(time_window[0].window_id, None)

    def test_3_ollama_healthy_no_windows(self) -> None:
        snap = CapacitySnapshot.from_dict(_clone(ollama_healthy()))
        self.assertEqual(snap.windows, ())
        assert snap.local_runtime is not None
        self.assertTrue(snap.local_runtime.reachable)
        self.assertEqual(snap.local_runtime.model_presence, "present")
        self.assertEqual(snap.local_runtime.configured_context_tokens, 163840)

    def test_4_known_used_zero(self) -> None:
        payload = openai_healthy()
        w = _windows(payload)[0]
        w["used_percent"] = 0
        w["remaining_percent"] = 100
        snap = CapacitySnapshot.from_dict(_clone(payload))
        self.assertEqual(snap.windows[0].used_percent, 0)
        self.assertEqual(snap.windows[0].remaining_percent, 100)

    def test_5_known_remaining_zero(self) -> None:
        payload = openai_healthy()
        w = _windows(payload)[0]
        w["used_percent"] = 100
        w["remaining_percent"] = 0
        snap = CapacitySnapshot.from_dict(_clone(payload))
        self.assertEqual(snap.windows[0].used_percent, 100)
        self.assertEqual(snap.windows[0].remaining_percent, 0)

    def test_6_percentage_pair_omitted(self) -> None:
        payload = openai_healthy()
        w = _windows(payload)[0]
        _ = w.pop("used_percent")
        _ = w.pop("remaining_percent")
        snap = CapacitySnapshot.from_dict(_clone(payload))
        self.assertIsNone(snap.windows[0].used_percent)
        self.assertIsNone(snap.windows[0].remaining_percent)

    def test_7_canonical_timestamps(self) -> None:
        payload = openai_healthy()
        payload["retrieved_at"] = "2026-09-01T22:49:51.250Z"
        _windows(payload)[0]["resets_at"] = "2026-09-02T04:00:00.999Z"
        snap = CapacitySnapshot.from_dict(_clone(payload))
        self.assertEqual(snap.retrieved_at, "2026-09-01T22:49:51.250Z")
        self.assertEqual(snap.windows[0].resets_at, "2026-09-02T04:00:00.999Z")

    def test_8_deterministic_round_trip(self) -> None:
        payload = zai_healthy()
        snap = CapacitySnapshot.from_dict(_clone(payload))
        serialized = snap.to_dict()
        # Deterministic: identical input yields identical bytes.
        self.assertEqual(json.dumps(serialized, sort_keys=True),
                         json.dumps(snap.to_dict(), sort_keys=True))
        # Round-trips through validation.
        reparsed = CapacitySnapshot.from_dict(_clone(serialized))
        self.assertEqual(reparsed, snap)
        self.assertEqual(reparsed.to_dict(), serialized)


class DistinctPercentageStates(unittest.TestCase):
    """The three percentage states are distinct and all first-class."""

    def test_known_zero_used(self) -> None:
        snap = CapacitySnapshot.from_dict(_clone(openai_healthy()))
        w = CapacityWindow(resource="tokens", kind="five_hour", duration_seconds=18_000,
                           used_percent=0, remaining_percent=100)
        snap2 = CapacitySnapshot(
            schema_version=snap.schema_version,
            provider=snap.provider,
            source=snap.source,
            retrieved_at=snap.retrieved_at,
            status="ok",
            windows=(w,),
            diagnostics=snap.diagnostics,
        )
        self.assertEqual((snap2.windows[0].used_percent, snap2.windows[0].remaining_percent), (0, 100))
        d = snap2.windows[0].to_dict()
        self.assertEqual(d["used_percent"], 0)
        self.assertEqual(d["remaining_percent"], 100)

    def test_known_exhausted(self) -> None:
        w = CapacityWindow(resource="tokens", kind="weekly", duration_seconds=604_800,
                           used_percent=100, remaining_percent=0)
        d = w.to_dict()
        self.assertEqual(d["used_percent"], 100)
        self.assertEqual(d["remaining_percent"], 0)

    def test_omitted_pair(self) -> None:
        w = CapacityWindow(resource="tokens", kind="unknown")
        d = w.to_dict()
        self.assertNotIn("used_percent", d)
        self.assertNotIn("remaining_percent", d)

    def test_three_states_are_distinguishable_after_round_trip(self) -> None:
        zero_used = CapacityWindow.from_dict({"resource": "tokens", "kind": "five_hour",
                                              "duration_seconds": 18_000,
                                              "used_percent": 0, "remaining_percent": 100})
        exhausted = CapacityWindow.from_dict({"resource": "tokens", "kind": "weekly",
                                              "duration_seconds": 604_800,
                                              "used_percent": 100, "remaining_percent": 0})
        omitted = CapacityWindow.from_dict({"resource": "tokens", "kind": "unknown"})

        a = zero_used.to_dict()
        b = exhausted.to_dict()
        c = omitted.to_dict()

        self.assertEqual(a["used_percent"], 0)
        self.assertEqual(a["remaining_percent"], 100)
        self.assertEqual(b["used_percent"], 100)
        self.assertEqual(b["remaining_percent"], 0)
        self.assertNotIn("used_percent", c)
        self.assertNotIn("remaining_percent", c)

        # Serialized shapes differ from one another.
        self.assertNotEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertNotEqual(json.dumps(a, sort_keys=True), json.dumps(c, sort_keys=True))
        self.assertNotEqual(json.dumps(b, sort_keys=True), json.dumps(c, sort_keys=True))


class FailSafeStates(unittest.TestCase):
    """Failure statuses must carry an empty windows array and their code."""

    def test_unavailable(self) -> None:
        p = _clone(openai_healthy())
        p["provider"] = "openai"
        p["status"] = "unavailable"
        p["windows"] = []
        p["diagnostics"] = [{"code": "source_unavailable"}]
        snap = CapacitySnapshot.from_dict(p)
        self.assertEqual(snap.windows, ())
        self.assertEqual(snap.diagnostics[0].code, "source_unavailable")

    def test_auth_required(self) -> None:
        p = _clone(openai_healthy())
        p["status"] = "auth_required"
        p["windows"] = []
        p["diagnostics"] = [{"code": "auth_required"}]
        snap = CapacitySnapshot.from_dict(p)
        self.assertEqual(snap.windows, ())

    def test_unsupported(self) -> None:
        p = _clone(openai_healthy())
        p["status"] = "unsupported"
        p["windows"] = []
        p["diagnostics"] = [{"code": "unsupported_source"}]
        snap = CapacitySnapshot.from_dict(p)
        self.assertEqual(snap.windows, ())

    def test_schema_changed(self) -> None:
        p = _clone(openai_healthy())
        p["status"] = "schema_changed"
        p["windows"] = []
        p["diagnostics"] = [{"code": "schema_changed"}]
        snap = CapacitySnapshot.from_dict(p)
        self.assertEqual(snap.windows, ())

    def test_unknown_status_allows_windows(self) -> None:
        p = openai_healthy()
        p["status"] = "unknown"
        p["diagnostics"] = [{"code": "telemetry_unknown"}]
        snap = CapacitySnapshot.from_dict(_clone(p))
        self.assertEqual(len(snap.windows), 2)


class LocalRuntimeValidation(unittest.TestCase):
    def test_reachable_present(self) -> None:
        lr =                 _ = LocalRuntime.from_dict({
            "reachable": True,
            "model_presence": "present",
            "model_name": "qwen3.8:27b",
        })
        self.assertTrue(lr.reachable)
        self.assertEqual(lr.model_presence, "present")

    def test_unreachable_requires_unknown(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ =                 _ = LocalRuntime.from_dict({
                "reachable": False,
                "model_presence": "present",
                "model_name": "qwen3.8:27b",
            })

    def test_present_requires_model_name(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ =                 _ = LocalRuntime.from_dict({
                "reachable": True,
                "model_presence": "present",
            })

    def test_context_fields_are_optional_ints(self) -> None:
        lr =                 _ = LocalRuntime.from_dict({
            "reachable": True,
            "model_presence": "present",
            "model_name": "qwen3.8:27b",
            "configured_context_tokens": 163840,
        })
        self.assertEqual(lr.configured_context_tokens, 163840)
        self.assertIsNone(lr.effective_context_tokens)


class SerializationShape(unittest.TestCase):
    def test_no_nulls_in_serialized(self) -> None:
        snap = CapacitySnapshot.from_dict(_clone(zai_healthy()))
        s = snap.to_dict()
        text = json.dumps(s)
        self.assertNotIn("None", text)

    def test_zero_percentages_survive(self) -> None:
        w = CapacityWindow.from_dict({
            "resource": "tokens",
            "kind": "five_hour",
            "duration_seconds": 18_000,
            "used_percent": 0,
            "remaining_percent": 100,
        })
        d = w.to_dict()
        self.assertEqual(d["used_percent"], 0)
        # Distinguish int 0 from bool False explicitly by type identity.
        self.assertIs(type(d["used_percent"]), int)
        self.assertNotIsInstance(d["used_percent"], bool)
        self.assertEqual(
            cast(int, d["used_percent"]) + cast(int, d["remaining_percent"]), 100
        )
        # Re-parsing preserves the integer shape exactly.
        w2 = CapacityWindow.from_dict(d)
        self.assertIs(type(w2.used_percent), int)
        self.assertNotIsInstance(w2.used_percent, bool)

    def test_enum_values_serialize_exactly(self) -> None:
        snap = CapacitySnapshot.from_dict(_clone(openai_healthy()))
        s = snap.to_dict()
        self.assertEqual(s["status"], "ok")
        self.assertEqual(_windows(s)[0]["resource"], "tokens")
        self.assertEqual(_windows(s)[0]["kind"], "five_hour")

    def test_diagnostics_shape(self) -> None:
        payload: dict[str, object] = {
            "schema_version": 1,
            "provider": "zai",
            "source": "zai_usage_endpoint",
            "retrieved_at": "2026-09-01T22:49:51.000Z",
            "status": "ok",
            "windows": [
                _window(kind="unknown", used=None, remaining=None, resets_at=None, window_id="win-a")
            ],
            "diagnostics": [
                {"code": "window_semantics_unknown", "window_id": "win-a"},
                {"code": "percentage_unknown", "window_id": "win-a"},
            ],
        }
        snap = CapacitySnapshot.from_dict(_clone(payload))
        d = snap.to_dict()
        codes = {x["code"] for x in _diagnostics(d)}
        self.assertIn("window_semantics_unknown", codes)
        self.assertIn("percentage_unknown", codes)
        diag_with_wid = [x for x in _diagnostics(d) if x["code"] == "window_semantics_unknown"][0]
        self.assertEqual(diag_with_wid["window_id"], "win-a")

    def test_no_python_impl_detail_in_serialized(self) -> None:
        snap = CapacitySnapshot.from_dict(_clone(openai_healthy()))
        text = json.dumps(snap.to_dict())
        for forbidden in ("capacity.", "CapacitySnapshot", "CapacityWindow",
                          "CapacityDiagnostic", "LocalRuntime", "class ", "dataclass"):
            self.assertNotIn(forbidden, text, msg=f"forbidden in serialized: {forbidden!r}")


class CredentialSafety(unittest.TestCase):
    """The safe-id validator rejects identifiers that contain characters the
    v1 safe-id rule does not allow: uppercase, spaces, forward/slash,
    leading punctuation, over-length, or non-ASCII. This covers the core
    guarantee that no credential shape with those characters can slip in."""

    def _unsafe(self, where: str, bad_value: str) -> dict[str, object]:
        p = openai_healthy()
        if where == "plan":
            p["plan"] = bad_value
        elif where == "source":
            p["source"] = bad_value
        elif where == "window_id":
            _windows(p)[0]["provider_metadata"] = {"window_id": bad_value}
        elif where == "diag_wid":
            p["status"] = "ok"
            p["diagnostics"] = [{"code": "window_semantics_unknown", "window_id": bad_value}]
        else:
            raise ValueError(where)
        return _clone(p)

    UNSAFE_VALUES: list[tuple[str, str]] = [
        ("uppercase",          "Sk-fake"),
        ("space",              "sk fake"),
        ("forward_slash",      "sk/abc"),
        ("backslash",          "sk\\abc"),
        ("over_length",        "a" * 65),
        ("non_ascii",          "sk-abc-中文"),
        ("dollar",             "sk-abc$1"),
        ("ampersand",          "sk-abc&1"),
        ("pipe",               "sk|abc"),
        ("question",           "sk?abc"),
    ]

    def test_unsafe_identifier_rejected_all_fields(self) -> None:
        for label, bad in self.UNSAFE_VALUES:
            for where in ("plan", "source", "window_id", "diag_wid"):
                with self.assertRaises(
                    CapacityValidationError,
                    msg=f"{label} in {where} ({bad!r})",
                ):
                    _ = CapacitySnapshot.from_dict(self._unsafe(where, bad))

    def test_safe_shaped_id_is_accepted(self) -> None:
        # A safe-shaped identifier (matches [a-z0-9][a-z0-9._:-]{0,63}) is
        # accepted by the core — the core is provider-independent and does not
        # inspect content.  The adapter is responsible for ensuring safety.
        p = openai_healthy()
        p["plan"] = "sk-fake"  # valid safe-id shape
        snap = CapacitySnapshot.from_dict(_clone(p))
        self.assertEqual(snap.plan, "sk-fake")
        self.assertEqual(snap.to_dict()["plan"], "sk-fake")


class ApiExports(unittest.TestCase):
    def test_public_api(self) -> None:
        # Exposed names are importable at the package level.
        self.assertTrue(callable(CapacitySnapshot.from_dict))
        self.assertTrue(callable(CapacitySnapshot.to_dict))
        self.assertTrue(callable(CapacityWindow.from_dict))
        self.assertTrue(callable(CapacityDiagnostic.from_dict))
        self.assertTrue(callable(LocalRuntime.from_dict))
        self.assertIn(CapacityError, CapacityValidationError.__mro__)


# ═════════════════════════ INVALID — must be rejected ═════════════════════════


class InvalidSnapshots(unittest.TestCase):
    def assert_rejected(self, payload: dict[str, object]) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = CapacitySnapshot.from_dict(_clone(payload))

    def test_01_unsupported_schema_version_wrong_int(self) -> None:
        p = openai_healthy()
        p["schema_version"] = 2
        self.assert_rejected(p)

    def test_02_unsupported_schema_version_non_int(self) -> None:
        p = openai_healthy()
        p["schema_version"] = "1"
        self.assert_rejected(p)

    def test_02b_schema_version_bool_rejected(self) -> None:
        p = openai_healthy()
        p["schema_version"] = True  # bool is technically int in Python; reject
        self.assert_rejected(p)

    def test_03_invalid_status_outside_vocab(self) -> None:
        p = openai_healthy()
        p["status"] = "degraded"
        self.assert_rejected(p)

    def test_04_only_one_percent_present_used(self) -> None:
        p = openai_healthy()
        w = _windows(p)[0]
        _ = w.pop("remaining_percent")
        self.assert_rejected(p)

    def test_04b_only_one_present_remaining(self) -> None:
        p = openai_healthy()
        w = _windows(p)[0]
        _ = w.pop("used_percent")
        self.assert_rejected(p)

    def test_05_percentage_out_of_range_high(self) -> None:
        p = openai_healthy()
        w = _windows(p)[0]
        w["used_percent"] = 101
        w["remaining_percent"] = 99
        self.assert_rejected(p)

    def test_05b_percentage_out_of_range_negative(self) -> None:
        p = openai_healthy()
        w = _windows(p)[0]
        w["used_percent"] = -1
        w["remaining_percent"] = 101
        self.assert_rejected(p)

    def test_05c_percentage_non_integer(self) -> None:
        p = openai_healthy()
        w = _windows(p)[0]
        w["used_percent"] = 6.5
        w["remaining_percent"] = 93.5
        self.assert_rejected(p)

    def test_05d_percentage_boolean_rejected(self) -> None:
        p = openai_healthy()
        w = _windows(p)[0]
        w["used_percent"] = True
        w["remaining_percent"] = False
        self.assert_rejected(p)

    def test_06_contradictory_pair(self) -> None:
        p = openai_healthy()
        w = _windows(p)[0]
        w["used_percent"] = 40
        w["remaining_percent"] = 55  # 40+55 = 95, not 100
        self.assert_rejected(p)

    def test_07_non_canonical_timestamp_offset(self) -> None:
        p = openai_healthy()
        p["retrieved_at"] = "2026-09-01T22:49:51+00:00"
        self.assert_rejected(p)

    def test_07b_non_canonical_timestamp_local(self) -> None:
        p = openai_healthy()
        p["retrieved_at"] = "2026-09-01T22:49:51.000-05:00"
        self.assert_rejected(p)

    def test_07c_non_canonical_timestamp_missing_fraction(self) -> None:
        p = openai_healthy()
        p["retrieved_at"] = "2026-09-01T22:49:51Z"
        self.assert_rejected(p)

    def test_07d_non_canonical_timestamp_wrong_fraction(self) -> None:
        p = openai_healthy()
        p["retrieved_at"] = "2026-09-01T22:49:51.0000Z"
        self.assert_rejected(p)

    def test_07e_non_canonical_reset_window(self) -> None:
        p = openai_healthy()
        _windows(p)[0]["resets_at"] = "2026-09-02T04:00:00Z"
        self.assert_rejected(p)

    def test_07f_invalid_calendar_date(self) -> None:
        p = openai_healthy()
        p["retrieved_at"] = "2026-02-30T00:00:00.000Z"
        self.assert_rejected(p)

    def test_08_windows_on_auth_required(self) -> None:
        p = openai_healthy()
        p["status"] = "auth_required"
        # keep windows; must be rejected
        p["diagnostics"] = [{"code": "auth_required"}]
        self.assert_rejected(p)

    def test_08b_windows_on_unsupported(self) -> None:
        p = openai_healthy()
        p["status"] = "unsupported"
        p["diagnostics"] = [{"code": "unsupported_source"}]
        self.assert_rejected(p)

    def test_08c_windows_on_schema_changed(self) -> None:
        p = openai_healthy()
        p["status"] = "schema_changed"
        p["diagnostics"] = [{"code": "schema_changed"}]
        self.assert_rejected(p)

    def test_08d_windows_on_unavailable(self) -> None:
        p = openai_healthy()
        p["status"] = "unavailable"
        p["diagnostics"] = [{"code": "source_unavailable"}]
        self.assert_rejected(p)

    def test_09_local_runtime_rejects_fake_quota_fields(self) -> None:
        # The core must not be able to represent a local runtime with a quota
        # percentage, an "unlimited" marker, or a scarcity score. The
        # LocalRuntime object has exactly the documented keys and nothing else,
        # so any injected fake-quota field is rejected.
        for fake in ({"quota_percent": 100}, {"unlimited": True},
                     {"scarcity": 0.0}, {"percentage": 42}, {"remaining": 100}):
            with self.assertRaises(CapacityValidationError, msg=f"rejected {fake}"):
                _ =                 _ = LocalRuntime.from_dict({
                    "reachable": True,
                    "model_presence": "present",
                    "model_name": "qwen3.8:27b",
                    **fake,
                })

    def test_10_unsafe_identifier_uppercase(self) -> None:
        p = openai_healthy()
        p["plan"] = "Plus"
        self.assert_rejected(p)

    def test_10b_unsafe_identifier_leading_punctuation(self) -> None:
        p = openai_healthy()
        p["plan"] = "-plus"
        self.assert_rejected(p)

    def test_10c_unsafe_identifier_too_long(self) -> None:
        p = openai_healthy()
        p["plan"] = "a" * 65
        self.assert_rejected(p)

    def test_10d_unsafe_identifier_credential_like(self) -> None:
        p = openai_healthy()
        p["plan"] = "sk-abc/def"
        self.assert_rejected(p)

    def test_10e_unsafe_source_with_forward_slash(self) -> None:
        p = openai_healthy()
        p["source"] = "zai/usage"
        self.assert_rejected(p)

    def test_11_diagnostic_code_outside_allowlist(self) -> None:
        p = openai_healthy()
        p["diagnostics"] = [{"code": "everything_is_fine"}]
        self.assert_rejected(p)

    def test_11b_diagnostic_window_code_requires_safe_id(self) -> None:
        p = openai_healthy()
        p["diagnostics"] = [
            {"code": "percentage_unknown", "window_id": "UPPERCASE_WID"},
        ]
        self.assert_rejected(p)

    def test_11c_snapshot_scoped_code_cannot_carry_window_id(self) -> None:
        p = openai_healthy()
        p["status"] = "unavailable"
        p["windows"] = []
        p["diagnostics"] = [{"code": "source_unavailable", "window_id": "win"}]
        self.assert_rejected(p)

    def test_11d_status_missing_required_code(self) -> None:
        p = openai_healthy()
        p["status"] = "unavailable"
        p["windows"] = []
        p["diagnostics"] = []
        self.assert_rejected(p)

    def test_12_undocumented_top_level_field(self) -> None:
        p = openai_healthy()
        p["account"] = "acct_123"
        self.assert_rejected(p)

    def test_12b_unknown_window_key(self) -> None:
        p = openai_healthy()
        _windows(p)[0]["unknown_key"] = "x"
        self.assert_rejected(p)

    def test_12c_provider_metadata_extra_key(self) -> None:
        p = openai_healthy()
        _windows(p)[0]["provider_metadata"] = {
            "window_id": "win",
            "raw": "extra",
        }
        self.assert_rejected(p)

    def test_13_five_hour_wrong_duration(self) -> None:
        p = openai_healthy()
        _windows(p)[0]["duration_seconds"] = 3600
        self.assert_rejected(p)

    def test_13b_weekly_wrong_duration(self) -> None:
        p = openai_healthy()
        _windows(p)[1]["duration_seconds"] = 86400
        self.assert_rejected(p)

    def test_14_unknown_kind_does_not_require_duration(self) -> None:
        p = openai_healthy()
        _windows(p)[0] = _window(kind="unknown", duration_seconds=None, used=10, remaining=90)
        snap = CapacitySnapshot.from_dict(_clone(p))
        self.assertIsNone(snap.windows[0].duration_seconds)

    def test_15_resource_tokens_and_time_independent_from_kind(self) -> None:
        # time resource with unknown kind is valid.
        w = CapacityWindow.from_dict({"resource": "time", "kind": "unknown"})
        self.assertEqual(w.resource, "time")
        self.assertEqual(w.kind, "unknown")
        # tokens resource with unknown kind is valid.
        w2 = CapacityWindow.from_dict({"resource": "tokens", "kind": "unknown"})
        self.assertEqual(w2.resource, "tokens")

    def test_16_unknown_resource_in_window(self) -> None:
        w = CapacityWindow.from_dict({"resource": "unknown", "kind": "unknown"})
        self.assertEqual(w.resource, "unknown")

    def test_17_window_unknown_resource_rejects_invalid(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = CapacityWindow.from_dict({"resource": "credits", "kind": "unknown"})

    def test_18_local_runtime_rejects_invalid_presence(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ =                 _ = LocalRuntime.from_dict({
                "reachable": True,
                "model_presence": "degraded",
            })

    def test_19_local_runtime_rejects_non_positive_context(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ =                 _ = LocalRuntime.from_dict({
                "reachable": True,
                "model_presence": "present",
                "model_name": "m",
                "configured_context_tokens": 0,
            })

    def test_20_local_runtime_rejects_invalid_context_type(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ =                 _ = LocalRuntime.from_dict({
                "reachable": True,
                "model_presence": "present",
                "model_name": "m",
                "configured_context_tokens": "big",
            })


# ══════════════════════════ REGRESSION: findings 1 & 2 ═══════════════════════


class ConstructorInvariants(unittest.TestCase):
    """Every public dataclass must enforce the same invariants on the
    direct-constructor path that ``from_dict`` enforces (finding 1). A frozen
    dataclass with no ``__post_init__`` let callers bypass validation entirely,
    producing objects that violated the contract but never raised. These tests
    pin the constructor down to the same rules."""

    def test_diagnostic_rejects_invalid_code(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = CapacityDiagnostic(code="not-a-real-code")

    def test_diagnostic_valid_builds(self) -> None:
        d = CapacityDiagnostic(code="window_semantics_unknown", window_id="win-a")
        self.assertEqual(d.code, "window_semantics_unknown")

    def test_window_rejects_contradictory_pair(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = CapacityWindow(
                resource="tokens",
                kind="unknown",
                used_percent=60,
                remaining_percent=60,
            )

    def test_window_rejects_five_hour_wrong_duration(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = CapacityWindow(
                resource="tokens",
                kind="five_hour",
                duration_seconds=1,
            )

    def test_window_valid_builds(self) -> None:
        w = CapacityWindow(
            resource="tokens",
            kind="five_hour",
            duration_seconds=18_000,
            used_percent=6,
            remaining_percent=94,
        )
        self.assertEqual(w.used_percent, 6)

    def test_local_runtime_present_requires_model_name(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = LocalRuntime(reachable=True, model_presence="present")

    def test_local_runtime_unreachable_requires_unknown(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = LocalRuntime(reachable=False, model_presence="present", model_name="m")

    def test_snapshot_rejects_non_ok_with_missing_code(self) -> None:
        # status "unavailable" requires the "quota_unavailable" diagnostic.
        with self.assertRaises(CapacityValidationError):
            _ = CapacitySnapshot(
                schema_version=1,
                provider="openai",
                source="codex_app_server",
                retrieved_at="2026-09-02T04:00:00.000Z",
                status="unavailable",
                windows=(),
                diagnostics=(),
            )


class FromDictMissingKeyGuard(unittest.TestCase):
    """``from_dict`` must raise ``CapacityValidationError`` for missing required
    keys (and an empty mapping), never leak a raw ``KeyError`` (finding 2)."""

    def test_snapshot_empty_mapping(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = CapacitySnapshot.from_dict({})

    def test_window_missing_kind(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = CapacityWindow.from_dict({"resource": "tokens"})

    def test_diagnostic_missing_code(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ = CapacityDiagnostic.from_dict({})

    def test_local_runtime_missing_both(self) -> None:
        with self.assertRaises(CapacityValidationError):
            _ =                 _ = LocalRuntime.from_dict({})


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
