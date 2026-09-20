"""Shared construction validators for the execution-gateway modules (M03).

One internal-but-module-public home for the restricted-grammar validators
the gateway family uses, so every module validates identically without
reaching into another module's private helpers. The rules mirror the
resource-state and routing-core validators exactly (same safe-id grammar,
same canonical timestamp shape) — this module adds no new rule and owns no
semantics; it is the gateway family's shared typing discipline only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import TypeVar, cast

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_CANONICAL_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

_T = TypeVar("_T")


def v_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected str, got {type(value).__name__}")
    return value


def v_safe_id(value: object, field: str) -> str:
    s = v_str(value, field)
    if not _SAFE_ID_RE.match(s):
        raise ValueError(
            f"{field}: unsafe identifier {s!r}; "
            + "must match [a-z0-9][a-z0-9._:-]{0,63} (lowercase, max 64 chars)"
        )
    return s


def v_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field}: expected bool, got {type(value).__name__} ({value!r})")
    return value


def v_int(value: object, field: str, *, lo: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field}: expected int, got {type(value).__name__} ({value!r})")
    if lo is not None and value < lo:
        raise ValueError(f"{field}: value {value} < {lo}")
    return value


def v_opt_int(value: object | None, field: str, *, lo: int | None = None) -> int | None:
    return None if value is None else v_int(value, field, lo=lo)


def v_canonical_ts(value: object, field: str) -> str:
    s = v_str(value, field)
    if not _CANONICAL_TS_RE.match(s):
        raise ValueError(
            f"{field}: non-canonical timestamp {s!r}; "
            + "expected YYYY-MM-DDTHH:MM:SS.sssZ"
        )
    return s


def v_text(value: object, field: str, *, max_len: int) -> str:
    s = v_str(value, field)
    if not s:
        raise ValueError(f"{field}: must be non-empty")
    if len(s) > max_len:
        raise ValueError(f"{field}: exceeds maximum length {max_len}")
    return s


def v_enum(value: object, allowed: frozenset[str], field: str) -> str:
    s = v_str(value, field)
    if s not in allowed:
        raise ValueError(
            f"{field}: value {s!r} not in allowed set {sorted(allowed)}"
        )
    return s


def v_instance(value: object, cls: type[_T], label: str) -> _T:
    if not isinstance(value, cls):
        raise ValueError(
            f"{label}: expected a {cls.__name__}, got {type(value).__name__}"
        )
    return value


def v_str_object_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{field}: expected a mapping, got {type(value).__name__}"
        )
    return cast(Mapping[str, object], value)


def v_aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field}: expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}: expected a timezone-aware datetime")
    return value


def exact_shape(
    d: object,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    label: str,
) -> Mapping[str, object]:
    """Validate one serialized record's exact key set and narrow it."""
    m = v_str_object_mapping(d, label)
    allowed = frozenset(required) | frozenset(optional)
    extra = sorted(set(m.keys()) - allowed)
    if extra:
        raise ValueError(f"{label}: unknown keys {extra}")
    missing = [key for key in required if key not in m]
    if missing:
        raise ValueError(f"{label}: missing required keys {sorted(missing)}")
    return m


__all__ = [
    "exact_shape",
    "v_aware_datetime",
    "v_bool",
    "v_canonical_ts",
    "v_enum",
    "v_instance",
    "v_int",
    "v_opt_int",
    "v_safe_id",
    "v_str",
    "v_str_object_mapping",
    "v_text",
]
