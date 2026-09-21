"""Real installed Codex smoke probe (M10-B area 5, local-only, opt-in).

SKIPPED when no real Codex binary exists on this machine (CI stays
deterministic); when present it runs the adapter's bounded READ-ONLY
eligibility path against the REAL binary with a REAL controlled home under
a tmpdir state directory:

- discovery (the Stage-1-evidenced U-001 VS Code extension layout is
  located read-only; the adapter's own discovery verifies the pinned
  binary),
- ``codex --version`` (bounded, parsed against the supported generation),
- the ``initialize`` handshake over stdio JSONL, including the
  controlled-home adoption check the adapter enforces,
- the official ``account/read`` verdict on the fresh, UNSIGNED-IN
  controlled home.

NO turn is ever started, NO quota read is sent
(``account/rateLimits/read`` is never called), and the user's real
``~/.codex`` is never pointed at, read or written: the runtime's
``CODEX_HOME`` is the adapter-owned controlled home inside the test's
tmpdir state directory, and the handshake proves the runtime adopted it.

The probe is supplementary evidence only: the deterministic suite must
never depend on it, and a pass proves structure (handshake shape, home
adoption, honest unsigned-in verdict) — never eligibility, never
subscription capability (D-039/D-042).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import override

from scarcity_router.worker_codex_adapter import (
    CONTROLLED_CONFIG_NAME,
    MIN_SUPPORTED_CODEX_VERSION,
    CodexLocalAdapter,
    default_codex_spawner,
    discover_codex_binary,
    probe_codex_version,
)

from tests.m10_codex_fixtures import (
    fixture_path_lookup,
    canonical_now,
    codex_identity,
)


def locate_real_binary() -> Path | None:
    """The Stage-1-evidenced extension layout, located read-only.

    ``~/.vscode-server/extensions/openai.chatgpt-*/bin/linux-x86_64/codex``
    (docs/codex-adapter-stage1-evidence.md, P2/P3). The glob only OBSERVES
    the layout; nothing is executed, upgraded or mutated here.
    """
    roots = sorted(
        Path.home().glob(
            ".vscode-server/extensions/openai.chatgpt-*/bin/linux-x86_64/codex"
        )
    )
    for candidate in roots:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


class RealCodexBinaryProbeTests(unittest.TestCase):
    """Structure-only probe against the real binary (skips when absent)."""

    binary_path: Path

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.binary_path = Path(".")

    @override
    def setUp(self) -> None:
        if sys.platform != "linux":
            self.skipTest("the evidenced local profile is Linux/WSL2 x86_64")
        binary = locate_real_binary()
        if binary is None:
            self.skipTest("no real codex binary on this machine (skipped by design)")
        self.binary_path = binary

    def test_structure_only_probe_against_the_real_binary(self) -> None:
        with TemporaryDirectory(prefix="scarcity-router-m10b-real-probe-") as tmp:
            state_dir = Path(tmp)

            # Discovery: the adapter's own verification of the pinned
            # binary (regular executable non-symlink file).
            binary, reason = discover_codex_binary(
                pinned_binary=self.binary_path,
                discovery_roots=(),
                path_lookup=lambda name: None,
            )
            self.assertIsNone(reason)
            assert binary is not None
            self.assertEqual("pinned", binary.source)

            # ``codex --version`` — real, bounded, parsed.
            version, version_reason = probe_codex_version(
                binary.path, spawner=default_codex_spawner
            )
            self.assertIsNone(version_reason, version_reason)
            assert version is not None
            self.assertGreaterEqual(version, MIN_SUPPORTED_CODEX_VERSION)

            # The adapter's READ-ONLY eligibility path against the real
            # runtime: discovery → version → sandbox gate → controlled
            # home → initialize handshake (adoption enforced) → the
            # official account/read verdict. NO turn, NO quota read.
            #
            # The bubblewrap lookup is deliberately satisfied by the
            # fixture: this probe executes no command inside any sandbox,
            # and the real sandbox-availability discipline is owned by the
            # M06 suite (a bwrap-less host is honestly ineligible for
            # execution; this structure-only probe is not an execution).
            adapter = CodexLocalAdapter(
                resource=codex_identity(),
                state_dir=state_dir,
                pinned_binary=binary.path,
                discovery_roots=(),
                path_lookup=fixture_path_lookup,
                spawner=default_codex_spawner,
            )
            snapshots = adapter.resource_snapshots(canonical_now())
            self.assertEqual(1, len(snapshots))
            snapshot = snapshots[0]
            # An unsigned-in fresh controlled home is honestly INELIGIBLE:
            # either a structured auth_missing verdict (auth_required) or
            # a fail-closed auth_unverified (unknown). ANY other status
            # means the real protocol drifted from the validated shape.
            self.assertIn(
                snapshot.health.status,
                ("auth_required", "unknown"),
                f"unexpected real-binary verdict: {snapshot.health.status}",
            )
            self.assertEqual(1, len(snapshot.health.diagnostics))
            self.assertIn(
                snapshot.health.diagnostics[0].code,
                ("auth_required", "telemetry_unknown"),
            )

            # The controlled home was really provisioned and really
            # adopted (the handshake verifies the runtime's codexHome
            # echo); no credential material was ever created there.
            home = state_dir / "codex" / "codex-home"
            self.assertTrue(home.is_dir())
            self.assertTrue((home / CONTROLLED_CONFIG_NAME).is_file())
            self.assertFalse((home / "auth.json").exists())
