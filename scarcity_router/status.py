"""Unified read-only status collection and presentation.

This module composes the existing provider collectors without interpreting
provider payloads. Each invocation creates one observation timestamp, calls the
collectors in a fixed order, and exposes either a compact human view or the
existing v1 snapshot dictionaries.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, TextIO, cast

from .capacity import CapacitySnapshot, CapacityWindow
from .providers.ollama_acquisition import (
    DEFAULT_ENDPOINT,
    canonical_local_endpoint,
    collect_ollama_capacity,
)
from .providers.openai_codex_acquisition import collect_openai_codex_capacity
from .providers.zai_acquisition import collect_zai_capacity

OLLAMA_MODEL_ENV = "SCARCITY_ROUTER_OLLAMA_MODEL"
OLLAMA_ENDPOINT_ENV = "SCARCITY_ROUTER_OLLAMA_ENDPOINT"
OLLAMA_CONTEXT_ENV = "SCARCITY_ROUTER_OLLAMA_CONTEXT_TOKENS"

_SAFE_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}")
_PROVIDER_ORDER = {"openai": 0, "zai": 1, "ollama": 2}
_WINDOW_KIND_ORDER = {"five_hour": 0, "weekly": 1, "unknown": 2}
_WINDOW_RESOURCE_ORDER = {"tokens": 0, "time": 1, "unknown": 2}


class StatusConfigurationError(ValueError):
    """Raised when status invocation configuration is missing or unsafe."""


class OpenAICapacityCollector(Protocol):
    def __call__(self, *, retrieved_at: str) -> CapacitySnapshot: ...


class ZaiCapacityCollector(Protocol):
    def __call__(self, *, retrieved_at: str) -> CapacitySnapshot: ...


class OllamaCapacityCollector(Protocol):
    def __call__(
        self,
        *,
        retrieved_at: str,
        model_name: str,
        endpoint: str,
        configured_context_tokens: int | None,
    ) -> CapacitySnapshot: ...


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class StatusCollectors:
    """Collector dependencies, with a seam for synthetic application tests."""

    openai: OpenAICapacityCollector = collect_openai_codex_capacity
    zai: ZaiCapacityCollector = collect_zai_capacity
    ollama: OllamaCapacityCollector = collect_ollama_capacity


@dataclass(frozen=True)
class OllamaConfiguration:
    """Validated, non-secret configuration for the local status probe."""

    model_name: str
    endpoint: str = DEFAULT_ENDPOINT
    configured_context_tokens: int | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def observation_timestamp(clock: Clock | None = None) -> str:
    """Return one canonical UTC millisecond timestamp for an invocation."""
    current = (clock or _utc_now)()
    if current.tzinfo is None:
        raise ValueError("observation clock must return a timezone-aware datetime")
    return (
        current.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _positive_context(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise StatusConfigurationError(
            "Ollama configured context must be a positive integer"
        ) from None
    if parsed <= 0:
        raise StatusConfigurationError(
            "Ollama configured context must be a positive integer"
        )
    return parsed


def _validate_model_name(model_name: object) -> str:
    if (
        not isinstance(model_name, str)
        or _SAFE_MODEL_RE.fullmatch(model_name) is None
        or not model_name.isprintable()
    ):
        raise StatusConfigurationError(
            "Ollama model must be a safe non-empty model identifier"
        )
    return model_name


def validate_ollama_configuration(
    *,
    model_name: object,
    endpoint: object = DEFAULT_ENDPOINT,
    configured_context_tokens: object | None = None,
) -> OllamaConfiguration:
    """Validate the small Ollama configuration boundary before collection."""
    safe_model = _validate_model_name(model_name)
    if not isinstance(endpoint, str):
        raise StatusConfigurationError(
            "Ollama endpoint must be a numeric-loopback HTTP endpoint"
        )
    try:
        safe_endpoint = canonical_local_endpoint(endpoint)
    except ValueError:
        raise StatusConfigurationError(
            "Ollama endpoint must be a numeric-loopback HTTP endpoint"
        ) from None

    if configured_context_tokens is not None and (
        isinstance(configured_context_tokens, bool)
        or not isinstance(configured_context_tokens, int)
        or configured_context_tokens <= 0
    ):
        raise StatusConfigurationError(
            "Ollama configured context must be a positive integer"
        )
    return OllamaConfiguration(
        model_name=safe_model,
        endpoint=safe_endpoint,
        configured_context_tokens=configured_context_tokens,
    )


def resolve_ollama_configuration(
    *,
    model_name: str | None,
    endpoint: str | None,
    configured_context_tokens: int | None,
    environment: Mapping[str, str] | None = None,
) -> OllamaConfiguration:
    """Resolve CLI-over-environment Ollama settings without persisting them."""
    environ = os.environ if environment is None else environment
    selected_model = (
        model_name if model_name is not None else environ.get(OLLAMA_MODEL_ENV)
    )
    if selected_model is None or not selected_model:
        raise StatusConfigurationError(
            "Ollama model is not configured; use --ollama-model or "
            + f"{OLLAMA_MODEL_ENV}"
        )

    selected_endpoint = (
        endpoint
        if endpoint is not None
        else environ.get(OLLAMA_ENDPOINT_ENV, DEFAULT_ENDPOINT)
    )

    selected_context: int | None = configured_context_tokens
    if selected_context is None:
        context_text = environ.get(OLLAMA_CONTEXT_ENV)
        if context_text is not None:
            selected_context = _positive_context(context_text)

    return validate_ollama_configuration(
        model_name=selected_model,
        endpoint=selected_endpoint,
        configured_context_tokens=selected_context,
    )


def collect_status(
    *,
    ollama: OllamaConfiguration,
    collectors: StatusCollectors | None = None,
    clock: Clock | None = None,
) -> tuple[CapacitySnapshot, CapacitySnapshot, CapacitySnapshot]:
    """Collect OpenAI, Z.ai and Ollama snapshots for one observation."""
    configuration = validate_ollama_configuration(
        model_name=ollama.model_name,
        endpoint=ollama.endpoint,
        configured_context_tokens=ollama.configured_context_tokens,
    )
    retrieved_at = observation_timestamp(clock)
    selected = StatusCollectors() if collectors is None else collectors
    return (
        selected.openai(retrieved_at=retrieved_at),
        selected.zai(retrieved_at=retrieved_at),
        selected.ollama(
            retrieved_at=retrieved_at,
            model_name=configuration.model_name,
            endpoint=configuration.endpoint,
            configured_context_tokens=configuration.configured_context_tokens,
        ),
    )


def _ordered_snapshots(
    snapshots: Sequence[CapacitySnapshot],
) -> list[CapacitySnapshot]:
    return sorted(
        snapshots,
        key=lambda snapshot: (
            _PROVIDER_ORDER.get(snapshot.provider, len(_PROVIDER_ORDER)),
            snapshot.provider,
        ),
    )


def _window_sort_key(window: CapacityWindow) -> tuple[object, ...]:
    return (
        _WINDOW_KIND_ORDER.get(window.kind, len(_WINDOW_KIND_ORDER)),
        _WINDOW_RESOURCE_ORDER.get(window.resource, len(_WINDOW_RESOURCE_ORDER)),
        window.window_id or "",
        window.duration_seconds if window.duration_seconds is not None else -1,
        window.resets_at or "",
        window.used_percent if window.used_percent is not None else -1,
        window.remaining_percent if window.remaining_percent is not None else -1,
    )


def _format_percentage(value: int | None) -> str:
    return "unknown" if value is None else f"{value}%"


def _format_window(window: CapacityWindow) -> str:
    fields = [
        f"kind={window.kind}",
        f"resource={window.resource}",
        f"used={_format_percentage(window.used_percent)}",
        f"remaining={_format_percentage(window.remaining_percent)}",
        f"reset={window.resets_at or 'unknown'}",
    ]
    if window.window_id is not None:
        fields.append(f"id={window.window_id}")
    return "  window " + " ".join(fields)


def _format_diagnostics(snapshot: CapacitySnapshot) -> str | None:
    if not snapshot.diagnostics:
        return None
    diagnostics = sorted(
        snapshot.diagnostics,
        key=lambda diagnostic: (diagnostic.code, diagnostic.window_id or ""),
    )
    values = [
        diagnostic.code
        + (f"[{diagnostic.window_id}]" if diagnostic.window_id is not None else "")
        for diagnostic in diagnostics
    ]
    return "  diagnostics=" + ",".join(values)


def render_human(snapshots: Sequence[CapacitySnapshot]) -> str:
    """Render snapshots using only safe normalized contract fields."""
    ordered = _ordered_snapshots(snapshots)
    if not ordered:
        raise ValueError("status requires at least one provider snapshot")
    lines = [f"Observed at {ordered[0].retrieved_at}"]
    for snapshot in ordered:
        header = [f"Provider {snapshot.provider}", f"status={snapshot.status}"]
        if snapshot.plan is not None:
            header.append(f"plan={snapshot.plan}")
        lines.append(" ".join(header))
        for window in sorted(snapshot.windows, key=_window_sort_key):
            lines.append(_format_window(window))
        if not snapshot.windows and snapshot.local_runtime is None:
            lines.append("  windows=none")
        runtime = snapshot.local_runtime
        if runtime is not None:
            lines.append(
                "  runtime"
                + f" reachable={'yes' if runtime.reachable else 'no'}"
                + f" presence={runtime.model_presence}"
                + f" model={runtime.model_name or 'unknown'}"
                + " configured_context="
                + (
                    str(runtime.configured_context_tokens)
                    if runtime.configured_context_tokens is not None
                    else "unknown"
                )
                + " effective_context="
                + (
                    str(runtime.effective_context_tokens)
                    if runtime.effective_context_tokens is not None
                    else "unknown"
                )
            )
        diagnostic_line = _format_diagnostics(snapshot)
        if diagnostic_line is not None:
            lines.append(diagnostic_line)
    return "\n".join(lines) + "\n"


def render_json(snapshots: Sequence[CapacitySnapshot]) -> str:
    """Render the ordered snapshots with their existing v1 serialization."""
    payload = [snapshot.to_dict() for snapshot in _ordered_snapshots(snapshots)]
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _arg_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _optional_string_argument(value: object, name: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise RuntimeError(f"parser produced an invalid {name} argument")


def _optional_context_argument(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise RuntimeError("parser produced an invalid Ollama context argument")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scarcity_router",
        description="Read-only normalized AI provider capacity status.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    status_parser = commands.add_parser(
        "status",
        help="collect current OpenAI, Z.ai and Ollama status",
        description="Collect read-only normalized status from all supported providers.",
    )
    _ = status_parser.add_argument(
        "--ollama-model",
        help=f"configured Ollama model (or {OLLAMA_MODEL_ENV})",
    )
    _ = status_parser.add_argument(
        "--ollama-endpoint",
        help=f"numeric-loopback Ollama endpoint (or {OLLAMA_ENDPOINT_ENV})",
    )
    _ = status_parser.add_argument(
        "--ollama-context-tokens",
        type=_arg_positive_int,
        help=f"known configured context size (or {OLLAMA_CONTEXT_ENV})",
    )
    _ = status_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the ordered normalized snapshot list as JSON",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    collectors: StatusCollectors | None = None,
    clock: Clock | None = None,
) -> int:
    """Run the provisional module CLI and return its process exit code."""
    parser = build_parser()
    arguments = cast(dict[str, object], vars(parser.parse_args(argv)))
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr

    try:
        configuration = resolve_ollama_configuration(
            model_name=_optional_string_argument(
                arguments.get("ollama_model"), "Ollama model"
            ),
            endpoint=_optional_string_argument(
                arguments.get("ollama_endpoint"), "Ollama endpoint"
            ),
            configured_context_tokens=_optional_context_argument(
                arguments.get("ollama_context_tokens")
            ),
            environment=environment,
        )
    except StatusConfigurationError as exc:
        print(f"configuration error: {exc}", file=errors)
        return 2

    snapshots = collect_status(
        ollama=configuration,
        collectors=collectors,
        clock=clock,
    )
    json_output = arguments.get("json")
    if not isinstance(json_output, bool):
        raise RuntimeError("parser produced an invalid JSON output argument")
    _ = output.write(render_json(snapshots) if json_output else render_human(snapshots))
    return 0


__all__ = [
    "OLLAMA_CONTEXT_ENV",
    "OLLAMA_ENDPOINT_ENV",
    "OLLAMA_MODEL_ENV",
    "OllamaConfiguration",
    "StatusCollectors",
    "StatusConfigurationError",
    "build_parser",
    "collect_status",
    "main",
    "observation_timestamp",
    "render_human",
    "render_json",
    "resolve_ollama_configuration",
    "validate_ollama_configuration",
]
