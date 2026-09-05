"""Local Ollama runtime response parsers.

Pure, deterministic provider-edge parsing: already decoded JSON-compatible
payloads from the local Ollama HTTP interface are validated against the
evidenced contract and reduced to the facts the collector needs. This module
performs zero I/O: it never reads the clock, filesystem, environment or
network, never touches credentials and never contacts a runtime itself. Live
acquisition is a separate concern (``ollama_acquisition``).

Provider semantics implemented here come only from the validated evidence in
docs/poc-evidence.md ("2026-09-04 M1 Ollama local runtime reconnaissance"):
the live local runtime (Ollama 0.33.1) and the installed binary's
serialization table. Every parser validates the shape deliberately and fails
closed: a response that does not satisfy the evidenced contract returns a
drift result (``None``) instead of partial facts.

Evidenced shapes (structure only; see poc-evidence.md):

- ``GET /api/version`` -> ``{"version": "<string>"}``; additive keys are
  tolerated, a missing or non-string ``version`` is drift;
- ``GET /api/tags`` -> ``{"models": [...]}``; every entry must be an object
  with a string ``name``; a non-object entry, a non-string/missing ``name``
  or a **duplicate** ``name`` is drift, because duplicate identities make
  the presence answer ambiguous rather than merely redundant;
- ``GET /api/ps`` -> ``{"models": [...]}``; every listed (loaded) entry must
  be an object with a string ``name`` and a positive integer
  ``context_length`` within the validated signed 64-bit band (the
  runtime's effective context for that loaded model). A listed entry
  without a usable ``context_length`` is drift: effective context is
  reported only from validated evidence, never inferred. A model that is
  simply not loaded is absent from the list and is not drift.

Digest identity: both listings carry a non-secret content ``digest``. The
validated shape is ``sha256:`` followed by 64 lowercase hex digits
(Ollama's manifest digest form); a missing or nonconforming digest is not
structural drift — it degrades that entry's identity evidence to ``None``
so the collector can withhold the effective context instead of attributing
it to an unverifiable model image. Digests are evidence for name+digest
agreement only and never enter normalized output.

The ``details.context_length`` value that ``/api/tags`` exposes is
model-file architecture metadata, not the runtime's effective context and
not user configuration; this parser deliberately never reads it, so the
configured and effective context facts stay independent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TypeGuard, cast

PROVIDER = "ollama"
SOURCE = "ollama_local"

# Validated non-secret content digest: Ollama's manifest digest form.
# Anything else degrades that entry's identity evidence (``None``), rather
# than drifting the whole listing or accepting an unverifiable image.
_SAFE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# A version string is identity evidence only when it is usable: a
# non-empty, non-control, non-padding string of bounded length. Unusable
# values fail closed instead of validating reachability.
_MAX_VERSION_LENGTH = 128

# Validated integer band for JSON-decoded values: signed 64-bit. The
# acquisition layer's strict decoder rejects out-of-band integers before
# any parser runs; this bound is defense in depth for direct parser calls
# so an arbitrary-precision Python integer can never become context
# evidence.
_MAX_I64 = 2**63 - 1
_MIN_I64 = -(2**63)

__all__ = [
    "PROVIDER",
    "SOURCE",
    "parse_ollama_ps_response",
    "parse_ollama_tags_response",
    "parse_ollama_version_response",
    "safe_digest",
]


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary value to a ``str``-keyed mapping, or ``None``."""
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _is_int(value: object) -> TypeGuard[int]:
    """True for a real JSON integer; booleans are not integers."""
    return isinstance(value, int) and not isinstance(value, bool)


def safe_digest(value: object) -> str | None:
    """Return the validated digest string, or ``None`` when unusable.

    ``None`` means the digest is absent or does not match the validated
    ``sha256:<64 lowercase hex>`` form; it is a degraded-identity signal,
    never a guessable value and never emitted.
    """
    if isinstance(value, str) and _SAFE_DIGEST_RE.fullmatch(value):
        return value
    return None


def parse_ollama_version_response(payload: object) -> bool:
    """Validate one decoded ``/api/version`` response.

    True only when the payload is an object carrying a **usable** string
    ``version``: non-empty, no control characters, no surrounding
    whitespace and bounded length. An empty, control-only or overlong
    value is drift (False) — reachability is never validated by an
    unusable identity, and the value is never echoed. Additive keys are
    tolerated; anything else is drift: the endpoint answered but not with
    the evidenced Ollama version contract.
    """
    envelope = _as_mapping(payload)
    if envelope is None:
        return False
    version = envelope.get("version")
    if not isinstance(version, str):
        return False
    if not 0 < len(version) <= _MAX_VERSION_LENGTH:
        return False
    if version != version.strip():
        return False
    return not any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in version
    )


def _listed_entries(
    payload: object,
) -> dict[str, Mapping[str, object]] | None:
    """Shared strict listing validation for ``/api/tags`` and ``/api/ps``.

    Validates the ``{"models": [...]}`` envelope and every entry's
    identity fields: each entry must carry a non-empty string ``name``
    and a matching non-empty string ``model`` (the evidenced contract
    carries both, with ``model`` equal to ``name``). A missing, malformed
    or **conflicting** ``model`` identity is drift — accepting it could
    attribute presence or effective context to a different model image —
    and so are duplicate names. Returns ``name -> entry`` on success,
    ``None`` on any structural drift.
    """
    envelope = _as_mapping(payload)
    if envelope is None:
        return None
    models = envelope.get("models")
    if not isinstance(models, list):
        return None
    listed: dict[str, Mapping[str, object]] = {}
    for item in cast("list[object]", models):
        entry = _as_mapping(item)
        if entry is None:
            return None
        name = entry.get("name")
        model = entry.get("model")
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(model, str) or not model or model != name:
            return None
        if name in listed:
            return None
        listed[name] = entry
    return listed


def parse_ollama_tags_response(payload: object) -> dict[str, str | None] | None:
    """Normalize one decoded ``/api/tags`` model listing.

    Returns ``name -> validated digest (or None)`` in listing order, or
    ``None`` when the payload is not the evidenced listing shape. A
    duplicate ``name`` is drift (ambiguous identity), not a redundant
    entry: the presence answer must never depend on which duplicate a
    reader happens to inspect. A missing or nonconforming digest yields a
    ``None`` digest for that entry (degraded identity evidence), never a
    guessed value.
    """
    listed = _listed_entries(payload)
    if listed is None:
        return None
    return {name: safe_digest(entry.get("digest")) for name, entry in listed.items()}


def parse_ollama_ps_response(
    payload: object,
) -> dict[str, tuple[str | None, int]] | None:
    """Normalize one decoded ``/api/ps`` loaded-model listing.

    Returns ``name -> (validated digest or None, context_length)`` for
    every loaded entry, or ``None`` on structural drift. Every listed entry
    must carry a positive integer ``context_length``: the effective context
    is accepted only as validated runtime evidence. A missing or
    nonconforming digest yields a ``None`` digest (degraded identity
    evidence) so the collector can withhold the effective context instead
    of attributing it to an unverifiable image. Absence of a model from the
    list (not loaded) is a normal state and is represented by the model's
    absence from the result, never by a guessed value.
    """
    listed = _listed_entries(payload)
    if listed is None:
        return None
    loaded: dict[str, tuple[str | None, int]] = {}
    for name, entry in listed.items():
        context_length = entry.get("context_length")
        if (
            not _is_int(context_length)
            or context_length < 1
            or context_length > _MAX_I64
            or context_length < _MIN_I64
        ):
            return None
        loaded[name] = (safe_digest(entry.get("digest")), context_length)
    return loaded
