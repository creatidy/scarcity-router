"""Default user-configuration discovery and provisioning (D-036).

The service has exactly one default configuration location:
``$(XDG_CONFIG_HOME or ~/.config)/scarcity-router/``. Today it holds one
optional file, ``selector-policy.json`` — the user selector policy applied
by every surface (CLI, REST, MCP) when the caller does not supply a more
specific one. The file is provisioned from the checked-in owner example
``examples/selector-policy.json`` (packaged as a wheel resource for
installed distributions) and is never silently overwritten.

This module is the only product code that writes to the user's file system
outside its own package, and it writes exactly one artifact: the selector
policy file in the configuration directory. The content is the audited
repository example — it contains no credentials, no provider endpoints and
no machine-specific data; the directory is created ``0o700`` and the file
``0o600``. Provisioning failures never block a selection: the caller
degrades to the documented neutral policy instead.

The environment mapping is injectable so tests stay deterministic and
independent of the host configuration.
"""

from __future__ import annotations

import importlib.resources
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from .errors import SelectionContractError
from .selection_app import REPO_ROOT, load_selector_policy
from .selector import SelectorPolicy

CONFIG_DIR_NAME = "scarcity-router"
SELECTOR_POLICY_FILE_NAME = "selector-policy.json"
PACKAGED_DEFAULT_RESOURCE_NAME = "default-selector-policy.json"


def user_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """The default configuration directory (XDG ``config home`` based).

    ``XDG_CONFIG_HOME`` is honored when set to an absolute path; empty or
    relative values are ignored per the XDG specification. Otherwise the
    conventional ``~/.config`` home is used (``HOME`` from the mapping,
    falling back to the process home).
    """
    environment = os.environ if env is None else env
    raw = environment.get("XDG_CONFIG_HOME", "")
    if raw and os.path.isabs(raw):
        base = Path(raw)
    else:
        home = environment.get("HOME", "")
        base = (Path(home).expanduser() if home else Path.home()) / ".config"
    return base / CONFIG_DIR_NAME


def default_selector_policy_path(env: Mapping[str, str] | None = None) -> Path:
    """The default user selector-policy file inside the config directory."""
    return user_config_dir(env) / SELECTOR_POLICY_FILE_NAME


def default_selector_policy_source() -> Path:
    """The audited default policy content: source-tree example or packaged.

    A source tree uses the single committed
    ``examples/selector-policy.json``; an installed distribution uses the
    wheel resource copy mapped at build time (no committed duplicate).
    """
    source_copy = REPO_ROOT / "examples" / SELECTOR_POLICY_FILE_NAME
    if source_copy.is_file():
        return source_copy
    with importlib.resources.as_file(
        importlib.resources.files("scarcity_router").joinpath(
            PACKAGED_DEFAULT_RESOURCE_NAME
        )
    ) as resource_path:
        return resource_path


def ensure_default_user_config(
    *, force: bool = False, env: Mapping[str, str] | None = None
) -> tuple[Path, bool]:
    """Provision the default selector-policy file when it does not exist.

    Idempotent: an existing file is kept untouched unless ``force`` replaces
    it with the audited defaults (never a silent overwrite in normal runs).
    Returns the policy path and whether this call wrote the file.
    """
    path = default_selector_policy_path(env)
    if path.is_file() and not force:
        return path, False
    text = default_selector_policy_source().read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # O_EXCL for a fresh provision — a race with a concurrent provisioner
    # must not truncate the winner — and O_TRUNC only for an explicit force
    # replace of an existing file.
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write(text)
    except BaseException:
        # Never leave a partially written default policy behind.
        path.unlink(missing_ok=True)
        raise
    return path, True


def load_default_selector_policy(
    env: Mapping[str, str] | None = None,
) -> SelectorPolicy | None:
    """Load the default user policy, or ``None`` when no file exists.

    A file that exists but fails to load or validate raises: a broken user
    policy is a configuration failure the caller must see, never a silent
    fall-back to the neutral policy.
    """
    path = default_selector_policy_path(env)
    if not path.is_file():
        return None
    return load_selector_policy(path)


def _warn_stderr(message: str) -> None:
    print(message, file=sys.stderr)


def resolve_default_selector_policy(
    *,
    env: Mapping[str, str] | None = None,
    warn: Callable[[str], None] = _warn_stderr,
    note: Callable[[str], None] = _warn_stderr,
) -> SelectorPolicy | None:
    """Provision (best effort) and load the default user policy.

    Convenience for the process entry points (CLI, MCP, REST): provisioning
    and loading failures print one concise warning through ``warn`` (stderr
    by default) and yield ``None`` — the documented neutral policy — so a
    read-only or broken configuration directory never blocks selection.
    The run that first provisions the file reports it once through ``note``
    (stderr by default); an existing file is never announced.
    """
    try:
        path, wrote = ensure_default_user_config(env=env)
        if wrote:
            note(f"note: provisioned default user config: {path}")
        return load_default_selector_policy(env=env)
    except (OSError, ValueError, SelectionContractError) as exc:
        warn(f"warning: default selector policy unavailable: {exc}")
        return None


__all__ = [
    "CONFIG_DIR_NAME",
    "PACKAGED_DEFAULT_RESOURCE_NAME",
    "SELECTOR_POLICY_FILE_NAME",
    "default_selector_policy_path",
    "default_selector_policy_source",
    "ensure_default_user_config",
    "load_default_selector_policy",
    "resolve_default_selector_policy",
    "user_config_dir",
]
