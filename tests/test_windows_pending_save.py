"""Event-driven coverage of settings saves crossing tray shutdown."""

from __future__ import annotations

import importlib.util
import queue
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import patch

from scarcity_router import windows_tray, worker_setup
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
    def test_shutdown_defers_close_until_save_finishes(self) -> None:
        self._exercise_shutdown(fail=False)

    def test_shutdown_reports_late_save_failure(self) -> None:
        self._exercise_shutdown(fail=True)

    def test_quit_after_transaction_completed(self) -> None:
        self._exercise_shutdown(fail=False, commit_first=True)

    def test_successful_save_before_quit_restarts_runtime(self) -> None:
        self._exercise_shutdown(fail=False, finish_first=True)

    def _exercise_shutdown(
        self, *, fail: bool, commit_first: bool = False, finish_first: bool = False
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
        fields = worker_setup.SetupFields(server_ui_url="http://127.0.0.1:43211")
        old = worker_setup.WorkerLocalSettings(server_ui_url="http://127.0.0.1:43210")

        def runtime_factory() -> FakeRuntime:
            runtime = FakeRuntime(immediate=False)
            runtimes.append(runtime)
            if len(runtimes) == 2:
                restarted.set()
            return runtime

        def blocked_save(value: worker_setup.SetupFields, *, store: WorkerLocalStore) -> worker_setup.WorkerLocalSettings:
            save_store.append(store)
            committed = original_save(value, store=store) if commit_first else None
            entered.set()
            if not release.wait(10):
                raise AssertionError("save release timed out")
            try:
                if fail:
                    raise worker_setup.WorkerSetupConfigError("synthetic save failure")
                return committed if committed is not None else original_save(value, store=store)
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
            _ = timeout
            return original_shutdown(lifetime, timeout=0)

        class Root:
            def after(self, ms: int, callback: Callable[[], None]) -> None:
                _ = ms, callback

            def destroy(self) -> None:
                closed.set()

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
                cast(Callable[[], None], getattr(dialog, "_on_primary"))()
                if finish_first:
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

            def run() -> None:
                try:
                    results.put(windows_tray.tray_main(["--state-dir", parent], view_factory=factory, setup_view_factory=View, runtime_factory=runtime_factory))
                except BaseException as exc:
                    results.put(exc)
                finally:
                    finished.set()

            with patch.object(windows_tray, "save_settings_fields", blocked_save), patch.object(WorkerLocalStore, "close", observe_close), patch.object(windows_tray.SettingsStoreLifetime, "shutdown", expire):
                main = threading.Thread(target=run)
                main.start()
                try:
                    self.assertTrue(ready.wait(10))
                    cast(Callable[[], None], actions["on_settings"])()
                    self.assertTrue(entered.wait(10))
                    save_threads = [thread for thread in threading.enumerate() if thread.name == "worker-settings"]
                    self.assertEqual(1, len(save_threads))
                    if finish_first:
                        release.set()
                        save_threads[0].join(10)
                        self.assertFalse(save_threads[0].is_alive())
                        poll_requested.set()
                        self.assertTrue(restarted.wait(10))
                    cast(windows_tray.TrayStateModel, actions["model"]).request_quit()
                    self.assertTrue(closed.wait(10))
                    self.assertTrue(finished.wait(10))
                    self.assertEqual(0, results.get_nowait())
                    if not finish_first:
                        self.assertFalse(store_closed.is_set())
                    release.set()
                    self.assertTrue(store_closed.wait(10))
                    save_threads[0].join(10)
                    self.assertFalse(save_threads[0].is_alive())
                    main.join(10)
                    self.assertTrue(cast(queue.Queue[object], getattr(dialogs[0], "_results")).empty())
                finally:
                    release.set()
                    main.join(10)
            reopened = open_store(parent)
            try:
                actual = worker_setup.load_worker_settings(reopened)
                self.assertEqual(old if fail else worker_setup.WorkerLocalSettings(server_ui_url=fields.server_ui_url), actual)
            finally:
                reopened.close()
            log = (Path(parent) / windows_tray.WORKER_LOG_NAME).read_text()
            if not finish_first:
                self.assertIn("settings shutdown timed out", log)
                self.assertEqual(1, len(runtimes))
            else:
                self.assertEqual(2, len(runtimes))
            if fail:
                self.assertIn("settings save failed (WorkerSetupConfigError)", log)


if __name__ == "__main__":
    _ = unittest.main()
