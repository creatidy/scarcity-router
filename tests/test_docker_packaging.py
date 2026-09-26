"""Docker image build-context parity (issue #129).

The canonical server image (``Dockerfile``, M10) builds the wheel inside a
minimal build stage: only the files its ``COPY`` lines bring into ``/build``
exist when hatchling resolves ``pyproject.toml``. A wheel force-include
source that exists at the repository root but is never copied there passes
every checkout-based gate (``make check``, ``make package-check`` — both
build from the full tree) and still breaks the canonical image build —
exactly the D-053 ``model-tracks.json`` rollout failure.

Running the real ``docker build`` inside the hermetic test gate would need
a Docker daemon plus registry and PyPI network access, so — like
``test_windows_packaging.py`` pins the unrunnable PyInstaller spec — this
suite pins the build-stage parity statically: every packaging input the
wheel build declares in ``pyproject.toml`` (the ``force-include`` sources
and the ``packages`` trees) must be materialized by a ``COPY`` in the
Dockerfile build stage, i.e. before the ``RUN pip wheel`` step. The
produced image itself stays verified by the canonical
``docker build -t scarcity-router .``, which remains the mandatory
acceptance check for image-affecting work.

The check deliberately reads only the stage that feeds the wheel build and
does not interpret dest paths or later runtime stages: it pins that each
declared input reaches the build stage at all.
"""

from __future__ import annotations

import shlex
import tomllib
import unittest
from pathlib import Path
from typing import cast

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE_PATH = REPO / "Dockerfile"
PYPROJECT_PATH = REPO / "pyproject.toml"


def _wheel_packaging_inputs() -> tuple[set[str], set[str]]:
    """Return the wheel build's declared (force_include_sources, package_trees),
    both repo-root-relative, read from the authoritative pyproject.toml."""
    with PYPROJECT_PATH.open("rb") as handle:
        document = cast(dict[str, object], tomllib.load(handle))
    tool = cast(dict[str, object], document["tool"])
    hatch = cast(dict[str, object], tool["hatch"])
    build = cast(dict[str, object], hatch["build"])
    targets = cast(dict[str, object], build["targets"])
    wheel = cast(dict[str, object], targets["wheel"])
    force_include = cast(dict[str, str], wheel["force-include"])
    packages = cast("list[str]", wheel["packages"])
    return set(force_include), set(packages)


def _build_stage_copy_sources() -> set[str]:
    """Repo-root-relative paths the Dockerfile build stage COPYs into /build.

    Narrow extraction for the parity property only: the stage from the first
    ``FROM ... AS build`` to the next ``FROM``, with line continuations
    folded, flag tokens dropped and each COPY's leading sources kept (the
    last operand is the destination). Directory sources keep their trailing
    slash so prefix coverage works. JSON-array COPY form and dest-path
    resolution are out of scope; the Dockerfile does not use them in this
    stage.
    """
    folded = DOCKERFILE_PATH.read_text(encoding="utf-8").replace("\\\n", " ")
    sources: set[str] = set()
    in_build_stage = False
    for raw_line in folded.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = shlex.split(line)
        if tokens[0].upper() == "FROM":
            if in_build_stage:
                break
            in_build_stage = len(tokens) >= 4 and tokens[-2].upper() == "AS" and tokens[-1] == "build"
            continue
        if not in_build_stage or tokens[0].upper() != "COPY":
            continue
        operands = [token for token in tokens[1:] if not token.startswith("--")]
        if len(operands) < 2:
            continue
        sources.update(operands[:-1])
    return sources


def _covered(sources: set[str], target: str) -> bool:
    for source in sources:
        if source.rstrip("/") == target:
            return True
        if source.endswith("/") and target.startswith(source):
            return True
    return False


class DockerBuildContextParityTests(unittest.TestCase):
    """Every wheel packaging input reaches the Dockerfile build stage."""

    def test_force_include_sources_are_in_the_build_stage(self) -> None:
        force_include, _packages = _wheel_packaging_inputs()
        self.assertTrue(force_include, "pyproject.toml declares no wheel force-include entries")
        copied = _build_stage_copy_sources()
        missing = sorted(source for source in force_include if not _covered(copied, source))
        self.assertEqual([], missing, f"wheel force-include sources absent from the Dockerfile build stage: {missing}")

    def test_force_include_sources_are_committed(self) -> None:
        force_include, _packages = _wheel_packaging_inputs()
        absent = sorted(source for source in force_include if not (REPO / source).is_file())
        self.assertEqual([], absent, f"wheel force-include sources missing from the repository: {absent}")

    def test_wheel_package_trees_are_in_the_build_stage(self) -> None:
        _force_include, packages = _wheel_packaging_inputs()
        copied = _build_stage_copy_sources()
        missing = sorted(tree for tree in packages if not _covered(copied, tree))
        self.assertEqual([], missing, f"wheel package trees absent from the Dockerfile build stage: {missing}")


if __name__ == "__main__":
    _ = unittest.main()
