# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Scarcity Router native Windows worker (M10, #95).

One-dir build (the COLLECT step) bundling the interpreter so users never
install Python for the normal Windows worker path. The optional tray
stack (pystray + Pillow + its win32 backend) is pulled in through the
``tray`` extra, which the release workflow installs before invoking
PyInstaller; the hidden import pins the backend pystray selects on
Windows so the analysis never silently ships a tray-less build.

Verified statically on non-Windows hosts (see
``tests/test_windows_packaging.py``, which parses this spec with stubbed
PyInstaller callables and asserts the shape below); the produced
artifact itself is verified on the tag-driven Windows release job, with
live end-user acceptance recorded as
EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE
(docs/m10-acceptance.md).
"""

a = Analysis(
    ["scarcity_worker_main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "pystray",
        "pystray._win32",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="scarcity-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="scarcity-worker",
)
