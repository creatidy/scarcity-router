"""Coordinator-level D-062 continuation tests (#137).

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
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast, override

from scarcity_router.gateway_adapters import (
    AdapterRegistry,
    AdapterAmbiguousError,
    AdapterCall,
    AdapterMessage,
    AdapterPermanentError,
    AdapterResult,
    AdapterTimeoutError,
    AdapterToolCall,
    CallObservation,
    ContinuationLostError,
    ExecutionContext,
    SuspensionHandle,
    ToolSuspension,
)
from scarcity_router.gateway_adapters import ExecutionAdapter
from scarcity_router.capacity import CapacityWindow
from scarcity_router.execution_assurance import NON_PAID_MODES, ExecutionControlScope, NonPaidControlEvidence
from scarcity_router.gateway_contracts import GatewayError
from scarcity_router.resource_state import (
    ExecutionCapabilities,
    QuotaFact,
    ResourceHealth,
    ResourceIdentity,
    ResourceRegistration,
    ResourceRegistry,
    ResourceStateSnapshot,
)
from scarcity_router.routing_core import AdministratorConstraints, ClientAuthorization
from scarcity_router.gateway_audit import ExecutedTarget
from scarcity_router.gateway_continuation import (
    PendingContinuation,
    ContinuationRegistry,
    message_fingerprint,
    new_continuation_token,
    tool_calls_fingerprint,
    tools_fingerprint,
)
from tests.gateway_fixtures import (
    CLIENT_ID,
    T_EVAL,
    ChatCompletionRequest,
    GatewayApplication,
    audit_records,
    build_registry,
    build_cells,
    canonical,
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

    _call_id: str = "call-internal-1"
    _next_call_id: str = "call-internal-2"

    def __init__(self) -> None:
        self.delivered: list[tuple[str, str]] = []
        self.binding: tuple[str, ...] = ("endpoint-test", "worker-test", "adapter-test")
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

    def continuation_binding(self, _resource_id: str) -> tuple[str, ...] | None:
        return self.binding

    def suspension_handle(self, continuation_token: str) -> SuspensionHandle | None:
        return SuspensionHandle(
            continuation_token=continuation_token,
            attempt_id="wa-1",
            resource_id="openai-worker",
            call_id=self._call_id,
            tool_name=TOOL_NAME,
        )

    def execute(self, call: AdapterCall, context: ExecutionContext) -> AdapterResult:
        """The D-062 dispatch contract: register BEFORE exposure."""
        from scarcity_router.gateway_adapters import FINISH_TOOL_CALLS

        if not call.tools:
            return AdapterResult(
                status="completed",
                calls=(
                    CallObservation(
                        call_index=0,
                        started_at="2026-09-28T12:00:00.000Z",
                        ended_at="2026-09-28T12:00:01.000Z",
                        status="completed",
                    ),
                ),
                message=AdapterMessage(role="assistant", content="FINAL"),
                finish_reason="stop",
            )
        token = new_continuation_token()
        handle = SuspensionHandle(
            continuation_token=token,
            attempt_id="wa-dispatch",
            resource_id=call.resource.resource_id,
            call_id=self._call_id,
            tool_name=TOOL_NAME,
        )
        arguments = "{}"
        registrar = context.register_continuation
        if registrar is None or not registrar(handle, arguments):
            # Bounded/no-surface failure: cancel and expose NOTHING.
            raise AdapterPermanentError(
                "the gateway cannot continue this client-tool turn"
            )
        return AdapterResult(
            status="completed",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at="2026-09-28T12:00:00.000Z",
                    ended_at="2026-09-28T12:00:01.000Z",
                    status="unknown",
                    note="the backend turn is suspended for a tool call",
                ),
            ),
            message=AdapterMessage(
                role="assistant",
                tool_calls=(
                    AdapterToolCall(
                        id=token,
                        name=TOOL_NAME,
                        arguments=arguments,
                    ),
                ),
            ),
            finish_reason=FINISH_TOOL_CALLS,
        )

    def deliver_tool_result(
        self, handle: SuspensionHandle, content: str, context: ExecutionContext
    ) -> AdapterResult | ToolSuspension:
        _ = handle
        self.delivered.append((self._call_id, content))
        outcome = self.next_result
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, ToolSuspension):
            # Mirror the real contract: the next continuation is
            # registered through the context's registrar BEFORE the new
            # token becomes observable (review round 2, finding 4).
            next_handle = SuspensionHandle(
                continuation_token=outcome.continuation_token,
                attempt_id="wa-1",
                resource_id="openai-worker",
                call_id=self._next_call_id,
                tool_name=outcome.tool_name,
            )
            registrar = context.register_continuation
            if registrar is None or not registrar(
                next_handle, outcome.arguments
            ):
                raise ContinuationLostError(
                    "the gateway cannot register the sequential round"
                )
        return outcome

    def cancel_suspension(self, handle: SuspensionHandle) -> None:
        _ = handle

    def suspension_alive(self, handle: SuspensionHandle) -> bool:
        _ = handle
        return True


def _application_with_continuation(
    registry: ContinuationRegistry,
    adapter: _FakeContinuationAdapter,
    **overrides: object,
) -> GatewayApplication:
    # The test fake satisfies the ExecutionAdapter seam structurally.
    # The registry includes the worker_bridged resource so the F6
    # authority recheck can resolve the ORIGINAL target's entry.
    return make_application(
        registry=build_registry(with_worker=True),
        adapters=cast("tuple[ExecutionAdapter, ...]", (adapter,)),
        continuations=registry,
        **overrides,  # pyright: ignore[reportArgumentType] - typed keyword helper
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
        assistant_tool_calls_digest=tool_calls_fingerprint(
            messages[-2].tool_calls
        ),
        handle=(
            adapter.suspension_handle(TOOL_TOKEN) if adapter is not None else None
        ),
        adapter=adapter,
        adapter_binding=adapter.continuation_binding("openai-worker") if adapter is not None else None,
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


class DetectionShapeTests(unittest.TestCase):
    """Review-round regression pins (PR #159 round 1 blockers)."""

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

    def _deliver_one_round(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        _ = self.application.execute(
            client_id=CLIENT_ID, request=parse_chat_request(document)
        )

    def test_follow_up_turn_after_a_round_is_not_a_replay(self) -> None:
        # A conforming full-history harness resends the whole
        # conversation on its next turn; the consumed tool result stays
        # in the middle of the history. That request must route
        # normally — never a tombstone 409 (review blocker 1).
        self._deliver_one_round()
        delivered_before = len(self.adapter.delivered)
        follow_up: dict[str, object] = {
            "model": "openai-worker-model",
            "messages": [
                *PREFIX_MESSAGES,
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": TOOL_TOKEN,
                            "type": "function",
                            "function": {
                                "name": TOOL_NAME,
                                "arguments": TOOL_ARGUMENTS,
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": TOOL_TOKEN,
                    "content": "TOOL-RESULT",
                },
                {"role": "assistant", "content": "FINAL"},
                {"role": "user", "content": "SECRET-NEXT-TURN"},
            ],
        }
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(follow_up)
            )
        # Normal routing ran (this fixture's model has no route); the
        # point is the absence of the continuation vocabulary.
        self.assertNotIn(
            caught.exception.code,
            {"continuation_already_resolved", "continuation_not_found"},
        )
        self.assertEqual(delivered_before, len(self.adapter.delivered))

    def test_delivering_result_to_a_lost_suspension_is_not_found(self) -> None:
        # Gateway restart (fresh registry): a result addressed to a
        # gateway-issued token must be the typed 404 — never fed to a
        # fresh model selection (review blocker 2).
        document = _canonical_request_document()
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(document)
            )
        self.assertEqual(404, caught.exception.http_status)
        self.assertEqual("continuation_not_found", caught.exception.code)
        self.assertEqual([], self.adapter.delivered)

    def test_mid_history_token_without_delivery_shape_is_normal_flow(self) -> None:
        # A tombstoned/srct token cited mid-history (not as the final
        # result message) is conversation history, not a delivery.
        self._deliver_one_round()
        citing: dict[str, object] = {
            "model": "openai-worker-model",
            "messages": [
                *PREFIX_MESSAGES,
                {"role": "assistant", "content": "cites " + TOOL_TOKEN},
                {"role": "user", "content": "continue"},
            ],
        }
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(citing)
            )
        self.assertNotIn(
            caught.exception.code,
            {"continuation_already_resolved", "continuation_not_found"},
        )

    def test_sequential_round_closes_the_superseded_token(self) -> None:
        # Review DEFER 3: the superseded token must reach a terminal
        # state when its result is consumed by a sequential round, so a
        # very-late replay gets the precise 409 and the slot is freed.
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        next_token = new_continuation_token()
        self.adapter.next_result = ToolSuspension(
            continuation_token=next_token,
            tool_name="second_tool",
            arguments="{}",
            content=None,
        )
        _ = self.application.execute(
            client_id=CLIENT_ID, request=parse_chat_request(document)
        )
        # The superseded token is terminal; a replay conflicts precisely.
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(document)
            )
        self.assertEqual(409, caught.exception.http_status)
        self.assertEqual("continuation_already_resolved", caught.exception.code)
        # And exactly ONE pending record remains (the new token's).
        self.assertEqual(1, self.registry.pending_count())


class WorkerContinuationEligibilityTests(unittest.TestCase):
    """Finding 3: the LIVE v3 worker fact gates tool requests BEFORE
    ranking; ordinary requests are unaffected."""

    resource_ids: tuple[str, ...] = ("worker-a", "worker-b")

    def _two_worker_registry(self) -> ResourceRegistry:
        """Two healthy worker_bridged registrations of the SAME identity
        (built from the public registration/observation contracts — the
        fixtures' private helpers are not imported)."""
        from tests.gateway_fixtures import T_NOW, T_OBS

        registry = ResourceRegistry(clock=lambda: "2026-09-28T12:00:00Z")
        for resource_id in self.resource_ids:
            identity = ResourceIdentity(
                resource_id=resource_id,
                channel="worker_bridged",
                provider="openai",
                model="gpt-5.6-luna",
                entitlement="subscription_included",
                variant=None,
                quota_pool_ids=(),
            )
            registry.register(
                ResourceRegistration(
                    identity=identity,
                    freshness_ttl_seconds=3600,
                    capabilities=ExecutionCapabilities(
                        context_limit_tokens=272_000
                    ),
                )
            )
            registry.apply_snapshot(
                ResourceStateSnapshot(
                    schema_version=1,
                    identity=identity,
                    observed_at=T_OBS,
                    health=ResourceHealth(status="ok", diagnostics=()),
                    quota_facts=(
                        QuotaFact(
                            observation_class="provider_telemetry",
                            window=CapacityWindow(
                                resource="tokens",
                                kind="five_hour",
                                scope_id="codex",
                                duration_seconds=18_000,
                                used_percent=50,
                                remaining_percent=50,
                            ),
                        ),
                    ),
                    promotions=(),
                )
            )
            _ = T_NOW
        return registry

    def _application(
        self, capable: "frozenset[str] | None"
    ) -> GatewayApplication:
        from tests.gateway_fixtures import build_cells

        cells = build_cells(
            overrides={
                ("worker_bridged", "openai", "gpt-5.6-luna", "tool_calls"): "PARTIAL",
            },
            include_worker=True,
        )
        return make_application(
            registry=self._two_worker_registry(),
            adapters=cast(
                "tuple[ExecutionAdapter, ...]", (_FakeContinuationAdapter(),)
            ),
            cells=cells,
            continuations=ContinuationRegistry(),
            continuation_capability_source=(
                (lambda: capable) if capable is not None else None
            ),
        )

    def _tool_request(self) -> "ChatCompletionRequest":
        return parse_chat_request(
            {
                "model": "gpt-5.6-luna",
                "messages": [_PREFIX_ALONE],
                "reasoning_effort": "max",
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": TOOL_NAME,
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            }
        )

    def test_v3_route_is_selected_over_the_otherwise_equal_v2_route(self) -> None:
        # Both routes serve the exact same identity; only worker-b has a
        # live v3 worker. The tool-bearing request must be pre-ranking
        # ineligible on worker-a and EXECUTE on worker-b — never select
        # A and fail at the backend.
        application = self._application(frozenset({"worker-b"}))
        outcome = application.execute(
            client_id=CLIENT_ID, request=self._tool_request()
        )
        self.assertEqual("tool_calls", outcome.finish_reason)
        audit = audit_records(application)[-1]
        assert audit.executed_target is not None
        self.assertEqual("worker-b", audit.executed_target.resource_id)

    def test_no_v3_fact_fails_tool_requests_closed(self) -> None:
        for capable in (frozenset[str](), None):
            with self.subTest(capable=capable is not None):
                application = self._application(capable)
                with self.assertRaises(GatewayError) as caught:
                    _ = application.execute(
                        client_id=CLIENT_ID, request=self._tool_request()
                    )
                self.assertEqual(503, caught.exception.http_status)
                self.assertEqual("no_eligible_target", caught.exception.code)

    def test_non_tool_requests_still_use_the_v2_worker(self) -> None:
        application = self._application(frozenset())
        outcome = application.execute(
            client_id=CLIENT_ID,
            request=parse_chat_request(
                {
                    "model": "gpt-5.6-luna",
                    "messages": [_PREFIX_ALONE],
                    "reasoning_effort": "max",
                }
            ),
        )
        self.assertEqual("stop", outcome.finish_reason)
        audit = audit_records(application)[-1]
        assert audit.executed_target is not None
        self.assertEqual("worker-a", audit.executed_target.resource_id)


class ReviewRound2Tests(unittest.TestCase):
    """Regression pins for the owner review of PR #159 (findings 1-6)."""

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

    def _deliver_one_round(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        _ = self.application.execute(
            client_id=CLIENT_ID, request=parse_chat_request(document)
        )

    # ── Finding 2: no raw tool arguments in retained state ───────────

    def test_continuation_state_retains_no_raw_arguments(self) -> None:
        document = _canonical_request_document(content="SECRET-RESULT-VALUE")
        _ = _registered_record(document, self.registry, self.adapter)
        record = self.registry.detect(TOOL_TOKEN)
        self.assertIsNotNone(record)
        live = cast(PendingContinuation, record)
        rendered = repr(live)
        for secret in (
            "SECRET-ARG-VALUE-42",
            "SECRET-RESULT-VALUE",
            "SECRET-PREFIX-CONTENT",
        ):
            self.assertNotIn(secret, rendered)
        self.assertFalse(hasattr(live, "assistant_tool_calls"))
        self.assertEqual(64, len(live.assistant_tool_calls_digest))

    # ── Finding 5: every post-claim exit reaches terminal cleanup ─────

    def test_every_post_claim_error_path_closes_the_record(self) -> None:
        cases: dict[str, Exception] = {
            "permanent": AdapterPermanentError("backend failed"),
            "ambiguous": AdapterAmbiguousError("may have been consumed"),
            "timeout": AdapterTimeoutError("deadline"),
            "lost": ContinuationLostError("gone before delivery"),
            "unexpected": RuntimeError("internal defect"),
        }
        for label, failure in cases.items():
            with self.subTest(case=label):
                registry = ContinuationRegistry()
                adapter = _FakeContinuationAdapter()
                application = _application_with_continuation(registry, adapter)
                adapter.next_result = failure
                document = _canonical_request_document()
                _ = _registered_record(document, registry, adapter)
                with self.assertRaises(Exception):  # noqa: B017 - any failure closes
                    _ = application.execute(
                        client_id=CLIENT_ID,
                        request=parse_chat_request(document),
                    )
                # The pending table is empty IMMEDIATELY: no record sits
                # in RESUMING until the deadline reaper.
                self.assertEqual(0, registry.pending_count())

    def test_failed_live_authority_read_closes_claim_and_cancels_once_without_leaking(self) -> None:
        for cleanup_fails, reaper_wins in ((False, False), (True, False), (False, True)):
            with self.subTest(cleanup_fails=cleanup_fails, reaper_wins=reaper_wins):
                registry = ContinuationRegistry()
                adapter = _FakeContinuationAdapter()
                application = _application_with_continuation(registry, adapter)
                document = _canonical_request_document()
                record = _registered_record(document, registry, adapter)
                cancelled: list[str] = []

                def cancel(pending: PendingContinuation) -> None:
                    cancelled.append(pending.continuation_token)
                    if cleanup_fails:
                        raise RuntimeError("SYNTHETIC-CLEANUP-SECRET")

                def unavailable(_client_id: str) -> tuple[ResourceRegistry, AdministratorConstraints, ClientAuthorization, AdapterRegistry]:
                    if reaper_wins:
                        _ = registry.expire_due(record.deadline + timedelta(seconds=1))
                    raise RuntimeError("SYNTHETIC-AUTHORITY-SECRET")

                record.cancel_callback = cancel
                application.authority_source = unavailable
                with self.assertRaises(GatewayError) as caught:
                    _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
                self.assertEqual(caught.exception.http_status, 503)
                self.assertEqual(caught.exception.code, "state_unavailable")
                self.assertNotIn("SYNTHETIC", str(caught.exception))
                self.assertEqual(adapter.delivered, [])
                self.assertEqual(registry.pending_count(), 0)
                self.assertEqual(cancelled, [TOOL_TOKEN])
                self.assertTrue(registry.was_terminal_for(TOOL_TOKEN, CLIENT_ID))

    def test_backend_failed_outcome_closes_the_record(self) -> None:
        registry = ContinuationRegistry()
        adapter = _FakeContinuationAdapter()
        application = _application_with_continuation(registry, adapter)
        adapter.next_result = AdapterResult(
            status="failed",
            calls=(
                CallObservation(
                    call_index=0,
                    started_at="2026-09-28T12:00:00.000Z",
                    ended_at="2026-09-28T12:00:05.000Z",
                    status="failed",
                ),
            ),
        )
        document = _canonical_request_document()
        _ = _registered_record(document, registry, adapter)
        with self.assertRaises(GatewayError):
            _ = application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(document)
            )
        self.assertEqual(0, registry.pending_count())

    # ── Finding 6: current client authority rechecked pre-delivery ────

    def test_revoked_grant_blocks_delivery_without_rerouting(self) -> None:
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        # The administrator narrows the client's grant AFTER the
        # suspension: the resource is now blocked for this client.
        revoked_application = make_application(
            registry=build_registry(with_worker=True),
            adapters=cast("tuple[ExecutionAdapter, ...]", (self.adapter,)),
            continuations=self.registry,
            client_authorizations={
                CLIENT_ID: ClientAuthorization(
                    blocked_resource_ids=("openai-worker",)
                )
            },
        )
        delivered_before = len(self.adapter.delivered)
        with self.assertRaises(GatewayError) as caught:
            _ = revoked_application.execute(
                client_id=CLIENT_ID, request=parse_chat_request(document)
            )
        self.assertEqual(403, caught.exception.http_status)
        self.assertEqual("unauthorized_target", caught.exception.code)
        self.assertEqual(delivered_before, len(self.adapter.delivered))
        # The continuation is terminal (cancelled), not resumable.
        self.assertEqual(0, self.registry.pending_count())
        self.assertTrue(self.registry.was_terminal_for(TOOL_TOKEN, CLIENT_ID))

    def test_strict_non_paid_grant_refuses_existing_unproved_native_continuation(self) -> None:
        document = _canonical_request_document()
        record = _registered_record(document, self.registry, self.adapter)
        cancelled: list[str] = []
        record.cancel_callback = lambda pending: cancelled.append(pending.continuation_token)
        self.application.client_authorizations = {CLIENT_ID: ClientAuthorization(strict_no_payg=True)}
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
        self.assertEqual(caught.exception.code, "no_payg_evidence_unavailable")
        self.assertEqual(self.adapter.delivered, [])
        self.assertEqual(cancelled, [TOOL_TOKEN])
        self.assertEqual(self.registry.pending_count(), 0)

    def test_rebinding_native_resource_to_http_cannot_resume_it_under_inference_grant(self) -> None:
        document = _canonical_request_document()
        record = _registered_record(document, self.registry, self.adapter)
        cancelled: list[str] = []
        record.cancel_callback = lambda pending: cancelled.append(pending.continuation_token)
        baseline = self.application.registry.registry_snapshot()
        current = ResourceRegistry(clock=lambda: baseline.generated_at)
        for entry in baseline.entries:
            identity = entry.identity
            if identity.resource_id == "openai-worker":
                identity = replace(identity, channel="server_direct_http")
            current.register(ResourceRegistration(
                identity=identity, freshness_ttl_seconds=3600, capabilities=entry.capabilities,
            ))
            assert entry.observation is not None
            current.apply_snapshot(replace(entry.observation, identity=identity))

        def rebound(_client_id: str) -> tuple[ResourceRegistry, AdministratorConstraints, ClientAuthorization, AdapterRegistry]:
            return current, self.application.admin_constraints, ClientAuthorization(inference_only=True), self.application.adapters

        self.application.authority_source = rebound
        with self.assertRaises(GatewayError) as caught:
            _ = self.application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
        self.assertEqual(caught.exception.code, "unauthorized_target")
        self.assertEqual(self.adapter.delivered, [])
        self.assertEqual(self.registry.pending_count(), 0)
        self.assertEqual(cancelled, [TOOL_TOKEN])

    def test_configuration_rebuild_between_claim_and_delivery_pins_original_adapter_binding(self) -> None:
        for binding_changed in (False, True):
            with self.subTest(binding_changed=binding_changed):
                registry = ContinuationRegistry()
                original = _FakeContinuationAdapter()
                application = _application_with_continuation(registry, original)
                document = _canonical_request_document()
                record = _registered_record(document, registry, original)
                cancelled: list[str] = []
                record.cancel_callback = lambda pending: cancelled.append(pending.continuation_token)
                replacement = _FakeContinuationAdapter()
                if binding_changed:
                    replacement.binding = ("endpoint-test", "worker-test", "replacement-adapter")

                def rebuild_after_claim(_client_id: str) -> tuple[ResourceRegistry, AdministratorConstraints, ClientAuthorization, AdapterRegistry]:
                    self.assertEqual(record.state, "resuming")
                    rebuilt = _application_with_continuation(registry, replacement)
                    return rebuilt.registry, rebuilt.admin_constraints, ClientAuthorization(), rebuilt.adapters

                application.authority_source = rebuild_after_claim
                if binding_changed:
                    with self.assertRaises(GatewayError) as caught:
                        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
                    self.assertEqual(caught.exception.code, "unauthorized_target")
                    self.assertEqual(original.delivered, [])
                    self.assertEqual(cancelled, [TOOL_TOKEN])
                else:
                    _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
                    self.assertEqual(len(original.delivered), 1)
                    self.assertEqual(cancelled, [])
                self.assertEqual(replacement.delivered, [])
                self.assertEqual(registry.pending_count(), 0)

    def test_allowed_grant_still_delivers(self) -> None:
        # The recheck must not over-block: the default (unrestricted)
        # grant delivers exactly as before.
        document = _canonical_request_document()
        _ = _registered_record(document, self.registry, self.adapter)
        outcome = self.application.execute(
            client_id=CLIENT_ID, request=parse_chat_request(document)
        )
        self.assertEqual("stop", outcome.finish_reason)


class _ControlContinuationAdapter(_FakeContinuationAdapter):
    """Synthetic metadata seam for original-context drift, not source attestation."""

    def __init__(self, resource: ResourceIdentity) -> None:
        super().__init__()
        self.revision: str = "synthetic-control-a"
        scope = ExecutionControlScope(resource, self.adapter_name, self.adapter_version,
                                      f"adapter-{id(self):x}", self.revision)
        self.proof: NonPaidControlEvidence = NonPaidControlEvidence(
            scope, "verified_control", canonical(T_EVAL), canonical(T_EVAL + timedelta(minutes=5)),
            "synthetic-context-comparison", NON_PAID_MODES,
        )

    def non_paid_control_revision(self, _resource_id: str) -> str:
        return self.revision

    def non_paid_control_evidence(self, _resource_id: str) -> NonPaidControlEvidence:
        return self.proof


class StrictContinuationEvidenceTests(unittest.TestCase):
    def _suspend(self, *, strict: bool = True) -> tuple[GatewayApplication, _ControlContinuationAdapter, PendingContinuation, dict[str, object], list[dict[str, object]]]:
        registry = build_registry(with_worker=True)
        resource = next(entry.identity for entry in registry.registry_snapshot(now=canonical(T_EVAL)).entries
                        if entry.identity.resource_id == "openai-worker")
        adapter = _ControlContinuationAdapter(resource)
        continuations = ContinuationRegistry()
        application = make_application(
            registry=registry, adapters=(adapter,), continuations=continuations,
            cells=build_cells(include_worker=True),
            continuation_capability_source=lambda: frozenset({"openai-worker"}),
            client_authorizations={CLIENT_ID: ClientAuthorization(strict_no_payg=strict)},
        )
        receipts: list[dict[str, object]] = []
        application.source_call_fact_sink = receipts.append
        model = "sr-pin:openai-worker/openai/gpt-5.6-luna/max"
        outcome = application.execute(client_id=CLIENT_ID, request=parse_chat_request({
            "model": model, "messages": PREFIX_MESSAGES, "tools": [dict(_TOOL_DECLARATION)], "tool_choice": "auto",
        }))
        call = outcome.message.tool_calls[0]
        record = continuations.detect(call.id)
        assert record is not None
        # Registry claims use real UTC; the metadata fixtures use T_EVAL.
        record.deadline = _DEADLINE
        document = _canonical_request_document(token=call.id, model=model, tools=[dict(_TOOL_DECLARATION)])
        messages = cast(list[dict[str, object]], document["messages"])
        messages[-2] = {"role": "assistant", "tool_calls": [{"id": call.id, "type": "function",
                          "function": {"name": call.name, "arguments": call.arguments}}]}
        self.assertTrue(cast(dict[str, object], receipts[-1]["reservation"])["router_local_held_for_attempt"])
        return application, adapter, record, document, receipts

    def test_new_matching_revision_evidence_cannot_authorize_original_turn(self) -> None:
        application, adapter, record, document, _receipts = self._suspend()
        cancelled: list[str] = []
        record.cancel_callback = lambda pending: cancelled.append(pending.continuation_token)
        adapter.revision = "synthetic-control-b"
        adapter.proof = replace(adapter.proof, scope=replace(adapter.proof.scope, control_revision=adapter.revision))
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
        self.assertEqual(caught.exception.code, "no_payg_binding_changed")
        self.assertEqual(adapter.delivered, [])
        self.assertEqual(cancelled, [record.continuation_token])
        assert application.continuations is not None
        self.assertEqual(application.continuations.pending_count(), 0)

    def test_benign_same_scope_refresh_resumes_without_invented_reservation(self) -> None:
        application, adapter, _record, document, receipts = self._suspend()
        adapter.proof = replace(adapter.proof, observed_at=canonical(T_EVAL - timedelta(seconds=1)))
        outcome = application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
        self.assertEqual(outcome.finish_reason, "stop")
        self.assertFalse(cast(dict[str, object], receipts[-1]["reservation"])["router_local_held_for_attempt"])

    def test_current_grant_cannot_downgrade_original_strict_turn(self) -> None:
        application, adapter, _record, document, _receipts = self._suspend()
        application.client_authorizations = {CLIENT_ID: ClientAuthorization()}
        adapter.revision = "synthetic-control-b"
        adapter.proof = replace(adapter.proof, scope=replace(adapter.proof.scope, control_revision=adapter.revision))
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
        self.assertEqual(caught.exception.code, "no_payg_binding_changed")
        self.assertEqual(adapter.delivered, [])

    def test_late_strict_grant_requires_original_context_proof_not_current_positive(self) -> None:
        application, adapter, _record, document, _receipts = self._suspend(strict=False)
        application.client_authorizations = {CLIENT_ID: ClientAuthorization(strict_no_payg=True)}
        with self.assertRaises(GatewayError) as caught:
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
        self.assertEqual(caught.exception.code, "no_payg_evidence_unavailable")
        self.assertEqual(adapter.delivered, [])

    def test_sequential_suspension_keeps_original_scope_and_honest_reservation(self) -> None:
        application, adapter, original, document, receipts = self._suspend()
        token = new_continuation_token()
        adapter.next_result = ToolSuspension(token, TOOL_NAME, "{}")
        _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
        assert application.continuations is not None
        next_record = application.continuations.detect(token)
        assert next_record is not None
        self.assertEqual(next_record.non_paid_control_scope, original.non_paid_control_scope)
        self.assertFalse(cast(dict[str, object], receipts[-1]["reservation"])["router_local_held_for_attempt"])

    def test_failed_delivery_does_not_claim_a_continuation_reservation(self) -> None:
        application, adapter, _record, document, receipts = self._suspend()
        adapter.next_result = AdapterPermanentError("synthetic failure")
        with self.assertRaises(GatewayError):
            _ = application.execute(client_id=CLIENT_ID, request=parse_chat_request(document))
        self.assertFalse(cast(dict[str, object], receipts[-1]["reservation"])["router_local_held_for_attempt"])


_PREFIX_ALONE = PREFIX_MESSAGES[1]


if __name__ == "__main__":
    _ = unittest.main()
