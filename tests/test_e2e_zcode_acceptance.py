"""ZCode plan-lane composed acceptance (M07 Stage 2 remediation, D-063).

The FULL production path — real verified-TLS worker listener, real M05
protocol, real pairing, real composition — with the ZCode runtime
replaced by the deterministic fake CLI. Nothing shortcuts through an
adapter seam: discovery travels through the worker's state report,
adoption happens in the server's source registry (the owner-reviewed
``zai/plan`` plan-managed track), and execution is dispatched through
the normal OpenAI-compatible surface to the plan lane by PLAIN
selection (no pin, no bypass).

Central acceptance criteria pinned here:

- the logical ``plan-managed`` lane becomes routable through normal
  configuration + discovery and a plain ``model: "plan-managed"``
  request selects it through the normal selector;
- the run executes in THE AUTHORIZED PROJECT WORKSPACE (the fake CLI
  edits a file there; nothing outside the repo changes);
- an effort-bearing request can never resolve onto the lane (typed
  rejection — exact-identity preserved, no physical model invented);
- the admission deadline cancels a stuck run through the real path and
  the cancellation is recorded honestly with the child reaped.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import cast, override

from tests.m10_fixtures import wait_until
from tests.test_e2e_sources_acceptance import SourcesComposedWorld

import unittest

from scarcity_router.worker_client import WorkerRuntime
from tests.test_e2e_codex_acceptance import CodexWorker
from tests.test_worker_zcode_adapter import FakeZCodeSpawner


class ZcodeWorker:
    """One in-process paired worker running the fake-backed ZCode adapter.

    Same lifecycle plumbing as the Codex e2e worker (real protocol,
    pairing, state reports, dispatch); the spawner is the ZCode fake.
    """

    runtime: WorkerRuntime
    worker_id: str
    spawner: FakeZCodeSpawner
    _close: object

    def __init__(
        self,
        *,
        runtime: WorkerRuntime,
        worker_id: str,
        spawner: FakeZCodeSpawner,
        close: object,
    ) -> None:
        self.runtime = runtime
        self.worker_id = worker_id
        self.spawner = spawner
        self._close = close
        self.session: threading.Thread | None = None

    def start(self) -> None:
        self.session = threading.Thread(
            target=self.runtime.run, name="zcode-composed-worker", daemon=True
        )
        self.session.start()

    def stop(self) -> None:
        self.runtime.request_stop()
        if self.session is not None:
            _ = self.session.join(timeout=15)
        self.session = None

SOURCE_ID = "zai-plan-1"
LANE = f"{SOURCE_ID}:plan-managed"
SENTINEL = "SENTINEL-LINE-FROM-ZCODE-42"


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _make_git_repo(root: Path) -> Path:
    """One disposable repository: README + one trivially editable file."""
    _ = root.mkdir(parents=True, exist_ok=True)
    _ = root.mkdir(parents=True, exist_ok=True)
    _ = (root / "README.md").write_text("# disposable repo\n")
    _ = (root / "notes.txt").write_text("original line\n")
    _ = subprocess.run(  # noqa: S603 - fixed argv, test fixture
        ["git", "init", "-q"], cwd=root, check=True, timeout=30
    )
    _ = subprocess.run(  # noqa: S603 - fixed argv, test fixture
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "add", "-A"],
        cwd=root, check=True, timeout=30,
    )
    _ = subprocess.run(  # noqa: S603 - fixed argv, test fixture
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", "fixture"],
        cwd=root, check=True, timeout=30,
    )
    return root


def _git_status(root: Path) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, test fixture
        ["git", "status", "--porcelain"],
        cwd=root, check=True, timeout=30, capture_output=True, text=True,
    )
    return result.stdout


class ZcodeComposedWorld(SourcesComposedWorld):
    """The composed world with a ZCode plan-lane source and worker."""


    def start_zcode_worker(
        self,
        scenario: dict[str, object],
        workspace: Path,
        *,
        label: str = "precision-zcode",
        inventory_ttl_seconds: float = 0.5,
        state_report_interval_seconds: int = 2,
    ) -> ZcodeWorker:
        """Pair ONE worker carrying the ZCode plan-lane adapter.

        The worker is REAL (protocol, pairing, state reports, dispatch);
        the ZCode adapter's injectable seams (spawner, path lookup) carry
        the deterministic fake backend against the authorized workspace.
        """
        import socket as _socket

        from scarcity_router.gateway_validation import v_safe_id
        from scarcity_router.worker_client import SESSION_IO_TIMEOUT_SECONDS
        from scarcity_router.worker_client import WorkerOrigin
        from scarcity_router.worker_local_adapters import LocalAdapterRegistry
        from scarcity_router.worker_protocol import SocketTransport
        from scarcity_router.worker_zcode_adapter import ZCodeLocalAdapter

        store = self.open_worker_store("zcode")
        adapter_tmp = Path(tempfile.mkdtemp(prefix="scarcity-router-zcode-adapter-"))
        self.addCleanup(lambda: _rmtree(adapter_tmp))
        trace_path = adapter_tmp / "trace.jsonl"
        _ = trace_path.write_text("")
        bin_path = adapter_tmp / "bin-zcode"
        _ = bin_path.write_bytes(b"#!/bin/sh\nexit 0\n")
        _ = os.chmod(bin_path, 0o755)
        _ = workspace.resolve()  # the canonical workspace (asserted below)
        spawner = FakeZCodeSpawner(scenario, trace_path=str(trace_path))
        adapter = ZCodeLocalAdapter(
            source_id=SOURCE_ID,
            authorized_workspace=workspace,
            pinned_binary=bin_path,
            path_lookup=lambda _name: str(bin_path),
            spawner=spawner,
            inventory_ttl_seconds=inventory_ttl_seconds,
        )
        registry = LocalAdapterRegistry()
        registry.register(adapter)
        _ = v_safe_id(SOURCE_ID, "zcode_source")
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": label}
        )
        assert status == 200, payload
        code = cast("dict[str, object]", payload)["pairing_code"]
        transports: list[SocketTransport] = []
        raw_sockets: list[_socket.socket] = []

        def factory(origin_ref: WorkerOrigin) -> SocketTransport:
            raw = _socket.create_connection(
                (origin_ref.host, origin_ref.port), timeout=10
            )
            try:
                wrapped = self.tls.client_context().wrap_socket(
                    raw, server_hostname=origin_ref.host
                )
                _ = wrapped.settimeout(SESSION_IO_TIMEOUT_SECONDS)
            except BaseException:
                raw.close()
                raise
            transports.append(SocketTransport(wrapped))
            raw_sockets.append(wrapped)
            return transports[-1]

        runtime = WorkerRuntime(
            origin=self.worker_origin(),
            store=store,
            local_adapters=registry,
            state_report_interval_seconds=state_report_interval_seconds,
            connect_factory=factory,
        )
        identity = runtime.pair(str(code))
        transports.clear()
        raw_sockets.clear()
        plumbing = CodexWorker(
            runtime=runtime,
            worker_id=identity.worker_id,
            store=store,
            spawner=spawner,  # pyright: ignore[reportArgumentType] - lifecycle plumbing never touches the spawner
            trace_path=trace_path,
            state_dir=adapter_tmp,
            transports=transports,
            raw_sockets=raw_sockets,
            origin=self.worker_origin(),
            connect_factory=factory,
        )
        self.addCleanup(plumbing.close)
        worker = ZcodeWorker(
            runtime=runtime,
            worker_id=identity.worker_id,
            spawner=spawner,
            close=plumbing.close,
        )
        # The administrator configures the SOURCE (kind
        # zcode_subscription; no model slug anywhere).
        status, payload = self.admin_post(
            "/control/sources",
            {
                "source_id": SOURCE_ID,
                "kind": "zcode_subscription",
                "label": "Z.ai Coding Plan (ZCode)",
                "worker_id": identity.worker_id,
                "entitlement": "subscription_included",
            },
        )
        assert status == 200, payload
        return worker

    def wait_for_lane(self) -> None:
        def fresh() -> bool:
            snapshot = self.plane.current_application().registry.registry_snapshot()
            for entry in snapshot.entries:
                if (
                    entry.identity.resource_id == LANE
                    and entry.freshness == "fresh"
                    and entry.observation is not None
                ):
                    return True
            return False

        wait_until(
            fresh,
            timeout=45,
            message=f"the plan lane {LANE} never materialized fresh",
        )

    @override
    def _last_executed_record(self) -> dict[str, object]:
        records = [
            record
            for record in self.audit_records()
            if record.get("executed_target") is not None
        ]
        assert records, "no executed audit record found"
        return records[-1]


class PlanLaneRoutingAcceptanceTests(ZcodeComposedWorld):
    def test_plan_lane_routes_and_edits_the_authorized_workspace(self) -> None:
        workspace = Path(
            tempfile.mkdtemp(prefix="scarcity-router-zcode-repo-")
        ) / "repo"
        self.addCleanup(lambda: _rmtree(workspace.parent))
        _ = _make_git_repo(workspace)
        scenario: dict[str, object] = {
            "append_line": {"file": "notes.txt", "line": SENTINEL},
            "result": {
                "type": "result",
                "sessionId": "sess-lane-1",
                "response": "added the sentinel line to notes.txt",
                "projection": {"status": "success"},
            },
        }
        worker = self.start_zcode_worker(scenario, workspace)
        worker.start()
        try:
            self.wait_for_lane()
            # A PLAIN logical request — normal selection, no pin, no
            # effort, no special-cased routing.
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "plan-managed",
                    "messages": [
                        {
                            "role": "user",
                            "content": "append the sentinel line to notes.txt",
                        }
                    ],
                },
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            body = cast("dict[str, object]", payload)
            choices = cast("list[object]", body["choices"])
            message = cast(
                "dict[str, object]",
                cast("dict[str, object]", choices[0])["message"],
            )
            self.assertEqual(
                "added the sentinel line to notes.txt", message["content"]
            )
            # The edit landed INSIDE the authorized repository and
            # nowhere else.
            notes = (workspace / "notes.txt").read_text(encoding="utf-8")
            self.assertIn(SENTINEL, notes)
            changed = _git_status(workspace)
            self.assertIn("notes.txt", changed)
            self.assertNotIn("README.md", changed)
            # The run executed in the authorized workspace through the
            # official-CLI argv shape (--cwd == the canonical repo).
            run_specs = worker.spawner.run_specs
            self.assertEqual(len(run_specs), 1)
            argv = list(run_specs[0].argv)
            self.assertEqual(
                argv[argv.index("--cwd") + 1], str(workspace.resolve())
            )
            self.assertEqual(argv[argv.index("--mode") + 1], "edit")
            self.assertEqual(
                argv[argv.index("--output-format") + 1], "stream-json"
            )
            # The router recorded the run honestly against the LANE.
            record = self._last_executed_record()
            self.assertEqual("completed", record["result_status"])
            executed = cast("dict[str, object]", record["executed_target"])
            self.assertEqual(LANE, executed["resource_id"])
            self.assertEqual("plan-managed", executed["model"])
        finally:
            worker.stop()

    def test_effort_bearing_request_never_resolves_onto_the_lane(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="scarcity-router-zcode-repo-")) / "repo"
        self.addCleanup(lambda: _rmtree(workspace.parent))
        _ = _make_git_repo(workspace)
        worker = self.start_zcode_worker({}, workspace)
        worker.start()
        try:
            self.wait_for_lane()
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "plan-managed",
                    "reasoning_effort": "high",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=30,
            )
            # Exact-identity preserved: an effort-bearing request is a
            # typed rejection, never a silent ride onto the lane.
            self.assertEqual(400, status, payload)
            body = cast("dict[str, object]", payload)
            error = cast("dict[str, object]", body.get("error", {}))
            self.assertEqual(
                "unsupported_reasoning_effort", error.get("code")
            )
            self.assertEqual(
                [], [s for s in worker.spawner.run_specs],
                "nothing may execute for a rejected request",
            )
        finally:
            worker.stop()


class PlanLaneCancellationAcceptanceTests(ZcodeComposedWorld):
    def test_admission_deadline_cancels_through_the_path(self) -> None:
        from scarcity_router.gateway_contracts import GatewayLimits

        # A short admission deadline is the router-side cancellation
        # control for the non-streaming lane (disconnect detection is
        # emit-driven; the deadline is the honest bounded stop).
        self.plane._save_config(  # pyright: ignore[reportPrivateUsage] - acceptance seam
            self.plane._updated(  # pyright: ignore[reportPrivateUsage] - acceptance seam
                limits=GatewayLimits(execution_time_limit_seconds=4)
            )
        )
        workspace = Path(tempfile.mkdtemp(prefix="scarcity-router-zcode-repo-")) / "repo"
        self.addCleanup(lambda: _rmtree(workspace.parent))
        _ = _make_git_repo(workspace)
        scenario: dict[str, object] = {"pause_before_result": 30.0}
        worker = self.start_zcode_worker(scenario, workspace)
        worker.start()
        try:
            self.wait_for_lane()
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                {
                    "model": "plan-managed",
                    "messages": [{"role": "user", "content": "stuck task"}],
                },
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            # The stuck run was bounded by the admission deadline and
            # reported honestly as a timeout non-completion.
            self.assertEqual(408, status, payload)
            body = cast("dict[str, object]", payload)
            error = cast("dict[str, object]", body.get("error", {}))
            self.assertEqual("execution_time_limit_exceeded", error.get("code"))
            # The child was terminated and reaped.
            wait_until(
                lambda: bool(
                    worker.spawner.all_reaped() and worker.spawner.run_specs
                ),
                timeout=30,
                message="the zcode child was never reaped after the deadline",
            )
            # The router recorded the bounded stop honestly (the
            # admission-deadline audit status is "timed_out"), against
            # the lane.
            wait_until(
                lambda: any(
                    record.get("result_status") == "timed_out"
                    for record in self.audit_records()
                ),
                timeout=15,
                message="no timed-out audit record after the deadline",
            )
            timed_out = [
                record
                for record in self.audit_records()
                if record.get("result_status") == "timed_out"
            ][-1]
            executed = cast("dict[str, object]", timed_out["executed_target"])
            self.assertEqual(LANE, executed["resource_id"])
        finally:
            worker.stop()


if __name__ == "__main__":
    _ = unittest.main()
