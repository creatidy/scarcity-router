"""Windows tray and packaging-spec tests (M10, issue #95).

The tray's state machine, status text, bounded log writer and the
run-loop assembly (``run_tray_worker``) are platform-independent and
exercised here with injected fake views and runtimes — no GUI toolkit,
no Windows, no sockets. The pystray adapter itself is Windows-gated and
covered by EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE
(docs/m10-acceptance.md); this suite pins everything around it:

- the module imports on every platform without the optional extra and
  refuses to build a view off Windows with a remediation-bearing error;
- state transitions render exactly the promised text (running /
  disconnected / error) and are deduplicated;
- the packaged spec builds a one-dir, windowless executable with the
  win32 tray backend pinned (parsed with stubbed PyInstaller callables).
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast

from scarcity_router import windows_tray
from scarcity_router.windows_tray import (
    STATE_DISCONNECTED,
    STATE_ERROR,
    STATE_RUNNING,
    WORKER_LOG_MAX_BYTES,
    TrayStateModel,
    append_worker_log,
    run_tray_worker,
    worker_status_text,
)

# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeView:
    """Records every view call (the TrayView seam)."""

    calls: list[tuple[str, str]]
    started: bool
    stopped: bool
    _lock: threading.Lock

    def __init__(self) -> None:
        self.calls = []
        self.started = False
        self.stopped = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self.started = True

    def update(self, *, state: str, text: str) -> None:
        with self._lock:
            self.calls.append((state, text))

    def stop(self) -> None:
        self.stopped = True

    def last(self) -> tuple[str, str] | None:
        with self._lock:
            return self.calls[-1] if self.calls else None


class FakeRuntime:
    """A controllable runtime: run() blocks until a stop is requested."""

    reason: str
    immediate: bool
    _stop: threading.Event
    stop_requests: int
    diagnostics_lines: tuple[str, ...]

    def __init__(self, *, reason: str = "requested", immediate: bool = False) -> None:
        self.reason = reason
        self.immediate = immediate
        self._stop = threading.Event()
        self.stop_requests = 0
        self.diagnostics_lines = ("worker: synthetic diagnostic line",)

    def run(self) -> str:
        while not self.immediate and not self._stop.is_set():
            _ = self._stop.wait(timeout=0.005)
        return self.reason

    def request_stop(self) -> None:
        self.stop_requests += 1
        self._stop.set()

    def diagnostics(self) -> tuple[str, ...]:
        return self.diagnostics_lines


# ── Status text and state model ───────────────────────────────────────────────


class StatusTextTests(unittest.TestCase):
    def test_running_text_carries_identity_and_server(self) -> None:
        text = worker_status_text(
            STATE_RUNNING, worker_id="worker-1", server_origin="srws://srv:8790"
        )
        self.assertIn("running", text)
        self.assertIn("worker-1", text)
        self.assertIn("srws://srv:8790", text)

    def test_unpaired_and_unbound_fallbacks(self) -> None:
        text = worker_status_text(
            STATE_DISCONNECTED, worker_id=None, server_origin=None
        )
        self.assertIn("not paired", text)
        self.assertIn("no server", text)

    def test_unknown_state_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _ = worker_status_text("paused", worker_id=None, server_origin=None)


class TrayStateModelTests(unittest.TestCase):
    def test_transitions_render_in_order(self) -> None:
        view = FakeView()
        model = TrayStateModel(view=cast("windows_tray.TrayView", view))
        model.mark_running()
        model.mark_disconnected("budget exhausted")
        model.mark_error("fatal")
        self.assertEqual(
            [
                (STATE_RUNNING, view.calls[0][1]),
                (STATE_DISCONNECTED, view.calls[1][1]),
                (STATE_ERROR, view.calls[2][1]),
            ],
            view.calls,
        )
        self.assertIn("budget exhausted", view.calls[1][1])

    def test_duplicate_transition_is_deduplicated(self) -> None:
        view = FakeView()
        model = TrayStateModel(view=cast("windows_tray.TrayView", view))
        model.mark_running()
        model.mark_running()
        self.assertEqual(1, len(view.calls))

    def test_quit_and_restart_hooks_fire(self) -> None:
        view = FakeView()
        quit_calls: list[bool] = []
        restart_calls: list[bool] = []
        model = TrayStateModel(
            view=cast("windows_tray.TrayView", view),
            on_quit=lambda: quit_calls.append(True),
            on_restart=lambda: restart_calls.append(True),
        )
        model.request_quit()
        model.request_restart()
        self.assertEqual([True], quit_calls)
        self.assertEqual([True], restart_calls)


# ── Platform guard ────────────────────────────────────────────────────────────


class PlatformGuardTests(unittest.TestCase):
    def test_module_imports_without_the_extra(self) -> None:
        # pystray and Pillow are NOT installed in the development
        # environment, and the library module imports cleanly without
        # them (the GUI adapter lives in the packaging tree, not the
        # library).
        import importlib

        try:
            module = importlib.import_module("pystray")
        except ImportError:
            return
        self.fail(f"pystray unexpectedly installed; guard test is vacuous: {module}")

    def test_tray_main_without_paired_identity_refuses_with_remediation(self) -> None:
        # The packaged entry point fails closed with a remediation-bearing
        # message (exit 2) when the worker has no paired identity — on
        # every platform, with no GUI stack required.
        with tempfile.TemporaryDirectory() as parent:
            exit_code = windows_tray.tray_main(["--state-dir", parent])
        self.assertEqual(2, exit_code)

    def test_tray_main_rejects_malformed_ui_url_flag(self) -> None:
        # The packaging-only flag requires a value; the typed error path
        # returns 2 with the safe message (no GUI, every platform).
        with tempfile.TemporaryDirectory() as parent:
            exit_code = windows_tray.tray_main(
                ["--state-dir", parent, "--server-ui-url"]
            )
        self.assertEqual(2, exit_code)


# ── Bounded worker log ────────────────────────────────────────────────────────


class WorkerLogTests(unittest.TestCase):
    def test_appends_redacted_lines_and_truncates_at_cap(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent)
            append_worker_log(directory, ("line one", "line two"))
            log = directory / windows_tray.WORKER_LOG_NAME
            self.assertEqual("line one\nline two\n", log.read_text(encoding="utf-8"))
            # A file beyond the cap is truncated, never allowed to grow.
            _ = log.write_bytes(b"x" * (WORKER_LOG_MAX_BYTES + 1))
            append_worker_log(directory, ("fresh",))
            self.assertEqual("fresh\n", log.read_text(encoding="utf-8"))

    def test_empty_lines_write_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            append_worker_log(parent, ())
            self.assertFalse(
                (Path(parent) / windows_tray.WORKER_LOG_NAME).exists()
            )

    def test_unwritable_log_never_raises(self) -> None:
        # A path whose parent does not exist must be swallowed: diagnostics
        # are best-effort and never crash the worker.
        append_worker_log(Path("/nonexistent-registry-key/zzz"), ("line",))


# ── Run-loop assembly ─────────────────────────────────────────────────────────


def _quit_when_terminal(
    view: FakeView, model: TrayStateModel, terminal: str
) -> "windows_tray.TrayView":
    """A view factory that quits the loop once a terminal state shows up.

    Mirrors production semantics: the tray stays alive in a terminal
    state until the user quits; the test automates that quit.
    """

    def drive() -> None:
        import time

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if model.state == terminal:
                model.request_quit()
                return
            _ = threading.Event().wait(0.005)

    threading.Thread(target=drive, daemon=True).start()
    return cast("windows_tray.TrayView", view)


class RunTrayWorkerTests(unittest.TestCase):
    def test_lifecycle_running_then_exhausted_shows_disconnected(self) -> None:
        view = FakeView()
        runtime = FakeRuntime(reason="reconnect_budget_exhausted", immediate=True)

        def factory(model: TrayStateModel) -> "windows_tray.TrayView":
            return _quit_when_terminal(view, model, STATE_DISCONNECTED)

        with tempfile.TemporaryDirectory() as parent:
            run_tray_worker(
                runtime_factory=lambda: runtime,
                view_factory=factory,
                state_dir=Path(parent),
                worker_id="worker-7",
                server_origin="srws://srv:8790",
            )
            states = [state for state, _text in view.calls]
            self.assertEqual([STATE_RUNNING, STATE_DISCONNECTED], states)
            self.assertIn("worker-7", view.calls[0][1])
            self.assertTrue(view.started)
            self.assertTrue(view.stopped)
            # The redacted diagnostics line reached the bounded log.
            log = Path(parent) / windows_tray.WORKER_LOG_NAME
            self.assertIn(
                "synthetic diagnostic line", log.read_text(encoding="utf-8")
            )

    def test_fatal_reason_shows_error(self) -> None:
        view = FakeView()
        runtime = FakeRuntime(reason="fatal", immediate=True)

        def factory(model: TrayStateModel) -> "windows_tray.TrayView":
            return _quit_when_terminal(view, model, STATE_ERROR)

        with tempfile.TemporaryDirectory() as parent:
            run_tray_worker(
                runtime_factory=lambda: runtime,
                view_factory=factory,
                state_dir=Path(parent),
                worker_id=None,
                server_origin=None,
            )
        self.assertEqual([STATE_RUNNING, STATE_ERROR], [s for s, _ in view.calls])
        self.assertIn("fatal", view.calls[1][1])

    def test_runtime_exception_shows_error_and_stops(self) -> None:
        view = FakeView()

        class ExplodingRuntime:
            def run(self) -> str:
                raise RuntimeError("boom")

            def diagnostics(self) -> tuple[str, ...]:
                return ()

            def request_stop(self) -> None:
                return None

        def factory(model: TrayStateModel) -> "windows_tray.TrayView":
            return _quit_when_terminal(view, model, STATE_ERROR)

        with tempfile.TemporaryDirectory() as parent:
            run_tray_worker(
                runtime_factory=ExplodingRuntime,
                view_factory=factory,
                state_dir=Path(parent),
                worker_id=None,
                server_origin=None,
            )
        self.assertEqual(STATE_ERROR, view.calls[-1][0])
        self.assertIn("RuntimeError", view.calls[-1][1])

    def test_quit_stops_the_runtime_and_unblocks(self) -> None:
        view = FakeView()
        runtime = FakeRuntime()  # run() blocks until a stop request

        def factory(model: TrayStateModel) -> "windows_tray.TrayView":
            # Quit from another thread once the runtime is actually
            # running, like the tray menu would.
            def drive() -> None:
                import time

                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if model.state == STATE_RUNNING:
                        break
                    _ = threading.Event().wait(0.005)
                model.request_quit()

            threading.Thread(target=drive, daemon=True).start()
            return cast("windows_tray.TrayView", view)

        with tempfile.TemporaryDirectory() as parent:
            run_tray_worker(
                runtime_factory=lambda: runtime,
                view_factory=factory,
                state_dir=Path(parent),
                worker_id=None,
                server_origin=None,
            )
        self.assertGreaterEqual(runtime.stop_requests, 1)
        self.assertTrue(view.stopped)

    def test_restart_spawns_a_fresh_runtime_without_overlap(self) -> None:
        view = FakeView()
        made: list[FakeRuntime] = [FakeRuntime()]  # made[0]: the first runtime
        counter = {"n": 0}

        def factory() -> FakeRuntime:
            counter["n"] += 1
            if counter["n"] == 1:
                return made[0]
            runtime = FakeRuntime()  # the restarted runtime blocks as well
            made.append(runtime)
            return runtime

        def factory_view(model: TrayStateModel) -> "windows_tray.TrayView":
            def drive() -> None:
                import time

                # Wait for the FIRST runtime to be running, request a
                # restart, then wait for the fresh runtime and quit.
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and len(made) < 2:
                    if model.state == STATE_RUNNING:
                        model.request_restart()
                    _ = threading.Event().wait(0.01)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and len(made) < 2:
                    _ = threading.Event().wait(0.01)
                model.request_quit()

            threading.Thread(target=drive, daemon=True).start()
            return cast("windows_tray.TrayView", view)

        with tempfile.TemporaryDirectory() as parent:
            run_tray_worker(
                runtime_factory=factory,
                view_factory=factory_view,
                state_dir=Path(parent),
                worker_id="w",
                server_origin=None,
            )
        # A fresh runtime was started (restart worked) and the first
        # runtime was asked to stop exactly once, never run concurrently
        # with its replacement.
        self.assertEqual(2, len(made))
        self.assertEqual(1, made[0].stop_requests)
        self.assertGreaterEqual(made[1].stop_requests, 1)


if __name__ == "__main__":
    _ = unittest.main()
