"""Shared M10-B Codex end-to-end acceptance fixtures (issue #95).

Everything here is deterministic, synthetic and CI-safe: the Codex backend
is ALWAYS ``tests/codex_fake_appserver.py`` (never a real binary), the
compatibility-matrix cells and the catalog binding for the Codex model
identity are supplied programmatically as clearly-synthetic test evidence
(the composed deployment keeps its honest fail-closed defaults — issue
#106), and every secret-like string is a conspicuous throwaway.

Served compositions: the composed-server pieces (synthetic catalog
document, resource document, audit-store reader, packaged-style worker
subprocess helpers) and the bare-world pieces (``build_codex_cells``/
``build_codex_catalog`` and friends for the bare M03 gateway + real
``WorkerBridgedAdapter`` + real worker endpoint + real worker runtime,
with synthetic compatibility cells supplied exactly like
``tests/test_e2e_acceptance``'s evidenced-streaming scenario — streaming
needs recorded cells; the composed control plane hard-wires the
fail-closed empty cell set).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

from scarcity_router.capacity import CapacitySnapshot, CapacityWindow
from scarcity_router.routing_core import CompatibilityCell
from scarcity_router.selection_types import (
    CapabilityAssessment,
    CapabilityAssessments,
    CapacityScopeRef,
    EvidenceRef,
    ModelCatalog,
    ModelCatalogEntry,
    ModelHardProperties,
    ModelIdentity,
)
from scarcity_router.server_store import STORE_FILE_NAME

from tests.test_worker_codex_adapter import FAKE as FAKE_APP_SERVER
from tests.test_worker_codex_adapter import FakeCodexSpawner

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from scarcity_router.resource_state import ResourceIdentity
    from scarcity_router.worker_local_adapters import LocalAdapterRegistry

REPO = Path(__file__).resolve().parents[1]

# ── Shared identities and synthetic secrets ───────────────────────────────────

RESOURCE_ID = "codex-e2e"
ADAPTER_ID = "codex"
PIN_MODEL = f"sr-pin:{RESOURCE_ID}/openai/codex/high"

PROMPT = "SYNTHETIC-M10B-CODEX-PROMPT-k4t8"
REPLY = "Hello"

#: Conspicuous throwaway inference-client key (never a real credential).
CLIENT_KEY = "sk-sr-m10b-codex-e2e-key-0"


def canonical_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def codex_identity(resource_id: str = RESOURCE_ID) -> ResourceIdentity:
    """The resource identity the real Codex adapter serves (model ``codex``).

    Mirrors ``worker_client.build_registry`` exactly: provider ``openai``,
    model ``codex``, entitlement ``subscription_included``, channel
    ``worker_bridged``.
    """
    from scarcity_router.resource_state import ResourceIdentity

    return ResourceIdentity(
        resource_id=resource_id,
        channel="worker_bridged",
        provider="openai",
        model="codex",
        entitlement="subscription_included",
    )


def codex_resource_document(worker_id: str) -> dict[str, object]:
    """The administrator configuration binding the Codex resource to a worker."""
    return {
        "registration": {
            "identity": {
                "resource_id": RESOURCE_ID,
                "channel": "worker_bridged",
                "provider": "openai",
                "model": "codex",
                "entitlement": "subscription_included",
            },
            "freshness_ttl_seconds": 300,
            "capabilities": {"context_limit_tokens": 272_000},
        },
        "enabled": True,
        "worker_id": worker_id,
        "local_adapter_id": ADAPTER_ID,
    }


# ── Synthetic catalog binding for the Codex model identity ────────────────────
#
# ``_bind_identities`` binds a resource to catalog variants of its exact
# (provider, model); the shipped recommendation catalog has no
# (openai, "codex") entry, so a deployment executing the worker-local Codex
# resource needs a catalog binding to admit anything. The tests supply ONE
# synthetic entry with clearly-synthetic evidence — a fixture, never a
# calibration claim (intrinsic capability stays catalog-owned, D-040).

_CATALOG_EVIDENCE = {
    "source": "synthetic_test",
    "identifier": "synthetic://m10b-codex-e2e",
    "date": "2026-09-20",
}


def _assessment(rating: int) -> dict[str, object]:
    return {
        "rating": rating,
        "evidence": [_CATALOG_EVIDENCE],
        "confidence": "medium",
        "assessed_on": "2026-09-20",
        "rationale": "synthetic M10-B fixture rating; not a calibration claim",
    }


def _catalog_entry_document(variant: str, effort: str) -> dict[str, object]:
    return {
        "identity": {"provider": "openai", "model": "codex", "variant": variant},
        "display_name": f"Codex {variant} (M10-B synthetic fixture)",
        "reasoning_effort": effort,
        "hard_properties": {
            "input_context_tokens": 272_000,
            "output_tokens": 128_000,
            "supports_tool_use": True,
            "supports_vision": False,
            "supports_reasoning_mode": True,
        },
        "capabilities": {
            "reasoning": _assessment(4),
            "coding": _assessment(4),
            "scientific_methodological": _assessment(3),
            "writing_editorial": _assessment(3),
            "tool_use": _assessment(4),
            "translation_multilingual": _assessment(3),
        },
        "capacity_bindings": [{"provider": "openai", "scope_id": "codex"}],
        "model_version_date": "2026-09-20",
        "last_reviewed_on": "2026-09-20",
    }


def write_codex_catalog_document(path: Path) -> Path:
    """Write the synthetic catalog document used by the composed tests."""
    document = {
        "catalog_version": 1,
        "updated_on": "2026-09-20",
        "entries": [_catalog_entry_document("high", "high")],
    }
    _ = path.write_text(json.dumps(document, indent=1), encoding="utf-8")
    return path


def build_codex_catalog() -> ModelCatalog:
    """The same synthetic binding as in-memory selection_types objects."""
    evidence = EvidenceRef(
        source="synthetic_test",
        identifier="synthetic://m10b-codex-e2e",
        date="2026-09-20",
    )

    def known(rating: int) -> CapabilityAssessment:
        return CapabilityAssessment(
            rating=rating,
            evidence=(evidence,),
            confidence="medium",
            assessed_on="2026-09-20",
            rationale="synthetic M10-B fixture rating; not a calibration claim",
        )

    unknown = CapabilityAssessment(rating=None)
    entry = ModelCatalogEntry(
        identity=ModelIdentity(provider="openai", model="codex", variant="high"),
        display_name="Codex high (M10-B synthetic fixture)",
        hard_properties=ModelHardProperties(
            input_context_tokens=272_000,
            output_tokens=128_000,
            supports_tool_use=True,
            supports_vision=False,
            supports_reasoning_mode=True,
        ),
        capabilities=CapabilityAssessments(
            reasoning=known(4),
            coding=known(4),
            scientific_methodological=unknown,
            writing_editorial=unknown,
            tool_use=known(4),
            translation_multilingual=unknown,
        ),
        capacity_bindings=(CapacityScopeRef(provider="openai", scope_id="codex"),),
        reasoning_effort="high",
    )
    return ModelCatalog(
        catalog_version=1,
        updated_on="2026-09-20",
        entries=(entry,),
    )


# ── Synthetic compatibility-matrix cells (programmatic seam, issue #106) ──────

_SYNTHETIC_EVIDENCE = EvidenceRef(
    source="synthetic_test",
    identifier="synthetic://m10b-codex-cells",
    date="2026-09-20",
)

#: The cell values the M10-B streaming world runs with. They mirror the
#: Stage-2 adapter matrix honestly: everything the adapter maps is PASS,
#: the stable-surface ``tool_calls``/``tool_results`` positions are
#: UNSUPPORTED (and drive the fail-closed pre-dispatch rejection test).
CODEX_CELL_VALUES: dict[str, str] = {
    "roles_history": "PASS",
    "streaming": "PASS",
    "tool_calls": "UNSUPPORTED",
    "tool_results": "UNSUPPORTED",
    "structured_output": "PASS",
    "reasoning_controls": "PASS",
}


def build_codex_cells(
    overrides: dict[str, str] | None = None,
) -> tuple[CompatibilityCell, ...]:
    """Synthetic cells for (worker_bridged, openai, codex), with overrides.

    Exactly the programmatic seam ``tests/test_e2e_acceptance.py``'s
    evidenced-streaming scenario uses: explicit test evidence supplied to
    the application constructor — the fail-closed default (an empty cell
    set) is never weakened anywhere.
    """
    values = dict(CODEX_CELL_VALUES)
    if overrides is not None:
        values.update(overrides)
    return tuple(
        CompatibilityCell(
            channel="worker_bridged",
            provider="openai",
            model="codex",
            feature=feature,
            value=value,
            adapter="codex-worker-local",
            adapter_version="1.0.0",
            evidence=_SYNTHETIC_EVIDENCE,
        )
        for feature, value in values.items()
    )


def build_codex_capacity_snapshots() -> tuple[CapacitySnapshot, ...]:
    """One healthy synthetic capacity snapshot for the codex scope."""
    return (
        CapacitySnapshot(
            schema_version=3,
            provider="openai",
            source="synthetic_test",
            retrieved_at=canonical_now(),
            status="ok",
            windows=(
                CapacityWindow(
                    resource="tokens",
                    kind="five_hour",
                    scope_id="codex",
                    duration_seconds=18_000,
                    used_percent=10,
                    remaining_percent=90,
                    window_id="codex-five",
                ),
            ),
            diagnostics=(),
        ),
    )


# ── Path lookups and the fake-backed adapter registry ─────────────────────────


def fixture_path_lookup(name: str) -> str | None:
    """A path lookup answering the two binaries the fixtures provide.

    ``codex`` resolves to the shared fake App Server (so the adapter's REAL
    PATH discovery finds it, exactly as a standalone CLI install would be
    discovered) and ``bwrap`` satisfies the documented sandbox-prerequisite
    probe. The sandbox binary is never executed by any test here (the fake
    App Server models the protocol; no command runs inside any sandbox).
    """
    if name == "bwrap":
        return "/usr/bin/bwrap"
    if name == "codex":
        return str(FAKE_APP_SERVER)
    return None


def make_codex_registry(
    scenario: dict[str, object] | None,
    *,
    state_dir: Path,
    trace_path: Path,
    resource_id: str = RESOURCE_ID,
) -> tuple[LocalAdapterRegistry, FakeCodexSpawner]:
    """A LocalAdapterRegistry holding one fake-backed CodexLocalAdapter.

    The adapter is built exactly like ``worker_client.build_registry``
    builds it (same identity, same allowlist id), with the reviewed
    injectable seams (spawner, path lookup, discovery roots) replaced by
    deterministic fixtures.
    """
    from scarcity_router.worker_codex_adapter import CodexLocalAdapter
    from scarcity_router.worker_local_adapters import LocalAdapterRegistry

    spawner = FakeCodexSpawner(scenario, trace_path=str(trace_path))
    adapter = CodexLocalAdapter(
        resource=codex_identity(resource_id),
        state_dir=state_dir,
        discovery_roots=(),
        path_lookup=fixture_path_lookup,
        spawner=spawner,
        platform_name="linux",
        platform_release="6.x-generic",
    )
    registry = LocalAdapterRegistry()
    registry.register(adapter)
    return registry, spawner


def read_trace(path: Path) -> list[dict[str, object]]:
    """The fake App Server's trace records (empty when nothing ran)."""
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(cast("dict[str, object]", json.loads(line)))
    return records


def trace_methods(path: Path) -> list[str]:
    return [
        cast("str", record["method"])
        for record in read_trace(path)
        if record.get("event") == "request"
    ]


def trace_request(path: Path, method: str) -> dict[str, object] | None:
    for record in read_trace(path):
        if record.get("event") == "request" and record.get("method") == method:
            return cast("dict[str, object]", record.get("params") or {})
    return None


# ── The packaged-style PATH world (fake codex binary + bwrap stub) ────────────


class PathShimWorld:
    """A tmpdir ``PATH`` entry holding the synthetic ``codex`` binary.

    The packaged-style worker discovers ``codex`` through its REAL PATH
    discovery (no injection seam exists on the subprocess path), so the
    fixture provides a self-contained shim that translates the adapter's
    fixed argv (``--version`` / ``app-server``) onto the shared fake App
    Server and points its trace at a fixture file. A ``bwrap`` stub
    satisfies the documented sandbox-prerequisite probe; nothing executes
    inside any sandbox here. This is fixture provisioning, not an adapter
    change.
    """

    root: Path
    bin_dir: Path
    scenario_path: Path
    trace_path: Path
    codex_path: Path
    bwrap_path: Path

    def __init__(self, scenario: dict[str, object]) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="scarcity-router-m10b-path-"))
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.scenario_path = self.root / "scenario.json"
        self.trace_path = self.root / "trace.jsonl"
        _ = self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        self.codex_path = self._write_shim()
        self.bwrap_path = self._write_bwrap_stub()

    def _write_shim(self) -> Path:
        path = self.bin_dir / "codex"
        script = "\n".join(
            [
                f"#!{sys.executable}",
                "import os, sys",
                f"FAKE = {str(FAKE_APP_SERVER)!r}",
                f"SCENARIO = {str(self.scenario_path)!r}",
                f"TRACE = {str(self.trace_path)!r}",
                "args = sys.argv[1:]",
                "os.environ['SR_FAKE_TRACE'] = TRACE",
                "if args and args[0] == '--version':",
                "    os.execv(sys.executable, [sys.executable, FAKE, '--version', '0.155.1'])",
                "if args and args[0] == 'app-server':",
                "    with open(SCENARIO, encoding='utf-8') as handle:",
                "        scenario = handle.read()",
                "    os.execv(sys.executable, [sys.executable, FAKE, '--app-server', scenario])",
                "raise SystemExit(2)",
                "",
            ]
        )
        _ = path.write_text(script, encoding="utf-8")
        _ = path.chmod(0o755)
        return path

    def _write_bwrap_stub(self) -> Path:
        path = self.bin_dir / "bwrap"
        # Loud failure if anything ever tries to RUN the sandbox tool: the
        # tests only require the documented prerequisite to be discoverable.
        _ = path.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        _ = path.chmod(0o755)
        return path

    def child_env(self, *, ca_file: Path | None = None) -> dict[str, str]:
        """A minimal but sufficient environment for worker subprocesses.

        ``PATH`` carries the synthetic binaries; ``SSL_CERT_FILE`` pins the
        test CA so the REAL verified-TLS worker path trusts only the test
        listener (the production behavior modulo the trust anchor).
        """
        env = {
            "PATH": os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"]),
            "HOME": str(self.root),
            "LC_ALL": "C",
        }
        if ca_file is not None:
            env["SSL_CERT_FILE"] = str(ca_file)
        return env

    def cleanup(self) -> None:
        _ = shutil.rmtree(self.root, ignore_errors=True)


def worker_command(arguments: list[str]) -> list[str]:
    """The packaged-style worker invocation (console script when installed).

    ``uv sync`` installs the ``scarcity-router-worker`` console script next
    to the environment's interpreter; the module entry is the identical
    code path and keeps the test deterministic on checkouts without the
    installed script.
    """
    script = Path(sys.executable).parent / "scarcity-router-worker"
    if script.is_file():
        return [str(script), *arguments]
    return [sys.executable, "-m", "scarcity_router.worker_client", *arguments]


def start_worker_process(
    arguments: list[str],
    *,
    env: dict[str, str],
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - test-controlled fixed argv
        worker_command(arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(REPO),
        start_new_session=True,
    )


def stop_worker_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            _ = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _ = proc.wait(timeout=15)


# ── Audit store helper (composed world) ───────────────────────────────────────


def audit_payloads(data_dir: Path) -> list[str]:
    """Every audit record payload persisted by the composed server."""
    database = data_dir / STORE_FILE_NAME
    connection = sqlite3.connect(database)
    try:
        rows = cast(
            "list[tuple[object]]",
            connection.execute("SELECT payload FROM audit_records").fetchall(),
        )
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


__all__ = [
    "ADAPTER_ID",
    "CLIENT_KEY",
    "CODEX_CELL_VALUES",
    "PIN_MODEL",
    "PROMPT",
    "REPLY",
    "RESOURCE_ID",
    "FakeCodexSpawner",
    "PathShimWorld",
    "audit_payloads",
    "fixture_path_lookup",
    "build_codex_capacity_snapshots",
    "build_codex_catalog",
    "build_codex_cells",
    "canonical_now",
    "codex_identity",
    "codex_resource_document",
    "make_codex_registry",
    "read_trace",
    "start_worker_process",
    "stop_worker_process",
    "trace_methods",
    "trace_request",
    "worker_command",
    "write_codex_catalog_document",
]
