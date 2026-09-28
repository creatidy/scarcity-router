"""Unit tests for the D-060 continuation registry (#137).

Covers the exactly-once claim discipline, the bounded replay tombstones,
the absolute lifetime expiry (with the worker-cancel callback running
outside the lock), the bounded table, and the bounded fingerprints.
Deterministic: threading only, no clocks beyond injected datetimes.
"""

from __future__ import annotations

import threading
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from scarcity_router.gateway_adapters import AdapterMessage, AdapterToolCall
from scarcity_router.gateway_continuation import (
    REJECT_ALREADY_RESOLVED,
    REJECT_EXPIRED,
    REJECT_NOT_FOUND,
    ContinuationRejected,
    ContinuationRegistry,
    PendingContinuation,
    message_fingerprint,
    new_continuation_token,
    tool_calls_match,
    tools_fingerprint,
)

_CLIENT = "client-a"


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


def _record(
    *,
    token: str | None = None,
    deadline: datetime | None = None,
    client_id: str = _CLIENT,
    cancel_callback: "Callable[[PendingContinuation], None] | None" = None,
) -> PendingContinuation:
    return PendingContinuation(
        continuation_token=token if token is not None else new_continuation_token(),
        attempt_id="wa-attempt",
        resource_id="codex-e2e:slug",
        channel="worker_bridged",
        call_id="call-synthetic-1",
        tool_name="synthetic_lookup",
        deadline=(
            deadline if deadline is not None else _real_now() + timedelta(hours=1)
        ),
        created_at="2026-09-28T12:00:00Z",
        client_id=client_id,
        model_echo="gpt-5.6-sol",
        reasoning_effort=None,
        tools_fingerprint=tools_fingerprint(
            ({"type": "function", "function": {"name": "t"}},)
        ),
        tool_choice_json=None,
        prefix_fingerprint="f" * 64,
        assistant_tool_calls=(
            AdapterToolCall(id="srct-x", name="t", arguments="{}"),
        ),
        cancel_callback=cancel_callback,
    )


class RegistryLifecycleTests(unittest.TestCase):
    def test_register_claim_close_is_exactly_once(self) -> None:
        registry = ContinuationRegistry()
        record = _record()
        self.assertTrue(registry.register(record))
        claimed = registry.claim(record.continuation_token, _CLIENT)
        self.assertIs(record, claimed)
        with self.assertRaises(ContinuationRejected) as caught:
            _ = registry.claim(record.continuation_token, _CLIENT)
        self.assertEqual(REJECT_ALREADY_RESOLVED, caught.exception.reason)
        registry.close(record.continuation_token, "completed")
        with self.assertRaises(ContinuationRejected) as caught:
            _ = registry.claim(record.continuation_token, _CLIENT)
        self.assertEqual(REJECT_ALREADY_RESOLVED, caught.exception.reason)
        self.assertTrue(registry.was_terminal_for(record.continuation_token, _CLIENT))
        self.assertFalse(
            registry.was_terminal_for(record.continuation_token, "client-b")
        )

    def test_unknown_token_is_not_found(self) -> None:
        registry = ContinuationRegistry()
        with self.assertRaises(ContinuationRejected) as caught:
            _ = registry.claim(new_continuation_token(), _CLIENT)
        self.assertEqual(REJECT_NOT_FOUND, caught.exception.reason)
        self.assertFalse(registry.was_terminal_for("srct-unknown", _CLIENT))

    def test_foreign_client_is_indistinguishably_not_found(self) -> None:
        registry = ContinuationRegistry()
        record = _record()
        self.assertTrue(registry.register(record))
        with self.assertRaises(ContinuationRejected) as caught:
            _ = registry.claim(record.continuation_token, "client-b")
        self.assertEqual(REJECT_NOT_FOUND, caught.exception.reason)
        # The record is untouched: the legitimate client can still claim.
        claimed = registry.claim(record.continuation_token, _CLIENT)
        self.assertIs(record, claimed)

    def test_claim_past_absolute_deadline_expires_and_cancels(self) -> None:
        cancelled: list[str] = []
        registry = ContinuationRegistry()
        record = _record(
            deadline=_real_now() - timedelta(seconds=1),
            cancel_callback=lambda _r: cancelled.append(_r.continuation_token),
        )
        self.assertTrue(registry.register(record))
        with self.assertRaises(ContinuationRejected) as caught:
            _ = registry.claim(record.continuation_token, _CLIENT)
        self.assertEqual(REJECT_EXPIRED, caught.exception.reason)
        self.assertEqual([record.continuation_token], cancelled)
        self.assertEqual(0, registry.pending_count())
        self.assertTrue(registry.was_terminal_for(record.continuation_token, _CLIENT))

    def test_expire_due_cancels_outside_the_lock_once(self) -> None:
        cancelled: list[str] = []
        registry = ContinuationRegistry()
        now = _real_now()
        due = _record(
            deadline=now - timedelta(seconds=1),
            cancel_callback=lambda _r: cancelled.append(_r.continuation_token),
        )
        live = _record(deadline=now + timedelta(hours=1))
        self.assertTrue(registry.register(due))
        self.assertTrue(registry.register(live))
        expired = registry.expire_due(now)
        self.assertEqual((due.continuation_token,), expired)
        self.assertEqual([due.continuation_token], cancelled)
        # A second tick inside the live record's lifetime cancels nothing.
        self.assertEqual((), registry.expire_due(now + timedelta(minutes=30)))
        self.assertEqual([due.continuation_token], cancelled)
        self.assertEqual(1, registry.pending_count())

    def test_bounded_table_refuses_new_suspensions_at_the_bound(self) -> None:
        registry = ContinuationRegistry(max_pending=1)
        self.assertTrue(registry.register(_record()))
        self.assertFalse(registry.register(_record()))
        self.assertEqual(1, registry.pending_count())


class FingerprintTests(unittest.TestCase):
    def test_message_fingerprint_covers_every_rewrite_vector(self) -> None:
        base = (
            AdapterMessage(role="user", content="hello"),
            AdapterMessage(
                role="assistant",
                content=None,
                tool_calls=(AdapterToolCall(id="a", name="t", arguments="{}"),),
            ),
            AdapterMessage(role="tool", tool_call_id="a", content="result"),
        )
        reference = message_fingerprint(base)
        self.assertEqual(reference, message_fingerprint(base))
        mutations = (
            (AdapterMessage(role="user", content="HELLO"),) + base[1:],
            base[:1]
            + (
                AdapterMessage(
                    role="assistant",
                    content=None,
                    tool_calls=(AdapterToolCall(id="a", name="t", arguments='{"x":1}'),),
                ),
            )
            + base[2:],
            base[:2]
            + (AdapterMessage(role="tool", tool_call_id="b", content="result"),),
            base[:1]
            + (
                AdapterMessage(
                    role="assistant",
                    content=None,
                    tool_calls=(AdapterToolCall(id="OTHER", name="t", arguments="{}"),),
                ),
            )
            + base[2:],
        )
        for mutated in mutations:
            self.assertNotEqual(
                reference,
                message_fingerprint(mutated),
                "a mutated conversation must not share the prefix fingerprint",
            )

    def test_tools_fingerprint_absent_tools_is_none(self) -> None:
        self.assertIsNone(tools_fingerprint(()))
        self.assertEqual(
            tools_fingerprint(({"type": "function", "function": {"name": "t"}},)),
            tools_fingerprint(({"type": "function", "function": {"name": "t"}},)),
        )
        self.assertNotEqual(
            tools_fingerprint(({"type": "function", "function": {"name": "t"}},)),
            tools_fingerprint(({"type": "function", "function": {"name": "u"}},)),
        )

    def test_tool_calls_match_is_exact(self) -> None:
        calls = (AdapterToolCall(id="a", name="t", arguments="{}"),)
        self.assertTrue(tool_calls_match(calls, calls))
        self.assertFalse(tool_calls_match(calls, None))
        self.assertFalse(
            tool_calls_match(
                calls, (AdapterToolCall(id="a", name="t", arguments='{"x":1}'),)
            )
        )
        self.assertFalse(tool_calls_match(calls, ()))


class TokenTests(unittest.TestCase):
    def test_tokens_are_unguessable_and_distinct(self) -> None:
        tokens = {new_continuation_token() for _ in range(64)}
        self.assertEqual(64, len(tokens))
        for token in tokens:
            self.assertTrue(token.startswith("srct-"))
            self.assertEqual(37, len(token))


class ConcurrencyTests(unittest.TestCase):
    def test_exactly_one_of_many_concurrent_claims_wins(self) -> None:
        registry = ContinuationRegistry()
        record = _record()
        self.assertTrue(registry.register(record))
        outcomes: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def contender() -> None:
            _ = barrier.wait()
            try:
                _ = registry.claim(record.continuation_token, _CLIENT)
                result = "claimed"
            except ContinuationRejected as exc:
                result = exc.reason
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=contender) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, outcomes.count("claimed"))
        self.assertEqual(3, outcomes.count(REJECT_ALREADY_RESOLVED))


if __name__ == "__main__":
    _ = unittest.main()
