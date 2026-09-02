"""V1 capacity model for Scarcity Router.

Pure, provider-independent, standard-library only.

Implements the frozen v1 normalized capacity contract from docs/capacity-model.md:
- snapshot, status, windows, local runtime, diagnostics
- validation of all v1 invariants
- deterministic JSON-compatible serialization
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

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

# ── Validators ────────────────────────────────────────────────────────────────

_CANONICAL_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


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
            "must match [a-z0-9][a-z0-9._:-]{0,63} (lowercase, max 64 chars)"
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
            "expected YYYY-MM-DDTHH:MM:SS.sssZ"
        )
    try:
        datetime.fromisoformat(s[:-1] + "+00:00")
    except (ValueError, TypeError):
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
            "or both absent"
        )
    if used is not None:
        u = _v_int(used, f"{ctx}.used_percent", lo=0, hi=100)
        r = _v_int(remain, f"{ctx}.remaining_percent", lo=0, hi=100)
        if u + r != 100:
            raise CapacityValidationError(
                f"{ctx}: used_percent {u} + remaining_percent {r} != 100"
            )


def _v_exact_keys(
    obj: object,
    allowed: frozenset[str],
    label: str,
) -> None:
    if not isinstance(obj, dict):
        raise CapacityValidationError(
            f"{label}: expected dict, got {type(obj).__name__}"
        )
    extra = set(obj.keys()) - allowed
    if extra:
        raise CapacityValidationError(f"{label}: unknown keys {sorted(extra)}")


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CapacityDiagnostic:
    """One allowlisted diagnostic record."""

    code: str
    window_id: str | None = None

    _ALL_KEYS = frozenset({"code", "window_id"})

    @classmethod
    def from_dict(cls, d: object) -> "CapacityDiagnostic":
        _v_exact_keys(d, cls._ALL_KEYS, "diagnostic")
        dd = d  # guaranteed dict by above
        code = _v_enum(dd["code"], _DIAGNOSTIC_CODES, "diagnostic.code")
        wid = dd.get("window_id")
        if code not in _WINDOW_SCOPED_CODES:
            if wid is not None:
                raise CapacityValidationError(
                    f"diagnostic: code {code!r} is not window-scoped and "
                    "must not carry window_id"
                )
            wid = None
        elif wid is not None:
            wid = _v_safe_id(wid, "diagnostic.window_id")
        return cls(code=code, window_id=wid)

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

    _ALL_KEYS = frozenset({
        "resource",
        "kind",
        "duration_seconds",
        "used_percent",
        "remaining_percent",
        "resets_at",
        "provider_metadata",
    })

    @classmethod
    def from_dict(cls, d: object) -> "CapacityWindow":
        _v_exact_keys(d, cls._ALL_KEYS, "window")
        dd = d  # guaranteed dict
        resource = _v_enum(dd["resource"], _RESOURCE_VALUES, "window.resource")
        kind = _v_enum(dd["kind"], _KIND_VALUES, "window.kind")

        duration = dd.get("duration_seconds")
        if duration is not None:
            duration = _v_int(duration, "window.duration_seconds", lo=1)
            fixed = _KIND_DURATION.get(kind)
            if fixed is not None and duration != fixed:
                raise CapacityValidationError(
                    f"window: kind={kind!r} requires duration_seconds={fixed}, "
                    f"got {duration}"
                )

        used = dd.get("used_percent")
        remain = dd.get("remaining_percent")
        _v_pct_pair(used, remain, "window")

        resets_at = dd.get("resets_at")
        if resets_at is not None:
            resets_at = _v_ts(resets_at, "window.resets_at")

        window_id = None
        pm = dd.get("provider_metadata")
        if pm is not None:
            if not isinstance(pm, dict) or set(pm.keys()) != {"window_id"}:
                raise CapacityValidationError(
                    "window.provider_metadata: must be a dict with exactly key "
                    "'window_id'"
                )
            window_id = _v_safe_id(pm["window_id"], "window.provider_metadata.window_id")

        return cls(
            resource=resource,
            kind=kind,
            duration_seconds=duration,
            used_percent=used,
            remaining_percent=remain,
            resets_at=resets_at,
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

    _ALL_KEYS = frozenset({
        "reachable",
        "model_presence",
        "model_name",
        "configured_context_tokens",
        "effective_context_tokens",
    })

    @classmethod
    def from_dict(cls, d: object) -> "LocalRuntime":
        _v_exact_keys(d, cls._ALL_KEYS, "local_runtime")
        dd = d  # guaranteed dict
        reachable = _v_bool(dd["reachable"], "local_runtime.reachable")
        model_presence = _v_enum(
            dd["model_presence"],
            _MODEL_PRESENCE_VALUES,
            "local_runtime.model_presence",
        )
        model_name = dd.get("model_name")
        if model_name is not None:
            model_name = _v_safe_id(model_name, "local_runtime.model_name")
        configured = dd.get("configured_context_tokens")
        if configured is not None:
            configured = _v_int(
                configured,
                "local_runtime.configured_context_tokens",
                lo=1,
            )
        effective = dd.get("effective_context_tokens")
        if effective is not None:
            effective = _v_int(
                effective,
                "local_runtime.effective_context_tokens",
                lo=1,
            )

        if model_presence in ("present", "missing") and model_name is None:
            raise CapacityValidationError(
                "local_runtime: model_name is required when model_presence is "
                "'present' or 'missing'"
            )
        if not reachable and model_presence != "unknown":
            raise CapacityValidationError(
                "local_runtime: model_presence must be 'unknown' when "
                "reachable is False"
            )
        return cls(
            reachable=reachable,
            model_presence=model_presence,
            model_name=model_name,
            configured_context_tokens=configured,
            effective_context_tokens=effective,
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

    _ALL_KEYS = frozenset({
        "schema_version",
        "provider",
        "source",
        "plan",
        "retrieved_at",
        "status",
        "windows",
        "local_runtime",
        "diagnostics",
    })

    @classmethod
    def from_dict(cls, d: object) -> "CapacitySnapshot":
        _v_exact_keys(d, cls._ALL_KEYS, "snapshot")
        dd = d  # guaranteed dict

        sv = dd["schema_version"]
        if isinstance(sv, bool) or not isinstance(sv, int) or sv != SCHEMA_VERSION:
            raise CapacityValidationError(
                f"schema_version: expected int {SCHEMA_VERSION}, got {sv!r}"
            )

        provider = _v_safe_id(dd["provider"], "provider")
        source = _v_safe_id(dd["source"], "source")
        retrieved_at = _v_ts(dd["retrieved_at"], "retrieved_at")
        status = _v_enum(dd["status"], _STATUS_VALUES, "status")

        raw_windows = dd["windows"]
        if not isinstance(raw_windows, list):
            raise CapacityValidationError(
                f"snapshot.windows: expected list, got {type(raw_windows).__name__}"
            )
        windows = tuple(CapacityWindow.from_dict(w) for w in raw_windows)

        raw_diagnostics = dd["diagnostics"]
        if not isinstance(raw_diagnostics, list):
            raise CapacityValidationError(
                f"snapshot.diagnostics: expected list, got "
                f"{type(raw_diagnostics).__name__}"
            )
        diagnostics = tuple(
            CapacityDiagnostic.from_dict(x) for x in raw_diagnostics
        )

        plan = dd.get("plan")
        if plan is not None:
            plan = _v_safe_id(plan, "plan")

        local_runtime = None
        raw_lr = dd.get("local_runtime")
        if raw_lr is not None:
            local_runtime = LocalRuntime.from_dict(raw_lr)

        # Cross-field invariants
        if status in _STATUSES_WITHOUT_WINDOWS and windows:
            raise CapacityValidationError(
                f"snapshot: status={status!r} requires an empty windows array, "
                f"got {len(windows)} window(s)"
            )
        if status != "ok":
            required_code = _STATUS_REQUIRED_CODE.get(status)
            if required_code is not None:
                codes = {diag.code for diag in diagnostics}
                if required_code not in codes:
                    raise CapacityValidationError(
                        f"snapshot: status={status!r} requires diagnostic code "
                        f"{required_code!r} in diagnostics"
                    )

        return cls(
            schema_version=SCHEMA_VERSION,
            provider=provider,
            source=source,
            retrieved_at=retrieved_at,
            status=status,
            windows=windows,
            diagnostics=diagnostics,
            plan=plan,
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
