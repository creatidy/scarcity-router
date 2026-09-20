"""Worker-side local adapter seam: the allowlisted execution edge (M05).

This is the ONLY path by which a server-dispatched execution reaches a
local resource. Its invariants are absolute (D-044, issue #90):

- **Allowlist enforcement is worker-side and unconditional.** A local
  adapter runs only when its adapter id is registered in THIS worker's
  :class:`LocalAdapterRegistry` — a registry the worker's own
  configuration built. Even a compromised or misconfigured server
  cannot cause any other behavior: the execute message's ``adapter_id``
  is matched against the registry, and a miss is a typed rejection
  (``adapter_not_allowed``) reported to the server, with the local
  adapter never invoked. The server can never supply an executable
  path, a shell command, environment variables, a filesystem root, a
  repository mount or subprocess flags: no such field exists anywhere
  in the protocol schema, and the strict parser rejects extra keys.
- **Typed messages only.** Local adapters consume the same normalized
  M03 vocabulary the coordinator speaks — an
  :class:`~scarcity_router.gateway_adapters.AdapterCall` in, an
  :class:`~scarcity_router.gateway_adapters.AdapterResult` plus
  :class:`~scarcity_router.gateway_adapters.AdapterStreamChunk` values
  out — so server coordinator and worker share one execution contract
  with no second message format.
- **Loopback-only transport.** The production :class:`LoopbackOllamaAdapter`
  performs the thin transport invocation for a localhost OpenAI-compatible
  endpoint (the bounded D-044 plain-HTTP localhost exception; no TLS is
  terminated or bypassed, no credential is attached). Its OpenAI-compat-
  ible translation semantics live behind the replaceable
  :class:`~scarcity_router.worker_local_translation.LoopbackTranslation`
  seam — the M04 shared translation core slots in there (see the
  translation module's cross-workstream note). This module owns the
  transport invocation only, never a second Ollama semantic
  implementation.

Isolation posture: a local adapter receives no filesystem root, no
environment variables, no shell and no client-supplied flags; it can
only do what its own compiled-in implementation does with the typed
call. No root/Administrator execution, no Docker socket, no arbitrary
mounts — structurally absent, not merely forbidden.
"""

from __future__ import annotations

import http.client
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Protocol

from .capacity import CapacityDiagnostic, CapacitySnapshot
from .gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterResult,
    AdapterStreamChunk,
    AdapterToolCall,
    CallObservation,
    CHUNK_FINISH,
    CHUNK_TOOL_CALL,
    CHUNK_USAGE,
)
from .gateway_contracts import UsageTokens
from .gateway_validation import v_safe_id
from .resource_state import (
    ResourceIdentity,
    ResourceStateSnapshot,
    resource_snapshot_from_capacity,
)
from .worker_protocol import WorkerProtocolError
from .worker_local_translation import (
    LoopbackHTTPRequest,
    LoopbackHTTPResponse,
    LoopbackTranslation,
    ProvisionalOpenAITranslation,
)

# The id of the worker-side adapter for loopback Ollama-style endpoints.
OLLAMA_ADAPTER_ID = "ollama"

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_MAX_PROBE_BODY_BYTES = 65_536


class LocalAdapter(Protocol):
    """One allowlisted local execution surface.

    ``adapter_id`` is the allowlist key; ``resource_ids`` names the
    executable resources this adapter observes and serves (worker-local
    configuration matching the server registry's resource ids).
    ``invoke`` runs one typed call to completion, emitting normalized
    stream chunks in order, honoring the cancel event and the absolute
    deadline, and returning the normalized result. ``resource_snapshots``
    produces the safe normalized observations the worker reports through
    the M01 registry path.
    """

    adapter_id: str
    resource_ids: tuple[str, ...]

    def invoke(
        self,
        call: AdapterCall,
        *,
        cancel_event: threading.Event,
        deadline: str,
        emit: Callable[[AdapterStreamChunk], None],
    ) -> AdapterResult: ...

    def resource_snapshots(self, observed_at: str) -> tuple[ResourceStateSnapshot, ...]: ...


class LocalAdapterRegistry:
    """The worker's allowlist: ONLY registered adapter ids can ever run.

    The registry is built from the worker's own local configuration at
    startup. There is no runtime mutation path: discovery, registration
    and allowlist expansion are operator actions on the worker host, not
    protocol operations — ``LocalAdapterRegistry`` exposes no method a
    server message could reach that adds an adapter.
    """

    def __init__(self, adapters: Iterable[LocalAdapter] = ()) -> None:
        self._adapters: dict[str, LocalAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: LocalAdapter) -> None:
        adapter_id = v_safe_id(adapter.adapter_id, "local_adapter.adapter_id")
        if adapter_id in self._adapters:
            raise ValueError(f"local_adapter_registry: {adapter_id!r} is already registered")
        self._adapters[adapter_id] = adapter

    def resolve(self, adapter_id: str) -> LocalAdapter | None:
        """The allowlisted adapter for ``adapter_id``, or ``None``."""
        return self._adapters.get(adapter_id)

    def adapter_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def is_allowed(self, adapter_id: str) -> bool:
        return adapter_id in self._adapters


class AdapterNotAllowedError(Exception):
    """The requested adapter id is not on this worker's allowlist.

    The local adapter was NOT invoked; the worker reports the typed
    rejection to the server as a definitive failure.
    """

    def __init__(self, adapter_id: str) -> None:
        super().__init__(f"adapter {adapter_id!r} is not on this worker's allowlist")
        self.adapter_id: str = adapter_id


def run_allowlisted(
    registry: LocalAdapterRegistry,
    *,
    adapter_id: str,
    call: AdapterCall,
    cancel_event: threading.Event,
    deadline: str,
    emit: Callable[[AdapterStreamChunk], None],
) -> AdapterResult:
    """Run one call through the allowlist gate — the ONLY invoke path.

    This function is the worker's allowlist enforcement point: an
    adapter id missing from the registry raises
    :class:`AdapterNotAllowedError` before any adapter code runs, no
    matter what the server asked for.
    """
    checked_id = v_safe_id(adapter_id, "execute.adapter_id")
    adapter = registry.resolve(checked_id)
    if adapter is None:
        raise AdapterNotAllowedError(checked_id)
    return adapter.invoke(
        call, cancel_event=cancel_event, deadline=deadline, emit=emit
    )


# ── Loopback Ollama adapter (thin transport; translation is a seam) ───────────

LoopbackTransport = Callable[[LoopbackHTTPRequest], LoopbackHTTPResponse]


class LoopbackOllamaAdapter:
    """Local adapter for a localhost OpenAI-compatible inference endpoint.

    Thin transport invocation only: build the request through the
    injected :class:`LoopbackTranslation` (the M04 shared core slots in
    here), deliver it over loopback HTTP with
    :meth:`http.client.HTTPConnection`, parse the response through the
    same translation. Defaults are the clearly-marked provisional
    translation (see ``worker_local_translation``). The endpoint host is
    validated against the loopback allowlist at construction — this
    adapter cannot be pointed at a remote origin.
    """

    adapter_id: str = OLLAMA_ADAPTER_ID

    def __init__(
        self,
        *,
        resource: ResourceIdentity,
        host: str = "127.0.0.1",
        port: int = 11434,
        translation: LoopbackTranslation | None = None,
        transport: LoopbackTransport | None = None,
        connection_timeout_seconds: float = 5.0,
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"loopback_adapter: host {host!r} is not a loopback address; "
                + "local adapters reach localhost-only endpoints"
            )
        self.resource_ids: tuple[str, ...] = (resource.resource_id,)
        self._resource: ResourceIdentity = resource
        self._host: str = host
        self._port: int = port
        self._translation: LoopbackTranslation = (
            translation if translation is not None else ProvisionalOpenAITranslation()
        )
        self._transport: LoopbackTransport = (
            transport if transport is not None else self._default_transport
        )
        self._timeout: float = connection_timeout_seconds

    def invoke(
        self,
        call: AdapterCall,
        *,
        cancel_event: threading.Event,
        deadline: str,
        emit: Callable[[AdapterStreamChunk], None],
    ) -> AdapterResult:
        _ = deadline
        started = _canonical_now()
        try:
            if call.stream:
                return self._invoke_streaming(call, cancel_event=cancel_event, emit=emit)
            return self._invoke_whole(call)
        except WorkerProtocolError as exc:
            return AdapterResult(
                status="failed",
                calls=(
                    CallObservation(
                        call_index=0,
                        started_at=started,
                        ended_at=_canonical_now(),
                        status="failed",
                        note=exc.message[:200],
                    ),
                ),
            )

    def _invoke_whole(self, call: AdapterCall) -> AdapterResult:
        started = _canonical_now()
        request = self._translation.build_chat_request(call)
        response = self._transport(request)
        message, finish_reason, usage = self._translation.parse_response(response)
        ended = _canonical_now()
        return AdapterResult(
            status="completed",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at=started,
                    ended_at=ended,
                    status="completed",
                    provider_reported_usage=usage,
                ),
            ),
            message=message,
            finish_reason=finish_reason,
        )

    def _invoke_streaming(
        self,
        call: AdapterCall,
        *,
        cancel_event: threading.Event,
        emit: Callable[[AdapterStreamChunk], None],
    ) -> AdapterResult:
        started = _canonical_now()
        request = self._translation.build_stream_request(call)
        response = self._transport(request)
        if response.status != 200:
            raise WorkerProtocolError(
                "internal_error",
                f"the loopback endpoint answered HTTP {response.status}",
            )
        usage: UsageTokens | None = None
        text_parts: list[str] = []
        tool_calls: list[AdapterToolCall] = []
        finish_reason: str | None = None
        for line in response.text.splitlines():
            if cancel_event.is_set():
                return AdapterResult(
                    status="cancelled",
                    calls=(
                        CallObservation(
                            call_index=0,
                            started_at=started,
                            ended_at=_canonical_now(),
                            status="cancelled",
                        ),
                    ),
                )
            chunk = self._translation.parse_stream_line(line)
            if chunk is None:
                continue
            if chunk.kind == CHUNK_USAGE and chunk.usage is not None:
                usage = chunk.usage
                continue
            if chunk.kind == CHUNK_FINISH:
                finish_reason = chunk.finish_reason
                emit(chunk)
                continue
            if chunk.kind == CHUNK_TOOL_CALL and chunk.tool_call is not None:
                tool_calls.append(chunk.tool_call)
            elif chunk.kind == "text_delta" and chunk.text is not None:
                text_parts.append(chunk.text)
            emit(chunk)
        if finish_reason is None:
            # The stream ended without a terminal frame: fail closed
            # rather than fabricate a finish reason.
            raise WorkerProtocolError(
                "internal_error", "the loopback stream ended without a finish frame"
            )
        ended = _canonical_now()
        return AdapterResult(
            status="completed",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at=started,
                    ended_at=ended,
                    status="completed",
                    provider_reported_usage=usage,
                ),
            ),
            message=AdapterMessage(
                role="assistant",
                content="".join(text_parts) or None,
                tool_calls=tuple(tool_calls),
            ),
            finish_reason=finish_reason,
        )

    def resource_snapshots(self, observed_at: str) -> tuple[ResourceStateSnapshot, ...]:
        """One safe observation: reachable/not, never fabricated telemetry.

        The probe never carries prompt content; quota facts are absent
        because a local inference endpoint exposes no subscription quota —
        unknown stays unknown (fail closed, never guessed as zero or full).
        """
        reachable = self._probe()
        snapshot = CapacitySnapshot(
            schema_version=3,
            provider=self._resource.provider,
            source=f"worker-local:{OLLAMA_ADAPTER_ID}",
            retrieved_at=observed_at,
            status="ok" if reachable else "unavailable",
            windows=(),
            diagnostics=(
                ()
                if reachable
                else (CapacityDiagnostic(code="source_unavailable"),)
            ),
        )
        return (
            resource_snapshot_from_capacity(
                snapshot,
                identity=self._resource,
                quota_observation_class="local_limit",
            ),
        )

    def _probe(self) -> bool:
        try:
            response = self._transport(self._translation.build_probe_request())
        except (OSError, WorkerProtocolError):
            return False
        return response.status == 200 and len(response.body) <= _MAX_PROBE_BODY_BYTES

    def _default_transport(self, request: LoopbackHTTPRequest) -> LoopbackHTTPResponse:
        """The real loopback HTTP delivery (bounded localhost exception)."""
        connection = http.client.HTTPConnection(
            self._host, self._port, timeout=self._timeout
        )
        try:
            headers = {"Content-Type": "application/json"} if request.body else {}
            connection.request(
                request.method, request.path, body=request.body, headers=headers
            )
            raw = connection.getresponse()
            body = raw.read()
            return LoopbackHTTPResponse(
                status=raw.status, body=body, content_type=raw.getheader("Content-Type") or ""
            )
        finally:
            connection.close()


def _canonical_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "OLLAMA_ADAPTER_ID",
    "AdapterNotAllowedError",
    "LocalAdapter",
    "LocalAdapterRegistry",
    "LoopbackOllamaAdapter",
    "LoopbackTransport",
    "run_allowlisted",
]
