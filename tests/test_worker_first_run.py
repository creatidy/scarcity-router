"""Windows worker first-run onboarding tests (issue #113).

Discriminating suite for the packaged first-run experience: every test
here fails on the pre-#113 code, where the standalone executable
dead-ended on a Python console script it did not contain. The onboarding
core is exercised through the SAME seams production uses — the packaged
entry router (``tray_main``), the bounded local store, the real
``WorkerRuntime.pair`` protocol path over in-memory transports — with
fake views standing in for the packaging-tree tkinter dialog and the
pystray tray. No GUI toolkit, no Windows, no sockets, no provider calls.

Locked behavior:

- no-arg launch routes unpaired workers into first-run setup and paired
  workers into the tray; explicit run flags keep CLI semantics;
- cancelling first-run persists nothing;
- valid pairing drives the REAL ``WorkerRuntime.pair`` path and
  persists the identity in the existing ``WorkerLocalStore``; the
  pairing code itself is never persisted anywhere;
- server rejections map to safe, actionable messages (no tracebacks);
- local settings are typed, versioned, non-secret, strictly parsed and
  fail closed when malformed; they survive a store reopen and drive the
  EXISTING adapter-registry builder (loopback Ollama only);
- the settings dialog is the SAME configuration UI (no second
  implementation) and a save triggers the controlled tray restart;
- Windows Codex is never advertised by the GUI path.
"""

from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
from collections import deque
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override

from scarcity_router import worker_setup, windows_tray
from scarcity_router.worker_client import (
    WorkerOrigin,
    build_local_adapter_registry,
)
from scarcity_router.worker_local_store import (
    WORKER_IDENTITY_KEY,
    WorkerLocalIdentity,
    WorkerLocalStore,
)
from scarcity_router.worker_protocol import (
    ErrorMessage,
    FrameReader,
    FrameTransport,
    FrameWriter,
    PairRequestMessage,
    PairResultMessage,
    WORKER_PROTOCOL_VERSION,
    parse_worker_message,
)
from tests.worker_fixtures import (
    SYNTHETIC_CODE,
    SYNTHETIC_CREDENTIAL,
    MemoryTransport,
)

REPO = Path(__file__).resolve().parents[1]
SETUP_VIEW_PATH = REPO / "packaging" / "windows" / "scarcity_worker_setup_view.py"
ORIGIN = "srws://127.0.0.1:8790"

# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakePairingServer:
    """Answers ONE ``pair_request`` over an in-memory transport.

    Either accepts the code (the conventional PairResult) or rejects it
    with a typed server error code — the same vocabulary the real
    endpoint uses. Runs on its own thread; the request it saw is
    recorded for assertions.
    """

    def __init__(
        self,
        *,
        error_code: str | None = None,
        worker_id: str = "worker-first-run-1",
        credential: str = SYNTHETIC_CREDENTIAL,
    ) -> None:
        self.worker_side: FrameTransport
        self.server_side: FrameTransport
        self.worker_side, self.server_side = MemoryTransport.pair()
        self.requests: list[PairRequestMessage] = []
        self._error_code: str | None = error_code
        self._worker_id: str = worker_id
        self._credential: str = credential
        self._thread: threading.Thread = threading.Thread(
            target=self._serve, daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        reader = FrameReader(self.server_side)
        writer = FrameWriter(self.server_side)
        payload = reader.read_message()
        if payload is None:
            return
        message = parse_worker_message(payload)
        if isinstance(message, PairRequestMessage):
            self.requests.append(message)
        if self._error_code is not None:
            writer.write_message(
                ErrorMessage(
                    code=self._error_code, message="synthetic rejection", fatal=True
                ).to_payload()
            )
        else:
            writer.write_message(
                PairResultMessage(
                    negotiated_version=WORKER_PROTOCOL_VERSION,
                    worker_id=self._worker_id,
                    credential=self._credential,
                    heartbeat_interval_seconds=15,
                ).to_payload()
            )


def scripted_connect_factory(
    *servers: FakePairingServer,
) -> Callable[[WorkerOrigin], FrameTransport]:
    """One connect factory handing out the given servers in order."""
    pending: deque[FakePairingServer] = deque(servers)

    def factory(origin: WorkerOrigin) -> FrameTransport:
        _ = origin  # the synthetic servers ignore the parsed origin
        server = pending.popleft()
        return server.worker_side

    return factory


class FakeSetupView:
    """A scripted :class:`worker_setup.SetupView` (no pixels).

    ``first_run_script`` entries are either :class:`SetupFields` (the
    dialog drives one ``submit`` attempt with them, keeps the dialog
    open on failure, and returns on the first success) or ``None``
    (the user cancelled). ``settings_script`` works the same against
    ``save``: fields drive one save attempt; ``None`` cancels.
    """

    def __init__(
        self,
        *,
        first_run_script: list[worker_setup.SetupFields | None] | None = None,
        settings_script: list[worker_setup.SetupFields | None] | None = None,
    ) -> None:
        self.first_run_script: list[worker_setup.SetupFields | None] = list(
            first_run_script or []
        )
        self.settings_script: list[worker_setup.SetupFields | None] = list(
            settings_script or []
        )
        self.first_run_calls: int = 0
        self.submissions: list[worker_setup.SetupFields] = []
        self.outcomes: list[worker_setup.SetupOutcome] = []
        self.settings_contexts: list[worker_setup.SettingsDialogContext] = []
        self.saved_fields: list[worker_setup.SetupFields] = []
        self.save_errors: list[Exception] = []

    def run_first_run(
        self, submit: Callable[[worker_setup.SetupFields], worker_setup.SetupOutcome]
    ) -> worker_setup.SetupOutcome | None:
        self.first_run_calls += 1
        while self.first_run_script:
            item = self.first_run_script.pop(0)
            if item is None:
                return None
            self.submissions.append(item)
            outcome = submit(item)
            self.outcomes.append(outcome)
            if outcome.ok:
                return outcome
        return None

    def run_settings(
        self,
        context: worker_setup.SettingsDialogContext,
        save: Callable[[worker_setup.SetupFields], worker_setup.WorkerLocalSettings],
    ) -> worker_setup.WorkerLocalSettings | None:
        self.settings_contexts.append(context)
        while self.settings_script:
            item = self.settings_script.pop(0)
            if item is None:
                return None
            self.saved_fields.append(item)
            try:
                return save(item)
            except Exception as exc:  # the dialog would display it
                self.save_errors.append(exc)
        return None


class SettingsSaveFailureStore(WorkerLocalStore):
    """A store whose settings value cannot be written (identity can)."""

    @override
    def save_value(self, key: str, value: str) -> None:
        if key == worker_setup.SETTINGS_STORE_KEY:
            raise OSError("synthetic settings save failure")
        _ = super().save_value(key, value)


class FakeRuntime:
    """A controllable tray runtime (mirrors the tray-suite fake)."""

    def __init__(self, *, reason: str = "requested", immediate: bool = True) -> None:
        self.reason: str = reason
        self.immediate: bool = immediate
        self.stop_requests: int = 0
        self._stop: threading.Event = threading.Event()

    def run(self) -> str:
        while not self.immediate and not self._stop.is_set():
            _ = self._stop.wait(timeout=0.005)
        return self.reason

    def request_stop(self) -> None:
        self.stop_requests += 1
        self._stop.set()

    def diagnostics(self) -> tuple[str, ...]:
        return ("synthetic first-run diagnostic line",)


class RecordingView:
    """Minimal TrayView stand-in recording updates."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, str]] = []
        self.started: bool = False
        self.stopped: bool = False

    def start(self) -> None:
        self.started = True

    def update(self, *, state: str, text: str) -> None:
        self.updates.append((state, text))

    def stop(self) -> None:
        self.stopped = True


def open_store(directory: str | Path) -> WorkerLocalStore:
    return WorkerLocalStore(Path(directory) / "worker-state.db")


def synthetic_identity(worker_id: str = "worker-paired-1") -> WorkerLocalIdentity:
    return WorkerLocalIdentity(
        worker_id=worker_id,
        credential=SYNTHETIC_CREDENTIAL,
        server_origin=ORIGIN,
    )


TrayViewFactory = Callable[
    [windows_tray.TrayStateModel, Callable[[], None], Path, Callable[[], None]],
    RecordingView,
]


def recording_view_factory(
    view: RecordingView, actions: dict[str, object]
) -> TrayViewFactory:
    """A view factory that only records the actions tray_main wires."""

    def factory(
        model: windows_tray.TrayStateModel,
        open_control_ui: Callable[[], None],
        diagnostics_dir: Path,
        on_settings: Callable[[], None],
    ) -> RecordingView:
        _ = diagnostics_dir  # recorded actions only; the dir is unused here
        actions["open_control_ui"] = open_control_ui
        actions["on_settings"] = on_settings
        actions["model"] = model
        return view

    return factory


def autoquit_view_factory(
    view: RecordingView, terminal: str, actions: dict[str, object]
) -> TrayViewFactory:
    """A view factory that quits once the terminal state renders."""

    def factory(
        model: windows_tray.TrayStateModel,
        open_control_ui: Callable[[], None],
        diagnostics_dir: Path,
        on_settings: Callable[[], None],
    ) -> RecordingView:
        _ = recording_view_factory(view, actions)(
            model, open_control_ui, diagnostics_dir, on_settings
        )

        def drive() -> None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if model.state == terminal:
                    model.request_quit()
                    return
                _ = threading.Event().wait(0.005)

        threading.Thread(target=drive, daemon=True).start()
        return view

    return factory


# ── Entry routing ─────────────────────────────────────────────────────────────


class FirstRunRoutingTests(unittest.TestCase):
    def test_unpaired_no_arg_launch_enters_first_run_then_tray(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            view = RecordingView()
            actions: dict[str, object] = {}
            setup = FakeSetupView(
                first_run_script=[
                    worker_setup.SetupFields(
                        server_url=ORIGIN,
                        pairing_code=SYNTHETIC_CODE,
                        ollama_enabled=False,
                    )
                ]
            )
            exit_code = windows_tray.tray_main(
                [],
                state_dir=parent,
                view_factory=autoquit_view_factory(view, "running", actions),
                setup_view_factory=lambda: setup,
                connect_factory=scripted_connect_factory(FakePairingServer()),
                runtime_factory=lambda: FakeRuntime(),
            )
            identity = store.load_identity()
            store.close()
        self.assertEqual(0, exit_code)
        self.assertEqual(1, setup.first_run_calls)
        self.assertTrue(view.started)
        self.assertTrue(view.stopped)
        # The stored identity is the real pairing result.
        assert identity is not None
        self.assertEqual("worker-first-run-1", identity.worker_id)
        self.assertEqual(ORIGIN, identity.server_origin)

    def test_cancelled_first_run_persists_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            setup = FakeSetupView(first_run_script=[None])
            exit_code = windows_tray.tray_main(
                [],
                state_dir=parent,
                view_factory=autoquit_view_factory(RecordingView(), "running", {}),
                setup_view_factory=lambda: setup,
                runtime_factory=lambda: FakeRuntime(),
            )
            store = open_store(parent)
            try:
                identity = store.load_identity()
                settings = worker_setup.load_worker_settings(store)
            finally:
                store.close()
        self.assertEqual(1, exit_code)
        self.assertEqual(1, setup.first_run_calls)
        self.assertIsNone(identity)
        self.assertIsNone(settings)

    def test_unpaired_without_setup_factory_names_the_executable(self) -> None:
        # The pre-#113 dead end: the remediation text sent users to the
        # Python console script the standalone ZIP does not contain.
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as parent:
            with redirect_stderr(stderr):
                exit_code = windows_tray.tray_main([], state_dir=parent)
        self.assertEqual(2, exit_code)
        text = stderr.getvalue()
        self.assertIn("not paired", text)
        self.assertIn("scarcity-worker", text)
        self.assertIn("pair --server srws://SERVER:8790 --code CODE", text)
        self.assertNotIn("scarcity-router-worker", text)

    def test_unpaired_no_arg_without_setup_factory_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            exit_code = windows_tray.tray_main(
                [],
                state_dir=parent,
                view_factory=autoquit_view_factory(RecordingView(), "running", {}),
            )
        self.assertEqual(2, exit_code)

    def test_explicit_run_flags_keep_cli_semantics(self) -> None:
        # Explicit argv is the automation path: it never opens the GUI,
        # even when a setup dialog is available.
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as parent:
            setup = FakeSetupView()
            with redirect_stderr(stderr):
                exit_code = windows_tray.tray_main(
                    ["run", "--state-dir", parent, "--allow-ollama", "--resource", "x"],
                    setup_view_factory=lambda: setup,
                )
            store = open_store(parent)
            try:
                identity = store.load_identity()
            finally:
                store.close()
        self.assertEqual(2, exit_code)
        self.assertEqual(0, setup.first_run_calls)
        self.assertIn("not paired", stderr.getvalue())
        self.assertIsNone(identity)

    def test_paired_no_arg_launch_goes_straight_to_tray(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            store.save_identity(synthetic_identity())
            store.close()
            view = RecordingView()
            setup = FakeSetupView()
            exit_code = windows_tray.tray_main(
                [],
                state_dir=parent,
                view_factory=autoquit_view_factory(view, "running", {}),
                setup_view_factory=lambda: setup,
                runtime_factory=lambda: FakeRuntime(),
            )
        self.assertEqual(0, exit_code)
        self.assertEqual(0, setup.first_run_calls)
        self.assertTrue(view.started)


# ── Packaged pair command ─────────────────────────────────────────────────────


class PackagedPairCommandTests(unittest.TestCase):
    def test_pair_command_pairs_through_the_shared_path(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as parent:
            with redirect_stdout(stdout):
                exit_code = windows_tray.tray_main(
                    [
                        "pair",
                        "--server",
                        ORIGIN,
                        "--code",
                        SYNTHETIC_CODE,
                        "--state-dir",
                        parent,
                    ],
                    connect_factory=scripted_connect_factory(FakePairingServer()),
                )
            store = open_store(parent)
            try:
                identity = store.load_identity()
            finally:
                store.close()
        self.assertEqual(0, exit_code)
        self.assertIn("paired as worker-first-run-1", stdout.getvalue())
        assert identity is not None
        self.assertEqual("worker-first-run-1", identity.worker_id)

    def test_pair_command_failure_is_safe_and_actionable(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as parent:
            with redirect_stderr(stderr):
                exit_code = windows_tray.tray_main(
                    [
                        "pair",
                        "--server",
                        ORIGIN,
                        "--code",
                        SYNTHETIC_CODE,
                        "--state-dir",
                        parent,
                    ],
                    connect_factory=scripted_connect_factory(
                        FakePairingServer(error_code="pairing_code_expired")
                    ),
                )
            store = open_store(parent)
            try:
                identity = store.load_identity()
            finally:
                store.close()
        self.assertEqual(2, exit_code)
        text = stderr.getvalue()
        self.assertIn("expired", text)
        self.assertIn("Workers page", text)
        self.assertNotIn("Traceback", text)
        # Nothing was persisted, and the code itself is never echoed.
        self.assertIsNone(identity)
        self.assertNotIn(SYNTHETIC_CODE, text)

    def test_pair_command_rejects_remote_plaintext(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as parent:
            with redirect_stderr(stderr):
                exit_code = windows_tray.tray_main(
                    [
                        "pair",
                        "--server",
                        "srw://intranet.example:8790",
                        "--code",
                        SYNTHETIC_CODE,
                        "--state-dir",
                        parent,
                    ],
                )
        self.assertEqual(2, exit_code)
        self.assertIn("loopback", stderr.getvalue())


# ── First-run flow (complete_setup core) ──────────────────────────────────────


class CompleteSetupTests(unittest.TestCase):
    def test_valid_pairing_uses_real_runtime_pair_and_persists_identity(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            server = FakePairingServer()
            outcome = worker_setup.complete_setup(
                worker_setup.SetupFields(
                    server_url=ORIGIN, pairing_code=SYNTHETIC_CODE
                ),
                store=store,
                connect_factory=scripted_connect_factory(server),
            )
            identity = store.load_identity()
            store.close()
        self.assertTrue(outcome.ok)
        self.assertEqual("worker-first-run-1", outcome.worker_id)
        self.assertTrue(outcome.settings_saved)
        assert identity is not None
        self.assertEqual("worker-first-run-1", identity.worker_id)
        self.assertEqual(SYNTHETIC_CREDENTIAL, identity.credential)
        self.assertEqual(ORIGIN, identity.server_origin)
        # The code went over the wire exactly once, on the protocol path.
        self.assertEqual(1, len(server.requests))
        self.assertEqual(SYNTHETIC_CODE, server.requests[0].pairing_code)

    def test_failed_pairing_persists_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            outcome = worker_setup.complete_setup(
                worker_setup.SetupFields(
                    server_url=ORIGIN, pairing_code=SYNTHETIC_CODE
                ),
                store=store,
                connect_factory=scripted_connect_factory(
                    FakePairingServer(error_code="pairing_code_used")
                ),
            )
            identity = store.load_identity()
            settings = worker_setup.load_worker_settings(store)
            store.close()
        self.assertFalse(outcome.ok)
        self.assertIn("already used", outcome.message)
        self.assertIn("Workers page", outcome.message)
        self.assertIsNone(identity)
        self.assertIsNone(settings)

    def test_dialog_retries_after_failure_until_success(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            setup = FakeSetupView(
                first_run_script=[
                    worker_setup.SetupFields(
                        server_url=ORIGIN, pairing_code="STALE-CODE"
                    ),
                    worker_setup.SetupFields(
                        server_url=ORIGIN, pairing_code=SYNTHETIC_CODE
                    ),
                ]
            )
            # One factory serving both attempts in order: the stale code
            # is rejected, the fresh one is accepted.
            factory = scripted_connect_factory(
                FakePairingServer(error_code="pairing_code_invalid"),
                FakePairingServer(),
            )
            outcome = setup.run_first_run(
                lambda fields: worker_setup.complete_setup(
                    fields, store=store, connect_factory=factory
                )
            )
            store.close()
        assert outcome is not None
        self.assertTrue(outcome.ok)
        self.assertFalse(setup.outcomes[0].ok)
        self.assertEqual(2, len(setup.outcomes))

    def test_pairing_code_is_never_persisted(self) -> None:
        code = "ONE-TIME-CODE-must-not-survive-42"
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            _ = worker_setup.complete_setup(
                worker_setup.SetupFields(
                    server_url=ORIGIN,
                    pairing_code=code,
                    ollama_enabled=True,
                    resource_id="my-ollama",
                ),
                store=store,
                connect_factory=scripted_connect_factory(FakePairingServer()),
            )
            settings_raw = store.load_value(worker_setup.SETTINGS_STORE_KEY)
            identity_raw = store.load_value(WORKER_IDENTITY_KEY)
            store.close()
            db_bytes = (Path(parent) / "worker-state.db").read_bytes()
        self.assertIsNotNone(settings_raw)
        self.assertIsNotNone(identity_raw)
        assert settings_raw is not None
        assert identity_raw is not None
        self.assertNotIn(code, settings_raw)
        self.assertNotIn(code, identity_raw)
        self.assertNotIn(code.encode("utf-8"), db_bytes)

    def test_settings_save_failure_after_pairing_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = SettingsSaveFailureStore(Path(parent) / "worker-state.db")
            outcome = worker_setup.complete_setup(
                worker_setup.SetupFields(
                    server_url=ORIGIN, pairing_code=SYNTHETIC_CODE
                ),
                store=store,
                connect_factory=scripted_connect_factory(FakePairingServer()),
            )
            identity = store.load_identity()
            settings = worker_setup.load_worker_settings(store)
            store.close()
        # Paired, no second code needed; the settings are just absent
        # (the next launch enters a recoverable settings state).
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.settings_saved)
        self.assertIn("Worker settings", outcome.message)
        self.assertIsNotNone(identity)
        self.assertIsNone(settings)

    def test_empty_code_fails_before_any_network_call(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            outcome = worker_setup.complete_setup(
                worker_setup.SetupFields(server_url=ORIGIN),
                store=store,
            )
            store.close()
        self.assertFalse(outcome.ok)
        self.assertIn("pairing code", outcome.message)


# ── Local settings document ───────────────────────────────────────────────────


class LocalSettingsDocumentTests(unittest.TestCase):
    def test_roundtrip_survives_store_reopen(self) -> None:
        settings = worker_setup.WorkerLocalSettings(
            server_ui_url="https://srv.example:9443",
            ollama=worker_setup.OllamaSettings(
                resource_id="my-ollama", host="localhost", port=11434
            ),
        )
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            worker_setup.save_worker_settings(store, settings)
            first = worker_setup.load_worker_settings(store)
            store.close()
            reopened = open_store(parent)
            try:
                second = worker_setup.load_worker_settings(reopened)
            finally:
                reopened.close()
        self.assertEqual(settings, first)
        self.assertEqual(settings, second)

    def test_strict_parsing_fails_closed(self) -> None:
        cases = [
            "not json at all",
            "[]",
            '{"schema_version": 1}',
            '{"schema_version": 1, "server_ui_url": null, "ollama": null, "extra": 1}',
            '{"schema_version": 2, "server_ui_url": null, "ollama": null}',
            '{"schema_version": true, "server_ui_url": null, "ollama": null}',
            '{"schema_version": 1, "server_ui_url": 3, "ollama": null}',
            '{"schema_version": 1, "server_ui_url": null, "ollama": {"resource_id": "r"}}',
            '{"schema_version": 1, "server_ui_url": null, "ollama": '
            + '{"resource_id": "r", "host": "127.0.0.1", "port": "11434"}}',
            '{"schema_version": 1, "server_ui_url": null, "ollama": '
            + '{"resource_id": "Bad Id", "host": "127.0.0.1", "port": 11434}}',
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(worker_setup.WorkerSetupConfigError):
                    _ = worker_setup.settings_from_json(text)

    def test_no_secret_fields_can_be_smuggled_in(self) -> None:
        for smuggled in ("pairing_code", "credential", "api_key", "codex_token"):
            text = (
                '{"schema_version": 1, "server_ui_url": null, "ollama": null, "'
                + smuggled
                + '": "SYNTHETIC"}'
            )
            with self.subTest(smuggled=smuggled):
                with self.assertRaises(worker_setup.WorkerSetupConfigError):
                    _ = worker_setup.settings_from_json(text)

    def test_control_ui_url_validation(self) -> None:
        self.assertEqual(
            "https://srv.example:9443",
            worker_setup.normalize_control_ui_url("https://SRV.example:9443/"),
        )
        for bad in (
            "ftp://srv.example",
            "https://user:pass@srv.example:8787",
            "https://srv.example:8787/admin",
            "https://srv.example:8787/?x=1",
            "https://srv.example:notaport",
            "just-text",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(worker_setup.WorkerSetupConfigError):
                    _ = worker_setup.normalize_control_ui_url(bad)

    def test_control_ui_origin_derivation(self) -> None:
        tls = WorkerOrigin.parse("srws://srv.example:8790")
        plain = WorkerOrigin.parse("srw://127.0.0.1:8790")
        self.assertEqual("https://srv.example:8787", worker_setup.control_ui_origin(tls))
        self.assertEqual("http://127.0.0.1:8787", worker_setup.control_ui_origin(plain))
        self.assertEqual(
            "https://srv.example:9443",
            worker_setup.control_ui_origin(tls, "https://srv.example:9443"),
        )


# ── Argument precedence ───────────────────────────────────────────────────────


class ArgumentPrecedenceTests(unittest.TestCase):
    _SETTINGS: worker_setup.WorkerLocalSettings = worker_setup.WorkerLocalSettings(
        ollama=worker_setup.OllamaSettings(
            resource_id="gui-ollama", host="::1", port=11434
        )
    )

    def test_settings_apply_when_cli_selects_nothing(self) -> None:
        merged = worker_setup.effective_arguments(
            {"allow_ollama": False, "allow_codex": False, "resource": None},
            self._SETTINGS,
        )
        self.assertTrue(merged["allow_ollama"])
        self.assertEqual("gui-ollama", merged["resource"])
        self.assertEqual("::1", merged["ollama_host"])
        self.assertEqual(11434, merged["ollama_port"])

    def test_explicit_cli_adapter_selection_wins(self) -> None:
        cli = {
            "allow_ollama": True,
            "allow_codex": False,
            "resource": "cli-resource",
            "ollama_host": "127.0.0.1",
            "ollama_port": 11434,
        }
        merged = worker_setup.effective_arguments(cli, self._SETTINGS)
        self.assertEqual("cli-resource", merged["resource"])
        self.assertEqual("127.0.0.1", merged["ollama_host"])

    def test_codex_cli_selection_ignores_settings(self) -> None:
        cli = {"allow_ollama": False, "allow_codex": True, "codex_resource": "c1"}
        merged = worker_setup.effective_arguments(cli, self._SETTINGS)
        self.assertFalse(merged["allow_ollama"])
        self.assertTrue(merged["allow_codex"])

    def test_pair_only_settings_build_no_registry(self) -> None:
        merged = worker_setup.effective_arguments(
            {"allow_ollama": False, "allow_codex": False, "resource": None},
            worker_setup.WorkerLocalSettings(),
        )
        self.assertIsNone(build_local_adapter_registry(merged, None))


# ── Settings-driven registry + tray restart ───────────────────────────────────


class SettingsRegistryTests(unittest.TestCase):
    def test_ollama_settings_build_the_existing_registry(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            _ = worker_setup.complete_setup(
                worker_setup.SetupFields(
                    server_url=ORIGIN,
                    pairing_code=SYNTHETIC_CODE,
                    ollama_enabled=True,
                    resource_id="my-ollama",
                    ollama_host="127.0.0.1",
                    ollama_port="11434",
                ),
                store=store,
                connect_factory=scripted_connect_factory(FakePairingServer()),
            )
            settings = worker_setup.load_worker_settings(store)
            store.close()
        self.assertIsNotNone(settings)
        merged = worker_setup.effective_arguments(
            {"allow_ollama": False, "allow_codex": False, "resource": None}, settings
        )
        registry = build_local_adapter_registry(merged, None)
        assert registry is not None
        self.assertEqual(("ollama",), registry.adapter_ids())
        adapter = registry.resolve("ollama")
        assert adapter is not None
        self.assertEqual(("my-ollama",), adapter.resource_ids)

    def test_non_loopback_ollama_refused_before_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)

            def exploding_factory(origin: WorkerOrigin) -> FrameTransport:
                _ = origin  # never reached: validation must refuse first
                raise AssertionError("pairing must not be attempted")

            try:
                outcome = worker_setup.complete_setup(
                    worker_setup.SetupFields(
                        server_url=ORIGIN,
                        pairing_code=SYNTHETIC_CODE,
                        ollama_enabled=True,
                        resource_id="my-ollama",
                        ollama_host="192.168.1.50",
                    ),
                    store=store,
                    connect_factory=exploding_factory,
                )
            finally:
                identity = store.load_identity()
                store.close()
        self.assertFalse(outcome.ok)
        self.assertIn("loopback", outcome.message)
        self.assertIsNone(identity)

    def test_build_runtime_uses_stored_settings_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            store.save_identity(synthetic_identity())
            worker_setup.save_worker_settings(
                store,
                worker_setup.WorkerLocalSettings(
                    ollama=worker_setup.OllamaSettings(resource_id="reg-1")
                ),
            )
            runtime = windows_tray.build_runtime(
                origin=WorkerOrigin.parse(ORIGIN),
                store=store,
                cli_arguments={"allow_ollama": False, "allow_codex": False},
                state_dir=parent,
            )
            self.assertEqual(("ollama",), runtime.local_adapters.adapter_ids())
            # A malformed stored document is a loud, typed failure.
            store.save_value(worker_setup.SETTINGS_STORE_KEY, "{broken")
            with self.assertRaises(worker_setup.WorkerSetupConfigError):
                _ = windows_tray.build_runtime(
                    origin=WorkerOrigin.parse(ORIGIN),
                    store=store,
                    cli_arguments={"allow_ollama": False, "allow_codex": False},
                    state_dir=parent,
                )
            store.close()


class TraySettingsActionTests(unittest.TestCase):
    def _paired_tree(self, parent: str) -> None:
        store = open_store(parent)
        store.save_identity(synthetic_identity())
        store.close()

    def test_settings_save_triggers_controlled_restart(self) -> None:
        made: list[FakeRuntime] = []

        def runtime_factory() -> FakeRuntime:
            runtime = FakeRuntime(immediate=False)
            made.append(runtime)
            return runtime

        with tempfile.TemporaryDirectory() as parent:
            self._paired_tree(parent)
            actions: dict[str, object] = {}
            view = RecordingView()
            setup = FakeSetupView(
                settings_script=[
                    worker_setup.SetupFields(
                        ollama_enabled=True, resource_id="tray-ollama"
                    )
                ]
            )
            factory = recording_view_factory(view, actions)

            def click_settings_then_quit() -> None:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not actions.get("on_settings"):
                    _ = threading.Event().wait(0.01)
                on_settings = cast("Callable[[], None]", actions["on_settings"])
                on_settings()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and len(made) < 2:
                    _ = threading.Event().wait(0.01)
                model = cast("windows_tray.TrayStateModel", actions["model"])
                model.request_quit()

            threading.Thread(target=click_settings_then_quit, daemon=True).start()
            exit_code = windows_tray.tray_main(
                ["--state-dir", parent],
                view_factory=factory,
                setup_view_factory=lambda: setup,
                runtime_factory=runtime_factory,
            )
            reopened = open_store(parent)
            try:
                settings = worker_setup.load_worker_settings(reopened)
            finally:
                reopened.close()
        self.assertEqual(0, exit_code)
        # The restart machinery built a SECOND runtime; the first was
        # asked to stop (never run concurrently with its replacement).
        self.assertEqual(2, len(made))
        self.assertGreaterEqual(made[0].stop_requests, 1)
        self.assertTrue(view.started)
        self.assertTrue(view.stopped)
        # The save went through the SAME settings path and persisted.
        assert settings is not None
        assert settings.ollama is not None
        self.assertEqual("tray-ollama", settings.ollama.resource_id)
        # The dialog received the paired context (no pairing section).
        self.assertEqual(1, len(setup.settings_contexts))
        self.assertEqual("worker-paired-1", setup.settings_contexts[0].worker_id)

    def test_settings_cancel_changes_nothing(self) -> None:
        made: list[FakeRuntime] = []

        def runtime_factory() -> FakeRuntime:
            runtime = FakeRuntime(immediate=False)
            made.append(runtime)
            return runtime

        with tempfile.TemporaryDirectory() as parent:
            self._paired_tree(parent)
            actions: dict[str, object] = {}
            setup = FakeSetupView(settings_script=[None])
            factory = recording_view_factory(RecordingView(), actions)

            def click_settings_then_quit() -> None:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not actions.get("on_settings"):
                    _ = threading.Event().wait(0.01)
                on_settings = cast("Callable[[], None]", actions["on_settings"])
                on_settings()  # scripted cancel: returns None
                model = cast("windows_tray.TrayStateModel", actions["model"])
                model.request_quit()

            threading.Thread(target=click_settings_then_quit, daemon=True).start()
            exit_code = windows_tray.tray_main(
                [],
                state_dir=parent,
                view_factory=factory,
                setup_view_factory=lambda: setup,
                runtime_factory=runtime_factory,
            )
            reopened = open_store(parent)
            try:
                settings = worker_setup.load_worker_settings(reopened)
            finally:
                reopened.close()
        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(made))
        self.assertIsNone(settings)

    def test_malformed_settings_reach_the_dialog_as_a_problem(self) -> None:
        made: list[FakeRuntime] = []

        def runtime_factory() -> FakeRuntime:
            runtime = FakeRuntime(immediate=False)
            made.append(runtime)
            return runtime

        with tempfile.TemporaryDirectory() as parent:
            self._paired_tree(parent)
            store = open_store(parent)
            store.save_value(worker_setup.SETTINGS_STORE_KEY, "{broken")
            store.close()
            actions: dict[str, object] = {}
            setup = FakeSetupView(settings_script=[None])
            factory = recording_view_factory(RecordingView(), actions)

            def click_settings_then_quit() -> None:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not actions.get("on_settings"):
                    _ = threading.Event().wait(0.01)
                on_settings = cast("Callable[[], None]", actions["on_settings"])
                on_settings()  # scripted cancel after reading the context
                model = cast("windows_tray.TrayStateModel", actions["model"])
                model.request_quit()

            threading.Thread(target=click_settings_then_quit, daemon=True).start()
            exit_code = windows_tray.tray_main(
                [],
                state_dir=parent,
                view_factory=factory,
                setup_view_factory=lambda: setup,
                runtime_factory=runtime_factory,
            )
        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(setup.settings_contexts))
        context = setup.settings_contexts[0]
        self.assertIsNone(context.current)
        assert context.problem is not None
        self.assertIn("not valid JSON", context.problem)


# ── Honest Windows Codex posture ──────────────────────────────────────────────


class WindowsCodexNotAdvertisedTests(unittest.TestCase):
    def test_settings_schema_rejects_codex_fields(self) -> None:
        text = (
            '{"schema_version": 1, "server_ui_url": null, "ollama": null, '
            + '"codex": {"resource_id": "c"}}'
        )
        with self.assertRaises(worker_setup.WorkerSetupConfigError):
            _ = worker_setup.settings_from_json(text)

    def test_setup_dialog_source_never_mentions_codex(self) -> None:
        # Windows-native Codex execution is platform_not_evidenced for
        # v0.1.0; the GUI must not even mention it as an option. The
        # dialog's source contains no codex token at all.
        source = SETUP_VIEW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("codex", source.lower())


if __name__ == "__main__":
    _ = unittest.main()
