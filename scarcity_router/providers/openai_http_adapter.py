"""Generic OpenAI-compatible HTTP execution adapter (M04, issue #89).

The ONE production execution adapter for the ``server_direct_http``
channel (D-042): every preset (OpenAI API, DeepSeek, OpenRouter, Z.ai
Coding Plan, Ollama, generic administrator-configured endpoints) is a
configuration of this adapter — never a per-provider gateway. Provider
differences stop at this adapter edge through the typed
:class:`~scarcity_router.providers.openai_http_presets.ProviderPreset`
policies and the shared translation core
(:mod:`scarcity_router.providers.openai_http_core`); the coordinator is
never taught a provider protocol (D-041/D-043).

Per-dispatch flow (the coordinator's :class:`~scarcity_router.gateway_adapters.ExecutionContext`
bounds every step):

1. resolve the administrator-configured :class:`ResourceBinding` for the
   admitted ``resource_id`` (configuration never comes from request
   content, D-044);
2. build the wire request through the preset's translation policy — a
   feature the preset does not evidence is refused before any byte is
   sent (:class:`~scarcity_router.providers.openai_http_core.TranslationError`);
3. perform exactly ONE outbound HTTP request (no internal retry, no
   failover — the coordinator forbids both, D-043) over a transport with:
   verified TLS via ``ssl.create_default_context()`` (plain HTTP only for
   explicit loopback origins, the bounded D-044 exception), credentials
   bound to the configured exact origin, redirects REFUSED (never
   followed, so ``Authorization`` can never cross origins), bounded
   response size, per-read socket bounds under the dispatch deadline, the
   ``X-Scarcity-Router-Gateway`` router-loop marker stamped on every
   outbound request, and refusal of any response that carries the same
   marker (a Scarcity Router gateway must never serve as another
   router's backend, D-044);
4. normalize the response (non-streaming JSON or SSE stream) through the
   shared core, emitting normalized stream chunks via
   ``context.emit_chunk`` (provider tool-call deltas are ACCUMULATED —
   normalized ``tool_call`` chunks carry complete calls), observing
   ``context.cancel_event`` cooperatively and the ``context.deadline``
   absolutely;
5. report one :class:`~scarcity_router.gateway_adapters.CallObservation`
   with provider-reported usage kept separate (usage is never estimated
   here: absent usage is reported as unavailable, never fabricated).

Failure vocabulary (typed, per the adapter seam): definitive refusals and
protocol drift raise :class:`~scarcity_router.gateway_adapters.AdapterPermanentError`;
a connection failure before any response arrived raises
:class:`~scarcity_router.gateway_adapters.AdapterAmbiguousError` (the
coordinator refuses blind retries, D-043); a deadline/socket timeout
raises :class:`~scarcity_router.gateway_adapters.AdapterTimeoutError`;
client cancellation returns a ``cancelled`` result after the connection
is closed. No exception message ever contains a credential, a provider
payload excerpt or request content.

Ollama specifics: the ``ollama`` preset is the SAME adapter (one
inference implementation, never two). Model discovery
(``GET /api/tags``) and health probing (``GET /api/version``) are native,
read-only endpoints that never consume inference quota, exposed through
:meth:`OpenAICompatibleHttpAdapter.discover_models` and
:meth:`OpenAICompatibleHttpAdapter.probe_health`. A localhost-only Ollama
is reached through the worker (M05), which invokes the SAME translation
core worker-side — the transports differ, the semantics do not.

The worker-bridged channel (``worker_bridged``) is deliberately NOT
registered by this module: M05 owns that transport. The integration
expectation is that the worker invokes :func:`build_chat_completion_request`,
:class:`SseStreamParser`, :func:`interpret_stream_frame` and
:func:`parse_chat_completion_response` on the worker host and relays the
normalized chunks and call observations through the worker protocol.
"""

from __future__ import annotations

import http.client
import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from ..gateway_adapters import (
    CHUNK_FINISH,
    CHUNK_TEXT_DELTA,
    CHUNK_TOOL_CALL,
    CHUNK_USAGE,
    AdapterCall,
    AdapterAmbiguousError,
    AdapterMessage,
    AdapterPermanentError,
    AdapterResult,
    AdapterStreamChunk,
    AdapterTimeoutError,
    CallObservation,
    ExecutionContext,
)
from ..gateway_contracts import UsageTokens
from ..gateway_validation import v_safe_id, v_text
from .http_origin import ProviderCredential, ProviderOrigin
from .openai_http_core import (
    SseStreamParser,
    ToolCallAccumulator,
    TranslationError,
    build_chat_completion_request,
    interpret_stream_frame,
    parse_chat_completion_response,
    provider_error_note,
)
from .openai_http_presets import ProviderPreset

#: Router-loop protection marker (D-044, docs/execution-surface.md): the
#: same header the gateway server refuses on ingress. Outbound adapter
#: traffic always carries it, and a response carrying it back is refused.
GATEWAY_MARKER_HEADER = "X-Scarcity-Router-Gateway"
GATEWAY_MARKER_VALUE = "scarcity-router-gateway/1"

#: Stable adapter identity for the audit trail and the matrix columns.
ADAPTER_NAME = "openai-http"
ADAPTER_VERSION = "1.0.0"

#: Upper bound for one non-streaming provider response body.
MAX_RESPONSE_BODY_BYTES = 32 * 1024 * 1024

#: Upper bound for one streaming provider response, in total.
MAX_STREAM_TOTAL_BYTES = 64 * 1024 * 1024

#: Upper bound for one error-response body read (notes use code tokens
#: only, so a small prefix of the body is enough).
_MAX_ERROR_BODY_BYTES = 64 * 1024

#: Transport read granularity for streaming bodies.
_STREAM_READ_CHUNK = 8192

#: Upper bound for one blocking socket operation; the dispatch deadline
#: stays the absolute bound, this bounds a single hung read or connect.
_MAX_SOCKET_TIMEOUT_SECONDS = 120.0


def _canonical_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _canonical_now_plus(*, seconds: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── Administrator-configured binding ──────────────────────────────────────────


@dataclass(frozen=True)
class ResourceBinding:
    """One server-direct resource's administrator configuration (D-044).

    Binds a registry ``resource_id`` to a preset, an EXACT configured
    origin and the credential for that origin. This is the only place a
    provider URL or credential may ever come from; request content can
    never supply or alter any of it. ``wire_model`` optionally carries the
    provider's exact model string when the registry's safe-identifier
    grammar cannot represent it; otherwise the resource's physical model
    name is used verbatim.
    """

    resource_id: str
    preset: ProviderPreset
    origin: ProviderOrigin
    credential: ProviderCredential | None = None
    wire_model: str | None = None

    def __post_init__(self) -> None:
        _ = v_safe_id(self.resource_id, "resource_binding.resource_id")
        # Runtime shape guards (annotations are not enforced at runtime).
        if self.preset.__class__ is not ProviderPreset:
            raise ValueError("resource_binding.preset: expected a ProviderPreset")
        if self.origin.__class__ is not ProviderOrigin:
            raise ValueError("resource_binding.origin: expected a ProviderOrigin")
        if self.credential is not None and (
            self.credential.__class__ is not ProviderCredential
        ):
            raise ValueError(
                "resource_binding.credential: expected a ProviderCredential"
            )
        if self.preset.policy.requires_credential and self.credential is None:
            raise ValueError(
                f"resource_binding[{self.resource_id!r}]: preset "
                + f"{self.preset.preset_id!r} requires a credential bound to "
                + f"{self.origin.origin}"
            )
        if self.wire_model is not None:
            _ = v_text(self.wire_model, "resource_binding.wire_model", max_len=512)


# ── Discovery / health results (Ollama native endpoints) ─────────────────────


@dataclass(frozen=True)
class DiscoveryResult:
    """One model-discovery outcome (read-only, never a model download)."""

    status: str  # ok | unsupported_preset | unreachable | schema_drift | refused
    models: tuple[str, ...]
    note: str | None = None


@dataclass(frozen=True)
class HealthProbeResult:
    """One health-probe outcome (read-only, consumes no inference quota)."""

    status: str  # ok | unsupported_preset | unreachable | schema_drift | refused
    note: str | None = None


# ── The adapter ───────────────────────────────────────────────────────────────


class OpenAICompatibleHttpAdapter:
    """The generic OpenAI-compatible HTTP execution adapter (M04).

    One instance serves every configured server-direct resource; the
    admitted target's ``resource_id`` selects the binding. Implements the
    :class:`~scarcity_router.gateway_adapters.ExecutionAdapter` protocol
    for the ``server_direct_http`` channel exactly: synchronous execute on
    the caller's thread, deadline honored, cancellation observed, one
    normalized result with honest per-call observations.
    """

    channel: str = "server_direct_http"
    adapter_name: str = ADAPTER_NAME
    adapter_version: str = ADAPTER_VERSION
    _socket_timeout: float

    def __init__(
        self,
        bindings: Mapping[str, ResourceBinding] | None = None,
        *,
        socket_timeout_seconds: float = _MAX_SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        source: Mapping[str, ResourceBinding] = (
            {} if bindings is None else bindings
        )
        self._bindings: dict[str, ResourceBinding] = {}
        for resource_id, binding in source.items():
            _ = v_safe_id(resource_id, "adapter_bindings.resource_id")
            if binding.__class__ is not ResourceBinding:
                raise ValueError(
                    "adapter_bindings: expected ResourceBinding values"
                )
            if binding.resource_id != resource_id:
                raise ValueError(
                    "adapter_bindings: binding key must equal its resource_id"
                )
            self._bindings[resource_id] = binding
        if (
            socket_timeout_seconds.__class__ is bool
            or socket_timeout_seconds.__class__ not in (int, float)
            or not 0 < float(socket_timeout_seconds) <= _MAX_SOCKET_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "socket_timeout_seconds: must be a positive number of at "
                + f"most {_MAX_SOCKET_TIMEOUT_SECONDS}"
            )
        self._socket_timeout = float(socket_timeout_seconds)

    # ── Configuration surface (administrator-owned) ──────────────────────

    def binding(self, resource_id: str) -> ResourceBinding | None:
        return self._bindings.get(resource_id)

    @property
    def registered_resource_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))

    # ── The dispatch seam ─────────────────────────────────────────────────

    def execute(self, call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        binding = self._bindings.get(call.resource.resource_id)
        if binding is None:
            raise AdapterPermanentError(
                "no provider binding is configured for resource "
                + f"'{call.resource.resource_id}'"
            )
        started_at = _canonical_now()
        try:
            request_body = build_chat_completion_request(
                call,
                binding.preset.policy,
                wire_model=binding.wire_model,
            )
        except TranslationError as exc:
            raise AdapterPermanentError(str(exc)) from None
        payload = json.dumps(request_body, allow_nan=False).encode("utf-8")
        headers = self._headers(
            binding, "text/event-stream" if call.stream else "application/json"
        )
        connection = self._connect(binding, context)
        try:
            self._send(connection, binding, payload, headers)
            response = self._receive(connection)
            self._refuse_router_backends(response)
            if 300 <= response.status < 400:
                # Redirects are never followed; Authorization can therefore
                # never cross origins (D-044).
                raise AdapterPermanentError(
                    f"provider redirect refused (HTTP {response.status}); "
                    + "configured origins are exact and never rewritten"
                )
            if response.status != 200:
                raise AdapterPermanentError(self._error_note(binding, response))
            if call.stream:
                return self._run_stream(
                    binding, context, response, started_at
                )
            return self._run_nonstreaming(
                binding, context, response, started_at
            )
        finally:
            connection.close()

    # ── Ollama native reads (discovery / health) ─────────────────────────

    def discover_models(self, resource_id: str) -> DiscoveryResult:
        """List the backend's locally available models (never a download).

        Reads the preset's discovery endpoint (Ollama: ``GET /api/tags``,
        "Fetch a list of models and their details"). Read-only; never
        pulls, creates or mutates anything; never consumes inference
        quota.
        """
        binding = self._bindings.get(resource_id)
        if binding is None:
            return DiscoveryResult(
                status="refused",
                models=(),
                note=f"no binding configured for resource '{resource_id}'",
            )
        path = binding.preset.policy.discovery_path
        if path is None:
            return DiscoveryResult(
                status="unsupported_preset",
                models=(),
                note=(
                    f"preset '{binding.preset.preset_id}' evidences no model "
                    + "discovery endpoint"
                ),
            )
        try:
            body = self._read_native_get(binding, path)
        except (AdapterAmbiguousError, AdapterTimeoutError):
            return DiscoveryResult(
                status="unreachable",
                models=(),
                note="the discovery endpoint could not be read",
            )
        except AdapterPermanentError as exc:
            return DiscoveryResult(status="refused", models=(), note=str(exc))
        parsed_document = self._parse_json_body(body)
        if not isinstance(parsed_document, Mapping):
            return DiscoveryResult(
                status="schema_drift", models=(), note="discovery body is not JSON"
            )
        document = cast("Mapping[str, object]", parsed_document)
        raw_models = document.get("models")
        if not isinstance(raw_models, list):
            return DiscoveryResult(
                status="schema_drift",
                models=(),
                note="discovery body carries no models array",
            )
        models: list[str] = []
        for raw_entry in cast("list[object]", raw_models):
            if not isinstance(raw_entry, Mapping):
                return DiscoveryResult(
                    status="schema_drift",
                    models=(),
                    note="discovery models array carries a non-object entry",
                )
            entry = cast("Mapping[str, object]", raw_entry)
            name = entry.get("name")
            if name.__class__ is not str or not name:
                return DiscoveryResult(
                    status="schema_drift",
                    models=(),
                    note="discovery entry carries no model name",
                )
            models.append(cast("str", name))
        return DiscoveryResult(status="ok", models=tuple(models))

    def probe_health(self, resource_id: str) -> HealthProbeResult:
        """Probe backend readiness (Ollama: ``GET /api/version``).

        Read-only and quota-free: no inference is executed, no model is
        loaded, nothing is downloaded.
        """
        binding = self._bindings.get(resource_id)
        if binding is None:
            return HealthProbeResult(
                status="refused",
                note=f"no binding configured for resource '{resource_id}'",
            )
        path = binding.preset.policy.health_path
        if path is None:
            return HealthProbeResult(
                status="unsupported_preset",
                note=(
                    f"preset '{binding.preset.preset_id}' evidences no health "
                    + "endpoint"
                ),
            )
        try:
            body = self._read_native_get(binding, path)
        except (AdapterAmbiguousError, AdapterTimeoutError):
            return HealthProbeResult(
                status="unreachable",
                note="the health endpoint could not be read",
            )
        except AdapterPermanentError as exc:
            return HealthProbeResult(status="refused", note=str(exc))
        parsed_document = self._parse_json_body(body)
        if not isinstance(parsed_document, Mapping):
            return HealthProbeResult(
                status="schema_drift", note="health body is not JSON"
            )
        document = cast("Mapping[str, object]", parsed_document)
        version = document.get("version")
        if isinstance(version, str) and version:
            return HealthProbeResult(status="ok", note=f"backend version {version}")
        return HealthProbeResult(
            status="schema_drift", note="health body carries no version"
        )

    # ── Response paths ────────────────────────────────────────────────────

    def _run_nonstreaming(
        self,
        binding: ResourceBinding,
        context: ExecutionContext,
        response: http.client.HTTPResponse,
        started_at: str,
    ) -> AdapterResult:
        _ = context  # read bounds are transport-enforced on this path
        try:
            raw = response.read(MAX_RESPONSE_BODY_BYTES + 1)
        except TimeoutError:
            raise AdapterTimeoutError(
                "the provider response exceeded the connection timeout"
            ) from None
        except (OSError, http.client.HTTPException):
            # The response had started; its consumption is definitive.
            raise AdapterPermanentError(
                "the provider response was cut off before it completed"
            ) from None
        if len(raw) > MAX_RESPONSE_BODY_BYTES:
            raise AdapterPermanentError(
                "the provider response exceeds the bounded size"
            )
        document = self._parse_json_body(raw)
        if document is None:
            raise AdapterPermanentError(
                "the provider response is not a JSON document"
            )
        try:
            parsed = parse_chat_completion_response(
                document, binding.preset.policy
            )
        except TranslationError as exc:
            raise AdapterPermanentError(str(exc)) from None
        observation = CallObservation(
            call_index=0,
            started_at=started_at,
            ended_at=_canonical_now(),
            status="completed",
            provider_reported_usage=parsed.usage,
        )
        return AdapterResult(
            status="completed",
            calls=(observation,),
            message=parsed.message,
            finish_reason=parsed.finish_reason,
        )

    def _run_stream(
        self,
        binding: ResourceBinding,
        context: ExecutionContext,
        response: http.client.HTTPResponse,
        started_at: str,
    ) -> AdapterResult:
        _ = binding  # the request was already built from this binding
        parser = SseStreamParser(max_total_bytes=MAX_STREAM_TOTAL_BYTES)
        accumulator = ToolCallAccumulator()
        finish_reason: str | None = None
        usage: UsageTokens | None = None
        text_parts: list[str] = []
        emit = context.emit_chunk
        try:
            while not parser.finished:
                if context.cancelled:
                    return self._cancelled_result(started_at)
                self._check_deadline(context)
                try:
                    # read1 returns whatever the socket already delivered:
                    # streaming stays incremental, bounded per read, and
                    # cancellation/deadline checks run between frames.
                    raw = response.read1(_STREAM_READ_CHUNK)
                except TimeoutError:
                    raise AdapterTimeoutError(
                        "the provider stream stalled past the connection timeout"
                    ) from None
                except (OSError, http.client.HTTPException):
                    raise AdapterPermanentError(
                        "the provider stream was cut off before it completed"
                    ) from None
                if not raw:
                    break
                for frame in parser.feed(raw):
                    view = interpret_stream_frame(frame)
                    if view.text_delta is not None:
                        # Empty-string deltas are valid provider framing but
                        # not a normalized text_delta (non-empty by contract).
                        text_parts.append(view.text_delta)
                        if emit is not None and view.text_delta:
                            emit(
                                AdapterStreamChunk(
                                    kind=CHUNK_TEXT_DELTA, text=view.text_delta
                                )
                            )
                    for fragment in view.tool_fragments:
                        accumulator.add_fragment(fragment)
                    if view.finish_reason is not None:
                        finish_reason = view.finish_reason
                    if view.usage is not None:
                        usage = view.usage
            for frame in parser.close():
                view = interpret_stream_frame(frame)
                if view.text_delta is not None:
                    text_parts.append(view.text_delta)
                    if emit is not None and view.text_delta:
                        emit(
                            AdapterStreamChunk(
                                kind=CHUNK_TEXT_DELTA, text=view.text_delta
                            )
                        )
                for fragment in view.tool_fragments:
                    accumulator.add_fragment(fragment)
                if view.finish_reason is not None:
                    finish_reason = view.finish_reason
                if view.usage is not None:
                    usage = view.usage
        except TranslationError as exc:
            raise AdapterPermanentError(str(exc)) from None
        if finish_reason is None:
            raise AdapterPermanentError(
                "the provider stream ended without a finish reason"
            )
        completed_calls = accumulator.complete()
        content = "".join(text_parts)
        observation = CallObservation(
            call_index=0,
            started_at=started_at,
            ended_at=_canonical_now(),
            status="completed",
            provider_reported_usage=usage,
        )
        if emit is not None:
            for tool_call in completed_calls:
                emit(AdapterStreamChunk(kind=CHUNK_TOOL_CALL, tool_call=tool_call))
            emit(AdapterStreamChunk(kind=CHUNK_FINISH, finish_reason=finish_reason))
            if usage is not None:
                emit(AdapterStreamChunk(kind=CHUNK_USAGE, usage=usage))
        return AdapterResult(
            status="completed",
            calls=(observation,),
            message=AdapterMessage(
                role="assistant",
                content=content if content else None,
                tool_calls=completed_calls,
            ),
            finish_reason=finish_reason,
        )

    def _cancelled_result(self, started_at: str) -> AdapterResult:
        return AdapterResult(
            status="cancelled",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at=started_at,
                    ended_at=_canonical_now(),
                    status="cancelled",
                ),
            ),
        )

    # ── Transport (D-044 outbound rules) ──────────────────────────────────

    def _headers(self, binding: ResourceBinding, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            GATEWAY_MARKER_HEADER: GATEWAY_MARKER_VALUE,
        }
        if binding.credential is not None:
            headers["Authorization"] = binding.credential.authorization_header
        return headers

    def _connect(
        self, binding: ResourceBinding, context: ExecutionContext
    ) -> http.client.HTTPConnection:
        timeout = min(self._remaining(context), self._socket_timeout)
        origin = binding.origin
        if origin.scheme == "https":
            # Verified certificates and hostname checking; D-044: a
            # verification bypass does not exist as an option anywhere.
            return http.client.HTTPSConnection(
                origin.hostname,
                origin.port,
                context=ssl.create_default_context(),
                timeout=timeout,
            )
        # Plain HTTP reached this point only for an explicit loopback
        # origin, enforced by ProviderOrigin at configuration time.
        return http.client.HTTPConnection(
            origin.hostname, origin.port, timeout=timeout
        )

    @staticmethod
    def _send(
        connection: http.client.HTTPConnection,
        binding: ResourceBinding,
        payload: bytes,
        headers: dict[str, str],
    ) -> None:
        try:
            connection.request(
                "POST",
                binding.preset.policy.endpoint_path,
                body=payload,
                headers=headers,
            )
        except TimeoutError:
            raise AdapterTimeoutError(
                "the provider connection timed out before the request was sent"
            ) from None
        except (OSError, http.client.HTTPException):
            raise AdapterAmbiguousError(
                "the provider connection failed before a response arrived; "
                + "whether the backend consumed the request is unknown"
            ) from None

    @staticmethod
    def _receive(connection: http.client.HTTPConnection) -> http.client.HTTPResponse:
        try:
            return connection.getresponse()
        except TimeoutError:
            raise AdapterTimeoutError(
                "the provider did not respond before the connection timeout"
            ) from None
        except (OSError, http.client.HTTPException):
            raise AdapterAmbiguousError(
                "the provider connection failed before a response arrived; "
                + "whether the backend consumed the request is unknown"
            ) from None

    @staticmethod
    def _refuse_router_backends(response: http.client.HTTPResponse) -> None:
        """Refuse a backend that identifies as this program's gateway."""
        if response.headers.get(GATEWAY_MARKER_HEADER) is not None:
            raise AdapterPermanentError(
                "the configured backend identified itself as a Scarcity "
                + "Router gateway; a router endpoint must never serve as "
                + "another router's backend"
            )

    @staticmethod
    def _error_note(
        binding: ResourceBinding, response: http.client.HTTPResponse
    ) -> str:
        _ = binding
        """A safe note for a non-200 provider response (bounded read).

        Only status codes and vocabulary-checked provider error-code
        tokens are copied; message text, payloads and credentials never
        appear in a note.
        """
        document: object = None
        try:
            raw = response.read(_MAX_ERROR_BODY_BYTES + 1)
            if raw and len(raw) <= _MAX_ERROR_BODY_BYTES:
                document = OpenAICompatibleHttpAdapter._parse_json_body(raw)
        except (OSError, http.client.HTTPException):
            document = None
        if response.status == 408:
            return "provider reported a request timeout (HTTP 408)"
        note = provider_error_note(response.status, document)
        if response.status in (401, 403):
            return "provider rejected the configured credential; " + note
        return note

    def _read_native_get(self, binding: ResourceBinding, path: str) -> bytes:
        context = ExecutionContext(
            request_id="native-read",
            deadline=_canonical_now_plus(seconds=60),
        )
        connection = self._connect(binding, context)
        try:
            headers = self._headers(binding, "application/json")
            _ = headers.pop("Content-Type", None)
            connection.request("GET", path, body=None, headers=headers)
            response = connection.getresponse()
            self._refuse_router_backends(response)
            if response.status != 200:
                raise AdapterPermanentError(
                    f"native endpoint returned HTTP {response.status}"
                )
            return response.read(MAX_RESPONSE_BODY_BYTES)
        except TimeoutError:
            raise AdapterTimeoutError("native endpoint read timed out") from None
        except (OSError, http.client.HTTPException) as exc:
            raise AdapterAmbiguousError("native endpoint could not be read") from exc
        finally:
            connection.close()

    @staticmethod
    def _parse_json_body(raw: bytes) -> object:
        try:
            document: object = cast("object", json.loads(raw.decode("utf-8")))
            return document
        except (UnicodeDecodeError, ValueError):
            return None

    # ── Deadline / cancellation helpers ───────────────────────────────────

    @staticmethod
    def _remaining(context: ExecutionContext) -> float:
        try:
            deadline = datetime.fromisoformat(context.deadline.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AdapterPermanentError(
                "the dispatch context carries an unparsable deadline"
            ) from exc
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise AdapterTimeoutError("the dispatch deadline already expired")
        return remaining

    def _check_deadline(self, context: ExecutionContext) -> None:
        _ = self._remaining(context)


__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "GATEWAY_MARKER_HEADER",
    "GATEWAY_MARKER_VALUE",
    "MAX_RESPONSE_BODY_BYTES",
    "DiscoveryResult",
    "HealthProbeResult",
    "OpenAICompatibleHttpAdapter",
    "ResourceBinding",
]
