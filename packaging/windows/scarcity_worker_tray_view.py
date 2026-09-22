# The pystray-backed tray view for the packaged Windows worker.
#
# This adapter lives in the PACKAGING tree, not in the library: pystray
# and Pillow are untyped Windows-only optional dependencies (the ``tray``
# extra), and the repository's basedpyright gate covers
# ``scarcity_router`` + ``tests`` only (the same scope precedent as
# ``tools/``). The adapter is exercised on Windows by the tag-driven
# release build and recorded as
# EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE in
# docs/m10-acceptance.md; everything it drives (the state model, status
# text, run loop, bounded log) is library code unit-tested on every
# platform in tests/test_windows_tray.py.

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from scarcity_router.windows_tray import (
    STATE_DISCONNECTED,
    STATE_ERROR,
    STATE_RUNNING,
    TrayNotAvailableError,
    TrayStateModel,
    TrayView,
)

_STATE_COLORS = {
    STATE_RUNNING: (46, 160, 67),
    STATE_DISCONNECTED: (210, 153, 34),
    STATE_ERROR: (218, 54, 51),
}


def is_windows() -> bool:
    return sys.platform == "win32"


def build_tray_view(
    model: TrayStateModel,
    open_control_ui: Callable[[], None],
    diagnostics_dir: Path,
) -> TrayView:
    """The pystray-backed :class:`TrayView` (Windows + the ``tray`` extra).

    Imports pystray and Pillow lazily; on any other platform or without
    the optional dependency this raises :class:`TrayNotAvailableError`
    with a remediation message instead of a bare ImportError. The icon
    is drawn programmatically (state-colored disc), so the packaging
    bundles no image assets.
    """
    if not is_windows():
        raise TrayNotAvailableError(
            "the tray UX is Windows-only; run the worker with the "
            + "scarcity-router-worker command instead"
        )
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise TrayNotAvailableError(
            "the tray UX requires the optional 'tray' extra: "
            + "pip install scarcity-router[tray]"
        ) from exc

    def icon_image(state: str):
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse(
            (8, 8, 56, 56), fill=_STATE_COLORS.get(state, _STATE_COLORS[STATE_ERROR])
        )
        return image

    class PystrayView:
        """The pystray adapter: start/update/stop over one Icon object.

        The icon runs its own loop on a daemon thread; ``update`` swaps
        the image and tooltip (both documented as thread-safe property
        assignments). "Show worker status" raises a best-effort
        notification; a failed notification never raises into the model.
        """

        def __init__(self) -> None:
            self._icon = pystray.Icon(
                "scarcity-router-worker",
                icon=icon_image(STATE_DISCONNECTED),
                title="Scarcity Router worker",
                menu=pystray.Menu(
                    pystray.MenuItem(
                        "Show worker status",
                        lambda icon, item: self._notify(model.status_text()),
                        default=True,
                    ),
                    pystray.MenuItem(
                        "Open server control UI",
                        lambda icon, item: open_control_ui(),
                    ),
                    pystray.MenuItem(
                        "Reconnect / restart",
                        lambda icon, item: model.request_restart(),
                    ),
                    pystray.MenuItem(
                        "Open diagnostics folder",
                        lambda icon, item: self._open_folder(),
                    ),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem(
                        "Quit", lambda icon, item: model.request_quit()
                    ),
                ),
            )
            self._thread = None
            self._diagnostics_dir = diagnostics_dir

        def _notify(self, text: str) -> None:
            def show() -> None:
                try:
                    self._icon.notify(text)
                except Exception:
                    return

            threading.Thread(target=show, daemon=True).start()

        def _open_folder(self) -> None:
            # The documented Windows shell association open for the
            # worker's own state directory (the bounded redacted log).
            os.startfile(str(self._diagnostics_dir))

        def start(self) -> None:
            self._thread = threading.Thread(
                target=self._icon.run, name="scarcity-router-tray", daemon=True
            )
            self._thread.start()

        def update(self, *, state: str, text: str) -> None:
            self._icon.icon = icon_image(state)
            self._icon.title = text.replace("\n", " | ")

        def stop(self) -> None:
            try:
                self._icon.stop()
            except Exception:
                return

    return PystrayView()
