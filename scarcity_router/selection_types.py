"""Task-requirement and model-catalog core contracts for Scarcity Router.

Pure, provider-independent, standard-library only. No filesystem, network or
environment access, no provider parsing and no runtime policy loading.

Implements the frozen M2b selection-input contracts (D-020, D-023, D-024):
- typed identity primitives: ``ModelRef``, ``ModelIdentity``,
  ``CapacityScopeRef``
- ``CapabilityMinima``: the fixed six-dimension task minima object
- ``HardConstraints``: the frozen nine-member typed hard-constraint vocabulary
- ``TaskRequirement``: task level plus minima plus hard constraints
- ``ModelHardProperties``: tri-state model hard properties
- capability provenance: ``EvidenceRef``, ``HumanOverride``,
  ``CapabilityAssessment`` and the six-dimension ``CapabilityAssessments``
- catalog values: ``ModelCatalogEntry`` and the ``ModelCatalog`` container

This module implements types, validation and deterministic serialization only.
It does not calibrate models or profiles, does not implement profile expansion
(profile expansion is a construction pathway owned by M2c, not a serialized
fourth field), does not score capability sufficiency and does not select.
No rating, minimum or catalog entry value exists in this module; populating
reviewed values is the M2c slice. Production code must not read the repository
policy artifact at runtime; repository consistency tests compare the frozen
vocabularies (``CAPABILITY_DIMENSIONS``, ``TASK_LEVELS``,
``SUPPORTED_PROVIDERS``) against it instead.

The invariants are enforced at *construction* so an invalid object can never
exist as a public, serializable value, whether produced by ``from_dict()`` or
by a direct public constructor. The private ``_v_*`` validators are the single
source of truth for the rules; ``from_dict`` uses them to check the serialized
shape and produce a well-typed value, while ``__post_init__`` applies the same
rules to the stored attributes. This is one validation path, not duplicated
rules. Serialized shapes are exact and deterministic: optional unknown values
are omitted except for the two states where explicit ``null`` is the contract
(``CapabilityAssessment`` unknown rating, ``capacity_bindings`` unknown
applicability).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import ClassVar, TypeVar, cast

from .errors import SelectionContractValidationError

# ── Frozen value sets ─────────────────────────────────────────────────────────

SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"openai", "zai"})

CAPABILITY_DIMENSIONS: tuple[str, ...] = (
    "reasoning",
    "coding",
    "scientific_methodological",
    "writing_editorial",
    "tool_use",
    "translation_multilingual",
)

TASK_LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4", "L5")

CONFIDENCE_VALUES: frozenset[str] = frozenset({"low", "medium", "high"})

MIN_RATING = 1
MAX_RATING = 5

# ── Validators (single source of truth for the M2b rules) ─────────────────────

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_T = TypeVar("_T")


def _v_str(value: object, fld: str) -> str:
    if not isinstance(value, str):
        raise SelectionContractValidationError(
            f"{fld}: expected str, got {type(value).__name__}"
        )
    return value


def _v_safe_id(value: object, fld: str) -> str:
    s = _v_str(value, fld)
    if not _SAFE_ID_RE.match(s):
        raise SelectionContractValidationError(
            f"{fld}: unsafe identifier {s!r}; "
            + "must match [a-z0-9][a-z0-9._:-]{0,63} (lowercase, max 64 chars)"
        )
    return s


def _v_provider(value: object, fld: str) -> str:
    s = _v_safe_id(value, fld)
    if s not in SUPPORTED_PROVIDERS:
        raise SelectionContractValidationError(
            f"{fld}: unsupported provider {s!r}; supported model providers "
            + f"are exactly {sorted(SUPPORTED_PROVIDERS)}; adding one is an "
            + "explicit contract/catalog change"
        )
    return s


def _v_enum(value: object, allowed: frozenset[str], fld: str) -> str:
    s = _v_str(value, fld)
    if s not in allowed:
        raise SelectionContractValidationError(
            f"{fld}: value {s!r} not in allowed set {sorted(allowed)}"
        )
    return s


def _v_bool(value: object, fld: str) -> bool:
    if not isinstance(value, bool):
        raise SelectionContractValidationError(
            f"{fld}: expected bool, got {type(value).__name__} ({value!r})"
        )
    return value


def _v_int(
    value: object,
    fld: str,
    *,
    lo: int | None = None,
    hi: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SelectionContractValidationError(
            f"{fld}: expected int, got {type(value).__name__} ({value!r})"
        )
    if lo is not None and value < lo:
        raise SelectionContractValidationError(f"{fld}: value {value} < {lo}")
    if hi is not None and value > hi:
        raise SelectionContractValidationError(f"{fld}: value {value} > {hi}")
    return value


def _v_rating(value: object, fld: str) -> int:
    return _v_int(value, fld, lo=MIN_RATING, hi=MAX_RATING)


def _v_date(value: object, fld: str) -> str:
    s = _v_str(value, fld)
    if not _DATE_RE.match(s):
        raise SelectionContractValidationError(
            f"{fld}: non-canonical date {s!r}; expected YYYY-MM-DD"
        )
    try:
        _ = date.fromisoformat(s)
    except ValueError:
        raise SelectionContractValidationError(
            f"{fld}: invalid calendar date {s!r}"
        )
    return s


def _v_text(
    value: object,
    fld: str,
    *,
    max_len: int,
) -> str:
    """Non-empty bounded human-readable text without control characters.

    Used for identifiers/labels that are not safe-ID slugs (evidence
    identifiers, rationales, display names, versions). Leading or trailing
    whitespace is rejected; interior whitespace is allowed.
    """
    s = _v_str(value, fld)
    if not s:
        raise SelectionContractValidationError(f"{fld}: must be non-empty")
    if len(s) > max_len:
        raise SelectionContractValidationError(
            f"{fld}: exceeds maximum length {max_len}"
        )
    if any(unicodedata.category(ch) == "Cc" for ch in s):
        raise SelectionContractValidationError(
            f"{fld}: control characters are not allowed"
        )
    if s != s.strip():
        raise SelectionContractValidationError(
            f"{fld}: leading/trailing whitespace is not allowed"
        )
    return s


def _as_str_object_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary mapping to ``str`` keys, or return ``None``.

    Python does not enforce annotations at runtime, so serialized input can be
    a mapping of any key type. ``isinstance`` cannot express the type
    parameters; this is the single explicit narrowing point for boundary data.
    """
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _v_tuple_of(value: object, item_type: type[_T], label: str) -> None:
    """Runtime shape guard: ``value`` must be a tuple of ``item_type``.

    Python does not enforce dataclass annotations at runtime, so direct
    construction can pass any container; this keeps the constructor invariant.
    """
    if not isinstance(value, tuple):
        raise SelectionContractValidationError(
            f"{label}: expected tuple, got {type(value).__name__}"
        )
    for item in cast("tuple[object, ...]", value):
        if not isinstance(item, item_type):
            raise SelectionContractValidationError(
                f"{label}: element must be a {item_type.__name__}, "
                + f"got {type(item).__name__}"
            )


def _v_instance_of(value: object, cls: type[_T], label: str) -> _T:
    """Runtime shape guard: ``value`` must be an instance of ``cls``.

    Python does not enforce dataclass annotations at runtime, so direct
    construction can pass any object; the check is meaningful at runtime even
    though the stored annotations look statically correct.
    """
    if not isinstance(value, cls):
        raise SelectionContractValidationError(
            f"{label}: expected a {cls.__name__}, got {type(value).__name__}"
        )
    return value


def _v_exact_shape(
    obj: object,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    label: str,
) -> Mapping[str, object]:
    """Validate the serialized shape of one record and narrow it to a mapping.

    Verifies the value is a mapping, rejects unknown keys, and reports any
    missing required keys as :class:`SelectionContractValidationError`. After
    this the caller may read required keys directly (``m[key]``) without a
    ``KeyError``. Semantics of the field values are NOT checked here; the
    ``_v_*`` validators and ``__post_init__`` own those rules.
    """
    m = _as_str_object_mapping(obj)
    if m is None:
        raise SelectionContractValidationError(
            f"{label}: expected a dict-like mapping, got {type(obj).__name__}"
        )
    allowed = frozenset(required) | frozenset(optional)
    extra = set(m.keys()) - allowed
    if extra:
        raise SelectionContractValidationError(
            f"{label}: unknown keys {sorted(extra)}"
        )
    missing = [key for key in required if key not in m]
    if missing:
        raise SelectionContractValidationError(
            f"{label}: missing required keys {sorted(missing)}"
        )
    return m


def _v_opt_safe_id(value: object | None, fld: str) -> str | None:
    return None if value is None else _v_safe_id(value, fld)


def _v_opt_provider(value: object | None, fld: str) -> str | None:
    return None if value is None else _v_provider(value, fld)


def _v_opt_int(
    value: object | None,
    fld: str,
    *,
    lo: int | None = None,
    hi: int | None = None,
) -> int | None:
    return None if value is None else _v_int(value, fld, lo=lo, hi=hi)


def _v_opt_date(value: object | None, fld: str) -> str | None:
    return None if value is None else _v_date(value, fld)


def _opt_bool_field(d: Mapping[str, object], key: str, label: str) -> bool:
    """Read an optional strict-boolean serialized field (absent/null = False)."""
    value = d.get(key)
    return False if value is None else _v_bool(value, f"{label}.{key}")


def _optional_present(d: Mapping[str, object], key: str) -> bool:
    """True when an optional serialized key carries a non-null value."""
    return d.get(key) is not None


# ── Identity primitives ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelRef:
    """Typed required-model constraint.

    Replaces the stringly qualified ``"provider/model"`` form: the provider
    and model are separate validated fields, so hard-constraint contradictions
    are detectable instead of parseable.
    """

    provider: str
    model: str

    _REQUIRED: ClassVar[tuple[str, ...]] = ("provider", "model")
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_provider(self.provider, "model_ref.provider")
        _ = _v_safe_id(self.model, "model_ref.model")

    @classmethod
    def from_dict(cls, d: object) -> "ModelRef":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "model_ref")
        return cls(
            provider=_v_provider(dd["provider"], "model_ref.provider"),
            model=_v_safe_id(dd["model"], "model_ref.model"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"provider": self.provider, "model": self.model}


@dataclass(frozen=True)
class ModelIdentity:
    """Stable catalog identity: ``provider + model + variant``.

    Identity is separate from the display name, the descriptive model class,
    the provider quota bucket, provider-internal aliases and capacity scopes.
    It is a typed triple, never one slash-delimited canonical string.
    """

    provider: str
    model: str
    variant: str

    _REQUIRED: ClassVar[tuple[str, ...]] = ("provider", "model", "variant")
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_provider(self.provider, "model_identity.provider")
        _ = _v_safe_id(self.model, "model_identity.model")
        _ = _v_safe_id(self.variant, "model_identity.variant")

    @classmethod
    def from_dict(cls, d: object) -> "ModelIdentity":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "model_identity")
        return cls(
            provider=_v_provider(dd["provider"], "model_identity.provider"),
            model=_v_safe_id(dd["model"], "model_identity.model"),
            variant=_v_safe_id(dd["variant"], "model_identity.variant"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "variant": self.variant,
        }


@dataclass(frozen=True)
class CapacityScopeRef:
    """Reference to one semantic capacity scope of the v3 capacity contract.

    The complete scope identity is ``(provider, scope_id)`` — exactly the
    ``(snapshot.provider, CapacityWindow.scope_id)`` pair of D-023. The
    reference carries no window kind, no window id, no model identity and no
    provider metadata; ``scope_id`` is opaque exact-match identity.
    """

    provider: str
    scope_id: str

    _REQUIRED: ClassVar[tuple[str, ...]] = ("provider", "scope_id")
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_provider(self.provider, "capacity_scope_ref.provider")
        _ = _v_safe_id(self.scope_id, "capacity_scope_ref.scope_id")

    @classmethod
    def from_dict(cls, d: object) -> "CapacityScopeRef":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "capacity_scope_ref")
        return cls(
            provider=_v_provider(dd["provider"], "capacity_scope_ref.provider"),
            scope_id=_v_safe_id(dd["scope_id"], "capacity_scope_ref.scope_id"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"provider": self.provider, "scope_id": self.scope_id}


# ── Task requirement ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CapabilityMinima:
    """Fixed six-dimension per-dimension task minima.

    ``None`` means this task has no minimum on the dimension; ``1..5`` means
    the required minimum on the frozen ordinal scale. This is a task-side
    requirement and is unrelated to model-capability unknownness. Dimensions
    are never averaged; sufficiency scoring is not implemented here.
    """

    reasoning: int | None = None
    coding: int | None = None
    scientific_methodological: int | None = None
    writing_editorial: int | None = None
    tool_use: int | None = None
    translation_multilingual: int | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ()
    _OPTIONAL: ClassVar[tuple[str, ...]] = CAPABILITY_DIMENSIONS

    def __post_init__(self) -> None:
        _ = _v_opt_rating(self.reasoning, "capability_minima.reasoning")
        _ = _v_opt_rating(self.coding, "capability_minima.coding")
        _ = _v_opt_rating(
            self.scientific_methodological,
            "capability_minima.scientific_methodological",
        )
        _ = _v_opt_rating(
            self.writing_editorial, "capability_minima.writing_editorial"
        )
        _ = _v_opt_rating(self.tool_use, "capability_minima.tool_use")
        _ = _v_opt_rating(
            self.translation_multilingual,
            "capability_minima.translation_multilingual",
        )

    @classmethod
    def from_dict(cls, d: object) -> "CapabilityMinima":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "capability_minima")
        return cls(
            reasoning=_v_opt_rating(dd.get("reasoning"), "capability_minima.reasoning"),
            coding=_v_opt_rating(dd.get("coding"), "capability_minima.coding"),
            scientific_methodological=_v_opt_rating(
                dd.get("scientific_methodological"),
                "capability_minima.scientific_methodological",
            ),
            writing_editorial=_v_opt_rating(
                dd.get("writing_editorial"), "capability_minima.writing_editorial"
            ),
            tool_use=_v_opt_rating(dd.get("tool_use"), "capability_minima.tool_use"),
            translation_multilingual=_v_opt_rating(
                dd.get("translation_multilingual"),
                "capability_minima.translation_multilingual",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.reasoning is not None:
            out["reasoning"] = self.reasoning
        if self.coding is not None:
            out["coding"] = self.coding
        if self.scientific_methodological is not None:
            out["scientific_methodological"] = self.scientific_methodological
        if self.writing_editorial is not None:
            out["writing_editorial"] = self.writing_editorial
        if self.tool_use is not None:
            out["tool_use"] = self.tool_use
        if self.translation_multilingual is not None:
            out["translation_multilingual"] = self.translation_multilingual
        return out


def _v_opt_rating(value: object | None, fld: str) -> int | None:
    return None if value is None else _v_rating(value, fld)


@dataclass(frozen=True)
class HardConstraints:
    """The frozen nine-member typed hard-constraint vocabulary.

    ``False`` for a ``requires_*`` field means the feature is not required by
    this task — never that a candidate must not support it. Numeric minima,
    when supplied, are positive integers. ``required_provider`` and
    ``required_model.provider`` must agree exactly when both are supplied;
    the contradiction is a validation error, never resolved heuristically.
    ``required_variant`` is the ninth independent constraint field.
    ``privacy_constraint`` is a safe opaque policy identifier; no privacy
    policy values or matching logic exist yet.
    """

    minimum_input_context_tokens: int | None = None
    minimum_output_tokens: int | None = None
    requires_tool_use: bool = False
    requires_vision: bool = False
    requires_reasoning_mode: bool = False
    required_provider: str | None = None
    required_model: ModelRef | None = None
    required_variant: str | None = None
    privacy_constraint: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ()
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "minimum_input_context_tokens",
        "minimum_output_tokens",
        "requires_tool_use",
        "requires_vision",
        "requires_reasoning_mode",
        "required_provider",
        "required_model",
        "required_variant",
        "privacy_constraint",
    )

    def __post_init__(self) -> None:
        _ = _v_opt_int(
            self.minimum_input_context_tokens,
            "hard_constraints.minimum_input_context_tokens",
            lo=1,
        )
        _ = _v_opt_int(
            self.minimum_output_tokens,
            "hard_constraints.minimum_output_tokens",
            lo=1,
        )
        _ = _v_bool(self.requires_tool_use, "hard_constraints.requires_tool_use")
        _ = _v_bool(self.requires_vision, "hard_constraints.requires_vision")
        _ = _v_bool(
            self.requires_reasoning_mode,
            "hard_constraints.requires_reasoning_mode",
        )
        _ = _v_opt_provider(
            self.required_provider, "hard_constraints.required_provider"
        )
        if self.required_model is not None:
            _ = _v_instance_of(
                self.required_model,
                ModelRef,
                "hard_constraints.required_model",
            )
        _ = _v_opt_safe_id(
            self.required_variant, "hard_constraints.required_variant"
        )
        _ = _v_opt_safe_id(
            self.privacy_constraint, "hard_constraints.privacy_constraint"
        )

        if (
            self.required_provider is not None
            and self.required_model is not None
            and self.required_model.provider != self.required_provider
        ):
            raise SelectionContractValidationError(
                "hard_constraints: required_provider "
                + f"{self.required_provider!r} contradicts required_model "
                + f"provider {self.required_model.provider!r}"
            )

    @classmethod
    def from_dict(cls, d: object) -> "HardConstraints":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "hard_constraints")
        required_model: ModelRef | None = None
        if _optional_present(dd, "required_model"):
            required_model = ModelRef.from_dict(dd["required_model"])
        return cls(
            minimum_input_context_tokens=_v_opt_int(
                dd.get("minimum_input_context_tokens"),
                "hard_constraints.minimum_input_context_tokens",
                lo=1,
            ),
            minimum_output_tokens=_v_opt_int(
                dd.get("minimum_output_tokens"),
                "hard_constraints.minimum_output_tokens",
                lo=1,
            ),
            requires_tool_use=_opt_bool_field(
                dd, "requires_tool_use", "hard_constraints"
            ),
            requires_vision=_opt_bool_field(dd, "requires_vision", "hard_constraints"),
            requires_reasoning_mode=_opt_bool_field(
                dd, "requires_reasoning_mode", "hard_constraints"
            ),
            required_provider=_v_opt_provider(
                dd.get("required_provider"), "hard_constraints.required_provider"
            ),
            required_model=required_model,
            required_variant=_v_opt_safe_id(
                dd.get("required_variant"), "hard_constraints.required_variant"
            ),
            privacy_constraint=_v_opt_safe_id(
                dd.get("privacy_constraint"), "hard_constraints.privacy_constraint"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.minimum_input_context_tokens is not None:
            out["minimum_input_context_tokens"] = self.minimum_input_context_tokens
        if self.minimum_output_tokens is not None:
            out["minimum_output_tokens"] = self.minimum_output_tokens
        if self.requires_tool_use:
            out["requires_tool_use"] = True
        if self.requires_vision:
            out["requires_vision"] = True
        if self.requires_reasoning_mode:
            out["requires_reasoning_mode"] = True
        if self.required_provider is not None:
            out["required_provider"] = self.required_provider
        if self.required_model is not None:
            out["required_model"] = self.required_model.to_dict()
        if self.required_variant is not None:
            out["required_variant"] = self.required_variant
        if self.privacy_constraint is not None:
            out["privacy_constraint"] = self.privacy_constraint
        return out


@dataclass(frozen=True)
class TaskRequirement:
    """A resolved task requirement.

    A resolved requirement stores exactly three parts: the task level
    (validated ``L0``–``L5`` vocabulary, not a capability score and never a
    source of capability minima), the explicit per-dimension capability
    minima and the typed hard constraints. Profile expansion is a
    *construction pathway* into these three parts, not a fourth serialized
    field; the expansion mechanism is owned by M2c and does not exist here.
    """

    task_level: str
    capability_minima: CapabilityMinima
    hard_constraints: HardConstraints

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "task_level",
        "capability_minima",
        "hard_constraints",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        if self.task_level not in TASK_LEVELS:
            raise SelectionContractValidationError(
                f"task_requirement.task_level: value {self.task_level!r} not "
                + f"in allowed set {list(TASK_LEVELS)}"
            )
        _ = _v_instance_of(
            self.capability_minima,
            CapabilityMinima,
            "task_requirement.capability_minima",
        )
        _ = _v_instance_of(
            self.hard_constraints, HardConstraints, "task_requirement.hard_constraints"
        )

    @classmethod
    def from_dict(cls, d: object) -> "TaskRequirement":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "task_requirement")
        return cls(
            task_level=_v_enum(
                dd["task_level"],
                frozenset(TASK_LEVELS),
                "task_requirement.task_level",
            ),
            capability_minima=CapabilityMinima.from_dict(dd["capability_minima"]),
            hard_constraints=HardConstraints.from_dict(dd["hard_constraints"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_level": self.task_level,
            "capability_minima": self.capability_minima.to_dict(),
            "hard_constraints": self.hard_constraints.to_dict(),
        }


# ── Model hard properties ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelHardProperties:
    """Tri-state model hard properties.

    ``None`` means the property is unknown or not yet evidenced; it is never
    silently interpreted as ``False``. Context/output allowances, when known,
    are positive integers. The three support fields are strict
    ``bool | None``. ``privacy_tags`` is ``None`` while privacy
    characteristics are not established, otherwise an explicit tuple of safe
    policy tags without duplicates; the stored tuple is canonical (sorted), so
    equality and serialization are independent of construction order.
    """

    input_context_tokens: int | None = None
    output_tokens: int | None = None
    supports_tool_use: bool | None = None
    supports_vision: bool | None = None
    supports_reasoning_mode: bool | None = None
    privacy_tags: tuple[str, ...] | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ()
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "input_context_tokens",
        "output_tokens",
        "supports_tool_use",
        "supports_vision",
        "supports_reasoning_mode",
        "privacy_tags",
    )

    def __post_init__(self) -> None:
        _ = _v_opt_int(
            self.input_context_tokens,
            "model_hard_properties.input_context_tokens",
            lo=1,
        )
        _ = _v_opt_int(
            self.output_tokens, "model_hard_properties.output_tokens", lo=1
        )
        _ = _v_opt_bool(
            self.supports_tool_use, "model_hard_properties.supports_tool_use"
        )
        _ = _v_opt_bool(self.supports_vision, "model_hard_properties.supports_vision")
        _ = _v_opt_bool(
            self.supports_reasoning_mode,
            "model_hard_properties.supports_reasoning_mode",
        )
        tags = self.privacy_tags
        if tags is not None:
            _ = _v_tuple_of(tags, str, "model_hard_properties.privacy_tags")
            seen: set[str] = set()
            validated_tags: list[str] = []
            for tag in cast("tuple[object, ...]", tags):
                validated = _v_safe_id(tag, "model_hard_properties.privacy_tags")
                if validated in seen:
                    raise SelectionContractValidationError(
                        "model_hard_properties.privacy_tags: duplicate tag "
                        + f"{validated!r}"
                    )
                seen.add(validated)
                validated_tags.append(validated)
            # Canonical stored form: sorted tags make equality and serialized
            # output deterministic and independent of construction order.
            object.__setattr__(self, "privacy_tags", tuple(sorted(validated_tags)))

    @classmethod
    def from_dict(cls, d: object) -> "ModelHardProperties":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "model_hard_properties")
        tags: tuple[str, ...] | None = None
        if _optional_present(dd, "privacy_tags"):
            raw_tags = dd["privacy_tags"]
            if not isinstance(raw_tags, list):
                raise SelectionContractValidationError(
                    "model_hard_properties.privacy_tags: expected list, got "
                    + f"{type(raw_tags).__name__}"
                )
            tags = tuple(
                _v_safe_id(tag, "model_hard_properties.privacy_tags")
                for tag in cast("list[object]", raw_tags)
            )
        return cls(
            input_context_tokens=_v_opt_int(
                dd.get("input_context_tokens"),
                "model_hard_properties.input_context_tokens",
                lo=1,
            ),
            output_tokens=_v_opt_int(
                dd.get("output_tokens"), "model_hard_properties.output_tokens", lo=1
            ),
            supports_tool_use=_v_opt_bool(
                dd.get("supports_tool_use"),
                "model_hard_properties.supports_tool_use",
            ),
            supports_vision=_v_opt_bool(
                dd.get("supports_vision"), "model_hard_properties.supports_vision"
            ),
            supports_reasoning_mode=_v_opt_bool(
                dd.get("supports_reasoning_mode"),
                "model_hard_properties.supports_reasoning_mode",
            ),
            privacy_tags=tags,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.input_context_tokens is not None:
            out["input_context_tokens"] = self.input_context_tokens
        if self.output_tokens is not None:
            out["output_tokens"] = self.output_tokens
        if self.supports_tool_use is not None:
            out["supports_tool_use"] = self.supports_tool_use
        if self.supports_vision is not None:
            out["supports_vision"] = self.supports_vision
        if self.supports_reasoning_mode is not None:
            out["supports_reasoning_mode"] = self.supports_reasoning_mode
        if self.privacy_tags is not None:
            out["privacy_tags"] = list(self.privacy_tags)
        return out


def _v_opt_bool(value: object | None, fld: str) -> bool | None:
    return None if value is None else _v_bool(value, fld)


# ── Capability provenance ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvidenceRef:
    """One provenance reference behind a capability assessment.

    ``source`` is a safe short identifier (for example ``official_docs``,
    ``benchmark``, ``owner_observation``); the source vocabulary is not a
    closed allowlist yet. ``identifier`` is non-empty bounded text sufficient
    to identify the source — URLs, DOI-like identifiers, release tags and
    benchmark IDs are all allowed, so it deliberately does not follow the
    safe-ID grammar. Control characters and unbounded values are rejected.
    """

    source: str
    identifier: str
    version: str | None = None
    date: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("source", "identifier")
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("version", "date")

    def __post_init__(self) -> None:
        _ = _v_safe_id(self.source, "evidence_ref.source")
        _ = _v_text(self.identifier, "evidence_ref.identifier", max_len=512)
        if self.version is not None:
            _ = _v_text(self.version, "evidence_ref.version", max_len=128)
        _ = _v_opt_date(self.date, "evidence_ref.date")

    @classmethod
    def from_dict(cls, d: object) -> "EvidenceRef":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "evidence_ref")
        version: str | None = None
        if _optional_present(dd, "version"):
            version = _v_text(dd["version"], "evidence_ref.version", max_len=128)
        return cls(
            source=_v_safe_id(dd["source"], "evidence_ref.source"),
            identifier=_v_text(dd["identifier"], "evidence_ref.identifier", max_len=512),
            version=version,
            date=_v_opt_date(dd.get("date"), "evidence_ref.date"),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"source": self.source, "identifier": self.identifier}
        if self.version is not None:
            out["version"] = self.version
        if self.date is not None:
            out["date"] = self.date
        return out


@dataclass(frozen=True)
class HumanOverride:
    """An explicit, preserved human capability-override record.

    The override never mutates or replaces the assessed rating: both the
    source assessment and the override remain serialized and reviewable, and
    the effective value is exposed only as a derived property. No account or
    user identity is recorded.
    """

    rating: int
    decided_on: str
    rationale: str

    _REQUIRED: ClassVar[tuple[str, ...]] = ("rating", "decided_on", "rationale")
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_rating(self.rating, "human_override.rating")
        _ = _v_date(self.decided_on, "human_override.decided_on")
        _ = _v_text(self.rationale, "human_override.rationale", max_len=2048)

    @classmethod
    def from_dict(cls, d: object) -> "HumanOverride":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "human_override")
        return cls(
            rating=_v_rating(dd["rating"], "human_override.rating"),
            decided_on=_v_date(dd["decided_on"], "human_override.decided_on"),
            rationale=_v_text(
                dd["rationale"], "human_override.rationale", max_len=2048
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "rating": self.rating,
            "decided_on": self.decided_on,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class CapabilityAssessment:
    """One dimension's capability assessment with reviewable provenance.

    ``rating=None`` is the canonical representation of unknown capability
    (``UNKNOWN_CAPABILITY``); it is never zero. A known rating (``1..5``)
    requires complete provenance: at least one ``EvidenceRef``, a coarse
    ``low|medium|high`` confidence (no float scores), an assessment date and a
    non-empty rationale — a naked rating is invalid. An unknown assessment
    serializes exactly ``{"rating": null}``; partial provenance on an unknown
    rating is not representable.

    ``human_override`` preserves the original curated rating and evidence and
    never mutates them; ``effective_rating`` is the derived view. An override
    requires an existing known base rating.
    """

    rating: int | None
    evidence: tuple[EvidenceRef, ...] = ()
    confidence: str | None = None
    assessed_on: str | None = None
    rationale: str | None = None
    human_override: HumanOverride | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("rating",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "evidence",
        "confidence",
        "assessed_on",
        "rationale",
        "human_override",
    )

    def __post_init__(self) -> None:
        _ = _v_opt_rating(self.rating, "capability_assessment.rating")
        _ = _v_tuple_of(
            self.evidence, EvidenceRef, "capability_assessment.evidence"
        )

        if self.rating is None:
            # Unknown capability: no partial provenance state exists.
            if self.evidence:
                raise SelectionContractValidationError(
                    "capability_assessment: unknown rating must not carry "
                    + "evidence"
                )
            for name in ("confidence", "assessed_on", "rationale"):
                if getattr(self, name) is not None:
                    raise SelectionContractValidationError(
                        f"capability_assessment: unknown rating must not "
                        + f"carry {name}"
                    )
            if self.human_override is not None:
                raise SelectionContractValidationError(
                    "capability_assessment: a human override requires an "
                    + "existing known base rating"
                )
            return

        if not self.evidence:
            raise SelectionContractValidationError(
                "capability_assessment: a known rating requires at least one "
                + "evidence reference"
            )
        _ = _v_enum(
            self.confidence, CONFIDENCE_VALUES, "capability_assessment.confidence"
        )
        _ = _v_date(self.assessed_on, "capability_assessment.assessed_on")
        if self.rationale is None:
            raise SelectionContractValidationError(
                "capability_assessment: a known rating requires a non-empty "
                + "rationale"
            )
        _ = _v_text(self.rationale, "capability_assessment.rationale", max_len=2048)

    @property
    def effective_rating(self) -> int | None:
        """The override's rating when present, otherwise the assessed rating."""
        if self.human_override is not None:
            return self.human_override.rating
        return self.rating

    @classmethod
    def from_dict(cls, d: object) -> "CapabilityAssessment":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "capability_assessment")
        raw_rating = dd["rating"]
        rating = None if raw_rating is None else _v_rating(
            raw_rating, "capability_assessment.rating"
        )

        raw_evidence = dd.get("evidence")
        evidence: tuple[EvidenceRef, ...] = ()
        if raw_evidence is not None:
            if not isinstance(raw_evidence, list):
                raise SelectionContractValidationError(
                    "capability_assessment.evidence: expected list, got "
                    + f"{type(raw_evidence).__name__}"
                )
            evidence = tuple(
                EvidenceRef.from_dict(x) for x in cast("list[object]", raw_evidence)
            )

        override: HumanOverride | None = None
        if _optional_present(dd, "human_override"):
            override = HumanOverride.from_dict(dd["human_override"])

        confidence: str | None = None
        if _optional_present(dd, "confidence"):
            confidence = _v_enum(
                dd["confidence"], CONFIDENCE_VALUES, "capability_assessment.confidence"
            )
        assessed_on = _v_opt_date(
            dd.get("assessed_on"), "capability_assessment.assessed_on"
        )
        rationale: str | None = None
        if _optional_present(dd, "rationale"):
            rationale = _v_text(
                dd["rationale"], "capability_assessment.rationale", max_len=2048
            )

        return cls(
            rating=rating,
            evidence=evidence,
            confidence=confidence,
            assessed_on=assessed_on,
            rationale=rationale,
            human_override=override,
        )

    def to_dict(self) -> dict[str, object]:
        # The explicit null is the contract: unknown capability is materially
        # different from a malformed or missing dimension.
        if self.rating is None:
            return {"rating": None}
        out: dict[str, object] = {
            "rating": self.rating,
            "evidence": [e.to_dict() for e in self.evidence],
            "confidence": self.confidence,
            "assessed_on": self.assessed_on,
            "rationale": self.rationale,
        }
        if self.human_override is not None:
            out["human_override"] = self.human_override.to_dict()
        return out


@dataclass(frozen=True)
class CapabilityAssessments:
    """The fixed six-dimension capability assessment vector.

    All six dimensions are required — in construction and in serialized
    catalog entries. Unknown capability is the explicit ``{"rating": null}``
    state, never an omitted dimension, so a well-formed "unknown" is
    distinguishable from a missing schema dimension. Extra dimensions are
    rejected.
    """

    reasoning: CapabilityAssessment
    coding: CapabilityAssessment
    scientific_methodological: CapabilityAssessment
    writing_editorial: CapabilityAssessment
    tool_use: CapabilityAssessment
    translation_multilingual: CapabilityAssessment

    _REQUIRED: ClassVar[tuple[str, ...]] = CAPABILITY_DIMENSIONS
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_instance_of(self.reasoning, CapabilityAssessment, "capability_assessments.reasoning")
        _ = _v_instance_of(self.coding, CapabilityAssessment, "capability_assessments.coding")
        _ = _v_instance_of(
            self.scientific_methodological,
            CapabilityAssessment,
            "capability_assessments.scientific_methodological",
        )
        _ = _v_instance_of(
            self.writing_editorial,
            CapabilityAssessment,
            "capability_assessments.writing_editorial",
        )
        _ = _v_instance_of(self.tool_use, CapabilityAssessment, "capability_assessments.tool_use")
        _ = _v_instance_of(
            self.translation_multilingual,
            CapabilityAssessment,
            "capability_assessments.translation_multilingual",
        )

    @classmethod
    def from_dict(cls, d: object) -> "CapabilityAssessments":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "capability_assessments")
        return cls(
            reasoning=CapabilityAssessment.from_dict(dd["reasoning"]),
            coding=CapabilityAssessment.from_dict(dd["coding"]),
            scientific_methodological=CapabilityAssessment.from_dict(
                dd["scientific_methodological"]
            ),
            writing_editorial=CapabilityAssessment.from_dict(dd["writing_editorial"]),
            tool_use=CapabilityAssessment.from_dict(dd["tool_use"]),
            translation_multilingual=CapabilityAssessment.from_dict(
                dd["translation_multilingual"]
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "reasoning": self.reasoning.to_dict(),
            "coding": self.coding.to_dict(),
            "scientific_methodological": self.scientific_methodological.to_dict(),
            "writing_editorial": self.writing_editorial.to_dict(),
            "tool_use": self.tool_use.to_dict(),
            "translation_multilingual": self.translation_multilingual.to_dict(),
        }


# ── Model catalog values ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelCatalogEntry:
    """One catalog model entry (contract only; no entries are populated yet).

    ``capacity_bindings`` semantics are critical: ``None`` means
    model-to-capacity applicability is **unknown** and must never be read as
    "this model consumes no subscription quota"; a non-empty tuple of
    ``CapacityScopeRef`` means applicability is known. The empty tuple is
    invalid and must never serve as an optimistic "unmetered" state. Known
    bindings must be unique, must serialize deterministically and must use
    the model's own provider; cross-provider capacity consumption is not an
    evidenced use case and requires an explicit future contract decision.
    """

    identity: ModelIdentity
    display_name: str
    hard_properties: ModelHardProperties
    capabilities: CapabilityAssessments
    capacity_bindings: tuple[CapacityScopeRef, ...] | None
    model_version: str | None = None
    model_version_date: str | None = None
    last_reviewed_on: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "identity",
        "display_name",
        "hard_properties",
        "capabilities",
        "capacity_bindings",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "model_version",
        "model_version_date",
        "last_reviewed_on",
    )

    def __post_init__(self) -> None:
        _ = _v_instance_of(self.identity, ModelIdentity, "model_catalog_entry.identity")
        _ = _v_text(
            self.display_name, "model_catalog_entry.display_name", max_len=128
        )
        _ = _v_instance_of(
            self.hard_properties,
            ModelHardProperties,
            "model_catalog_entry.hard_properties",
        )
        _ = _v_instance_of(
            self.capabilities,
            CapabilityAssessments,
            "model_catalog_entry.capabilities",
        )

        bindings = self.capacity_bindings
        if bindings is not None:
            _ = _v_tuple_of(
                bindings, CapacityScopeRef, "model_catalog_entry.capacity_bindings"
            )
            if not bindings:
                raise SelectionContractValidationError(
                    "model_catalog_entry.capacity_bindings: known "
                    + "applicability must be a non-empty tuple of "
                    + "CapacityScopeRef; the empty set is invalid and None "
                    + "means unknown applicability"
                )
            seen: set[tuple[str, str]] = set()
            for binding in bindings:
                if binding.provider != self.identity.provider:
                    raise SelectionContractValidationError(
                        "model_catalog_entry.capacity_bindings: binding "
                        + f"provider {binding.provider!r} does not match the "
                        + f"model provider {self.identity.provider!r}; "
                        + "cross-provider bindings are unsupported in the "
                        + "initial contract"
                    )
                key = (binding.provider, binding.scope_id)
                if key in seen:
                    raise SelectionContractValidationError(
                        "model_catalog_entry.capacity_bindings: duplicate "
                        + f"binding {key}"
                    )
                seen.add(key)

        if self.model_version is not None:
            _ = _v_text(
                self.model_version, "model_catalog_entry.model_version", max_len=64
            )
        _ = _v_opt_date(
            self.model_version_date, "model_catalog_entry.model_version_date"
        )
        _ = _v_opt_date(
            self.last_reviewed_on, "model_catalog_entry.last_reviewed_on"
        )

    @classmethod
    def from_dict(cls, d: object) -> "ModelCatalogEntry":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "model_catalog_entry")
        bindings: tuple[CapacityScopeRef, ...] | None = None
        raw_bindings = dd["capacity_bindings"]
        if raw_bindings is not None:
            if not isinstance(raw_bindings, list):
                raise SelectionContractValidationError(
                    "model_catalog_entry.capacity_bindings: expected list or "
                    + f"null, got {type(raw_bindings).__name__}"
                )
            bindings = tuple(
                CapacityScopeRef.from_dict(x)
                for x in cast("list[object]", raw_bindings)
            )
        model_version: str | None = None
        if _optional_present(dd, "model_version"):
            model_version = _v_text(
                dd["model_version"], "model_catalog_entry.model_version", max_len=64
            )
        return cls(
            identity=ModelIdentity.from_dict(dd["identity"]),
            display_name=_v_text(
                dd["display_name"], "model_catalog_entry.display_name", max_len=128
            ),
            hard_properties=ModelHardProperties.from_dict(dd["hard_properties"]),
            capabilities=CapabilityAssessments.from_dict(dd["capabilities"]),
            capacity_bindings=bindings,
            model_version=model_version,
            model_version_date=_v_opt_date(
                dd.get("model_version_date"),
                "model_catalog_entry.model_version_date",
            ),
            last_reviewed_on=_v_opt_date(
                dd.get("last_reviewed_on"), "model_catalog_entry.last_reviewed_on"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "identity": self.identity.to_dict(),
            "display_name": self.display_name,
            "hard_properties": self.hard_properties.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            # None (unknown applicability) serializes as an explicit null.
            "capacity_bindings": (
                None
                if self.capacity_bindings is None
                else [
                    b.to_dict()
                    for b in sorted(
                        self.capacity_bindings,
                        key=lambda b: (b.provider, b.scope_id),
                    )
                ]
            ),
        }
        if self.model_version is not None:
            out["model_version"] = self.model_version
        if self.model_version_date is not None:
            out["model_version_date"] = self.model_version_date
        if self.last_reviewed_on is not None:
            out["last_reviewed_on"] = self.last_reviewed_on
        return out


@dataclass(frozen=True)
class ModelCatalog:
    """Pure typed model-catalog container.

    Model identities must be unique. An empty catalog is structurally valid:
    M2b implements the contract before M2c populates the reviewed artifact.
    Serialization is deterministic and independent of insertion order:
    entries are sorted by ``(provider, model, variant)``.
    """

    catalog_version: int
    updated_on: str
    entries: tuple[ModelCatalogEntry, ...]

    _REQUIRED: ClassVar[tuple[str, ...]] = ("catalog_version", "updated_on", "entries")
    _OPTIONAL: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self) -> None:
        _ = _v_int(self.catalog_version, "model_catalog.catalog_version", lo=1)
        _ = _v_date(self.updated_on, "model_catalog.updated_on")
        _ = _v_tuple_of(self.entries, ModelCatalogEntry, "model_catalog.entries")
        seen: set[tuple[str, str, str]] = set()
        for entry in self.entries:
            identity = entry.identity
            key = (identity.provider, identity.model, identity.variant)
            if key in seen:
                raise SelectionContractValidationError(
                    f"model_catalog.entries: duplicate model identity {key}"
                )
            seen.add(key)

    @classmethod
    def from_dict(cls, d: object) -> "ModelCatalog":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "model_catalog")
        raw_entries = dd["entries"]
        if not isinstance(raw_entries, list):
            raise SelectionContractValidationError(
                f"model_catalog.entries: expected list, got "
                + f"{type(raw_entries).__name__}"
            )
        entries = tuple(
            ModelCatalogEntry.from_dict(x) for x in cast("list[object]", raw_entries)
        )
        return cls(
            catalog_version=_v_int(
                dd["catalog_version"], "model_catalog.catalog_version", lo=1
            ),
            updated_on=_v_date(dd["updated_on"], "model_catalog.updated_on"),
            entries=entries,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog_version": self.catalog_version,
            "updated_on": self.updated_on,
            "entries": [
                e.to_dict()
                for e in sorted(
                    self.entries,
                    key=lambda e: (
                        e.identity.provider,
                        e.identity.model,
                        e.identity.variant,
                    ),
                )
            ],
        }
