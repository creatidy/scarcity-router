"""Versioned, allowlisted projection of internal producer snapshots.

The journal is an internal store, not an HTTP representation. All current and
historical snapshots pass through this same projection before exposure.
"""

from __future__ import annotations

import json
import re
from typing import cast

_NULLABLE = frozenset({
    "worker_connected", "observed_at", "inventory_observed_at", "health", "variant", "reason", "lifecycle",
    "duration_seconds", "used_percent", "remaining_percent", "resets_at",
})
_ENUMS = {
    "origin": {"configured", "source"},
    "freshness": {"fresh", "stale", "never_observed", "not_evaluated"},
    "inventory_freshness": {"unknown", "never_observed"},
    "health": {"ok", "unavailable", "auth_required", "schema_changed", "unsupported", "unknown"},
    "availability": {"unknown", "unavailable", "disabled"},
    "state": {"routable", "discovered", "retired", "not_in_latest_inventory"},
    "reason": {"model_not_allowed", "legacy_track_policy", "adoption_disabled", "awaiting_inventory_refresh",
               "plan_lane_unverified", "source_not_authenticated", "effort_not_reported"},
    "lifecycle": {"retained_missing", "retired"},
    "observation_class": {"direct_observation", "provider_telemetry", "estimate", "local_limit", "unknown"},
}


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid producer document")
    result = cast("dict[object, object]", value)
    if not all(isinstance(key, str) for key in result):
        raise ValueError("invalid producer document")
    return cast("dict[str, object]", result)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("invalid producer document")
    return cast("list[object]", value)


def _fields(value: object, names: tuple[str, ...]) -> dict[str, object]:
    data = _object(value)
    result: dict[str, object] = {}
    for name in names:
        if name not in data:
            raise ValueError("invalid producer document")
        item = data[name]
        if item is None and name not in _NULLABLE:
            raise ValueError("invalid producer document")
        if item is not None:
            if name in ("enabled", "worker_connected"):
                valid = type(item) is bool
            elif name in ("duration_seconds", "used_percent", "remaining_percent"):
                valid = type(item) is int and (item > 0 if name == "duration_seconds" else 0 <= item <= 100)
            elif name in ("observed_at", "inventory_observed_at", "resets_at"):
                valid = isinstance(item, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", item) is not None
            else:
                valid = isinstance(item, str) and re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,63}", item) is not None
                if valid and name in _ENUMS:
                    valid = item in _ENUMS[name]
            if not valid:
                raise ValueError("invalid producer document")
        result[name] = item
    return result


def project_snapshot(payload: str) -> dict[str, object]:
    """Project only approved v1 facts; never pass journal objects through."""
    try:
        document = _object(cast(object, json.loads(payload)))
        if type(document.get("schema_version")) is not int or document.get("schema_version") != 1 or document.get("mode") != "execution_gateway":
            raise ValueError("unsupported producer schema")
        resources: list[dict[str, object]] = []
        for raw in _list(document.get("resources")):
            item = _object(raw)
            resource = _fields(item, ("origin", "enabled", "worker_connected", "observed_at", "freshness", "health", "availability"))
            resource["identity"] = _fields(item.get("identity"), ("resource_id", "channel", "provider", "model", "variant", "entitlement"))
            quotas: list[dict[str, object]] = []
            for raw_quota in _list(item.get("quota_facts")):
                quota = _object(raw_quota)
                projected = _fields(quota, ("observation_class",))
                projected["window"] = _fields(quota.get("window"), (
                    "resource", "kind", "duration_seconds", "used_percent", "remaining_percent", "resets_at",
                ))
                quotas.append(projected)
            resource["quota_facts"] = quotas
            resources.append(resource)
        sources = [_fields(item, ("source_id", "worker_connected", "inventory_observed_at", "inventory_freshness"))
                   for item in _list(document.get("sources"))]
        discoveries = [_fields(item, ("source_id", "model", "state", "reason", "observed_at", "lifecycle"))
                       for item in _list(document.get("discoveries"))]
        return {"schema_version": 1, "mode": "execution_gateway", "resources": resources,
                "sources": sources, "discoveries": discoveries}
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("invalid producer document") from None
