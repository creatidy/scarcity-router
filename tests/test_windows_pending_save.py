"""Event-driven coverage of settings saves crossing tray shutdown."""

from __future__ import annotations

import importlib.util
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast, override
from unittest.mock import patch

from scarcity_router import windows_tray, worker_setup
from scarcity_router.worker_client import open_worker_store
from scarcity_router.worker_local_store import WorkerLocalStore
from tests.test_worker_first_run import (
    FakeRuntime,
    RecordingView,
    open_store,
    recording_view_factory,
    synthetic_identity,
)


class _Dialog(Protocol):
    _in_flight: bool
    _mode: str
    _save: object
    _results: queue.Queue[object]
    _root: object
    _close_request: threading.Event
    _collect_fields: Callable[[], worker_setup.SetupFields]
    _set_inputs_enabled: Callable[[bool], None]
    _show_status: Callable[..., None]
    result: object

    def _on_primary(self) -> None: ...
    def _poll_close(self) -> None: ...


def _dialog_class() -> type[_Dialog]:
    path = Path(__file__).resolve().parents[1] / "packaging/windows/scarcity_worker_setup_view.py"
    spec = importlib.util.spec_from_file_location("pending_save_adapter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(type[_Dialog], getattr(module, "_SetupDialog"))


class PendingSaveTests(unittest.TestCase):
    def test_save_restarts_after_terminal_state_and_during_transition(self) -> None:
        for mode in ("exhausted", "error", "transition"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as parent:
                self._exercise_terminal_restart(parent, mode)

    def _exercise_terminal_restart(self, parent: str, mode: str) -> None:
        ready, terminal, release, second_started, finished, restart_called = (
            threading.Event() for _ in range(6)
        )
        actions: dict[str, object] = {}
        captured: list[worker_setup.WorkerLocalSettings | None] = []
        result: queue.Queue[object] = queue.Queue()
        old = worker_setup.WorkerLocalSettings(server_ui_url="http://127.0.0.1:43210")
        new = worker_setup.WorkerLocalSettings(server_ui_url="http://127.0.0.1:43211")
        calls: list[int] = []
        original_restart = windows_tray.TrayStateModel.request_restart

        def restart(model: windows_tray.TrayStateModel) -> None:
            original_restart(model)
            restart_called.set()

        class View:
            def arm_close_request(self, event: threading.Event) -> None:
                _ = event

            def run_first_run(self, submit: object) -> None:
                _ = submit
                raise AssertionError("unexpected first run")

            def run_settings(self, context: object, save: Callable[[worker_setup.SetupFields], worker_setup.WorkerLocalSettings]) -> worker_setup.WorkerLocalSettings:
                _ = context
                return save(worker_setup.SetupFields(server_ui_url="http://127.0.0.1:43211"))

        class TerminalView(RecordingView):
            @override
            def update(self, *, state: str, text: str) -> None:
                super().update(state=state, text=text)
                if state in (windows_tray.STATE_DISCONNECTED, windows_tray.STATE_ERROR):
                    terminal.set()
                    if mode == "transition" and not release.wait(10):
                        raise AssertionError("terminal transition release timed out")

        def factory(model: windows_tray.TrayStateModel, opener: Callable[[], None], directory: Path, on_settings: Callable[[], None]) -> RecordingView:
            view = recording_view_factory(TerminalView(), actions)(model, opener, directory, on_settings)
            ready.set()
            return view

        store = open_store(parent)
        store.save_identity(synthetic_identity())
        worker_setup.save_worker_settings(store, old)
        store.close()

        def runtime_factory() -> FakeRuntime:
            calls.append(1)
            if len(calls) == 1:
                if mode == "error":
                    raise ValueError("synthetic runtime failure")
                return FakeRuntime(reason="reconnect_budget_exhausted")
            current_store = open_store(parent)
            try:
                captured.append(worker_setup.load_worker_settings(current_store))
            finally:
                current_store.close()
            second_started.set()
            return FakeRuntime(immediate=False)

        def run() -> None:
            try:
                result.put(windows_tray.tray_main(["--state-dir", parent], view_factory=factory, setup_view_factory=View, runtime_factory=runtime_factory))
            except BaseException as exc:
                result.put(exc)
            finally:
                finished.set()

        main = threading.Thread(target=run)
        restart_patch = patch.object(windows_tray.TrayStateModel, "request_restart", restart)
        _ = restart_patch.start()
        main.start()
        try:
            self.assertTrue(ready.wait(10))
            self.assertTrue(terminal.wait(10))
            cast(Callable[[], None], actions["on_settings"])()
            self.assertTrue(restart_called.wait(10))
            release.set()
            self.assertTrue(second_started.wait(10))
            self.assertEqual([new], captured)
            cast(windows_tray.TrayStateModel, actions["model"]).request_quit()
            self.assertTrue(finished.wait(10))
            self.assertEqual(0, result.get_nowait())
        finally:
            release.set()
            if "model" in actions:
                cast(windows_tray.TrayStateModel, actions["model"]).request_quit()
            main.join(10)
            restart_patch.stop()

    def test_save_during_runtime_construction_restarts_with_new_settings(self) -> None:
        ready, entered, release, restart_called, started, finished = (
            threading.Event() for _ in range(6)
        )
        actions: dict[str, object] = {}
        captured: list[worker_setup.WorkerLocalSettings | None] = []
        runtimes: list[FakeRuntime] = []
        result: queue.Queue[object] = queue.Queue()
        original_restart = windows_tray.TrayStateModel.request_restart
        old = worker_setup.WorkerLocalSettings(server_ui_url="http://127.0.0.1:43210")
        new = worker_setup.WorkerLocalSettings(server_ui_url="http://127.0.0.1:43211")

        def restart(model: windows_tray.TrayStateModel) -> None:
            original_restart(model)
            restart_called.set()

        class View:
            def arm_close_request(self, event: threading.Event) -> None:
                _ = event

            def run_first_run(self, submit: object) -> None:
                _ = submit
                raise AssertionError("unexpected first run")

            def run_settings(self, context: object, save: Callable[[worker_setup.SetupFields], worker_setup.WorkerLocalSettings]) -> worker_setup.WorkerLocalSettings:
                _ = context
                return save(worker_setup.SetupFields(server_ui_url="http://127.0.0.1:43211"))

        def factory(model: windows_tray.TrayStateModel, opener: Callable[[], None], directory: Path, on_settings: Callable[[], None]) -> RecordingView:
            view = recording_view_factory(RecordingView(), actions)(model, opener, directory, on_settings)
            ready.set()
            return view

        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            store.save_identity(synthetic_identity())
            worker_setup.save_worker_settings(store, old)
            store.close()

            def runtime_factory() -> FakeRuntime:
                current_store = open_store(parent)
                try:
                    captured.append(worker_setup.load_worker_settings(current_store))
                finally:
                    current_store.close()
                runtime = FakeRuntime(immediate=False)
                runtimes.append(runtime)
                if len(runtimes) == 1:
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("runtime construction timed out")
                else:
                    started.set()
                return runtime

            def run() -> None:
                try:
                    result.put(windows_tray.tray_main(["--state-dir", parent], view_factory=factory, setup_view_factory=View, runtime_factory=runtime_factory))
                except BaseException as exc:
                    result.put(exc)
                finally:
                    finished.set()

            with patch.object(windows_tray.TrayStateModel, "request_restart", restart):
                main = threading.Thread(target=run)
                main.start()
                try:
                    self.assertTrue(ready.wait(10))
                    self.assertTrue(entered.wait(10))
                    cast(Callable[[], None], actions["on_settings"])()
                    self.assertTrue(restart_called.wait(10))
                    release.set()
                    self.assertTrue(started.wait(10))
                    self.assertEqual([old, new], captured)
                    self.assertEqual(1, runtimes[0].stop_requests)
                    cast(windows_tray.TrayStateModel, actions["model"]).request_quit()
                    self.assertTrue(finished.wait(10))
                    self.assertEqual(0, result.get_nowait())
                finally:
                    release.set()
                    if "model" in actions:
                        cast(windows_tray.TrayStateModel, actions["model"]).request_quit()
                    main.join(10)

    def test_quit_racing_runtime_publication_stops_runtime(self) -> None:
        ready, factory_done, checked, release, quitting, finished = (
            threading.Event() for _ in range(6)
        )
        actions: dict[str, object] = {}
        runtime = FakeRuntime(immediate=False)
        original_is_set = threading.Event.is_set

        def is_set(event: threading.Event) -> bool:
            result = original_is_set(event)
            if (
                threading.current_thread().name == "scarcity-router-worker"
                and original_is_set(factory_done)
                and not original_is_set(checked)
            ):
                checked.set()
                if not release.wait(10):
                    raise AssertionError("runtime publication release timed out")
            return result

        def runtime_factory() -> FakeRuntime:
            factory_done.set()
            return runtime

        def factory(model: windows_tray.TrayStateModel, opener: Callable[[], None], directory: Path, on_settings: Callable[[], None]) -> RecordingView:
            view = recording_view_factory(RecordingView(), actions)(model, opener, directory, on_settings)
            ready.set()
            return view

        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            store.save_identity(synthetic_identity())
            store.close()

            def run() -> None:
                try:
                    _ = windows_tray.tray_main(["--state-dir", parent], view_factory=factory, runtime_factory=runtime_factory)
                finally:
                    finished.set()

            def quit_worker() -> None:
                quitting.set()
                cast(windows_tray.TrayStateModel, actions["model"]).request_quit()

            with patch.object(threading.Event, "is_set", is_set):
                main = threading.Thread(target=run)
                main.start()
                quit_thread = threading.Thread(target=quit_worker)
                try:
                    self.assertTrue(ready.wait(10))
                    self.assertTrue(checked.wait(10))
                    quit_thread.start()
                    self.assertTrue(quitting.wait(10))
                    release.set()
                    self.assertTrue(finished.wait(10))
                    quit_thread.join(10)
                    main.join(10)
                    self.assertGreaterEqual(runtime.stop_requests, 1)
                    self.assertFalse(any(thread.name == "scarcity-router-worker" for thread in threading.enumerate()))
                finally:
                    release.set()
                    main.join(10)
                    if quit_thread.ident is not None:
                        quit_thread.join(10)

    def test_process_exit_reports_unconfirmed_save(self) -> None:
        program = '''
import queue, sys, threading
from unittest.mock import patch
from scarcity_router import windows_tray, worker_setup
from tests.test_windows_pending_save import _dialog_class
from tests.test_worker_first_run import FakeRuntime, RecordingView, open_store, recording_view_factory, synthetic_identity

directory = sys.argv[1]
mode = sys.argv[2]
entered, ready, write_release = threading.Event(), threading.Event(), threading.Event()
actions = {}
store = open_store(directory)
store.save_identity(synthetic_identity())
worker_setup.save_worker_settings(store, worker_setup.WorkerLocalSettings(server_ui_url="http://127.0.0.1:43210"))
store.close()

def blocked_save(fields, *, store, begin_write=None):
    if mode == "failure":
        raise worker_setup.WorkerSetupConfigError("synthetic failure")
    if mode == "committed":
        def admit():
            accepted = begin_write()
            entered.set()
            assert write_release.wait(10)
            return accepted
        return worker_setup.save_settings_fields(fields, store=store, begin_write=admit)
    entered.set()
    threading.Event().wait()
    raise AssertionError("unreachable")

write_log = windows_tray._try_append_worker_log
def block_failure_log(directory, lines):
    if mode == "failure" and lines[0].startswith("settings save failed"):
        entered.set()
        threading.Event().wait()
    return write_log(directory, lines)

class Root:
    def after(self, ms, callback): pass
    def destroy(self): pass

class View:
    def arm_close_request(self, event): self.event = event
    def run_first_run(self, submit): raise AssertionError("first run")
    def run_settings(self, context, save):
        cls = _dialog_class()
        dialog = cls.__new__(cls)
        dialog._in_flight = False
        dialog._mode = "settings"
        dialog._save = save
        dialog._results = queue.Queue()
        dialog._root = Root()
        dialog._close_request = self.event
        dialog._collect_fields = lambda: worker_setup.SetupFields(server_ui_url="http://127.0.0.1:43211")
        dialog._set_inputs_enabled = lambda enabled: setattr(dialog, "_in_flight", not enabled)
        dialog._show_status = lambda message, *, error: None
        dialog.result = None
        dialog._on_primary()
        assert self.event.wait(10)
        dialog._poll_close()
        return None

def factory(model, opener, path, settings):
    view = recording_view_factory(RecordingView(), actions)(model, opener, path, settings)
    ready.set()
    return view

def quit_when_saving():
    assert ready.wait(10)
    actions["on_settings"]()
    assert entered.wait(10)
    actions["model"].request_quit()
    write_release.set()

shutdown = windows_tray.SettingsStoreLifetime.shutdown
def expire(lifetime, *, timeout=10): return shutdown(lifetime, timeout=timeout if mode == "committed" else 0)
threading.Thread(target=quit_when_saving).start()
with patch.object(windows_tray, "save_settings_fields", blocked_save), patch.object(windows_tray, "_try_append_worker_log", block_failure_log), patch.object(windows_tray.SettingsStoreLifetime, "shutdown", expire):
    result = windows_tray.tray_main(["--state-dir", directory], view_factory=factory, setup_view_factory=View, runtime_factory=lambda: FakeRuntime(immediate=False))
assert result == 0
'''
        for mode in ("save", "failure", "committed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as parent:
                completed = subprocess.run(
                    [sys.executable, "-c", program, parent, mode],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                log = (Path(parent) / windows_tray.WORKER_LOG_NAME).read_text()
                if mode == "committed":
                    self.assertIn("settings save committed", log)
                    self.assertNotIn("shutdown timed out", log)
                else:
                    self.assertIn("pending work is unconfirmed and settings may not persist", log)
                store = open_store(parent)
                try:
                    self.assertEqual(
                        worker_setup.WorkerLocalSettings(server_ui_url="http://127.0.0.1:43211" if mode == "committed" else "http://127.0.0.1:43210"),
                        worker_setup.load_worker_settings(store),
                    )
                finally:
                    store.close()

    def test_delayed_runtime_keeps_store_open_after_shutdown(self) -> None:
        entered, release, ready, finished, closed = (threading.Event() for _ in range(5))
        actions: dict[str, object] = {}
        results: queue.Queue[object] = queue.Queue()
        stores: list[WorkerLocalStore] = []
        original_open = open_worker_store
        original_close = WorkerLocalStore.close
        original_join = threading.Thread.join
        original_shutdown = windows_tray.SettingsStoreLifetime.shutdown
        runtime = FakeRuntime(immediate=False)

        def open_session_store(directory: str | None) -> WorkerLocalStore:
            store = original_open(directory)
            stores.append(store)
            return store

        def observe_close(store: WorkerLocalStore) -> None:
            original_close(store)
            if stores and store is stores[0]:
                closed.set()

        def join(thread: threading.Thread, timeout: float | None = None) -> None:
            original_join(thread, timeout=0 if thread.name == "scarcity-router-worker" else timeout)

        def expire(lifetime: windows_tray.SettingsStoreLifetime, *, timeout: float = 10.0) -> bool:
            _ = timeout
            return original_shutdown(lifetime, timeout=0)

        def runtime_factory() -> FakeRuntime:
            entered.set()
            if not release.wait(10):
                raise AssertionError("runtime release timed out")
            self.assertIsNotNone(stores[0].load_identity())
            return runtime

        def factory(model: windows_tray.TrayStateModel, opener: Callable[[], None], directory: Path, on_settings: Callable[[], None]) -> RecordingView:
            view = recording_view_factory(RecordingView(), actions)(model, opener, directory, on_settings)
            ready.set()
            return view

        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            store.save_identity(synthetic_identity())
            store.close()

            def run() -> None:
                try:
                    results.put(windows_tray.tray_main(["--state-dir", parent], view_factory=factory, runtime_factory=runtime_factory))
                except BaseException as exc:
                    results.put(exc)
                finally:
                    finished.set()

            with patch.object(windows_tray, "open_worker_store", open_session_store), patch.object(WorkerLocalStore, "close", observe_close), patch.object(threading.Thread, "join", join), patch.object(windows_tray.SettingsStoreLifetime, "shutdown", expire):
                main = threading.Thread(target=run)
                main.start()
                try:
                    self.assertTrue(ready.wait(10))
                    self.assertTrue(entered.wait(10))
                    workers = [thread for thread in threading.enumerate() if thread.name == "scarcity-router-worker"]
                    self.assertEqual(1, len(workers))
                    cast(windows_tray.TrayStateModel, actions["model"]).request_quit()
                    self.assertTrue(finished.wait(10))
                    self.assertEqual(0, results.get_nowait())
                    self.assertFalse(closed.is_set())
                    release.set()
                    self.assertTrue(closed.wait(10))
                    original_join(workers[0], timeout=10)
                    self.assertFalse(workers[0].is_alive())
                    self.assertEqual(1, runtime.stop_requests)
                finally:
                    release.set()
                    original_join(main, timeout=10)

    def test_shutdown_defers_close_until_save_finishes(self) -> None:
        self._exercise_shutdown(fail=False)

    def test_packaged_dialog_run_releases_ui_owner_on_save_timeout(self) -> None:
        self._exercise_shutdown(fail=False, actual_dialog=True)

    def test_shutdown_reports_late_save_failure(self) -> None:
        self._exercise_shutdown(fail=True)

    def test_quit_after_transaction_completed(self) -> None:
        self._exercise_shutdown(fail=False, commit_first=True)

    def test_admitted_write_commits_after_quit_and_is_reported(self) -> None:
        self._exercise_shutdown(fail=False, commit_after_quit=True)

    def test_successful_save_before_quit_restarts_runtime(self) -> None:
        self._exercise_shutdown(fail=False, finish_first=True)

    def test_failed_diagnostic_changes_shutdown_disposition(self) -> None:
        self._exercise_shutdown(fail=True, report_failure_first=True)

    def _exercise_shutdown(
        self,
        *,
        fail: bool,
        commit_first: bool = False,
        finish_first: bool = False,
        report_failure_first: bool = False,
        actual_dialog: bool = False,
        commit_after_quit: bool = False,
    ) -> None:
        entered, release, closed, ready, finished, save_done = (
            threading.Event() for _ in range(6)
        )
        actions: dict[str, object] = {}
        results: queue.Queue[object] = queue.Queue()
        dialogs: list[_Dialog] = []
        save_store: list[WorkerLocalStore] = []
        store_closed = threading.Event()
        poll_requested = threading.Event()
        restarted = threading.Event()
        runtimes: list[FakeRuntime] = []
        original_save = worker_setup.save_settings_fields
        original_close = WorkerLocalStore.close
        original_shutdown = windows_tray.SettingsStoreLifetime.shutdown
        original_join = threading.Thread.join
        fields = worker_setup.SetupFields(server_ui_url="http://127.0.0.1:43211")
        old = worker_setup.WorkerLocalSettings(server_ui_url="http://127.0.0.1:43210")

        def runtime_factory() -> FakeRuntime:
            runtime = FakeRuntime(immediate=False)
            runtimes.append(runtime)
            if len(runtimes) == 2:
                restarted.set()
            return runtime

        def blocked_save(
            value: worker_setup.SetupFields,
            *,
            store: WorkerLocalStore,
            begin_write: Callable[[], bool] | None = None,
        ) -> worker_setup.WorkerLocalSettings:
            save_store.append(store)
            if commit_after_quit:
                def admit() -> bool:
                    assert begin_write is not None
                    accepted = begin_write()
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("admitted write release timed out")
                    return accepted

                try:
                    return original_save(value, store=store, begin_write=admit)
                finally:
                    save_done.set()
            committed = original_save(value, store=store, begin_write=begin_write) if commit_first else None
            entered.set()
            if not release.wait(10):
                raise AssertionError("save release timed out")
            try:
                if fail:
                    raise worker_setup.WorkerSetupConfigError("synthetic save failure")
                return committed if committed is not None else original_save(value, store=store, begin_write=begin_write)
            finally:
                save_done.set()

        def observe_close(store: WorkerLocalStore) -> None:
            if save_store and store is save_store[0]:
                self.assertTrue(save_done.is_set(), "store closed before save completed")
                original_close(store)
                store_closed.set()
            else:
                original_close(store)

        def expire(lifetime: windows_tray.SettingsStoreLifetime, *, timeout: float = 10.0) -> bool:
            return original_shutdown(lifetime, timeout=timeout if commit_after_quit else 0)

        def join(thread: threading.Thread, timeout: float | None = None) -> None:
            original_join(thread, timeout=0 if actual_dialog and thread.name == "worker-settings" else timeout)

        class Root:
            def title(self, title: str) -> None:
                _ = title

            def resizable(self, width: bool, height: bool) -> None:
                _ = width, height

            def protocol(self, name: str, callback: Callable[[], None]) -> None:
                _ = name, callback

            def after(self, ms: int, callback: Callable[[], None]) -> None:
                _ = ms, callback

            def destroy(self) -> None:
                closed.set()

            def mainloop(self) -> None:
                dialog = dialogs[-1]
                cast(Callable[[], None], getattr(dialog, "_on_primary"))()
                event = cast(threading.Event, getattr(dialog, "_close_request"))
                if not event.wait(10):
                    raise AssertionError("dialog close signal timed out")
                cast(Callable[[], None], getattr(dialog, "_poll_close"))()

        class Widget:
            def __init__(self, *args: object, **kwargs: object) -> None:
                _ = args, kwargs

            def grid(self, **kwargs: object) -> None:
                _ = kwargs

            def columnconfigure(self, column: int, *, weight: int) -> None:
                _ = column, weight

        class Tk:
            @staticmethod
            def Tk() -> Root:
                return Root()

        class Ttk:
            Frame: type[Widget] = Widget
            Label: type[Widget] = Widget

        class View:
            def __init__(self) -> None:
                self.close_event: threading.Event = threading.Event()

            def arm_close_request(self, event: threading.Event) -> None:
                self.close_event = event

            def run_first_run(self, submit: object) -> None:
                _ = submit
                raise AssertionError("unexpected first run")

            def run_settings(self, context: object, save: object) -> worker_setup.WorkerLocalSettings | None:
                _ = context
                dialog_type = _dialog_class()
                dialog = dialog_type.__new__(dialog_type)
                setattr(dialog, "_in_flight", False)
                setattr(dialog, "_mode", "settings")
                setattr(dialog, "_save", save)
                setattr(dialog, "_results", queue.Queue[object]())
                setattr(dialog, "_root", Root())
                setattr(dialog, "_close_request", self.close_event)
                setattr(dialog, "_collect_fields", lambda: fields)
                def set_inputs(enabled: bool) -> None:
                    setattr(dialog, "_in_flight", not enabled)

                def show_status(message: str, *, error: bool) -> None:
                    _ = message, error

                setattr(dialog, "_set_inputs_enabled", set_inputs)
                setattr(dialog, "_show_status", show_status)
                dialog.result = None
                dialogs.append(dialog)
                if actual_dialog:
                    def build_rows(body: object, row: int, first: bool) -> int:
                        _ = body, first
                        return row

                    setattr(dialog, "_tk", Tk)
                    setattr(dialog, "_ttk", Ttk)
                    setattr(dialog, "_context", context)
                    setattr(dialog, "_save_thread", None)
                    setattr(dialog, "_build_rows", build_rows)
                    setattr(dialog, "_build_status_and_buttons", build_rows)
                    setattr(dialog, "_sync_ollama_state", lambda: None)
                    setattr(dialog, "_on_cancel", lambda: None)
                    return cast(worker_setup.WorkerLocalSettings | None, cast(Callable[[], object], getattr(dialog, "run"))())
                cast(Callable[[], None], getattr(dialog, "_on_primary"))()
                if finish_first or report_failure_first:
                    if not poll_requested.wait(10):
                        raise AssertionError("result poll timed out")
                    cast(Callable[[], None], getattr(dialog, "_poll_results"))()
                    return cast(worker_setup.WorkerLocalSettings | None, dialog.result)
                if not self.close_event.wait(10):
                    raise AssertionError("close signal timed out")
                cast(Callable[[], None], getattr(dialog, "_poll_close"))()
                return None

        def factory(model: windows_tray.TrayStateModel, opener: Callable[[], None], directory: Path, on_settings: Callable[[], None]) -> RecordingView:
            view = recording_view_factory(RecordingView(), actions)(model, opener, directory, on_settings)
            ready.set()
            return view

        with tempfile.TemporaryDirectory() as parent:
            store = open_store(parent)
            store.save_identity(synthetic_identity())
            worker_setup.save_worker_settings(store, old)
            store.close()
            if report_failure_first:
                (Path(parent) / windows_tray.WORKER_LOG_NAME).mkdir()

            def run() -> None:
                try:
                    results.put(windows_tray.tray_main(["--state-dir", parent], view_factory=factory, setup_view_factory=View, runtime_factory=runtime_factory))
                except BaseException as exc:
                    results.put(exc)
                finally:
                    finished.set()

            with patch.object(windows_tray, "save_settings_fields", blocked_save), patch.object(WorkerLocalStore, "close", observe_close), patch.object(windows_tray.SettingsStoreLifetime, "shutdown", expire), patch.object(threading.Thread, "join", join):
                main = threading.Thread(target=run)
                main.start()
                try:
                    self.assertTrue(ready.wait(10))
                    cast(Callable[[], None], actions["on_settings"])()
                    self.assertTrue(entered.wait(10))
                    save_threads = [thread for thread in threading.enumerate() if thread.name == "worker-settings"]
                    self.assertEqual(1, len(save_threads))
                    if finish_first or report_failure_first:
                        release.set()
                        save_threads[0].join(10)
                        self.assertFalse(save_threads[0].is_alive())
                        poll_requested.set()
                        if finish_first:
                            self.assertTrue(restarted.wait(10))
                    cast(windows_tray.TrayStateModel, actions["model"]).request_quit()
                    if not report_failure_first:
                        self.assertTrue(closed.wait(10))
                    if commit_after_quit:
                        release.set()
                    self.assertTrue(finished.wait(10))
                    self.assertEqual(2 if report_failure_first else 0, results.get_nowait())
                    if actual_dialog:
                        self.assertFalse(any(thread.name == "scarcity-router-ui" for thread in threading.enumerate()))
                    if not (finish_first or report_failure_first or commit_after_quit):
                        self.assertFalse(store_closed.is_set())
                    release.set()
                    self.assertTrue(store_closed.wait(10))
                    original_join(save_threads[0], timeout=10)
                    self.assertFalse(save_threads[0].is_alive())
                    main.join(10)
                    self.assertTrue(cast(queue.Queue[object], getattr(dialogs[0], "_results")).empty())
                finally:
                    release.set()
                    main.join(10)
            reopened = open_store(parent)
            try:
                actual = worker_setup.load_worker_settings(reopened)
                expected = old if fail or not (commit_first or finish_first or commit_after_quit) else worker_setup.WorkerLocalSettings(server_ui_url=fields.server_ui_url)
                self.assertEqual(expected, actual)
            finally:
                reopened.close()
            if report_failure_first:
                self.assertEqual(1, len(runtimes))
                return
            log = (Path(parent) / windows_tray.WORKER_LOG_NAME).read_text()
            if commit_after_quit:
                self.assertIn("settings save committed", log)
                self.assertNotIn("shutdown timed out", log)
                self.assertEqual(1, len(runtimes))
            elif not finish_first:
                self.assertIn("shutdown timed out; pending work is unconfirmed", log)
                self.assertEqual(1, len(runtimes))
            else:
                self.assertEqual(2, len(runtimes))
            if fail or not (commit_first or finish_first or commit_after_quit):
                self.assertIn("settings save failed (WorkerSetupConfigError)", log)
            if actual_dialog:
                self.assertIn("settings dialog ended without confirmed result", log)


if __name__ == "__main__":
    _ = unittest.main()
