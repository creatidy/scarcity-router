"""V1 capacity model for Scarcity Router.

Pure, provider-independent, standard-library only.

Implements the frozen v1 normalized capacity contract from docs/capacity-model.md:
- snapshot, status, windows, local runtime, diagnostics
- validation of all v1 invariants
- deterministic JSON-compatible serialization

The v1 invariants are enforced at *construction* so an invalid object can never
exist as a public, serializable value whether it is produced by ``from_dict()``
or by a direct public constructor. The private ``_v_*`` validators are the single
source of truth for the rules; ``from_dict`` uses them to check the serialized
shape and produce a well-typed value, while ``__post_init__`` applies the same
rules to the stored attributes. This is one validation path, not duplicated rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, TypeVar, cast

from .errors import CapacityValidationError

# ── Frozen value sets ─────────────────────────────────────────────────────────

SCHEMA_VERSION = 1

_STATUS_VALUES: frozenset[str] = frozenset({
    "ok",
    "unavailable",
    "auth_required",
    "unsupported",
    "schema_changed",
    "unknown",
})

_RESOURCE_VALUES: frozenset[str] = frozenset({"tokens", "time", "unknown"})

_KIND_VALUES: frozenset[str] = frozenset({"five_hour", "weekly", "unknown"})

_MODEL_PRESENCE_VALUES: frozenset[str] = frozenset({"present", "missing", "unknown"})

_DIAGNOSTIC_CODES: frozenset[str] = frozenset({
    "source_unavailable",
    "auth_required",
    "unsupported_source",
    "schema_changed",
    "telemetry_unknown",
    "window_semantics_unknown",
    "percentage_unknown",
    "reset_unknown",
    "runtime_unreachable",
    "model_missing",
    "model_presence_unknown",
    "configured_context_unknown",
    "effective_context_unknown",
})

_WINDOW_SCOPED_CODES: frozenset[str] = frozenset({
    "window_semantics_unknown",
    "percentage_unknown",
    "reset_unknown",
})

_STATUSES_WITHOUT_WINDOWS: frozenset[str] = frozenset({
    "unavailable",
    "auth_required",
    "unsupported",
    "schema_changed",
})

_STATUS_REQUIRED_CODE: dict[str, str] = {
    "unavailable": "source_unavailable",
    "auth_required": "auth_required",
    "unsupported": "unsupported_source",
    "schema_changed": "schema_changed",
    "unknown": "telemetry_unknown",
}

_KIND_DURATION: dict[str, int] = {
    "five_hour": 18_000,
    "weekly": 604_800,
}

# ── Validators (single source of truth for the v1 rules) ─────────────────────

_CANONICAL_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

T = TypeVar("T")


def _v_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise CapacityValidationError(
            f"{field}: expected bool, got {type(value).__name__} ({value!r})"
        )
    return value


def _v_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise CapacityValidationError(
            f"{field}: expected str, got {type(value).__name__}"
        )
    return value


def _v_safe_id(value: object, field: str) -> str:
    s = _v_str(value, field)
    if not _SAFE_ID_RE.match(s):
        raise CapacityValidationError(
            f"{field}: unsafe identifier {s!r}; "
            + "must match [a-z0-9][a-z0-9._:-]{0,63} (lowercase, max 64 chars)"
        )
    return s


def _v_enum(value: object, allowed: frozenset[str], field: str) -> str:
    s = _v_str(value, field)
    if s not in allowed:
        raise CapacityValidationError(
            f"{field}: value {s!r} not in allowed set {sorted(allowed)}"
        )
    return s


def _v_ts(value: object, field: str) -> str:
    s = _v_str(value, field)
    if not _CANONICAL_TS_RE.match(s):
        raise CapacityValidationError(
            f"{field}: non-canonical timestamp {s!r}; "
            + "expected YYYY-MM-DDTHH:MM:SS.sssZ"
        )
    try:
        _ = datetime.fromisoformat(s[:-1] + "+00:00")
    except ValueError:
        raise CapacityValidationError(f"{field}: invalid date/time in {s!r}")
    return s


def _v_int(
    value: object,
    field: str,
    *,
    lo: int | None = None,
    hi: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapacityValidationError(
            f"{field}: expected int, got {type(value).__name__} ({value!r})"
        )
    if lo is not None and value < lo:
        raise CapacityValidationError(f"{field}: value {value} < {lo}")
    if hi is not None and value > hi:
        raise CapacityValidationError(f"{field}: value {value} > {hi}")
    return value


def _v_pct_pair(used: object | None, remain: object | None, ctx: str) -> None:
    if (used is None) != (remain is None):
        raise CapacityValidationError(
            f"{ctx}: used_percent and remaining_percent must both be present "
            + "or both absent"
        )
    if used is not None:
        u = _v_int(used, f"{ctx}.used_percent", lo=0, hi=100)
        r = _v_int(remain, f"{ctx}.remaining_percent", lo=0, hi=100)
        if u + r != 100:
            raise CapacityValidationError(
                f"{ctx}: used_percent {u} + remaining_percent {r} != 100"
            )


def _as_str_object_mapping(value: object) -> Mapping[str, object] | None:
    """Narrow a boundary mapping to ``str`` keys, or return ``None``.

    Python does not enforce annotations at runtime, so serialized input can be
    a mapping of any key type. ``isinstance`` cannot express the type
    parameters; this is the single explicit narrowing point for boundary data.
    """
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return None


def _v_tuple_of(value: object, item_type: type[T], label: str) -> None:
    """Runtime shape guard: ``value`` must be a tuple of ``item_type``.

    Python does not enforce dataclass annotations at runtime, so direct
    construction can pass any container; this keeps the constructor invariant.
    """
    if not isinstance(value, tuple):
        raise CapacityValidationError(
            f"{label}: expected tuple, got {type(value).__name__}"
        )
    for item in cast("tuple[object, ...]", value):
        if not isinstance(item, item_type):
            raise CapacityValidationError(
                f"{label}: element must be a {item_type.__name__}, "
                + f"got {type(item).__name__}"
            )


def _v_instance_of(value: object, cls: type[T], label: str) -> None:
    if not isinstance(value, cls):
        raise CapacityValidationError(
            f"{label}: must be a {cls.__name__}, got {type(value).__name__}"
        )


def _v_exact_shape(
    obj: object,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    label: str,
) -> Mapping[str, object]:
    """Validate the serialized shape of one record and narrow it to a mapping.

    Verifies the value is a mapping, rejects unknown keys, and reports any
    missing required keys as :class:`CapacityValidationError`. After this the
    caller may read required keys directly (``m[key]``) without a ``KeyError``.
    Semantics of the field values are NOT checked here; the ``_v_*`` validators
    and ``__post_init__`` own those rules.
    """
    m = _as_str_object_mapping(obj)
    if m is None:
        raise CapacityValidationError(
            f"{label}: expected a dict-like mapping, got {type(obj).__name__}"
        )
    allowed = frozenset(required) | frozenset(optional)
    extra = set(m.keys()) - allowed
    if extra:
        raise CapacityValidationError(f"{label}: unknown keys {sorted(extra)}")
    missing = [key for key in required if key not in m]
    if missing:
        raise CapacityValidationError(f"{label}: missing required keys {sorted(missing)}")
    return m


def _v_opt_ts(value: object | None, field: str) -> str | None:
    return None if value is None else _v_ts(value, field)


def _v_opt_safe_id(value: object | None, field: str) -> str | None:
    return None if value is None else _v_safe_id(value, field)


def _v_opt_int(
    value: object | None,
    field: str,
    *,
    lo: int | None = None,
    hi: int | None = None,
) -> int | None:
    return None if value is None else _v_int(value, field, lo=lo, hi=hi)


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CapacityDiagnostic:
    """One allowlisted diagnostic record."""

    code: str
    window_id: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("code",)
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("window_id",)

    def __post_init__(self) -> None:
        code = _v_enum(self.code, _DIAGNOSTIC_CODES, "diagnostic.code")
        wid = self.window_id
        if code in _WINDOW_SCOPED_CODES:
            if wid is not None:
                _ = _v_safe_id(wid, "diagnostic.window_id")
        elif wid is not None:
            raise CapacityValidationError(
                f"diagnostic: code {code!r} is not window-scoped and "
                + "must not carry window_id"
            )

    @classmethod
    def from_dict(cls, d: object) -> "CapacityDiagnostic":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "diagnostic")
        return cls(
            code=_v_enum(dd["code"], _DIAGNOSTIC_CODES, "diagnostic.code"),
            window_id=_v_opt_safe_id(dd.get("window_id"), "diagnostic.window_id"),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"code": self.code}
        if self.window_id is not None:
            out["window_id"] = self.window_id
        return out


@dataclass(frozen=True)
class CapacityWindow:
    """One normalized quota/limit window."""

    resource: str
    kind: str
    duration_seconds: int | None = None
    used_percent: int | None = None
    remaining_percent: int | None = None
    resets_at: str | None = None
    window_id: str | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("resource", "kind")
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "duration_seconds",
        "used_percent",
        "remaining_percent",
        "resets_at",
        "provider_metadata",
    )

    def __post_init__(self) -> None:
        _ = _v_enum(self.resource, _RESOURCE_VALUES, "window.resource")
        kind = _v_enum(self.kind, _KIND_VALUES, "window.kind")

        duration = self.duration_seconds
        if duration is not None:
            dur = _v_int(duration, "window.duration_seconds", lo=1)
            fixed = _KIND_DURATION.get(kind)
            if fixed is not None and dur != fixed:
                raise CapacityValidationError(
                    f"window: kind={kind!r} requires duration_seconds={fixed}, got {dur}"
                )

        _v_pct_pair(self.used_percent, self.remaining_percent, "window")

        if self.resets_at is not None:
            _ = _v_ts(self.resets_at, "window.resets_at")

        if self.window_id is not None:
            _ = _v_safe_id(self.window_id, "window.window_id")

    @classmethod
    def from_dict(cls, d: object) -> "CapacityWindow":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "window")
        # provider_metadata has a fixed v1 shape: exactly the key "window_id".
        window_id: str | None = None
        pm = dd.get("provider_metadata")
        if pm is not None:
            pm_map = _as_str_object_mapping(pm)
            if pm_map is None or set(pm_map.keys()) != {"window_id"}:
                raise CapacityValidationError(
                    "window.provider_metadata: must be a dict with exactly key "
                    + "'window_id'"
                )
            window_id = _v_safe_id(
                pm_map["window_id"], "window.provider_metadata.window_id"
            )
        return cls(
            resource=_v_enum(dd["resource"], _RESOURCE_VALUES, "window.resource"),
            kind=_v_enum(dd["kind"], _KIND_VALUES, "window.kind"),
            duration_seconds=_v_opt_int(dd.get("duration_seconds"), "window.duration_seconds", lo=1),
            used_percent=_v_opt_int(dd.get("used_percent"), "window.used_percent", lo=0, hi=100),
            remaining_percent=_v_opt_int(dd.get("remaining_percent"), "window.remaining_percent", lo=0, hi=100),
            resets_at=_v_opt_ts(dd.get("resets_at"), "window.resets_at"),
            window_id=window_id,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "resource": self.resource,
            "kind": self.kind,
        }
        if self.duration_seconds is not None:
            out["duration_seconds"] = self.duration_seconds
        if self.used_percent is not None:
            out["used_percent"] = self.used_percent
        if self.remaining_percent is not None:
            out["remaining_percent"] = self.remaining_percent
        if self.resets_at is not None:
            out["resets_at"] = self.resets_at
        if self.window_id is not None:
            out["provider_metadata"] = {"window_id": self.window_id}
        return out


@dataclass(frozen=True)
class LocalRuntime:
    """Local runtime reachability and model facts (no quota semantics)."""

    reachable: bool
    model_presence: str
    model_name: str | None = None
    configured_context_tokens: int | None = None
    effective_context_tokens: int | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = ("reachable", "model_presence")
    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "model_name",
        "configured_context_tokens",
        "effective_context_tokens",
    )

    def __post_init__(self) -> None:
        reachable = _v_bool(self.reachable, "local_runtime.reachable")
        model_presence = _v_enum(
            self.model_presence,
            _MODEL_PRESENCE_VALUES,
            "local_runtime.model_presence",
        )
        if self.model_name is not None:
            _ = _v_safe_id(self.model_name, "local_runtime.model_name")
        if self.configured_context_tokens is not None:
            _ = _v_int(self.configured_context_tokens, "local_runtime.configured_context_tokens", lo=1)
        if self.effective_context_tokens is not None:
            _ = _v_int(self.effective_context_tokens, "local_runtime.effective_context_tokens", lo=1)

        if model_presence in ("present", "missing") and self.model_name is None:
            raise CapacityValidationError(
                "local_runtime: model_name is required when model_presence is "
                + "'present' or 'missing'"
            )
        if not reachable and model_presence != "unknown":
            raise CapacityValidationError(
                "local_runtime: model_presence must be 'unknown' when "
                + "reachable is False"
            )

    @classmethod
    def from_dict(cls, d: object) -> "LocalRuntime":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "local_runtime")
        return cls(
            reachable=_v_bool(dd["reachable"], "local_runtime.reachable"),
            model_presence=_v_enum(
                dd["model_presence"],
                _MODEL_PRESENCE_VALUES,
                "local_runtime.model_presence",
            ),
            model_name=_v_opt_safe_id(dd.get("model_name"), "local_runtime.model_name"),
            configured_context_tokens=_v_opt_int(
                dd.get("configured_context_tokens"),
                "local_runtime.configured_context_tokens",
                lo=1,
            ),
            effective_context_tokens=_v_opt_int(
                dd.get("effective_context_tokens"),
                "local_runtime.effective_context_tokens",
                lo=1,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "reachable": self.reachable,
            "model_presence": self.model_presence,
        }
        if self.model_name is not None:
            out["model_name"] = self.model_name
        if self.configured_context_tokens is not None:
            out["configured_context_tokens"] = self.configured_context_tokens
        if self.effective_context_tokens is not None:
            out["effective_context_tokens"] = self.effective_context_tokens
        return out


@dataclass(frozen=True)
class CapacitySnapshot:
    """One normalized capacity observation (v1)."""

    schema_version: int
    provider: str
    source: str
    retrieved_at: str
    status: str
    windows: tuple[CapacityWindow, ...]
    diagnostics: tuple[CapacityDiagnostic, ...]
    plan: str | None = None
    local_runtime: LocalRuntime | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "provider",
        "source",
        "retrieved_at",
        "status",
        "windows",
        "diagnostics",
    )
    _OPTIONAL: ClassVar[tuple[str, ...]] = ("plan", "local_runtime")

    def __post_init__(self) -> None:
        sv = _v_int(self.schema_version, "schema_version")
        if sv != SCHEMA_VERSION:
            raise CapacityValidationError(
                f"schema_version: expected int {SCHEMA_VERSION}, got {sv!r}"
            )

        _ = _v_safe_id(self.provider, "provider")
        _ = _v_safe_id(self.source, "source")
        _ = _v_ts(self.retrieved_at, "retrieved_at")
        status = _v_enum(self.status, _STATUS_VALUES, "status")
        if self.plan is not None:
            _ = _v_safe_id(self.plan, "plan")

        windows = self.windows
        _ = _v_tuple_of(self.windows, CapacityWindow, "snapshot.windows")

        diagnostics = self.diagnostics
        _ = _v_tuple_of(self.diagnostics, CapacityDiagnostic, "snapshot.diagnostics")
        codes: set[str] = set()
        for diag in diagnostics:
            codes.add(diag.code)

        if self.local_runtime is not None:
            _ = _v_instance_of(
                self.local_runtime, LocalRuntime, "snapshot.local_runtime"
            )

        # Cross-field invariants.
        if status in _STATUSES_WITHOUT_WINDOWS and windows:
            raise CapacityValidationError(
                f"snapshot: status={status!r} requires an empty windows array, "
                + f"got {len(windows)} window(s)"
            )
        if status != "ok":
            required_code = _STATUS_REQUIRED_CODE.get(status)
            if required_code is not None and required_code not in codes:
                raise CapacityValidationError(
                    f"snapshot: status={status!r} requires diagnostic code "
                    + f"{required_code!r} in diagnostics"
                )

    @classmethod
    def from_dict(cls, d: object) -> "CapacitySnapshot":
        dd = _v_exact_shape(d, cls._REQUIRED, cls._OPTIONAL, "snapshot")

        raw_windows = dd["windows"]
        if not isinstance(raw_windows, list):
            raise CapacityValidationError(
                f"snapshot.windows: expected list, got {type(raw_windows).__name__}"
            )
        windows = tuple(
            CapacityWindow.from_dict(w) for w in cast("list[object]", raw_windows)
        )

        raw_diagnostics = dd["diagnostics"]
        if not isinstance(raw_diagnostics, list):
            raise CapacityValidationError(
                f"snapshot.diagnostics: expected list, got {type(raw_diagnostics).__name__}"
            )
        diagnostics = tuple(
            CapacityDiagnostic.from_dict(x)
            for x in cast("list[object]", raw_diagnostics)
        )

        local_runtime: LocalRuntime | None = None
        raw_lr = dd.get("local_runtime")
        if raw_lr is not None:
            local_runtime = LocalRuntime.from_dict(raw_lr)

        return cls(
            schema_version=_v_int(dd["schema_version"], "schema_version"),
            provider=_v_safe_id(dd["provider"], "provider"),
            source=_v_safe_id(dd["source"], "source"),
            retrieved_at=_v_ts(dd["retrieved_at"], "retrieved_at"),
            status=_v_enum(dd["status"], _STATUS_VALUES, "status"),
            windows=windows,
            diagnostics=diagnostics,
            plan=_v_opt_safe_id(dd.get("plan"), "plan"),
            local_runtime=local_runtime,
        )

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        out["schema_version"] = self.schema_version
        out["provider"] = self.provider
        out["source"] = self.source
        if self.plan is not None:
            out["plan"] = self.plan
        out["retrieved_at"] = self.retrieved_at
        out["status"] = self.status
        out["windows"] = [w.to_dict() for w in self.windows]
        if self.local_runtime is not None:
            out["local_runtime"] = self.local_runtime.to_dict()
        out["diagnostics"] = [dg.to_dict() for dg in self.diagnostics]
        return out

    def validate(self) -> "CapacitySnapshot":
        """Re-validate an already-constructed snapshot; returns self or raises."""
        return CapacitySnapshot.from_dict(self.to_dict())
