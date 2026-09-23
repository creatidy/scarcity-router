"""Windows tray UX and packaged entry point of the Scarcity Router worker.

M10 (issue #95): the PyInstaller-packaged Windows worker shows a minimal
tray icon with its state (running / disconnected / error), the worker
identity and server it is bound to, and actions: show the identity/
status text, open the server's control UI in a browser, open the Worker
settings dialog (issue #113), reconnect (restart the runtime loop with a
fresh bounded reconnect budget), open the diagnostics folder (the
worker's state directory holding its bounded redacted log), and quit.

Issue #113 made the packaged executable's entry point a real router:
no-arg launch enters the compact first-run setup dialog when the worker
is unpaired (:mod:`scarcity_router.worker_setup` is the GUI-independent
core; the tkinter dialog lives in the packaging tree like the pystray
adapter), the packaged ``pair`` command shares the dialog's pairing
path, and explicit ``run``-style flags keep their CLI semantics. The
tray's runtime factory rebuilds the local adapter registry from the
stored non-secret local settings on every (re)connect, so a settings
change takes effect through the existing restart machinery.

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
  build; the pystray adapter itself lives in the packaging tree
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

from . import worker_setup
from .worker_client import (
    ConnectFactory,
    WorkerConfigError,
    WorkerOrigin,
    WorkerRuntime,
    WorkerRuntimeError,
    build_local_adapter_registry,
    build_parser,
    open_worker_store,
)
from .worker_local_store import WorkerLocalStore
from .worker_protocol import WorkerProtocolError
from .worker_setup import (
    SettingsDialogContext,
    SetupFields,
    WorkerSetupConfigError,
    complete_setup,
    control_ui_origin,
    effective_arguments,
    load_worker_settings,
    resolve_launch,
    save_settings_fields,
)

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
#: model, receiving the control-UI opener, the diagnostics folder and the
#: Worker-settings action (issue #113).
ViewFactory = Callable[
    [TrayStateModel, Callable[[], None], Path, Callable[[], None]], TrayView
]

#: The packaging side's setup-dialog factory (tkinter adapter; issue #113).
SetupViewFactory = Callable[[], "worker_setup.SetupView"]


def tray_main(
    argv: list[str] | None = None,
    *,
    view_factory: ViewFactory | None = None,
    setup_view_factory: SetupViewFactory | None = None,
    connect_factory: ConnectFactory | None = None,
    runtime_factory: Callable[[], TrayRuntime] | None = None,
    state_dir: str | None = None,
) -> int:
    """Entry point of the packaged Windows worker executable (issue #113).

    Routing (one executable, three paths):

    - ``pair --server … --code …`` → packaged CLI pairing through the
      SAME pairing path the first-run dialog drives
      (:func:`worker_setup.pair_worker` over ``WorkerRuntime.pair``);
      output reaches the calling PowerShell/cmd window via a
      best-effort parent-console attach (the build is windowed);
    - no arguments → first-run setup when unpaired (the compact setup
      dialog from the packaging tree), the tray when paired;
    - ``run``-style flags → explicit advanced run (tray), exactly the
      pre-#113 CLI semantics.

    The tray's runtime factory rebuilds the local adapter registry from
    the stored local settings (:mod:`worker_setup`) on every
    (re)connect, so a settings change takes effect through the existing
    restart machinery. Explicit run flags that select an adapter
    override the stored selection for that process.

    ``--server-ui-url URL`` (packaging-only flag) names the server's
    web-UI origin for the tray's "Open server control UI" action and
    wins over the stored setting; without either, the DOCUMENTED default
    derivation applies (https://HOST:8787 or http://HOST:8787 from the
    worker origin's host).

    ``state_dir`` is the library-level override of the ``--state-dir``
    flag (the entry-routing and GUI seams are exercised in tests against
    throwaway directories; production passes only ``argv``).
    """
    argv_list = list(argv) if argv is not None else []
    try:
        argv_list, ui_url_override = _extract_ui_url(argv_list)
    except WorkerTrayUsageError as exc:
        _pre_tray_error(str(exc))
        return 2

    if argv_list and argv_list[0] == "pair":
        return _packaged_pair(argv_list[1:], connect_factory=connect_factory)

    try:
        arguments = _parse_run_arguments(argv_list)
    except SystemExit:
        # argparse rejected the flags: it already printed its usage to
        # the (best-effort attached) console; keep the exit contract.
        raise
    resolved_dir = state_dir if state_dir is not None else _arguments_state_dir(arguments)
    try:
        worker_id, server_origin = load_worker_identity(resolved_dir)
    except (ValueError, OSError) as exc:
        _pre_tray_error(str(exc))
        return 2

    paired = worker_id is not None and server_origin is not None
    if resolve_launch(paired=paired) == worker_setup.FIRST_RUN:
        if argv_list or setup_view_factory is None:
            _pre_tray_error(_unpaired_remediation())
            return 2
        outcome = _run_first_run(
            setup_view_factory, resolved_dir, connect_factory=connect_factory
        )
        if outcome is None:
            # User cancelled before pairing: no identity/config mutation.
            return 1
        if not outcome.ok or outcome.worker_id is None:
            # Defensive: the dialog loop only returns on success or
            # cancel; treat anything else as a failed setup.
            _pre_tray_error(outcome.message or "setup failed")
            return 2
        try:
            worker_id, server_origin = load_worker_identity(resolved_dir)
        except (ValueError, OSError) as exc:
            _pre_tray_error(str(exc))
            return 2
        if worker_id is None or server_origin is None:
            _pre_tray_error("pairing completed but the stored identity could not be read")
            return 2
        diagnostics_dir = Path(resolved_dir) if resolved_dir else worker_state_dir()
        if not outcome.settings_saved:
            append_worker_log(
                diagnostics_dir,
                (
                    "local settings were not saved after pairing; "
                    + "complete them via the tray's Worker settings action",
                ),
            )

    diagnostics_dir = Path(resolved_dir) if resolved_dir else worker_state_dir()
    try:
        origin = (
            WorkerOrigin.parse(str(arguments["server"]))
            if arguments.get("server")
            else WorkerOrigin.parse(str(server_origin))
        )
        store = open_worker_store(resolved_dir)
        try:
            # The runtime and the settings dialog use the store for the
            # whole tray session, so it stays open until run_tray_worker
            # returns (the session thread is joined by then) — and it is
            # closed exactly once on every path out of this block.
            origin_text = f"{'srws' if origin.tls else 'srw'}://{origin.host}:{origin.port}"
            try:
                stored_settings = load_worker_settings(store)
            except WorkerSetupConfigError:
                # A malformed document must not choose the control-UI
                # origin; the runtime factory fails closed on it separately.
                stored_settings = None
            ui_origin = control_ui_origin(
                origin, ui_url_override or (stored_settings.server_ui_url if stored_settings else None)
            )

            def open_control_ui() -> None:
                import webbrowser

                _ = webbrowser.open(f"{ui_origin}/admin")

            if view_factory is None:
                raise TrayNotAvailableError(
                    "the packaged worker was built without the tray view; "
                    + "use the scarcity-router-worker console command instead"
                )

            # The settings dialog asks the tray model for the controlled
            # restart after a successful save; the hook is bound when the
            # real view is built (before the session thread starts), so the
            # action can never fire into the void.
            restart_hook: list[Callable[[], None]] = []

            def open_worker_settings() -> None:
                _open_settings_dialog(
                    setup_view_factory,
                    store,
                    worker_id or "",
                    origin_text,
                    restart=restart_hook[0] if restart_hook else None,
                )

            def make_runtime() -> WorkerRuntime:
                return build_runtime(
                    origin=origin,
                    store=store,
                    cli_arguments=arguments,
                    state_dir=resolved_dir,
                )

            def view_wrapper(model: TrayStateModel) -> TrayView:
                restart_hook[:] = [model.request_restart]
                return view_factory(model, open_control_ui, diagnostics_dir, open_worker_settings)

            factory: Callable[[], TrayRuntime] = (
                runtime_factory if runtime_factory is not None else make_runtime
            )
            run_tray_worker(
                runtime_factory=factory,
                view_factory=view_wrapper,
                state_dir=diagnostics_dir,
                worker_id=worker_id,
                server_origin=origin_text,
            )
        finally:
            store.close()
    except (WorkerConfigError, ValueError, OSError, TrayNotAvailableError) as exc:
        _pre_tray_error(str(exc))
        return 2
    return 0


def build_runtime(
    *,
    origin: WorkerOrigin,
    store: WorkerLocalStore,
    cli_arguments: dict[str, object],
    state_dir: str | None,
) -> WorkerRuntime:
    """The tray's runtime factory: settings-aware adapter registry.

    Called on every (re)connect, so a settings change (or a stored
    malformed document) takes effect at the next runtime construction:
    the registry is rebuilt through the EXISTING
    ``build_local_adapter_registry`` (identical semantics to the CLI),
    never mutated at runtime. A malformed stored document raises the
    typed setup error — the tray surfaces the failure and the settings
    dialog is the recovery path (fail closed, never a guess).
    """
    settings = load_worker_settings(store)
    arguments = effective_arguments(cli_arguments, settings)
    registry = build_local_adapter_registry(arguments, state_dir)
    return WorkerRuntime(origin=origin, store=store, local_adapters=registry)


def _open_settings_dialog(
    setup_view_factory: SetupViewFactory | None,
    store: WorkerLocalStore,
    worker_id: str,
    origin_text: str,
    *,
    restart: Callable[[], None] | None,
) -> None:
    """Open the SAME local configuration UI in its settings mode.

    Reopens the first-run dialog without the pairing section (issue
    #113): no second configuration implementation. A successful save
    asks the tray for a controlled restart (the runtime factory rebuilds
    the registry from the store); a cancel changes nothing.
    """
    if setup_view_factory is None:
        return
    try:
        current = load_worker_settings(store)
        problem: str | None = None
    except WorkerSetupConfigError as exc:
        current, problem = None, str(exc)
    context = SettingsDialogContext(
        worker_id=worker_id,
        server_origin=origin_text,
        current=current,
        problem=problem,
    )

    def save(fields: SetupFields) -> "worker_setup.WorkerLocalSettings":
        return save_settings_fields(fields, store=store)

    try:
        saved = setup_view_factory().run_settings(context, save)
    except Exception:  # noqa: BLE001 - a dialog failure never kills the tray
        return
    if saved is not None and restart is not None:
        restart()


def _run_first_run(
    setup_view_factory: SetupViewFactory,
    state_dir: str | None,
    *,
    connect_factory: ConnectFactory | None,
) -> worker_setup.SetupOutcome | None:
    """Run the first-run dialog against a store opened on ``state_dir``."""
    try:
        setup_view = setup_view_factory()
    except Exception as exc:  # the dialog stack itself is unavailable
        _pre_tray_error(f"the setup dialog is unavailable ({type(exc).__name__})")
        return None
    store = open_worker_store(state_dir)
    try:

        def submit(fields: SetupFields) -> "worker_setup.SetupOutcome":
            return complete_setup(fields, store=store, connect_factory=connect_factory)

        return setup_view.run_first_run(submit)
    finally:
        store.close()


def _packaged_pair(rest: list[str], *, connect_factory: ConnectFactory | None) -> int:
    """The packaged ``pair`` command (issue #113): same pairing path as
    the dialog, usable from PowerShell/cmd without Python installed."""
    attached = _attach_parent_console()
    arguments: dict[str, object] = dict(vars(build_parser().parse_args(["pair", *rest])))
    state_dir = _arguments_state_dir(arguments)
    store = open_worker_store(state_dir)
    try:
        origin = WorkerOrigin.parse(str(arguments["server"]))
        identity = worker_setup.pair_worker(
            origin,
            str(arguments["code"]),
            store=store,
            device_label=(
                str(arguments["label"]) if arguments.get("label") else None
            ),
            connect_factory=connect_factory,
        )
    except (
        WorkerConfigError,
        WorkerRuntimeError,
        WorkerProtocolError,
        ValueError,
        OSError,
    ) as exc:
        message = worker_setup.describe_pairing_failure(exc)
        print(f"worker: {message}", file=sys.stderr)
        if not attached:
            _pre_tray_error(message)
        return 2
    finally:
        # This function opened the store, so it closes it exactly once —
        # on success, on a typed pairing failure (a normal first-run
        # path) and on anything unexpected alike.
        store.close()
    print(f"paired as {identity.worker_id}; identity stored")
    return 0


def _attach_parent_console() -> bool:
    """Best-effort: show packaged CLI output in the calling console.

    The packaged executable is a windowed build (no console of its
    own); when launched from an existing PowerShell/cmd window this
    attaches the parent console and rebinds stdout/stderr to it, so
    ``pair`` output is visible. Any failure leaves the (invisible)
    streams in place — the caller falls back to the message box.

    The console device is reached through the Win32 ``CreateFileW``
    handle + fd conversion, never a filesystem open: this module writes
    no files (the repository guardrail keeps it that way).
    """
    if not is_supported():
        return False
    try:
        import ctypes
        import io
        import msvcrt
        import os

        # Windows-only Win32 console attach through the untyped ctypes
        # shell; the narrow suppressions below are the explicit
        # justification (constant arguments, no credential data).
        windll: object = ctypes.windll  # type: ignore[attr-defined] - Windows only
        kernel32 = getattr(windll, "kernel32")  # pyright: ignore[reportAny]
        attach = getattr(kernel32, "AttachConsole")  # pyright: ignore[reportAny]
        create_file = getattr(kernel32, "CreateFileW")  # pyright: ignore[reportAny]
        ATTACH_PARENT_PROCESS = 0xFFFFFFFF
        if not attach(ATTACH_PARENT_PROCESS):
            return False
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ_WRITE = 0x3
        OPEN_EXISTING = 3
        handle: int | None = create_file(  # pyright: ignore[reportAny]
            "CONOUT$",
            GENERIC_WRITE,
            FILE_SHARE_READ_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if not isinstance(handle, int) or handle == -1:  # INVALID_HANDLE_VALUE
            return False
        console = msvcrt.open_osfhandle(handle, 0)
        _ = os.dup2(console, 1)
        _ = os.dup2(console, 2)
        sys.stdout = io.TextIOWrapper(
            io.FileIO(1, "w", closefd=False), encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            io.FileIO(2, "w", closefd=False), encoding="utf-8", errors="replace"
        )
        return True
    except Exception:
        return False


def _invoked_packaged_name() -> str:
    """The packaged executable's own name for remediation messages."""
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    if invoked.startswith("scarcity-worker"):
        return invoked
    return "scarcity-worker.exe"


def _unpaired_remediation() -> str:
    """The unpaired remediation text, naming THIS executable (issue #113).

    The standalone ZIP contains only ``scarcity-worker.exe``; its
    instructions must never send the user to the Python console script
    (the v0.1.0 first-launch dead end).
    """
    exe = _invoked_packaged_name()
    return (
        "the worker is not paired yet.\n\n"
        + "Double-click this executable and complete the first-run setup, or run:\n"
        + f"  {exe} pair --server srws://SERVER:8790 --code CODE\n"
        + "(the one-time code comes from the server web UI, Workers page)"
    )


def _parse_run_arguments(argv: list[str]) -> dict[str, object]:
    """Parse ``run``-style flags with the worker CLI's own parser.

    ``pair`` never reaches here (the packaged router intercepts it); any
    other leading word is treated as a run flag, so argparse's rejection
    behavior stays exactly what the pre-#113 executable did.
    """
    if not argv or argv[0] != "run":
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
    "build_runtime",
    "is_supported",
    "load_worker_identity",
    "run_tray_worker",
    "tray_main",
    "worker_state_dir",
    "worker_status_text",
]
