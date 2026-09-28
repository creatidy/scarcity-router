"""Coordinator-level D-060 continuation tests (#137).

The composed HTTP -> worker -> fake-App-Server round trip lives in the
Codex acceptance suite; this file pins the COORDINATOR continuation
semantics in isolation: exact-request validation (fingerprinted model,
effort, tools, tool_choice, conversation prefix, assistant echo),
the typed rejection vocabulary (mismatch / not-found / conflict), the
single-claim discipline under a repeated result, and the sticky
no-routing property — a continuation resolves without ANY routing I/O,
so competitive policy, scarcity and D-059 schedule changes can never
move a suspended turn. Deterministic: injected clocks, in-memory fakes.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import cast, override

from scarcity_router.gateway_adapters import (
    AdapterMessage,
    AdapterResult,
    CallObservation,
    ContinuationLostError,
    ExecutionContext,
    SuspensionHandle,
    ToolSuspension,
)
from scarcity_router.gateway_adapters import ExecutionAdapter
from scarcity_router.gateway_contracts import GatewayError
from scarcity_router.gateway_audit import ExecutedTarget
from scarcity_router.gateway_continuation import (
    PendingContinuation,
    ContinuationRegistry,
    message_fingerprint,
    new_continuation_token,
    tools_fingerprint,
)
from tests.gateway_fixtures import (
    CLIENT_ID,
    GatewayApplication,
    audit_records,
    make_application,
    parse_chat_request,
)

PREFIX_MESSAGES = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "SECRET-PREFIX-CONTENT"},
]
TOOL_TOKEN = "srct-" + "a" * 32
TOOL_NAME = "synthetic_lookup"
TOOL_ARGUMENTS = '{"n": 0}'

_DEADLINE = datetime.now(timezone.utc) + timedelta(seconds=600)


def _canonical_request_document(
    *,
    token: str = TOOL_TOKEN,
    model: str = "openai-worker-model",
    tools: list[dict[str, object]] | None = None,
    tool_choice: object = "auto",
    content: str = "TOOL-RESULT",
) -> dict[str, object]:
    document: dict[str, object] = {
        "model": model,
        "messages": [
            *PREFIX_MESSAGES,
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": token,
                        "type": "function",
                        "function": {"name": TOOL_NAME, "arguments": TOOL_ARGUMENTS},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": token, "content": content},
        ],
        "tool_choice": tool_choice,
    }
    if tools is not None:
        document["tools"] = tools
    return document


_TOOL_DECLARATION = {
    "type": "function",
    "function": {"name": TOOL_NAME, "parameters": {"type": "object"}},
}


class _FakeContinuationAdapter:
    """Records `deliver_tool_result` and returns a terminal result."""

    channel: str = "worker_bridged"
    adapter_name: str = "fake-continuation"
    adapter_version: str = "1.0.0"

    def __init__(self) -> None:
        self.delivered: list[tuple[str, str]] = []
        self.next_result: AdapterResult | ToolSuspension | Exception = AdapterResult(
            status="completed",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at="2026-09-28T12:00:00.000Z",
                    ended_at="2026-09-28T12:00:05.000Z",
                    status="completed",
                ),
            ),
            message=AdapterMessage(role="assistant", content="FINAL"),
            finish_reason="stop",
        )

    def suspension_handle(self, token: str) -> SuspensionHandle | None:
        return SuspensionHandle(
            continuation_token=token,
            attempt_id="wa-1",
            resource_id="openai-worker",
            call_id="call-internal-1",
            tool_name=TOOL_NAME,
        )

    def deliver_tool_result(
        self, handle: SuspensionHandle, content: str, context: ExecutionContext
    ) -> AdapterResult | ToolSuspension:
        _ = context
        self.delivered.append((handle.call_id, content))
        outcome = self.next_result
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def cancel_suspension(self, handle: SuspensionHandle) -> None:
        _ = handle

    def suspension_alive(self, handle: SuspensionHandle) -> bool:
        _ = handle
        return True


def _application_with_continuation(
    registry: ContinuationRegistry,
    adapter: _FakeContinuationAdapter,
) -> GatewayApplication:
    # The test fake satisfies the ExecutionAdapter seam structurally.
    return make_application(
        adapters=cast("tuple[ExecutionAdapter, ...]", (adapter,)),
        continuations=registry,
    )


def _registered_record(
    document: dict[str, object],
    registry: ContinuationRegistry,
    adapter: _FakeContinuationAdapter | None = None,
) -> PendingContinuation:
    """The record the initial dispatch would have registered."""
    request = parse_chat_request(document)
    messages = request.messages
    record = PendingContinuation(
        continuation_token=TOOL_TOKEN,
        attempt_id="wa-1",
        resource_id="openai-worker",
        channel="worker_bridged",
        call_id="call-internal-1",
        tool_name=TOOL_NAME,
        deadline=_DEADLINE,
        created_at="2026-09-28T12:00:00Z",
        client_id=CLIENT_ID,
        model_echo=request.model,
        reasoning_effort=request.reasoning_effort,
        tools_fingerprint=tools_fingerprint(request.tools),
        tool_choice_json=(
            json.dumps(request.tool_choice, sort_keys=True, separators=(",", ":"))
            if request.tool_choice is not None
            else None
        ),
        prefix_fingerprint=message_fingerprint(messages[:-2]),
        assistant_tool_calls=messages[-2].tool_calls,
        handle=(
            adapter.suspension_handle(TOOL_TOKEN) if adapter is not None else None
        ),
        selected_target=ExecutedTarget(
            resource_id="openai-worker",
            provider="openai",
            model="gpt-5.6-luna",
            variant="high",
        ),
        executed_target=ExecutedTarget(
            resource_id="openai-worker",
            provider="openai",
            model="gpt-5.6-luna",
            variant="high",
        ),
    )
    _ = registry.register(record)
    return record


class ContinuationLifecycleTests(unittest.TestCase):
    registry: ContinuationRegistry
    adapter: "_FakeContinuationAdapter"
    application: GatewayApplication

    def __init__(self, method_name: str = "runTest") -> None:
        # Placeholders; setUp replaces them before each test body runs.
        self.registry = cast("ContinuationRegistry", object())
        self.adapter = cast("_FakeContinuationAdapter", object())
        self.application = cast("GatewayApplication", object())
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self.registry = ContinuationRegistry()
        self.adapter = _FakeContinuationAdapter()
        self.application = _application_with_continuation(
            self.registry, self.adapter
        )

    def test_valid_continuation_resumes_without_any_routing(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        request = parse_chat_request(document)
        outcome = self.application.execute(client_id=CLIENT_ID, request=request)
        self.assertEqual("stop", outcome.finish_reason)
        self.assertEqual("FINAL", outcome.message.content)
        self.assertEqual(
            [("call-internal-1", "TOOL-RESULT")], self.adapter.delivered
        )
        # Sticky: the exact target/decision of the ORIGINAL dispatch are
        # repeated, and the registry entry is terminal.
        self.assertEqual(0, self.registry.pending_count())
        record = audit_records(self.application)[-1]
        self.assertEqual("completed", record.result_status)

    def test_continuation_does_not_read_capacity_or_policy_state(self) -> None:
        # Break every admission input: a normal request now fails 503,
        # while the continuation still resolves — proof that no routing,
        # no capacity read and no policy re-evaluation runs on the
        # continuation leg (D-059: policy changes cannot move a turn).
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        self.application.capacity_source = (
            lambda now: (_ for _ in ()).throw(ValueError("must not be read"))
        )
        with self.assertRaises(GatewayError):
            _ = self.application.execute(
                client_id=CLIENT_ID,
                request=parse_chat_request(
                    {"model": "deep-coding", "messages": [_PREFIX_ALONE]}
                ),
            )
        outcome = self.application.execute(
            client_id=CLIENT_ID, request=parse_chat_request(document)
        )
        self.assertEqual("stop", outcome.finish_reason)

    def test_changed_tools_are_rejected(self) -> None:
        document = _canonical_request_document(
            tools=[dict(_TOOL_DECLARATION)],
        )
        _ = _registered_record(document, self.registry, self.adapter)
        mutated = _canonical_request_document(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "parameters": {"type": "object", "properties": {"x": {}}},
                    },
                }
            ]
        )
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(mutated)
            )
        self.assertEqual(400, caught.exception.http_status)
        self.assertEqual("continuation_mismatch", caught.exception.code)
        # The suspended turn is untouched: the corrected request can
        # still continue it.
        outcome = self.application.execute(
            client_id=CLIENT_ID, request=parse_chat_request(document)
        )
        self.assertEqual("stop", outcome.finish_reason)

    def test_changed_prefix_is_rejected(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        mutated = _canonical_request_document()
        mutated_messages = cast("list[dict[str, object]]", mutated["messages"])
        mutated["messages"] = [
            *PREFIX_MESSAGES[:-1],
            {"role": "user", "content": "REWRITTEN-CONTENT"},
            *mutated_messages[len(PREFIX_MESSAGES) :],
        ]
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(mutated)
            )
        self.assertEqual("continuation_mismatch", caught.exception.code)

    def test_substituted_assistant_tool_calls_are_rejected(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        forged = _canonical_request_document()
        messages = cast("list[dict[str, object]]", forged["messages"])
        calls = cast("list[dict[str, object]]", messages[-2]["tool_calls"])
        # Same token, rewritten echo: the gateway must not trust the
        # client's repetition of the assistant tool_calls it returned.
        function = cast("dict[str, object]", calls[0]["function"])
        function["arguments"] = '{"rewritten": true}'
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(forged)
            )
        self.assertEqual("continuation_mismatch", caught.exception.code)
        # ...and nothing was delivered to the suspended execution.
        self.assertEqual([], self.adapter.delivered)

    def test_foreign_client_is_not_found(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id="someone-else", request=parse_chat_request(document)
            )
        self.assertEqual(404, caught.exception.http_status)
        self.assertEqual("continuation_not_found", caught.exception.code)

    def test_expired_continuation_is_typed(self) -> None:
        document = _canonical_request_document()
        record = _registered_record(document, self.registry)
        record.deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(document)
            )
        self.assertEqual("continuation_expired", caught.exception.code)

    def test_replayed_result_is_a_conflict(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        first = self.application.execute(
            client_id=CLIENT_ID, request=parse_chat_request(document)
        )
        self.assertEqual("stop", first.finish_reason)
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(document)
            )
        self.assertEqual(409, caught.exception.http_status)
        self.assertEqual("continuation_already_resolved", caught.exception.code)
        # Delivered exactly once.
        self.assertEqual(1, len(self.adapter.delivered))

    def test_lost_suspension_is_typed_not_found(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        self.adapter.next_result = ContinuationLostError("gone")
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(document)
            )
        self.assertEqual(404, caught.exception.http_status)
        self.assertEqual("continuation_not_found", caught.exception.code)

    def test_sequential_tool_round_registers_the_next_continuation(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        next_token = new_continuation_token()
        self.adapter.next_result = ToolSuspension(
            continuation_token=next_token,
            tool_name="second_tool",
            arguments='{"step": 2}',
            content="FINAL-LEG-1",
        )
        outcome = self.application.execute(
            client_id=CLIENT_ID, request=parse_chat_request(document)
        )
        self.assertEqual("tool_calls", outcome.finish_reason)
        self.assertEqual("second_tool", outcome.message.tool_calls[0].name)
        pending = self.registry.detect(next_token)
        self.assertIsNotNone(pending)
        record = audit_records(self.application)[-1]
        self.assertIn("suspended_for_client_tool", record.reason_codes)
        self.assertIn("continuation_resumed", record.reason_codes)


_PREFIX_ALONE = PREFIX_MESSAGES[1]


if __name__ == "__main__":
    _ = unittest.main()
