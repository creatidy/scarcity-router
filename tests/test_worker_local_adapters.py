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
from scarcity_router.worker_protocol import WorkerProtocolError  # noqa: E402
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
    OpenAICompatibleLoopbackTranslation,
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
        # The refusal comes from the M04 core's evidence-backed policy:
        # json_schema has no evidenced mapping for the ollama preset.
        self.assertIn("response_format", result.calls[0].note)

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
    """The production translation is the shared M04 core (one implementation)."""

    def test_stream_session_handles_keepalive_and_done(self) -> None:
        translation = OpenAICompatibleLoopbackTranslation()
        session = translation.open_stream_session()
        self.assertEqual((), session.feed_line(": keep-alive"))
        self.assertEqual((), session.feed_line("data: [DONE]"))
        self.assertEqual((), session.close())

    def test_stream_session_rejects_malformed_frames(self) -> None:
        from scarcity_router.worker_protocol import WorkerProtocolError

        translation = OpenAICompatibleLoopbackTranslation()
        session = translation.open_stream_session()
        # The adapter feeds every line of the SSE body, blank separators
        # included; the malformed event fails on its terminating blank line.
        with self.assertRaises(WorkerProtocolError):
            _ = session.feed_line("data: {not json")
            _ = session.feed_line("")

    def test_deeply_nested_response_fails_closed(self) -> None:
        # json.loads raises RecursionError (not ValueError) on deeply
        # nested input; the translation must turn it into a typed failure
        # so the worker's execution thread survives (M05 review 1).
        translation = OpenAICompatibleLoopbackTranslation()
        with self.assertRaises(WorkerProtocolError):
            _ = translation.parse_response(
                LoopbackHTTPResponse(status=200, body=b"[" * 60000)
            )

    def test_stream_session_deeply_nested_frame_fails_closed(self) -> None:
        # The shared core's SSE json.loads raises a BARE RecursionError on
        # a deeply nested frame; the streaming adaptation layer must
        # re-type it exactly like the whole-document direction (wave
        # audit blocker) — never an untyped escape.
        translation = OpenAICompatibleLoopbackTranslation()
        session = translation.open_stream_session()
        with self.assertRaises(WorkerProtocolError):
            _ = session.feed_line("data: " + "[" * 60000)
            _ = session.feed_line("")

    def test_adapter_reports_failed_result_for_unparsable_response(self) -> None:
        adapter = LoopbackOllamaAdapter(
            resource=_resource(),
            transport=lambda _request: LoopbackHTTPResponse(
                status=200, body=b"[" * 60000
            ),
        )
        result = adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=T_NOW,
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None

    def test_adapter_streaming_deeply_nested_frame_yields_failed_result(self) -> None:
        # The streaming direction of the same hardening: a deep-nested SSE
        # data line through a REAL LoopbackOllamaAdapter invoke must yield
        # a typed failed AdapterResult — never a raised RecursionError
        # that would kill the worker's per-attempt execution thread (wave
        # audit MERGE_BLOCKER).
        adapter = LoopbackOllamaAdapter(
            resource=_resource(),
            transport=lambda _request: LoopbackHTTPResponse(
                status=200,
                body=('data: ' + "[" * 60000 + "\n\n").encode("utf-8"),
                content_type="text/event-stream",
            ),
        )
        result = adapter.invoke(
            _call(stream=True),
            cancel_event=threading.Event(),
            deadline=T_NOW,
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None

    def test_tool_call_fragments_accumulate_to_a_complete_call(self) -> None:
        translation = OpenAICompatibleLoopbackTranslation()
        session = translation.open_stream_session()
        chunks: tuple[AdapterStreamChunk, ...] = ()
        fragment_one = (
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, '
            '"id": "c1", "function": {"name": "lookup", '
            '"arguments": "{\\"q\\""}}]}}]}'
        )
        fragment_two = (
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, '
            '"function": {"arguments": ": 1}"}}]}}]}'
        )
        finish_frame = (
            'data: {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}'
        )
        for line in (
            fragment_one,
            "",
            fragment_two,
            "",
            finish_frame,
            "",
            "data: [DONE]",
            "",
        ):
            chunks = chunks + session.feed_line(line)
        chunks = chunks + session.close()
        # The core accumulates fragments; the normalized chunk carries one
        # COMPLETE tool call, never a fragment.
        tool_chunks = [chunk for chunk in chunks if chunk.kind == "tool_call"]
        self.assertEqual(1, len(tool_chunks))
        call = tool_chunks[0].tool_call
        assert call is not None
        self.assertEqual("lookup", call.name)
        self.assertEqual('{"q": 1}', call.arguments)

    def test_whole_response_tool_calls_parse(self) -> None:
        translation = OpenAICompatibleLoopbackTranslation()
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

    def test_streaming_request_carries_evidenced_usage_request(self) -> None:
        # The ollama preset evidences stream_options.include_usage; the
        # shared core adds it exactly as the server-direct adapter does.
        translation = OpenAICompatibleLoopbackTranslation()
        request = translation.build_stream_request(_call(stream=True))
        assert request.body is not None
        self.assertIn(b"stream_options", request.body)

    def test_default_policy_is_the_ollama_preset(self) -> None:
        from scarcity_router.providers.openai_http_presets import OLLAMA_PRESET

        translation = OpenAICompatibleLoopbackTranslation()
        self.assertEqual(OLLAMA_PRESET.policy, translation.policy)


if __name__ == "__main__":
    _ = unittest.main()
