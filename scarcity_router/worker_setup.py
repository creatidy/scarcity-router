"""Worker first-run setup core: local settings, pairing, launch routing.

Issue #113 / D-051: the packaged Windows worker must have a usable path
from a double-click to a paired, running worker. This module is the
GUI-independent core of that experience: the typed local settings
document, its strict (fail-closed) persistence over the existing
``WorkerLocalStore``, the pairing orchestration shared by the dialog and
the packaged CLI, the deterministic error mapping, and the launch
routing (first-run setup vs. tray). Everything here imports and runs on
every platform; the only pixels — the tkinter dialog and the tray's
"Worker settings…" item — live in the packaging tree, exactly like the
pystray tray-view seam.

Deliberate boundaries:

- **One pairing system.** The dialog and the packaged CLI both call
  :func:`pair_worker`, a thin orchestration over the existing
  ``WorkerRuntime.pair`` (protocol handshake + ``WorkerLocalStore``
  persistence). There is no second pairing implementation; the Python
  console script keeps driving ``WorkerRuntime.pair`` directly.
- **The allowlist stays local (D-044).** Local settings can only ever
  describe a loopback Ollama endpoint; the non-loopback refusal happens
  here at construction, and ``LoopbackOllamaAdapter`` re-validates at
  its own construction — no UI path to a remote endpoint exists. The
  server can never expand this allowlist: registration is a worker-host
  configuration action, never a protocol operation.
- **Non-secrets only.** The settings document holds the optional server
  control-UI URL and the optional Ollama resource/host/port — never the
  pairing code, never the worker credential (which lives only in
  ``WorkerLocalStore``'s identity row), never provider or admin
  credentials. Parsing is strict (exact key set, exact types, unknown
  keys fail closed), so nothing can be smuggled into the document.
- **Deterministic partial failure.** All input validation happens
  BEFORE pairing; a failed pairing persists no identity (the runtime
  saves identity only after server acceptance); a settings-save failure
  after successful pairing keeps the identity and is recoverable
  through the settings dialog — never a second pairing code.
- **Precedence.** Explicit CLI ``run`` flags that select an adapter
  replace the stored selection for that process; with no ``--allow-*``
  flag the stored settings drive the adapter registry. The origin
  override order for the control UI is: explicit ``--server-ui-url``
  flag, stored setting, documented derivation (https://HOST:8787).
"""

from __future__ import annotations

import json
import sqlite3
import ssl
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from .gateway_validation import v_safe_id, v_text
from .worker_client import (
    ConnectFactory,
    WorkerConfigError,
    WorkerOrigin,
    WorkerRuntime,
    WorkerRuntimeError,
    default_connect_factory,
)
from .worker_local_store import WorkerLocalIdentity, WorkerLocalStore
from .worker_protocol import WorkerProtocolError

#: The local settings document's schema version. A mismatch is a loud
#: typed failure, never a guess (the same discipline as the identity
#: store's schema version).
WORKER_LOCAL_SETTINGS_SCHEMA_VERSION = 1

#: The ``WorkerLocalStore`` value key holding the settings JSON document.
SETTINGS_STORE_KEY = "local_settings"

#: Serialized-size ceiling for the settings document. The document is
#: three small fields; anything larger is by definition malformed.
MAX_SETTINGS_JSON_CHARS = 8192

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_SETTINGS_KEYS: frozenset[str] = frozenset({"schema_version", "server_ui_url", "ollama"})
_OLLAMA_KEYS: frozenset[str] = frozenset({"resource_id", "host", "port"})


class WorkerSetupConfigError(WorkerConfigError):
    """A first-run/local-settings problem (safe message, no secrets)."""


# ── Typed local settings ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class OllamaSettings:
    """The loopback Ollama endpoint this worker allowlists locally.

    ``host`` is restricted to the loopback set by construction: the
    setup UI can never turn the Ollama adapter into a network proxy
    (D-044). ``resource_id`` is the registry resource id this adapter
    serves (the administrator binds a ``worker_bridged`` resource with
    the matching id on the server).
    """

    resource_id: str
    host: str = "127.0.0.1"
    port: int = 11434

    def __post_init__(self) -> None:
        try:
            _ = v_safe_id(self.resource_id, "resource id")
        except ValueError as exc:
            raise WorkerSetupConfigError(str(exc)) from None
        if self.host not in _LOOPBACK_HOSTS:
            raise WorkerSetupConfigError(
                "the Ollama host must be a loopback address (127.0.0.1, "
                + "localhost or ::1); local adapters reach localhost-only "
                + "endpoints only"
            )
        if isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise WorkerSetupConfigError("the Ollama port is out of range")


@dataclass(frozen=True)
class WorkerLocalSettings:
    """The worker's non-secret local configuration (typed, versioned).

    ``server_ui_url`` names the server's web-UI origin so the tray's
    "Open server control UI" action and the first-run dialog's shortcut
    reach the right page on non-default ports. ``ollama`` is absent for
    a pair-only worker (a valid, supported state: the tray connects and
    reports the worker; a local adapter can be enabled later through
    the settings dialog).
    """

    server_ui_url: str | None = None
    ollama: OllamaSettings | None = None


def settings_to_json(settings: WorkerLocalSettings) -> str:
    """The bounded canonical JSON document for one settings value."""
    document: dict[str, object] = {
        "schema_version": WORKER_LOCAL_SETTINGS_SCHEMA_VERSION,
        "server_ui_url": settings.server_ui_url,
        "ollama": (
            None
            if settings.ollama is None
            else {
                "resource_id": settings.ollama.resource_id,
                "host": settings.ollama.host,
                "port": settings.ollama.port,
            }
        ),
    }
    text = json.dumps(document, sort_keys=True)
    if len(text) > MAX_SETTINGS_JSON_CHARS:
        # Unreachable for validated settings; the bound exists so a future
        # field can never silently turn this value into an unbounded blob.
        raise WorkerSetupConfigError("the local settings document is too large")
    return text


def settings_from_json(text: str) -> WorkerLocalSettings:
    """Parse one stored settings document (strict; fails closed).

    Unknown keys, wrong types, a wrong schema version or malformed JSON
    raise :class:`WorkerSetupConfigError` — a corrupted settings value
    is a loud, recoverable failure, never a partial guess.
    """
    try:
        parsed = cast("object", json.loads(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise WorkerSetupConfigError(
            "the saved local settings are not valid JSON; open Worker "
            + "settings and save again to replace them"
        ) from exc
    if not isinstance(parsed, dict):
        raise WorkerSetupConfigError(
            "the saved local settings are malformed; open Worker settings "
            + "and save again to replace them"
        )
    document = cast("dict[str, object]", parsed)
    if frozenset(document) != _SETTINGS_KEYS:
        raise WorkerSetupConfigError(
            "the saved local settings have unexpected fields; open Worker "
            + "settings and save again to replace them"
        )
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise WorkerSetupConfigError("the saved local settings have a malformed schema version")
    if version != WORKER_LOCAL_SETTINGS_SCHEMA_VERSION:
        raise WorkerSetupConfigError(
            f"the saved local settings use schema version {version}, not "
            + f"{WORKER_LOCAL_SETTINGS_SCHEMA_VERSION}; an explicit "
            + "migration is required"
        )
    ui_url = document["server_ui_url"]
    if ui_url is not None and not isinstance(ui_url, str):
        raise WorkerSetupConfigError("the saved server UI URL is malformed")
    ollama_value = document["ollama"]
    ollama: OllamaSettings | None = None
    if ollama_value is not None:
        if not isinstance(ollama_value, dict):
            raise WorkerSetupConfigError("the saved Ollama settings are malformed")
        ollama_document = cast("dict[str, object]", ollama_value)
        if frozenset(ollama_document) != _OLLAMA_KEYS:
            raise WorkerSetupConfigError("the saved Ollama settings have unexpected fields")
        resource_id = ollama_document["resource_id"]
        host = ollama_document["host"]
        port = ollama_document["port"]
        if not isinstance(resource_id, str) or not isinstance(host, str):
            raise WorkerSetupConfigError("the saved Ollama settings are malformed")
        if isinstance(port, bool) or not isinstance(port, int):
            raise WorkerSetupConfigError("the saved Ollama port is malformed")
        ollama = OllamaSettings(resource_id=resource_id, host=host, port=port)
    server_ui_url = normalize_control_ui_url(ui_url) if ui_url is not None else None
    return WorkerLocalSettings(server_ui_url=server_ui_url, ollama=ollama)


def load_worker_settings(store: WorkerLocalStore) -> WorkerLocalSettings | None:
    """The stored settings, or ``None`` when none were saved yet.

    Raises :class:`WorkerSetupConfigError` (fail closed) when the stored
    document is malformed — callers surface that as a recoverable
    settings state, never as a guessed configuration.
    """
    raw = store.load_value(SETTINGS_STORE_KEY)
    if raw is None:
        return None
    return settings_from_json(raw)


def save_worker_settings(store: WorkerLocalStore, settings: WorkerLocalSettings) -> None:
    """Persist the settings atomically (one SQLite row replacement)."""
    try:
        _ = store.save_value(SETTINGS_STORE_KEY, settings_to_json(settings))
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise WorkerSetupConfigError(
            "the local settings could not be saved; the worker state "
            + "directory must be writable"
        ) from exc


# ── Control-UI origin ─────────────────────────────────────────────────────────


def normalize_control_ui_url(text: str) -> str:
    """Validate and normalize a control-UI origin (``scheme://host[:port]``).

    Credentials, query strings, fragments and paths are refused: this is
    an origin for the "Open server control UI" shortcut, not a carried
    secret and not a redirect surface.
    """
    _ = v_text(text, "server control UI URL", max_len=512)
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WorkerSetupConfigError(
            "the server control UI must be an http(s) URL, e.g. https://SERVER:8787"
        )
    if parts.username is not None or parts.password is not None:
        raise WorkerSetupConfigError("the control UI URL must not contain credentials")
    if parts.query or parts.fragment:
        raise WorkerSetupConfigError("the control UI URL must be a bare origin")
    try:
        port = parts.port
    except ValueError as exc:
        raise WorkerSetupConfigError("the control UI URL has an invalid port") from exc
    if parts.path not in ("", "/"):
        raise WorkerSetupConfigError("the control UI URL must be a bare origin")
    host = parts.hostname
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme.lower()}://{netloc}"


def control_ui_origin(origin: WorkerOrigin, override: str | None = None) -> str:
    """The server web-UI origin for one worker origin (documented default).

    The worker protocol does not carry the server's HTTP origin (it has
    its own port), so without an explicit override the DOCUMENTED
    derivation applies: ``https://HOST:8787`` (or ``http://`` for a
    loopback plaintext origin). Deployments on custom ports pass the
    override (the ``--server-ui-url`` flag or the stored setting).
    """
    if override:
        return normalize_control_ui_url(override)
    return f"{'https' if origin.tls else 'http'}://{origin.host}:8787"


# ── Setup fields, pairing orchestration, deterministic errors ─────────────────


@dataclass(frozen=True)
class SetupFields:
    """Raw setup-dialog input (strings; validated here, never in the view).

    Keeping every field a string makes the view dumb pixels and puts all
    parsing/validation in this module where it is unit-tested everywhere.
    """

    server_url: str = ""
    pairing_code: str = ""
    server_ui_url: str = ""
    ollama_enabled: bool = False
    resource_id: str = ""
    ollama_host: str = "127.0.0.1"
    ollama_port: str = "11434"


@dataclass(frozen=True)
class SetupOutcome:
    """One first-run submission's outcome (safe, displayable text only).

    ``settings_saved is False`` with ``ok is True`` is the recoverable
    partial-failure state: the identity is stored, the worker will run,
    and the settings can be completed later without a second code.
    """

    ok: bool
    message: str = ""
    worker_id: str | None = None
    settings_saved: bool = False


def _parse_ollama_port(text: str) -> int:
    stripped = text.strip()
    try:
        port = int(stripped)
    except ValueError as exc:
        raise WorkerSetupConfigError(
            "the Ollama port must be a number between 1 and 65535"
        ) from exc
    if not 1 <= port <= 65535:
        raise WorkerSetupConfigError("the Ollama port is out of range")
    return port


def settings_from_fields(fields: SetupFields) -> WorkerLocalSettings:
    """Validate setup fields into settings (no side effects).

    Raises :class:`WorkerSetupConfigError` with an actionable message on
    any invalid input — including a non-loopback Ollama host, which this
    function refuses BEFORE anything is paired or persisted.
    """
    ui_text = fields.server_ui_url.strip()
    server_ui_url = normalize_control_ui_url(ui_text) if ui_text else None
    ollama: OllamaSettings | None = None
    if fields.ollama_enabled:
        ollama = OllamaSettings(
            resource_id=fields.resource_id.strip(),
            host=fields.ollama_host.strip() or "127.0.0.1",
            port=_parse_ollama_port(fields.ollama_port),
        )
    return WorkerLocalSettings(server_ui_url=server_ui_url, ollama=ollama)


def pair_worker(
    origin: WorkerOrigin,
    pairing_code: str,
    *,
    store: WorkerLocalStore,
    device_label: str | None = None,
    connect_factory: ConnectFactory | None = None,
) -> WorkerLocalIdentity:
    """Redeem one pairing code through the EXISTING runtime pairing path.

    A thin orchestration over ``WorkerRuntime.pair`` — the protocol
    handshake, server acceptance and atomic identity persistence are the
    runtime's, not duplicated here. Nothing is persisted unless the
    server accepts the code.
    """
    runtime = WorkerRuntime(
        origin=origin,
        store=store,
        device_label=device_label,
        connect_factory=(
            connect_factory if connect_factory is not None else default_connect_factory
        ),
    )
    return runtime.pair(pairing_code)


def describe_pairing_failure(exc: Exception) -> str:
    """One safe, actionable user-facing line for a pairing failure.

    Never a traceback, never credential material. Server rejection codes
    map to remediation-bearing text; transport failures name the check
    the user should make; TLS failures preserve verification discipline
    (D-044) instead of offering a bypass.
    """
    if isinstance(exc, ssl.SSLError):
        return (
            "the server's TLS certificate could not be verified. Install "
            + "the server's certificate authority into the Windows trust "
            + "store or fix the server certificate — the worker never "
            + "skips certificate verification."
        )
    if isinstance(exc, WorkerRuntimeError):
        text = str(exc)
        if "pairing_code_invalid" in text:
            return (
                "the pairing code was not accepted (invalid). Issue a "
                + "fresh one-time code in the server web UI (Workers page) "
                + "and try again."
            )
        if "pairing_code_expired" in text:
            return (
                "the pairing code has expired. Issue a fresh one-time "
                + "code in the server web UI (Workers page) and try again."
            )
        if "pairing_code_used" in text:
            return (
                "the pairing code was already used. A code works exactly "
                + "once — issue a fresh one-time code in the server web "
                + "UI (Workers page) and try again."
            )
        if "closed the connection during pairing" in text:
            return (
                "the server closed the connection during pairing. Check "
                + "that the server is running and that the origin and "
                + "port are correct."
            )
        return text
    if isinstance(exc, WorkerConfigError):
        return str(exc)
    if isinstance(exc, WorkerProtocolError):
        return "the server answered the pairing request with a malformed message."
    if isinstance(exc, TimeoutError):
        return (
            "the server did not answer in time. Check the origin, the "
            + "network, and that the server is running."
        )
    if isinstance(exc, OSError):
        return (
            f"could not reach the server ({type(exc).__name__}). Check the "
            + "origin, the network, and that the server is running."
        )
    return f"pairing failed ({type(exc).__name__})."


def _redact(message: str, pairing_code: str) -> str:
    """Defensive: no failure text ever echoes the pairing code."""
    if pairing_code and pairing_code in message:
        return message.replace(pairing_code, "<redacted>")
    return message


def complete_setup(
    fields: SetupFields,
    *,
    store: WorkerLocalStore,
    connect_factory: ConnectFactory | None = None,
) -> SetupOutcome:
    """One first-run submission: validate → pair → persist settings.

    Deterministic partial-failure semantics:

    - invalid input or a failed pairing persists NOTHING (the dialog
      stays open with the actionable message);
    - a successful pairing followed by a settings-save failure keeps
      the stored identity and reports ``settings_saved=False`` — the
      next launch enters a recoverable settings state, never a second
      pairing code.
    """
    server_url = fields.server_url.strip()
    pairing_code = fields.pairing_code.strip()
    if not server_url:
        return SetupOutcome(
            ok=False, message="enter the worker server origin, e.g. srws://SERVER:8790"
        )
    if not pairing_code:
        return SetupOutcome(
            ok=False,
            message=(
                "enter the one-time pairing code from the server web UI "
                + "(Workers page)"
            ),
        )
    if len(pairing_code) > 512:
        return SetupOutcome(
            ok=False,
            message=(
                "the pairing code looks wrong; paste the one-time code "
                + "exactly as issued"
            ),
        )
    try:
        origin = WorkerOrigin.parse(server_url)
        settings = settings_from_fields(fields)
    except WorkerConfigError as exc:
        return SetupOutcome(ok=False, message=_redact(str(exc), pairing_code))
    try:
        identity = pair_worker(
            origin,
            pairing_code,
            store=store,
            connect_factory=connect_factory,
        )
    except (
        WorkerConfigError,
        WorkerRuntimeError,
        WorkerProtocolError,
        ValueError,
        OSError,
    ) as exc:
        message = describe_pairing_failure(exc)
        return SetupOutcome(ok=False, message=_redact(message, pairing_code))
    settings_saved = True
    try:
        save_worker_settings(store, settings)
    except WorkerConfigError:
        settings_saved = False
    if settings_saved:
        message = f"paired as {identity.worker_id}"
    else:
        message = (
            f"paired as {identity.worker_id}, but saving the local "
            + "settings failed; complete them later via the tray's "
            + "Worker settings action"
        )
    return SetupOutcome(
        ok=True,
        worker_id=identity.worker_id,
        settings_saved=settings_saved,
        message=message,
    )


# ── Settings-dialog save (paired mode) ────────────────────────────────────────


def save_settings_fields(fields: SetupFields, *, store: WorkerLocalStore) -> WorkerLocalSettings:
    """Validate settings-mode fields and persist them (paired worker).

    This is the settings dialog's whole write path — the same typed
    validation as first-run, no second configuration implementation.
    Raises :class:`WorkerSetupConfigError` with an actionable message on
    invalid input or a failed save (the dialog stays open).
    """
    settings = settings_from_fields(fields)
    save_worker_settings(store, settings)
    return settings


# ── Launch routing and argument precedence ────────────────────────────────────

FIRST_RUN = "first_run"
TRAY = "tray"


def resolve_launch(*, paired: bool) -> str:
    """The no-arg launch action for one identity state (pure routing).

    Unpaired workers enter first-run setup; paired workers start the
    tray — including pair-only workers with no local resource configured
    yet (the tray connects and reports them; settings can be completed
    later through the tray).
    """
    return TRAY if paired else FIRST_RUN


def effective_arguments(
    cli: Mapping[str, object],
    settings: WorkerLocalSettings | None,
) -> dict[str, object]:
    """Merge explicit CLI run flags with the stored settings (documented
    precedence).

    An explicit CLI adapter selection (``--allow-ollama`` or
    ``--allow-codex``) replaces the stored selection for that process —
    the operator chose the command line. With no ``--allow-*`` flag the
    stored settings drive the registry (enabling Ollama through the
    settings dialog is sufficient; no flag is needed).
    """
    merged: dict[str, object] = dict(cli)
    selects_adapter = bool(cli.get("allow_ollama")) or bool(cli.get("allow_codex"))
    if not selects_adapter and settings is not None and settings.ollama is not None:
        merged["allow_ollama"] = True
        merged["resource"] = settings.ollama.resource_id
        merged["ollama_host"] = settings.ollama.host
        merged["ollama_port"] = settings.ollama.port
    return merged


# ── The view seam (implemented by the packaging tree's tkinter dialog) ────────


@dataclass(frozen=True)
class SettingsDialogContext:
    """What the settings dialog shows about the paired worker."""

    worker_id: str
    server_origin: str
    current: WorkerLocalSettings | None
    #: Non-None when the stored settings are malformed (recoverable):
    #: the dialog shows this banner and pre-fills defaults; saving
    #: replaces the malformed document.
    problem: str | None = None


class SetupView(Protocol):
    """The first-run/settings dialog seam (tkinter adapter in production).

    The view owns its event loop and retries: it calls ``submit`` (or
    ``save``) once per attempt — possibly from a worker thread — shows
    the returned failure message, and only returns on success or user
    cancel (``None``). All orchestration policy lives in the library;
    the view is replaceable pixels.
    """

    def run_first_run(
        self, submit: Callable[[SetupFields], SetupOutcome]
    ) -> SetupOutcome | None: ...

    def run_settings(
        self,
        context: SettingsDialogContext,
        save: Callable[[SetupFields], WorkerLocalSettings],
    ) -> WorkerLocalSettings | None: ...


__all__ = [
    "FIRST_RUN",
    "MAX_SETTINGS_JSON_CHARS",
    "SETTINGS_STORE_KEY",
    "TRAY",
    "WORKER_LOCAL_SETTINGS_SCHEMA_VERSION",
    "OllamaSettings",
    "SettingsDialogContext",
    "SetupFields",
    "SetupOutcome",
    "SetupView",
    "WorkerLocalSettings",
    "WorkerSetupConfigError",
    "complete_setup",
    "control_ui_origin",
    "describe_pairing_failure",
    "effective_arguments",
    "load_worker_settings",
    "normalize_control_ui_url",
    "pair_worker",
    "resolve_launch",
    "save_settings_fields",
    "save_worker_settings",
    "settings_from_fields",
    "settings_from_json",
    "settings_to_json",
]
