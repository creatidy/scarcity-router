"""ModelInventory: the typed, bounded discovery document (D-053, #117).

What a worker's runtime discovery observed about the executable models of
its configured execution sources, in the exact normalized form the server
is allowed to consume. The document is the ONLY carrier of discovery
results across the worker protocol (version 2 state reports):

- **Typed and closed.** Every field is enumerated and validated; parsing
  rejects unknown keys, wrong types, unsafe identifiers and unknown enum
  values. There is deliberately NO passthrough for raw provider payload —
  the router consumes exactly the fields below and nothing else.
- **Bounded.** Sources, models per source, efforts per model and every
  string field carry hard caps so a malicious or broken runtime cannot
  inflate the protocol frame or the server's memory.
- **Credential-free by construction.** The field set cannot carry
  provider tokens, cookies or account metadata: no email, no raw account
  identifiers, no free-text notes. Runtime identity is limited to the
  program name and its self-reported version string.
- **Runtime-authoritative.`` Reasoning efforts are the runtime's OWN
  reported names (safe identifiers), never forced into a static
  vocabulary — the provider may introduce new effort names without a
  router release. Requests may only use efforts from the calibrated
  policy vocabulary, and selection may only use a model/effort pair this
  document actually reports (fail closed otherwise).

This is a versioned contract: :data:`MODEL_INVENTORY_SCHEMA_VERSION`
changes only through an explicit, tested migration, like every other
serialized contract in this repository.
"""

from __future__ import annotations

import json
import re
from typing import cast, override

from .gateway_validation import exact_shape, v_int, v_safe_id, v_str

#: The inventory document's own version (changes only via explicit
#: migration; the worker protocol negotiates transport separately).
MODEL_INVENTORY_SCHEMA_VERSION = 1

#: Hard bounds. A source exposes a bounded catalog; anything beyond the
#: caps is a fail-closed malformed document, never a truncation.
MAX_SOURCES_PER_REPORT = 8
MAX_MODELS_PER_SOURCE = 64
MAX_EFFORTS_PER_MODEL = 8
_MAX_RUNTIME_VERSION = 64
_MAX_DISPLAY_NAME = 128

#: Closed auth-state vocabulary for one source's observation. The worker
#: reports exactly one state per source per report:
#: - ``authenticated``: the source's own account is signed in and usable;
#: - ``auth_required``: the source needs its official login;
#: - ``unverified``: a protocol/schema failure prevented a verdict;
#: - ``unavailable``: the source/runtime is not usable at all.
SOURCE_AUTH_STATES: tuple[str, ...] = (
    "authenticated",
    "auth_required",
    "unverified",
    "unavailable",
)

#: The closed adapter-kind vocabulary. ``codex_subscription`` is the first
#: evidenced kind; new kinds arrive only through an explicit decision.
SOURCE_KINDS: tuple[str, ...] = ("codex_subscription",)

#: Upper bound for a source_id so the derived resource id
#: ``<source_id>:<slug>`` always fits the safe-id contract (64 chars)
#: with room for realistic physical slugs.
SOURCE_ID_MAX_LENGTH = 20

#: Upper bound for a runtime-reported slug: ``<source_id>:<slug>`` must
#: stay inside the safe-id contract (20 + 1 + 40 <= 64). A longer slug is
#: structural listing drift and fails closed at the worker, before any
#: report — it can never poison adoption or the registry read model.
SLUG_MAX_LENGTH = 40


def source_resource_id(source_id: str, slug: str) -> str:
    """The deterministic derived resource id (D-053 point 6).

    Both sides (worker discovery and server adoption) compute the same
    id from the same inventory, so no extra protocol state is needed.
    The combined form must stay a valid safe identifier.
    """
    if len(source_id) > SOURCE_ID_MAX_LENGTH:
        raise ModelInventoryError(
            f"source_id {source_id!r}: longer than {SOURCE_ID_MAX_LENGTH} chars; "
            + "derived resource ids would exceed the safe-id contract"
        )
    combined = f"{source_id}:{slug}"
    _ = v_safe_id(combined, "source_resource_id")
    return combined


def is_source_resource_id(resource_id: str) -> bool:
    """Whether an id has the derived ``<source_id>:<slug>`` shape.

    Shape-only check used by configuration validation to keep hand-made
    resources out of the derived namespace; ownership still resolves
    through the configured sources.
    """
    head, sep, tail = resource_id.partition(":")
    return (
        bool(sep)
        and bool(head)
        and bool(tail)
        and len(head) <= SOURCE_ID_MAX_LENGTH
    )


class ModelInventoryError(ValueError):
    """A discovery document failed validation (fail closed, no partial load)."""


def _v_effort_name(value: object, field: str) -> str:
    """A runtime-reported effort name: a bounded safe identifier."""
    name = v_safe_id(value, field)
    return name


_CANONICAL_MOMENT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def parse_canonical_moment(value: object, field: str) -> str:
    """Validate an RFC3339 UTC-milliseconds ``...Z`` timestamp."""
    text = v_str(value, field)
    if not _CANONICAL_MOMENT_RE.match(text):
        raise ModelInventoryError(f"{field}: not a canonical UTC timestamp")
    return text


def parse_inventory_document(document: object) -> dict[str, object]:
    """Parse one inventory document from untrusted bytes (strict JSON)."""
    if isinstance(document, (str, bytes, bytearray)):
        text = (
            document.decode("utf-8")
            if isinstance(document, (bytes, bytearray))
            else document
        )
        try:
            document = cast(
                "object",
                json.loads(text, object_pairs_hook=_reject_duplicate_keys),
            )
        except ValueError as exc:
            raise ModelInventoryError(f"inventory: malformed JSON: {exc}") from None
    if not isinstance(document, dict):
        raise ModelInventoryError("inventory: not a JSON object")
    return cast("dict[str, object]", document)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


class DiscoveredModel:
    """One physical model a source's runtime currently advertises.

    ``slug`` is the physical execution identity (exactly what the runtime
    accepts on dispatch); ``reasoning_efforts`` are the runtime-reported
    effort names (order preserved from the listing; unique). The pair is
    the exact-binding authority: a requested model/effort pair absent
    here fails closed at the worker AND at the server.
    """

    __slots__: tuple[str, ...] = ("slug", "reasoning_efforts", "display_name")

    def __init__(
        self,
        slug: str,
        reasoning_efforts: tuple[str, ...],
        display_name: str = "",
    ) -> None:
        try:
            if len(slug) > SLUG_MAX_LENGTH:
                raise ModelInventoryError(
                    f"discovered_model.slug: longer than {SLUG_MAX_LENGTH} chars; "
                    + "derived resource ids would exceed the safe-id contract"
                )
            checked_slug = v_safe_id(slug, "discovered_model.slug")
            seen: set[str] = set()
            efforts: list[str] = []
            for effort in reasoning_efforts:
                name = _v_effort_name(effort, "discovered_model.reasoning_efforts")
                if name in seen:
                    raise ModelInventoryError(
                        f"discovered_model {slug!r}: duplicate effort {name!r}"
                    )
                seen.add(name)
                efforts.append(name)
            if len(efforts) > MAX_EFFORTS_PER_MODEL:
                raise ModelInventoryError(
                    f"discovered_model {slug!r}: more than "
                    + f"{MAX_EFFORTS_PER_MODEL} reasoning efforts"
                )
            checked_display = display_name if display_name else ""
            if len(checked_display) > _MAX_DISPLAY_NAME:
                raise ModelInventoryError("discovered_model.display_name too long")
        except ModelInventoryError:
            raise
        except ValueError as exc:
            raise ModelInventoryError(f"discovered_model: {exc}") from None
        self.slug: str = checked_slug
        self.reasoning_efforts: tuple[str, ...] = tuple(efforts)
        self.display_name: str = checked_display

    @override
    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, DiscoveredModel)
            and self.slug == other.slug
            and self.reasoning_efforts == other.reasoning_efforts
            and self.display_name == other.display_name
        )

    @override
    def __repr__(self) -> str:
        return f"DiscoveredModel(slug={self.slug!r}, efforts={self.reasoning_efforts!r})"

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "slug": self.slug,
            "reasoning_efforts": list(self.reasoning_efforts),
        }
        if self.display_name:
            out["display_name"] = self.display_name
        return out

    @classmethod
    def from_dict(cls, d: object) -> "DiscoveredModel":
        dd = exact_shape(
            d, ("slug", "reasoning_efforts"), ("display_name",), "discovered_model"
        )
        efforts_raw = dd["reasoning_efforts"]
        if not isinstance(efforts_raw, list):
            raise ModelInventoryError("discovered_model.reasoning_efforts must be a list")
        return cls(
            slug=v_str(dd["slug"], "discovered_model.slug"),
            reasoning_efforts=tuple(
                v_str(item, "discovered_model.reasoning_efforts")
                for item in cast("list[object]", efforts_raw)
            ),
            display_name=(
                v_str(dd["display_name"], "discovered_model.display_name")
                if "display_name" in dd
                else ""
            ),
        )


class SourceInventory:
    """One execution source's discovery observation at one instant.

    ``adapter_id`` is the worker-local adapter INSTANCE id
    (``codex:<source_id>``); ``auth_state`` is the source's OWN provider
    authentication state — never the worker pairing state. ``models`` is
    the runtime's current listing (bounded).
    """

    __slots__: tuple[str, ...] = (
        "source_id",
        "adapter_id",
        "kind",
        "observed_at",
        "auth_state",
        "runtime_name",
        "runtime_version",
        "models",
    )

    def __init__(
        self,
        source_id: str,
        adapter_id: str,
        kind: str,
        observed_at: str,
        auth_state: str,
        runtime_name: str,
        runtime_version: str,
        models: tuple[DiscoveredModel, ...],
    ) -> None:
        self.source_id: str = v_safe_id(source_id, "source_inventory.source_id")
        self.adapter_id: str = v_safe_id(adapter_id, "source_inventory.adapter_id")
        if kind not in SOURCE_KINDS:
            raise ModelInventoryError(f"source_inventory.kind: unknown kind {kind!r}")
        self.kind: str = kind
        self.observed_at: str = parse_canonical_moment(
            observed_at, "source_inventory.observed_at"
        )
        if auth_state not in SOURCE_AUTH_STATES:
            raise ModelInventoryError(
                f"source_inventory.auth_state: unknown state {auth_state!r}"
            )
        self.auth_state: str = auth_state
        self.runtime_name: str = v_safe_id(
            runtime_name, "source_inventory.runtime_name"
        )
        version = v_str(runtime_version, "source_inventory.runtime_version")
        if len(version) > _MAX_RUNTIME_VERSION:
            raise ModelInventoryError("source_inventory.runtime_version too long")
        self.runtime_version: str = version
        if len(models) > MAX_MODELS_PER_SOURCE:
            raise ModelInventoryError(
                f"source_inventory {source_id!r}: more than "
                + f"{MAX_MODELS_PER_SOURCE} models"
            )
        slugs = {model.slug for model in models}
        if len(slugs) != len(models):
            raise ModelInventoryError(
                f"source_inventory {source_id!r}: duplicate model slug"
            )
        self.models: tuple[DiscoveredModel, ...] = tuple(
            sorted(models, key=lambda model: model.slug)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "adapter_id": self.adapter_id,
            "kind": self.kind,
            "observed_at": self.observed_at,
            "auth_state": self.auth_state,
            "runtime_name": self.runtime_name,
            "runtime_version": self.runtime_version,
            "models": [model.to_dict() for model in self.models],
        }

    @classmethod
    def from_dict(cls, d: object) -> "SourceInventory":
        try:
            return cls._from_dict_validated(d)
        except ModelInventoryError:
            raise
        except ValueError as exc:
            raise ModelInventoryError(f"source_inventory: {exc}") from None

    @classmethod
    def _from_dict_validated(cls, d: object) -> "SourceInventory":
        dd = exact_shape(
            d,
            (
                "source_id",
                "adapter_id",
                "kind",
                "observed_at",
                "auth_state",
                "runtime_name",
                "runtime_version",
                "models",
            ),
            (),
            "source_inventory",
        )
        models_raw = dd["models"]
        if not isinstance(models_raw, list):
            raise ModelInventoryError("source_inventory.models must be an array")
        try:
            return cls(
                source_id=v_str(dd["source_id"], "source_inventory.source_id"),
                adapter_id=v_str(dd["adapter_id"], "source_inventory.adapter_id"),
                kind=v_str(dd["kind"], "source_inventory.kind"),
                observed_at=v_str(dd["observed_at"], "source_inventory.observed_at"),
                auth_state=v_str(dd["auth_state"], "source_inventory.auth_state"),
                runtime_name=v_str(dd["runtime_name"], "source_inventory.runtime_name"),
                runtime_version=(
                    v_str(dd["runtime_version"], "source_inventory.runtime_version")
                ),
                models=tuple(
                    DiscoveredModel.from_dict(item)
                    for item in cast("list[object]", models_raw)
                ),
            )
        except (ModelInventoryError, ValueError) as exc:
            raise ModelInventoryError(f"source_inventory: {exc}") from None


class ModelInventoryReport:
    """The full bounded discovery document one worker reports.

    Schema version, worker identity and one inventory per configured
    source (bounded). Parsing is fail-closed: any violation rejects the
    whole document, never a partial load.
    """

    __slots__: tuple[str, ...] = ("schema_version", "worker_id", "sources")

    def __init__(
        self,
        worker_id: str,
        sources: tuple[SourceInventory, ...] = (),
        *,
        schema_version: int = MODEL_INVENTORY_SCHEMA_VERSION,
    ) -> None:
        if schema_version != MODEL_INVENTORY_SCHEMA_VERSION:
            raise ModelInventoryError(
                f"inventory.schema_version {schema_version} is not supported "
                + f"(expected {MODEL_INVENTORY_SCHEMA_VERSION})"
            )
        self.schema_version: int = schema_version
        self.worker_id: str = v_safe_id(worker_id, "inventory.worker_id")
        if len(sources) > MAX_SOURCES_PER_REPORT:
            raise ModelInventoryError(
                f"inventory: more than {MAX_SOURCES_PER_REPORT} sources"
            )
        source_ids = {source.source_id for source in sources}
        if len(source_ids) != len(sources):
            raise ModelInventoryError("inventory: duplicate source_id")
        adapter_ids = {source.adapter_id for source in sources}
        if len(adapter_ids) != len(sources):
            raise ModelInventoryError("inventory: duplicate adapter_id")
        self.sources: tuple[SourceInventory, ...] = tuple(
            sorted(sources, key=lambda source: source.source_id)
        )

    def source(self, source_id: str) -> SourceInventory | None:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "worker_id": self.worker_id,
            "sources": [source.to_dict() for source in self.sources],
        }

    @classmethod
    def from_dict(cls, d: object) -> "ModelInventoryReport":
        try:
            return cls._from_dict_validated(d)
        except ModelInventoryError:
            raise
        except ValueError as exc:
            raise ModelInventoryError(f"inventory: {exc}") from None

    @classmethod
    def _from_dict_validated(cls, d: object) -> "ModelInventoryReport":
        dd = exact_shape(
            d,
            ("schema_version", "worker_id", "sources"),
            (),
            "inventory",
        )
        sources_raw = dd["sources"]
        if not isinstance(sources_raw, list):
            raise ModelInventoryError("inventory.sources must be an array")
        try:
            return cls(
                worker_id=v_str(dd["worker_id"], "inventory.worker_id"),
                schema_version=v_int(dd["schema_version"], "inventory.schema_version"),
                sources=tuple(
                    SourceInventory.from_dict(item)
                    for item in cast("list[object]", sources_raw)
                ),
            )
        except (ModelInventoryError, ValueError) as exc:
            raise ModelInventoryError(f"inventory: {exc}") from None

    @classmethod
    def parse(cls, raw: object) -> "ModelInventoryReport":
        """Parse from untrusted input (strict JSON when given bytes/str)."""
        return cls.from_dict(parse_inventory_document(raw))


__all__ = [
    "DiscoveredModel",
    "SLUG_MAX_LENGTH",
    "SOURCE_ID_MAX_LENGTH",
    "is_source_resource_id",
    "source_resource_id",
    "MAX_EFFORTS_PER_MODEL",
    "MAX_MODELS_PER_SOURCE",
    "MAX_SOURCES_PER_REPORT",
    "MODEL_INVENTORY_SCHEMA_VERSION",
    "ModelInventoryError",
    "ModelInventoryReport",
    "SOURCE_AUTH_STATES",
    "SOURCE_KINDS",
    "SourceInventory",
]
