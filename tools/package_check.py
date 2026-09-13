#!/usr/bin/env python3
"""Reproducible package artifact and isolated-install checks (issue #63).

Builds the distribution with ``uv build --no-sources``, inspects the wheel
and sdist contents and metadata, then installs the wheel into a throwaway
``uv tool`` environment and exercises the installed surface: console-script
help, default catalog/policy resource loading (catalog version 2, policy
version 6) without a checkout, cwd dependency or source-tree environment,
official MCP-client initialization and tool discovery with clean shutdown
and no stdout contamination, and a loopback REST ``/healthz`` smoke on a
kernel-assigned port with clean SIGINT shutdown. No live provider endpoint
is contacted. Nothing is published; no permanent tool installation or
global configuration is touched: the tool root, bin dir and HOME stay
inside one temporary directory that is removed at the end.

Usage: uv run python tools/package_check.py
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist"
WHEEL_NAME = "scarcity_router-0.1.0-py3-none-any.whl"
SDIST_NAME = "scarcity_router-0.1.0.tar.gz"
README_TAGLINE = "> Choose the least scarce model that is capable enough for the task."

EXPECTED_ENTRY_POINTS = {
    "console_scripts": {
        "scarcity-router": "scarcity_router.cli:main",
        "scarcity-router-mcp": "scarcity_router.mcp:main",
        "scarcity-router-server": "scarcity_router.server:main",
    }
}

EXPECTED_TOOLS = ["scarcity_status", "scarcity_select", "scarcity_simulate"]

FORBIDDEN_NAME_PARTS = (
    "tests/",
    "docs/",
    "tools/",
    "examples/",
    ".kilo/",
    "agents.md",
    "uv.lock",
    ".env",
    "credentials",
    "secrets",
    "sessions/",
    "quota-snapshots",
    "__pycache__",
    ".pyc",
)

# Hatchling always injects the root .gitignore and PKG-INFO into sdists.
SDIST_ALLOWED_EXTRA_NAMES = (
    "scarcity_router-0.1.0/.gitignore",
    "scarcity_router-0.1.0/PKG-INFO",
)

REQUIRED_WHEEL_NAMES = (
    "scarcity_router/__init__.py",
    "scarcity_router/__main__.py",
    "scarcity_router/cli.py",
    "scarcity_router/mcp.py",
    "scarcity_router/server.py",
    "scarcity_router/selection_app.py",
    "scarcity_router/model-catalog.json",
    "scarcity_router/model-policy.json",
    "scarcity_router/default-selector-policy.json",
    "scarcity_router-0.1.0.dist-info/METADATA",
    "scarcity_router-0.1.0.dist-info/entry_points.txt",
    "scarcity_router-0.1.0.dist-info/licenses/LICENSE",
)

REQUIRED_SDIST_NAMES = (
    "scarcity_router/__init__.py",
    "scarcity_router/__main__.py",
    "scarcity_router/cli.py",
    "scarcity_router/mcp.py",
    "scarcity_router/server.py",
    "scarcity_router/selection_app.py",
    "model-catalog.json",
    "model-policy.json",
    "examples/selector-policy.json",
    "pyproject.toml",
    "README.md",
    "LICENSE",
)


def _fail(message: str) -> None:
    print(f"package-check: FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def _check(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _metadata_field(metadata_text: str, header: str) -> str | None:
    for line in metadata_text.splitlines():
        if line.startswith(header):
            return line.split(":", 1)[1].strip()
    return None


def build_artifacts() -> None:
    result = _run(["uv", "build", "--no-sources"], cwd=REPO)
    _check(result.returncode == 0, f"uv build failed:\n{result.stderr}")
    for name in (WHEEL_NAME, SDIST_NAME):
        _check((DIST / name).is_file(), f"missing build artifact {name}")
    print("PASS uv build --no-sources produced wheel and sdist")


# D-036: the default user selector policy is deliberately packaged. The
# audited examples/selector-policy.json is the provisioning source (sdist)
# and its build-time mapping is the wheel resource; it is the single allowed
# exception to the blanket examples/ tripwire above.
ALLOWED_EXCEPTIONS = ("examples/selector-policy.json",)


def _forbid_entries(names: Sequence[str], kind: str) -> None:
    for name in names:
        lowered = name.lower()
        if lowered.endswith(ALLOWED_EXCEPTIONS):
            continue
        for part in FORBIDDEN_NAME_PARTS:
            _check(part not in lowered, f"{kind} contains forbidden entry {name} ({part})")


def inspect_wheel() -> None:
    with zipfile.ZipFile(DIST / WHEEL_NAME) as archive:
        names = archive.namelist()
        for required in REQUIRED_WHEEL_NAMES:
            _check(required in names, f"wheel missing {required}")
        _forbid_entries(names, "wheel")
        metadata = archive.read("scarcity_router-0.1.0.dist-info/METADATA").decode("utf-8")
        _check(_metadata_field(metadata, "Name") == "scarcity-router", "wheel METADATA name mismatch")
        _check(_metadata_field(metadata, "Version") == "0.1.0", "wheel METADATA version mismatch")
        _check(
            _metadata_field(metadata, "License-Expression") == "Apache-2.0",
            "wheel METADATA license expression mismatch",
        )
        _check(_metadata_field(metadata, "Requires-Python") == ">=3.12", "wheel Requires-Python mismatch")
        requires_dist = _metadata_field(metadata, "Requires-Dist") or ""
        _check(
            requires_dist in ("mcp>=2,<3", "mcp<3,>=2"),
            f"wheel Requires-Dist mismatch: {requires_dist}",
        )
        _check(README_TAGLINE in metadata, "wheel METADATA lacks the README description payload")
        entry_points = archive.read("scarcity_router-0.1.0.dist-info/entry_points.txt").decode("utf-8")
        parser = configparser.ConfigParser()
        parser.read_string(entry_points)
        sections = {section: dict(parser.items(section)) for section in parser.sections()}
        _check(sections == EXPECTED_ENTRY_POINTS, f"wheel entry points mismatch: {sections}")
    print("PASS wheel contents and metadata verified")


def _package_python_files() -> set[str]:
    return {
        str(path.relative_to(REPO))
        for path in sorted((REPO / "scarcity_router").rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def inspect_sdist() -> None:
    with tarfile.open(DIST / SDIST_NAME) as archive:
        names = [member.name for member in archive.getmembers()]
        allowed = {f"scarcity_router-0.1.0/{required}" for required in REQUIRED_SDIST_NAMES}
        allowed.update(f"scarcity_router-0.1.0/{relative}" for relative in _package_python_files())
        allowed.update(SDIST_ALLOWED_EXTRA_NAMES)
        # Exact allowlist: the sdist contains the minimal rebuild set and
        # nothing else (no tests, docs, tooling, local or secret material).
        _check(
            set(names) == allowed,
            "sdist content set mismatch; unexpected: "
            f"{sorted(set(names) - allowed)}; missing: {sorted(allowed - set(names))}",
        )
        _forbid_entries(names, "sdist")
    print("PASS sdist contents verified")


MCP_CLIENT_CHECK = """
import json
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import anyio

async def main() -> list:
    parameters = StdioServerParameters(command=COMMAND)
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams, read_timeout_seconds=5) as client:
            await client.initialize()
            tools = await client.list_tools()
    return [tool.name for tool in tools.tools]

names = anyio.run(main)
assert names == EXPECTED_TOOLS, names
print("ok")
"""


def check_mcp(bin_dir: Path, tool_python: Path, home: Path) -> None:
    code = MCP_CLIENT_CHECK.replace("COMMAND", json.dumps(str(bin_dir / "scarcity-router-mcp"))).replace(
        "EXPECTED_TOOLS", repr(EXPECTED_TOOLS)
    )
    result = _run([str(tool_python), "-I", "-c", code], cwd=home, env=_clean_env(home))
    _check(
        result.returncode == 0 and result.stdout.strip().endswith("ok"),
        f"installed MCP init/discovery failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
    )
    print("PASS installed MCP initialization and tool discovery with clean shutdown")


RESOURCE_CHECK = """
import scarcity_router
from scarcity_router.selection_app import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_MODEL_POLICY_PATH,
    load_configured_artifacts,
)

package_dir = scarcity_router.__file__.rsplit("/", 1)[0]
assert not package_dir.startswith(CHECKOUT), package_dir
for path in (DEFAULT_CATALOG_PATH, DEFAULT_MODEL_POLICY_PATH):
    assert str(path).startswith(package_dir + "/"), (path, package_dir)
catalog, _profiles, policy_version = load_configured_artifacts(
    DEFAULT_CATALOG_PATH, DEFAULT_MODEL_POLICY_PATH
)
assert catalog.catalog_version == 2, catalog.catalog_version
assert policy_version == 6, policy_version
assert scarcity_router.get_version() == "0.1.0", scarcity_router.get_version()
print("ok")
"""


def check_installed_resources(tool_python: Path, home: Path) -> None:
    code = RESOURCE_CHECK.replace("CHECKOUT", repr(str(REPO)))
    result = _run([str(tool_python), "-I", "-c", code], cwd=home, env=_clean_env(home))
    _check(
        result.returncode == 0 and result.stdout.strip().endswith("ok"),
        f"installed resource loading failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
    )
    print("PASS installed defaults load catalog 2 / policy 6 without checkout, cwd or source env")


def check_cli_scripts(bin_dir: Path, home: Path) -> None:
    for script in ("scarcity-router", "scarcity-router-server"):
        result = _run([str(bin_dir / script), "--help"], cwd=home, env=_clean_env(home))
        _check(result.returncode == 0, f"{script} --help failed:\n{result.stderr}")
        _check("usage:" in result.stdout, f"{script} --help lacks usage output")
    print("PASS installed console-script help for scarcity-router and scarcity-router-server")


def check_rest(bin_dir: Path, home: Path) -> None:
    server = subprocess.Popen(
        [str(bin_dir / "scarcity-router-server"), "--port", "0"],
        cwd=str(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_clean_env(home),
    )
    try:
        listening = server.stdout.readline() if server.stdout is not None else ""
        if not listening.startswith("scarcity-router REST listening on http://127.0.0.1:"):
            stderr = server.stderr.read() if server.stderr is not None else ""
            _fail(f"installed REST server did not report a loopback listener: {listening!r}\n{stderr}")
        port = int(listening.rsplit(":", 1)[1].strip())
        request = Request(f"http://127.0.0.1:{port}/healthz", headers={"Host": f"127.0.0.1:{port}"})
        with urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        _check(response.status == 200 and body == {"status": "ok"}, f"healthz mismatch: {response.status} {body}")
    finally:
        server.send_signal(signal.SIGINT)
        try:
            returncode = server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            _ = server.wait(timeout=10)
            _fail("installed REST server did not shut down on SIGINT")
    _check(returncode == 0, f"installed REST server exit code {returncode}")
    print("PASS installed loopback REST /healthz smoke with clean shutdown")


def _clean_env(home: Path) -> dict[str, str]:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
    for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_PYTHON_INSTALL_DIR", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        _ = environment.pop(name, None)
    return environment


def isolated_install_checks() -> None:
    with tempfile.TemporaryDirectory(prefix="scarcity-router-package-check-") as temporary:
        root = Path(temporary)
        tool_dir = root / "tools"
        bin_dir = root / "bin"
        home = root / "home"
        _ = home.mkdir()
        install_env = {
            **os.environ,
            "UV_TOOL_DIR": str(tool_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "HOME": str(home),
        }
        result = _run(["uv", "tool", "install", str(DIST / WHEEL_NAME)], env=install_env)
        _check(result.returncode == 0, f"isolated uv tool install failed:\n{result.stderr}")
        scripts = sorted(entry.name for entry in bin_dir.iterdir())
        expected = sorted(["scarcity-router", "scarcity-router-mcp", "scarcity-router-server"])
        _check(scripts == expected, f"installed scripts {scripts} != {expected}")
        print(f"PASS isolated temporary uv tool install exposes exactly {expected}")
        tool_python = tool_dir / "scarcity-router" / "bin" / "python"
        _check(tool_python.is_file(), f"tool environment python missing at {tool_python}")
        check_cli_scripts(bin_dir, home)
        check_installed_resources(tool_python, home)
        check_mcp(bin_dir, tool_python, home)
        check_rest(bin_dir, home)
    print("PASS temporary tool environment removed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.parse_args()
    build_artifacts()
    inspect_wheel()
    inspect_sdist()
    isolated_install_checks()
    print("package-check: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
