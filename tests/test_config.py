"""D-036 default user-configuration tests.

Covers the config-directory resolution (XDG-aware), the idempotent
provisioning of ``selector-policy.json`` from the checked-in example, the
load/precedence semantics, the process-level resolver's degrade-to-neutral
behavior, the ``install-config`` CLI command and the end-to-end CLI
precedence (explicit flag > user default config > neutral). Deterministic:
every test provisions into a throwaway ``XDG_CONFIG_HOME`` and never
touches the host configuration.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest import mock

from scarcity_router import (
    CapacitySnapshot,
    CapacityWindow,
    SelectionContractError,
    SelectorPolicy,
)
from scarcity_router.cli import main as cli_main
from scarcity_router.config import (
    SELECTOR_POLICY_FILE_NAME,
    default_selector_policy_path,
    default_selector_policy_source,
    ensure_default_user_config,
    load_default_selector_policy,
    resolve_default_selector_policy,
    user_config_dir,
)
from scarcity_router.selection_app import load_selector_policy
from scarcity_router.status import StatusCollectors

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "model-catalog.json"
POLICY_PATH = REPO / "model-policy.json"
EXAMPLE_PATH = REPO / "examples" / SELECTOR_POLICY_FILE_NAME

# A Monday 15:00 Asia/Singapore instant: inside the example policy's
# Mon–Fri 14:00–18:00 blackout, so the example policy and the neutral
# policy produce different winners.
FIXED_AT = datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc)

NEUTRAL_DOCUMENT: dict[str, object] = {
    "mode": "balanced",
    "resource_policy": {
        "policy_version": 1,
        "unknown_capacity_mode": "degraded",
        "replenishment_mode": "advisory",
        "reservations": [],
        "blackouts": [],
    },
}


@contextmanager
def _config_home(files: dict[str, object] | None = None) -> Generator[Path]:
    """A throwaway XDG config home, optionally pre-seeded with files."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        directory = home / "scarcity-router"
        directory.mkdir(mode=0o700)
        for name, document in (files or {}).items():
            _ = (directory / name).write_text(
                document if isinstance(document, str) else json.dumps(document),
                encoding="utf-8",
            )
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home)}):
            yield directory


def _snapshot(provider: str, remaining: int) -> CapacitySnapshot:
    scope = "codex" if provider == "openai" else "coding_plan"
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at="2026-09-14T07:00:00.000Z",
        status="ok",
        windows=(
            CapacityWindow(
                resource="tokens",
                kind="five_hour",
                scope_id=scope,
                duration_seconds=18_000,
                used_percent=100 - remaining,
                remaining_percent=remaining,
                window_id=f"{provider}-five",
            ),
            CapacityWindow(
                resource="tokens",
                kind="weekly",
                scope_id=scope,
                duration_seconds=604_800,
                used_percent=100 - remaining,
                remaining_percent=remaining,
                window_id=f"{provider}-weekly",
            ),
        ),
        diagnostics=(),
    )


def _collectors() -> StatusCollectors:
    def openai(*, retrieved_at: str) -> CapacitySnapshot:
        _ = retrieved_at
        return _snapshot("openai", 40)

    def zai(*, retrieved_at: str) -> CapacitySnapshot:
        _ = retrieved_at
        return _snapshot("zai", 80)

    return StatusCollectors(openai=openai, zai=zai)


class _Quiet(unittest.TestCase):
    """CLI harness: captures stdout/stderr, fixed clock, synthetic collectors."""

    def _run(
        self, argv: list[str], *, artifacts: bool = True
    ) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        full_argv = list(argv)
        if artifacts:
            full_argv += [
                "--catalog",
                str(CATALOG_PATH),
                "--model-policy",
                str(POLICY_PATH),
            ]
        with redirect_stderr(err):
            code = cli_main(
                full_argv,
                stdout=out,
                collectors=_collectors(),
                clock=lambda: FIXED_AT,
            )
        return code, out.getvalue(), err.getvalue()


class PathResolutionTests(unittest.TestCase):
    """XDG-aware directory and file path resolution."""

    def test_absolute_xdg_config_home_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_CONFIG_HOME": tmp}
            self.assertEqual(
                Path(tmp) / "scarcity-router", user_config_dir(env=env)
            )
            self.assertEqual(
                Path(tmp) / "scarcity-router" / SELECTOR_POLICY_FILE_NAME,
                default_selector_policy_path(env=env),
            )

    def test_missing_xdg_falls_back_to_home_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "XDG_CONFIG_HOME": ""}
            self.assertEqual(
                Path(tmp) / ".config" / "scarcity-router",
                user_config_dir(env=env),
            )

    def test_relative_xdg_is_ignored_per_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "XDG_CONFIG_HOME": "relative/config"}
            self.assertEqual(
                Path(tmp) / ".config" / "scarcity-router",
                user_config_dir(env=env),
            )


class ProvisioningTests(unittest.TestCase):
    """Idempotent provisioning from the audited example."""

    def test_provisions_from_the_checked_in_example(self) -> None:
        with _config_home() as directory:
            path, wrote = ensure_default_user_config()
            self.assertTrue(wrote)
            self.assertEqual(directory / SELECTOR_POLICY_FILE_NAME, path)
            self.assertEqual(
                EXAMPLE_PATH.read_text(encoding="utf-8"),
                path.read_text(encoding="utf-8"),
            )
            # Idempotent: a second run keeps the existing file.
            path2, wrote2 = ensure_default_user_config()
            self.assertFalse(wrote2)
            self.assertEqual(path, path2)
            self.assertEqual(
                EXAMPLE_PATH.read_text(encoding="utf-8"),
                path2.read_text(encoding="utf-8"),
            )

    def test_provisioned_permissions_are_private(self) -> None:
        with _config_home() as directory:
            path, _ = ensure_default_user_config()
            if os.name != "posix":  # pragma: no cover - non-POSIX hosts
                self.skipTest("POSIX permission bits only")
            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_existing_user_edits_are_never_silently_overwritten(self) -> None:
        edited = dict(NEUTRAL_DOCUMENT)
        with _config_home({SELECTOR_POLICY_FILE_NAME: edited}) as directory:
            path, wrote = ensure_default_user_config()
            self.assertFalse(wrote)
            self.assertEqual(
                json.dumps(edited), path.read_text(encoding="utf-8")
            )
            # Only --force replaces the file with the shipped defaults.
            path2, wrote2 = ensure_default_user_config(force=True)
            self.assertTrue(wrote2)
            self.assertEqual(path2, directory / SELECTOR_POLICY_FILE_NAME)
            self.assertEqual(
                EXAMPLE_PATH.read_text(encoding="utf-8"),
                path2.read_text(encoding="utf-8"),
            )

    def test_source_is_the_example_in_a_source_tree(self) -> None:
        self.assertEqual(EXAMPLE_PATH, default_selector_policy_source())


class LoadAndResolveTests(unittest.TestCase):
    """Load semantics and the process-level degrade-to-neutral resolver."""

    def test_missing_file_loads_as_none(self) -> None:
        with _config_home():
            self.assertIsNone(load_default_selector_policy())

    def test_existing_file_loads_and_validates(self) -> None:
        with _config_home({SELECTOR_POLICY_FILE_NAME: dict(NEUTRAL_DOCUMENT)}):
            policy = load_default_selector_policy()
            assert policy is not None
            self.assertEqual("balanced", policy.mode)

    def test_broken_config_is_a_loud_configuration_failure(self) -> None:
        with _config_home({SELECTOR_POLICY_FILE_NAME: "{not json"}):
            with self.assertRaises(ValueError):
                _ = load_default_selector_policy()
        invalid: dict[str, object] = {
            "mode": "balanced",
            "resource_policy": {"policy_version": 1},
        }
        with _config_home({SELECTOR_POLICY_FILE_NAME: invalid}):
            with self.assertRaises(SelectionContractError):
                _ = load_default_selector_policy()

    def test_resolver_degrades_to_neutral_with_a_warning(self) -> None:
        # An unusable policy path (the file name taken by a directory) must
        # degrade to the neutral policy with exactly one concise warning.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            blocked = home / "scarcity-router" / SELECTOR_POLICY_FILE_NAME
            blocked.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home)}):
                warnings: list[str] = []
                notes: list[str] = []
                policy = resolve_default_selector_policy(
                    warn=warnings.append, note=notes.append
                )
                self.assertIsNone(policy)
                self.assertEqual(1, len(warnings))
                self.assertIn(
                    "default selector policy unavailable", warnings[0]
                )
                self.assertEqual([], notes)
        # A broken policy file is equally a loud degrade, never a crash.
        with _config_home({SELECTOR_POLICY_FILE_NAME: "{not json"}):
            broken_warnings: list[str] = []
            broken_notes: list[str] = []
            policy = resolve_default_selector_policy(
                warn=broken_warnings.append, note=broken_notes.append
            )
            self.assertIsNone(policy)
            self.assertEqual(1, len(broken_warnings))
            self.assertIn(
                "default selector policy unavailable", broken_warnings[0]
            )
            self.assertEqual([], broken_notes)

    def test_resolver_provisions_and_loads_the_example(self) -> None:
        with _config_home() as directory:
            notes: list[str] = []
            policy = resolve_default_selector_policy(
                warn=lambda _message: None, note=notes.append
            )
            assert policy is not None
            self.assertEqual(load_selector_policy(EXAMPLE_PATH), policy)
            # The provisioning run announces the created file exactly once.
            self.assertEqual(1, len(notes))
            self.assertIn("note: provisioned default user config", notes[0])
            self.assertIn(
                str(directory / SELECTOR_POLICY_FILE_NAME), notes[0]
            )
            # An existing file is never announced again.
            _ = resolve_default_selector_policy(
                warn=lambda _message: None, note=notes.append
            )
            self.assertEqual(1, len(notes))


class InstallConfigCommandTests(_Quiet):
    """The explicit ``install-config`` CLI command."""

    def test_writes_then_keeps_then_force_replaces(self) -> None:
        with _config_home() as directory:
            path = directory / SELECTOR_POLICY_FILE_NAME
            code, out, err = self._run(["install-config"], artifacts=False)
            self.assertEqual((0, ""), (code, err))
            self.assertIn(f"wrote: {path}", out)
            code, out, err = self._run(["install-config"], artifacts=False)
            self.assertEqual((0, ""), (code, err))
            self.assertIn(f"kept existing: {path}", out)
            code, out, err = self._run(
                ["install-config", "--force"], artifacts=False
            )
            self.assertEqual((0, ""), (code, err))
            self.assertIn(f"wrote: {path}", out)
            self.assertEqual(
                EXAMPLE_PATH.read_text(encoding="utf-8"),
                path.read_text(encoding="utf-8"),
            )


class CliPrecedenceTests(_Quiet):
    """Explicit flag > user default config > neutral, end to end."""

    def _selected_model(self, output: str) -> str:
        decision = cast("dict[str, object]", json.loads(output))
        selected = cast("dict[str, object]", decision["selected"])
        identity = cast("dict[str, object]", selected["identity"])
        return cast(str, identity["model"])

    def test_user_default_config_applies_without_flags(self) -> None:
        # The provisioned example policy blocks all Z.ai models Mon–Fri
        # 14:00–18:00 SGT; the fixed clock is a Monday 15:00 SGT, so the
        # neutral winner (GLM-5.3-Flash at 80%) is policy-blocked and the
        # least scarce OpenAI configuration wins instead.
        with _config_home() as directory:
            code, out, err = self._run(
                ["select", "--profile", "routine_coding", "--json"]
            )
            self.assertEqual(0, code)
            # The implicit first provisioning is announced once on stderr;
            # the JSON stdout stays clean.
            self.assertEqual(
                [
                    "note: provisioned default user config: "
                    + str(directory / SELECTOR_POLICY_FILE_NAME)
                ],
                err.splitlines(),
            )
            self.assertEqual("gpt-5.6-luna", self._selected_model(out))

    def test_neutral_policy_flag_ignores_the_user_config(self) -> None:
        with _config_home():
            code, out, err = self._run(
                [
                    "select",
                    "--profile",
                    "routine_coding",
                    "--neutral-policy",
                    "--json",
                ]
            )
            self.assertEqual((0, ""), (code, err))
            self.assertEqual("glm-5.3-flash", self._selected_model(out))

    def test_explicit_flag_wins_over_the_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit-policy.json"
            _ = explicit.write_text(json.dumps(NEUTRAL_DOCUMENT), encoding="utf-8")
            with _config_home():
                code, out, err = self._run(
                    [
                        "select",
                        "--profile",
                        "routine_coding",
                        "--selector-policy",
                        str(explicit),
                        "--json",
                    ]
                )
                self.assertEqual((0, ""), (code, err))
                self.assertEqual("glm-5.3-flash", self._selected_model(out))

    def test_selector_policy_flags_are_mutually_exclusive(self) -> None:
        with _config_home():
            with self.assertRaises(SystemExit) as ctx:
                _ = self._run(
                    [
                        "select",
                        "--profile",
                        "routine_coding",
                        "--neutral-policy",
                        "--selector-policy",
                        str(EXAMPLE_PATH),
                    ]
                )
            self.assertEqual(2, ctx.exception.code)

    def test_simulation_resolves_the_user_default_policy(self) -> None:
        overrides = {"evaluated_at": "2026-09-14T07:00:00.000Z"}
        with tempfile.TemporaryDirectory() as tmp:
            overrides_path = Path(tmp) / "overrides.json"
            _ = overrides_path.write_text(json.dumps(overrides), encoding="utf-8")
            with _config_home():
                # The user default (blackout active at the fixed instant)
                # governs the baseline, so Z.ai is policy-blocked in the
                # CURRENT decision even though its quota is healthier.
                code, out, err = self._run(
                    [
                        "simulate",
                        "--profile",
                        "routine_coding",
                        "--overrides",
                        str(overrides_path),
                        "--json",
                    ]
                )
                self.assertEqual(0, code)
                self.assertIn(
                    "note: provisioned default user config", err
                )
                self.assertIn('"policy_blocked"', out)
                self.assertIn("gpt-5.6-luna", out)


class ExplanationRenderingTests(_Quiet):
    """End-user visible explanation of the happy-hour mechanics."""

    def _write_policy(self, directory: Path, document: dict[str, object]) -> str:
        path = directory / "policy.json"
        _ = path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def test_compact_output_names_the_active_preference(self) -> None:
        # Campaign-window override: Tuesday 02:00 SGT is inside the happy
        # hour and outside the blackout. Even the compact output must say
        # why the preference applied.
        overrides = {"evaluated_at": "2026-09-15T02:00:00+08:00"}
        with tempfile.TemporaryDirectory() as tmp:
            overrides_path = Path(tmp) / "overrides.json"
            _ = overrides_path.write_text(json.dumps(overrides), encoding="utf-8")
            code, out, err = self._run(
                [
                    "simulate",
                    "--profile",
                    "routine_coding",
                    "--selector-policy",
                    str(EXAMPLE_PATH),
                    "--overrides",
                    str(overrides_path),
                ]
            )
            self.assertEqual((0, ""), (code, err))
            self.assertIn(
                "Happy hour: rule zai-flash-campaign-night-sgt "
                + "(glm53flash_campaign_zero_quota)",
                out,
            )

    def test_explain_lists_date_expired_rules(self) -> None:
        expired: dict[str, object] = {
            "mode": "balanced",
            "resource_policy": {
                "policy_version": 1,
                "unknown_capacity_mode": "degraded",
                "replenishment_mode": "advisory",
                "reservations": [],
                "blackouts": [],
                "happy_hours": [
                    {
                        "rule_id": "flash-expired",
                        "target": {"provider": "zai"},
                        "timezone": "Asia/Singapore",
                        "weekdays": [
                            "mon", "tue", "wed", "thu", "fri", "sat", "sun",
                        ],
                        "start_local": "00:00",
                        "end_local": "23:59",
                        "reason_code": "campaign_over",
                        "start_date": "2026-09-01",
                        "end_date": "2026-09-05",
                    }
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = self._write_policy(Path(tmp), expired)
            code, out, err = self._run(
                [
                    "select",
                    "--profile",
                    "routine_coding",
                    "--selector-policy",
                    policy_path,
                    "--explain",
                ]
            )
            self.assertEqual((0, ""), (code, err))
            self.assertIn("Expired happy-hour rules", out)
            self.assertIn("rule flash-expired", out)
            # Explanation-only: the compact output carries no expiry note.
            code, out, _ = self._run(
                [
                    "select",
                    "--profile",
                    "routine_coding",
                    "--selector-policy",
                    policy_path,
                ]
            )
            self.assertEqual(0, code)
            self.assertNotIn("Expired happy-hour rules", out)


class SerializedPolicyContractTests(unittest.TestCase):
    """The example policy document parses through the public contract."""

    def test_example_round_trips_through_selector_policy(self) -> None:
        policy = load_selector_policy(EXAMPLE_PATH)
        restored = SelectorPolicy.from_dict(policy.to_dict())
        self.assertEqual(policy, restored)


if __name__ == "__main__":
    _ = unittest.main()
