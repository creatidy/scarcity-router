"""Worker-local private CA trust: file validation, record, TLS context.

Issue #224 scope 2 (D-083 successor of the excluded #139): a worker whose
server certificate is issued by a PRIVATE certificate authority may trust
exactly one explicit CA file IN ADDITION to the platform roots. This
module is the one shared helper across the client runtime, the store
persistence and the service lifecycle (no second TLS stack):

- **Validated before use.** :func:`validate_ca_cert_file` accepts only a
  bounded, readable, regular, certificates-only PEM file. Binary,
  oversize, non-regular, corrupt and private-key-bearing inputs fail
  closed with a safe message (no path value, no PEM material, no
  credentials — the operator just typed the path).
- **Supplemental, never pinning.** :func:`tls_context_for_worker_ca`
  starts from ``ssl.create_default_context()`` (CERT_REQUIRED, hostname
  checking, platform roots — the exact default
  :func:`scarcity_router.worker_client.tls_context_for_worker` builds)
  and only ADDS the CA file. There is no verify-off, no TOFU, no
  system-trust-store mutation, no ``SSL_CERT_FILE`` environment trick.
- **Origin-bound persistence record.** The stored value is one bounded,
  explicitly versioned JSON row in the EXISTING
  :class:`~scarcity_router.worker_local_store.WorkerLocalStore`
  (key :data:`WORKER_CA_TRUST_STORE_KEY`) holding only the origin it was
  validated for and the CANONICAL ABSOLUTE path of the CA file (never the
  certificate contents). Parsing is strict and fails closed. A record for
  a different origin is simply not applicable — never silently reused for
  an unrelated explicit ``--server`` target.

Compatibility (deliberate, minimal): the worker store keeps schema
version 1 — the trust record is one extra ``worker_state`` key older
software does not know, and because the CA only ever SUPPLEMENTS the
default verified context, old software ignoring the row behaves exactly
as before; nothing can bypass verification by ignoring or removing it.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

#: The trust record's schema version. A mismatch is a loud typed failure,
#: never a guess (the same discipline as the identity/settings versions).
WORKER_CA_TRUST_SCHEMA_VERSION = 1

#: The ``WorkerLocalStore`` value key holding the trust record. Reserved
#: against generic ``save_value`` writes so the record can only change
#: through the atomic pairing/persistence paths.
WORKER_CA_TRUST_STORE_KEY = "worker_ca_trust"

#: Size ceiling for one CA certificate file. A CA bundle of normal size is
#: a few KiB; anything near this bound is by definition not a curated
#: operator-provided trust file.
MAX_CA_CERT_FILE_BYTES = 65536

#: Serialized-size ceiling for the trust record (three small fields).
MAX_CA_TRUST_JSON_CHARS = 4096

_TRUST_KEYS: frozenset[str] = frozenset({"schema_version", "server_origin", "ca_path"})

_PEM_BEGIN_END = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)
_PEM_ANY_BEGIN = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----")


class WorkerCaTrustError(ValueError):
    """A worker CA-trust problem (safe message: no paths, PEM or secrets)."""


class _TrustValueReader(Protocol):
    """The one store capability the trust helpers need (keeps the
    helper importable from the store without an import cycle)."""

    def load_value(self, key: str) -> str | None: ...

    def load_identity(self) -> object | None: ...


@dataclass(frozen=True)
class WorkerCaTrust:
    """The origin-bound private-CA trust record (non-secret by design).

    ``server_origin`` is the origin key the trust was validated for
    (``srws://host:port``; a record never applies to another origin).
    ``ca_path`` is the CANONICAL ABSOLUTE path of the validated PEM file
    — persisted so a later service start (with a different working
    directory) resolves the same file, never a relative re-interpretation.
    """

    server_origin: str
    ca_path: str


def trust_to_json(trust: WorkerCaTrust) -> str:
    """The bounded canonical JSON document for one trust record."""
    document: dict[str, object] = {
        "schema_version": WORKER_CA_TRUST_SCHEMA_VERSION,
        "server_origin": trust.server_origin,
        "ca_path": trust.ca_path,
    }
    text = json.dumps(document, sort_keys=True)
    if len(text) > MAX_CA_TRUST_JSON_CHARS:
        # Unreachable for validated records; the bound exists so the
        # record can never silently become an unbounded blob.
        raise WorkerCaTrustError("the CA trust record is too large")
    return text


def trust_from_json(text: str) -> WorkerCaTrust:
    """Parse one stored trust record (strict; fails closed).

    Unknown keys, wrong types, a wrong schema version, a non-absolute or
    empty path or malformed JSON raise :class:`WorkerCaTrustError` — a
    corrupted record is a loud failure, never a partial guess and never a
    silently ignored trust change.
    """
    try:
        parsed = cast("object", json.loads(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise WorkerCaTrustError(
            "the stored CA trust record is malformed; use --ca-cert on a "
            + "verified run for the paired origin to replace it"
        ) from exc
    if not isinstance(parsed, dict):
        raise WorkerCaTrustError(
            "the stored CA trust record is malformed; use --ca-cert on a "
            + "verified run for the paired origin to replace it"
        )
    document = cast("dict[str, object]", parsed)
    if frozenset(document) != _TRUST_KEYS:
        raise WorkerCaTrustError(
            "the stored CA trust record has unexpected fields; use "
            + "--ca-cert on a verified run for the paired origin to replace it"
        )
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise WorkerCaTrustError("the stored CA trust record has a malformed schema version")
    if version != WORKER_CA_TRUST_SCHEMA_VERSION:
        raise WorkerCaTrustError(
            f"the stored CA trust record uses schema version {version}, "
            + f"not {WORKER_CA_TRUST_SCHEMA_VERSION}; use --ca-cert on a "
            + "verified run for the paired origin to replace it"
        )
    server_origin = document["server_origin"]
    ca_path = document["ca_path"]
    if not isinstance(server_origin, str) or not isinstance(ca_path, str):
        raise WorkerCaTrustError("the stored CA trust record is malformed")
    if not server_origin or not ca_path:
        raise WorkerCaTrustError("the stored CA trust record is malformed")
    if not os.path.isabs(ca_path):
        raise WorkerCaTrustError(
            "the stored CA trust record does not carry an absolute CA "
            + "path; use --ca-cert on a verified run for the paired origin "
            + "to replace it"
        )
    return WorkerCaTrust(server_origin=server_origin, ca_path=ca_path)


_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _read_validated_ca_material(
    path: str | os.PathLike[str],
    *,
    context: ssl.SSLContext | None = None,
) -> tuple[Path, str]:
    """Read and OpenSSL-parse one CA file from one bounded descriptor.

    The descriptor is fstat'd before reading and the exact bytes read are
    passed to ``load_verify_locations(cadata=...)``. This avoids validating
    one pathname version and then reopening a replaced file for TLS use.
    """
    try:
        text_argument = os.fspath(path)
    except TypeError as exc:
        raise WorkerCaTrustError(
            "--ca-cert needs the path of a PEM CA certificate file"
        ) from exc
    if not text_argument:
        raise WorkerCaTrustError("--ca-cert needs the path of a PEM CA certificate file")
    try:
        resolved = Path(os.path.realpath(text_argument))
    except OSError as exc:
        raise WorkerCaTrustError(
            "--ca-cert: the file could not be read; check that the path "
            + "names an existing readable PEM certificate file"
        ) from exc
    try:
        fd = os.open(
            os.fspath(resolved),
            os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW | _O_NONBLOCK,
        )
    except OSError as exc:
        raise WorkerCaTrustError(
            "--ca-cert: the file could not be read; check that the path "
            + "names an existing readable PEM certificate file"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WorkerCaTrustError(
                "--ca-cert: the path is not a regular file; pass the path of "
                + "a PEM CA certificate file"
            )
        if info.st_size > MAX_CA_CERT_FILE_BYTES:
            raise WorkerCaTrustError(
                "--ca-cert: the file is too large to be a curated CA bundle; "
                + "pass a certificates-only PEM file"
            )
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_CA_CERT_FILE_BYTES:
            chunk = os.read(fd, MAX_CA_CERT_FILE_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise WorkerCaTrustError(
            "--ca-cert: the file could not be read; check the file's "
            + "permissions"
        ) from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if len(data) > MAX_CA_CERT_FILE_BYTES:
        raise WorkerCaTrustError(
            "--ca-cert: the file is too large to be a curated CA bundle; "
            + "pass a certificates-only PEM file"
        )
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise WorkerCaTrustError(
            "--ca-cert: the file is not a PEM certificate file (binary "
            + "content is refused)"
        ) from exc
    markers = cast("list[str]", _PEM_ANY_BEGIN.findall(text))
    if any(marker != "CERTIFICATE" for marker in markers):
        raise WorkerCaTrustError(
            "--ca-cert: the file contains material other than CA "
            + "certificates (for example a private key); pass a "
            + "certificates-only PEM file — a private key is never adopted"
        )
    blocks = cast("list[str]", _PEM_BEGIN_END.findall(text))
    if not blocks:
        raise WorkerCaTrustError(
            "--ca-cert: the file contains no PEM certificate; pass a "
            + "certificates-only PEM CA file"
        )
    for block in blocks:
        try:
            der = ssl.PEM_cert_to_DER_cert(block)
        except (ValueError, ssl.SSLError) as exc:
            raise WorkerCaTrustError(
                "--ca-cert: a certificate in the file could not be "
                + "parsed; pass a valid certificates-only PEM CA file"
            ) from exc
        if not der:
            # base64 decoding is lenient (non-alphabet bytes are
            # discarded); an empty DER means the block was not a
            # certificate at all — fail closed, never adopt it.
            raise WorkerCaTrustError(
                "--ca-cert: a certificate in the file could not be "
                + "parsed; pass a valid certificates-only PEM CA file"
            )
    remainder = _PEM_BEGIN_END.sub("", text)
    remainder = "".join(remainder.split())
    if remainder:
        raise WorkerCaTrustError(
            "--ca-cert: the file carries content outside its PEM "
            + "certificate blocks; pass a certificates-only PEM CA file"
        )
    if context is None:
        context = ssl.create_default_context()
    try:
        # OpenSSL is the authoritative X.509 parser. cadata is the exact
        # bounded descriptor content already checked above, not a second
        # pathname read that could observe a replacement file.
        context.load_verify_locations(cadata=text)
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise WorkerCaTrustError(
            "--ca-cert: a certificate in the file could not be parsed; "
            + "pass a valid certificates-only PEM CA file"
        ) from exc
    return resolved, text


def validate_ca_cert_file(path: str | os.PathLike[str]) -> Path:
    """Validate one operator-provided CA file; return its canonical path."""
    resolved, _material = _read_validated_ca_material(path)
    return resolved


def tls_context_for_worker_ca(ca_path: str | os.PathLike[str]) -> ssl.SSLContext:
    """The default verified worker context PLUS one explicit private CA.

    Exactly :func:`scarcity_router.worker_client.tls_context_for_worker`
    (certificate AND hostname verification, platform roots — CERT_REQUIRED)
    with the CA file's certificates ADDED through ``load_verify_locations``.
    The private CA SUPPLEMENTS the platform roots; it never replaces them
    (this is additional trust, not certificate pinning), and no system
    trust store is ever mutated.
    """
    context = ssl.create_default_context()
    _resolved, _material = _read_validated_ca_material(ca_path, context=context)
    return context


def stored_ca_trust(store: _TrustValueReader) -> WorkerCaTrust | None:
    """The stored trust record, or ``None`` when none was persisted.

    Fails closed (:class:`WorkerCaTrustError`) when the stored record is
    malformed or uses an unsupported schema version — never a guess.
    """
    raw = store.load_value(WORKER_CA_TRUST_STORE_KEY)
    if raw is None:
        return None
    if len(raw) > MAX_CA_TRUST_JSON_CHARS:
        raise WorkerCaTrustError("the stored CA trust record is too large")
    return trust_from_json(raw)


def effective_stored_ca_path(
    store: _TrustValueReader, origin_key: str
) -> str | None:
    """The stored CA path WHEN it was validated for exactly this origin.

    A record bound to a different origin is not applicable (returns
    ``None`` — the plain default verified context applies); it is never
    silently reused for an unrelated explicit ``--server`` target. A
    malformed record fails closed.
    """
    trust = stored_ca_trust(store)
    if trust is None:
        return None
    identity = store.load_identity()
    if identity is None or getattr(identity, "server_origin", None) != origin_key:
        return None
    if trust.server_origin != origin_key:
        return None
    return trust.ca_path


__all__ = [
    "MAX_CA_CERT_FILE_BYTES",
    "MAX_CA_TRUST_JSON_CHARS",
    "WORKER_CA_TRUST_SCHEMA_VERSION",
    "WORKER_CA_TRUST_STORE_KEY",
    "WorkerCaTrust",
    "WorkerCaTrustError",
    "effective_stored_ca_path",
    "stored_ca_trust",
    "tls_context_for_worker_ca",
    "trust_from_json",
    "trust_to_json",
    "validate_ca_cert_file",
]
