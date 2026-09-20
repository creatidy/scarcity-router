"""Windows tray UX for the packaged Scarcity Router native worker.

M10 (issue #95): the PyInstaller-packaged Windows worker shows a minimal
tray icon with its state (running / disconnected / error), the worker
identity and server it is bound to, and five actions: show the identity/
status text, open the server's control UI in a browser, reconnect
(restart the runtime loop with a fresh bounded reconnect budget), open
the diagnostics folder (the worker's state directory holding its bounded
redacted log), and quit.

Deliberate boundaries (D-044/D-049; the tray is a status surface, never
a second control plane):

- **No routing or configuration duplication.** The tray reads the paired
  identity (worker id + server origin) from the SAME worker store the
  CLI uses, and :func:`tray_main` reuses the worker CLI's parser and
  adapter-registry builder, so flag names and allowlist semantics cannot
  drift. Every routing decision stays server-side.
- **No provider secrets in the tray.** The worker holds no provider
  credentials at all; the tray only ever sees the worker id, the server
  origin and the redacted diagnostic lines the runtime already exposes.
- **Small GUI, packaging-owned adapter.** The GUI stack (pystray +
  Pillow) is an optional Windows-only extra bundled by the PyInstaller
  build; the pystry adapter itself lives in the packaging tree
  (``packaging/windows/scarcity_worker_tray_view.py``), NOT in the
  library: the core package keeps its dependency set, this module
  imports on every platform, and everything except the adapter is pure
  and unit-tested everywhere. Only the adapter is Windows-gated
  (EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE,
  docs/m10-acceptance.md).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

#: Tray states (closed vocabulary; icon color and status text derive from it).
STATE_RUNNING = "running"
STATE_DISCONNECTED = "disconnected"
STATE_ERROR = "error"
TRAY_STATES = (STATE_RUNNING, STATE_DISCONNECTED, STATE_ERROR)

#: Bounded worker-log name and size cap. The log holds only the runtime's
#: already redacted, bounded diagnostic lines — never request content.
WORKER_LOG_NAME = "worker-log.txt"
WORKER_LOG_MAX_BYTES = 1_048_576


class TrayNotAvailableError(RuntimeError):
    """The tray GUI stack is unavailable on this platform or install."""


def is_supported() -> bool:
    """Whether the tray UX is supported on this platform (Windows only)."""
    return sys.platform == "win32"


def worker_status_text(
    state: str,
    *,
    worker_id: str | None,
    server_origin: str | None,
    detail: str = "",
) -> str:
    """The tray status text for one state (pure, testable everywhere)."""
    if state not in TRAY_STATES:
        raise ValueError(f"windows_tray: unknown state {state!r}")
    identity = worker_id if worker_id else "not paired"
    server = server_origin if server_origin else "no server"
    lines = [
        f"Scarcity Router worker — {state}",
        f"identity: {identity}",
        f"server: {server}",
    ]
    if detail:
        lines.append(detail)
    return "\n".join(lines)


class TrayView(Protocol):
    """The GUI seam the state model drives (pystray-backed in production)."""

    def start(self) -> None: ...

    def update(self, *, state: str, text: str) -> None: ...

    def stop(self) -> None: ...


class NullView:
    """A no-op view used between model construction and view binding."""

    def start(self) -> None:
        return None

    def update(self, *, state: str, text: str) -> None:
        _ = state, text

    def stop(self) -> None:
        return None


class TrayStateModel:
    """The tray's state machine (GUI-independent; unit-tested everywhere).

    Transitions are deduplicated (re-marking the same state with the same
    detail does not touch the GUI) and every transition is rendered to the
    injected :class:`TrayView`. ``request_quit``/``request_restart`` are
    the menu actions; their behavior is wired by the owner (see
    :func:`run_tray_worker`).
    """

    _view: TrayView
    _worker_id: str | None
    _server_origin: str | None
    _on_quit: Callable[[], None] | None
    _on_restart: Callable[[], None] | None
    _state: str | None
    _detail: str
    _lock: threading.Lock

    def __init__(
        self,
        *,
        view: TrayView,
        worker_id: str | None = None,
        server_origin: str | None = None,
        on_quit: Callable[[], None] | None = None,
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        self._view = view
        self._worker_id = worker_id
        self._server_origin = server_origin
        self._on_quit = on_quit
        self._on_restart = on_restart
        self._state = None
        self._detail = ""
        self._lock = threading.Lock()

    @property
    def state(self) -> str | None:
        return self._state

    def status_text(self) -> str:
        """The current identity/status text (the tooltip payload)."""
        with self._lock:
            return self._render_text_locked()

    def _render_text_locked(self) -> str:
        """Render the status text; the caller holds ``_lock``."""
        return worker_status_text(
            self._state or STATE_DISCONNECTED,
            worker_id=self._worker_id,
            server_origin=self._server_origin,
            detail=self._detail,
        )

    def mark_running(self, detail: str = "") -> None:
        self._transition(STATE_RUNNING, detail)

    def mark_disconnected(self, detail: str = "") -> None:
        self._transition(STATE_DISCONNECTED, detail)

    def mark_error(self, detail: str = "") -> None:
        self._transition(STATE_ERROR, detail)

    def request_quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()

    def request_restart(self) -> None:
        if self._on_restart is not None:
            self._on_restart()

    def bind_view(self, view: TrayView) -> None:
        """Swap in the real view (must happen before the loop starts).

        ``run_tray_worker`` constructs the model with a
        :class:`NullView`, builds the platform view from this model and
        binds it BEFORE starting the session thread, so every transition
        is rendered exactly once to the real view.
        """
        self._view = view

    def _transition(self, state: str, detail: str) -> None:
        with self._lock:
            if self._state == state and self._detail == detail:
                return
            self._state = state
            self._detail = detail
            text = self._render_text_locked()
        self._view.update(state=state, text=text)


def load_worker_identity(
    state_dir: str | Path | None = None,
) -> tuple[str | None, str | None]:
    """The paired ``(worker_id, server_origin)`` from the worker's own store.

    Read-only over the CLI's store — never a second identity source.
    """
    from .worker_client import open_worker_store

    store = open_worker_store(str(state_dir) if state_dir else None)
    try:
        identity = store.load_identity()
    finally:
        store.close()
    if identity is None:
        return None, None
    return identity.worker_id, identity.server_origin


def worker_state_dir() -> Path:
    """The worker's state directory (also the diagnostics folder)."""
    from .worker_local_store import default_worker_state_dir

    return Path(default_worker_state_dir())


def append_worker_log(state_dir: str | Path, lines: tuple[str, ...] | list[str]) -> None:
    """Append redacted diagnostic lines to the bounded worker log.

    The lines are exactly what the CLI entry prints to stderr
    (``WorkerRuntime.diagnostics()``): redacted and bounded. The file is
    truncated once it exceeds the size cap, and any I/O failure is
    swallowed — diagnostics are best-effort and must never crash the
    worker.
    """
    if not lines:
        return
    log_path = Path(state_dir) / WORKER_LOG_NAME
    try:
        if log_path.exists() and log_path.stat().st_size > WORKER_LOG_MAX_BYTES:
            log_path.unlink()
        with log_path.open("a", encoding="utf-8") as handle:
            for line in lines:
                _ = handle.write(line.rstrip("\n") + "\n")
    except OSError:
        return


#: The duck-typed runtime surface the tray loop drives (satisfied by the
#: real ``WorkerRuntime`` and by the test fakes alike).
class TrayRuntime(Protocol):
    def run(self) -> object: ...

    def diagnostics(self) -> tuple[str, ...] | list[str]: ...

    def request_stop(self) -> None: ...


def run_tray_worker(
    *,
    runtime_factory: Callable[[], TrayRuntime],
    view_factory: Callable[[TrayStateModel], TrayView],
    state_dir: str | Path,
    worker_id: str | None,
    server_origin: str | None,
) -> None:
    """Drive the tray worker: one reconnecting runtime plus the tray view.

    Platform-independent assembly, unit-tested with injected factories:

    - the runtime session loop runs on its own thread; its terminal
      reason maps to a tray state (exhausted budget → disconnected,
      fatal/unknown → error, stop → exit);
    - "Reconnect / restart" cooperatively stops the current runtime and
      starts a fresh one (fresh connection, fresh bounded reconnect
      budget) — it never runs two runtimes concurrently;
    - "Quit" requests the runtime stop and unblocks, so the process can
      exit;
    - every terminal transition appends the runtime's redacted
      diagnostics to the bounded worker log.
    """
    from .worker_client import STOP_EXHAUSTED, STOP_REQUESTED

    directory = Path(state_dir)
    quit_event = threading.Event()
    restart_requested = threading.Event()
    runtime_box: list[TrayRuntime] = []

    def write_log(runtime: TrayRuntime) -> None:
        append_worker_log(directory, runtime.diagnostics())

    def request_stop(runtime: TrayRuntime) -> None:
        runtime.request_stop()

    def request_quit() -> None:
        quit_event.set()
        for runtime in runtime_box:
            request_stop(runtime)

    def request_restart() -> None:
        restart_requested.set()
        for runtime in runtime_box:
            request_stop(runtime)

    model = TrayStateModel(
        view=NullView(),
        worker_id=worker_id,
        server_origin=server_origin,
        on_quit=request_quit,
        on_restart=request_restart,
    )

    def session_loop() -> None:
        try:
            _session_loop()
        except Exception as unexpected:  # never leave the tray silently stuck
            model.mark_error(f"internal error ({type(unexpected).__name__})")

    def _session_loop() -> None:
        while not quit_event.is_set():
            restart_requested.clear()
            try:
                runtime = runtime_factory()
            except Exception as exc:
                model.mark_error(f"cannot start runtime ({type(exc).__name__})")
                return
            runtime_box[:] = [runtime]
            model.mark_running()
            try:
                reason: str = str(runtime.run())
            except Exception as exc:
                model.mark_error(f"runtime failure ({type(exc).__name__})")
                write_log(runtime)
                runtime_box.clear()
                return
            write_log(runtime)
            runtime_box.clear()
            if reason == STOP_REQUESTED and not quit_event.is_set():
                if restart_requested.is_set():
                    continue  # a fresh runtime: new connection, fresh budget
                return
            if reason == STOP_EXHAUSTED:
                model.mark_disconnected(
                    "reconnect budget exhausted — use Reconnect / restart"
                )
            elif not quit_event.is_set():
                model.mark_error(f"runtime stopped: {reason}")
            return

    session = threading.Thread(
        target=session_loop, name="scarcity-router-worker", daemon=True
    )

    # Bind the view BEFORE the session thread starts so no transition is
    # ever rendered into the void (the model deduplicates, so a state
    # that was already rendered pre-bind would never re-render).
    view = view_factory(model)
    model.bind_view(view)
    view.start()
    session.start()
    try:
        while not quit_event.wait(timeout=0.2):
            continue
    finally:
        quit_event.set()
        for runtime in runtime_box:
            request_stop(runtime)
        _ = session.join(timeout=10)
        view.stop()


#: The packaging side's view factory: builds the platform tray view for a
#: model, receiving the control-UI opener and the diagnostics folder.
ViewFactory = Callable[[TrayStateModel, Callable[[], None], Path], TrayView]


def tray_main(argv: list[str] | None = None, *, view_factory: ViewFactory | None = None) -> int:
    """Entry point of the packaged Windows worker executable.

    Reuses the worker CLI's parser (so ``run`` flags and allowlist
    semantics are identical by construction) and the real runtime. The
    view factory comes from the packaging tree (the pystray adapter);
    without one — or with the adapter unavailable — the packaged
    executable surfaces a remediation message instead of a bare
    ImportError (windowed executables have no console).

    ``--server-ui-url URL`` (packaging-only flag) names the server's
    web-UI origin for the tray's "Open server control UI" action. The
    worker protocol does not carry the server's HTTP origin (it has its
    own port), so without this flag the action derives the DOCUMENTED
    default (https://HOST:8787 or http://HOST:8787 from the worker
    origin's host); deployments on custom ports should pass the flag.
    """
    argv_list = list(argv) if argv is not None else []
    try:
        argv_list, ui_url_override = _extract_ui_url(argv_list)
    except WorkerTrayUsageError as exc:
        _pre_tray_error(str(exc))
        return 2
    arguments = _parse_run_arguments(argv_list)
    state_dir = _arguments_state_dir(arguments)
    try:
        worker_id, server_origin = load_worker_identity(state_dir)
    except (ValueError, OSError) as exc:
        _pre_tray_error(str(exc))
        return 2
    if worker_id is None or server_origin is None:
        _pre_tray_error(
            "the worker is not paired yet.\n\nRun:\n"
            + "  scarcity-router-worker pair --server srws://SERVER:8790 --code CODE\n"
            + "(the one-time code comes from the server web UI, Workers page)"
        )
        return 2

    from .worker_client import (
        WorkerConfigError,
        WorkerOrigin,
        WorkerRuntime,
        build_local_adapter_registry,
        open_worker_store,
    )

    diagnostics_dir = Path(state_dir) if state_dir else worker_state_dir()
    try:
        origin = (
            WorkerOrigin.parse(str(arguments["server"]))
            if arguments.get("server")
            else WorkerOrigin.parse(server_origin)
        )
        store = open_worker_store(state_dir)
        registry = build_local_adapter_registry(arguments)
        origin_text = f"{'srws' if origin.tls else 'srw'}://{origin.host}:{origin.port}"
        ui_origin = ui_url_override or (
            f"{'https' if origin.tls else 'http'}://{origin.host}:8787"
        )

        def open_control_ui() -> None:
            import webbrowser

            _ = webbrowser.open(f"{ui_origin}/admin")

        if view_factory is None:
            raise TrayNotAvailableError(
                "the packaged worker was built without the tray view; "
                + "use the scarcity-router-worker console command instead"
            )

        runtime_factory = lambda: WorkerRuntime(  # noqa: E731 - small factory
            origin=origin, store=store, local_adapters=registry
        )
        run_tray_worker(
            runtime_factory=runtime_factory,
            view_factory=lambda model: view_factory(
                model, open_control_ui, diagnostics_dir
            ),
            state_dir=diagnostics_dir,
            worker_id=worker_id,
            server_origin=origin_text,
        )
    except (WorkerConfigError, ValueError, OSError, TrayNotAvailableError) as exc:
        _pre_tray_error(str(exc))
        return 2
    return 0


def _parse_run_arguments(argv: list[str]) -> dict[str, object]:
    """Parse ``run``-style flags with the worker CLI's own parser."""
    from .worker_client import build_parser

    if not argv or argv[0] not in {"pair", "run"}:
        argv = ["run", *argv]
    arguments: dict[str, object] = dict(vars(build_parser().parse_args(argv)))
    if arguments.get("command") != "run":
        raise SystemExit(2)
    return arguments


def _arguments_state_dir(arguments: dict[str, object]) -> str | None:
    value = arguments.get("state_dir")
    return str(value) if isinstance(value, str) and value else None


def _extract_ui_url(argv: list[str]) -> tuple[list[str], str | None]:
    """Pull the packaging-only ``--server-ui-url`` flag out of ``argv``."""
    remaining: list[str] = []
    ui_url: str | None = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--server-ui-url":
            if index + 1 >= len(argv):
                raise WorkerTrayUsageError("--server-ui-url requires a URL")
            ui_url = argv[index + 1]
            index += 2
            continue
        if item.startswith("--server-ui-url="):
            ui_url = item.split("=", 1)[1]
            index += 1
            continue
        remaining.append(item)
        index += 1
    return remaining, ui_url


class WorkerTrayUsageError(ValueError):
    """A packaged-executable-only flag was misused (safe message)."""


def _pre_tray_error(message: str) -> None:  # pragma: no cover - Windows shell
    """Surface a pre-tray failure without a console (windowed build).

    On Windows the message goes to a native message box; elsewhere (and
    whenever the native call fails) it goes to stderr. The message is a
    safe, remediation-bearing instruction — never credential material.
    """
    if is_supported():
        try:
            import ctypes

            # The Windows message box is reached only through the untyped
            # ctypes shell; the narrow suppression below is the explicit
            # justification (safe constant string, no credential data).
            windll: object = ctypes.windll  # type: ignore[attr-defined] - Windows only
            user32 = getattr(windll, "user32")  # pyright: ignore[reportAny]
            box = getattr(user32, "MessageBoxW")  # pyright: ignore[reportAny]
            _ = box(None, message, "Scarcity Router worker", 0x10)  # pyright: ignore[reportAny]
            return
        except Exception:
            pass
    print(f"worker: {message}", file=sys.stderr)


__all__ = [
    "STATE_DISCONNECTED",
    "STATE_ERROR",
    "STATE_RUNNING",
    "TRAY_STATES",
    "TrayNotAvailableError",
    "TrayStateModel",
    "TrayView",
    "ViewFactory",
    "WORKER_LOG_MAX_BYTES",
    "WORKER_LOG_NAME",
    "append_worker_log",
    "is_supported",
    "load_worker_identity",
    "run_tray_worker",
    "tray_main",
    "worker_state_dir",
    "worker_status_text",
]
