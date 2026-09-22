"""M10-B: Codex end-to-end acceptance suite (issue #95, M06 integration).

Deterministic, synthetic, CI-safe Codex acceptance through the FULL
composed stacks — the OpenAI-compatible client, the real server surfaces,
the real worker protocol and the real ``CodexLocalAdapter`` — with the
shared fake App Server (``tests/codex_fake_appserver.py``) as the ONLY
Codex backend. No test here executes a real Codex binary, contacts any
account or consumes any quota (the structure-only real-binary probe lives
in ``tests/test_codex_real_binary_probe.py`` and skips when absent).

Identity and matrix discipline (D-042/D-043, integration blockers 1+2):

- the Codex resource binds the SHIPPED recommendation catalog through its
  PHYSICAL model identity (``openai`` / ``gpt-5.6-sol``; ``codex`` is the
  execution surface ``local_adapter_id``, never a model). No synthetic
  catalog entry and no synthetic compatibility cell exists anywhere; the
  composed server's matrix is the production one — M04 preset evidence
  plus the reviewed M06 Codex worker-local evidence cells built by
  ``server_composition`` from administrator configuration (issue #106);
- the fake App Server advertises the REAL physical slug with the shipped
  catalog's calibrated efforts, and every execution pins exactly the
  selected physical model + effort — a configured-model/selected-model
  mismatch is rejected before anything runs.

Area mapping (M10-B deliverables 1-13, skipping the real-binary probe):

1.  ``PackagedWorkerPathTests`` — the packaged-style worker path: the
    ``scarcity-router-worker`` console-script pair/run with
    ``--allow-codex --codex-model`` discovers the fake codex binary on a
    tmpdir PATH, pairs and connects over REAL verified TLS, and the Codex
    resource appears in the server's registry/state and executes end to end.
2.  ``CodexCompositionTests.test_exact_configured_ownership...`` — the
    ``worker_bridged`` resource bound to ``local_adapter_id: "codex"``;
    exact configured ownership (worker B can neither report nor serve
    worker A's Codex resource).
3.  ``CodexCompositionTests.test_auth_verdicts_drive_eligibility...`` —
    honest auth verdicts (``auth_unverified`` → ineligible;
    ``chatgpt`` → eligible) through the full snapshot path.
4.  ``CodexExecutionTests`` (non-streaming) and
    ``CodexStreamingExecutionTests`` (streaming) — full execution round
    trips through the ACTUAL composed server with NO synthetic cell
    injection anywhere: the production matrix (built-in evidence) serves
    the gates.
6.  ``CodexStreamingExecutionTests.test_client_disconnect_interrupts...``
    — client disconnect → coordinator → worker → ``turn/interrupt`` →
    interrupted; never completed-after-cancel.
7.  ``...test_reasoning_effort_forwarded_verbatim`` /
    ``...test_unrepresentable_effort_rejected_before_the_turn``.
8.  ``...test_structured_output_schema_forwarded``.
9.  ``...test_tools_bearing_request_rejected_before_dispatch`` (the real
    M06 ``UNSUPPORTED`` cell) and
    ``PhysicalModelIdentityTests`` (physical model binding, exact
    pinned identity, configured/selected mismatch rejection before any
    spawn, worker CLI ``--codex-model`` discipline).
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

import json
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast, override

from scarcity_router.codex_worker_evidence import CODEX_WORKER_CELL_VALUES
from scarcity_router.routing_core import CompatibilityCell
from scarcity_router.resource_state import ResourceStateSnapshot
from scarcity_router.worker_client import (
    SESSION_IO_TIMEOUT_SECONDS,
    WorkerOrigin,
    WorkerRuntime,
)
from scarcity_router.worker_codex_adapter import CONTROLLED_CONFIG_NAME
from scarcity_router.worker_local_store import WorkerLocalStore
from scarcity_router.worker_protocol import SocketTransport

from tests.m10_codex_fixtures import (
    CODEX_CATALOG_EFFORTS,
    CODEX_CATALOG_MODEL,
    PIN_MODEL,
    PROMPT,
    REPLY,
    RESOURCE_ID,
    PathShimWorld,
    audit_payloads,
    canonical_now,
    codex_resource_document,
    make_codex_registry,
    start_worker_process,
    stop_worker_process,
    trace_methods,
    trace_request,
    worker_command,
)
from tests.m10_fixtures import wait_until
from tests.server_fixtures import FAKE_PROVIDER_SECRET
from tests.test_e2e_execution import WorkerWorld
from tests.test_worker_codex_adapter import (
    ADAPTER_ALLOWED_WIRE_METHODS,
    FORBIDDEN_WIRE_METHODS,
    FakeCodexSpawner,
)
from tests.worker_fixtures import ScriptedWorker, build_worker_report

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


def codex_model_listing(
    efforts: tuple[str, ...] = CODEX_CATALOG_EFFORTS,
) -> list[dict[str, object]]:
    """The fake runtime's model listing: the REAL physical slug.

    The advertised slug is the shipped catalog's ``gpt-5.6-sol`` with the
    shipped CALIBRATED efforts (never the execution-surface name and
    never invented efforts): the adapter's exact-binding verification
    against ``model/list`` therefore proves the configured physical model
    is representable by THIS runtime.
    """
    return [
        {
            "id": CODEX_CATALOG_MODEL,
            "model": CODEX_CATALOG_MODEL,
            "displayName": "Synthetic",
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
    efforts: tuple[str, ...] = CODEX_CATALOG_EFFORTS,
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
    state_dir: Path
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
        state_dir: Path,
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
        self.state_dir = state_dir
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
        # Deterministic socket ownership: the run loop closed its live
        # transport on unwind; close every captured transport/raw socket
        # here (idempotent) so nothing waits for GC.
        for transport in self.transports:
            transport.close()
        self.transports.clear()
        for raw in self.raw_sockets:
            try:
                raw.close()
            except OSError:
                pass
        self.raw_sockets.clear()
        self.store.close()

    def replace_adapter(
        self,
        scenario: dict[str, object],
        state_dir: Path,
        trace_path: Path,
        *,
        model: str = CODEX_CATALOG_MODEL,
    ) -> FakeCodexSpawner:
        """Rebind a fresh Codex adapter (same store, given identity)."""
        registry, spawner = make_codex_registry(
            scenario, state_dir=state_dir, trace_path=trace_path, model=model
        )
        self.spawner = spawner
        self.trace_path = trace_path
        self.state_dir = state_dir
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

    The application is the production composition: the SHIPPED
    recommendation catalog (the default control-plane artifact) binds the
    Codex resource through its physical model, and the compatibility
    matrix is the production one (``server_composition`` cells — M04
    preset evidence plus the reviewed M06 Codex cells). No synthetic
    catalog entry, no synthetic cells, no cell injection seam.
    """

    @override
    def setUp(self) -> None:
        super().setUp()
        self.onboard()
        _ = self.attach_worker_listener()

    def configure_codex_resource(
        self, worker_id: str, *, model: str = CODEX_CATALOG_MODEL
    ) -> None:
        status, payload = self.admin_post(
            "/control/resources", codex_resource_document(worker_id, model=model)
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
        model: str = CODEX_CATALOG_MODEL,
        resource_id: str = RESOURCE_ID,
    ) -> CodexWorker:
        """Pair one worker and arm it with the fake-backed Codex adapter.

        The worker is REAL (protocol, pairing, state reports, dispatch);
        the Codex adapter's reviewed injectable seams (spawner, path
        lookup) carry the deterministic fake backend. ``resource_id``
        names the resource the worker's own adapter claims (distinct
        workers may each own a resource for the SAME physical model,
        D-042).
        """
        store = self.open_worker_store("codex")
        adapter_tmp = Path(tempfile.mkdtemp(prefix="scarcity-router-m10b-adapter-"))
        self.addCleanup(lambda: _rmtree(adapter_tmp))
        trace_path = adapter_tmp / "trace.jsonl"
        registry, spawner = make_codex_registry(
            scenario,
            state_dir=adapter_tmp,
            trace_path=trace_path,
            model=model,
            resource_id=resource_id,
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
                # Mirror the production factory: the session socket gets
                # the protocol-sane I/O ceiling, never the connect timeout.
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
            state_dir=adapter_tmp,
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

    def audit_records(self) -> list[dict[str, object]]:
        """Every audit record persisted by the composed server."""
        records: list[dict[str, object]] = []
        for blob in audit_payloads(self.data_dir):
            record = cast("dict[str, object]", json.loads(blob))
            records.append(record)
        return records

    def executed_audit_records(self) -> list[dict[str, object]]:
        records = [
            record
            for record in self.audit_records()
            if record.get("executed_target") is not None
            and RESOURCE_ID in json.dumps(record.get("executed_target"))
        ]
        assert records, f"no executed audit record for {RESOURCE_ID}"
        return records


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
                    f"--code={code}",
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
                "--codex-model",
                CODEX_CATALOG_MODEL,
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
        # The reported identity carries the PHYSICAL model (the
        # ``--codex-model`` value), never the execution-surface name.
        self.assertEqual(CODEX_CATALOG_MODEL, snapshot.identity.model)
        self.assertEqual("openai", snapshot.identity.provider)
        self.assertIsNone(snapshot.identity.variant)

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
        if status != 200:
            # Failure evidence, not a weaker assertion: the closed
            # worker-side failure reason is only visible in the worker
            # process's own diagnostics, so stop it (idempotent) and
            # surface its stderr with the assertion.
            _out, err = stop_worker_process(proc)
            self.fail(
                f"expected 200, received {status}: {payload}; "
                + "worker stderr tail: "
                + err.decode(encoding="utf-8", errors="replace")[-2000:]
            )
        self.assertEqual(200, status, payload)
        document = cast("dict[str, object]", payload)
        choices = cast("list[object]", document["choices"])
        message = cast("dict[str, object]", choices[0])["message"]
        self.assertEqual(REPLY, cast("dict[str, object]", message)["content"])
        # The worker is still serving (no crash, no reconnect exhaustion).
        self.assertIsNone(proc.poll())

        # The fake backend saw exactly one turn, pinned to the CONFIGURED
        # physical model and isolated.
        methods = trace_methods(self.shim.trace_path)
        self.assertEqual(1, methods.count("turn/start"))
        self.assertNotIn("turn/interrupt", methods)
        turn = trace_request(self.shim.trace_path, "turn/start")
        assert turn is not None
        self.assertEqual(CODEX_CATALOG_MODEL, turn.get("model"))
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
                        model=CODEX_CATALOG_MODEL,
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


# ── Matrix canonicalization: two resources, one physical model (D-042) ─────────


SECOND_CODEX_RESOURCE_ID = "codex-e2e-b"


def _second_codex_resource_document(worker_id: str) -> dict[str, object]:
    """The second Codex resource: SAME physical model, different owner."""
    document = codex_resource_document(worker_id)
    registration = cast("dict[str, object]", document["registration"])
    identity = cast("dict[str, object]", registration["identity"])
    identity["resource_id"] = SECOND_CODEX_RESOURCE_ID
    return document


def _pin_body_for(resource_id: str, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": f"sr-pin:{resource_id}/openai/{CODEX_CATALOG_MODEL}/high",
        "messages": [{"role": "user", "content": PROMPT}],
    }
    body.update(extra)
    return body


class TwoCodexResourcesSamePhysicalModelTests(CodexComposedTlsWorld):
    """D-042 through the composed server: two resources, ONE backend.

    Two workers each own a configured resource, and both resources
    represent the SAME physical model (``openai``/``gpt-5.6-sol``). The
    D-043 matrix is keyed by the frozen backend key — never
    ``resource_id`` — so the composition canonicalizes the per-resource
    emission into ONE set of reviewed M06 cells, a real ``RouteRequest``
    accepts the matrix (no duplicate-cell rejection), and routing stays
    resource-distinct (each pin reaches only its owning worker).
    """

    def _configure_two_resources(self) -> tuple[CodexWorker, CodexWorker]:
        worker_a = self.start_codex_worker(chatgpt_scenario(), label="codex-a")
        worker_b = self.start_codex_worker(
            chatgpt_scenario(),
            label="codex-b",
            resource_id=SECOND_CODEX_RESOURCE_ID,
        )
        self.assertNotEqual(worker_a.worker_id, worker_b.worker_id)
        self.configure_codex_resource(worker_a.worker_id)
        status, payload = self.admin_post(
            "/control/resources",
            _second_codex_resource_document(worker_b.worker_id),
        )
        assert status == 200, payload
        return worker_a, worker_b

    def application_cells(self) -> tuple[CompatibilityCell, ...]:
        return self.plane.current_application().compatibility_cells

    def test_matrix_holds_exactly_one_cell_set_per_backend_key(self) -> None:
        _worker_a, _worker_b = self._configure_two_resources()
        cells = self.application_cells()
        keys = [
            (cell.channel, cell.provider, cell.model, cell.variant, cell.feature)
            for cell in cells
        ]
        self.assertEqual(
            len(keys), len(set(keys)), "duplicate cell for one frozen D-043 key"
        )
        # The two Codex resources contribute exactly ONE reviewed M06 set.
        codex_cells = [
            cell
            for cell in cells
            if (cell.channel, cell.provider, cell.model)
            == ("worker_bridged", "openai", CODEX_CATALOG_MODEL)
        ]
        self.assertEqual(len(CODEX_WORKER_CELL_VALUES), len(codex_cells))
        self.assertEqual(
            {
                feature: value
                for feature, (value, _note) in CODEX_WORKER_CELL_VALUES.items()
            },
            {cell.feature: cell.value for cell in codex_cells},
        )

    def test_a_real_route_request_accepts_the_deduplicated_matrix(self) -> None:
        """The coordinator's RouteRequest takes the composed matrix as-is.

        With duplicate cells for one frozen key this dispatch dies INSIDE
        RouteRequest validation (the duplicate-cell rejection surfaces as
        an ``invalid_state`` 500); with the canonicalized matrix the
        pinned execution serves end to end.
        """
        worker_a, worker_b = self._configure_two_resources()
        worker_a.start()
        worker_b.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            _ = self.wait_for_observation(SECOND_CODEX_RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body_for(RESOURCE_ID),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            self.assertIn("turn/start", trace_methods(worker_a.trace_path))
        finally:
            worker_a.stop()
            worker_b.stop()

    def test_both_resources_stay_independent_routing_targets(self) -> None:
        """Dedup never collapses resources: each pin reaches its owner."""
        worker_a, worker_b = self._configure_two_resources()
        worker_a.start()
        worker_b.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            _ = self.wait_for_observation(SECOND_CODEX_RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body_for(RESOURCE_ID),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body_for(SECOND_CODEX_RESOURCE_ID),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            # Exactly one turn per worker — ownership stayed configured:
            # worker B served B's resource, never A's, and vice versa.
            self.assertEqual(1, trace_methods(worker_a.trace_path).count("turn/start"))
            self.assertEqual(1, trace_methods(worker_b.trace_path).count("turn/start"))
            turn_a = trace_request(worker_a.trace_path, "turn/start")
            turn_b = trace_request(worker_b.trace_path, "turn/start")
            assert turn_a is not None and turn_b is not None
            self.assertEqual(CODEX_CATALOG_MODEL, turn_a.get("model"))
            self.assertEqual(CODEX_CATALOG_MODEL, turn_b.get("model"))
        finally:
            worker_a.stop()
            worker_b.stop()


# ── Blocker 1: physical model identity end to end ─────────────────────────────


class PhysicalModelIdentityTests(CodexComposedTlsWorld):
    """The Codex resource binds the SHIPPED catalog by physical model.

    ``(openai, gpt-5.6-sol)`` — the administrator never invents a catalog
    entry; the pinned reference resolves to the shipped calibrated
    identity; the adapter receives exactly the selected model; and a
    configured/selected mismatch fails before anything runs.
    """

    def test_pinned_request_binds_shipped_catalog_and_pins_exact_identity(
        self,
    ) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            snapshot = self.wait_for_observation(RESOURCE_ID)
            self.assertEqual("ok", snapshot.health.status)

            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(reasoning_effort="high"),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            document = cast("dict[str, object]", payload)
            choices = cast("list[object]", document["choices"])
            message = cast("dict[str, object]", choices[0])["message"]
            self.assertEqual(REPLY, cast("dict[str, object]", message)["content"])
            # The audited executed target IS the shipped calibrated
            # identity: exact provider, physical model and variant.
            record = self.executed_audit_records()[-1]
            target = cast("dict[str, object]", record["executed_target"])
            self.assertEqual(RESOURCE_ID, target.get("resource_id"))
            self.assertEqual("openai", target.get("provider"))
            self.assertEqual(CODEX_CATALOG_MODEL, target.get("model"))
            self.assertEqual("high", target.get("variant"))
            # The adapter pinned EXACTLY that model+effort for the turn —
            # the fake's listing advertises ``gpt-5.6-sol`` (never
            # ``codex``), so this proves the exact-binding chain.
            turn = trace_request(worker.trace_path, "turn/start")
            assert turn is not None
            self.assertEqual(CODEX_CATALOG_MODEL, turn.get("model"))
            self.assertEqual("high", turn.get("effort"))
            self.assertIn("model/list", trace_methods(worker.trace_path))
        finally:
            worker.stop()

    def test_configured_model_mismatch_fails_before_any_spawn(self) -> None:
        """A desynced worker configuration can never execute another model.

        The server's resource represents ``gpt-5.6-sol``; the worker's
        adapter was re-armed with ``gpt-5.6-luna``. Its reports are
        rejected (identity mismatch), the last ok observation stays fresh,
        so admission passes and dispatch reaches the worker — where the
        adapter's invariant rejects the selected model BEFORE any process
        spawn, before any thread/turn, with the typed reason.
        """
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
        finally:
            worker.stop()

        adapter_tmp = Path(tempfile.mkdtemp(prefix="scarcity-router-m10b-desync-"))
        self.addCleanup(lambda: _rmtree(adapter_tmp))
        _ = worker.replace_adapter(
            chatgpt_scenario(),
            state_dir=adapter_tmp,
            trace_path=adapter_tmp / "trace.jsonl",
            model="gpt-5.6-luna",
        )
        worker.start()
        try:
            # Wait until the desynced worker's session is live AND its
            # initial eligibility probe has run (the probe spawns one
            # app-server session worker-side), so the dispatch below is
            # deterministic — any additional spawn belongs to the DISPATCH.
            wait_until(
                lambda: len(worker.transports) >= 2,
                timeout=20,
                message="the desynced worker never reconnected",
            )
            wait_until(
                lambda: len(worker.spawner.app_server_specs) >= 1,
                timeout=20,
                message="the desynced adapter's eligibility probe never ran",
            )
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                # A single-message, non-streaming, effort-less request
                # needs NO compatibility feature: on a build without the
                # adapter invariant this dispatch EXECUTES (200) — the
                # exact failure this invariant forbids.
                _pin_body(),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(502, status)
            error_body = cast(
                "dict[str, object]", cast("dict[str, object]", payload)["error"]
            )
            self.assertEqual("backend_failure", error_body["code"])
            # The audit records the failed execution (the typed worker-side
            # reason stays on the worker path; the audit carries the closed
            # reason code).
            record = self.executed_audit_records()[-1]
            self.assertEqual("failed", record["result_status"])
            self.assertIn("backend_failure", cast("list[str]", record["reason_codes"]))
            # The dispatch spawned NOTHING: exactly the initial eligibility
            # probe session exists — no second app-server session, no
            # thread, no turn.
            self.assertEqual(1, len(worker.spawner.app_server_specs))
            self.assertNotIn("thread/start", trace_methods(worker.trace_path))
            self.assertNotIn("turn/start", trace_methods(worker.trace_path))
        finally:
            worker.stop()


# ── Areas 4a, 11, 12: composed execution, usage honesty, leakage ──────────────


class CodexExecutionTests(CodexComposedTlsWorld):
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
            record = self.executed_audit_records()[-1]
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
            record = self.executed_audit_records()[-1]
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
            executed = [
                record
                for record in self.audit_records()
                if record.get("executed_target") is not None
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


# ── Blocker 2: the production compatibility matrix ────────────────────────────


_ZAI_PROVIDER_DOCUMENT: dict[str, object] = {
    "provider_id": "zai-http",
    "adapter_id": "zai-coding-plan",
    "base_url": "https://api.z.ai",
    "secret": FAKE_PROVIDER_SECRET,
}

_ZAI_RESOURCE_DOCUMENT: dict[str, object] = {
    "registration": {
        "identity": {
            "resource_id": "zai-plan-1",
            "channel": "server_direct_http",
            "provider": "zai",
            "model": "glm-5.3",
            "entitlement": "subscription_included",
        },
        "freshness_ttl_seconds": 3600,
        "capabilities": {"context_limit_tokens": 272_000},
    },
    "enabled": True,
    "endpoint_id": "zai-http",
}


def _cell_keys(cells: tuple[CompatibilityCell, ...]) -> set[tuple[str, str, str, str]]:
    """The ``(channel, provider, model, feature)`` keys of a cell tuple."""
    return {
        (cell.channel, cell.provider, cell.model, cell.feature) for cell in cells
    }


def _cell_map(
    cells: tuple[CompatibilityCell, ...],
) -> dict[tuple[str, str, str, str], str]:
    return {
        (cell.channel, cell.provider, cell.model, cell.feature): cell.value
        for cell in cells
    }


class ProductionCompatibilityMatrixTests(CodexComposedTlsWorld):
    """The composed application carries the REAL evidence-backed matrix.

    Cells come from administrator configuration through the production
    composition seam (M04 preset evidence + the reviewed M06 Codex
    evidence) — no synthetic cell injection, no empty-matrix shortcut.
    """

    def application_cells(self) -> tuple[CompatibilityCell, ...]:
        return self.plane.current_application().compatibility_cells

    def test_codex_resource_gets_the_reviewed_m06_cells(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        values = _cell_map(self.application_cells())
        for feature, (expected_value, _note) in CODEX_WORKER_CELL_VALUES.items():
            key = ("worker_bridged", "openai", CODEX_CATALOG_MODEL, feature)
            self.assertIn(key, values, f"missing M06 cell for {feature}")
            self.assertEqual(
                expected_value,
                values[key],
                f"M06 cell {feature} must equal the reviewed value",
            )
        # No cells beyond the reviewed table's features for this backend.
        reviewed_features = {feature for feature in CODEX_WORKER_CELL_VALUES}
        present = {
            feature
            for (_ch, _p, _m, feature) in _cell_keys(self.application_cells())
            if (_ch, _p, _m) == ("worker_bridged", "openai", CODEX_CATALOG_MODEL)
        }
        self.assertEqual(reviewed_features, present)

    def test_m06_cells_carry_dated_provenance_and_adapter_identity(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        codex_cells = [
            cell
            for cell in self.application_cells()
            if (cell.channel, cell.provider, cell.model)
            == ("worker_bridged", "openai", CODEX_CATALOG_MODEL)
        ]
        self.assertTrue(codex_cells, "the M06 cells must exist for the resource")
        for cell in codex_cells:
            self.assertIsNone(cell.variant)
            self.assertEqual("codex-worker-local", cell.adapter)
            self.assertEqual("1.0.0", cell.adapter_version)
            self.assertEqual("codex_adapter_stage2", cell.evidence.source)
            self.assertEqual("2026-09-20", cell.evidence.date)
            self.assertIn(
                "codex-cli 0.154.0-alpha.6.2", cell.evidence.identifier
            )
            self.assertIn(
                "docs/codex-adapter-stage1-evidence.md", cell.evidence.identifier
            )

    def test_evidence_backed_m04_resource_gets_its_preset_cells(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        status, payload = self.admin_post("/control/providers", _ZAI_PROVIDER_DOCUMENT)
        self.assertEqual(200, status, payload)
        status, payload = self.admin_post(
            "/control/resources", _ZAI_RESOURCE_DOCUMENT
        )
        self.assertEqual(200, status, payload)
        values = _cell_map(self.application_cells())
        # The zai-coding-plan preset's evidenced cells, keyed to the
        # configured (provider, model).
        self.assertEqual(
            "PASS", values[("server_direct_http", "zai", "glm-5.3", "streaming")]
        )
        self.assertEqual(
            "PARTIAL",
            values[("server_direct_http", "zai", "glm-5.3", "reasoning_controls")],
        )
        # No leakage in either direction between the two backends.
        self.assertNotIn(
            ("server_direct_http", "openai", CODEX_CATALOG_MODEL, "streaming"),
            values,
        )
        self.assertNotIn(("worker_bridged", "zai", "glm-5.3", "streaming"), values)

    def test_configuration_removal_rebuilds_the_matrix(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        keys_before = _cell_keys(self.application_cells())
        self.assertIn(
            ("worker_bridged", "openai", CODEX_CATALOG_MODEL, "streaming"),
            keys_before,
        )
        csrf = self.plane.csrf_token_for_cookie(self.cookie)
        assert csrf is not None
        status, payload, _headers = self.exchange(
            "DELETE",
            f"/control/resources/{RESOURCE_ID}",
            headers={"X-Scarcity-CSRF": csrf},
        )
        self.assertEqual(200, status, payload)
        keys_after = _cell_keys(self.application_cells())
        self.assertNotIn(
            ("worker_bridged", "openai", CODEX_CATALOG_MODEL, "streaming"),
            keys_after,
        )
        self.assertNotIn(
            ("worker_bridged", "openai", CODEX_CATALOG_MODEL, "tool_calls"),
            keys_after,
        )
        # Re-adding the resource brings the cells back (configuration is
        # the only matrix source on the composed path).
        self.configure_codex_resource(worker.worker_id)
        self.assertIn(
            ("worker_bridged", "openai", CODEX_CATALOG_MODEL, "streaming"),
            _cell_keys(self.application_cells()),
        )

    def test_a_codex_streaming_request_serves_on_the_built_in_evidence(self) -> None:
        """End-to-end: the REAL cells (streaming PARTIAL) admit the stream.

        No synthetic cell exists anywhere in this test — if the composed
        matrix were empty (or the evidence not wired), this request would
        fail closed with ``compatibility_unknown``.
        """
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(stream=True),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            self.assertEqual(1, trace_methods(worker.trace_path).count("turn/start"))
        finally:
            worker.stop()


# ── Areas 4b, 6-9, 13: streaming execution semantics (composed server) ────────


class CodexStreamingExecutionTests(CodexComposedTlsWorld):
    """Streaming semantics through the ACTUAL composed server.

    Every gate here is served by the production matrix (the built-in M06
    evidence cells); no test in this class supplies or injects cells.
    """

    def open_raw_stream(self, payload: dict[str, object]) -> socket.socket:
        """One raw-socket SSE request (for client-disconnect control)."""
        body = json.dumps(payload).encode("utf-8")
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        request = (
            "POST /v1/chat/completions HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            f"Authorization: Bearer {self.client_key}\r\n"
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

    def test_streaming_roles_and_history_end_to_end(self) -> None:
        """Full SSE round trip: roles map, history is injected, deltas flow."""
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(
                    messages=[
                        {"role": "system", "content": "SYS-M10B-system-text"},
                        {"role": "user", "content": "earlier question"},
                        {"role": "assistant", "content": "earlier answer"},
                        {"role": "user", "content": PROMPT},
                    ],
                    stream=True,
                ),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            text = payload if isinstance(payload, str) else str(payload)
            self.assertTrue(text.rstrip().endswith("data: [DONE]"))
            # Parse the SSE frames. The honest surface shape for a stream
            # the adapter delivered as text deltas: the role frame first,
            # the deltas assembling the fake's message, then [DONE] — the
            # explicit finish_reason frame appears only when the adapter
            # itself emits a finish chunk (the Codex adapter does not;
            # recorded in the acceptance doc).
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

            thread_params = trace_request(worker.trace_path, "thread/start")
            assert thread_params is not None
            self.assertEqual("SYS-M10B-system-text", thread_params.get("baseInstructions"))
            inject = trace_request(worker.trace_path, "thread/inject_items")
            assert inject is not None
            items = cast("list[object]", inject.get("items"))
            roles = [cast("dict[str, object]", item).get("role") for item in items]
            self.assertEqual(["user", "assistant"], roles)
            turn = trace_request(worker.trace_path, "turn/start")
            assert turn is not None
            turn_input = cast("list[object]", turn.get("input"))
            first = cast("dict[str, object]", turn_input[0])
            self.assertEqual(PROMPT, first.get("text"))

            executed = [
                record
                for record in self.audit_records()
                if record.get("executed_target") is not None
            ]
            self.assertEqual(1, len(executed))
            self.assertEqual("completed", executed[0]["result_status"])
        finally:
            worker.stop()

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
        worker = self.start_codex_worker(chatgpt_scenario(turn=turn))
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            raw = self.open_raw_stream(_pin_body(stream=True))
            try:
                wait_until(
                    lambda: "turn/start" in trace_methods(worker.trace_path),
                    timeout=30,
                    message="the turn never started",
                )
                _ = raw.settimeout(60.0)
                head = self.read_until_headers(raw)
                self.assertIn(" 200 ", head.decode("ascii", errors="replace"))
                # Drain whatever frames already rode with the headers
                # (bounded): the FIN then lands while the fake still holds
                # pending output.
                _ = raw.settimeout(5.0)
                try:
                    _ = raw.recv(4096)
                except (TimeoutError, OSError):
                    pass
                # The client disconnects: a clean FIN on the request socket.
                _ = raw.shutdown(socket.SHUT_WR)
                # The next pending chunk drives the gateway's EOF check;
                # the cancel must propagate all the way to turn/interrupt
                # while the turn is still open.
                wait_until(
                    lambda: "turn/interrupt" in trace_methods(worker.trace_path),
                    timeout=25,
                    message="cancel never reached the codex turn",
                )
                methods = trace_methods(worker.trace_path)
                self.assertEqual(1, methods.count("turn/start"))
                self.assertEqual(1, methods.count("turn/interrupt"))
            finally:
                _ = raw.close()

            # The outcome was reported honestly: cancelled (client gone),
            # and no completion was ever produced after the cancellation.
            executed = [
                record
                for record in self.audit_records()
                if record.get("executed_target") is not None
            ]
            self.assertEqual(1, len(executed))
            self.assertEqual("cancelled", executed[0]["result_status"])
            self.assertIn("client_disconnected", cast("list[str]", executed[0]["reason_codes"]))
            # Exactly two app-server sessions ran: the state-report probe
            # and the dispatch — no third session, no second execution.
            wait_until(
                lambda: len(worker.spawner.app_server_specs) >= 2,
                timeout=15,
                message="the probe and dispatch sessions never both ran",
            )
            self.assertEqual(2, len(worker.spawner.app_server_specs))
        finally:
            worker.stop()

    def test_reasoning_effort_forwarded_verbatim(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(reasoning_effort="high"),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            turn = trace_request(worker.trace_path, "turn/start")
            assert turn is not None
            self.assertEqual(CODEX_CATALOG_MODEL, turn.get("model"))
            self.assertEqual("high", turn.get("effort"))
        finally:
            worker.stop()

    def test_unrepresentable_effort_rejected_before_the_turn(self) -> None:
        """The runtime listing is the effort authority — and never a model
        selector: the configured physical model is selected, but the
        pinned effort is absent from ITS ``supportedReasoningEfforts``
        (the shipped-calibrated ``medium``/``high``), so the adapter's
        exact-binding check rejects it before any thread/turn exists."""
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(reasoning_effort="minimal"),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(502, status)
            error_body = cast(
                "dict[str, object]", cast("dict[str, object]", payload)["error"]
            )
            self.assertEqual("backend_failure", error_body["code"])
            methods = trace_methods(worker.trace_path)
            # The binding verdict needs the runtime's own model/list, but
            # the TURN never starts: no thread, no turn, nothing executed.
            self.assertIn("model/list", methods)
            self.assertNotIn("thread/start", methods)
            self.assertNotIn("turn/start", methods)
            executed = [
                record
                for record in self.audit_records()
                if record.get("executed_target") is not None
            ]
            self.assertEqual(1, len(executed))
            self.assertEqual("failed", executed[0]["result_status"])
        finally:
            worker.stop()

    def test_structured_output_schema_forwarded(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            schema = {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            }
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "answer",
                            "schema": schema,
                            "strict": True,
                        },
                    }
                ),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)
            turn = trace_request(worker.trace_path, "turn/start")
            assert turn is not None
            self.assertEqual(schema, turn.get("outputSchema"))
        finally:
            worker.stop()

    def test_tools_bearing_request_rejected_before_dispatch(self) -> None:
        """The REAL M06 ``UNSUPPORTED`` tool_calls cell fails the request
        closed at admission — no execute message, no dispatch session,
        no turn."""
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
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
                ),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(400, status)
            error_body = cast(
                "dict[str, object]", cast("dict[str, object]", payload)["error"]
            )
            self.assertEqual("compatibility_unsupported", error_body["code"])
            # Rejected at admission: no dispatch session, no turn — only
            # the initial state-report probe ever runs.
            wait_until(
                lambda: len(worker.spawner.app_server_specs) >= 1,
                timeout=15,
                message="the state-report probe session never ran",
            )
            methods = trace_methods(worker.trace_path)
            self.assertNotIn("thread/start", methods)
            self.assertNotIn("turn/start", methods)
            self.assertEqual(1, len(worker.spawner.app_server_specs))
            # The rejection is audited as `rejected` (never dispatched, so
            # the record carries no executed target).
            rejected = [
                record
                for record in self.audit_records()
                if record.get("result_status") == "rejected"
            ]
            self.assertEqual(1, len(rejected))
            self.assertIn(
                "compatibility_unsupported",
                cast("list[str]", rejected[0]["reason_codes"]),
            )
            self.assertIsNone(rejected[0].get("executed_target"))
        finally:
            worker.stop()

    def test_isolation_profile_end_to_end(self) -> None:
        worker = self.start_codex_worker(chatgpt_scenario())
        self.configure_codex_resource(worker.worker_id)
        worker.start()
        try:
            _ = self.wait_for_observation(RESOURCE_ID)
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                _pin_body(
                    messages=[
                        {"role": "system", "content": "SYS-TEXT"},
                        {"role": "developer", "content": "DEV-TEXT"},
                        {"role": "user", "content": "first"},
                        {"role": "assistant", "content": "second"},
                        {"role": "user", "content": PROMPT},
                    ]
                ),
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            self.assertEqual(200, status, payload)

            state_dir = worker.state_dir
            home = state_dir / "codex" / "codex-home"
            # The controlled home: 0o700, minimal generated config, no MCP
            # servers, no credential material.
            self.assertEqual(0o700, stat.S_IMODE(home.stat().st_mode))
            config = (home / CONTROLLED_CONFIG_NAME).read_text(encoding="utf-8")
            self.assertNotIn("mcp_servers", config)
            self.assertFalse((home / "auth.json").exists())

            # The spawn spec is fully adapter-owned: minimal env, controlled
            # home, scratch cwd under the worker state dir.
            self.assertEqual(2, len(worker.spawner.app_server_specs))
            spec = worker.spawner.app_server_specs[-1]
            self.assertLessEqual(
                set(spec.env) - {"SR_FAKE_TRACE"}, {"PATH", "HOME", "CODEX_HOME"}
            )
            self.assertEqual(str(home), spec.env["CODEX_HOME"])
            scratch_root = state_dir / "codex" / "scratch"
            self.assertTrue(str(spec.cwd).startswith(str(scratch_root)))

            # The protocol requests carry the composed isolation policy.
            thread_params = trace_request(worker.trace_path, "thread/start")
            assert thread_params is not None
            self.assertIs(True, thread_params.get("ephemeral"))
            self.assertEqual("never", thread_params.get("approvalPolicy"))
            self.assertEqual("workspace-write", thread_params.get("sandbox"))
            self.assertEqual("SYS-TEXT", thread_params.get("baseInstructions"))
            self.assertEqual("DEV-TEXT", thread_params.get("developerInstructions"))
            scratch = thread_params.get("cwd")
            self.assertTrue(str(scratch).startswith(str(scratch_root)))

            turn_params = trace_request(worker.trace_path, "turn/start")
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
        finally:
            worker.stop()


if __name__ == "__main__":
    _ = unittest.main()
