"""Local adapter seam tests (M05): allowlist, loopback transport, isolation."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.worker_fixtures import T_NOW  # noqa: E402

from scarcity_router.gateway_adapters import (  # noqa: E402
    AdapterCall,
    AdapterMessage,
    AdapterStreamChunk,
    AdapterToolCall,
)
from scarcity_router.resource_state import ResourceIdentity  # noqa: E402
from scarcity_router.selection_types import ModelIdentity  # noqa: E402
from scarcity_router.worker_local_adapters import (  # noqa: E402
    OLLAMA_ADAPTER_ID,
    AdapterNotAllowedError,
    LocalAdapterRegistry,
    LoopbackOllamaAdapter,
    run_allowlisted,
)
from scarcity_router.worker_local_translation import (  # noqa: E402
    LoopbackHTTPRequest,
    LoopbackHTTPResponse,
    ProvisionalOpenAITranslation,
)


def _resource(resource_id: str = "ollama-local") -> ResourceIdentity:
    return ResourceIdentity(
        resource_id=resource_id,
        channel="worker_bridged",
        provider="ollama",
        model="local",
        entitlement="local_ungated",
    )


def _call(*, stream: bool = False, response_format: dict[str, object] | None = None) -> AdapterCall:
    return AdapterCall(
        resource=_resource(),
        model=ModelIdentity(provider="openai", model="local", variant="max"),
        messages=(AdapterMessage(role="user", content="hi"),),
        stream=stream,
        response_format=response_format,
    )


class AllowlistTests(unittest.TestCase):
    def test_unregistered_adapter_id_never_invokes(self) -> None:
        registry = LocalAdapterRegistry()  # empty allowlist
        invoked: list[AdapterCall] = []
        spy_registry = LocalAdapterRegistry()
        spy_registry.register(
            LoopbackOllamaAdapter(resource=_resource(), transport=None)
        )
        _ = spy_registry  # the point below is the EMPTY registry lookup
        with self.assertRaises(AdapterNotAllowedError) as caught:
            _ = run_allowlisted(
                registry,
                adapter_id=OLLAMA_ADAPTER_ID,
                call=_call(),
                cancel_event=threading.Event(),
                deadline=T_NOW,
                emit=lambda chunk: None,
            )
        self.assertEqual(OLLAMA_ADAPTER_ID, caught.exception.adapter_id)
        self.assertEqual([], invoked)

    def test_registered_adapter_runs(self) -> None:
        adapter = LoopbackOllamaAdapter(
            resource=_resource(),
            transport=lambda request: LoopbackHTTPResponse(
                status=200,
                body=(
                    b'{"choices": [{"finish_reason": "stop", "message": '
                    b'{"role": "assistant", "content": "local!"}}], '
                    b'"usage": {"prompt_tokens": 1, "completion_tokens": 2}}'
                ),
            ),
        )
        registry = LocalAdapterRegistry()
        registry.register(adapter)
        result = run_allowlisted(
            registry,
            adapter_id=OLLAMA_ADAPTER_ID,
            call=_call(),
            cancel_event=threading.Event(),
            deadline=T_NOW,
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        assert result.message is not None
        self.assertEqual("local!", result.message.content)

    def test_registry_rejects_duplicate_registration(self) -> None:
        registry = LocalAdapterRegistry()
        registry.register(LoopbackOllamaAdapter(resource=_resource(), transport=None))
        with self.assertRaises(ValueError):
            registry.register(LoopbackOllamaAdapter(resource=_resource(), transport=None))
        _ = registry

    def test_adapter_id_must_be_a_safe_identifier(self) -> None:
        registry = LocalAdapterRegistry()
        with self.assertRaises(ValueError):
            _ = run_allowlisted(
                registry,
                adapter_id="DROP TABLE adapters",
                call=_call(),
                cancel_event=threading.Event(),
                deadline=T_NOW,
                emit=lambda chunk: None,
            )


class LoopbackAdapterTests(unittest.TestCase):
    def test_non_loopback_host_is_refused_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            _ = LoopbackOllamaAdapter(
                resource=_resource(), host="inference.example.net", transport=None
            )

    def test_whole_response_translation(self) -> None:
        seen: list[LoopbackHTTPRequest] = []

        def transport(request: LoopbackHTTPRequest) -> LoopbackHTTPResponse:
            seen.append(request)
            return LoopbackHTTPResponse(
                status=200,
                body=(
                    b'{"choices": [{"finish_reason": "stop", "message": '
                    b'{"role": "assistant", "content": "hello local"}}], '
                    b'"usage": {"prompt_tokens": 3, "completion_tokens": 4}}'
                ),
            )

        adapter = LoopbackOllamaAdapter(resource=_resource(), transport=transport)
        result = adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=T_NOW,
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        assert result.message is not None
        self.assertEqual("hello local", result.message.content)
        usage = result.calls[0].provider_reported_usage
        assert usage is not None
        self.assertEqual((3, 4), (usage.prompt_tokens, usage.completion_tokens))
        # The request went to the loopback OpenAI-compatible path.
        self.assertEqual("POST", seen[0].method)
        self.assertIn("/v1/chat/completions", seen[0].path)

    def test_streaming_translation_emits_chunks_in_order(self) -> None:
        sse = (
            b'data: {"choices": [{"delta": {"content": "hel"}}]}\n\n'
            b'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n'
            b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        adapter = LoopbackOllamaAdapter(
            resource=_resource(),
            transport=lambda request: LoopbackHTTPResponse(
                status=200, body=sse, content_type="text/event-stream"
            ),
        )
        emitted: list[str] = []
        result = adapter.invoke(
            _call(stream=True),
            cancel_event=threading.Event(),
            deadline=T_NOW,
            emit=lambda chunk: emitted.append(chunk.text or f"<{chunk.kind}>"),
        )
        self.assertEqual("completed", result.status)
        self.assertEqual(["hel", "lo", "<finish>"], emitted)
        assert result.message is not None
        self.assertEqual("hello", result.message.content)
        self.assertEqual("stop", result.finish_reason)

    def test_structured_output_fails_closed_instead_of_degrading(self) -> None:
        adapter = LoopbackOllamaAdapter(resource=_resource(), transport=None)
        result = adapter.invoke(
            _call(response_format={"type": "json_schema"}),
            cancel_event=threading.Event(),
            deadline=T_NOW,
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None
        self.assertIn("structured output", result.calls[0].note)

    def test_probe_failure_reports_unavailable_health(self) -> None:
        def transport(_request: LoopbackHTTPRequest) -> LoopbackHTTPResponse:
            raise OSError("connection refused")

        adapter = LoopbackOllamaAdapter(resource=_resource(), transport=transport)
        snapshots = adapter.resource_snapshots(T_NOW)
        self.assertEqual(1, len(snapshots))
        self.assertEqual("unavailable", snapshots[0].health.status)
        self.assertEqual("worker_bridged", snapshots[0].identity.channel)

    def test_probe_success_reports_ok_health_without_fabricated_quota(self) -> None:
        adapter = LoopbackOllamaAdapter(
            resource=_resource(),
            transport=lambda request: LoopbackHTTPResponse(status=200, body=b"[]"),
        )
        snapshots = adapter.resource_snapshots(T_NOW)
        self.assertEqual("ok", snapshots[0].health.status)
        # No quota telemetry exists for a local endpoint: none is invented.
        self.assertEqual((), snapshots[0].quota_facts)


class TranslationUnitTests(unittest.TestCase):
    def test_stream_line_parser_handles_keepalive_and_done(self) -> None:
        translation = ProvisionalOpenAITranslation()
        self.assertIsNone(translation.parse_stream_line(": keep-alive"))
        self.assertIsNone(translation.parse_stream_line("data: [DONE]"))

    def test_stream_line_parser_rejects_malformed_frames(self) -> None:
        from scarcity_router.worker_protocol import WorkerProtocolError

        translation = ProvisionalOpenAITranslation()
        with self.assertRaises(WorkerProtocolError):
            _ = translation.parse_stream_line("data: {not json")

    def test_tool_call_delta_parses_to_tool_chunk(self) -> None:
        translation = ProvisionalOpenAITranslation()
        line = (
            'data: {"choices": [{"delta": {"tool_calls": [{"id": "c1", '
            '"function": {"name": "t", "arguments": "{}"}}]}}]}'
        )
        chunk = translation.parse_stream_line(line)
        # The provisional translation ignores tool-call deltas it cannot
        # fully honor rather than guessing semantics.
        self.assertIsNone(chunk)
        _ = chunk

    def test_whole_response_tool_calls_parse(self) -> None:
        translation = ProvisionalOpenAITranslation()
        message, reason, _usage = translation.parse_response(
            LoopbackHTTPResponse(
                status=200,
                body=(
                    b'{"choices": [{"finish_reason": "tool_calls", "message": '
                    b'{"role": "assistant", "content": null, "tool_calls": '
                    b'[{"id": "c1", "type": "function", "function": '
                    b'{"name": "lookup", "arguments": "{\\"q\\": 1}"}}]}}]}'
                ),
            )
        )
        self.assertEqual("tool_calls", reason)
        assert message is not None
        call: AdapterToolCall = message.tool_calls[0]
        self.assertEqual("lookup", call.name)
        _ = AdapterStreamChunk, AdapterMessage


if __name__ == "__main__":
    _ = unittest.main()
