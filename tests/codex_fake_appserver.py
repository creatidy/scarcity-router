"""Deterministic fake Codex App Server for M06 adapter tests (no live Codex).

This script is spawned by the test suite's injected spawner as the stand-in
``codex`` binary — no test executes a real Codex binary, contacts any
account, or consumes any quota. Every string it emits or accepts is
synthetic; no credential-shaped value ever appears anywhere.

Usage (the test spawner composes exactly one of these):

- ``codex_fake_appserver.py --version <version>`` — the ``--version`` probe
  stand-in: prints ``codex-cli <version>`` and exits 0.
- ``codex_fake_appserver.py --app-server <scenario-json>`` — the App Server
  stand-in: speaks enough stdio JSONL (initialize, account/read,
  model/list, thread/start, thread/inject_items, turn/start, notifications,
  turn/interrupt, approval server-requests) to exercise one scripted
  scenario.

Scenario knobs added for the M10-B composed acceptance suite (opt-in;
absent keys keep the exact M06 behavior): ``pauseBeforeDelta`` (seconds)
pauses before streaming every delta AFTER the first, exiting early when a
``turn/interrupt`` arrives — this gives the M10-B client-disconnect test a
deterministic window where the turn is open and further output is pending.

Every request the fake receives is appended as one JSON line to the file
named by the ``SR_FAKE_TRACE`` environment variable (the test spawner adds
it), so tests can pin exactly which protocol methods ran and with which
parameters — including that approval server-requests were answered with the
``cancel`` decision and never ``accept``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import cast

_APPROVAL_REQUEST_ID = 9101


def _trace(record: dict[str, object]) -> None:
    path = os.environ.get("SR_FAKE_TRACE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        _ = handle.write(json.dumps(record) + "\n")


def _as_object(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


class _Fake:
    """The scripted App Server behavior for one scenario."""

    def __init__(self, scenario: dict[str, object]) -> None:
        self._scenario: dict[str, object] = scenario
        self._stdout_lock: threading.Lock = threading.Lock()
        self._queue: list[dict[str, object]] = []
        self._queue_lock: threading.Lock = threading.Lock()
        self._queued: threading.Event = threading.Event()
        self._stop_streaming: threading.Event = threading.Event()
        self._approval_decision: str | None = None
        self._pending_approval: bool = False
        self._refreshed: bool = False

    # -- output helpers ------------------------------------------------------

    def _write(self, payload: dict[str, object]) -> None:
        with self._stdout_lock:
            _ = sys.stdout.write(json.dumps(payload) + "\n")
            _ = sys.stdout.flush()

    def _respond(self, request_id: object, result: object) -> None:
        self._write({"id": request_id, "result": result})

    def _respond_error(self, request_id: object, code: int) -> None:
        self._write(
            {"id": request_id, "error": {"code": code, "message": "synthetic"}}
        )

    def _require_params(self, request: dict[str, object]) -> bool:
        """Reject an absent params member with -32600 (codex 0.155 drift)."""
        if "params" in request:
            return True
        self._respond_error(request.get("id"), -32600)
        return False

    def _notify(self, method: str, params: dict[str, object]) -> None:
        self._write({"method": method, "params": params})

    def _raw_line(self, line: str) -> None:
        with self._stdout_lock:
            _ = sys.stdout.write(line + "\n")
            _ = sys.stdout.flush()

    # -- scripted pieces ------------------------------------------------------

    def _initialize_result(self) -> dict[str, object]:
        if self._scenario.get("init") == "bad-shape":
            return {"unexpected": "shape"}
        home = os.environ.get("CODEX_HOME", "")
        if self._scenario.get("init") == "mismatch-home":
            home = "SYNTHETIC-not-the-controlled-home"
        return {
            "userAgent": "codex_cli_rs/0.155.1 (synthetic)",
            "codexHome": home,
            "platformFamily": "unix",
            "platformOs": "linux",
        }

    def _account_result(self) -> dict[str, object] | None:
        kind = self._scenario.get("account", "chatgpt")
        if kind in ("chatgpt", "internal-error-then-chatgpt"):
            # Synthetic identifiers only; the adapter must discard them.
            return {
                "requiresOpenaiAuth": False,
                "account": {
                    "type": "chatgpt",
                    "email": "SYNTHETIC@example.invalid",
                    "planType": "plus",
                },
            }
        if kind == "apikey":
            return {"requiresOpenaiAuth": False, "account": {"type": "apiKey"}}
        return {"requiresOpenaiAuth": True, "account": None}

    def _models_page(self) -> dict[str, object]:
        if self._scenario.get("modelCursorLoop"):
            # Pagination that never ends: the adapter's bounded page budget
            # must fail closed with the explicit budget reason.
            return {"data": [], "nextCursor": "synthetic-next-page"}
        models = self._scenario.get("models")
        if models is None:
            models = [
                {
                    "id": "gpt-5.6-sol",
                    "model": "gpt-5.6-sol",
                    "displayName": "Synthetic",
                    "hidden": False,
                    "isDefault": True,
                    "supportedReasoningEfforts": [
                        {"description": "d", "reasoningEffort": effort}
                        for effort in ("minimal", "low", "medium", "high")
                    ],
                    "defaultReasoningEffort": "medium",
                }
            ]
        return {"data": models, "nextCursor": ""}

    # -- request reading ----------------------------------------------------

    def _read_requests(self) -> None:
        """One background reader: queue requests, match approval answers.

        The approval answer arrives on stdin as a plain JSON-RPC response
        (``id`` + ``result``, no ``method``) while the main thread is blocked
        streaming the turn, so the reader matches it against the pending
        approval id and records the decision.
        """
        for line in sys.stdin:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = _as_object(
                    cast("object", json.loads(stripped))
                )
            except ValueError:
                continue
            if message is None:
                continue
            if "method" not in message:
                self._record_answer(message)
                continue
            method = message.get("method")
            if method == "turn/interrupt":
                self._enqueue(message)
                # Abort any scripted pause so the main loop serves it.
                self._stop_streaming.set()
                continue
            self._enqueue(message)

    def _enqueue(self, request: dict[str, object]) -> None:
        with self._queue_lock:
            self._queue.append(request)
        self._queued.set()

    def _record_answer(self, message: dict[str, object]) -> None:
        if self._pending_approval and message.get("id") == _APPROVAL_REQUEST_ID:
            result = message.get("result")
            result_map = _as_object(result)
            decision = (
                result_map.get("decision")
                if result_map is not None
                else "invalid"
            )
            self._approval_decision = str(decision)
            self._pending_approval = False
            _trace(
                {
                    "event": "approval",
                    "method": "item/commandExecution/requestApproval",
                    "decision": str(decision),
                }
            )

    def _next_request(self) -> dict[str, object] | None:
        while True:
            with self._queue_lock:
                if self._queue:
                    return self._queue.pop(0)
            if self._queued.wait(timeout=0.05):
                self._queued.clear()

    # -- main app-server loop ---------------------------------------------------

    def run(self) -> None:
        behavior = self._scenario.get("behavior", "normal")
        if behavior == "exit-immediately":
            os._exit(3)  # noqa: S311 - deliberate hard exit for the test fake
        reader = threading.Thread(target=self._read_requests, daemon=True)
        reader.start()
        while True:
            request = self._next_request()
            if request is None:
                return
            method = request.get("method")
            # Every protocol message the adapter puts on the wire is traced
            # exactly once, here at the dispatch point, so tests can pin the
            # complete method surface against the stable-surface allowlist.
            _trace(
                {
                    "event": "request",
                    "method": method,
                    "params": request.get("params"),
                }
            )
            if method == "initialize":
                if self._scenario.get("init") == "refuse":
                    self._respond_error(request.get("id"), -32601)
                elif behavior == "stall":
                    self._stall()  # never answers: startup deadline fires
                else:
                    self._respond(request.get("id"), self._initialize_result())
                    if behavior == "exit-after-init":
                        os._exit(3)
            elif method == "initialized":
                continue
            elif method == "account/read":
                # Real codex-cli 0.155 behavior (live evidence 2026-09-24):
                # these methods REQUIRE a params member; an absent member
                # is rejected with JSON-RPC -32600. The fake is strict so
                # adapter regressions back to the pre-0.155 wire shape
                # fail here instead of silently passing.
                if not self._require_params(request):
                    continue
                self._answer_account(request)
            elif method == "model/list":
                if not self._require_params(request):
                    continue
                self._respond(request.get("id"), self._models_page())
            elif method == "thread/start":
                if self._scenario.get("thread") == "drift":
                    self._respond(request.get("id"), {"unexpected": True})
                    return
                self._respond(request.get("id"), {"thread": {"id": "thr-synthetic-1"}})
                if behavior == "exit-after-thread":
                    os._exit(3)
            elif method == "thread/inject_items":
                if self._scenario.get("inject") == "drift":
                    self._respond(request.get("id"), None)
                else:
                    self._respond(request.get("id"), {})
            elif method == "turn/start":
                if self._scenario.get("turnStart") == "drift":
                    self._respond(request.get("id"), {})
                    return
                self._respond(
                    request.get("id"),
                    {"turn": {"id": "turn-synthetic-1", "status": "inProgress"}},
                )
                self._stream_turn()
            elif method == "turn/interrupt":
                self._respond(request.get("id"), {})
                self._finish_turn("interrupted")
                return
            else:
                self._respond_error(request.get("id"), -32601)

    def _answer_account(self, request: dict[str, object]) -> None:
        params_raw = request.get("params")
        params = _as_object(params_raw) if params_raw is not None else {}
        kind = self._scenario.get("account", "chatgpt")
        if (params or {}).get("refreshToken") is True:
            # The bounded D-018 refresh: outcome only visible to the retry.
            if kind == "refresh-still-error":
                self._respond_error(request.get("id"), -32603)
            else:
                self._refreshed = True
                self._respond(request.get("id"), {})
            return
        if kind in ("internal-error", "refresh-still-error"):
            self._respond_error(request.get("id"), -32603)
            return
        if kind == "internal-error-then-chatgpt" and not self._refreshed:
            self._respond_error(request.get("id"), -32603)
            return
        result = self._account_result()
        if result is None:
            self._respond_error(request.get("id"), -32603)
        else:
            self._respond(request.get("id"), result)

    # -- turn streaming -------------------------------------------------------

    def _stream_turn(self) -> None:
        turn_raw = self._scenario.get("turn")
        turn = _as_object(turn_raw) if turn_raw is not None else {}
        assert turn is not None
        if turn.get("malformedFirst"):
            self._raw_line("this is not json")
        if turn.get("duplicateKeys"):
            self._raw_line('{"method":"x","params":{},"params":{}}')
        if turn.get("deepNested"):
            self._raw_line("[" * 60000)
        if turn.get("oversizedLine"):
            self._raw_line("x" * (300 * 1024))
        unknown = turn.get("unknownNotifications", 0)
        for _ in range(int(unknown) if isinstance(unknown, (int, float)) else 0):
            if self._stop_streaming.is_set():
                return
            self._notify("totally/unknown", {})
        if self._stop_streaming.is_set():
            return
        self._notify(
            "turn/started",
            {"threadId": "thr-synthetic-1", "turn": {"id": "turn-synthetic-1"}},
        )
        if turn.get("approval"):
            self._pending_approval = True
            self._write(
                {
                    "id": _APPROVAL_REQUEST_ID,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thr-synthetic-1",
                        "turnId": "turn-synthetic-1",
                        "itemId": "item-approval",
                        "startedAtMs": 0,
                    },
                }
            )
            # The reader records the answer; give it a bounded moment.
            for _ in range(500):
                if self._approval_decision is not None:
                    break
                if self._stop_streaming.wait(timeout=0.02):
                    return
            if self._approval_decision != "cancel":
                # The adapter accepted an approval: prove the failure mode by
                # completing the turn (the strict test then fails).
                self._finish_turn("completed")
                return
            self._finish_turn("interrupted")
            return
        if turn.get("stderrSpam"):
            # 20 KiB: over the adapter's capture cap, under the pipe buffer,
            # so the fake never blocks on a stopped drainer.
            try:
                _ = sys.stderr.write("SYNTHETIC-STDERR-" + "s" * (20 * 1024) + "\n")
                _ = sys.stderr.flush()
            except (OSError, ValueError):
                pass
        deltas = turn.get("deltas") or ["Hel", "lo"]
        assert isinstance(deltas, list)
        text_deltas = [str(delta) for delta in cast("list[object]", deltas)]
        pause_raw = turn.get("pauseBeforeDelta", 0)
        pause_between = (
            float(pause_raw) if isinstance(pause_raw, (int, float)) else 0.0
        )
        for index, delta in enumerate(text_deltas):
            if index > 0 and pause_between > 0:
                # M10-B knob: keep the turn open with output pending so a
                # client disconnect mid-turn is deterministic; an interrupt
                # ends the pause early (the queued interrupt is served next).
                if self._stop_streaming.wait(timeout=pause_between):
                    return
            if self._stop_streaming.is_set():
                return
            self._notify(
                "item/agentMessage/delta",
                {
                    "threadId": "thr-synthetic-1",
                    "turnId": "turn-synthetic-1",
                    "itemId": f"item-{index}",
                    "delta": delta,
                },
            )
        if self._stop_streaming.is_set():
            return
        usage = turn.get("usage")
        if usage is not None:
            self._notify(
                "thread/tokenUsage/updated",
                {
                    "threadId": "thr-synthetic-1",
                    "turnId": "turn-synthetic-1",
                    "tokenUsage": usage,
                },
            )
        if turn.get("exitMidTurn"):
            os._exit(3)  # noqa: S311 - deliberate hard exit for the test fake
        pause_raw = turn.get("pauseBeforeCompleted", 0)
        pause = float(pause_raw) if isinstance(pause_raw, (int, float)) else 0.0
        if pause > 0 and self._stop_streaming.wait(timeout=pause):
            return
        if self._stop_streaming.is_set():
            return
        status = turn.get("status", "completed")
        assert isinstance(status, str)
        self._finish_turn(status)

    def _finish_turn(self, status: str) -> None:
        turn_raw = self._scenario.get("turn")
        turn = _as_object(turn_raw) if turn_raw is not None else {}
        assert turn is not None
        if status == "completed" and self._approval_decision == "cancel":
            # Protocol-correct: a cancel decision denies AND interrupts.
            status = "interrupted"
        error = None
        if status == "failed":
            info = turn.get("errorInfo")
            error = {
                "message": "SYNTHETIC free text that would carry prompt content",
                "codexErrorInfo": info if info is not None else "other",
            }
        self._notify(
            "turn/completed",
            {
                "threadId": "thr-synthetic-1",
                "turn": {"id": "turn-synthetic-1", "status": status, "error": error},
            },
        )

    def _stall(self) -> None:
        while not self._stop_streaming.wait(timeout=0.2):
            pass


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _ = sys.stderr.write("usage: fake [--version V | --app-server SCENARIO]\n")
        return 2
    if argv[1] == "--version":
        print(f"codex-cli {argv[2] if len(argv) > 2 else '0.0.0'}")
        return 0
    if argv[1] != "--app-server":
        _ = sys.stderr.write("unknown mode\n")
        return 2
    scenario = _as_object(cast("object", json.loads(argv[2])))
    assert scenario is not None
    _ = _Fake(scenario).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
