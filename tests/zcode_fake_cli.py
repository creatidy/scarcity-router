"""Deterministic fake ZCode CLI for M07 adapter tests (no live ZCode).

This script is spawned by the test suite's injected spawner as the stand-in
``zcode`` binary — no test executes a real ZCode binary, contacts any
account, or consumes any quota. Every string it emits or accepts is
synthetic; no credential-shaped value ever appears anywhere.

The spawner composes exactly one of these modes (dispatched on the real
argv shape the adapter produces):

- ``zcode_fake_cli.py version <scenario-file>`` — the ``--version`` probe
  stand-in: prints the scenario's version string (default ``0.16.9``) and
  exits with the scenario's ``version_exit`` code (default 0).
- ``zcode_fake_cli.py doctor <scenario-file>`` — the ``doctor --json``
  probe stand-in: prints the scenario's doctor document (default: the
  evidenced healthy shape) and exits with ``doctor_exit`` (default 0).
- ``zcode_fake_cli.py run <scenario-file>`` — the headless ``--prompt``
  run stand-in: appends one trace record (real cwd, its mode, sorted env
  keys as the child observed them) to the file named by
  ``SR_ZCODE_FAKE_TRACE``, then emits the scenario's NDJSON events and
  terminal result under the scenario's timing knobs and exits with
  ``exit_code``.

Scenario knobs (all optional):

- ``events``: list of raw event objects printed one per line before the
  terminal result (progress events; strings are printed verbatim for
  malformed-line scenarios).
- ``result``: the terminal result object (default: a healthy result with
  ``response``/``sessionId``/``traceId``/``projection.status``).
- ``omit_result``: stream events then exit without any result line.
- ``exit_code``: process exit code after the output (default 0).
- ``stderr``: text written to stderr before the events (never read by
  the adapter; proves concurrent stderr drain and content isolation).
- ``pause_before_result``: seconds to sleep (in small increments) before
  printing the terminal result — the deterministic cancellation/timeout
  window.
- ``partial_line``: write the terminal result WITHOUT its trailing
  newline first, flush, sleep briefly, then finish the line (proves
  bounded partial-read behavior).
- ``marker_file``: basenames of files to create inside the run's cwd
  (proves the workspace is real, writable by the child, and explicit).

The trace's argv record always reflects the adapter's real argv vector —
tests pin the exact invocation shape (prompt as data, ``--mode build``,
explicit ``--cwd``, ``stream-json``) and environment hygiene from it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import cast


def _trace(record: dict[str, object]) -> None:
    path = os.environ.get("SR_ZCODE_FAKE_TRACE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        _ = handle.write(json.dumps(record) + "\n")


def _scenario(argv: list[str]) -> dict[str, object]:
    """Load the scenario document (a file path: scenarios can be large)."""
    if len(argv) < 3:
        return {}
    try:
        with open(argv[2], encoding="utf-8") as handle:
            parsed = cast("object", json.load(handle))
    except (OSError, ValueError):
        return {}
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


def _emit(line: str) -> None:
    _ = sys.stdout.write(line + "\n")
    _ = sys.stdout.flush()


def _sleep_bounded(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _run_version(scenario: dict[str, object]) -> int:
    _ = sys.stdout.write(str(scenario.get("version", "0.16.9")) + "\n")
    _ = sys.stdout.flush()
    code = scenario.get("version_exit", 0)
    return code if isinstance(code, int) else 0


def _run_doctor(scenario: dict[str, object]) -> int:
    doctor = scenario.get(
        "doctor",
        {
            "cli": {"name": "zcode", "version": "0.16.9"},
            "runtime": {"arch": "x64", "cwd": "/tmp", "platform": "linux"},
            "packaging": {"default": True, "sea": False},
        },
    )
    _ = sys.stdout.write(
        json.dumps(doctor) + "\n"
        if not isinstance(doctor, str)
        else doctor + "\n"
    )
    _ = sys.stdout.flush()
    code = scenario.get("doctor_exit", 0)
    return code if isinstance(code, int) else 0


def _run_headless(scenario: dict[str, object]) -> int:
    cwd = os.getcwd()
    try:
        cwd_mode = os.stat(cwd).st_mode & 0o777
    except OSError:
        cwd_mode = -1
    # The adapter's argv/env/cwd composition is pinned by the tests from
    # the captured spawn spec; the trace proves what the child process
    # actually observed at run time (real cwd, its mode, its env keys).
    _trace(
        {
            "mode": "run",
            "cwd": cwd,
            "cwd_mode": cwd_mode,
            "env_keys": sorted(os.environ.keys()),
        }
    )
    markers = scenario.get("marker_file")
    if isinstance(markers, list):
        named_markers = cast("list[object]", markers)
        for marker in named_markers:
            if isinstance(marker, str) and marker:
                try:
                    path = os.path.join(cwd, marker)
                    with open(path, "w", encoding="utf-8") as handle:
                        _ = handle.write("marker\n")
                except OSError:
                    pass
    # Simulated coding edit: append one deterministic line to a file in
    # the workspace (proves the run can modify the authorized project).
    append = scenario.get("append_line")
    if isinstance(append, dict):
        append_map = cast("dict[str, object]", append)
        append_file = append_map.get("file")
        append_line_text = append_map.get("line")
        if isinstance(append_file, str) and isinstance(append_line_text, str):
            try:
                path = os.path.join(cwd, append_file)
                with open(path, "a", encoding="utf-8") as handle:
                    _ = handle.write(append_line_text + "\n")
            except OSError:
                pass
    stderr = scenario.get("stderr")
    if isinstance(stderr, str) and stderr:
        _ = sys.stderr.write(stderr)
        _ = sys.stderr.flush()
    events = scenario.get("events")
    if isinstance(events, list):
        scripted_events = cast("list[object]", events)
        for event in scripted_events:
            _emit(event if isinstance(event, str) else json.dumps(event))
    if bool(scenario.get("pause_before_result")):
        _sleep_bounded(cast("float", scenario["pause_before_result"]))
    result = scenario.get(
        "result",
        {
            "type": "result",
            "sessionId": "sess-synthetic-1",
            "traceId": "trace-synthetic-1",
            "response": "synthetic answer",
            "projection": {"status": "success"},
        },
    )
    if not bool(scenario.get("omit_result")):
        line = (
            result
            if isinstance(result, str)
            else json.dumps(cast("dict[str, object]", result))
        )
        if bool(scenario.get("partial_line")):
            _ = sys.stdout.write(line)
            _ = sys.stdout.flush()
            time.sleep(0.2)
            _ = sys.stdout.write("\n")
            _ = sys.stdout.flush()
        else:
            _emit(line)
    code = scenario.get("exit_code", 0)
    return code if isinstance(code, int) else 0


def main() -> int:
    argv = sys.argv
    mode = argv[1] if len(argv) > 1 else ""
    scenario = _scenario(argv)
    if mode == "version":
        return _run_version(scenario)
    if mode == "doctor":
        return _run_doctor(scenario)
    if mode == "run":
        return _run_headless(scenario)
    _ = sys.stderr.write("zcode_fake_cli: unknown mode\n")
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
