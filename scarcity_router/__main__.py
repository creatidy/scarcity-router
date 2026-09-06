"""Provisional module entry point: status, select and simulate (M2e).

Dispatches through the top-level CLI; the read-only ``status`` behavior and
its direct entry point (``scarcity_router.status.main``) are preserved.
"""

from __future__ import annotations

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
