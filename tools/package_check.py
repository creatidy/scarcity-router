#!/usr/bin/env python3
"""Reproducible package artifact and isolated-install checks (issue #63).

Builds the distribution with ``uv build --no-sources``, inspects the wheel
and sdist contents and metadata, then runs one isolated installation
lifecycle (issue #186) inside a throwaway ``uv tool`` environment:

1. fresh install of the current local wheel plus the installed-surface
   smokes: console-script help, default catalog/policy resource loading
   (the versions expected are read from the root-authoritative artifacts,
   so this check never pins them) without a checkout, cwd dependency or
   source-tree environment, official MCP-client initialization and tool
   discovery with clean shutdown and no stdout contamination, and a
   loopback REST ``/healthz`` smoke on a kernel-assigned port with clean
   SIGINT shutdown;
2. synthetic operator state seeded through the installed public APIs at
   the default paths — a selector policy derived from the packaged default
   with one harmless rule id replaced, and one neutral server-store
   configuration record with a harmless ttl value — no credentials,
   prompts or account data; the state lives under the temporary HOME,
   never inside the tool environment;
3. same-version local-wheel replacement (``uv tool install --force``) of
   the exact same wheel, then the installed-surface smokes again and
   proof that the seeded user policy bytes and the logical configuration
   record/schema are retained. This is a same-version reinstall proof,
   NOT an old-release→new-release upgrade proof (no release exists);
4. ``uv tool uninstall`` of only the temporary tool, then proof that the
   four entry points and the tool environment are gone (no leftover
   REST/MCP process — every smoke above joins its server) while the
   synthetic operator state is retained: uninstall removes the tool, it
   never purges operator state.

No live provider endpoint is contacted. Nothing is published; no permanent
tool installation or global configuration is touched: the tool root, bin
dir and HOME stay inside one temporary directory that is removed at the
end. Build and install subprocesses receive a deliberate environment
allowlist — ambient ``os.environ`` is never inherited wholesale.

Usage: uv run python tools/package_check.py
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPResponse
from pathlib import Path
from typing import NoReturn, cast
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist"
README_TAGLINE = "> Choose the least scarce model that is capable enough for the task."

# The distribution version is read from the single authoritative source
# literal in scarcity_router/__init__.py — the same literal hatch reads for
# build metadata — so this check never pins a version and stays valid for
# release builds of any version (issue #97).
_VERSION_PATTERN = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


def _source_version() -> str:
    text = (REPO / "scarcity_router" / "__init__.py").read_text(encoding="utf-8")
    match = _VERSION_PATTERN.search(text)
    if match is None:
        raise SystemExit("package-check: FAIL: cannot read __version__ from scarcity_router/__init__.py")
    return match.group(1)


VERSION = _source_version()
WHEEL_NAME = f"scarcity_router-{VERSION}-py3-none-any.whl"
SDIST_NAME = f"scarcity_router-{VERSION}.tar.gz"


def _root_artifact_versions() -> tuple[int, int]:
    """Return the expected packaged catalog/policy versions from the root
    artifacts — the same files hatch force-includes into the wheel — so the
    smoke check never pins stale numbers."""
    catalog = cast(
        "dict[str, object]",
        json.loads((REPO / "model-catalog.json").read_text(encoding="utf-8")),
    )
    policy = cast(
        "dict[str, object]",
        json.loads((REPO / "model-policy.json").read_text(encoding="utf-8")),
    )
    try:
        return int(cast(int, catalog["catalog_version"])), int(cast(int, policy["policy_version"]))
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"package-check: FAIL: cannot read artifact versions: {error}") from error


EXPECTED_CATALOG_VERSION, EXPECTED_POLICY_VERSION = _root_artifact_versions()

EXPECTED_ENTRY_POINTS = {
    "console_scripts": {
        "scarcity-router": "scarcity_router.cli:main",
        "scarcity-router-mcp": "scarcity_router.mcp:main",
        "scarcity-router-server": "scarcity_router.server:main",
        "scarcity-router-worker": "scarcity_router.worker_client:main",
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
    f"scarcity_router-{VERSION}/.gitignore",
    f"scarcity_router-{VERSION}/PKG-INFO",
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
    "scarcity_router/model-tracks.json",
    "scarcity_router/default-selector-policy.json",
    f"scarcity_router-{VERSION}.dist-info/METADATA",
    f"scarcity_router-{VERSION}.dist-info/entry_points.txt",
    f"scarcity_router-{VERSION}.dist-info/licenses/LICENSE",
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
    "model-tracks.json",
    "examples/selector-policy.json",
    "pyproject.toml",
    "README.md",
    "LICENSE",
)


def _fail(message: str) -> NoReturn:
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
    # The build subprocess runs under the same deliberate allowlist as the
    # install subprocesses: no wholesale ambient environment inheritance.
    with tempfile.TemporaryDirectory(prefix="scarcity-router-package-check-build-") as temporary:
        home = Path(temporary) / "home"
        _ = home.mkdir()
        result = _run(["uv", "build", "--no-sources"], cwd=REPO, env=_allowlist_env(home))
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
        metadata = archive.read(f"scarcity_router-{VERSION}.dist-info/METADATA").decode("utf-8")
        _check(_metadata_field(metadata, "Name") == "scarcity-router", "wheel METADATA name mismatch")
        _check(_metadata_field(metadata, "Version") == VERSION, "wheel METADATA version mismatch")
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
        entry_points = archive.read(f"scarcity_router-{VERSION}.dist-info/entry_points.txt").decode("utf-8")
        parser = configparser.ConfigParser()
        parser.read_string(entry_points)
        sections = {section: dict(parser.items(section)) for section in parser.sections()}
        _check(sections == EXPECTED_ENTRY_POINTS, f"wheel entry points mismatch: {sections}")
        # The optional tray and exact-pinned MI extras are metadata only: the core
        # wheel's runtime dependency set is still exactly mcp — the extra's
        # packages are extra-conditional Requires-Dist entries, never core.
        extra_requires = sorted(
            line.split(":", 1)[1].strip()
            for line in metadata.splitlines()
            if line.startswith("Requires-Dist:")
        )
        conditional = sorted(
            requirement
            for requirement in extra_requires
            if "extra ==" in requirement
        )
        core = [requirement for requirement in extra_requires if "extra ==" not in requirement]
        _check(
            core in (["mcp>=2,<3"], ["mcp<3,>=2"]),
            f"core runtime dependencies changed: {core}",
        )
        joined_conditionals = "\n".join(conditional).lower()
        _check(
            len(conditional) == 3
            and "pystray" in joined_conditionals
            and "pillow" in joined_conditionals
            and (
                "model-intelligence @ git+https://forgejo.creatidy.com/Creatidy/"
                "model-intelligence.git@d65891668b7e79f7b666c021f02a1b4bf8c4ca9c"
            ).lower() in joined_conditionals,
            f"unexpected conditional extra dependencies: {conditional}",
        )
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
        allowed = {f"scarcity_router-{VERSION}/{required}" for required in REQUIRED_SDIST_NAMES}
        allowed.update(f"scarcity_router-{VERSION}/{relative}" for relative in _package_python_files())
        allowed.update(SDIST_ALLOWED_EXTRA_NAMES)
        # Exact allowlist: the sdist contains the minimal rebuild set and
        # nothing else (no tests, docs, tooling, local or secret material).
        unexpected = sorted(set(names) - allowed)
        missing = sorted(allowed - set(names))
        _check(
            set(names) == allowed,
            f"sdist content set mismatch; unexpected: {unexpected}; missing: {missing}",
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
    result = _run([str(tool_python), "-I", "-c", code], cwd=home, env=clean_env(home))
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
assert catalog.catalog_version == @CATALOG_VERSION@, catalog.catalog_version
assert policy_version == @POLICY_VERSION@, policy_version
assert scarcity_router.get_version() == @VERSION@, scarcity_router.get_version()
print("ok")
"""


def check_installed_resources(tool_python: Path, home: Path) -> None:
    code = (
        RESOURCE_CHECK.replace("CHECKOUT", repr(str(REPO)))
        .replace("@VERSION@", repr(VERSION))
        .replace("@CATALOG_VERSION@", repr(EXPECTED_CATALOG_VERSION))
        .replace("@POLICY_VERSION@", repr(EXPECTED_POLICY_VERSION))
    )
    result = _run([str(tool_python), "-I", "-c", code], cwd=home, env=clean_env(home))
    _check(
        result.returncode == 0 and result.stdout.strip().endswith("ok"),
        f"installed resource loading failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
    )
    print(
        f"PASS installed defaults load catalog {EXPECTED_CATALOG_VERSION} / policy {EXPECTED_POLICY_VERSION} without checkout, cwd or source env"
    )


def check_cli_scripts(bin_dir: Path, home: Path) -> None:
    for script in ("scarcity-router", "scarcity-router-server", "scarcity-router-worker"):
        result = _run([str(bin_dir / script), "--help"], cwd=home, env=clean_env(home))
        _check(result.returncode == 0, f"{script} --help failed:\n{result.stderr}")
        _check("usage:" in result.stdout, f"{script} --help lacks usage output")
    print(
        "PASS installed console-script help for scarcity-router, "
        + "scarcity-router-server and scarcity-router-worker"
    )


def check_rest(bin_dir: Path, home: Path) -> None:
    server = subprocess.Popen(
        [str(bin_dir / "scarcity-router-server"), "--port", "0"],
        cwd=str(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=clean_env(home),
    )
    try:
        listening = server.stdout.readline() if server.stdout is not None else ""
        if not listening.startswith("scarcity-router REST listening on http://127.0.0.1:"):
            stderr = server.stderr.read() if server.stderr is not None else ""
            _fail(f"installed REST server did not report a loopback listener: {listening!r}\n{stderr}")
        port = int(listening.rsplit(":", 1)[1].strip())
        request = Request(f"http://127.0.0.1:{port}/healthz", headers={"Host": f"127.0.0.1:{port}"})
        with cast("HTTPResponse", urlopen(request, timeout=10)) as response:
            body = cast(object, json.loads(response.read().decode("utf-8")))
        _check(
            response.status == 200 and body == {"status": "ok"},
            f"healthz mismatch: {response.status} {body}",
        )
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


def clean_env(home: Path) -> dict[str, str]:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
    for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_PYTHON_INSTALL_DIR", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        _ = environment.pop(name, None)
    return environment


def _allowlist_env(home: Path) -> dict[str, str]:
    """A deliberate allowlist for uv build/install subprocesses.

    Ambient ``os.environ`` is never inherited wholesale: a subprocess gets
    only PATH and a synthetic HOME, plus the operational uv cache location
    when the calling environment already pinned one (the value is passed
    through, never printed). Every other ambient entry — credentials,
    interpreter-isolation variables (PYTHONPATH, PYTHONHOME, VIRTUAL_ENV),
    index, proxy or other network settings — is excluded by construction.
    """
    environment = clean_env(home)
    cache = os.environ.get("UV_CACHE_DIR")
    if cache:
        environment["UV_CACHE_DIR"] = cache
    return environment


def install_env(home: Path, tool_dir: Path, bin_dir: Path) -> dict[str, str]:
    """The allowlist above plus the temporary uv tool locations."""
    environment = _allowlist_env(home)
    environment["UV_TOOL_DIR"] = str(tool_dir)
    environment["UV_TOOL_BIN_DIR"] = str(bin_dir)
    return environment


# Synthetic operator state seeded through the installed public APIs at the
# default paths (issue #186). The marker replaces one packaged blackout rule
# id and the ttl replaces the neutral default: both values are schema-valid,
# harmless and operator-shaped, which makes retention across the replacement
# and uninstall observable. No credentials, prompts or account data.
_POLICY_RULE_MARKER = "package-check-synthetic-blackout"
_SEED_SESSION_TTL_SECONDS = 7777
_SEED_TIMESTAMP = "2026-01-01T00:00:00+00:00"

SEED_STATE_CHECK = """
import json
import os

from scarcity_router.config import (
    default_selector_policy_path,
    default_selector_policy_source,
    load_selector_policy,
)
from scarcity_router.server_config import ServerConfiguration
from scarcity_router.server_store import (
    STORE_FILE_NAME,
    ServerStore,
    default_server_data_dir,
)

POLICY_MARKER = @POLICY_MARKER@
SESSION_TTL_SECONDS = @SESSION_TTL_SECONDS@

# One synthetic operator selector policy at the default config path: the
# audited default document with one harmless rule id replaced, validated
# with the installed loader. (HOME is synthetic, so this lands under the
# temporary home, never inside the tool environment.)
policy_path = default_selector_policy_path()
document = json.loads(default_selector_policy_source().read_text(encoding="utf-8"))
blackouts = document["resource_policy"]["blackouts"]
assert blackouts, "packaged default policy has no blackout rule"
blackouts[0]["rule_id"] = POLICY_MARKER
policy_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
policy_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
os.chmod(policy_path, 0o600)
assert load_selector_policy(policy_path) is not None

# One synthetic server-store configuration record at the default data dir
# through the public configuration APIs: the actual neutral configuration
# factory document with one harmless ttl value, schema-validated and
# round-tripped through the authoritative store.
data_dir = default_server_data_dir()
store = ServerStore.open(data_dir)
record = ServerConfiguration.neutral().to_document()
record["session_ttl_seconds"] = SESSION_TTL_SECONDS
ServerConfiguration.from_document(record)
store.save_configuration_document(record, at=@SEED_TIMESTAMP@)
stored = store.load_configuration_document()
assert stored == record, stored
store.close()
print(json.dumps({
    "policy_path": str(policy_path),
    "store_path": str(data_dir / STORE_FILE_NAME),
    "record": record,
}))
"""

RECORD_RETENTION_CHECK = """
import json
from pathlib import Path

from scarcity_router.server_config import ServerConfiguration
from scarcity_router.server_store import ServerStore

RECORD = json.loads(@RECORD@)
store = ServerStore.open(Path(@DATA_DIR@))
stored = store.load_configuration_document()
store.close()
assert stored == RECORD, (stored, RECORD)
ServerConfiguration.from_document(stored)
print("ok")
"""


@dataclass(frozen=True)
class _SeededState:
    """Synthetic operator state seeded through the installed APIs."""

    policy_path: Path
    policy_bytes: bytes
    store_path: Path
    record: dict[str, object]


def _expected_scripts() -> list[str]:
    return sorted(EXPECTED_ENTRY_POINTS["console_scripts"])


def _expect_entry_points(bin_dir: Path) -> None:
    scripts = sorted(entry.name for entry in bin_dir.iterdir())
    expected = _expected_scripts()
    _check(scripts == expected, f"installed scripts {scripts} != {expected}")


def _tool_python(tool_dir: Path) -> Path:
    tool_python = tool_dir / "scarcity-router" / "bin" / "python"
    _check(tool_python.is_file(), f"tool environment python missing at {tool_python}")
    return tool_python


def _installed_surface_smokes(bin_dir: Path, tool_python: Path, home: Path) -> None:
    check_cli_scripts(bin_dir, home)
    check_installed_resources(tool_python, home)
    check_mcp(bin_dir, tool_python, home)
    check_rest(bin_dir, home)


def _install_tool_wheel(
    env: Mapping[str, str], wheel: Path, *, bin_dir: Path, force: bool
) -> None:
    command = ["uv", "tool", "install", *(["--force"] if force else []), str(wheel)]
    result = _run(command, env=env)
    label = "same-version local-wheel replacement" if force else "fresh"
    _check(
        result.returncode == 0,
        f"isolated uv tool {label} install failed:\n{result.stderr}",
    )
    _expect_entry_points(bin_dir)
    print(f"PASS isolated temporary uv tool {label} install exposes exactly {_expected_scripts()}")


def _seed_user_state(home: Path, tool_python: Path) -> _SeededState:
    code = (
        SEED_STATE_CHECK.replace("@POLICY_MARKER@", repr(_POLICY_RULE_MARKER))
        .replace("@SESSION_TTL_SECONDS@", str(_SEED_SESSION_TTL_SECONDS))
        .replace("@SEED_TIMESTAMP@", repr(_SEED_TIMESTAMP))
    )
    result = _run([str(tool_python), "-I", "-c", code], cwd=home, env=clean_env(home))
    _check(
        result.returncode == 0,
        f"seeding synthetic user state through the installed APIs failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    _check(bool(lines), "seed state summary is empty")
    try:
        payload = cast(object, json.loads(lines[-1]))
    except json.JSONDecodeError as error:
        _fail(f"seed state summary is not JSON: {error}")
    _check(isinstance(payload, dict), "seed state summary is not a JSON object")
    summary = cast("dict[str, object]", payload)
    policy_raw = summary.get("policy_path")
    store_raw = summary.get("store_path")
    record = summary.get("record")
    _check(
        isinstance(policy_raw, str) and isinstance(store_raw, str),
        "seed state summary lacks the state paths",
    )
    _check(isinstance(record, dict), "seed state summary lacks the configuration record")
    policy_path = Path(cast(str, policy_raw))
    store_path = Path(cast(str, store_raw))
    _check(policy_path.is_file(), f"seeded policy file missing: {policy_path}")
    _check(store_path.is_file(), f"seeded server store missing: {store_path}")
    _check(
        policy_path.resolve().is_relative_to(home.resolve())
        and store_path.resolve().is_relative_to(home.resolve()),
        "seeded state escaped the temporary HOME",
    )
    policy_bytes = policy_path.read_bytes()
    _check(
        _POLICY_RULE_MARKER.encode("utf-8") in policy_bytes,
        "seeded policy lacks the synthetic rule marker",
    )
    return _SeededState(policy_path, policy_bytes, store_path, cast("dict[str, object]", record))


def _verify_retention_after_replacement(
    state: _SeededState, tool_python: Path, home: Path
) -> None:
    _check(
        state.policy_path.read_bytes() == state.policy_bytes,
        "the same-version replacement modified the seeded user selector policy",
    )
    code = RECORD_RETENTION_CHECK.replace(
        "@RECORD@", json.dumps(json.dumps(state.record))
    ).replace("@DATA_DIR@", repr(str(state.store_path.parent)))
    result = _run([str(tool_python), "-I", "-c", code], cwd=home, env=clean_env(home))
    _check(
        result.returncode == 0 and result.stdout.strip().endswith("ok"),
        f"the same-version replacement lost or reschema'd the server-store configuration record:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
    )
    print("PASS synthetic user policy and server-store record survive the same-version replacement")


def _uninstall_tool(env: Mapping[str, str], bin_dir: Path, tool_dir: Path) -> None:
    result = _run(["uv", "tool", "uninstall", "scarcity-router"], env=env)
    _check(result.returncode == 0, f"uv tool uninstall failed:\n{result.stderr}")
    expected = set(_expected_scripts())
    remaining = sorted(entry.name for entry in bin_dir.iterdir()) if bin_dir.is_dir() else []
    _check(
        not expected.intersection(remaining),
        f"console entry points survived uninstall: {sorted(expected.intersection(remaining))}",
    )
    _check(
        not (tool_dir / "scarcity-router").exists(),
        "the tool environment directory survived uninstall",
    )
    print("PASS uv tool uninstall removed the four entry points and the tool environment")


def _read_configuration_record_readonly(store_file: Path) -> object:
    """The stored configuration record via stdlib sqlite3, read-only."""
    connection = sqlite3.connect(f"file:{store_file}?mode=ro", uri=True)
    try:
        row = cast(
            "tuple[object, ...] | None",
            connection.execute(
                "SELECT document FROM server_configuration WHERE id = 1"
            ).fetchone(),
        )
    finally:
        connection.close()
    return None if row is None else cast(object, json.loads(cast(str, row[0])))


def _verify_state_retained_after_uninstall(state: _SeededState, store_bytes: bytes) -> None:
    _check(
        state.policy_path.is_file()
        and state.policy_path.read_bytes() == state.policy_bytes,
        f"tool uninstall purged or modified the user selector policy (uninstall removes the tool, never operator state)",
    )
    _check(
        state.store_path.is_file() and state.store_path.read_bytes() == store_bytes,
        "tool uninstall purged or modified the server store",
    )
    _check(
        _read_configuration_record_readonly(state.store_path) == state.record,
        "the server-store configuration record is missing after uninstall",
    )
    print("PASS user policy and server-store record retained after tool uninstall (no purge)")


def run_install_lifecycle(root: Path) -> None:
    """Fresh install → smokes → seed operator state → same-version local-wheel
    replacement → smokes + retention → uninstall → retained-state proof.

    The replacement reinstalls the exact same current local wheel; it is NOT
    an old-release→new-release upgrade proof (no release exists to upgrade
    from). Uninstall proves operator state is deliberately retained — the
    temporary tool is removed, the seeded state is never purged. Every REST
    and MCP smoke joins its process before returning, so no server survives
    into the uninstall step.
    """
    tool_dir = root / "tools"
    bin_dir = root / "bin"
    home = root / "home"
    _ = home.mkdir()
    tool_env = install_env(home, tool_dir, bin_dir)
    wheel = DIST / WHEEL_NAME

    _install_tool_wheel(tool_env, wheel, bin_dir=bin_dir, force=False)
    tool_python = _tool_python(tool_dir)
    _installed_surface_smokes(bin_dir, tool_python, home)

    seeded = _seed_user_state(home, tool_python)

    _install_tool_wheel(tool_env, wheel, bin_dir=bin_dir, force=True)
    tool_python = _tool_python(tool_dir)
    _installed_surface_smokes(bin_dir, tool_python, home)
    _verify_retention_after_replacement(seeded, tool_python, home)

    store_bytes = seeded.store_path.read_bytes()
    _uninstall_tool(tool_env, bin_dir, tool_dir)
    _verify_state_retained_after_uninstall(seeded, store_bytes)


def isolated_install_checks() -> None:
    with tempfile.TemporaryDirectory(prefix="scarcity-router-package-check-") as temporary:
        run_install_lifecycle(Path(temporary))
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
