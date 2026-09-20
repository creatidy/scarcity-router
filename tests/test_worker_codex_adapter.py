"""Codex worker-local execution adapter tests (M06, issue #91 Stage 2).

Deterministic and CI-safe: the adapter's subprocess seam is replaced with a
spawner that launches the scripted fake App Server
(``tests/codex_fake_appserver.py``); no test executes a real Codex binary,
contacts any account or consumes any quota. Every recorded expectation is
synthetic. The numbered comments map each test to the required coverage
areas of the Stage 2 implementation.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast, override

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
from scarcity_router.worker_client import (  # noqa: E402
    WorkerConfigError,
    build_registry,
    build_parser,
)
from scarcity_router.worker_codex_adapter import (  # noqa: E402
    CODEX_ADAPTER_ID,
    CONTROLLED_CONFIG_NAME,
    CodexLocalAdapter,
    CodexSpawnSpec,
    check_sandbox_availability,
    default_codex_spawner,
    discover_codex_binary,
    map_conversation,
    parse_model_listing,
    parse_token_usage,
    probe_codex_version,
    safe_error_note,
    terminate_codex_process,
    validate_structured_schema,
    verify_model_and_effort,
)
from scarcity_router.worker_local_adapters import (  # noqa: E402
    AdapterNotAllowedError,
    LocalAdapterRegistry,
    run_allowlisted,
)

FAKE = Path(__file__).resolve().parent / "codex_fake_appserver.py"
# The pinned-binary validation requires an executable regular file.
_ = os.chmod(FAKE, 0o755)

SLUG = "gpt-5.6-sol"
EFFORT = "high"


def _canonical(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _future_deadline(seconds: float = 30.0) -> str:
    return _canonical(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def _scenario(**kwargs: object) -> dict[str, object]:
    """A typed scenario literal (dict[str, object] despite str values)."""
    return kwargs


def _resource(resource_id: str = "codex-local") -> ResourceIdentity:
    return ResourceIdentity(
        resource_id=resource_id,
        channel="worker_bridged",
        provider="openai",
        model="codex",
        entitlement="subscription_included",
    )


def _call(
    *,
    stream: bool = False,
    messages: tuple[AdapterMessage, ...] | None = None,
    response_format: dict[str, object] | None = None,
    reasoning_effort: str | None = None,
    tools: tuple[dict[str, object], ...] = (),
    resource: ResourceIdentity | None = None,
) -> AdapterCall:
    return AdapterCall(
        resource=resource if resource is not None else _resource(),
        model=ModelIdentity(provider="openai", model=SLUG, variant="codex"),
        messages=messages
        if messages is not None
        else (AdapterMessage(role="user", content="hi"),),
        stream=stream,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
        tools=tools,
    )


def _default_scenario() -> dict[str, object]:
    return {
        "init": "ok",
        "account": "chatgpt",
        "turn": {
            "deltas": ["Hel", "lo"],
            "usage": {"last": {"inputTokens": 11, "outputTokens": 7}},
            "status": "completed",
        },
    }


class FakeCodexSpawner:
    """The injected process seam: launches the scripted fake binary."""

    def __init__(
        self,
        scenario: dict[str, object] | None = None,
        *,
        version: str = "0.155.1",
        trace_path: str | None = None,
    ) -> None:
        self.scenario: dict[str, object] = (
            scenario if scenario is not None else _default_scenario()
        )
        self.version: str = version
        self.trace_path: str | None = trace_path
        self.specs: list[CodexSpawnSpec] = []
        self.app_server_specs: list[CodexSpawnSpec] = []
        self.processes: list["subprocess.Popen[bytes]"] = []

    def __call__(self, spec: CodexSpawnSpec) -> "subprocess.Popen[bytes]":
        self.specs.append(spec)
        if list(spec.argv[1:2]) == ["--version"]:
            argv = [sys.executable, str(FAKE), "--version", self.version]
        else:
            self.app_server_specs.append(spec)
            argv = [
                sys.executable,
                str(FAKE),
                "--app-server",
                json.dumps(self.scenario),
            ]
        env = dict(spec.env)
        if self.trace_path:
            env["SR_FAKE_TRACE"] = self.trace_path
        proc = subprocess.Popen(  # noqa: S603 - test-controlled fixed argv
            argv,
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

    def all_reaped(self) -> bool:
        return all(proc.poll() is not None for proc in self.processes)


def _bwrap_lookup(name: str) -> str | None:
    if name == "bwrap":
        return "/usr/bin/bwrap"
    return None


class Harness:
    """One test case's adapter + spawner + trace + temp state directory."""

    def __init__(
        self,
        scenario: dict[str, object] | None = None,
        *,
        version: str = "0.155.1",
        pinned: bool = True,
        path_lookup: Callable[[str], str | None] | None = None,
        discovery_roots: tuple[Path, ...] | None = (),
        **adapter_kwargs: float,
    ) -> None:
        self.tmp: TemporaryDirectory[str] = TemporaryDirectory()
        self.state_dir: Path = Path(self.tmp.name)
        self.trace_path: str = str(self.state_dir / "trace.jsonl")
        self.spawner: FakeCodexSpawner = FakeCodexSpawner(
            scenario, version=version, trace_path=self.trace_path
        )
        lookup = path_lookup if path_lookup is not None else _bwrap_lookup
        self.adapter: CodexLocalAdapter = CodexLocalAdapter(
            resource=_resource(),
            state_dir=self.state_dir,
            pinned_binary=FAKE if pinned else None,
            discovery_roots=discovery_roots,
            path_lookup=lookup,
            spawner=self.spawner,
            platform_name="linux",
            platform_release="6.x-generic",
            **adapter_kwargs,
        )

    def cleanup(self) -> None:
        self.spawner.reap_all()
        self.tmp.cleanup()

    def trace(self) -> list[dict[str, object]]:
        path = Path(self.trace_path)
        if not path.exists():
            return []
        records: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(cast("dict[str, object]", json.loads(line)))
        return records

    def trace_methods(self) -> list[str]:
        return [
            cast("str", record["method"])
            for record in self.trace()
            if record.get("event") == "request"
        ]

    def trace_request(self, method: str) -> dict[str, object] | None:
        for record in self.trace():
            if record.get("event") == "request" and record.get("method") == method:
                return cast("dict[str, object]", record.get("params") or {})
        return None


class CodexAdapterTests(unittest.TestCase):
    _harnesses: list[Harness]

    def __init__(self, method_name: str = "runTest") -> None:
        self._harnesses = []
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self._harnesses = []

    @override
    def tearDown(self) -> None:
        for harness in self._harnesses:
            harness.cleanup()

    def _harness(
        self,
        scenario: dict[str, object] | None = None,
        *,
        version: str = "0.155.1",
        pinned: bool = True,
        path_lookup: Callable[[str], str | None] | None = None,
        discovery_roots: tuple[Path, ...] | None = (),
        **adapter_kwargs: float,
    ) -> Harness:
        harness = Harness(
            scenario,
            version=version,
            pinned=pinned,
            path_lookup=path_lookup,
            discovery_roots=discovery_roots,
            **adapter_kwargs,
        )
        self._harnesses.append(harness)
        return harness

    # ── (2) version contract ───────────────────────────────────────────

    def test_supported_version_completes(self) -> None:
        harness = self._harness()
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)

    def test_old_version_fails_closed_without_session(self) -> None:
        harness = self._harness(version="0.115.0")
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None
        self.assertEqual("version_unsupported", result.calls[0].note)
        self.assertEqual([], harness.spawner.app_server_specs)

    def test_unparseable_version_fails_closed(self) -> None:
        harness = self._harness(version="not-a-version")
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None
        self.assertEqual("version_unparseable", result.calls[0].note)

    def test_version_probe_requires_codex_cli_prefix(self) -> None:
        harness = self._harness(version="banana")
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("version_unparseable", result.calls[0].note)

    # ── (3)+(4) controlled home + isolation posture ────────────────────

    def test_controlled_home_created_with_minimal_config(self) -> None:
        harness = self._harness()
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        home = harness.state_dir / "codex" / "codex-home"
        self.assertTrue(home.is_dir())
        mode = stat.S_IMODE(home.stat().st_mode)
        self.assertEqual(0o700, mode)
        config = home / CONTROLLED_CONFIG_NAME
        self.assertTrue(config.is_file())
        content = config.read_text(encoding="utf-8")
        self.assertNotIn("mcp_servers", content)
        # The scratch working directory is cleaned up after the call.
        scratch_root = harness.state_dir / "codex" / "scratch"
        self.assertEqual([], list(scratch_root.iterdir()))

    def test_isolation_spec_is_adapter_owned(self) -> None:
        harness = self._harness()
        _ = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual(1, len(harness.spawner.app_server_specs))
        spec = harness.spawner.app_server_specs[0]
        self.assertEqual((str(FAKE), "app-server"), spec.argv)
        # Minimal env only: no worker environment inheritance.
        self.assertEqual(
            {"PATH", "HOME", "CODEX_HOME"}, set(spec.env.keys()) - {"SR_FAKE_TRACE"}
        )
        home = harness.state_dir / "codex" / "codex-home"
        self.assertEqual(str(home), spec.env["CODEX_HOME"])
        self.assertTrue(spec.cwd.startswith(str(harness.state_dir / "codex" / "scratch")))

    def test_isolation_parameters_in_protocol_requests(self) -> None:
        harness = self._harness()
        result = harness.adapter.invoke(
            _call(reasoning_effort=EFFORT),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        thread_params = harness.trace_request("thread/start")
        assert thread_params is not None
        self.assertEqual(True, thread_params.get("ephemeral"))
        self.assertEqual("never", thread_params.get("approvalPolicy"))
        self.assertEqual("workspace-write", thread_params.get("sandbox"))
        turn_params = harness.trace_request("turn/start")
        assert turn_params is not None
        policy = cast("dict[str, object]", turn_params["sandboxPolicy"])
        self.assertEqual("workspaceWrite", policy.get("type"))
        self.assertEqual(False, policy.get("networkAccess"))
        self.assertEqual([turn_params.get("cwd")], policy.get("writableRoots"))
        # The dangerous mode is never sent anywhere.
        rendered = json.dumps(harness.trace())
        self.assertNotIn("dangerFullAccess", rendered)
        self.assertNotIn("dynamicTools", rendered)

    # ── (1) discovery paths ────────────────────────────────────────────

    def test_discovery_prefers_the_pinned_binary(self) -> None:
        harness = self._harness(pinned=True, path_lookup=_bwrap_lookup)
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        spec = harness.spawner.app_server_specs[0]
        self.assertEqual(str(FAKE), spec.argv[0])

    def test_discovery_falls_back_to_path_lookup(self) -> None:
        def lookup(name: str) -> str | None:
            if name == "codex":
                return str(FAKE)
            if name == "bwrap":
                return "/usr/bin/bwrap"
            return None

        harness = self._harness(pinned=False, path_lookup=lookup)
        binary, reason = discover_codex_binary(
            pinned_binary=None, discovery_roots=(), path_lookup=lookup
        )
        self.assertIsNone(reason)
        assert binary is not None
        self.assertEqual("path", binary.source)
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)

    def test_discovery_supports_the_extension_layout(self) -> None:
        from scarcity_router.providers.openai_codex_acquisition import (
            platform_directory,
        )

        platform_dir = platform_directory()
        if platform_dir is None:  # pragma: no cover - platform dependent
            self.skipTest("no evidenced platform directory on this host")
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / ".vscode" / "extensions"
            ext = root / "openai.chatgpt-9.0.0-linux-x64"
            platform_bin = ext / "bin" / platform_dir
            platform_bin.mkdir(parents=True)
            binary = platform_bin / "codex"
            _ = binary.write_bytes(b"#!/bin/sh\nexit 0\n")
            _ = os.chmod(binary, 0o755)
            _ = (platform_bin / "codex-package.json").write_text(
                json.dumps(
                    {"layoutVersion": 1, "variant": "codex", "version": "0.155.1"}
                ),
                encoding="utf-8",
            )

            def lookup(name: str) -> str | None:
                if name == "bwrap":
                    return "/usr/bin/bwrap"
                return None

            harness = self._harness(
                pinned=False, path_lookup=lookup, discovery_roots=(root,)
            )
            result = harness.adapter.invoke(
                _call(),
                cancel_event=threading.Event(),
                deadline=_future_deadline(),
                emit=lambda chunk: None,
            )
            self.assertEqual("completed", result.status)

    def test_missing_installation_is_ineligible(self) -> None:
        harness = self._harness(
            pinned=False,
            path_lookup=lambda name: (
                "/usr/bin/bwrap" if name == "bwrap" else None
            ),
        )
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("source_unavailable", result.calls[0].note)

    def test_pinned_path_must_be_a_regular_non_symlink_executable(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "real-codex"
            _ = target.write_bytes(b"#!/bin/sh\nexit 0\n")
            _ = os.chmod(target, 0o755)
            link = Path(tmp) / "linked-codex"
            os.symlink(target, link)
            binary, reason = discover_codex_binary(
                pinned_binary=link, discovery_roots=(), path_lookup=lambda name: None
            )
            self.assertIsNone(binary)
            self.assertEqual("pinned_binary_invalid", reason)
            binary, reason = discover_codex_binary(
                pinned_binary=target, discovery_roots=(), path_lookup=lambda name: None
            )
            self.assertIsNone(reason)
            assert binary is not None
            self.assertEqual("pinned", binary.source)

    # ── (5) auth diagnostics ───────────────────────────────────────────

    def test_missing_auth_is_ineligible_with_login_remediation(self) -> None:
        harness = self._harness(_scenario(account="none", turn={"status": "completed"}))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None
        self.assertIn("auth_missing", result.calls[0].note)
        self.assertIn("codex login", result.calls[0].note)

    def test_api_key_auth_is_not_eligible_for_execution(self) -> None:
        harness = self._harness(_scenario(account="apikey", turn={"status": "completed"}))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None
        self.assertIn("auth_payg_unsupported", result.calls[0].note)
        # Remediation is the official login, never token copying.
        self.assertIn("codex login", result.calls[0].note)

    def test_bounded_d018_refresh_recovers_then_executes(self) -> None:
        harness = self._harness(
            _scenario(account="internal-error-then-chatgpt", turn={"status": "completed"})
        )
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        account_reads = [
            record
            for record in harness.trace()
            if record.get("event") == "request"
            and record.get("method") == "account/read"
        ]
        # Exactly: initial read, one refresh, one retry — no loops.
        self.assertEqual(3, len(account_reads))
        params = cast("dict[str, object]", account_reads[1].get("params") or {})
        self.assertEqual(True, params.get("refreshToken"))

    def test_failed_recovery_stays_unverified_without_loops(self) -> None:
        harness = self._harness(
            _scenario(account="refresh-still-error", turn={"status": "completed"})
        )
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("auth_unverified", result.calls[0].note)
        account_reads = [
            record
            for record in harness.trace()
            if record.get("event") == "request"
            and record.get("method") == "account/read"
        ]
        # Initial read + one refresh; a FAILED refresh is not retried (the
        # same bounded sequence as the D-018 collector implementation).
        self.assertEqual(2, len(account_reads))

    # ── (6)+(19) model/effort binding ──────────────────────────────────

    def test_pinned_model_and_effort_are_sent_exactly(self) -> None:
        harness = self._harness()
        result = harness.adapter.invoke(
            _call(reasoning_effort="low"),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        params = harness.trace_request("turn/start")
        assert params is not None
        self.assertEqual(SLUG, params.get("model"))
        self.assertEqual("low", params.get("effort"))

    def test_unlisted_model_is_rejected_before_execution(self) -> None:
        harness = self._harness()
        call = AdapterCall(
            resource=_resource(),
            model=ModelIdentity(provider="openai", model="gpt-nope", variant="codex"),
            messages=(AdapterMessage(role="user", content="hi"),),
        )
        result = harness.adapter.invoke(
            call,
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("model_not_listed", result.calls[0].note)
        self.assertIsNone(harness.trace_request("turn/start"))

    def test_unsupported_effort_is_rejected_before_execution(self) -> None:
        harness = self._harness()
        result = harness.adapter.invoke(
            _call(reasoning_effort="xhigh"),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("effort_not_supported", result.calls[0].note)
        self.assertIsNone(harness.trace_request("turn/start"))

    # ── (7) role/history mapping ───────────────────────────────────────

    def test_role_and_history_mapping_preserves_structure(self) -> None:
        harness = self._harness()
        messages = (
            AdapterMessage(role="system", content="be brief"),
            AdapterMessage(role="developer", content="use tools wisely"),
            AdapterMessage(role="user", content="first question"),
            AdapterMessage(role="assistant", content="first answer"),
            AdapterMessage(role="user", content="second question"),
        )
        result = harness.adapter.invoke(
            _call(messages=messages),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        thread_params = harness.trace_request("thread/start")
        assert thread_params is not None
        self.assertEqual("be brief", thread_params.get("baseInstructions"))
        self.assertEqual("use tools wisely", thread_params.get("developerInstructions"))
        inject = harness.trace_request("thread/inject_items")
        assert inject is not None
        items = cast("list[dict[str, object]]", inject["items"])
        self.assertEqual(2, len(items))
        self.assertEqual("user", items[0]["role"])
        content = cast("list[dict[str, object]]", items[0]["content"])
        self.assertEqual(
            ({"type": "input_text", "text": "first question"},), 
            tuple(content),
        )
        self.assertEqual("assistant", items[1]["role"])
        turn_params = harness.trace_request("turn/start")
        assert turn_params is not None
        turn_input = cast("list[dict[str, object]]", turn_params["input"])
        self.assertEqual("second question", turn_input[0]["text"])

    def test_conversation_not_ending_with_user_message_is_rejected(self) -> None:
        harness = self._harness()
        messages = (
            AdapterMessage(role="user", content="hi"),
            AdapterMessage(role="assistant", content="hello"),
        )
        result = harness.adapter.invoke(
            _call(messages=messages),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual(
            "conversation_must_end_with_user_message", result.calls[0].note
        )
        self.assertEqual([], harness.spawner.specs)

    # ── (8) structured output ──────────────────────────────────────────

    def test_json_schema_maps_to_output_schema(self) -> None:
        harness = self._harness()
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        result = harness.adapter.invoke(
            _call(
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "answer", "schema": schema},
                }
            ),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        params = harness.trace_request("turn/start")
        assert params is not None
        self.assertEqual(schema, params.get("outputSchema"))

    def test_schema_less_json_object_is_rejected_not_dropped(self) -> None:
        harness = self._harness()
        result = harness.adapter.invoke(
            _call(response_format={"type": "json_object"}),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual(
            "response_format_json_object_unsupported", result.calls[0].note
        )
        self.assertEqual([], harness.spawner.specs)

    # ── (9) tool-bearing requests rejected before execution ────────────

    def test_tool_calls_are_rejected_before_any_execution(self) -> None:
        harness = self._harness()
        messages = (
            AdapterMessage(
                role="assistant",
                content=None,
                tool_calls=(
                    AdapterToolCall(id="c1", name="lookup", arguments="{}"),
                ),
            ),
            AdapterMessage(role="user", content="go"),
        )
        result = harness.adapter.invoke(
            _call(messages=messages),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("tool_messages_unsupported", result.calls[0].note)
        # Nothing was spawned at all: not even the version probe.
        self.assertEqual([], harness.spawner.specs)

    def test_explicit_tools_are_rejected_before_any_execution(self) -> None:
        harness = self._harness()
        result = harness.adapter.invoke(
            _call(tools=({"type": "function", "function": {"name": "f"}},)),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("tool_calls_unsupported", result.calls[0].note)
        self.assertEqual([], harness.spawner.specs)

    # ── (10) streaming ─────────────────────────────────────────────────

    def test_streaming_emits_deltas_then_final_message(self) -> None:
        harness = self._harness()
        emitted: list[str] = []
        result = harness.adapter.invoke(
            _call(stream=True),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: emitted.append(chunk.text or ""),
        )
        self.assertEqual("completed", result.status)
        self.assertEqual(["Hel", "lo"], emitted)
        assert result.message is not None
        self.assertEqual("Hello", result.message.content)
        self.assertEqual("stop", result.finish_reason)

    def test_non_streaming_call_never_emits(self) -> None:
        harness = self._harness()
        emitted: list[AdapterStreamChunk] = []
        result = harness.adapter.invoke(
            _call(stream=False),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=emitted.append,
        )
        self.assertEqual("completed", result.status)
        self.assertEqual([], emitted)

    # ── (11)+(12) protocol drift and bounded unknown events ────────────

    def test_malformed_line_fails_closed(self) -> None:
        harness = self._harness(
            _scenario(turn={"malformedFirst": True, "status": "completed"}),
        )
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("protocol_malformed", result.calls[0].note)

    def test_deeply_nested_event_fails_closed(self) -> None:
        harness = self._harness(_scenario(turn={"deepNested": True, "status": "completed"}))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("protocol_malformed", result.calls[0].note)

    def test_duplicate_keys_fail_closed(self) -> None:
        harness = self._harness(_scenario(turn={"duplicateKeys": True, "status": "completed"}))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("protocol_malformed", result.calls[0].note)

    def test_oversized_line_fails_closed(self) -> None:
        harness = self._harness(_scenario(turn={"oversizedLine": True, "status": "completed"}))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("protocol_budget_exceeded", result.calls[0].note)

    def test_unknown_notifications_are_ignored_and_bounded(self) -> None:
        harness = self._harness(
            _scenario(turn={"unknownNotifications": 5, "status": "completed"})
        )
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)

    def test_unknown_notification_budget_expires_fail_closed(self) -> None:
        harness = self._harness(
            _scenario(turn={"unknownNotifications": 2000, "status": "completed"})
        )
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(60.0),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("notification_budget_exceeded", result.calls[0].note)

    def test_unknown_turn_status_fails_closed(self) -> None:
        harness = self._harness(_scenario(turn={"status": "bogus"}))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("turn_status_unrecognized", result.calls[0].note)

    # ── (13) usage mapping ─────────────────────────────────────────────

    def test_provider_reported_usage_is_carried_honestly(self) -> None:
        harness = self._harness()
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        usage = result.calls[0].provider_reported_usage
        assert usage is not None
        self.assertEqual((11, 7), (usage.prompt_tokens, usage.completion_tokens))

    def test_absent_usage_stays_absent(self) -> None:
        harness = self._harness(_scenario(turn={"status": "completed"}))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("completed", result.status)
        self.assertIsNone(result.calls[0].provider_reported_usage)

    def test_failed_turn_maps_to_safe_note_from_documented_vocabulary(self) -> None:
        harness = self._harness(
            _scenario(turn={"status": "failed", "errorInfo": "usageLimitExceeded"}),
        )
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None
        self.assertIn("usage limit exceeded", result.calls[0].note)
        # The free-text error body never reaches the note.
        self.assertNotIn("SYNTHETIC free text", result.calls[0].note)

    # ── (14) cancellation ──────────────────────────────────────────────

    def test_cancellation_before_start_never_spawns(self) -> None:
        harness = self._harness()
        cancel = threading.Event()
        cancel.set()
        result = harness.adapter.invoke(
            _call(),
            cancel_event=cancel,
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("cancelled", result.status)
        self.assertEqual([], harness.spawner.specs)

    def test_mid_turn_cancellation_interrupts_and_reports_cancelled(self) -> None:
        harness = self._harness()
        cancel = threading.Event()

        def emit(chunk: AdapterStreamChunk) -> None:
            if chunk.kind == "text_delta":
                cancel.set()

        result = harness.adapter.invoke(
            _call(stream=True),
            cancel_event=cancel,
            deadline=_future_deadline(30.0),
            emit=emit,
        )
        self.assertEqual("cancelled", result.status)
        interrupt = harness.trace_request("turn/interrupt")
        assert interrupt is not None
        self.assertEqual("thr-synthetic-1", interrupt.get("threadId"))
        self.assertEqual("turn-synthetic-1", interrupt.get("turnId"))

    def test_approval_requests_are_cancelled_never_accepted(self) -> None:
        harness = self._harness(_scenario(turn={"approval": True, "status": "completed"}))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(60.0),
            emit=lambda chunk: None,
        )
        # cancel denies AND interrupts the turn (protocol semantics), so a
        # compliant adapter can never report a completed result here.
        self.assertEqual("cancelled", result.status)
        approvals = [
            record
            for record in harness.trace()
            if record.get("event") == "approval"
        ]
        self.assertEqual(1, len(approvals))
        self.assertEqual("cancel", approvals[0].get("decision"))

    # ── (15) startup timeout and execution deadline ────────────────────

    def test_startup_timeout_fails_closed_bounded(self) -> None:
        harness = self._harness(_scenario(behavior="stall"), startup_timeout=0.5)
        started = datetime.now(timezone.utc)
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(30.0),
            emit=lambda chunk: None,
        )
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        self.assertEqual("failed", result.status)
        self.assertEqual("protocol_timeout", result.calls[0].note)
        self.assertLess(elapsed, 15.0)

    def test_execution_deadline_interrupts_and_reports_cancelled(self) -> None:
        harness = self._harness(
            _scenario(turn={"pauseBeforeCompleted": 30.0, "status": "completed"})
        )
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(1.0),
            emit=lambda chunk: None,
        )
        self.assertEqual("cancelled", result.status)
        assert result.calls[0].note is not None
        self.assertIn("deadline", result.calls[0].note)
        self.assertIsNotNone(harness.trace_request("turn/interrupt"))

    def test_already_past_deadline_never_spawns(self) -> None:
        harness = self._harness()
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_canonical(datetime.now(timezone.utc) - timedelta(seconds=1)),
            emit=lambda chunk: None,
        )
        self.assertEqual("cancelled", result.status)
        self.assertEqual([], harness.spawner.specs)

    # ── (16)+(17) process loss before/after start ──────────────────────

    def test_process_exit_before_turn_is_a_definitive_failure(self) -> None:
        harness = self._harness(_scenario(behavior="exit-after-init"))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("codex_process_lost", result.calls[0].note)
        self.assertEqual(1, len(harness.spawner.app_server_specs))

    def test_process_loss_after_turn_start_is_ambiguous(self) -> None:
        harness = self._harness(_scenario(turn={"deltas": ["par"], "exitMidTurn": True}))
        result = harness.adapter.invoke(
            _call(stream=True),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        observation = result.calls[0]
        self.assertEqual("unknown", observation.status)
        assert observation.note is not None
        self.assertIn("unknown", observation.note)

    # ── (18) no retry / single invocation ──────────────────────────────

    def test_every_outcome_uses_exactly_one_codex_invocation(self) -> None:
        scenarios = (
            _default_scenario(),
            _scenario(behavior="exit-after-init"),
            _scenario(turn={"status": "failed", "errorInfo": "sandboxError"}),
            _scenario(account="apikey"),
        )
        for scenario in scenarios:
            harness = self._harness(scenario)
            _ = harness.adapter.invoke(
                _call(),
                cancel_event=threading.Event(),
                deadline=_future_deadline(),
                emit=lambda chunk: None,
            )
            self.assertEqual(
                1, len(harness.spawner.app_server_specs), msg=str(scenario)[:60]
            )

    # ── (21) bounded stderr handling ───────────────────────────────────

    def test_stderr_is_captured_with_a_hard_cap_never_forwarded(self) -> None:
        harness = self._harness(_scenario(turn={"stderrSpam": True, "status": "completed"}))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        # A chatty runtime never blocks the call and its stderr content
        # never reaches any result field.
        self.assertEqual("completed", result.status)
        self.assertIsNone(result.calls[0].note)
        assert result.message is not None
        assert result.message.content is not None
        self.assertNotIn("SYNTHETIC-STDERR", result.message.content)

    # ── (22)+(23) allowlist and resource ownership ─────────────────────

    def test_allowlist_miss_never_invokes_the_adapter(self) -> None:
        harness = self._harness()
        registry = LocalAdapterRegistry()
        registry.register(harness.adapter)
        with self.assertRaises(AdapterNotAllowedError):
            _ = run_allowlisted(
                registry,
                adapter_id="ollama",
                call=_call(),
                cancel_event=threading.Event(),
                deadline=_future_deadline(),
                emit=lambda chunk: None,
            )
        self.assertEqual([], harness.spawner.specs)

    def test_adapter_serves_only_its_configured_resource(self) -> None:
        harness = self._harness()
        self.assertEqual(("codex-local",), harness.adapter.resource_ids)
        result = harness.adapter.invoke(
            _call(resource=_resource(resource_id="other-resource")),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual("resource_not_served", result.calls[0].note)
        self.assertEqual([], harness.spawner.specs)

    # ── (24) clean shutdown, no orphans ────────────────────────────────

    def test_process_is_reaped_after_every_outcome(self) -> None:
        scenarios = (
            _default_scenario(),
            _scenario(behavior="exit-after-init"),
            _scenario(turn={"pauseBeforeCompleted": 30.0, "status": "completed"}),
        )
        for scenario in scenarios:
            harness = self._harness(scenario)
            _ = harness.adapter.invoke(
                _call(),
                cancel_event=threading.Event(),
                deadline=_future_deadline(3.0),
                emit=lambda chunk: None,
            )
            self.assertTrue(
                harness.spawner.all_reaped(), msg="child not reaped"
            )

    def test_default_spawner_terminates_and_reaps(self) -> None:
        spec = CodexSpawnSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(60)"),
            env={"PATH": os.environ.get("PATH", "")},
            cwd=os.getcwd(),
        )
        proc = default_codex_spawner(spec)
        popen = cast("subprocess.Popen[bytes]", cast("object", proc))
        try:
            self.assertIsNone(popen.poll())
        finally:
            reaped = terminate_codex_process(proc)
        self.assertTrue(reaped)
        self.assertIsNotNone(popen.poll())
        for stream in (popen.stdin, popen.stdout, popen.stderr):
            if stream is not None:
                stream.close()

    def test_unadopted_controlled_home_fails_closed(self) -> None:
        harness = self._harness(_scenario(init="mismatch-home"))
        result = harness.adapter.invoke(
            _call(),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        self.assertEqual(
            "controlled_home_not_adopted", result.calls[0].note
        )

    def test_version_probe_unit(self) -> None:
        with TemporaryDirectory() as tmp:
            good = Path(tmp) / "codex-good"
            _ = good.write_bytes(b"#!/bin/sh\nexit 0\n")
            _ = os.chmod(good, 0o755)
            spawner = FakeCodexSpawner(version="0.155.1")
            version, reason = probe_codex_version(good, spawner=spawner)
            self.assertIsNone(reason)
            self.assertEqual((0, 155, 1), version)
            spawner.reap_all()
            spawner = FakeCodexSpawner(version="0.115.0")
            version, reason = probe_codex_version(good, spawner=spawner)
            self.assertIsNone(version)
            self.assertEqual("version_unsupported", reason)
            spawner.reap_all()
            spawner = FakeCodexSpawner(version="banana")
            version, reason = probe_codex_version(good, spawner=spawner)
            self.assertIsNone(version)
            self.assertEqual("version_unparseable", reason)
            spawner.reap_all()

    # ── resource snapshots ─────────────────────────────────────────────

    def test_snapshot_ok_when_every_gate_passes(self) -> None:
        harness = self._harness()
        snapshots = harness.adapter.resource_snapshots(T_NOW)
        self.assertEqual(1, len(snapshots))
        self.assertEqual("ok", snapshots[0].health.status)
        self.assertEqual("openai", snapshots[0].identity.provider)
        self.assertEqual((), snapshots[0].quota_facts)

    def test_snapshot_unavailable_when_no_binary_exists(self) -> None:
        harness = self._harness(
            pinned=False,
            path_lookup=lambda name: (
                "/usr/bin/bwrap" if name == "bwrap" else None
            ),
        )
        snapshots = harness.adapter.resource_snapshots(T_NOW)
        self.assertEqual("unavailable", snapshots[0].health.status)
        self.assertEqual(
            "source_unavailable", snapshots[0].health.diagnostics[0].code
        )

    def test_snapshot_unavailable_when_version_unsupported(self) -> None:
        harness = self._harness(version="0.100.0")
        snapshots = harness.adapter.resource_snapshots(T_NOW)
        self.assertEqual("unsupported", snapshots[0].health.status)
        self.assertEqual(
            "unsupported_source", snapshots[0].health.diagnostics[0].code
        )

    def test_snapshot_auth_required_when_auth_is_api_key(self) -> None:
        # account.type is a STRUCTURED verdict (unlike the conflated
        # rate-limits error surface), so auth_required is honestly reachable.
        harness = self._harness(_scenario(account="apikey", turn={"status": "completed"}))
        snapshots = harness.adapter.resource_snapshots(T_NOW)
        self.assertEqual("auth_required", snapshots[0].health.status)
        self.assertEqual(
            "auth_required", snapshots[0].health.diagnostics[0].code
        )

    def test_snapshot_unsupported_when_sandbox_prerequisite_missing(self) -> None:
        harness = self._harness(path_lookup=lambda name: None)
        snapshots = harness.adapter.resource_snapshots(T_NOW)
        self.assertEqual("unsupported", snapshots[0].health.status)
        self.assertEqual(
            "unsupported_source", snapshots[0].health.diagnostics[0].code
        )

    def test_snapshot_unknown_when_auth_unverifiable(self) -> None:
        harness = self._harness(
            _scenario(account="refresh-still-error", turn={"status": "completed"})
        )
        snapshots = harness.adapter.resource_snapshots(T_NOW)
        self.assertEqual("unknown", snapshots[0].health.status)
        self.assertEqual(
            "telemetry_unknown", snapshots[0].health.diagnostics[0].code
        )


class UnitTests(unittest.TestCase):
    """Focused unit coverage of the pure mapping helpers."""

    def test_sandbox_verdicts_are_honest(self) -> None:
        self.assertIsNone(
            check_sandbox_availability(
                platform_name="linux",
                release="6.x-generic",
                path_lookup=_bwrap_lookup,
            )
        )
        self.assertEqual(
            "sandbox_prerequisite_missing",
            check_sandbox_availability(
                platform_name="linux", release="6.x-generic", path_lookup=lambda n: None
            ),
        )
        self.assertEqual(
            "wsl1_unsupported",
            check_sandbox_availability(
                platform_name="linux",
                release="4.4.0-Microsoft",
                path_lookup=_bwrap_lookup,
            ),
        )
        self.assertIsNone(
            check_sandbox_availability(
                platform_name="linux",
                release="5.15.167.4-microsoft-standard-WSL2",
                path_lookup=_bwrap_lookup,
            )
        )
        self.assertEqual(
            "platform_not_evidenced",
            check_sandbox_availability(
                platform_name="win32", release="", path_lookup=_bwrap_lookup
            ),
        )
        self.assertEqual(
            "platform_not_evidenced",
            check_sandbox_availability(
                platform_name="darwin", release="", path_lookup=_bwrap_lookup
            ),
        )

    def test_map_conversation_units(self) -> None:
        mapped = map_conversation(
            (
                AdapterMessage(role="system", content="s1"),
                AdapterMessage(role="system", content="s2"),
                AdapterMessage(role="user", content="u1"),
                AdapterMessage(role="assistant", content="a1"),
                AdapterMessage(role="user", content="u2"),
            )
        )
        self.assertEqual("s1\n\ns2", mapped.base_instructions)
        self.assertIsNone(mapped.developer_instructions)
        self.assertEqual(2, len(mapped.history_items))
        self.assertEqual("u2", mapped.turn_input)

    def test_structured_schema_bounds(self) -> None:
        schema = {"type": "object"}
        self.assertEqual(
            schema,
            validate_structured_schema(
                {"type": "json_schema", "json_schema": {"name": "n", "schema": schema}}
            ),
        )
        self.assertIsNone(validate_structured_schema({"type": "text"}))
        self.assertIsNone(validate_structured_schema(None))
        with self.assertRaises(Exception) as caught:
            _ = validate_structured_schema(
                {"type": "json_schema", "json_schema": {"schema": "not-object"}}
            )
        self.assertEqual("response_format_invalid", str(caught.exception))
        deep: dict[str, object] = {"leaf": 1}
        for _ in range(40):
            deep = {"n": deep}
        with self.assertRaises(Exception) as caught_deep:
            _ = validate_structured_schema(
                {"type": "json_schema", "json_schema": {"schema": deep}}
            )
        self.assertEqual("response_format_too_deep", str(caught_deep.exception))

    def test_parse_model_listing_and_verification(self) -> None:
        models = parse_model_listing(
            cast(
                "object",
                {
                "data": [
                    {
                        "id": "a",
                        "model": "a",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "high"},
                        ],
                    },
                    {"id": "b", "model": "b", "supportedReasoningEfforts": []},
                ]
                },
            )
        )
        self.assertEqual(("low", "high"), models["a"])
        self.assertIsNone(verify_model_and_effort(models, "a", "high"))
        self.assertEqual("model_not_listed", verify_model_and_effort(models, "z", None))
        self.assertEqual(
            "effort_not_supported", verify_model_and_effort(models, "b", "high")
        )
        with self.assertRaises(Exception) as caught_model:
            _ = parse_model_listing({"data": [{"model": 3}]})
        self.assertEqual("model_list_malformed", str(caught_model.exception))

    def test_parse_token_usage_honesty(self) -> None:
        usage = parse_token_usage(
            {
                "last": {"inputTokens": 5, "outputTokens": 3},
                "total": {"inputTokens": 50, "outputTokens": 30},
            }
        )
        assert usage is not None
        self.assertEqual((5, 3), (usage.prompt_tokens, usage.completion_tokens))
        self.assertIsNone(parse_token_usage({"last": None, "total": None}))
        self.assertIsNone(parse_token_usage({"total": {"inputTokens": -1}}))
        self.assertIsNone(parse_token_usage("junk"))

    def test_safe_error_note_uses_only_documented_vocabulary(self) -> None:
        self.assertEqual(
            "the codex turn failed: usage limit exceeded",
            safe_error_note(
                {"codexErrorInfo": "usageLimitExceeded", "message": "SYNTHETIC"}
            ),
        )
        self.assertEqual(
            "the codex turn failed: backend connection failed",
            safe_error_note({"codexErrorInfo": {"httpConnectionFailed": {}}}),
        )
        self.assertEqual(
            "the codex turn failed", safe_error_note({"codexErrorInfo": "mystery"})
        )
        self.assertEqual("the codex turn failed", safe_error_note("junk"))

    def test_result_never_completed_after_confirmed_cancellation(self) -> None:
        # The event loop checks cancellation again before reporting a
        # completed turn; a cancel racing the completed notification yields
        # the cancelled result.
        harness = Harness(_default_scenario())
        try:
            cancel = threading.Event()

            def emit(_chunk: AdapterStreamChunk) -> None:
                cancel.set()

            result = harness.adapter.invoke(
                _call(stream=True),
                cancel_event=cancel,
                deadline=_future_deadline(),
                emit=emit,
            )
            self.assertEqual("cancelled", result.status)
        finally:
            harness.cleanup()


class WorkerWiringTests(unittest.TestCase):
    """The M05 worker CLI composes the codex adapter from its own flags."""

    def test_allow_codex_registers_the_codex_adapter(self) -> None:
        with TemporaryDirectory() as tmp:
            registry = build_registry(
                {"allow_codex": True, "resource": "codex-res"},
                state_dir=tmp,
            )
            assert registry is not None
            adapter = registry.resolve(CODEX_ADAPTER_ID)
            assert adapter is not None
            self.assertEqual(("codex-res",), adapter.resource_ids)

    def test_both_adapters_register_alongside(self) -> None:
        with TemporaryDirectory() as tmp:
            registry = build_registry(
                {
                    "allow_ollama": True,
                    "resource": "ollama-res",
                    "allow_codex": True,
                    "codex_resource": "codex-res",
                },
                state_dir=tmp,
            )
            assert registry is not None
            self.assertEqual(("codex", "ollama"), registry.adapter_ids())

    def test_codex_only_accepts_the_shared_resource_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            registry = build_registry(
                {"allow_codex": True, "resource": "shared-res"},
                state_dir=tmp,
            )
            assert registry is not None
            adapter = registry.resolve(CODEX_ADAPTER_ID)
            assert adapter is not None
            self.assertEqual(("shared-res",), adapter.resource_ids)

    def test_missing_resource_fails_closed(self) -> None:
        with self.assertRaises(WorkerConfigError):
            _ = build_registry({"allow_codex": True}, state_dir=".")
        with self.assertRaises(WorkerConfigError):
            _ = build_registry(
                {"allow_ollama": True, "resource": "a", "allow_codex": True},
                state_dir=".",
            )

    def test_flags_parse(self) -> None:
        arguments = cast(
            "dict[str, object]",
            vars(
                build_parser().parse_args(
                    [
                        "run",
                        "--allow-codex",
                        "--resource",
                        "res-1",
                        "--codex-bin",
                        "/usr/local/bin/codex",
                    ]
                )
            ),
        )
        self.assertTrue(arguments["allow_codex"])
        self.assertEqual("/usr/local/bin/codex", arguments["codex_bin"])
        self.assertFalse(bool(arguments.get("allow_ollama")))


if __name__ == "__main__":
    _ = unittest.main()
