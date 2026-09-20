"""Worker protocol v1 tests (M05): framing, negotiation, strict parsing.

Deterministic: no sockets, no threads, no wall clock. Covers bounded
framing, strict JSON parsing, the closed message vocabulary (including
rejection of any unknown/arbitrary command type), version negotiation
failures, and the typed AdapterCall/chunk/observation round-trips the two
ends share.
"""

from __future__ import annotations

import unittest

from scarcity_router.gateway_adapters import (
    AdapterCall,
    AdapterMessage,
    AdapterStreamChunk,
    AdapterToolCall,
    CallObservation,
)
from scarcity_router.gateway_contracts import UsageTokens
from scarcity_router.resource_state import ResourceIdentity
from scarcity_router.selection_types import ModelIdentity
from scarcity_router.worker_protocol import (
    ERR_MALFORMED,
    ERR_PROTOCOL_VERSION,
    ERR_UNKNOWN_MESSAGE,
    MAX_FRAME_BYTES,
    MSG_HELLO,
    MSG_STATE_REPORT,
    AttemptInterruptedMessage,
    StateReportMessage,
    ExecuteMessage,
    ExecuteResultMessage,
    FrameReader,
    HelloMessage,
    WorkerProtocolError,
    adapter_call_from_dict,
    adapter_call_to_dict,
    call_observation_from_dict,
    call_observation_to_dict,
    chunk_from_dict,
    chunk_to_dict,
    decode_frame,
    encode_frame,
    negotiate_version,
    parse_server_message,
    parse_worker_message,
)


class _BytesTransport:
    """A trivial byte carrier for framing tests."""

    _buffer: bytearray

    def __init__(self, data: bytes = b"") -> None:
        self._buffer = bytearray(data)
        self.sent: list[bytes] = []

    def recv_exact(self, size: int) -> bytes | None:
        if not self._buffer:
            return None
        data = bytes(self._buffer[:size])
        del self._buffer[: min(size, len(self._buffer))]
        return data if data else None

    def send_all(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        return None


def _hello_payload() -> dict[str, object]:
    return {
        "type": MSG_HELLO,
        "worker_id": "w-abc123",
        "credential": "SYNTHETIC-CREDENTIAL",
        "supported_versions": [1],
    }


class FramingTests(unittest.TestCase):
    def test_frame_round_trip(self) -> None:
        payload = _hello_payload()
        transport = _BytesTransport(encode_frame(payload))
        decoded = FrameReader(transport).read_message()
        self.assertEqual(payload, decoded)

    def test_writer_produces_bounded_length_prefix(self) -> None:
        encoded = encode_frame({"a": 1})
        length = int.from_bytes(encoded[:4], "big")
        self.assertEqual(len(encoded) - 4, length)
        self.assertLessEqual(length, MAX_FRAME_BYTES)

    def test_truncated_payload_is_rejected(self) -> None:
        encoded = encode_frame(_hello_payload())
        transport = _BytesTransport(encoded[:-5])
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = FrameReader(transport).read_message()
        self.assertEqual(ERR_MALFORMED, caught.exception.code)

    def test_oversized_declared_length_is_rejected(self) -> None:
        header = (MAX_FRAME_BYTES + 1).to_bytes(4, "big")
        transport = _BytesTransport(header)
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = FrameReader(transport).read_message()
        self.assertEqual("frame_too_large", caught.exception.code)

    def test_oversized_payload_encoding_is_rejected(self) -> None:
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = encode_frame({"blob": "x" * (MAX_FRAME_BYTES + 1)})
        self.assertEqual("frame_too_large", caught.exception.code)

    def test_non_utf8_payload_is_rejected(self) -> None:
        payload = b"\xff\xfe\x00"
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = decode_frame(payload)
        self.assertEqual(ERR_MALFORMED, caught.exception.code)

    def test_duplicate_keys_are_rejected(self) -> None:
        text = b'{"type": "hello", "type": "hello"}'
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = decode_frame(text)
        self.assertEqual(ERR_MALFORMED, caught.exception.code)

    def test_non_object_payload_is_rejected(self) -> None:
        with self.assertRaises(WorkerProtocolError):
            _ = decode_frame(b"[1, 2, 3]")

    def test_clean_eof_returns_none(self) -> None:
        self.assertIsNone(FrameReader(_BytesTransport()).read_message())


class NegotiationTests(unittest.TestCase):
    def test_common_version_is_selected(self) -> None:
        self.assertEqual(2, negotiate_version((1, 2), (2, 1)))

    def test_highest_common_version_wins(self) -> None:
        self.assertEqual(3, negotiate_version((1, 2, 3), (3, 2)))

    def test_disjoint_versions_fail_safely(self) -> None:
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = negotiate_version((1,), (2,))
        self.assertEqual(ERR_PROTOCOL_VERSION, caught.exception.code)

    def test_empty_supported_list_is_malformed(self) -> None:
        payload = _hello_payload()
        payload["supported_versions"] = []
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = HelloMessage.from_payload(payload)
        self.assertEqual(ERR_MALFORMED, caught.exception.code)


class VocabularyTests(unittest.TestCase):
    def test_unknown_worker_message_type_is_rejected(self) -> None:
        for arbitrary in ("shell", "ssh", "exec", "run_command", "file_read"):
            with self.assertRaises(WorkerProtocolError) as caught:
                _ = parse_worker_message({"type": arbitrary, "command": "anything"})
            self.assertEqual(ERR_UNKNOWN_MESSAGE, caught.exception.code)

    def test_unknown_server_message_type_is_rejected(self) -> None:
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = parse_server_message({"type": "shell"})
        self.assertEqual(ERR_UNKNOWN_MESSAGE, caught.exception.code)

    def test_unknown_keys_are_rejected(self) -> None:
        payload = _hello_payload()
        payload["environment"] = {"PATH": "/evil"}
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = HelloMessage.from_payload(payload)
        self.assertEqual(ERR_MALFORMED, caught.exception.code)

    def test_error_codes_are_closed(self) -> None:
        from scarcity_router.worker_protocol import ErrorMessage

        payload = {"type": "error", "code": "totally_new", "message": "x", "fatal": True}
        with self.assertRaises(WorkerProtocolError):
            _ = ErrorMessage.from_payload(payload)

    def test_execute_result_status_is_closed(self) -> None:
        payload: dict[str, object] = {
            "type": "execute_result",
            "attempt_id": "wa-1",
            "status": "exploded",
            "calls": [],
        }
        with self.assertRaises(WorkerProtocolError) as caught:
            _ = ExecuteResultMessage.from_payload(payload)
        self.assertEqual(ERR_MALFORMED, caught.exception.code)


class SerializationTests(unittest.TestCase):
    def _sample_call(self) -> AdapterCall:
        resource = ResourceIdentity(
            resource_id="synthetic-resource",
            channel="worker_bridged",
            provider="synthetic",
            model="syn-model",
            entitlement="local_ungated",
        )
        return AdapterCall(
            resource=resource,
            model=ModelIdentity(provider="openai", model="syn-model", variant="max"),
            messages=(
                AdapterMessage(role="user", content="hello"),
                AdapterMessage(
                    role="assistant",
                    content=None,
                    tool_calls=(
                        AdapterToolCall(id="call-1", name="tool", arguments='{"a": 1}'),
                    ),
                ),
            ),
            stream=True,
            tools=({"type": "function", "function": {"name": "tool"}},),
            tool_choice="auto",
            max_output_tokens=128,
            generation_params={"temperature": 0.5},
        )

    def test_adapter_call_round_trip(self) -> None:
        call = self._sample_call()
        rebuilt = adapter_call_from_dict(adapter_call_to_dict(call))
        self.assertEqual(call.resource, rebuilt.resource)
        self.assertEqual(call.model, rebuilt.model)
        self.assertEqual(len(call.messages), len(rebuilt.messages))
        self.assertEqual(call.messages[1].tool_calls[0].name, rebuilt.messages[1].tool_calls[0].name)
        self.assertTrue(rebuilt.stream)
        self.assertEqual(128, rebuilt.max_output_tokens)

    def test_chunk_round_trip(self) -> None:
        chunks = (
            AdapterStreamChunk(kind="text_delta", text="hello"),
            AdapterStreamChunk(
                kind="tool_call", tool_call=AdapterToolCall(id="c1", name="t", arguments="{}")
            ),
            AdapterStreamChunk(kind="finish", finish_reason="stop"),
            AdapterStreamChunk(kind="usage", usage=UsageTokens(3, 4)),
        )
        for chunk in chunks:
            rebuilt = chunk_from_dict(chunk_to_dict(chunk))
            self.assertEqual(chunk, rebuilt)

    def test_call_observation_round_trip(self) -> None:
        observation = CallObservation(
            call_index=0,
            started_at="2026-09-20T12:00:00.000Z",
            ended_at="2026-09-20T12:00:01.000Z",
            status="completed",
            provider_reported_usage=UsageTokens(11, 7),
        )
        rebuilt = call_observation_from_dict(call_observation_to_dict(observation))
        self.assertEqual(observation, rebuilt)

    def test_execute_message_round_trip(self) -> None:
        call = self._sample_call()
        message = ExecuteMessage(
            request_id="chatcmpl-1",
            attempt_id="wa-abc",
            adapter_id="synthetic",
            deadline="2026-09-20T12:05:00.000Z",
            call=call,
        )
        rebuilt = parse_server_message(message.to_payload())
        assert isinstance(rebuilt, ExecuteMessage)
        self.assertEqual(message.attempt_id, rebuilt.attempt_id)
        self.assertEqual(message.call.resource, rebuilt.call.resource)

    def test_state_report_message_carries_document(self) -> None:
        payload = {
            "type": MSG_STATE_REPORT,
            "report": {"schema_version": 1, "worker_id": "w-1"},
        }
        message = parse_worker_message(payload)
        assert isinstance(message, StateReportMessage)
        report_document = dict(message.report)
        self.assertEqual({"schema_version": 1, "worker_id": "w-1"}, report_document)


class AttemptIdentityTests(unittest.TestCase):
    def test_attempt_interrupted_message_round_trip(self) -> None:
        message = AttemptInterruptedMessage(attempt_id="wa-abc123")
        rebuilt = parse_worker_message(message.to_payload())
        assert isinstance(rebuilt, AttemptInterruptedMessage)
        self.assertEqual("wa-abc123", rebuilt.attempt_id)

    def test_attempt_ids_follow_safe_grammar(self) -> None:
        with self.assertRaises(WorkerProtocolError):
            _ = parse_server_message({"type": "cancel", "attempt_id": "BAD ID!"})


if __name__ == "__main__":
    _ = unittest.main()
