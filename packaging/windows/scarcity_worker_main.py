"""PyInstaller entry point for the packaged Windows worker executable.

M10 (issue #95): the spec file (``worker.spec`` beside this script)
builds the one-dir Windows worker package around this launcher. The
launcher intentionally contains no logic: everything (argument parsing,
allowlists, identity, transport, tray state machine, bounded log) is
library code; this module only wires the library's ``tray_main`` to the
packaging-owned pystray adapter
(``scarcity_worker_tray_view.py`` beside this script).
"""

import sys
from pathlib import Path

from scarcity_router.windows_tray import tray_main

# The packaging-side adapter sits beside this launcher; the spec bundles
# this directory, so make the import work regardless of the CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scarcity_worker_tray_view import build_tray_view  # noqa: E402 - packaging seam


def main() -> int:
    return tray_main(sys.argv[1:], view_factory=build_tray_view)


if __name__ == "__main__":
    raise SystemExit(main())
