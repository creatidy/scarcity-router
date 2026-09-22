"""Windows worker packaging-spec verification (M10, issue #95).

The PyInstaller spec (``packaging/windows/worker.spec``) cannot run on a
non-Windows development host, so this suite pins its shape statically:

- the spec file is executed with stubbed PyInstaller callables that
  record every argument, and the recorded build must be the promised
  artifact — a one-dir, windowless ``scarcity-worker`` executable around
  the library launcher, with the win32 tray backend pinned as a hidden
  import;
- the packaging-side pystray adapter imports on every platform (lazy GUI
  imports), refuses to build a view off Windows, and carries no
  module-level GUI imports;
- the launcher stays a thin delegation to the library and the adapter.

The produced artifact itself is verified on the tag-driven Windows
release job, with live end-user acceptance recorded as
EXTERNAL_ACCEPTANCE_GATE: LIVE_WINDOWS_ACCEPTANCE
(docs/m10-acceptance.md).
"""

from __future__ import annotations

import ast
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, override

from scarcity_router.windows_tray import TrayStateModel, TrayView

REPO = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO / "packaging" / "windows" / "worker.spec"
LAUNCHER_PATH = REPO / "packaging" / "windows" / "scarcity_worker_main.py"
ADAPTER_PATH = REPO / "packaging" / "windows" / "scarcity_worker_tray_view.py"

BuildCall = tuple[str, tuple[object, ...], dict[str, object]]


class _BuildArtifact:
    """Inert stand-in for the Analysis/PYZ results the spec consumes."""

    name: str
    scripts: list[object]
    pure: list[object]
    binaries: list[object]
    datas: list[object]
    zipfiles: list[object]

    def __init__(self, name: str) -> None:
        self.name = name
        self.scripts = []
        self.pure = []
        self.binaries = []
        self.datas = []
        self.zipfiles = []


class _Recorder:
    """Records one PyInstaller build call and returns inert children."""

    _name: str
    _calls: list[BuildCall]

    def __init__(self, name: str, calls: list[BuildCall]) -> None:
        self._name = name
        self._calls = calls

    def __call__(self, *args: object, **kwargs: object) -> _BuildArtifact:
        self._calls.append((self._name, args, kwargs))
        return _BuildArtifact(self._name)


class SpecShapeTests(unittest.TestCase):
    calls: list[BuildCall]

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.calls = []

    @override
    def setUp(self) -> None:
        self.calls = []
        namespace: dict[str, object] = {
            "Analysis": _Recorder("Analysis", self.calls),
            "PYZ": _Recorder("PYZ", self.calls),
            "EXE": _Recorder("EXE", self.calls),
            "COLLECT": _Recorder("COLLECT", self.calls),
        }
        code = compile(SPEC_PATH.read_text(encoding="utf-8"), str(SPEC_PATH), "exec")
        exec(code, namespace)  # noqa: S102 - trusted in-repo spec, stubbed build

    def _one(self, name: str) -> tuple[tuple[object, ...], dict[str, object]]:
        matches = [(args, kwargs) for call_name, args, kwargs in self.calls if call_name == name]
        self.assertEqual(1, len(matches), f"expected exactly one {name} call")
        return matches[0]

    def test_analysis_entry_is_the_packaging_launcher(self) -> None:
        args, _kwargs = self._one("Analysis")
        self.assertEqual((["scarcity_worker_main.py"],), args)

    def test_analysis_pins_the_win32_tray_stack(self) -> None:
        _args, kwargs = self._one("Analysis")
        hidden = kwargs.get("hiddenimports")
        if not isinstance(hidden, list):
            raise AssertionError("hiddenimports must be a list")
        hidden_names: list[object] = cast("list[object]", hidden)
        for required in (
            "pystray",
            "pystray._win32",
            "PIL",
            "PIL.Image",
            "PIL.ImageDraw",
        ):
            self.assertIn(required, hidden_names)

    def test_analysis_bundles_no_datas_or_binaries(self) -> None:
        # No bundled data files and no extra binaries: everything is the
        # library plus the tray extra's own package contents.
        _args, kwargs = self._one("Analysis")
        self.assertEqual([], kwargs.get("datas"))
        self.assertEqual([], kwargs.get("binaries"))

    def test_exe_is_windowless_named_scarcity_worker(self) -> None:
        _args, kwargs = self._one("EXE")
        self.assertEqual("scarcity-worker", kwargs.get("name"))
        self.assertIs(False, kwargs.get("console"))
        # exclude_binaries=True + a COLLECT step = the one-dir build.
        self.assertIs(True, kwargs.get("exclude_binaries"))

    def test_collect_makes_it_a_one_dir_build(self) -> None:
        _args, kwargs = self._one("COLLECT")
        self.assertEqual("scarcity-worker", kwargs.get("name"))


class LauncherShapeTests(unittest.TestCase):
    def test_launcher_is_a_thin_delegation(self) -> None:
        tree = ast.parse(LAUNCHER_PATH.read_text(encoding="utf-8"))
        imported = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        ]
        self.assertIn("scarcity_router.windows_tray", imported)
        self.assertIn("scarcity_worker_tray_view", imported)
        # The launcher defines no classes: all behavior stays in the
        # library and the packaging adapter.
        top_level_classes = [
            node for node in tree.body if isinstance(node, ast.ClassDef)
        ]
        self.assertEqual([], top_level_classes)

    def test_spec_launcher_and_adapter_exist_in_tree(self) -> None:
        self.assertTrue(SPEC_PATH.is_file())
        self.assertTrue(LAUNCHER_PATH.is_file())
        self.assertTrue(ADAPTER_PATH.is_file())


@dataclass(frozen=True)
class _Adapter:
    """The typed surface of the packaging adapter the tests drive."""

    is_windows: Callable[[], bool]
    build_tray_view: Callable[[TrayStateModel, Callable[[], None], Path], TrayView]


class _AdapterModule(Protocol):
    """Module-level protocol for the safe double cast below."""

    def is_windows(self) -> bool: ...

    def build_tray_view(
        self,
        model: TrayStateModel,
        open_control_ui: Callable[[], None],
        diagnostics_dir: Path,
    ) -> TrayView: ...


def _load_adapter_module() -> _Adapter:
    """Import the packaging-side pystray adapter by file path.

    The module is safe to import on every platform: pystray and Pillow
    are imported lazily inside ``build_tray_view`` only.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("scarcity_worker_tray_view", ADAPTER_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - loader seam
        raise AssertionError("adapter module spec unavailable")
    module = importlib.util.module_from_spec(spec)
    _ = spec.loader.exec_module(module)
    typed = cast("_AdapterModule", cast("object", module))
    return _Adapter(is_windows=typed.is_windows, build_tray_view=typed.build_tray_view)


class _NullViewProbe:
    """Minimal TrayStateModel view stand-in (no behavior needed)."""

    def start(self) -> None:
        return None

    def update(self, *, state: str, text: str) -> None:
        _ = state, text

    def stop(self) -> None:
        return None


class TrayViewAdapterTests(unittest.TestCase):
    def test_adapter_imports_on_every_platform(self) -> None:
        # pystray/Pillow are NOT installed here; the module-level import
        # succeeding is the lazy-import discipline assertion.
        adapter = _load_adapter_module()
        self.assertTrue(callable(adapter.build_tray_view))

    def test_adapter_refuses_off_windows_with_remediation(self) -> None:
        from scarcity_router.windows_tray import TrayNotAvailableError

        adapter = _load_adapter_module()
        if adapter.is_windows():
            self.skipTest("tray adapter is supported on this platform")
        model = TrayStateModel(view=_NullViewProbe())
        with self.assertRaises(TrayNotAvailableError) as ctx:
            _ = adapter.build_tray_view(model, lambda: None, REPO)
        self.assertIn("Windows-only", str(ctx.exception))

    def test_adapter_has_no_module_level_gui_imports(self) -> None:
        tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"))
        module_level_names: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                module_level_names.append(node.module)
            elif isinstance(node, ast.Import):
                module_level_names.extend(alias.name for alias in node.names)
        for name in module_level_names:
            self.assertNotIn("pystray", name)
            self.assertNotIn("PIL", name)


if __name__ == "__main__":
    _ = unittest.main()
