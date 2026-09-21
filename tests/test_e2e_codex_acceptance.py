"""M10-B: Codex end-to-end acceptance suite (issue #95, M06 integration).

Deterministic, synthetic, CI-safe Codex acceptance through the FULL
composed stacks — the OpenAI-compatible client, the real server surfaces,
the real worker protocol and the real ``CodexLocalAdapter`` — with the
shared fake App Server (``tests/codex_fake_appserver.py``) as the ONLY
Codex backend. No test here executes a real Codex binary, contacts any
account or consumes any quota (the structure-only real-binary probe lives
in ``tests/test_codex_real_binary_probe.py`` and skips when absent).

Area mapping (M10-B deliverables 1-13, skipping the real-binary probe):

1.  ``PackagedWorkerPathTests`` — the packaged-style worker path: the
    ``scarcity-router-worker`` console-script pair/run with
    ``--allow-codex`` discovers the fake codex binary on a tmpdir PATH,
    pairs and connects over REAL verified TLS, and the Codex resource
    appears in the server's registry/state and executes end to end.
2.  ``CodexCompositionTests.test_exact_configured_ownership...`` — the
    ``worker_bridged`` resource bound to ``local_adapter_id: "codex"``;
    exact configured ownership (worker B can neither report nor serve
    worker A's Codex resource).
3.  ``CodexCompositionTests.test_auth_verdicts_drive_eligibility...`` —
    honest auth verdicts (``auth_unverified`` → ineligible;
    ``chatgpt`` → eligible) through the full snapshot path.
4.  ``CodexExecutionTests`` (non-streaming, composed server) and
    ``CodexStreamingExecutionTests.test_streaming...`` (streaming) — full
    execution round trips. Streaming uses synthetic compatibility cells
    supplied programmatically, exactly like the M10-A evidenced-streaming
    scenario; the fail-closed default is never weakened.
6.  ``CodexStreamingExecutionTests.test_client_disconnect_interrupts...``
    — client disconnect → coordinator → worker → ``turn/interrupt`` →
    interrupted; never completed-after-cancel.
7.  ``...test_reasoning_effort_forwarded_verbatim`` /
    ``...test_unrepresentable_effort_rejected_before_the_turn``.
8.  ``...test_structured_output_schema_forwarded``.
9.  ``...test_tools_bearing_request_rejected_before_dispatch``.
10. ``CodexWorkerLossTests`` — mid-execution worker loss through the real
    protocol: honest ambiguous semantics, no blind retry, audited.
11. ``CodexExecutionTests`` usage honesty (reported usage lands as
    provider-reported; absent usage stays absent).
12. ``CodexExecutionTests.test_stack_holds_the_leakage_line`` — no token
    reads, minimal child environment, closed wire surface, redacted
    diagnostics/audit.
13. ``CodexStreamingExecutionTests.test_isolation_profile_end_to_end`` —
    controlled ``CODEX_HOME``, scratch cwd under the worker state dir,
    workspaceWrite/networkAccess:false/approvalPolicy never, visible in
    the fake's received requests.

Every secret-like string is a conspicuous throwaway; prompt content is
carried only to the authorized target (the fake) and asserted absent from
every persistence surface.
"""

from __future__ import annotations

import http.client
import json
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override

from scarcity_router.capacity import CapacitySnapshot
from scarcity_router.control_api import ControlPlane
from scarcity_router.eligibility import ExecutionEligibility
from scarcity_router.gateway_adapters import AdapterRegistry
from scarcity_router.gateway_audit import BoundedAuditTrail
from scarcity_router.gateway_contracts import ClientKeyDirectory, GatewayLimits
from scarcity_router.gateway_coordinator import GatewayApplication
from scarcity_router.gateway_server import GatewayHTTPServer, make_gateway_server
from scarcity_router.resource_state import (
    ExecutionCapabilities,
    ResourceHealth,
    ResourceRegistration,
    ResourceRegistry,
    ResourceStateSnapshot,
)
from scarcity_router.routing_core import AdministratorConstraints
from scarcity_router.selector import neutral_selector_policy
from scarcity_router.server_store import ServerStore
from scarcity_router.worker_bridged_adapter import WorkerBridgedAdapter
from scarcity_router.worker_client import (
    ReconnectPolicy,
    WorkerOrigin,
    WorkerRuntime,
)
from scarcity_router.worker_codex_adapter import CONTROLLED_CONFIG_NAME
from scarcity_router.worker_endpoint import WorkerEndpoint
from scarcity_router.worker_identity_store import WorkerIdentityStore
from scarcity_router.worker_local_store import WorkerLocalStore
from scarcity_router.worker_protocol import SocketTransport

from tests.gateway_fixtures import (
    audit_records,
    build_aliases,
    build_profiles,
    make_sequential_request_ids,
)
from tests.m10_codex_fixtures import (
    ADAPTER_ID,
    CLIENT_KEY,
    PIN_MODEL,
    PROMPT,
    REPLY,
    RESOURCE_ID,
    PathShimWorld,
    audit_payloads,
    build_codex_capacity_snapshots,
    build_codex_catalog,
    build_codex_cells,
    canonical_now,
    codex_identity,
    codex_resource_document,
    make_codex_registry,
    start_worker_process,
    stop_worker_process,
    trace_methods,
    trace_request,
    worker_command,
    write_codex_catalog_document,
)
from tests.m10_fixtures import wait_until
from tests.server_fixtures import PBKDF2_TEST_ITERATIONS, synthetic_collectors
from tests.test_e2e_execution import WorkerWorld
from tests.test_worker_codex_adapter import (
    ADAPTER_ALLOWED_WIRE_METHODS,
    FORBIDDEN_WIRE_METHODS,
    FakeCodexSpawner,
)
from tests.worker_fixtures import (
    MutableClock,
    ScriptedWorker,
    build_worker_report,
)

REPO = Path(__file__).resolve().parents[1]


def _rmtree(directory: Path) -> None:
    import shutil

    _ = shutil.rmtree(directory, ignore_errors=True)


# ── Scenario helpers ──────────────────────────────────────────────────────────


def default_turn() -> dict[str, object]:
    return {
        "deltas": ["Hel", "lo"],
        "usage": {"last": {"inputTokens": 11, "outputTokens": 7}},
        "status": "completed",
    }


def codex_model_listing(efforts: tuple[str, ...] = ("minimal", "low", "medium", "high")) -> list[dict[str, object]]:
    """The fake runtime's model listing: the pinned slug with its efforts.

    The real worker path pins the model slug ``codex`` (the resource
    identity the Codex adapter serves), so the fake's listing must carry
    that slug. Tests that exercise the exact-binding rejection start the
    worker with a narrower effort list.
    """
    return [
        {
            "id": "codex",
            "model": "codex",
            "displayName": "Synthetic Codex",
            "hidden": False,
            "isDefault": True,
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort} for effort in efforts
            ],
            "defaultReasoningEffort": efforts[-1],
        }
    ]


def chatgpt_scenario(
    *,
    efforts: tuple[str, ...] = ("minimal", "low", "medium", "high"),
    turn: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "init": "ok",
        "account": "chatgpt",
        "models": codex_model_listing(efforts),
        "turn": dict(turn) if turn is not None else default_turn(),
    }


def _pin_body(**extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": PIN_MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
    }
    body.update(extra)
    return body


# ── The composed world with the real-TLS worker listener ──────────────────────


class CodexWorker:
    """One in-process paired worker running the fake-backed Codex adapter."""

    runtime: WorkerRuntime
    worker_id: str
    store: WorkerLocalStore
    spawner: FakeCodexSpawner
    trace_path: Path
    transports: list[SocketTransport]
    raw_sockets: list[socket.socket]
    origin: WorkerOrigin
    connect_factory: Callable[[WorkerOrigin], SocketTransport]
    session: threading.Thread | None

    def __init__(
        self,
        *,
        runtime: WorkerRuntime,
        worker_id: str,
        store: WorkerLocalStore,
        spawner: FakeCodexSpawner,
        trace_path: Path,
        transports: list[SocketTransport],
        raw_sockets: list[socket.socket],
        origin: WorkerOrigin,
        connect_factory: Callable[[WorkerOrigin], SocketTransport],
    ) -> None:
        self.runtime = runtime
        self.worker_id = worker_id
        self.store = store
        self.spawner = spawner
        self.trace_path = trace_path
        self.transports = transports
        self.raw_sockets = raw_sockets
        self.origin = origin
        self.connect_factory = connect_factory
        self.session = None

    def start(self) -> None:
        self.session = threading.Thread(
            target=self.runtime.run, name="m10b-codex-worker", daemon=True
        )
        self.session.start()

    def stop(self) -> None:
        self.runtime.request_stop()
        if self.session is not None:
            _ = self.session.join(timeout=15)
        self.session = None

    def close(self) -> None:
        self.stop()
        self.store.close()

    def replace_adapter(
        self, scenario: dict[str, object], state_dir: Path, trace_path: Path
    ) -> FakeCodexSpawner:
        """Rebind a fresh Codex adapter (same store, same identity)."""
        registry, spawner = make_codex_registry(
            scenario, state_dir=state_dir, trace_path=trace_path
        )
        self.spawner = spawner
        self.trace_path = trace_path
        self.runtime = WorkerRuntime(
            origin=self.origin,
            store=self.store,
            local_adapters=registry,
            state_report_interval_seconds=0,
            connect_factory=self.connect_factory,
        )
        return spawner


class CodexComposedTlsWorld(WorkerWorld):
    """The composed M09 server + ``WorkerWorld``'s real verified-TLS listener.

    Identical to the M10-A composed harness except ``catalog_path`` points
    at the synthetic (openai, codex) catalog binding: the composed server
    loads the recommendation catalog for admission, and the Codex worker
    resource binds no catalog variant without one (fail-closed
    ``capability_unassessed`` otherwise).
    """

    catalog_path: Path

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.catalog_path = Path(".")

    @override
    def setUp(self) -> None:
        holder = Path(tempfile.mkdtemp(prefix="scarcity-router-m10b-catalog-"))
        self.catalog_path = write_codex_catalog_document(holder / "catalog.json")
        self.addCleanup(lambda: _rmtree(holder))
        super().setUp()
        self.onboard()
        _ = self.attach_worker_listener()

    @override
    def make_plane(self, data_dir: Path) -> ControlPlane:
        from scarcity_router.server_ui import dispatch_ui

        return ControlPlane(
            store=ServerStore.open(data_dir),
            clock=None,  # real clock: real dispatch paths need real deadlines
            collectors=synthetic_collectors(),
            pbkdf2_iterations=PBKDF2_TEST_ITERATIONS,
            version="0.1.0.test",
            own_origins=("http://127.0.0.1:8787",),
            ui_dispatcher=dispatch_ui,
            catalog_path=self.catalog_path,
        )

    def configure_codex_resource(self, worker_id: str) -> None:
        status, payload = self.admin_post(
            "/control/resources", codex_resource_document(worker_id)
        )
        assert status == 200, payload

    def observations(self) -> dict[str, object]:
        """The live observation seam (acceptance read, mirroring M10-A)."""
        return cast(
            "dict[str, object]",
            self.plane._observations,  # pyright: ignore[reportPrivateUsage] - acceptance reads the seam
        )

    def start_codex_worker(
        self,
        scenario: dict[str, object],
        *,
        label: str = "m10b-codex",
    ) -> CodexWorker:
        """Pair one worker and arm it with the fake-backed Codex adapter.

        The worker is REAL (protocol, pairing, state reports, dispatch);
        the Codex adapter's reviewed injectable seams (spawner, path
        lookup) carry the deterministic fake backend.
        """
        store = self.open_worker_store("codex")
        adapter_tmp = Path(tempfile.mkdtemp(prefix="scarcity-router-m10b-adapter-"))
        self.addCleanup(lambda: _rmtree(adapter_tmp))
        trace_path = adapter_tmp / "trace.jsonl"
        registry, spawner = make_codex_registry(
            scenario, state_dir=adapter_tmp, trace_path=trace_path
        )
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": label}
        )
        assert status == 200, payload
        code = cast("dict[str, object]", payload)["pairing_code"]
        transports: list[SocketTransport] = []
        raw_sockets: list[socket.socket] = []

        def factory(origin_ref: WorkerOrigin) -> SocketTransport:
            raw = socket.create_connection(
                (origin_ref.host, origin_ref.port), timeout=10
            )
            try:
                wrapped = self.tls.client_context().wrap_socket(
                    raw, server_hostname=origin_ref.host
                )
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
            state_report_interval_seconds=0,
            connect_factory=factory,
        )
        identity = runtime.pair(str(code))
        # The pairing connection rode the same factory; drop it so the
        # captured lists hold ONLY the live session connection.
        transports.clear()
        raw_sockets.clear()
        worker = CodexWorker(
            runtime=runtime,
            worker_id=identity.worker_id,
            store=store,
            spawner=spawner,
            trace_path=trace_path,
            transports=transports,
            raw_sockets=raw_sockets,
            origin=self.worker_origin(),
            connect_factory=factory,
        )
        self.addCleanup(worker.close)
        return worker

    def observation(self, resource_id: str) -> ResourceStateSnapshot | None:
        found = self.observations().get(resource_id)
        if found is None:
            return None
        return cast(ResourceStateSnapshot, found)

    def observation_status(self, resource_id: str) -> str | None:
        snapshot = self.observation(resource_id)
        return None if snapshot is None else snapshot.health.status

    def wait_for_observation(self, resource_id: str) -> ResourceStateSnapshot:
        wait_until(
            lambda: resource_id in self.observations(),
            timeout=45,
            message=f"worker state report for {resource_id} never landed",
        )
        snapshot = self.observation(resource_id)
        assert snapshot is not None
        return snapshot


# ── Area 1: the packaged-style worker path ────────────────────────────────────


class PackagedWorkerPathTests(CodexComposedTlsWorld):
    """``scarcity-router-worker`` pair/run with ``--allow-codex`` end to end."""

    shim: PathShimWorld
    state_dir: Path

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.shim = cast(PathShimWorld, object())
        self.state_dir = Path(".")

    @override
    def setUp(self) -> None:
        super().setUp()
        self.shim = PathShimWorld(chatgpt_scenario())
        self.addCleanup(self.shim.cleanup)
        self.state_dir = Path(tempfile.mkdtemp(prefix="scarcity-router-m10b-worker-"))
        self.addCleanup(lambda: _rmtree(self.state_dir))

    def _pair(self) -> None:
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "m10b-packaged"}
        )
        self.assertEqual(200, status)
        code = cast("dict[str, object]", payload)["pairing_code"]
        result = subprocess.run(  # noqa: S603 - test-controlled fixed argv
            worker_command(
                [
                    "pair",
                    "--server",
                    f"srws://127.0.0.1:{self.worker_port}",
                    "--code",
                    str(code),
                    "--state-dir",
                    str(self.state_dir),
                ]
            ),
            capture_output=True,
            text=True,
            timeout=120,
            env=self.shim.child_env(ca_file=self.tls.ca_file),
            cwd=str(REPO),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("paired as", result.stdout)

    def test_console_script_worker_discovers_codex_pairs_and_serves(self) -> None:
        self._pair()
        listing = self.plane.worker_admin.list_workers()
        worker_id = next(row.worker_id for row in listing)
        self.configure_codex_resource(worker_id)

        proc = start_worker_process(
            [
                "run",
                "--server",
                f"srws://127.0.0.1:{self.worker_port}",
                "--state-dir",
                str(self.state_dir),
                "--allow-codex",
                "--codex-resource",
                RESOURCE_ID,
            ],
            env=self.shim.child_env(ca_file=self.tls.ca_file),
        )
        self.addCleanup(lambda: stop_worker_process(proc))

        # The Codex resource appears in the server's registry/state with
        # the worker's honest ok snapshot (PATH discovery + version probe
        # + controlled home + chatgpt auth verdict all passed).
        snapshot = self.wait_for_observation(RESOURCE_ID)
        self.assertEqual("ok", snapshot.health.status)

        status, _models, _headers = self.exchange(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {self.client_key}"}
        )
        self.assertEqual(200, status)

        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            _pin_body(),
            headers={"Authorization": f"Bearer {self.client_key}"},
            timeout=60,
        )
        self.assertEqual(200, status, payload)
        document = cast("dict[str, object]", payload)
        choices = cast("list[object]", document["choices"])
        message = cast("dict[str, object]", choices[0])["message"]
        self.assertEqual(REPLY, cast("dict[str, object]", message)["content"])
        # The worker is still serving (no crash, no reconnect exhaustion).
        self.assertIsNone(proc.poll())

        # The fake backend saw exactly one turn, pinned and isolated.
        methods = trace_methods(self.shim.trace_path)
        self.assertEqual(1, methods.count("turn/start"))
        self.assertNotIn("turn/interrupt", methods)
        turn = trace_request(self.shim.trace_path, "turn/start")
        assert turn is not None
        self.assertEqual("codex", turn.get("model"))
        turn_input = cast("list[object]", turn.get("input"))
        first_item = cast("dict[str, object]", turn_input[0])
        self.assertEqual(PROMPT, first_item.get("text"))
        self.assertTrue(
            str(turn.get("cwd")).startswith(str(self.state_dir)),
            "the turn cwd must live under the worker state directory",
        )

        # The controlled CODEX_HOME exists under the worker state dir, is
        # minimal, and no credential material was ever provisioned there.
        home = self.state_dir / "codex" / "codex-home"
        self.assertTrue(home.is_dir())
        self.assertEqual(0o700, stat.S_IMODE(home.stat().st_mode))
        config = home / CONTROLLED_CONFIG_NAME
        self.assertTrue(config.is_file())
        config_text = config.read_text(encoding="utf-8")
        self.assertNotIn("mcp_servers", config_text)
        self.assertFalse((home / "auth.json").exists())

        # Server-side persistence stays free of prompt content.
        for blob in audit_payloads(self.data_dir):
            self.assertNotIn(PROMPT, blob)


# ── Areas 2-3: composition, ownership, eligibility verdicts ───────────────────


class CodexCompositionTests(CodexComposedTlsWorld):
    def test_exact_configured_ownership_for_the_codex_resource(self) -> None:
        """D-049: worker B can neither report nor serve worker A's resource."""
        worker_a = self.start_codex_worker(chatgpt_scenario(), label="owner")
        worker_b = self.start_codex_worker(chatgpt_scenario(), label="other")
        self.configure_codex_resource(worker_a.worker_id)
        worker_a.start()
        worker_b.start()
        try:
            snapshot = self.wait_for_observation(RESOURCE_ID)
            self.assertEqual("ok", snapshot.health.status)

            # Worker B's REAL protocol session (its own adapter claims the
            # same resource id) has its report rejected by the endpoint:
            # configured ownership is the only authority.
            identity_b = worker_b.store.load_identity()
            assert identity_b is not None
            transport = self.connect_transport(self.tls.client_context())
            scripted = ScriptedWorker(transport)
            try:
                hello = scripted.send_hello(
                    identity_b.worker_id, identity_b.credential
                )
                self.assertIsNotNone(hello)
                answer = scripted.send_state_report(
                    build_worker_report(
                        worker_id=identity_b.worker_id,
                        resource_id=RESOURCE_ID,
                        provider="openai",
                        model="codex",
                        entitlement="subscription_included",
                        reported_at=canonical_now(),
                    )
                )
                detail = getattr(answer, "message", None)
                self.assertIsInstance(detail, str)
                assert isinstance(detail, str)
                self.assertIn("not configured for this worker", detail)
            finally:
                transport.close()

            # A pinned execution reaches ONLY the owning worker.
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            methods_a = trace_methods(worker_a.trace_path)
            self.assertIn("thread/start", methods_a)
            self.assertIn("turn/start", methods_a)
            methods_b = trace_methods(worker_b.trace_path)
            self.assertNotIn("thread/start", methods_b)
            self.assertNotIn("turn/start", methods_b)
        finally:
            worker_a.stop()
            worker_b.stop()

    def test_auth_verdicts_drive_eligibility_through_snapshots(self) -> None:
        """auth_unverified → ineligible; chatgpt verdict → eligible."""
        worker = self.start_codex_worker(
            {"init": "ok", "account": "internal-error", "turn": default_turn()}
        )
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            wait_until(
                lambda: self.observation_status(RESOURCE_ID) is not None,
                timeout=45,
                message="the unverified-auth snapshot never landed",
            )
            snapshot = self.observation(RESOURCE_ID)
            assert snapshot is not None
            # The honest verdict: account/read could not establish auth →
            # status unknown with the closed telemetry_unknown diagnostic.
            self.assertEqual("unknown", snapshot.health.status)
            self.assertEqual(1, len(snapshot.health.diagnostics))
            self.assertEqual("telemetry_unknown", snapshot.health.diagnostics[0].code)

            # Admission refuses the unhealthy resource; nothing executes.
            status, _payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(503, status)
            methods = trace_methods(worker.trace_path)
            self.assertNotIn("thread/start", methods)
            self.assertNotIn("turn/start", methods)
        finally:
            worker.stop()

        # Phase 2: the same worker store, a chatgpt auth verdict → the
        # resource reports eligible and executes end to end.
        adapter_tmp = Path(tempfile.mkdtemp(prefix="scarcity-router-m10b-adapter2-"))
        self.addCleanup(lambda: _rmtree(adapter_tmp))
        _ = worker.replace_adapter(
            chatgpt_scenario(),
            state_dir=adapter_tmp,
            trace_path=adapter_tmp / "trace.jsonl",
        )
        worker.start()
        wait_until(
            lambda: self.observation_status(RESOURCE_ID) == "ok",
            timeout=45,
            message="the chatgpt verdict never became an ok snapshot",
        )
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/chat/completions",
            _pin_body(),
            headers={"Authorization": f"Bearer {self.client_key}"},
            timeout=60,
        )
        self.assertEqual(200, status, payload)
        worker.stop()


# ── Areas 4a, 11, 12: composed execution, usage honesty, leakage ──────────────


class CodexExecutionTests(CodexComposedTlsWorld):
    def _completed_records(self) -> list[dict[str, object]]:
        records = [
            cast("dict[str, object]", json.loads(blob))
            for blob in audit_payloads(self.data_dir)
        ]
        executed = [
            record
            for record in records
            if record.get("executed_target") is not None
            and RESOURCE_ID in json.dumps(record.get("executed_target"))
        ]
        assert executed, f"no executed audit record for {RESOURCE_ID}"
        return executed

    def test_nonstreaming_execution_reports_provider_usage_honestly(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            record = self._completed_records()[-1]
            self.assertEqual("completed", record["result_status"])
            self.assertEqual(1, record["call_count"])
            self.assertEqual(
                {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
                record["provider_reported_usage"],
            )
        finally:
            worker.stop()

    def test_absent_provider_usage_stays_absent(self) -> None:
        turn: dict[str, object] = {"deltas": ["Hel", "lo"], "status": "completed"}
        worker = self.start_codex_worker(chatgpt_scenario(turn=turn))
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            record = self._completed_records()[-1]
            self.assertEqual("completed", record["result_status"])
            self.assertIsNone(record["provider_reported_usage"])
            self.assertIsNone(record["estimated_usage"])
            # Never fabricated as zero: the record carries no usage numbers.
            self.assertNotIn("prompt_tokens", json.dumps(record))
        finally:
            worker.stop()

    def test_stack_holds_the_leakage_line(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            status, _payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                # A single-message request: the composed deployment records
                # no compatibility cells (fail-closed, issue #106), so a
                # multi-message conversation would be refused outright.
                _pin_body(),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status)

            # Server persistence: audit, export and diagnostics carry no
            # prompt/response content and no client credential.
            for blob in audit_payloads(self.data_dir):
                self.assertNotIn(PROMPT, blob)
                self.assertNotIn(REPLY, blob)
                self.assertNotIn(self.client_key, blob)
            status, exported, _headers = self.exchange("GET", "/control/export")
            self.assertEqual(200, status)
            export_text = json.dumps(exported)
            self.assertNotIn(PROMPT, export_text)
            self.assertNotIn(REPLY, export_text)
            status, diagnostics, _headers = self.exchange("GET", "/control/diagnostics")
            self.assertEqual(200, status)
            diagnostics_text = json.dumps(diagnostics)
            self.assertNotIn(PROMPT, diagnostics_text)
            self.assertNotIn(REPLY, diagnostics_text)

            # The wire surface stayed inside the reviewed allowlist and
            # never touched a forbidden method.
            methods = trace_methods(worker.trace_path)
            self.assertTrue(methods)
            for method in methods:
                self.assertIn(method, ADAPTER_ALLOWED_WIRE_METHODS)
            for forbidden in FORBIDDEN_WIRE_METHODS:
                self.assertNotIn(forbidden, methods)
            thread_params = trace_request(worker.trace_path, "thread/start")
            assert thread_params is not None
            self.assertLessEqual(
                set(thread_params),
                {
                    "ephemeral",
                    "cwd",
                    "approvalPolicy",
                    "sandbox",
                    "baseInstructions",
                    "developerInstructions",
                },
            )
            turn_params = trace_request(worker.trace_path, "turn/start")
            assert turn_params is not None
            self.assertLessEqual(
                set(turn_params),
                {
                    "threadId",
                    "input",
                    "model",
                    "approvalPolicy",
                    "cwd",
                    "sandboxPolicy",
                    "effort",
                    "outputSchema",
                },
            )

            # The spawned runtime got a minimal, adapter-owned environment.
            # Two sessions ran: the state-report probe and the dispatch.
            wait_until(
                lambda: len(worker.spawner.app_server_specs) >= 2,
                timeout=15,
                message="the probe and dispatch sessions never both ran",
            )
            self.assertEqual(2, len(worker.spawner.app_server_specs))
            spec = worker.spawner.app_server_specs[-1]
            self.assertLessEqual(
                set(spec.env) - {"SR_FAKE_TRACE"}, {"PATH", "HOME", "CODEX_HOME"}
            )

            # Worker diagnostics stay redacted.
            for line in worker.runtime.diagnostics():
                self.assertNotIn(PROMPT, line)
        finally:
            worker.stop()


# ── Area 10: mid-execution worker loss through the real protocol ──────────────


class CodexWorkerLossTests(CodexComposedTlsWorld):
    def test_midexecution_worker_loss_is_ambiguous_and_never_retried(self) -> None:
        turn: dict[str, object] = {
            "deltas": ["Hel", "lo"],
            "pauseBeforeCompleted": 25,
            "status": "completed",
        }
        worker = self.start_codex_worker(chatgpt_scenario(turn=turn))
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            responses: list[tuple[int, object]] = []

            def dispatch() -> None:
                responses.append(
                    self.exchange(
                        "POST",
                        "/v1/chat/completions",
                        _pin_body(),
                        headers={"Authorization": f"Bearer {self.client_key}"},
                        timeout=60,
                    )[:2]
                )

            thread = threading.Thread(target=dispatch, daemon=True)
            thread.start()
            wait_until(
                lambda: "turn/start" in trace_methods(worker.trace_path),
                timeout=30,
                message="the turn never started",
            )
            # The worker connection vanishes mid-turn — the honest
            # ambiguous case (the turn may have consumed quota). The
            # shutdown wakes BOTH sides deterministically: the endpoint
            # resolves the attempt as interrupted, and the worker's own
            # read loop observes the loss and cancels the local attempt.
            lost = worker.raw_sockets[0]
            _ = lost.shutdown(socket.SHUT_RDWR)
            _ = lost.close()
            _ = thread.join(timeout=45)
            self.assertTrue(responses, "the request never completed")
            status, payload = responses[0]
            self.assertEqual(500, status)
            error = cast("dict[str, object]", payload)
            error_body = cast("dict[str, object]", error["error"])
            self.assertEqual("ambiguous_execution_state", error_body["code"])

            # The audit records the ambiguous outcome.
            records = [
                cast("dict[str, object]", json.loads(blob))
                for blob in audit_payloads(self.data_dir)
            ]
            executed = [
                record for record in records if record.get("executed_target") is not None
            ]
            assert executed
            self.assertIn(
                "ambiguous_execution_state",
                cast("list[str]", executed[-1]["reason_codes"]),
            )
            # The bounded cleanup told the Codex turn to stop.
            wait_until(
                lambda: "turn/interrupt" in trace_methods(worker.trace_path),
                timeout=25,
                message="the worker-side attempt was never cancelled",
            )

            # The worker reconnects (bounded backoff) and is NEVER handed
            # the lost attempt again: exactly one turn ever started.
            wait_until(
                lambda: len(worker.transports) >= 2,
                timeout=20,
                message="the worker never reconnected",
            )
            self.assertEqual(1, trace_methods(worker.trace_path).count("turn/start"))
        finally:
            worker.stop()


# ── The bare M03 gateway + evidenced cells + real worker world ────────────────


def _healthy_observation() -> ResourceStateSnapshot:
    return ResourceStateSnapshot(
        schema_version=1,
        identity=codex_identity(),
        observed_at=canonical_now(),
        health=ResourceHealth(status="ok", diagnostics=()),
        quota_facts=(),
        promotions=(),
    )


class BareCodexWorld(unittest.TestCase):
    """Bare M03 gateway + synthetic cells + real worker endpoint/runtime.

    The one synthetic input is the compatibility-matrix evidence, supplied
    programmatically exactly like the M10-A evidenced-streaming scenario
    (the composed control plane hard-wires the fail-closed empty cell set;
    issue #106 records the administrative gap). Everything else is real:
    the HTTP surface, the coordinator, the WorkerBridgedAdapter, the
    worker endpoint, the worker runtime and the CodexLocalAdapter.
    """

    tmp: Path
    identity_store: WorkerIdentityStore
    endpoint: WorkerEndpoint
    application: GatewayApplication
    server: GatewayHTTPServer
    server_thread: threading.Thread
    http_port: int
    worker_port: int
    assignment: dict[str, str | None]
    worker_store: WorkerLocalStore
    runtime: WorkerRuntime
    worker_session: threading.Thread | None
    spawner: FakeCodexSpawner
    trace_path: Path
    transports: list[SocketTransport]
    raw_sockets: list[socket.socket]

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.tmp = Path(".")
        self.identity_store = cast(WorkerIdentityStore, object())
        self.endpoint = cast(WorkerEndpoint, object())
        self.application = cast(GatewayApplication, object())
        self.server = cast(GatewayHTTPServer, object())
        self.server_thread = cast(threading.Thread, object())
        self.http_port = 0
        self.worker_port = 0
        self.assignment = {}
        self.worker_store = cast(WorkerLocalStore, object())
        self.runtime = cast(WorkerRuntime, object())
        self.worker_session = None
        self.spawner = cast(FakeCodexSpawner, object())
        self.trace_path = Path(".")
        self.transports = []
        self.raw_sockets = []

    def _fresh_registry(self) -> ResourceRegistry:
        registry = ResourceRegistry(clock=canonical_now)
        registry.register(
            ResourceRegistration(
                identity=codex_identity(),
                freshness_ttl_seconds=300,
                capabilities=ExecutionCapabilities(context_limit_tokens=272_000),
            )
        )
        return registry

    @staticmethod
    def _capacity_source(
        now: str,
    ) -> tuple[tuple[CapacitySnapshot, ...], tuple[ExecutionEligibility, ...]]:
        _ = now
        return (build_codex_capacity_snapshots(), ())

    def _connect_factory(self, origin_ref: WorkerOrigin) -> SocketTransport:
        _ = origin_ref
        raw = socket.create_connection(("127.0.0.1", self.worker_port), timeout=10)
        self.raw_sockets.append(raw)
        transport = SocketTransport(raw)
        self.transports.append(transport)
        return transport

    @override
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="scarcity-router-m10b-bare-"))
        self.addCleanup(lambda: _rmtree(self.tmp))
        self.assignment = {}
        self.transports = []
        self.raw_sockets = []
        self.identity_store = WorkerIdentityStore(
            self.tmp / "identities.db", clock=MutableClock()
        )
        self.addCleanup(self.identity_store.close)
        self.endpoint = WorkerEndpoint(
            identity_store=self.identity_store,
            registry=self._fresh_registry(),
            configured_owner=self.assignment.get,
            heartbeat_interval_seconds=15,
        )
        app_registry = self._fresh_registry()
        app_registry.apply_snapshot(_healthy_observation())
        adapters = AdapterRegistry()
        adapters.register(
            WorkerBridgedAdapter(
                self.endpoint, resource_adapter_map={RESOURCE_ID: ADAPTER_ID}
            )
        )
        self.application = GatewayApplication(
            catalog=build_codex_catalog(),
            profiles=build_profiles(),
            profile_policy_version=1,
            policy=neutral_selector_policy(),
            registry=app_registry,
            capacity_source=self._capacity_source,
            compatibility_cells=build_codex_cells(),
            admin_constraints=AdministratorConstraints(),
            aliases=build_aliases(),
            adapters=adapters,
            audit=BoundedAuditTrail(),
            limits=GatewayLimits(),
            client_key_directory=ClientKeyDirectory.from_secrets(
                {"e2e-client": CLIENT_KEY}
            ),
            clock=lambda: datetime.now(timezone.utc),
            request_id_factory=make_sequential_request_ids(),
        )
        self.server = make_gateway_server(self.application, host="127.0.0.1", port=0)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.addCleanup(self.server.server_close)
        self.http_port = cast("tuple[str, int]", self.server.server_address)[1]
        listener = self.endpoint.attach_listener(
            host="127.0.0.1", port=0, tls_context=None
        )
        listener.serve_in_background()
        self.addCleanup(listener.shutdown)
        self.worker_port = listener.bound_port
        self.worker_store = WorkerLocalStore(self.tmp / "worker-state.db")
        self.worker_session = None

    @override
    def tearDown(self) -> None:
        self.endpoint.close_all_sessions(note="m10b teardown")
        if self.worker_session is not None:
            self.runtime.request_stop()
            _ = self.worker_session.join(timeout=15)
        self.worker_store.close()
        self.server.shutdown()
        _ = self.server_thread.join(timeout=10)

    def start_codex_worker(self, scenario: dict[str, object]) -> None:
        registry, spawner = make_codex_registry(
            scenario,
            state_dir=self.tmp / "codex-adapter-state",
            trace_path=self.tmp / "codex-trace.jsonl",
        )
        self.trace_path = self.tmp / "codex-trace.jsonl"
        pairing = self.identity_store.begin_pairing(label="m10b-bare")
        runtime = WorkerRuntime(
            origin=WorkerOrigin.parse(f"srw://127.0.0.1:{self.worker_port}"),
            store=self.worker_store,
            local_adapters=registry,
            state_report_interval_seconds=0,
            connect_factory=self._connect_factory,
            reconnect=ReconnectPolicy(
                initial_delay_seconds=0.2, max_delay_seconds=1.0, max_attempts=5
            ),
        )
        identity = runtime.pair(pairing.pairing_code)
        self.assignment[RESOURCE_ID] = identity.worker_id
        self.spawner = spawner
        self.runtime = runtime
        self.worker_session = threading.Thread(
            target=runtime.run, name="m10b-bare-worker", daemon=True
        )
        self.worker_session.start()
        # Availability evidence: dispatch requires the configured owner's
        # own valid report (the endpoint refuses an unreported resource).
        wait_until(
            lambda: self.endpoint._observed_bindings.get(RESOURCE_ID)  # pyright: ignore[reportPrivateUsage] - acceptance reads the seam
            == identity.worker_id,
            timeout=30,
            message="the worker's initial state report never landed",
        )

    # -- HTTP helpers ------------------------------------------------------

    def post_json(self, payload: dict[str, object], *, timeout: float = 60.0) -> tuple[int, object]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.http_port, timeout=timeout
        )
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {CLIENT_KEY}",
                },
            )
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        finally:
            connection.close()
        parsed: object = json.loads(raw.decode("utf-8")) if raw else None
        return status, parsed

    def post_stream(self, payload: dict[str, object], *, timeout: float = 60.0) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.http_port, timeout=timeout
        )
        try:
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {CLIENT_KEY}",
                },
            )
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        finally:
            connection.close()
        return status, raw

    def open_raw_stream(self, payload: dict[str, object]) -> socket.socket:
        body = json.dumps(payload).encode("utf-8")
        raw = socket.create_connection(("127.0.0.1", self.http_port), timeout=30)
        request = (
            "POST /v1/chat/completions HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            f"Authorization: Bearer {CLIENT_KEY}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        _ = raw.sendall(request + body)
        return raw

    @staticmethod
    def read_until_headers(sock: socket.socket) -> bytes:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            piece = sock.recv(4096)
            if not piece:
                break
            buffer += piece
        return buffer


# ── Areas 4b, 6-9, 13: streaming execution semantics ──────────────────────────


class CodexStreamingExecutionTests(BareCodexWorld):
    def test_streaming_roles_and_history_end_to_end(self) -> None:
        """Full SSE round trip: roles map, history is injected, deltas flow."""
        self.start_codex_worker(chatgpt_scenario())
        status, raw = self.post_stream(
            _pin_body(
                messages=[
                    {"role": "system", "content": "SYS-M10B-system-text"},
                    {"role": "user", "content": "earlier question"},
                    {"role": "assistant", "content": "earlier answer"},
                    {"role": "user", "content": PROMPT},
                ],
                stream=True,
            )
        )
        self.assertEqual(200, status)
        text = raw.decode("utf-8")
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))
        # Parse the SSE frames. The honest surface shape for a stream the
        # adapter delivered as text deltas: the role frame first, the
        # deltas assembling the fake's message, then [DONE] — the explicit
        # finish_reason frame appears only when the adapter itself emits a
        # finish chunk (the Codex adapter does not; recorded in the
        # acceptance doc).
        frames = [
            cast("dict[str, object]", json.loads(line[len("data: ") :]))
            for line in text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        self.assertGreaterEqual(len(frames), 3)
        content = ""
        for frame in frames:
            for choice in cast("list[object]", frame.get("choices", [])):
                choice_map = cast("dict[str, object]", choice)
                delta = cast("dict[str, object]", choice_map.get("delta", {}))
                piece = delta.get("content")
                if piece:
                    content += str(piece)
                self.assertIsNone(choice_map.get("finish_reason"))
        self.assertEqual(REPLY, content)

        thread_params = trace_request(self.trace_path, "thread/start")
        assert thread_params is not None
        self.assertEqual("SYS-M10B-system-text", thread_params.get("baseInstructions"))
        inject = trace_request(self.trace_path, "thread/inject_items")
        assert inject is not None
        items = cast("list[object]", inject.get("items"))
        roles = [cast("dict[str, object]", item).get("role") for item in items]
        self.assertEqual(["user", "assistant"], roles)
        turn = trace_request(self.trace_path, "turn/start")
        assert turn is not None
        turn_input = cast("list[object]", turn.get("input"))
        first = cast("dict[str, object]", turn_input[0])
        self.assertEqual(PROMPT, first.get("text"))

        records = audit_records(self.application)
        self.assertEqual(1, len(records))
        self.assertEqual("completed", records[0].result_status)

    def test_client_disconnect_interrupts_the_codex_turn(self) -> None:
        """Client disconnect propagates: turn/interrupt, never completed.

        The fake keeps the turn open with output pending after the client's
        FIN (inter-delta pauses plus a long pre-completion pause), so the
        gateway's EOF check fires on the next pending chunk while the turn
        is still in progress — never after the backend already completed.
        """
        turn: dict[str, object] = {
            "deltas": ["part1", "part2", "part3"],
            "pauseBeforeDelta": 3.0,
            "pauseBeforeCompleted": 10.0,
            "status": "completed",
        }
        self.start_codex_worker(chatgpt_scenario(turn=turn))
        raw = self.open_raw_stream(_pin_body(stream=True))
        try:
            wait_until(
                lambda: "turn/start" in trace_methods(self.trace_path),
                timeout=30,
                message="the turn never started",
            )
            _ = raw.settimeout(60.0)
            head = self.read_until_headers(raw)
            self.assertIn(" 200 ", head.decode("ascii", errors="replace"))
            # Drain whatever frames already rode with the headers (bounded):
            # the FIN then lands while the fake still holds pending output.
            _ = raw.settimeout(5.0)
            try:
                _ = raw.recv(4096)
            except (TimeoutError, OSError):
                pass
            # The client disconnects: a clean FIN on the request socket.
            _ = raw.shutdown(socket.SHUT_WR)
            # The next pending chunk drives the gateway's EOF check; the
            # cancel must propagate all the way to turn/interrupt while
            # the turn is still open.
            wait_until(
                lambda: "turn/interrupt" in trace_methods(self.trace_path),
                timeout=25,
                message="cancel never reached the codex turn",
            )
            methods = trace_methods(self.trace_path)
            self.assertEqual(1, methods.count("turn/start"))
            self.assertEqual(1, methods.count("turn/interrupt"))
        finally:
            _ = raw.close()

        # The outcome was reported honestly: cancelled (client gone), and
        # no completion was ever produced after the cancellation.
        records = audit_records(self.application)
        self.assertEqual(1, len(records))
        self.assertEqual("cancelled", records[0].result_status)
        self.assertIn("client_disconnected", records[0].reason_codes)
        # Exactly two app-server sessions ran: the state-report probe and
        # the dispatch — no third session, no second execution.
        wait_until(
            lambda: len(self.spawner.app_server_specs) >= 2,
            timeout=15,
            message="the probe and dispatch sessions never both ran",
        )
        self.assertEqual(2, len(self.spawner.app_server_specs))

    def test_reasoning_effort_forwarded_verbatim(self) -> None:
        self.start_codex_worker(chatgpt_scenario())
        status, payload = self.post_json(_pin_body(reasoning_effort="high"))
        self.assertEqual(200, status, payload)
        turn = trace_request(self.trace_path, "turn/start")
        assert turn is not None
        self.assertEqual("codex", turn.get("model"))
        self.assertEqual("high", turn.get("effort"))

    def test_unrepresentable_effort_rejected_before_the_turn(self) -> None:
        # The fake runtime's listing deliberately omits "minimal": the
        # surface-valid effort is unrepresentable for THIS runtime, so the
        # adapter's exact-binding check must reject it before the turn.
        self.start_codex_worker(chatgpt_scenario(efforts=("low", "medium", "high")))
        status, payload = self.post_json(_pin_body(reasoning_effort="minimal"))
        self.assertEqual(502, status)
        error_body = cast("dict[str, object]", cast("dict[str, object]", payload)["error"])
        self.assertEqual("backend_failure", error_body["code"])
        methods = trace_methods(self.trace_path)
        # The binding verdict needs the runtime's own model/list, but the
        # TURN never starts: no thread, no turn, nothing executed.
        self.assertIn("model/list", methods)
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("turn/start", methods)
        records = audit_records(self.application)
        self.assertEqual(1, len(records))
        self.assertEqual("failed", records[0].result_status)

    def test_structured_output_schema_forwarded(self) -> None:
        self.start_codex_worker(chatgpt_scenario())
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        }
        status, payload = self.post_json(
            _pin_body(
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "answer", "schema": schema, "strict": True},
                }
            )
        )
        self.assertEqual(200, status, payload)
        turn = trace_request(self.trace_path, "turn/start")
        assert turn is not None
        self.assertEqual(schema, turn.get("outputSchema"))

    def test_tools_bearing_request_rejected_before_dispatch(self) -> None:
        self.start_codex_worker(chatgpt_scenario())
        status, payload = self.post_json(
            _pin_body(
                tools=cast(
                    "list[dict[str, object]]",
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": "synthetic_lookup",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                )
            )
        )
        self.assertEqual(400, status)
        error_body = cast("dict[str, object]", cast("dict[str, object]", payload)["error"])
        self.assertEqual("compatibility_unsupported", error_body["code"])
        # Rejected at admission: no execute message, no dispatch session,
        # no turn — only the initial state-report probe ever runs.
        wait_until(
            lambda: len(self.spawner.app_server_specs) >= 1,
            timeout=15,
            message="the state-report probe session never ran",
        )
        methods = trace_methods(self.trace_path)
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("turn/start", methods)
        self.assertEqual(1, len(self.spawner.app_server_specs))
        records = audit_records(self.application)
        self.assertEqual(1, len(records))
        self.assertEqual("rejected", records[0].result_status)
        self.assertIn("compatibility_unsupported", records[0].reason_codes)

    def test_isolation_profile_end_to_end(self) -> None:
        self.start_codex_worker(chatgpt_scenario())
        status, payload = self.post_json(
            _pin_body(
                messages=[
                    {"role": "system", "content": "SYS-TEXT"},
                    {"role": "developer", "content": "DEV-TEXT"},
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "second"},
                    {"role": "user", "content": PROMPT},
                ]
            )
        )
        self.assertEqual(200, status, payload)

        state_dir = self.tmp / "codex-adapter-state"
        home = state_dir / "codex" / "codex-home"
        # The controlled home: 0o700, minimal generated config, no MCP
        # servers, no credential material.
        self.assertEqual(0o700, stat.S_IMODE(home.stat().st_mode))
        config = (home / CONTROLLED_CONFIG_NAME).read_text(encoding="utf-8")
        self.assertNotIn("mcp_servers", config)
        self.assertFalse((home / "auth.json").exists())

        # The spawn spec is fully adapter-owned: minimal env, controlled
        # home, scratch cwd under the worker state dir.
        self.assertEqual(2, len(self.spawner.app_server_specs))
        spec = self.spawner.app_server_specs[-1]
        self.assertLessEqual(
            set(spec.env) - {"SR_FAKE_TRACE"}, {"PATH", "HOME", "CODEX_HOME"}
        )
        self.assertEqual(str(home), spec.env["CODEX_HOME"])
        scratch_root = state_dir / "codex" / "scratch"
        self.assertTrue(str(spec.cwd).startswith(str(scratch_root)))

        # The protocol requests carry the composed isolation policy.
        thread_params = trace_request(self.trace_path, "thread/start")
        assert thread_params is not None
        self.assertIs(True, thread_params.get("ephemeral"))
        self.assertEqual("never", thread_params.get("approvalPolicy"))
        self.assertEqual("workspace-write", thread_params.get("sandbox"))
        self.assertEqual("SYS-TEXT", thread_params.get("baseInstructions"))
        self.assertEqual("DEV-TEXT", thread_params.get("developerInstructions"))
        scratch = thread_params.get("cwd")
        self.assertTrue(str(scratch).startswith(str(scratch_root)))

        turn_params = trace_request(self.trace_path, "turn/start")
        assert turn_params is not None
        self.assertEqual("never", turn_params.get("approvalPolicy"))
        self.assertEqual(scratch, turn_params.get("cwd"))
        self.assertEqual(
            {
                "type": "workspaceWrite",
                "writableRoots": [scratch],
                "networkAccess": False,
            },
            turn_params.get("sandboxPolicy"),
        )
        self.assertNotIn("effort", turn_params)
        self.assertNotIn("outputSchema", turn_params)

        # The scratch directory is cleaned up after the call.
        self.assertEqual([], list(scratch_root.iterdir()))


if __name__ == "__main__":
    _ = unittest.main()
