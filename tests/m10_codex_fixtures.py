"""Shared M10-B Codex end-to-end acceptance fixtures (issue #95).

Everything here is deterministic, synthetic and CI-safe: the Codex backend
is ALWAYS ``tests/codex_fake_appserver.py`` (never a real binary), and
every secret-like string is a conspicuous throwaway. The Codex resource
binds the SHIPPED recommendation catalog through its PHYSICAL model
identity (D-042: ``openai`` / ``gpt-5.6-sol``; the slug ``codex`` is the
execution surface ``local_adapter_id``, never a model), and the composed
deployment's compatibility matrix is the production one — M04 preset
evidence plus the reviewed M06 Codex worker-local evidence cells built by
``server_composition`` from administrator configuration (issue #106).
No synthetic catalog entry and no synthetic compatibility cell exists
anywhere in these fixtures.

Served compositions: the composed-server pieces (resource document,
audit-store reader, packaged-style worker subprocess helpers) shared by
every suite in ``tests/test_e2e_codex_acceptance.py`` — server surfaces,
coordinator, worker protocol, worker endpoint and the real
``CodexLocalAdapter`` are all the production code paths.
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

from scarcity_router.resource_state import ResourceIdentity
from scarcity_router.server_store import STORE_FILE_NAME

from tests.test_worker_codex_adapter import FAKE as FAKE_APP_SERVER
from tests.test_worker_codex_adapter import FakeCodexSpawner

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from scarcity_router.worker_local_adapters import LocalAdapterRegistry

REPO = Path(__file__).resolve().parents[1]

# ── Shared identities and synthetic secrets ───────────────────────────────────

RESOURCE_ID = "codex-e2e"
ADAPTER_ID = "codex"

#: The physical model the Codex resource represents (D-042): a REAL
#: calibrated slug from the shipped ``model-catalog.json`` — never the
#: execution surface name. The shipped catalog calibrates ``gpt-5.6-sol``
#: with the ``medium`` and ``high`` reasoning variants.
CODEX_CATALOG_MODEL = "gpt-5.6-sol"
CODEX_CATALOG_EFFORTS: tuple[str, ...] = ("medium", "high")

#: The pinned client reference resolves against the shipped catalog's
#: calibrated ``gpt-5.6-sol``/``high`` identity.
PIN_MODEL = f"sr-pin:{RESOURCE_ID}/openai/{CODEX_CATALOG_MODEL}/high"

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


def codex_identity(
    resource_id: str = RESOURCE_ID,
    *,
    model: str = CODEX_CATALOG_MODEL,
) -> ResourceIdentity:
    """The resource identity the real Codex adapter serves.

    Mirrors ``worker_client.build_registry`` exactly: provider ``openai``,
    the configured PHYSICAL model slug, entitlement ``subscription_included``,
    channel ``worker_bridged`` — variant-less (the M02 binding rule binds
    every calibrated variant of the physical model).
    """
    return ResourceIdentity(
        resource_id=resource_id,
        channel="worker_bridged",
        provider="openai",
        model=model,
        entitlement="subscription_included",
    )


def codex_resource_document(
    worker_id: str, *, model: str = CODEX_CATALOG_MODEL
) -> dict[str, object]:
    """The administrator configuration binding the Codex resource to a worker.

    The registration identity names the physical model (the worker's
    ``--codex-model`` value); ``local_adapter_id`` names the execution
    surface.
    """
    return {
        "registration": {
            "identity": {
                "resource_id": RESOURCE_ID,
                "channel": "worker_bridged",
                "provider": "openai",
                "model": model,
                "entitlement": "subscription_included",
            },
            "freshness_ttl_seconds": 300,
            "capabilities": {"context_limit_tokens": 272_000},
        },
        "enabled": True,
        "worker_id": worker_id,
        "local_adapter_id": ADAPTER_ID,
    }


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
    model: str = CODEX_CATALOG_MODEL,
) -> tuple[LocalAdapterRegistry, FakeCodexSpawner]:
    """A LocalAdapterRegistry holding one fake-backed CodexLocalAdapter.

    The adapter is built exactly like ``worker_client.build_registry``
    builds it (same identity shape, same allowlist id), with the reviewed
    injectable seams (spawner, path lookup, discovery roots) replaced by
    deterministic fixtures.
    """
    from scarcity_router.worker_codex_adapter import CodexLocalAdapter
    from scarcity_router.worker_local_adapters import LocalAdapterRegistry

    spawner = FakeCodexSpawner(scenario, trace_path=str(trace_path))
    adapter = CodexLocalAdapter(
        resource=codex_identity(resource_id, model=model),
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


def make_codex_source_registry(
    scenario: dict[str, object] | None,
    *,
    state_dir: Path,
    trace_path: Path,
    source_id: str,
    inventory_ttl_seconds: float = 300.0,
) -> tuple[LocalAdapterRegistry, FakeCodexSpawner]:
    """A registry holding one SOURCE-INSTANCE Codex adapter (D-053).

    Same construction discipline as ``make_codex_registry``, in source
    mode: adapter id ``codex:<source_id>``, per-source controlled home
    under ``state_dir/codex-sources/<source_id>/``, resources discovered
    from the runtime's own listing — nothing configured by hand.
    """
    from scarcity_router.worker_codex_adapter import CodexLocalAdapter
    from scarcity_router.worker_local_adapters import LocalAdapterRegistry

    spawner = FakeCodexSpawner(scenario, trace_path=str(trace_path))
    adapter = CodexLocalAdapter(
        source_id=source_id,
        state_dir=state_dir,
        discovery_roots=(),
        path_lookup=fixture_path_lookup,
        spawner=spawner,
        inventory_ttl_seconds=inventory_ttl_seconds,
        platform_name="linux",
        platform_release="6.x-generic",
    )
    registry = LocalAdapterRegistry()
    registry.register(adapter)
    return registry, spawner


def codex_source_document(worker_id: str, *, source_id: str) -> dict[str, object]:
    """The administrator SOURCE configuration (no model slug anywhere)."""
    return {
        "source_id": source_id,
        "kind": "codex_subscription",
        "label": "Personal ChatGPT Pro",
        "worker_id": worker_id,
        "entitlement": "subscription_included",
    }


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


def stop_worker_process(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Fully own the worker subprocess and return ``(stdout, stderr)``.

    Bounded terminate/kill/reap first, then drain and CLOSE both pipes
    deterministically: the writers are dead after the reap, so the reads
    cannot block, and closing here is what keeps the suite free of
    unclosed-pipe ResourceWarnings. Safe to call more than once.
    """
    if proc.poll() is None:
        proc.terminate()
        try:
            _ = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _ = proc.wait(timeout=15)
    captured: list[bytes] = []
    for stream in (proc.stdout, proc.stderr):
        data = b""
        if stream is not None and not stream.closed:
            try:
                data = stream.read() or b""
            except (OSError, ValueError):
                data = b""
            finally:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        captured.append(data)
    return captured[0], captured[1]


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
    "CODEX_CATALOG_EFFORTS",
    "CODEX_CATALOG_MODEL",
    "PIN_MODEL",
    "PROMPT",
    "REPLY",
    "RESOURCE_ID",
    "FakeCodexSpawner",
    "PathShimWorld",
    "audit_payloads",
    "fixture_path_lookup",
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
]
