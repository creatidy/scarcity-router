"""Transport, security and integration tests for the M04 HTTP adapter.

Every test runs against the deterministic in-thread synthetic provider in
``tests/openai_http_fixtures.py`` (loopback only, no live provider, no
real credential). Categories covered: completion, SSE streaming, tool-call
delta accumulation, structured-output policies, provider error mapping,
malformed/drift responses, timeout, mid-stream cancellation, redirect
refusal, credential safety, router-loop marker stamping and gateway-as-
backend refusal, usage accounting, Ollama direct behavior (discovery +
health), the worker-bridged seam, Z.ai without any ZCode dependency, and
API-only operation with no worker through the real M03 coordinator.
"""

from __future__ import annotations

import sys
import threading
import unittest
from datetime import datetime, timezone
from typing import cast, override

from scarcity_router.capacity import CapacitySnapshot, CapacityWindow
from scarcity_router.gateway_adapters import (
    CHUNK_FINISH,
    CHUNK_TEXT_DELTA,
    CHUNK_TOOL_CALL,
    CHUNK_USAGE,
    AdapterPermanentError,
    AdapterRegistry,
    AdapterResult,
    AdapterStreamChunk,
    AdapterTimeoutError,
)
from scarcity_router.gateway_audit import BoundedAuditTrail
from scarcity_router.gateway_coordinator import GatewayApplication
from scarcity_router.providers.openai_http_adapter import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    GATEWAY_MARKER_HEADER,
    OpenAICompatibleHttpAdapter,
)
from scarcity_router.providers.http_origin import ProviderOrigin
from scarcity_router.providers.openai_http_presets import (
    GENERIC_PRESET,
    OLLAMA_PRESET,
    PRESETS,
    preset_by_id,
)
from scarcity_router.providers.openai_http_evidence import (
    PRESET_CELL_VALUES,
    default_cells_for,
)
from scarcity_router.resource_state import (
    ExecutionCapabilities,
    QuotaFact,
    ResourceHealth,
    ResourceIdentity,
    ResourceRegistry,
    ResourceRegistration,
    ResourceStateSnapshot,
)
from tests.gateway_fixtures import (
    CLIENT_ID,
    make_application,
    parse_chat_request,
)
from tests.openai_http_fixtures import (
    FAKE_PROVIDER_KEY,
    MARKER_HEADER,
    RecordedRequest,
    ScriptedProviderServer,
    ScriptedResponse,
    USAGE,
    completion_frame,
    make_adapter,
    make_binding,
    make_call,
    make_context,
    preset,
)

_NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 - test clock


class ChunkCollector:
    """Thread-safe normalized chunk collector for streaming tests."""

    chunks: list[AdapterStreamChunk]
    first_chunk_event: threading.Event
    _lock: threading.Lock

    def __init__(self) -> None:
        self.chunks = []
        self.first_chunk_event = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, chunk: object) -> None:
        assert isinstance(chunk, AdapterStreamChunk)
        with self._lock:
            self.chunks.append(chunk)
        self.first_chunk_event.set()

    @property
    def texts(self) -> list[str]:
        return [
            chunk.text
            for chunk in self.chunks
            if chunk.kind == CHUNK_TEXT_DELTA and chunk.text is not None
        ]


def _fresh_observation(
    identity: ResourceIdentity, observed_at: str
) -> ResourceStateSnapshot:
    scope = "codex" if identity.provider == "openai" else "coding_plan"
    return ResourceStateSnapshot(
        schema_version=1,
        identity=identity,
        observed_at=observed_at,
        health=ResourceHealth(status="ok", diagnostics=()),
        quota_facts=(
            QuotaFact(
                observation_class="provider_telemetry",
                window=CapacityWindow(
                    resource="tokens",
                    kind="five_hour",
                    scope_id=scope,
                    duration_seconds=18_000,
                    used_percent=10,
                    remaining_percent=90,
                ),
            ),
        ),
        promotions=(),
    )


def _canonical(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fresh_registry() -> ResourceRegistry:
    """The default healthy world, observed against the real clock."""
    identities = (
        ResourceIdentity(
            resource_id="openai-http",
            channel="server_direct_http",
            provider="openai",
            model="gpt-5.6-luna",
            entitlement="payg_metered",
        ),
        ResourceIdentity(
            resource_id="zai-http",
            channel="server_direct_http",
            provider="zai",
            model="glm-5.3",
            entitlement="subscription_included",
        ),
    )
    registry = ResourceRegistry(clock=lambda: _canonical(_NOW()))
    for identity in identities:
        registry.register(
            ResourceRegistration(
                identity=identity,
                freshness_ttl_seconds=300,
                capabilities=ExecutionCapabilities(context_limit_tokens=272_000),
            )
        )
        registry.apply_snapshot(
            _fresh_observation(identity, _canonical(_NOW()))
        )
    return registry


def fresh_capacity_snapshots() -> tuple[CapacitySnapshot, ...]:
    snapshots: list[CapacitySnapshot] = []
    for provider, scope in (("openai", "codex"), ("zai", "coding_plan")):
        snapshots.append(
            CapacitySnapshot(
                schema_version=3,
                provider=provider,
                source="synthetic_test",
                retrieved_at=_canonical(_NOW()),
                status="ok",
                windows=(
                    CapacityWindow(
                        resource="tokens",
                        kind="five_hour",
                        scope_id=scope,
                        duration_seconds=18_000,
                        used_percent=10,
                        remaining_percent=90,
                        window_id=f"{provider}-five",
                    ),
                ),
                diagnostics=(),
            )
        )
    return tuple(snapshots)


class AdapterTestCase(unittest.TestCase):
    """Base: one synthetic provider server per test."""

    server: ScriptedProviderServer

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        # Placeholder; setUp replaces it before each test body runs.
        self.server = cast("ScriptedProviderServer", object())

    @override
    def setUp(self) -> None:
        self.server = ScriptedProviderServer()
        self.server.start()
        self.addCleanup(self.server.stop)

    def openai_binding(self, *, resource_id: str = "openai-http"):
        return make_binding(
            self.server,
            preset("openai-api"),
            resource_id=resource_id,
        )


class CompletionTest(AdapterTestCase):
    def test_standard_completion_round_trip(self) -> None:
        self.server.enqueue_completion(content="the answer", usage=USAGE)
        adapter = make_adapter(self.openai_binding())
        result = adapter.execute(make_call(), make_context())
        self.assertEqual(result.status, "completed")
        assert result.message is not None
        self.assertEqual(result.message.content, "the answer")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(len(result.calls), 1)
        observation = result.calls[0]
        self.assertEqual(observation.status, "completed")
        assert observation.provider_reported_usage is not None
        self.assertEqual(observation.provider_reported_usage.total_tokens, 17)

    def test_request_is_bound_to_configured_origin_and_stamped(self) -> None:
        self.server.enqueue_completion()
        adapter = make_adapter(self.openai_binding())
        _ = adapter.execute(make_call(), make_context())
        request = self.server.last_request
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.path, "/v1/chat/completions")
        self.assertEqual(
            request.headers.get("authorization"), f"Bearer {FAKE_PROVIDER_KEY}"
        )
        self.assertEqual(request.headers.get(MARKER_HEADER.lower()), "scarcity-router-gateway/1")
        body = request.json
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "gpt-5.6-luna")
        self.assertEqual(body["messages"][0]["content"], "synthetic prompt")

    def test_wire_model_override_reaches_the_provider(self) -> None:
        self.server.enqueue_completion()
        binding = make_binding(
            self.server,
            preset("openai-api"),
            wire_model="gpt-5.6-luna-2026-01-01",
        )
        adapter = make_adapter(binding)
        _ = adapter.execute(make_call(model="gpt-5-6-luna"), make_context())
        body = self.server.last_request.json
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "gpt-5.6-luna-2026-01-01")


class StreamingTest(AdapterTestCase):
    FRAMES: list[dict[str, object]] = [
        completion_frame(role="assistant"),
        completion_frame(text="hel"),
        completion_frame(text="lo"),
        completion_frame(finish_reason="stop"),
        completion_frame(usage=USAGE),
    ]

    def test_sse_streaming_emits_normalized_chunks_in_order(self) -> None:
        self.server.enqueue_stream(self.FRAMES)
        adapter = make_adapter(self.openai_binding())
        collector = ChunkCollector()
        result = adapter.execute(
            make_call(stream=True), make_context(emit=collector)
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(collector.texts, ["hel", "lo"])
        kinds = [chunk.kind for chunk in collector.chunks]
        self.assertEqual(
            kinds,
            [CHUNK_TEXT_DELTA, CHUNK_TEXT_DELTA, CHUNK_FINISH, CHUNK_USAGE],
        )
        assert result.message is not None
        self.assertEqual(result.message.content, "hello")
        finish = collector.chunks[-2]
        self.assertEqual(finish.kind, CHUNK_FINISH)
        self.assertEqual(finish.finish_reason, "stop")

    def test_streaming_tool_call_deltas_accumulate_into_complete_calls(self) -> None:
        frames: list[dict[str, object]] = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-9",
                                    "function": {
                                        "name": "list_files",
                                        "arguments": '{"pa',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": 'th": "."}'}}
                            ]
                        }
                    }
                ]
            },
            completion_frame(finish_reason="tool_calls"),
            completion_frame(usage=USAGE),
        ]
        self.server.enqueue_stream(frames)
        adapter = make_adapter(self.openai_binding())
        collector = ChunkCollector()
        result = adapter.execute(
            make_call(stream=True), make_context(emit=collector)
        )
        self.assertEqual(result.status, "completed")
        tool_chunks = [c for c in collector.chunks if c.kind == CHUNK_TOOL_CALL]
        self.assertEqual(len(tool_chunks), 1)  # exactly one COMPLETE call
        assert tool_chunks[0].tool_call is not None
        self.assertEqual(tool_chunks[0].tool_call.id, "call-9")
        self.assertEqual(
            tool_chunks[0].tool_call.arguments, '{"path": "."}'
        )
        assert result.message is not None
        assert result.message.tool_calls
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.message.tool_calls[0].name, "list_files")

    def test_stream_without_finish_reason_is_drift(self) -> None:
        self.server.enqueue_stream(
            [completion_frame(text="partial")], send_done=True
        )
        adapter = make_adapter(self.openai_binding())
        with self.assertRaises(AdapterPermanentError) as caught:
            _ = adapter.execute(make_call(stream=True), make_context())
        self.assertIn("finish reason", str(caught.exception))


class TranslationPolicyEnforcementTest(AdapterTestCase):
    """Unevidenced request features are refused BEFORE any byte is sent."""

    def test_deepseek_preset_refuses_json_schema_before_sending(self) -> None:
        binding = make_binding(
            self.server, preset("deepseek"), resource_id="deepseek-http"
        )
        adapter = make_adapter(binding)
        call = make_call(
            resource_id="deepseek-http",
            provider="deepseek",
            model="deepseek-chat",
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "out", "schema": {}},
            },
        )
        with self.assertRaises(AdapterPermanentError):
            _ = adapter.execute(call, make_context())
        self.assertEqual(self.server.request_count, 0)

    def test_zai_preset_refuses_forced_tool_choice_before_sending(self) -> None:
        binding = make_binding(
            self.server, preset("zai-coding-plan"), resource_id="zai-http"
        )
        adapter = make_adapter(binding)
        call = make_call(
            resource_id="zai-http",
            provider="zai",
            model="glm-5.3",
            tool_choice="required",
        )
        with self.assertRaises(AdapterPermanentError):
            _ = adapter.execute(call, make_context())
        self.assertEqual(self.server.request_count, 0)

    def test_openai_preset_forwards_structured_output(self) -> None:
        self.server.enqueue_completion(content='{"a": 1}')
        adapter = make_adapter(self.openai_binding())
        call = make_call(
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "out", "strict": True, "schema": {}},
            }
        )
        result = adapter.execute(call, make_context())
        self.assertEqual(result.status, "completed")
        body = self.server.last_request.json
        assert isinstance(body, dict)
        self.assertEqual(body["response_format"]["type"], "json_schema")


class ProviderErrorMappingTest(AdapterTestCase):
    def test_http_errors_map_to_permanent_failures_with_safe_notes(self) -> None:
        cases: tuple[tuple[int, object, str], ...] = (
            (
                401,
                {
                    "error": {
                        "message": f"bad key {FAKE_PROVIDER_KEY}",
                        "code": "invalid_api_key",
                    }
                },
                "invalid_api_key",
            ),
            (429, {"error": {"code": "rate_limit_exceeded"}}, "rate_limit_exceeded"),
            (503, None, "503"),
        )
        for status, envelope, expected_token in cases:
            with self.subTest(status=status):
                if envelope is None:
                    self.server.enqueue_error(status, None, raw=b"")
                else:
                    self.server.enqueue_error(status, envelope)
                before = self.server.request_count
                adapter = make_adapter(self.openai_binding())
                with self.assertRaises(AdapterPermanentError) as caught:
                    _ = adapter.execute(make_call(), make_context())
                note = str(caught.exception)
                self.assertIn(expected_token, note)
                self.assertNotIn(FAKE_PROVIDER_KEY, note)
                # Exactly one request: never retried, never failed over.
                self.assertEqual(self.server.request_count, before + 1)

    def test_provider_error_message_text_never_enters_notes(self) -> None:
        self.server.enqueue_error(
            400,
            {"error": {"message": "echo back: synthetic prompt content", "code": "bad"}},
        )
        adapter = make_adapter(self.openai_binding())
        with self.assertRaises(AdapterPermanentError) as caught:
            _ = adapter.execute(make_call(), make_context())
        self.assertNotIn("synthetic prompt content", str(caught.exception))

    def test_zai_code_message_shape_maps_by_status(self) -> None:
        self.server.enqueue_error(429, {"code": 1302, "message": "rate limited"})
        binding = make_binding(
            self.server, preset("zai-coding-plan"), resource_id="zai-http"
        )
        adapter = make_adapter(binding)
        with self.assertRaises(AdapterPermanentError) as caught:
            _ = adapter.execute(
                make_call(resource_id="zai-http", provider="zai", model="glm-5.3"),
                make_context(),
            )
        self.assertIn("1302", str(caught.exception))


class DriftAndMalformedTest(AdapterTestCase):
    def test_malformed_body_is_drift(self) -> None:
        self.server.enqueue_error(200, None, raw=b"not json {{ at all")
        adapter = make_adapter(self.openai_binding())
        with self.assertRaises(AdapterPermanentError):
            _ = adapter.execute(make_call(), make_context())

    def test_protocol_drift_unknown_finish_reason_fails_closed(self) -> None:
        self.server.enqueue_completion(finish_reason="sensitive")
        adapter = make_adapter(self.openai_binding())
        with self.assertRaises(AdapterPermanentError) as caught:
            _ = adapter.execute(make_call(), make_context())
        self.assertIn("sensitive", str(caught.exception))

    def test_non_stream_response_without_choices_is_drift(self) -> None:
        self.server.enqueue_error(200, {"id": "x", "choices": []})
        adapter = make_adapter(self.openai_binding())
        with self.assertRaises(AdapterPermanentError):
            _ = adapter.execute(make_call(), make_context())


class TimeoutAndCancellationTest(AdapterTestCase):
    def test_socket_timeout_maps_to_typed_timeout(self) -> None:
        def slow(_request: RecordedRequest) -> ScriptedResponse:
            _ = threading.Event().wait(timeout=2.0)
            return ScriptedResponse(200, {}, b"{}")

        self.server.enqueue(slow)
        adapter = make_adapter(
            self.openai_binding(), socket_timeout_seconds=0.3
        )
        with self.assertRaises(AdapterTimeoutError):
            _ = adapter.execute(make_call(), make_context())

    def test_expired_deadline_fails_before_sending(self) -> None:
        adapter = make_adapter(self.openai_binding())
        with self.assertRaises(AdapterTimeoutError) as caught:
            _ = adapter.execute(make_call(), make_context(deadline_seconds=-1.0))
        self.assertIn("expired", str(caught.exception))
        self.assertEqual(self.server.request_count, 0)

    def test_cancellation_with_fully_coalesced_stream_is_cancelled(self) -> None:
        # The race F1 closed: every frame plus [DONE] is available in one
        # read and cancellation is set before the dispatch runs. The adapter
        # must never render a completed result for a cancelled context.
        self.server.enqueue_stream(
            [
                completion_frame(text="all"),
                completion_frame(finish_reason="stop"),
            ]
        )
        adapter = make_adapter(self.openai_binding())
        context = make_context()
        context.cancel_event.set()
        result = adapter.execute(make_call(stream=True), context)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.calls[0].status, "cancelled")

    def test_nonstreaming_cancellation_returns_cancelled_result(self) -> None:
        # F3 symmetry: the non-streaming body loop checks cancellation too.
        self.server.enqueue_completion(content="unused")
        adapter = make_adapter(self.openai_binding())
        context = make_context()
        context.cancel_event.set()
        result = adapter.execute(make_call(), context)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.calls[0].status, "cancelled")

    def test_cancellation_mid_stream_returns_cancelled_result(self) -> None:
        gate = threading.Event()
        frames = [
            completion_frame(text="par"),
            completion_frame(text="tial"),
            completion_frame(finish_reason="stop"),
        ]
        self.server.enqueue_stream(frames, gate=gate, gate_after=1)
        adapter = make_adapter(self.openai_binding())
        collector = ChunkCollector()
        context = make_context(emit=collector)
        outcome: dict[str, AdapterResult | None] = {"result": None}
        error: dict[str, BaseException | None] = {"exc": None}

        def run() -> None:
            try:
                outcome["result"] = adapter.execute(
                    make_call(stream=True), context
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                error["exc"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(collector.first_chunk_event.wait(timeout=5))
        context.cancel_event.set()
        gate.set()
        thread.join(timeout=10)
        self.assertIsNone(error["exc"])
        result = outcome["result"]
        assert result is not None
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.calls[0].status, "cancelled")


class RedirectAndRouterLoopTest(AdapterTestCase):
    _requests_before: int

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self._requests_before = 0

    def test_redirects_are_refused_and_never_followed(self) -> None:
        self._requests_before = self.server.request_count
        for status in (301, 302, 307):
            with self.subTest(status=status):
                self.server.enqueue_redirect(
                    status, "http://evil.example/harvest"
                )
                adapter = make_adapter(self.openai_binding())
                with self.assertRaises(AdapterPermanentError) as caught:
                    _ = adapter.execute(make_call(), make_context())
                self.assertIn("redirect refused", str(caught.exception))
                # Exactly ONE request per dispatch: the redirect was never
                # followed, so no Authorization ever crossed origins.
                self.assertEqual(
                    self.server.request_count,
                    self._requests_before + 1,
                )
            self._requests_before = self.server.request_count

    def test_router_loop_marker_is_stamped_on_streaming_requests_too(self) -> None:
        self.server.enqueue_stream(
            [
                completion_frame(text="hi"),
                completion_frame(finish_reason="stop"),
            ]
        )
        adapter = make_adapter(self.openai_binding())
        _ = adapter.execute(make_call(stream=True), make_context())
        self.assertEqual(
            self.server.last_request.headers.get(MARKER_HEADER.lower()),
            "scarcity-router-gateway/1",
        )

    def test_backend_identifying_as_gateway_is_refused(self) -> None:
        self.server.enqueue_completion(
            headers={GATEWAY_MARKER_HEADER: "scarcity-router-gateway/1"}
        )
        adapter = make_adapter(self.openai_binding())
        with self.assertRaises(AdapterPermanentError) as caught:
            _ = adapter.execute(make_call(), make_context())
        self.assertIn("Scarcity Router gateway", str(caught.exception))
        self.assertEqual(
            GATEWAY_MARKER_HEADER,
            "X-Scarcity-Router-Gateway",  # frozen ingress marker (D-044)
        )


class CredentialSafetyTest(AdapterTestCase):
    def test_credential_is_redacted_from_reprs_and_failures(self) -> None:
        binding = self.openai_binding()
        credential = binding.credential
        assert credential is not None
        self.assertNotIn(FAKE_PROVIDER_KEY, repr(credential))
        self.assertNotIn(FAKE_PROVIDER_KEY, str(credential))
        self.assertNotIn(FAKE_PROVIDER_KEY, repr(binding))
        self.server.enqueue_error(500, {"error": {"message": "boom"}})
        adapter = make_adapter(binding)
        with self.assertRaises(AdapterPermanentError) as caught:
            _ = adapter.execute(make_call(), make_context())
        self.assertNotIn(FAKE_PROVIDER_KEY, str(caught.exception))

    def test_preset_requiring_credential_refuses_unbound_configuration(self) -> None:
        with self.assertRaises(ValueError):
            _ = make_binding(
                self.server, preset("openai-api"), with_credential=False
            )

    def test_ollama_preset_allows_credential_free_loopback(self) -> None:
        binding = make_binding(
            self.server,
            preset("ollama"),
            resource_id="ollama-local",
            with_credential=False,
        )
        self.server.enqueue_completion(content="local reply")
        adapter = make_adapter(binding)
        result = adapter.execute(
            make_call(
                resource_id="ollama-local",
                provider="ollama",
                model="llama3-8b",
            ),
            make_context(),
        )
        self.assertEqual(result.status, "completed")
        self.assertIsNone(self.server.last_request.headers.get("authorization"))

    def test_unknown_resource_is_refused_without_network(self) -> None:
        adapter = make_adapter(self.openai_binding())
        with self.assertRaises(AdapterPermanentError) as caught:
            _ = adapter.execute(
                make_call(resource_id="no-such-resource"), make_context()
            )
        self.assertIn("no provider binding", str(caught.exception))
        self.assertEqual(self.server.request_count, 0)


class UsageAccountingTest(AdapterTestCase):
    def test_usage_unavailable_is_honest_not_zero(self) -> None:
        self.server.enqueue_completion(usage=None)
        adapter = make_adapter(self.openai_binding())
        result = adapter.execute(make_call(), make_context())
        observation = result.calls[0]
        self.assertIsNone(observation.provider_reported_usage)
        self.assertIsNone(observation.estimated_usage)

    def test_stream_usage_chunk_is_emitted_and_observed(self) -> None:
        self.server.enqueue_stream(
            [
                completion_frame(text="hi"),
                completion_frame(finish_reason="stop"),
                completion_frame(usage=USAGE),
            ]
        )
        adapter = make_adapter(self.openai_binding())
        collector = ChunkCollector()
        result = adapter.execute(
            make_call(stream=True), make_context(emit=collector)
        )
        assert result.calls[0].provider_reported_usage is not None
        self.assertEqual(result.calls[0].provider_reported_usage.total_tokens, 17)
        self.assertEqual(collector.chunks[-1].kind, CHUNK_USAGE)


class OriginConfigurationTest(unittest.TestCase):
    def test_plain_http_requires_loopback(self) -> None:
        with self.assertRaises(ValueError):
            _ = ProviderOrigin.parse("http://api.example.com")
        origin = ProviderOrigin.parse("http://localhost:11434")
        self.assertTrue(origin.is_loopback)
        self.assertEqual(origin.port, 11434)

    def test_origins_are_bare(self) -> None:
        for bad in (
            "https://api.deepseek.com/v1",
            "https://api.deepseek.com?x=1",
            "https://api.deepseek.com/#f",
            "https://user:pass@api.deepseek.com",
            "ftp://api.deepseek.com",
            "https://api.deepseek.com:99999",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _ = ProviderOrigin.parse(bad)

    def test_https_default_port_and_origin_property(self) -> None:
        origin = ProviderOrigin.parse("https://api.openai.com")
        self.assertEqual(origin.port, 443)
        self.assertEqual(origin.origin, "https://api.openai.com:443")


class OllamaDirectTest(AdapterTestCase):
    """Direct-network Ollama over the ONE generic implementation."""

    def _ollama_adapter(self) -> OpenAICompatibleHttpAdapter:
        return make_adapter(
            make_binding(
                self.server,
                preset("ollama"),
                resource_id="ollama-local",
                with_credential=False,
            )
        )

    def test_discovery_reads_native_tags_endpoint(self) -> None:
        self.server.enqueue_json(
            200,
            {
                "models": [
                    {"name": "llama3:8b", "model": "llama3:8b", "size": 1},
                    {"name": "qwen3:4b", "model": "qwen3:4b", "size": 2},
                ]
            },
        )
        adapter = self._ollama_adapter()
        discovery = adapter.discover_models("ollama-local")
        self.assertEqual(discovery.status, "ok")
        self.assertEqual(discovery.models, ("llama3:8b", "qwen3:4b"))
        request = self.server.last_request
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path, "/api/tags")
        self.assertIsNotNone(request.headers.get(MARKER_HEADER.lower()))

    def test_discovery_drift_fails_closed(self) -> None:
        self.server.enqueue_json(200, {"models": [{"size": 1}]})
        adapter = self._ollama_adapter()
        discovery = adapter.discover_models("ollama-local")
        self.assertEqual(discovery.status, "schema_drift")

    def test_discovery_refused_for_preset_without_discovery(self) -> None:
        adapter = make_adapter(self.openai_binding())
        result = adapter.discover_models("openai-http")
        self.assertEqual(result.status, "unsupported_preset")
        self.assertEqual(self.server.request_count, 0)

    def test_unrepresentable_version_never_enters_a_note(self) -> None:
        from scarcity_router.providers.openai_http_adapter import (
            HealthProbeResult,
        )

        self.server.enqueue_json(200, {"version": "0.13.3 <script>x" * 20})
        adapter = self._ollama_adapter()
        probe = adapter.probe_health("ollama-local")
        self.assertEqual(probe.status, "ok")
        assert probe.note is not None
        self.assertIn("unrepresentable", probe.note)
        self.assertNotIn("<script>", probe.note)
        self.assertLessEqual(len(probe.note), 200)
        with self.assertRaises(ValueError):
            _ = HealthProbeResult(status="ok", note="x" * 201)

    def test_health_probe_reads_version_without_inference(self) -> None:
        self.server.enqueue_json(200, {"version": "0.13.3"})
        adapter = self._ollama_adapter()
        probe = adapter.probe_health("ollama-local")
        self.assertEqual(probe.status, "ok")
        self.assertIn("0.13.3", probe.note or "")
        request = self.server.last_request
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path, "/api/version")
        # A health read never consumes inference quota: it never POSTs.
        self.assertFalse(
            any(r.method == "POST" for r in self.server.requests)
        )

    def test_ollama_completion_uses_openai_compatible_surface(self) -> None:
        self.server.enqueue_completion(content="local reply")
        adapter = self._ollama_adapter()
        result = adapter.execute(
            make_call(
                resource_id="ollama-local",
                provider="ollama",
                model="llama3-8b",
            ),
            make_context(),
        )
        self.assertEqual(result.status, "completed")
        request = self.server.last_request
        self.assertEqual(request.path, "/v1/chat/completions")
        body = request.json
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "llama3-8b")


class WorkerBridgedSeamTest(unittest.TestCase):
    """The documented M04/M05 seam: one translation core, two transports."""

    def test_this_module_does_not_implement_the_worker_channel(self) -> None:
        registry = AdapterRegistry()
        registry.register(OpenAICompatibleHttpAdapter())
        self.assertEqual(registry.registered_channels(), ("server_direct_http",))
        self.assertIsNone(registry.resolve("worker_bridged"))

    def test_translation_core_round_trip_needs_no_transport(self) -> None:
        from scarcity_router.providers.openai_http_core import (
            build_chat_completion_request,
            interpret_stream_frame,
            parse_chat_completion_response,
        )

        call = make_call()
        wire = build_chat_completion_request(call, preset("openai-api").policy)
        self.assertEqual(wire["model"], "gpt-5.6-luna")
        view = interpret_stream_frame(
            {"choices": [{"delta": {"content": "x"}, "finish_reason": None}]}
        )
        self.assertEqual(view.text_delta, "x")
        parsed = parse_chat_completion_response(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}
                ]
            },
            preset("openai-api").policy,
        )
        self.assertEqual(parsed.message.content, "x")

    def test_preset_count_matches_documented_surface(self) -> None:
        ids = {p.preset_id for p in PRESETS}
        self.assertEqual(
            ids,
            {
                "openai-api",
                "deepseek",
                "openrouter",
                "zai-coding-plan",
                "ollama",
                "generic-openai",
            },
        )


class ZaiWithoutZcodeTest(AdapterTestCase):
    """Z.ai Coding Plan executes through the generic HTTP path only."""

    def test_zai_dispatch_leaves_no_zcode_dependency(self) -> None:
        binding = make_binding(
            self.server, preset("zai-coding-plan"), resource_id="zai-http"
        )
        self.server.enqueue_completion(content="glm reply")
        adapter = make_adapter(binding)
        result = adapter.execute(
            make_call(
                resource_id="zai-http",
                provider="zai",
                model="glm-5.3",
                reasoning_effort="high",
            ),
            make_context(),
        )
        self.assertEqual(result.status, "completed")
        request = self.server.last_request
        self.assertEqual(request.path, "/chat/completions")
        body = request.json
        assert isinstance(body, dict)
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertFalse(
            any(
                name.startswith("scarcity_router") and "zcode" in name
                for name in sys.modules
            )
        )

    def test_entitlement_suggestions_keep_access_classes_distinct(self) -> None:
        # Z.ai Coding Plan is subscription-backed; the PAYG platform API is
        # a DIFFERENT preset/generic configuration. Matching model names
        # never merge them (D-042).
        zai = preset("zai-coding-plan")
        self.assertEqual(zai.suggested_entitlement, "subscription_included")
        self.assertEqual(preset("openai-api").suggested_entitlement, "payg_metered")
        self.assertEqual(preset("deepseek").suggested_entitlement, "payg_metered")
        self.assertEqual(preset("openrouter").suggested_entitlement, "payg_metered")
        self.assertEqual(preset("ollama").suggested_entitlement, "local_ungated")
        self.assertNotEqual(
            zai.suggested_entitlement,
            preset("generic-openai").suggested_entitlement,
        )


class ApiOnlyEndToEndTest(AdapterTestCase):
    """API-only operation: the real M03 coordinator with NO worker."""

    application: GatewayApplication

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.application = cast("GatewayApplication", object())

    @override
    def setUp(self) -> None:
        super().setUp()
        openai_binding = make_binding(
            self.server, preset("openai-api"), resource_id="openai-http"
        )
        zai_binding = make_binding(
            self.server, preset("zai-coding-plan"), resource_id="zai-http"
        )
        adapter = make_adapter(openai_binding, zai_binding)
        self.application = make_application(
            registry=fresh_registry(),
            capacity_snapshots=fresh_capacity_snapshots(),
            adapters=[adapter],
            clock=_NOW,
        )

    def test_nonstreaming_completion_without_any_worker(self) -> None:
        self.assertIsNone(self.application.adapters.resolve("worker_bridged"))
        self.server.enqueue_completion(content="gateway reply")
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        outcome = self.application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual(outcome.finish_reason, "stop")
        self.assertEqual(outcome.message.content, "gateway reply")
        self.assertEqual(self.server.request_count, 1)
        self.assertEqual(
            self.server.last_request.headers.get(MARKER_HEADER.lower()),
            "scarcity-router-gateway/1",
        )
        audit = self.application.audit
        assert isinstance(audit, BoundedAuditTrail)
        records = audit.snapshot()
        self.assertEqual(records[-1].adapter_name, ADAPTER_NAME)
        self.assertEqual(records[-1].adapter_version, ADAPTER_VERSION)

    def test_streaming_completion_without_any_worker(self) -> None:
        self.server.enqueue_stream(
            [
                completion_frame(text="gate"),
                completion_frame(text="way"),
                completion_frame(finish_reason="stop"),
                completion_frame(usage=USAGE),
            ]
        )
        collector = ChunkCollector()
        request = parse_chat_request(
            {
                "model": "deep-coding",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }
        )
        outcome = self.application.execute(
            client_id=CLIENT_ID, request=request, emit_chunk=collector
        )
        self.assertEqual(outcome.message.content, "gateway")
        self.assertIn("gate", collector.texts)
        self.assertEqual(collector.chunks[-2].kind, CHUNK_FINISH)
        self.assertEqual(collector.chunks[-1].kind, CHUNK_USAGE)


class EvidenceMatrixTest(unittest.TestCase):
    """The dated compatibility evidence shipped with the presets."""

    def test_every_preset_has_values_for_every_matrix_feature(self) -> None:
        from scarcity_router.routing_core import COMPAT_FEATURES

        for preset_id in PRESET_CELL_VALUES:
            table = PRESET_CELL_VALUES[preset_id]
            for feature in COMPAT_FEATURES:
                self.assertIn(feature, table, f"{preset_id}/{feature}")

    def test_generic_preset_fails_closed_everywhere(self) -> None:
        cells = default_cells_for(
            GENERIC_PRESET, provider="generic", model="some-model"
        )
        self.assertTrue(cells)
        for cell in cells:
            self.assertEqual(cell.value, "UNKNOWN")
            self.assertEqual(cell.adapter, ADAPTER_NAME)
            self.assertEqual(cell.adapter_version, ADAPTER_VERSION)

    def test_ollama_error_semantics_stay_unknown(self) -> None:
        cells = default_cells_for(
            OLLAMA_PRESET, provider="ollama", model="llama3-8b"
        )
        error_cell = next(c for c in cells if c.feature == "error_semantics")
        self.assertEqual(error_cell.value, "UNKNOWN")

    def test_cells_carry_dated_evidence(self) -> None:
        cells = default_cells_for(
            preset_by_id("zai-coding-plan") or GENERIC_PRESET,
            provider="zai",
            model="glm-5.3",
        )
        for cell in cells:
            self.assertEqual(cell.evidence.date, "2026-09-20")
            self.assertIn("docs.z.ai", cell.evidence.identifier)

    def test_default_cells_pass_matrix_construction(self) -> None:
        for preset_obj in PRESETS:
            cells = default_cells_for(
                preset_obj, provider=preset_obj.provider, model="m1"
            )
            self.assertTrue(cells)


if __name__ == "__main__":
    _ = unittest.main()
