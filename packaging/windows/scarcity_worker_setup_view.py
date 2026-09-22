# The compact tkinter first-run/settings dialog for the packaged Windows
# worker (issue #113).
#
# This adapter lives in the PACKAGING tree, not in the library — the same
# seam discipline as the pystray tray view beside this file: tkinter is
# bundled by the PyInstaller build, but the library core stays
# GUI-toolkit-free and the onboarding logic it drives
# (``scarcity_router.worker_setup``) is unit-tested on every platform
# with fake views. Deliberately compact (D-051): ONE dialog with two
# modes — first-run (pairing + optional local Ollama) and settings (the
# same local configuration for an already-paired worker, reached from
# the tray's "Worker settings..." action). No wizard, no second
# application, no configuration server.
#
# Security boundaries mirrored from the library core:
# - the dialog never stores, logs or displays the pairing code beyond
#   the in-memory entry field; the persisted settings go through
#   ``worker_setup`` (typed, non-secret fields only);
# - pairing goes through the SAME ``worker_setup.complete_setup`` path
#   as the packaged CLI (one pairing implementation; the identity lives
#   only in the existing ``WorkerLocalStore``);
# - the dialog exposes ONLY the locally evidenced Windows execution
#   path (loopback Ollama); unevidenced execution surfaces are never
#   advertised here (see docs/m10-acceptance.md);
# - the Ollama section can only produce loopback endpoints (the library
#   rejects anything else before anything is persisted).

from __future__ import annotations

import queue
import threading
import webbrowser
from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING

from scarcity_router.worker_client import WorkerOrigin
from scarcity_router.worker_setup import (
    SettingsDialogContext,
    SetupFields,
    SetupOutcome,
    SetupView,
    WorkerLocalSettings,
    control_ui_origin,
)

if TYPE_CHECKING:  # pragma: no cover - type-only import
    import tkinter as tk
    from tkinter import ttk

_ERROR_RED = "#b3261e"
_STATUS_GREEN = "#1b7f3b"

#: Milliseconds between result-queue polls while a background attempt
#: (pairing or saving) is in flight.
_POLL_MS = 80


class SetupDialogUnavailableError(RuntimeError):
    """The tkinter dialog stack is unavailable on this install."""


def is_setup_available() -> bool:
    """Whether the tkinter dialog can be built on this interpreter."""
    try:
        import tkinter  # noqa: F401 - availability probe only
    except ImportError:
        return False
    return True


class TkSetupView:
    """The tkinter :class:`SetupView` (one dialog per invocation).

    Every ``run_first_run``/``run_settings`` call creates its own Tk
    root and runs a local mainloop on the CALLING thread — safe both for
    the pre-tray first run (main thread) and for the tray's
    "Worker settings..." action (the pystray menu thread), because all
    tkinter objects stay on the thread that created them. The
    submit/save callback runs on a short-lived worker thread; results
    are marshalled back through a queue so the UI thread owns every
    widget access.
    """

    def run_first_run(
        self, submit: Callable[[SetupFields], SetupOutcome]
    ) -> SetupOutcome | None:
        import tkinter as tk
        from tkinter import ttk

        dialog = _SetupDialog(
            tk=tk,
            ttk=ttk,
            mode="first_run",
            submit=submit,
            save=None,
            context=None,
        )
        result = dialog.run()
        return result if isinstance(result, SetupOutcome) else None

    def run_settings(
        self,
        context: SettingsDialogContext,
        save: Callable[[SetupFields], WorkerLocalSettings],
    ) -> WorkerLocalSettings | None:
        import tkinter as tk
        from tkinter import ttk

        dialog = _SetupDialog(
            tk=tk,
            ttk=ttk,
            mode="settings",
            submit=None,
            save=save,
            context=context,
        )
        result = dialog.run()
        return result if isinstance(result, WorkerLocalSettings) else None


class _SetupDialog:
    """One dialog instance: builds the widgets, runs the local mainloop."""

    def __init__(
        self,
        *,
        tk: ModuleType,
        ttk: ModuleType,
        mode: str,
        submit: Callable[[SetupFields], SetupOutcome] | None,
        save: Callable[[SetupFields], WorkerLocalSettings] | None,
        context: SettingsDialogContext | None,
    ) -> None:
        self._tk = tk
        self._ttk = ttk
        self._mode = mode
        self._submit = submit
        self._save = save
        self._context = context
        self._results: queue.Queue[object] = queue.Queue()
        self._in_flight = False
        self.result: SetupOutcome | WorkerLocalSettings | None = None

    # ── Construction and mainloop ───────────────────────────────────

    def run(self) -> SetupOutcome | WorkerLocalSettings | None:
        first_run = self._mode == "first_run"
        root = self._tk.Tk()
        self._root = root
        root.title(
            "Scarcity Router worker setup"
            if first_run
            else "Scarcity Router worker settings"
        )
        root.resizable(False, False)

        body = self._ttk.Frame(root, padding=14)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        row = 0
        if first_run:
            intro = self._ttk.Label(
                body,
                text=(
                    "Set up this worker: redeem a one-time pairing code "
                    + "from your Scarcity Router server, then optionally "
                    + "enable a local Ollama resource."
                ),
                wraplength=460,
                justify="left",
            )
            intro.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
            row += 1
        else:
            context = self._context
            assert context is not None
            header = self._ttk.Label(
                body,
                text=f"Paired as {context.worker_id}  to  {context.server_origin}",
                wraplength=460,
                justify="left",
            )
            header.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
            row += 1
            if context.problem:
                problem = self._ttk.Label(
                    body,
                    text=f"Warning: {context.problem}",
                    wraplength=460,
                    justify="left",
                    foreground=_ERROR_RED,
                )
                problem.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
                row += 1

        row = self._build_rows(body, row, first_run)
        row = self._build_status_and_buttons(body, row, first_run)

        root.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._sync_ollama_state()
        root.mainloop()
        return self.result

    def _build_rows(self, body: "ttk.Frame", row: int, first_run: bool) -> int:
        if first_run:
            self._server_entry = self._labeled_entry(
                body, row, "Worker server:", "srws://", None
            )
            row += 1
            label = self._ttk.Label(body, text="Pairing code:")
            label.grid(row=row, column=0, sticky="w", pady=2)
            self._code_entry = self._ttk.Entry(body, width=38)
            self._code_entry.grid(row=row, column=1, sticky="w", pady=2)
            hint = self._ttk.Label(
                body,
                text="one-time code from the server web UI (Workers page)",
                foreground="#555555",
            )
            hint.grid(row=row, column=2, sticky="w", padx=(8, 0), pady=2)
            row += 1

        ui_default = ""
        if not first_run and self._context is not None and self._context.current:
            ui_default = self._context.current.server_ui_url or ""
        self._ui_entry = self._labeled_entry(
            body, row, "Server control UI (optional):", ui_default, None
        )
        self._open_ui_button = self._ttk.Button(
            self._ui_entry.master,
            text="Open server web UI",
            command=self._open_control_ui,
        )
        # Re-place the button in column 2 of the UI row.
        self._open_ui_button.grid(row=row, column=2, sticky="w", padx=(8, 0), pady=2)
        row += 1

        separator = self._ttk.Separator(body)
        separator.grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
        row += 1

        ollama_current = None
        if self._context is not None and self._context.current is not None:
            ollama_current = self._context.current.ollama

        self._ollama_enabled = self._tk.BooleanVar(value=ollama_current is not None)
        check = self._ttk.Checkbutton(
            body,
            text="Enable local Ollama (loopback only)",
            variable=self._ollama_enabled,
            command=self._sync_ollama_state,
        )
        check.grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1

        self._resource_entry = self._labeled_entry(
            body,
            row,
            "Resource ID:",
            ollama_current.resource_id if ollama_current is not None else "",
            "e.g. my-ollama (must match the server resource)",
        )
        row += 1
        self._host_entry = self._labeled_entry(
            body,
            row,
            "Ollama host:",
            ollama_current.host if ollama_current is not None else "127.0.0.1",
            None,
        )
        row += 1
        self._port_entry = self._labeled_entry(
            body,
            row,
            "Ollama port:",
            str(ollama_current.port) if ollama_current is not None else "11434",
            None,
        )
        row += 1
        return row

    def _labeled_entry(
        self,
        body: "ttk.Frame",
        row: int,
        label: str,
        value: str,
        hint: str | None,
    ) -> "ttk.Entry":
        self._ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = self._ttk.Entry(body, width=38)
        if value:
            _ = entry.insert(0, value)
        entry.grid(row=row, column=1, sticky="w", pady=2)
        if hint:
            self._ttk.Label(body, text=hint, foreground="#555555").grid(
                row=row, column=2, sticky="w", padx=(8, 0), pady=2
            )
        return entry

    def _build_status_and_buttons(
        self, body: "ttk.Frame", row: int, first_run: bool
    ) -> int:
        self._status = self._ttk.Label(body, text="", wraplength=460, justify="left")
        self._status.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 4))
        row += 1

        buttons = self._ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=3, sticky="e", pady=(4, 0))
        primary_text = "Pair and start" if first_run else "Save"
        self._primary = self._ttk.Button(
            buttons, text=primary_text, command=self._on_primary
        )
        self._primary.grid(row=0, column=0, padx=4)
        cancel = self._ttk.Button(buttons, text="Cancel", command=self._on_cancel)
        cancel.grid(row=0, column=1)
        return row + 1

    # ── Field collection and enablement ─────────────────────────────

    def _collect_fields(self) -> SetupFields:
        return SetupFields(
            server_url=self._server_entry.get() if hasattr(self, "_server_entry") else "",
            pairing_code=self._code_entry.get() if hasattr(self, "_code_entry") else "",
            server_ui_url=self._ui_entry.get(),
            ollama_enabled=bool(self._ollama_enabled.get()),
            resource_id=self._resource_entry.get(),
            ollama_host=self._host_entry.get(),
            ollama_port=self._port_entry.get(),
        )

    def _set_inputs_enabled(self, enabled: bool) -> None:
        self._in_flight = not enabled
        state = "normal" if enabled else "disabled"
        for entry in (
            getattr(self, "_server_entry", None),
            getattr(self, "_code_entry", None),
            self._ui_entry,
            self._resource_entry,
            self._host_entry,
            self._port_entry,
        ):
            if entry is not None:
                entry.configure(state=state)
        self._primary.configure(state=state)
        self._open_ui_button.configure(state=state)

    def _sync_ollama_state(self) -> None:
        enabled = bool(self._ollama_enabled.get())
        state = "normal" if enabled else "disabled"
        for entry in (self._resource_entry, self._host_entry, self._port_entry):
            entry.configure(state=state)

    def _show_status(self, message: str, *, error: bool) -> None:
        self._status.configure(
            text=message[:400], foreground=_ERROR_RED if error else _STATUS_GREEN
        )

    # ── Actions ─────────────────────────────────────────────────────

    def _on_primary(self) -> None:
        if self._in_flight:
            return
        fields = self._collect_fields()
        self._set_inputs_enabled(False)
        if self._mode == "first_run":
            submit = self._submit
            assert submit is not None

            def attempt() -> None:
                try:
                    outcome: object = submit(fields)
                except Exception as exc:  # never leak a traceback to the dialog
                    outcome = SetupOutcome(
                        ok=False, message=f"setup failed ({type(exc).__name__})"
                    )
                self._results.put(outcome)

            threading.Thread(target=attempt, name="worker-setup", daemon=True).start()
            self._show_status("Pairing over verified TLS...", error=False)
        else:
            save = self._save
            assert save is not None

            def persist() -> None:
                from scarcity_router.worker_setup import WorkerSetupConfigError

                try:
                    saved: object = save(fields)
                except WorkerSetupConfigError as exc:
                    saved = exc
                except Exception as exc:
                    saved = WorkerSetupConfigError(
                        f"saving failed ({type(exc).__name__})"
                    )
                self._results.put(saved)

            threading.Thread(target=persist, name="worker-settings", daemon=True).start()
            self._show_status("Saving...", error=False)
        self._root.after(_POLL_MS, self._poll_results)

    def _poll_results(self) -> None:
        try:
            result = self._results.get_nowait()
        except queue.Empty:
            self._root.after(_POLL_MS, self._poll_results)
            return
        self._set_inputs_enabled(True)
        if isinstance(result, SetupOutcome):
            if result.ok:
                self.result = result
                self._root.destroy()
                return
            self._show_status(result.message, error=True)
            return
        if isinstance(result, WorkerLocalSettings):
            self.result = result
            self._root.destroy()
            return
        # A WorkerSetupConfigError from the settings save path.
        self._show_status(str(result), error=True)

    def _on_cancel(self) -> None:
        if self._in_flight:
            # Let the in-flight attempt finish; closing now could leave a
            # half-finished pairing invisible to the user. The result
            # poller re-enables the buttons when the attempt lands.
            return
        self.result = None
        self._root.destroy()

    def _open_control_ui(self) -> None:
        from scarcity_router.worker_setup import (
            WorkerSetupConfigError,
            normalize_control_ui_url,
        )

        fields = self._collect_fields()
        ui_text = fields.server_ui_url.strip()
        try:
            url = (
                normalize_control_ui_url(ui_text)
                if ui_text
                else control_ui_origin(WorkerOrigin.parse(fields.server_url.strip()))
            )
            _ = webbrowser.open(f"{url}/admin")
            self._show_status("opened the server web UI in your browser", error=False)
        except WorkerSetupConfigError as exc:
            self._show_status(str(exc), error=True)
        except Exception:
            self._show_status("could not open the server web UI", error=True)


def build_setup_view() -> SetupView:
    """The tkinter :class:`SetupView` (lazy import; packaging seam)."""
    try:
        import tkinter  # noqa: F401 - the dialog imports it per invocation
    except ImportError as exc:
        raise SetupDialogUnavailableError(
            "the setup dialog requires tkinter, which the packaged worker "
            + "bundles; reinstall from the release ZIP"
        ) from exc
    return TkSetupView()


__all__ = [
    "SetupDialogUnavailableError",
    "TkSetupView",
    "build_setup_view",
    "is_setup_available",
]
