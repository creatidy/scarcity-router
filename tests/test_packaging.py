"""Focused packaging tests (issue #63, D-034).

Covers the distribution version contract, the source-tree/installed default
artifact resolution and the console-script surface. Artifact content itself
(catalog version 2, policy version 6) is asserted through the authoritative
loader, not by re-parsing the JSON documents. Installed-package behavior
(isolated tool install, wheel/sdist contents, MCP/REST smoke) is verified by
``tools/package_check.py``, not here.
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Mapping
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest import mock

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from scarcity_router import get_version
from scarcity_router.cli import build_parser as build_cli_parser
from scarcity_router.mcp import build_server
from scarcity_router.selection_app import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_MODEL_POLICY_PATH,
    REPO_ROOT,
    load_configured_artifacts,
    resolve_default_artifact,
)
from scarcity_router.server import build_parser as build_server_parser

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "scarcity_router"
EXPECTED_CATALOG_VERSION = 2
EXPECTED_POLICY_VERSION = 6

EXPECTED_SCRIPTS = {
    "scarcity-router": "scarcity_router.cli:main",
    "scarcity-router-mcp": "scarcity_router.mcp:main",
    "scarcity-router-server": "scarcity_router.server:main",
}


class VersionTests(unittest.TestCase):
    """The installed package version replaces milestone branding."""

    def test_source_literal_is_the_distribution_version(self) -> None:
        import scarcity_router

        self.assertEqual("0.1.0", scarcity_router.__version__)

    def test_get_version_prefers_installed_metadata_with_fallback(self) -> None:
        # In the development environment the distribution is either absent
        # (deterministic source fallback) or installed at the same version;
        # both must yield the same normalized distribution version.
        self.assertEqual("0.1.0", get_version())
        self.assertIsNotNone(re.fullmatch(r"\d+\.\d+\.\d+", get_version()))


class PackagingMetadataTests(unittest.TestCase):
    """The committed packaging metadata matches the frozen decisions."""

    def _project_table(self) -> Mapping[str, object]:
        with (REPO / "pyproject.toml").open("rb") as handle:
            document = cast(dict[str, object], tomllib.load(handle))
        return cast(Mapping[str, object], document["project"])

    def test_pyproject_project_table(self) -> None:
        project = self._project_table()
        self.assertEqual("scarcity-router", project["name"])
        dynamic = project["dynamic"]
        self.assertIsInstance(dynamic, list)
        self.assertIn("version", cast("list[object]", dynamic))
        self.assertEqual("Apache-2.0", project["license"])
        self.assertEqual(">=3.12", project["requires-python"])
        self.assertEqual(["mcp>=2,<3"], project["dependencies"])

    def test_exactly_the_three_console_scripts(self) -> None:
        project = self._project_table()
        scripts = project["scripts"]
        self.assertIsInstance(scripts, dict)
        self.assertEqual(EXPECTED_SCRIPTS, cast("dict[str, str]", scripts))

    def test_no_committed_artifact_duplicates_in_package(self) -> None:
        # The root copies stay the single committed authoritative artifacts;
        # the wheel maps them in at build time (force-include), so a checkout
        # must not carry packaged duplicates.
        self.assertFalse((PACKAGE_DIR / "model-catalog.json").is_file())
        self.assertFalse((PACKAGE_DIR / "model-policy.json").is_file())


class DefaultArtifactTests(unittest.TestCase):
    """Default catalog/policy resolution for both execution contexts."""

    def test_source_tree_defaults_are_the_root_authoritative_copies(self) -> None:
        self.assertEqual(REPO_ROOT / "model-catalog.json", DEFAULT_CATALOG_PATH)
        self.assertEqual(REPO_ROOT / "model-policy.json", DEFAULT_MODEL_POLICY_PATH)
        self.assertTrue(DEFAULT_CATALOG_PATH.is_file())
        self.assertTrue(DEFAULT_MODEL_POLICY_PATH.is_file())

    def test_source_tree_defaults_load_catalog_2_policy_6(self) -> None:
        catalog, _profiles, policy_version = load_configured_artifacts(
            DEFAULT_CATALOG_PATH, DEFAULT_MODEL_POLICY_PATH
        )
        self.assertEqual(EXPECTED_CATALOG_VERSION, catalog.catalog_version)
        self.assertEqual(EXPECTED_POLICY_VERSION, policy_version)

    def test_resolution_prefers_an_existing_source_root_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "model-catalog.json"
            _ = marker.write_text("{}", encoding="utf-8")
            self.assertEqual(marker, resolve_default_artifact("model-catalog.json", source_root=root))

    def test_resolution_falls_back_to_the_packaged_resource(self) -> None:
        # Without a source-root copy the packaged resource inside
        # scarcity_router is used: the importlib.resources location of a
        # filesystem-backed package is the package directory itself.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("model-catalog.json", "model-policy.json"):
                resolved = resolve_default_artifact(name, source_root=root)
                self.assertEqual(PACKAGE_DIR / name, resolved)


class ConsoleScriptSurfaceTests(unittest.TestCase):
    """Parser branding follows the installed entry-point invocation."""

    def test_cli_prog_tracks_the_invoked_entry_point(self) -> None:
        for argv, expected in (
            ("/prefix/bin/scarcity-router", "scarcity-router"),
            ("scarcity-router", "scarcity-router"),
            ("/repo/scarcity_router/__main__.py", "python -m scarcity_router"),
        ):
            with self.subTest(argv=argv):
                with mock.patch.object(sys, "argv", [argv]):
                    self.assertEqual(expected, build_cli_parser().prog)

    def test_server_prog_tracks_the_invoked_entry_point(self) -> None:
        for argv, expected in (
            ("/prefix/bin/scarcity-router-server", "scarcity-router-server"),
            ("scarcity-router-server", "scarcity-router-server"),
            ("/repo/scarcity_router/server.py", "python -m scarcity_router.server"),
        ):
            with self.subTest(argv=argv):
                with mock.patch.object(sys, "argv", [argv]):
                    self.assertEqual(expected, build_server_parser().prog)


async def _server_info_async() -> tuple[str, str]:
    server = build_server()
    result: tuple[str, str] | None = None
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(
                server.run,
                server_streams[0],
                server_streams[1],
                server.create_initialization_options(),
            )
            try:
                with anyio.fail_after(10):
                    async with ClientSession(
                        *client_streams, read_timeout_seconds=5
                    ) as client:
                        initialization = await client.initialize()
                        result = (
                        initialization.server_info.name,
                        initialization.server_info.version or "",
                        )
            finally:
                tasks.cancel_scope.cancel()
    if result is None:
        raise AssertionError("MCP initialization did not return")
    return result


class McpServerVersionTests(unittest.TestCase):
    """The MCP server advertises the installed package version, not a milestone."""

    def test_initialize_reports_package_version(self) -> None:
        name, version = anyio.run(_server_info_async)
        self.assertEqual("scarcity-router", name)
        self.assertEqual(get_version(), version)


class HelpSurfaceTests(unittest.TestCase):
    """Console-script help renders for the dispatcher and the REST server."""

    def test_help_renders_without_tracing(self) -> None:
        for parser in (build_cli_parser(), build_server_parser()):
            captured = io.StringIO()
            with redirect_stdout(captured):
                with self.assertRaises(SystemExit) as ctx:
                    _ = parser.parse_args(["--help"])
            self.assertEqual(0, ctx.exception.code)
            self.assertIn("usage:", captured.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
