"""Secure Z.ai Coding Plan quota acquisition.

Production acquisition boundary between the existing Kilo credential and the
pure ``parse_zai_quota_response`` parser:

    existing Kilo credential
      -> strict credential discovery (exact provider, evidenced shape)
      -> fixed HTTPS Z.ai quota GET (validated destination, no redirects)
      -> bounded strict JSON decoding
      -> existing parse_zai_quota_response(...)
      -> CapacitySnapshot

Security contract (docs/security.md):

- the credential is discovered only from the ``zai-coding-plan`` entry of the
  Kilo auth file (default ``~/.local/share/kilo/auth.json``) with the
  evidenced shape ``type == "api"`` and a non-empty string ``key``; the value
  is sent as-is, never stripped, prefixed or reformatted;
- the destination is validated against the fixed endpoint policy (see
  ``is_approved_destination``) before the Authorization header is attached;
  arbitrary endpoint URLs are not exposed;
- exactly one request, one credential use, no retry, no Bearer fallback, no
  redirect following (every 3xx is rejected without a second request);
- a finite timeout (``TIMEOUT_SECONDS``) and a bounded response body
  (``MAX_BODY_BYTES``); the auth file read is bounded by
  ``MAX_AUTH_FILE_BYTES``; oversized bodies are never parsed or persisted;
- the credential never enters exceptions, diagnostics, logs or the returned
  snapshot, and this module emits no stdout/stderr output.

Expected operational conditions normalize to safe v1 snapshots
(docs/capacity-model.md): unusable credential source and HTTP 401 map to
``auth_required``; a received success response whose body cannot satisfy the
validated contract maps to ``schema_changed``; connection/DNS/timeout failure
maps to ``unavailable``; redirect rejection and other unevidenced HTTP
failures map to ``unknown``. Programmer errors (for example an edited
endpoint constant failing destination policy) raise ``RuntimeError`` instead
of being disguised as telemetry failures.

The quota-window semantics remain entirely in the pure parser; this module
adds zero provider semantics of its own. The reconnaissance helper
``tools/m1_zai_quota_recon.py`` is historical evidence tooling and is not
imported here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from http.client import HTTPException, HTTPMessage
from pathlib import Path
from typing import IO, cast, override

from ..capacity import CapacityDiagnostic, CapacitySnapshot
from .zai import PROVIDER, SOURCE, parse_zai_quota_response

# Evidenced default Kilo auth location (docs/poc-evidence.md).
DEFAULT_KILO_AUTH_FILE = Path.home() / ".local" / "share" / "kilo" / "auth.json"

# Fixed production endpoint; no override is exposed by design.
ENDPOINT = "https://api.z.ai/api/monitor/usage/quota/limit"
_ENDPOINT_SCHEME = "https"
_ENDPOINT_HOST = "api.z.ai"
_ENDPOINT_PATH = "/api/monitor/usage/quota/limit"

# One attempt, finite wait, bounded reads. The auth-file bound is generous
# for a provider-key JSON document; anything larger fails closed.
TIMEOUT_SECONDS = 15.0
MAX_BODY_BYTES = 64 * 1024
MAX_AUTH_FILE_BYTES = 1024 * 1024

# Exact target provider identity; no other entry is ever read.
_PROVIDER_ENTRY_ID = "zai-coding-plan"
_CREDENTIAL_TYPE = "api"
_CREDENTIAL_FIELD = "key"


class _DestinationPolicyError(RuntimeError):
    """Refusal to attach Authorization to an unapproved destination.

    Raised only when the fixed endpoint constant itself fails policy, which
    is a programmer error, not a provider telemetry condition; it is
    deliberately not normalized into a snapshot. The message never contains
    the URL or any credential material.
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Decline every redirect so no second request can ever be issued."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def is_approved_destination(url: str) -> bool:
    """Exact-match the fixed provider endpoint policy.

    Rejects at minimum: non-HTTPS schemes, any host other than the exact
    provider host (including suffix tricks such as ``api.z.ai.example.com``),
    userinfo, explicit unexpected ports, any query or fragment, and any path
    other than the exact endpoint path. Malformed URLs fail closed.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != _ENDPOINT_SCHEME:
            return False
        if parts.hostname != _ENDPOINT_HOST:
            return False
        if parts.path != _ENDPOINT_PATH:
            return False
        if parts.query or parts.fragment:
            return False
        if parts.username is not None or parts.password is not None:
            return False
        port = parts.port
    except ValueError:
        return False
    return port in (None, 443)


def _ensure_approved_destination(url: str) -> None:
    """Enforce endpoint policy before any Authorization header is attached."""
    if not is_approved_destination(url):
        raise _DestinationPolicyError(
            "refusing to attach Authorization: destination does not satisfy the fixed provider endpoint policy"
        )


def _build_authenticated_request(
    destination: str,
    credential: str,
) -> urllib.request.Request:
    """Build the single GET request; attach the credential only after
    destination validation succeeds. The credential value is used exactly as
    stored (no ``Bearer`` prefix, no mutation)."""
    request = urllib.request.Request(destination, method="GET")
    _ensure_approved_destination(request.full_url)
    request.add_header("Authorization", credential)
    request.add_header("Accept", "application/json")
    return request


def _open_response(
    request: urllib.request.Request,
    timeout: float,
) -> urllib.response.addinfourl:
    """Perform the one network interaction through a no-redirect opener.

    Test seam: tests replace this function with a fake transport; no test
    contacts the network.
    """
    opener = urllib.request.build_opener(_NoRedirect())
    return cast(
        "urllib.response.addinfourl", opener.open(request, timeout=timeout)
    )


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Narrow a decoded boundary value to a ``str``-keyed dict, or ``None``."""
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def _read_stored_credential(auth_file: Path) -> str | None:
    """Strictly discover the target provider credential, or ``None``.

    Reads the Kilo auth file with a hard size bound, decodes strictly as
    UTF-8 and JSON, and selects only the exact ``zai-coding-plan`` entry. The
    ``key`` value is inspected only after that entry is established, and
    unrelated provider entries are never read as candidates. The evidenced
    shape is ``type == "api"`` with a non-empty string ``key``; any other
    shape or any I/O/decoding failure fails safely to ``None``. The supported
    layout is a JSON object keyed by provider ID, so duplicate target entries
    cannot occur (duplicate JSON keys collapse deterministically); any other
    layout fails closed instead of guessing.
    """
    try:
        with auth_file.open("rb") as handle:
            raw = handle.read(MAX_AUTH_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_AUTH_FILE_BYTES:
        return None
    try:
        document = _as_object_dict(
            cast("object", json.loads(raw.decode("utf-8")))
        )
    except ValueError:
        # Covers strict-UTF-8 UnicodeDecodeError and JSONDecodeError.
        return None
    if document is None:
        return None
    entry = _as_object_dict(document.get(_PROVIDER_ENTRY_ID))
    if entry is None:
        return None
    if entry.get("type") != _CREDENTIAL_TYPE:
        return None
    key = entry.get(_CREDENTIAL_FIELD)
    if not isinstance(key, str) or not key:
        return None
    return key


def _snapshot(
    status: str,
    diagnostic_code: str,
    retrieved_at: str,
) -> CapacitySnapshot:
    """Safe failure snapshot; carries only allowlisted safe identifiers."""
    return CapacitySnapshot(
        schema_version=1,
        provider=PROVIDER,
        source=SOURCE,
        retrieved_at=retrieved_at,
        status=status,
        windows=(),
        diagnostics=(CapacityDiagnostic(code=diagnostic_code),),
        plan=None,
    )


def collect_zai_capacity(
    *,
    retrieved_at: str,
    auth_file: Path | None = None,
) -> CapacitySnapshot:
    """Acquire one Z.ai Coding Plan capacity snapshot.

    ``retrieved_at`` stays caller-supplied; this function introduces no
    clock or freshness policy (U-003). ``auth_file`` defaults to the
    evidenced Kilo location and exists for deterministic tests and
    controlled local configuration. The credential path and value never
    enter the returned snapshot; expected operational conditions normalize
    to safe failure snapshots instead of leaking transport exceptions. An
    invalid ``retrieved_at`` keeps failing through the typed
    capacity-contract validation rather than being misreported as provider
    telemetry.
    """
    path = DEFAULT_KILO_AUTH_FILE if auth_file is None else auth_file
    credential = _read_stored_credential(path)
    if credential is None:
        return _snapshot("auth_required", "auth_required", retrieved_at)

    request = _build_authenticated_request(ENDPOINT, credential)
    try:
        response = _open_response(request, TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        # Inspected only via ``code``; the error body and message are never
        # read into output. 3xx arrives here because redirects are declined.
        if exc.code == 401:
            return _snapshot("auth_required", "auth_required", retrieved_at)
        return _snapshot("unknown", "telemetry_unknown", retrieved_at)
    except OSError:
        # URLError, ConnectionError and TimeoutError (socket.timeout) all
        # derive from OSError: the remote source could not be obtained.
        return _snapshot("unavailable", "source_unavailable", retrieved_at)

    try:
        with response:
            body = response.read(MAX_BODY_BYTES + 1)
    except (OSError, HTTPException):
        # The connection failed mid-body; the partial content is discarded,
        # never parsed or persisted.
        return _snapshot("unavailable", "source_unavailable", retrieved_at)
    if len(body) > MAX_BODY_BYTES:
        return _snapshot("unknown", "telemetry_unknown", retrieved_at)

    try:
        payload = cast("object", json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # A response was received but cannot satisfy the validated response
        # contract; never parse or persist the raw text.
        return _snapshot("schema_changed", "schema_changed", retrieved_at)

    return parse_zai_quota_response(payload, retrieved_at=retrieved_at)
