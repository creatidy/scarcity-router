"""Gateway-side client-tool continuation registry (D-062, issue #137).

When the Codex execution source suspends one backend turn to request a
CLIENT-owned tool (the D-056/D-062 lifecycle), the gateway must hold ONE
bounded, exactly-once correlation between the OpenAI ``tool_call_id`` the
harness sees and the suspended execution inside the worker. This module
owns that state and nothing else: it performs no routing, never executes
a tool, never touches worker sessions directly and never interprets tool
content.

Security and lifecycle invariants (frozen by #137/D-062):

- **Opaque correlation.** The externally visible ``tool_call_id`` is a
  server-issued random token (``srct-`` + 128 hex bits). It grants
  access to exactly ONE pending continuation of exactly ONE client; no
  worker, thread, process or path identifier ever crosses the wire.
- **Single subscriber, exactly-once delivery.** A continuation is
  claimed at most once (:meth:`ContinuationRegistry.claim` performs the
  single ``waiting_for_client_tool -> resuming`` transition under one
  lock). A replayed result, a second concurrent result, a result from a
  different client and a late result after expiry/cancellation each get
  a distinct typed rejection — never a silent first-win.
- **One absolute lifetime.** A continuation's deadline is the ORIGINAL
  attempt's admission deadline (``started + execution_time_limit_seconds``
  at first dispatch). There is no per-leg reset: every resumed-inference
  and further tool cycle draws from the SAME budget, so no request
  pattern can keep a backend turn alive indefinitely. ``expire_due``
  (called by the server's reaper loop) cancels the worker-side turn and
  removes the record; the worker's own deadline timer is the backstop.
- **Bounded state.** At most :data:`MAX_PENDING_CONTINUATIONS`
  continuations exist at once and each record holds only bounded
  fingerprints and identifiers — never a prompt, never tool arguments or
  results, never a raw conversation.
- **Sticky identity.** The registry never reroutes: competitive policy,
  scarcity and quota changes cannot move an already-suspended turn. Hard
  authority loss (worker revocation, source disappearance, session
  death) is observable through the record's liveness probe and surfaces
  as a typed lost/ambiguous outcome, never a substitute execution.
- **Honest restart semantics.** State is in-memory by architecture; a
  gateway restart loses every pending continuation, and later tool
  results receive the explicit ``continuation_not_found`` failure
  instead of pretended durability.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .gateway_adapters import AdapterMessage, AdapterToolCall, SuspensionHandle
from .gateway_audit import ExecutedTarget
from .gateway_validation import v_safe_id, v_text

# ── Closed continuation-state vocabulary ──────────────────────────────────────

CONTINUATION_WAITING = "waiting_for_client_tool"
CONTINUATION_RESUMING = "resuming"
CONTINUATION_COMPLETED = "completed"
CONTINUATION_CANCELLED = "cancelled"
CONTINUATION_EXPIRED = "expired"
CONTINUATION_LOST = "lost"

CONTINUATION_STATES: frozenset[str] = frozenset({
    CONTINUATION_WAITING,
    CONTINUATION_RESUMING,
    CONTINUATION_COMPLETED,
    CONTINUATION_CANCELLED,
    CONTINUATION_EXPIRED,
    CONTINUATION_LOST,
})

#: The bounded count of pending continuations gateway-wide. This is a
#: server-side structural bound (never client-expandable), deliberately
#: integrated with the worker endpoint's own pending-attempt bound: every
#: continuation holds exactly one worker-session attempt slot, so the
#: effective per-worker bound is ``min(this, MAX_PENDING_ATTEMPTS_PER_SESSION)``.
MAX_PENDING_CONTINUATIONS = 64

#: The bounded replay-tombstone ring: how many terminally resolved
#: continuation tokens are remembered for the explicit
#: already-resolved replay rejection (the oldest is forgotten first).
MAX_TERMINAL_TOMBSTONES = 256

_TOKEN_PREFIX = "srct-"


def new_continuation_token() -> str:
    """One unguessable external tool_call id (opaque, server-issued)."""
    return _TOKEN_PREFIX + secrets.token_hex(16)


# ── Typed rejection reasons (closed) ──────────────────────────────────────────

REJECT_NOT_FOUND = "not_found"
REJECT_EXPIRED = "expired"
REJECT_ALREADY_RESOLVED = "already_resolved"

REJECTION_REASONS: frozenset[str] = frozenset({
    REJECT_NOT_FOUND,
    REJECT_EXPIRED,
    REJECT_ALREADY_RESOLVED,
})


class ContinuationRejected(Exception):
    """A tool result cannot claim its continuation (typed, bounded).

    ``reason`` is one of :data:`REJECTION_REASONS`. ``not_found`` covers
    both an unknown id and a foreign client's id — the two are
    deliberately indistinguishable from the outside so the failure never
    reveals another client's pending execution.
    """

    def __init__(self, reason: str) -> None:
        if reason not in REJECTION_REASONS:
            raise ValueError(f"continuation rejection: unknown reason {reason!r}")
        super().__init__(reason)
        self.reason: str = reason


# ── Fingerprints (bounded identity checks; never raw content) ─────────────────


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


def canonical_json_text(value: object) -> str | None:
    """The canonical serialized form of a client-visible control value, or
    ``None`` for ``None`` — used to compare repeated request controls
    (e.g. ``tool_choice``) across continuation requests without storing
    raw objects."""
    if value is None:
        return None
    return _canonical_json(value)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def message_fingerprint(messages: Sequence[AdapterMessage]) -> str:
    """A bounded canonical fingerprint of a conversation prefix.

    Covers roles, content, tool-call ids/names/arguments and tool_call_id
    correlations — everything a continuation must not be able to rewrite.
    The fingerprint is a SHA-256 digest; no message content is retained.
    """
    structure: list[dict[str, object]] = []
    for message in messages:
        item: dict[str, object] = {"role": message.role}
        if message.content is not None:
            item["content"] = message.content
        if message.tool_call_id is not None:
            item["tool_call_id"] = message.tool_call_id
        if message.name is not None:
            item["name"] = message.name
        if message.tool_calls:
            item["tool_calls"] = [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in message.tool_calls
            ]
        structure.append(item)
    return _sha256(_canonical_json(structure))


def tools_fingerprint(tools: Sequence[Mapping[str, object]]) -> str | None:
    """A bounded canonical fingerprint of the declared tool set.

    Thread-scoped Codex declarations (the #137 evidence) make the
    original tool-definition set part of continuation identity: a
    changed set mid-thread is a typed fail-closed rejection, never a
    silent substitution.
    """
    if not tools:
        return None
    return _sha256(_canonical_json([dict(tool) for tool in tools]))


def tool_calls_match(
    expected: Sequence[AdapterToolCall],
    actual: Sequence[AdapterToolCall] | None,
) -> bool:
    """Exact assistant tool_calls identity (ids, names, arguments)."""
    if actual is None or len(expected) != len(actual):
        return False
    return all(
        expected_call.id == actual_call.id
        and expected_call.name == actual_call.name
        and expected_call.arguments == actual_call.arguments
        for expected_call, actual_call in zip(expected, actual)
    )


# ── Records ───────────────────────────────────────────────────────────────────


@dataclass
class PendingContinuation:
    """One suspended backend turn awaiting its harness tool result.

    Everything here is server-side correlation state: identifiers,
    bounded fingerprints and the audit provenance of the ORIGINAL
    dispatch. Prompt text, tool arguments and tool results are never
    stored (arguments live in the tool_calls tuple the client must echo;
    results flow straight through delivery and are never retained).
    """

    continuation_token: str
    attempt_id: str
    resource_id: str
    channel: str
    call_id: str
    tool_name: str
    deadline: datetime
    created_at: str
    client_id: str
    model_echo: str
    reasoning_effort: str | None
    tools_fingerprint: str | None
    tool_choice_json: str | None
    prefix_fingerprint: str
    assistant_tool_calls: tuple[AdapterToolCall, ...]
    #: The adapter-side identity of the suspended execution (set at
    #: registration; the coordinator passes it back to the adapter's
    #: continuation surface).
    handle: SuspensionHandle | None = None
    registry_revision: int | None = None
    registry_generated_at: str | None = None
    #: The ORIGINAL dispatch's audit identity — the continuation is the
    #: same logical backend execution, so both audit records carry it
    #: (and the single provider call is counted once, on the terminal
    #: record).
    selected_target: ExecutedTarget | None = None
    executed_target: ExecutedTarget | None = None
    decision_id: str | None = None
    adapter_name: str | None = None
    adapter_version: str | None = None
    #: Supplied by the composition: cancels the worker-side turn
    #: best-effort (never raises). Called by :meth:`expire_due` OUTSIDE
    #: the registry lock.
    cancel_callback: Callable[["PendingContinuation"], None] | None = None
    state: str = CONTINUATION_WAITING
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def expired_at(self, now: datetime) -> bool:
        return now >= self.deadline

    def claim_transition(self, now: datetime) -> str:
        """The single ``waiting -> resuming`` transition under the
        record's own lock. Returns ``"resumed"``, ``"busy"`` (a claim
        or terminal already happened) or ``"expired"`` (the record is
        past its absolute deadline and is marked so)."""
        with self._lock:
            if self.state != CONTINUATION_WAITING:
                return "busy"
            if self.expired_at(now):
                self.state = CONTINUATION_EXPIRED
                return "expired"
            self.state = CONTINUATION_RESUMING
            return "resumed"

    def mark_terminal(self, state: str) -> None:
        """Record the terminal state (the registry dropped the record)."""
        with self._lock:
            self.state = state

    def mark_expired_if_waiting(self) -> bool:
        """Expire a still-waiting record (reaper path); ``False`` when a
        claim already moved it (the expiry then loses the race honestly)."""
        with self._lock:
            if self.state != CONTINUATION_WAITING:
                return False
            self.state = CONTINUATION_EXPIRED
            return True

    def restore_waiting(self) -> None:
        """Return a claimed record to ``waiting`` after a validation-only
        failure (the suspended turn is untouched)."""
        with self._lock:
            if self.state == CONTINUATION_RESUMING:
                self.state = CONTINUATION_WAITING


# ── The registry ──────────────────────────────────────────────────────────────


class ContinuationRegistry:
    """The bounded, exactly-once map from tool_call token to suspension.

    One instance is created per server process next to the worker
    endpoint and shared by the coordinator and the ``worker_bridged``
    adapter through application (re)composition, so a state-report
    adoption rebuild — which recreates adapters — never orphans a
    pending continuation.
    """

    def __init__(self, *, max_pending: int = MAX_PENDING_CONTINUATIONS) -> None:
        if max_pending < 1:
            raise ValueError("continuation_registry: max_pending must be positive")
        self._max_pending: int = max_pending
        self._lock: threading.Lock = threading.Lock()
        self._records: dict[str, PendingContinuation] = {}
        self._terminal_tombstones: deque[tuple[str, str]] = deque(
            maxlen=MAX_TERMINAL_TOMBSTONES
        )

    # -- registration (initial dispatch path) -----------------------------

    def register(self, record: PendingContinuation) -> bool:
        """Register one suspension; ``False`` when the bound is reached.

        The caller (the coordinator, on the dispatch thread) registered
        nothing yet; a ``False`` means the harness's tool result will be
        rejected ``continuation_not_found`` later — the honest bounded
        failure instead of an unbounded suspension table.
        """
        _ = v_safe_id(record.client_id, "pending_continuation.client_id")
        _ = v_text(record.model_echo, "pending_continuation.model_echo", max_len=512)
        if record.state != CONTINUATION_WAITING:  # pragma: no cover - constructor default
            raise ValueError("continuation_registry: records register as waiting")
        with self._lock:
            if len(self._records) >= self._max_pending:
                return False
            if record.continuation_token in self._records:
                return False
            self._records[record.continuation_token] = record
            return True

    # -- continuation-request path ----------------------------------------

    def detect(self, token: str) -> PendingContinuation | None:
        """The live record for ``token``, or ``None`` (no state change)."""
        with self._lock:
            return self._records.get(token)

    def was_terminal_for(self, token: str, client_id: str) -> bool:
        """Whether ``token`` named a continuation of THIS client that
        already reached a terminal state (a bounded tombstone ring).

        A replayed tool result after completion/expiry/cancellation must
        receive the explicit already-resolved conflict — never be
        re-interpreted as ordinary conversation history. A FOREIGN
        client's replay is ``False`` (indistinguishably not-found), so a
        token never reveals any other client's continuation history. The
        ring bounds the memory: beyond
        :data:`MAX_TERMINAL_TOMBSTONES` the oldest entry is forgotten
        (its replay then takes the honest not-found/unsupported path)."""
        with self._lock:
            return (token, client_id) in self._terminal_tombstones

    def claim(self, token: str, client_id: str) -> PendingContinuation:
        """The single ``waiting -> resuming`` transition (exactly once).

        Foreign clients and unknown/expired tokens are indistinguishably
        ``not_found``; a second claim is ``already_resolved``; a claim
        past the absolute deadline expires the record first (cancelling
        the worker turn outside the lock) and rejects ``expired``.
        """
        expired: PendingContinuation | None = None
        with self._lock:
            record = self._records.get(token)
            if record is None:
                # Terminal replay (same client) is the explicit conflict;
                # unknown and foreign are indistinguishably not-found.
                if (token, client_id) in self._terminal_tombstones:
                    raise ContinuationRejected(REJECT_ALREADY_RESOLVED)
                raise ContinuationRejected(REJECT_NOT_FOUND)
            if record.client_id != client_id:
                raise ContinuationRejected(REJECT_NOT_FOUND)
            outcome = record.claim_transition(datetime.now(timezone.utc))
            if outcome == "busy":
                raise ContinuationRejected(REJECT_ALREADY_RESOLVED)
            if outcome == "expired":
                _ = self._records.pop(token, None)
                self._terminal_tombstones.append((token, client_id))
                expired = record
        if expired is not None:
            if expired.cancel_callback is not None:
                expired.cancel_callback(expired)
            raise ContinuationRejected(REJECT_EXPIRED)
        return record

    def release_unclaimed(self, record: PendingContinuation) -> None:
        """Return a claimed record to ``waiting`` after a validation-only
        failure (the suspended turn is untouched and may still be
        continued by a corrected request)."""
        record.restore_waiting()

    def close(self, token: str, state: str) -> None:
        """Remove a continuation on its terminal transition.

        ``state`` must be a terminal one (completed/cancelled/expired/
        lost); the record is dropped, so any later replay is an
        indistinguishable ``not_found`` — except that the token joins the
        bounded tombstone ring and a replayed result is answered with the
        explicit already-resolved conflict.
        """
        if state not in (
            CONTINUATION_COMPLETED,
            CONTINUATION_CANCELLED,
            CONTINUATION_EXPIRED,
            CONTINUATION_LOST,
        ):
            raise ValueError(f"continuation_registry: {state!r} is not terminal")
        with self._lock:
            record = self._records.pop(token, None)
            self._terminal_tombstones.append(
                (token, record.client_id if record is not None else "")
            )
        if record is not None:
            record.mark_terminal(state)

    # -- maintenance -------------------------------------------------------

    def expire_due(self, now: datetime) -> tuple[str, ...]:
        """Expire every continuation past its absolute deadline.

        Returns the expired tokens. Cancellation callbacks run OUTSIDE
        the registry lock (they may touch worker sessions); each is
        invoked at most once because the record is removed first.
        """
        expired: list[PendingContinuation] = []
        with self._lock:
            for token, record in list(self._records.items()):
                if not record.expired_at(now):
                    continue
                _ = self._records.pop(token, None)
                if record.mark_expired_if_waiting():
                    expired.append(record)
        for record in expired:
            if record.cancel_callback is not None:
                record.cancel_callback(record)
        return tuple(record.continuation_token for record in expired)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._records)


__all__ = [
    "CONTINUATION_CANCELLED",
    "CONTINUATION_COMPLETED",
    "CONTINUATION_EXPIRED",
    "CONTINUATION_LOST",
    "CONTINUATION_RESUMING",
    "CONTINUATION_STATES",
    "CONTINUATION_WAITING",
    "MAX_PENDING_CONTINUATIONS",
    "MAX_TERMINAL_TOMBSTONES",
    "REJECT_ALREADY_RESOLVED",
    "REJECT_EXPIRED",
    "REJECT_NOT_FOUND",
    "ContinuationRejected",
    "ContinuationRegistry",
    "PendingContinuation",
    "canonical_json_text",
    "message_fingerprint",
    "new_continuation_token",
    "tool_calls_match",
    "tools_fingerprint",
]
