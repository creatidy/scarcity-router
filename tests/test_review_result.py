"""Offline stdlib subprocess capture tests; no inference or live identity claims."""

from pathlib import Path
from dataclasses import dataclass, replace
import json
import os
import signal
import sys
import tempfile
import unittest
from typing import override
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.review_result import (
    Code, Context, Diagnostic, EventCapture, INPUT_LIMIT, Json, PrivateStore, PublicSource,
    Rejected, Stage, TextPolicy, agent_manager_handoff_action, encode, inspect,
    inspect_agent_manager_result, parse, recover, run_prepared, status_code,
)

CONTEXT = Context("a" * 40, "b" * 40, 1, "synthetic-model", "synthetic-job")


def result(verdict: str = "COMMENT") -> dict[str, Json]:
    return {"reviewed_head": CONTEXT.head, "reviewed_base": CONTEXT.base,
            "verdict": verdict, "findings": [], "limitations": [], "checks_run": []}


def agent_manager_result(verdict: str = "COMMENT") -> dict[str, Json]:
    return {**result(verdict), "reviewed_merge_base": "c" * 40, "ux_impact": "none"}


@dataclass
class TestStores:
    temporary: tempfile.TemporaryDirectory[str]
    store: PrivateStore
    native: PrivateStore


@unittest.skipUnless(os.name == "posix", "capture helper requires POSIX no-follow dirfd capabilities")
class ReviewResultTests(unittest.TestCase):
    state: TestStores | None = None

    @property
    def temporary(self) -> tempfile.TemporaryDirectory[str]:
        assert self.state is not None
        return self.state.temporary

    @property
    def store(self) -> PrivateStore:
        assert self.state is not None
        return self.state.store

    @store.setter
    def store(self, value: PrivateStore) -> None:
        assert self.state is not None
        self.state.store = value

    @property
    def native(self) -> PrivateStore:
        assert self.state is not None
        return self.state.native

    @native.setter
    def native(self, value: PrivateStore) -> None:
        assert self.state is not None
        self.state.native = value
    @staticmethod
    def make_store(parent: Path, name: str) -> PrivateStore:
        path = parent / name
        path.mkdir(mode=0o700)
        return PrivateStore(path, parent)

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        parent = Path(temporary.name)
        self.state = TestStores(temporary, self.make_store(parent, "receipts"), self.make_store(parent, "native"))

    @override
    def tearDown(self) -> None:
        self.store.close()
        self.native.close()
        self.temporary.cleanup()

    def launch(self, mode: str, *, timeout: float = 5,
               progress: list[dict[str, Json]] | None = None,
               policy: TextPolicy | None = None) -> Diagnostic:
        # No owner's ambient environment or home reaches the child.
        env = {"HOME": str(self.native.path), "TMPDIR": str(self.native.path),
               "PYTHONPATH": str(ROOT), "SYNTHETIC_VALUE": "fixture-only"}
        return run_prepared(self.store, self.native, CONTEXT,
                            [sys.executable, str(ROOT / "tests/review_result_fixture.py"), mode,
                              CONTEXT.head, CONTEXT.base], b"synthetic prompt", env,
                             policy if policy is not None else TextPolicy(),
                            executable_version="synthetic Python fixture", timeout=timeout,
                            progress=progress.append if progress is not None else None)

    def reset_stores(self) -> None:
        self.store.close()
        self.native.close()
        parent = Path(self.temporary.name)
        name = str(len(list(parent.iterdir())))
        self.store = self.make_store(parent, "receipts" + name)
        self.native = self.make_store(parent, "native" + name)

    def test_all_verdicts_are_capture_not_approval(self) -> None:
        for verdict in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
            with self.subTest(verdict=verdict):
                events: list[dict[str, Json]] = []
                captured = self.launch(verdict, progress=events)
                self.assertTrue(captured.complete)
                self.assertEqual(captured.actual_verdict, verdict)
                self.assertEqual(status_code(captured), 0)
                self.assertFalse(captured.parent_approval)
                self.assertEqual(captured.model_identity, "UNVERIFIED")
                self.assertEqual(events[0]["lifecycle"], "started")
                self.assertEqual(events[-1]["lifecycle"], "validated")
                for name in ("launch.claim", "spawn.json", "exit.json", "turn.json", "receipt.json"):
                    self.assertEqual(self.store.info(name).st_mode & 0o777, 0o600)
                    data = self.store.read(name)[0]
                    self.assertNotIn(b"PRIVATE", data)
                    self.assertNotIn(b"synthetic prompt", data)
                self.assertFalse((self.native.path / "last-message.json").exists())
                self.reset_stores()

    def test_subprocess_rejection_diagnostics(self) -> None:
        cases = {"malformed": Code.JSON, "duplicate": Code.DUPLICATE, "nonfinite": Code.NUMBER,
                 "missing-field": Code.FIELDS, "unknown-field": Code.FIELDS,
                 "wrong-type": Code.TYPE, "revision": Code.REVISION, "unsafe": Code.UNSAFE,
                 "forbidden-field": Code.UNSAFE, "mode": Code.MODE, "conflict": Code.CONFLICT,
                 "file-malformed": Code.JSON, "file-unsafe": Code.UNSAFE}
        for mode, expected in cases.items():
            with self.subTest(mode=mode):
                captured = self.launch(mode)
                self.assertEqual(captured.code, expected)
                self.assertFalse(captured.complete)
                self.assertEqual(status_code(captured), 2)
                receipt = self.store.read("receipt.json")[0]
                self.assertNotIn(b"Bearer", receipt)
                self.assertNotIn(b"synthetic-credential-value", receipt)
                if mode == "mode":
                    self.assertEqual(captured.observed_mode, "0644")
                if mode == "missing-field":
                    self.assertIn("checks_run", captured.missing_fields)
                if mode == "wrong-type":
                    self.assertEqual(captured.field_types["limitations"], "int")
                if mode == "unknown-field":
                    self.assertEqual(captured.unknown_field_count, 1)
                    self.assertNotIn(b"untrusted", receipt)
                self.reset_stores()

    def test_incomplete_failed_nonzero_timeout_remain_distinct(self) -> None:
        for mode, code in (("incomplete", Code.INCOMPLETE), ("interrupted", Code.FAILED),
                           ("nonzero", Code.EXIT), ("timeout", Code.TIMEOUT),
                           ("later-incomplete", Code.INCOMPLETE), ("jsonl-malformed", Code.FAILED)):
            with self.subTest(mode=mode):
                captured = self.launch(mode, timeout=0.2 if mode == "timeout" else 5)
                self.assertIn(code, captured.execution_failures)
                self.assertFalse(captured.complete)
                self.assertFalse(captured.parent_approval)
                self.reset_stores()

    def test_missing_file_uses_last_completed_message_and_recovers_lost_handle(self) -> None:
        captured = self.launch("missing-file")
        self.assertTrue(captured.complete)
        self.assertEqual(captured.actual_verdict, "COMMENT")
        self.assertEqual(captured.source, "jsonl_last_completed_agent_message")
        self.store.close()
        self.store = PrivateStore(self.store.path, self.store.path.parent)
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)
        with self.assertRaises(Rejected) as error:
            _ = self.launch("APPROVE")
        self.assertEqual(error.exception.code, Code.LAUNCH)

    def test_corrupted_receipt_cannot_erase_terminal_native_rejection(self) -> None:
        for mode, raw, expected in (("file-unsafe", b"{", Code.JSON), ("file-unsafe", b"[]", Code.TYPE),
                                    ("file-unsafe", b"{}", Code.TYPE),
                                    ("COMMENT", b"{", Code.JSON), ("COMMENT", b"[]", Code.TYPE)):
            self.reset_stores()
            with self.subTest(mode=mode, raw=raw):
                self.assertEqual(self.launch(mode).code, Code.UNSAFE if mode == "file-unsafe" else Code.OK)
                self.assertFalse((self.native.path / "last-message.json").exists())
                turn = parse(self.store.read("turn.json")[0])
                assert isinstance(turn, dict)
                self.assertEqual(turn["last"], result("COMMENT"))
                path = self.store.path / "receipt.json"
                _ = path.write_bytes(raw)
                for _ in range(2):
                    with self.assertRaises(Rejected) as caught:
                        _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(path.read_bytes(), raw)

    def test_invalid_required_receipt_cannot_be_rebuilt_as_success(self) -> None:
        mutations: list[tuple[str, Json]] = [
            ("NOT_APPROVAL", None), ("NOT_APPROVAL", False), ("terminal", None), ("terminal", 1),
            ("binding", None), ("binding", {}), ("diagnostic", None), ("diagnostic", {}),
            ("diagnostic.complete", None), ("diagnostic.complete", 1), ("diagnostic.complete", 0),
            ("source_rejection", []), ("native_identity", []),
        ]
        for mode in ("file-unsafe", "COMMENT"):
            for field, value in mutations:
                self.reset_stores()
                with self.subTest(mode=mode, field=field, value=value):
                    _ = self.launch(mode)
                    receipt = parse(self.store.read("receipt.json")[0])
                    assert isinstance(receipt, dict)
                    target = receipt
                    if field.startswith("diagnostic."):
                        diagnostic = receipt["diagnostic"]
                        assert isinstance(diagnostic, dict)
                        target = diagnostic
                        field = field.split(".")[1]
                    if value is None:
                        del target[field]
                    else:
                        target[field] = value
                    self.store.atomic("receipt.json", receipt)
                    raw = self.store.read("receipt.json")[0]
                    for _ in range(2):
                        with patch("tools.review_result.subprocess.Popen", side_effect=AssertionError("no redispatch")):
                            with self.assertRaises(Rejected) as caught:
                                _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                        self.assertEqual(caught.exception.code, Code.TYPE)
                        self.assertEqual(self.store.read("receipt.json")[0], raw)

    def test_receipt_read_rejection_is_typed_and_preserves_artifacts(self) -> None:
        _ = self.launch("file-unsafe")
        path = self.store.path / "receipt.json"
        raw = path.read_bytes()
        self.native.atomic("last-message.json", result("APPROVE"))
        native = self.native.read("last-message.json")[0]
        path.chmod(0o644)
        for _ in range(2):
            with self.assertRaises(Rejected) as caught:
                _ = recover(self.store, self.native, CONTEXT, TextPolicy())
            self.assertEqual(caught.exception.code, Code.MODE)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(self.native.read("last-message.json")[0], native)
        path.chmod(0o600)
        original_read = self.store.read

        def refuse(name: str) -> tuple[bytes, os.stat_result]:
            if name == "receipt.json":
                raise PermissionError("Bearer synthetic-credential-value")
            return original_read(name)

        with patch.object(self.store, "read", refuse):
            with self.assertRaises(Rejected) as caught:
                _ = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertEqual(caught.exception.code, Code.IO)
        self.assertEqual(str(caught.exception), Code.IO)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.native.read("last-message.json")[0], native)

    def test_receipt_completion_requires_successful_capture_invariants(self) -> None:
        mutations: list[tuple[str, str, Json]] = [
            ("file-unsafe", "complete", True), ("COMMENT", "schema_valid", False),
            ("COMMENT", "revisions_valid", False), ("COMMENT", "actual_verdict", None),
            ("COMMENT", "lifecycle", "rejected"), ("COMMENT", "execution_failures", [Code.IO]),
        ]
        for mode, field, value in mutations:
            self.reset_stores()
            with self.subTest(mode=mode, field=field):
                _ = self.launch(mode)
                receipt = parse(self.store.read("receipt.json")[0])
                assert isinstance(receipt, dict)
                diagnostic = receipt["diagnostic"]
                assert isinstance(diagnostic, dict)
                diagnostic[field] = value
                self.store.atomic("receipt.json", receipt)
                raw = self.store.read("receipt.json")[0]
                for _ in range(2):
                    with self.assertRaises(Rejected) as caught:
                        _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                    self.assertEqual(caught.exception.code, Code.TYPE)
                    self.assertEqual(self.store.read("receipt.json")[0], raw)

    def test_receipt_binding_requires_exact_json_types(self) -> None:
        _ = self.launch("file-unsafe")
        receipt = parse(self.store.read("receipt.json")[0])
        assert isinstance(receipt, dict)
        binding = receipt["binding"]
        assert isinstance(binding, dict)
        context = binding["context"]
        assert isinstance(context, dict)
        context["ordinal"] = True
        self.store.atomic("receipt.json", receipt)
        raw = self.store.read("receipt.json")[0]
        for _ in range(2):
            with self.assertRaises(Rejected) as caught:
                _ = recover(self.store, self.native, CONTEXT, TextPolicy())
            self.assertEqual(caught.exception.code, Code.CLAIM)
            self.assertEqual(self.store.read("receipt.json")[0], raw)

    def test_corrupt_lifecycle_objects_are_not_missing_partial_artifacts(self) -> None:
        for name in ("launch.claim", "turn.json", "spawn.json", "exit.json", "capture.json"):
            for raw, expected in ((b"{", Code.JSON), (b"[]", Code.TYPE)):
                self.reset_stores()
                with self.subTest(name=name, raw=raw):
                    _ = self.launch("missing-file")
                    if name == "capture.json":
                        self.store.atomic(name, {})
                    _ = (self.store.path / name).write_bytes(raw)
                    receipt = self.store.read("receipt.json")[0]
                    with self.assertRaises(Rejected) as caught:
                        _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(self.store.read("receipt.json")[0], receipt)

    def test_truly_missing_receipt_rebuilds_only_from_intact_durable_sources(self) -> None:
        for native_present in (False, True):
            self.reset_stores()
            with self.subTest(native_present=native_present):
                if native_present:
                    _ = self.prepare_recovery(encode(result("APPROVE")), exited=True)
                else:
                    _ = self.launch("missing-file")
                    (self.store.path / "receipt.json").unlink()
                captured = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertTrue(captured.complete)
                self.assertEqual(captured.actual_verdict, "APPROVE" if native_present else "COMMENT")
                self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)

    def test_claim_without_spawn_or_exit_is_not_retry_authority(self) -> None:
        self.store.atomic("launch.claim", {"context": CONTEXT.json(), "public_policy_sha256": TextPolicy().binding()})
        captured = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertIn(Code.NO_EXIT, captured.execution_failures)
        self.assertIn(Code.NO_SPAWN, captured.execution_failures)
        with self.assertRaises(Rejected) as error:
            _ = self.launch("APPROVE")
        self.assertEqual(error.exception.code, Code.LAUNCH)

    def test_explicit_synthetic_environment(self) -> None:
        self.assertTrue(self.launch("environment").complete)

    def test_unsafe_version_metadata_is_rejected_before_claim_or_spawn(self) -> None:
        for version, policy in (("Bearer SYNTHETIC_ONLY_VALUE", TextPolicy()),
                                ("fixture-only version", TextPolicy(forbidden=("fixture-only",)))):
            with self.subTest(version=version):
                with self.assertRaises(Rejected) as caught:
                    _ = run_prepared(self.store, self.native, CONTEXT, [sys.executable], b"", {},
                                     policy, executable_version=version)
                self.assertEqual(caught.exception.code, Code.UNSAFE)
                for name in ("launch.claim", "spawn.json", "exit.json", "receipt.json"):
                    self.assertFalse((self.store.path / name).exists())

    def test_forbidden_session_is_rejected_without_retaining_literal_or_hash(self) -> None:
        session = "12345678-1234-1234-1234-123456789abc"
        capture = EventCapture(CONTEXT, TextPolicy(forbidden=(session,)))
        capture.feed(encode({"type": "thread.started", "thread_id": session}))
        capture.persist(self.store)
        self.assertNotIn(session.encode(), self.store.read("turn.json")[0])
        self.assertIsNone(capture.session)
        self.assertEqual(capture.diagnostic.code, Code.UNSAFE)
        self.assertIsNone(capture.diagnostic.sha256)
        self.assertTrue(capture.failed)

    def test_forbidden_hostname_is_rejected_before_any_durable_launch_effect(self) -> None:
        for host, policy in (("fixture-host", TextPolicy(forbidden=("fixture-host",))),
                             ("sk-SYNTHETIC_ONLY_VALUE", TextPolicy())):
            self.reset_stores()
            with self.subTest(host_kind="explicit" if policy.forbidden else "pattern"):
                with patch("tools.review_result.socket.gethostname", return_value=host):
                    with self.assertRaises(Rejected) as caught:
                        _ = self.launch("COMMENT", policy=policy)
                self.assertEqual(caught.exception.stage, Stage.SAFETY)
                self.assertEqual(caught.exception.code, Code.UNSAFE)
                self.assertEqual(list(self.store.path.iterdir()), [])
                self.assertEqual(list(self.native.path.iterdir()), [])

    def test_invalid_hostname_is_typed_and_rejected_before_claim(self) -> None:
        with patch("tools.review_result.socket.gethostname", return_value="invalid host"):
            with self.assertRaises(Rejected) as caught:
                _ = self.launch("COMMENT")
        self.assertEqual(caught.exception.code, Code.TYPE)
        self.assertEqual(list(self.store.path.iterdir()), [])

    def test_forbidden_executable_context_and_public_source_cannot_leave_claim_or_hash(self) -> None:
        public_path = "src/fixture.py"
        source = PublicSource("d" * 40, "e" * 64, (public_path,), parent_verified=True)
        for policy in (TextPolicy(forbidden=(str(Path(sys.executable).resolve()),)),
                       TextPolicy(forbidden=(CONTEXT.job_id,)),
                       TextPolicy(forbidden=(CONTEXT.head,)),
                       TextPolicy(forbidden=(public_path,), sources=(source,))):
            with self.assertRaises(Rejected) as caught:
                _ = self.launch("COMMENT", policy=policy)
            self.assertEqual(caught.exception.code, Code.UNSAFE)
            self.assertEqual(list(self.store.path.iterdir()), [])
            self.assertEqual(list(self.native.path.iterdir()), [])

    def test_forbidden_session_rejects_actual_capture_despite_safe_native_result(self) -> None:
        captured = self.launch("APPROVE", policy=TextPolicy(forbidden=(
            "12345678-1234-1234-1234-123456789abc",)))
        self.assertEqual(captured.code, Code.UNSAFE)
        self.assertFalse(captured.complete)
        self.assertIsNone(captured.actual_verdict)
        for path in self.store.path.iterdir():
            self.assertNotIn(b"12345678-1234-1234-1234-123456789abc", self.store.read(path.name)[0])

    def test_newly_forbidden_public_source_scrubs_old_fingerprint_without_new_authority(self) -> None:
        source = PublicSource("d" * 40, "e" * 64, ("src/fixture.py",), parent_verified=True)
        policy = TextPolicy(sources=(source,))
        self.assertTrue(self.launch("APPROVE", policy=policy).complete)
        fingerprint = policy.binding()
        with self.assertRaises(Rejected) as caught:
            _ = recover(self.store, self.native, CONTEXT, TextPolicy(
                forbidden=("src/fixture.py",), sources=(source,)))
        self.assertEqual(caught.exception.code, Code.UNSAFE)
        for path in self.store.path.iterdir():
            self.assertNotIn(fingerprint.encode(), self.store.read(path.name)[0])
        claim = parse(self.store.read("launch.claim")[0])
        assert isinstance(claim, dict) and isinstance(claim["context"], dict)
        self.assertEqual(claim["context"]["ordinal"], CONTEXT.ordinal)
        receipt = parse(self.store.read("receipt.json")[0])
        assert isinstance(receipt, dict) and isinstance(receipt["diagnostic"], dict)
        self.assertIsNone(receipt["diagnostic"]["actual_verdict"])
        self.assertFalse(receipt["diagnostic"]["complete"])
        with self.assertRaises(Rejected) as duplicate:
            _ = self.launch("APPROVE", policy=policy)
        self.assertEqual(duplicate.exception.code, Code.LAUNCH)

    def test_valid_long_identity_metadata_does_not_relax_reviewer_text_policy(self) -> None:
        host = "fixture-host-" * 5
        with patch("tools.review_result.socket.gethostname", return_value=host):
            self.assertTrue(self.launch("COMMENT").complete)
        self.assertIn(host.encode(), self.store.read("spawn.json")[0])
        self.assertIn(b"12345678-1234-1234-1234-123456789abc", self.store.read("turn.json")[0])
        self.assertTrue(recover(self.store, self.native, CONTEXT, TextPolicy()).complete)
        payload = result()
        payload["limitations"] = [host]
        self.assertEqual(inspect(encode(payload), CONTEXT, TextPolicy())[0].code, Code.UNSAFE)

    def test_current_policy_scrubs_all_retained_identity_metadata(self) -> None:
        session = "12345678-1234-1234-1234-123456789abc"
        for forbidden in (session, "fixture-host", "synthetic Python fixture",
                          str(Path(sys.executable).resolve()), CONTEXT.job_id,
                          CONTEXT.requested_model, CONTEXT.head):
            self.reset_stores()
            with self.subTest(metadata_kind="context" if forbidden in (
                    CONTEXT.job_id, CONTEXT.requested_model, CONTEXT.head) else "external"):
                with patch("tools.review_result.socket.gethostname", return_value="fixture-host"):
                    self.assertTrue(self.launch("APPROVE").complete)
                policy = TextPolicy(forbidden=(forbidden,))
                captured = recover(self.store, self.native, CONTEXT, policy)
                self.assertEqual(captured.code, Code.UNSAFE)
                self.assertFalse(captured.complete)
                self.assertIsNone(captured.actual_verdict)
                self.assertEqual(captured.model_identity, "UNVERIFIED")
                for path in self.store.path.iterdir():
                    self.assertNotIn(forbidden.encode(), self.store.read(path.name)[0])
                claim = parse(self.store.read("launch.claim")[0])
                assert isinstance(claim, dict) and isinstance(claim["context"], dict)
                self.assertEqual(claim["context"]["ordinal"], CONTEXT.ordinal)
                try:
                    repeated = recover(self.store, self.native, CONTEXT, TextPolicy())
                except Rejected as caught:
                    self.assertEqual(caught.code, Code.CLAIM)
                else:
                    self.assertEqual(repeated.code, Code.UNSAFE)
                    self.assertFalse(repeated.complete)
                    self.assertIsNone(repeated.actual_verdict)

    def test_pending_cleanup_metadata_rescreen_cannot_restore_original_verdict(self) -> None:
        for forbidden in ("fixture-host", "12345678-1234-1234-1234-123456789abc", CONTEXT.job_id):
            self.reset_stores()
            original_unlink = os.unlink

            def crash(name: str, *, dir_fd: int | None = None) -> None:
                if name == "last-message.json" and dir_fd == self.native.fd:
                    raise RuntimeError("synthetic crash before unlink")
                original_unlink(name, dir_fd=dir_fd)

            with patch("tools.review_result.socket.gethostname", return_value="fixture-host"):
                with patch("tools.review_result.os.unlink", crash):
                    with self.assertRaises(RuntimeError):
                        _ = self.launch("APPROVE")
            captured = recover(self.store, self.native, CONTEXT, TextPolicy(forbidden=(forbidden,)))
            self.assertEqual(captured.code, Code.UNSAFE)
            self.assertFalse(captured.complete)
            self.assertIsNone(captured.actual_verdict)
            self.assertFalse((self.native.path / "last-message.json").exists())
            for path in self.store.path.iterdir():
                self.assertNotIn(forbidden.encode(), self.store.read(path.name)[0])
            receipt = parse(self.store.read("receipt.json")[0])
            assert isinstance(receipt, dict)
            for key in ("diagnostic", "source_diagnostic", "source_rejection"):
                diagnostic = receipt[key]
                assert isinstance(diagnostic, dict)
                self.assertIsNone(diagnostic["actual_verdict"])
                self.assertFalse(diagnostic["complete"])

    def test_input_bound_is_separate_from_result_bound_and_checked_before_claim(self) -> None:
        captured = run_prepared(self.store, self.native, CONTEXT,
                               [sys.executable, str(ROOT / "tests/review_result_fixture.py"), "COMMENT",
                                CONTEXT.head, CONTEXT.base], b"x" * (128 * 1024 + 1),
                               {"HOME": str(self.native.path), "PYTHONPATH": str(ROOT)}, TextPolicy(),
                               executable_version="synthetic Python fixture")
        self.assertTrue(captured.complete)
        self.reset_stores()
        with self.assertRaises(ValueError):
            _ = run_prepared(self.store, self.native, CONTEXT, [sys.executable],
                             b"x" * (INPUT_LIMIT + 1), {}, TextPolicy(), executable_version="synthetic")
        self.assertFalse((self.store.path / "launch.claim").exists())

    def test_partial_prompt_delivery_cannot_accept_valid_looking_result(self) -> None:
        captured = run_prepared(self.store, self.native, CONTEXT,
                               [sys.executable, str(ROOT / "tests/review_result_fixture.py"), "input-disconnect",
                                CONTEXT.head, CONTEXT.base], b"x" * INPUT_LIMIT,
                               {"HOME": str(self.native.path), "PYTHONPATH": str(ROOT)}, TextPolicy(),
                               executable_version="synthetic Python fixture")
        self.assertIn(Code.INPUT, captured.execution_failures)
        self.assertFalse(captured.complete)
        self.assertFalse(captured.parent_approval)

    def test_request_changes_with_actionable_findings_is_retained_not_approved(self) -> None:
        captured = self.launch("findings")
        self.assertTrue(captured.complete)
        self.assertEqual(captured.actual_verdict, "REQUEST_CHANGES")
        self.assertFalse(captured.parent_approval)
        self.assertIn(b"offline source", self.store.read("receipt.json")[0])

    def test_agent_manager_request_changes_routes_to_implementer_without_approval(self) -> None:
        context = Context(CONTEXT.head, CONTEXT.base, 15, "synthetic-model", "manager-session")
        finding: dict[str, Json] = {
            "severity": "P1",
            "file": "scarcity_router/server_store.py",
            "line_start": 1,
            "line_end": 2,
            "evidence": "Synthetic same-process transaction lock release.",
            "consequence": "A committed row can be lost.",
            "required_remediation": "Keep descriptor lifetime coordinated with open SQLite connections.",
        }
        response = agent_manager_result("REQUEST_CHANGES")
        response["findings"] = [finding]

        diagnostic, validated = inspect_agent_manager_result(
            encode(response), context, "c" * 40, TextPolicy(), session_completed=True
        )
        self.assertEqual(diagnostic.code, Code.OK)
        self.assertTrue(diagnostic.complete)
        self.assertEqual(diagnostic.actual_verdict, "REQUEST_CHANGES")
        self.assertFalse(diagnostic.parent_approval)
        self.assertEqual(agent_manager_handoff_action(diagnostic, validated), "REMEDIATE")
        if not isinstance(validated, dict):
            self.fail("valid Agent Manager findings must produce a result object")
        self.assertEqual(validated["findings"], [finding])

    def test_agent_manager_missing_completion_or_stale_merge_base_holds(self) -> None:
        context = Context(CONTEXT.head, CONTEXT.base, 15, "synthetic-model", "manager-session")
        response = encode(agent_manager_result("APPROVE"))
        incomplete, no_result = inspect_agent_manager_result(
            response, context, "c" * 40, TextPolicy(), session_completed=False
        )
        self.assertFalse(incomplete.complete)
        self.assertEqual(agent_manager_handoff_action(incomplete, no_result), "HOLD")

        stale, no_result = inspect_agent_manager_result(
            response, context, "d" * 40, TextPolicy(), session_completed=True
        )
        self.assertEqual(stale.code, Code.REVISION)
        self.assertEqual(agent_manager_handoff_action(stale, no_result), "HOLD")

    def test_cached_terminal_receipt_cannot_bypass_new_native_artifact(self) -> None:
        self.assertTrue(self.launch("COMMENT").complete)
        self.native.atomic("last-message.json", result("APPROVE"))
        captured = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertFalse(captured.complete)
        self.assertEqual(captured.code, Code.CONFLICT)

    def test_changed_raw_cleanup_persists_rejection_after_original_diagnostic(self) -> None:
        original_remove = PrivateStore.remove
        receipts_seen: list[Json] = []

        def race(store: PrivateStore, name: str, info: os.stat_result) -> None:
            receipts_seen.append(parse(self.store.read("receipt.json")[0]))
            os.utime(store.path / name, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
            original_remove(store, name, info)

        with patch.object(PrivateStore, "remove", race):
            captured = self.launch("COMMENT")
        self.assertTrue(receipts_seen)
        self.assertFalse(captured.complete)
        self.assertEqual(captured.code, Code.CHANGED)
        self.assertTrue((self.native.path / "last-message.json").exists())
        receipt = parse(self.store.read("receipt.json")[0])
        self.assertIsInstance(receipt, dict)
        self.assertIn(b"FILE_CHANGED", self.store.read("receipt.json")[0])

    def test_receipt_write_failure_never_discards_raw_result(self) -> None:
        original_atomic = PrivateStore.atomic

        def fail_receipt(store: PrivateStore, name: str, value: Json, *, exclusive: bool = False) -> None:
            if name == "receipt.json":
                raise OSError("synthetic storage failure")
            original_atomic(store, name, value, exclusive=exclusive)

        with patch.object(PrivateStore, "atomic", fail_receipt):
            with self.assertRaises(OSError):
                _ = self.launch("COMMENT")
        self.assertTrue((self.native.path / "last-message.json").exists())
        self.assertTrue(recover(self.store, self.native, CONTEXT, TextPolicy()).complete)

    def test_spawn_failure_has_no_invented_exit(self) -> None:
        captured = run_prepared(self.store, self.native, CONTEXT, ["/nonexistent/synthetic-cli"], b"", {},
                                TextPolicy(), executable_version="synthetic", timeout=1)
        self.assertIn(Code.NO_SPAWN, captured.execution_failures)
        self.assertIn(Code.NO_EXIT, captured.execution_failures)
        self.assertIn(Code.IO, captured.execution_failures)

    def test_actual_parent_interrupt_captures_exit_and_restores_signal_handler(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)

        def interrupt(metadata: dict[str, Json]) -> None:
            if metadata.get("lifecycle") == "started":
                os.kill(os.getpid(), signal.SIGTERM)

        captured = run_prepared(self.store, self.native, CONTEXT,
                                [sys.executable, str(ROOT / "tests/review_result_fixture.py"), "timeout",
                                 CONTEXT.head, CONTEXT.base], b"", {"PYTHONPATH": str(ROOT)}, TextPolicy(),
                                executable_version="synthetic Python fixture", progress=interrupt)
        self.assertIn(Code.INTERRUPTED, captured.execution_failures)
        self.assertTrue(self.store.read("exit.json")[0])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertFalse(captured.complete)

    def test_symlink_hardlink_mode_and_parent_are_refused(self) -> None:
        self.store.atomic("target.json", result())
        (self.native.path / "last-message.json").symlink_to(self.store.path / "target.json")
        with self.assertRaises(Rejected):
            _ = self.native.read("last-message.json")
        (self.native.path / "last-message.json").unlink()
        os.link(self.store.path / "target.json", self.native.path / "last-message.json")
        with self.assertRaises(Rejected):
            _ = self.native.read("last-message.json")
        with self.assertRaises(Rejected):
            _ = PrivateStore(self.store.path, self.native.path)
        self.native.path.chmod(0o755)
        with self.assertRaises(Rejected):
            _ = PrivateStore(self.native.path, self.native.path.parent)

    def test_foreign_owner_and_fifo_are_refused_without_content_reads(self) -> None:
        self.native.atomic("foreign.json", result())
        with patch("tools.review_result.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(Rejected) as error:
                _ = self.native.read("foreign.json")
        self.assertEqual(error.exception.code, Code.OWNER)
        os.mkfifo(self.native.path / "fifo", mode=0o600)
        with self.assertRaises(Rejected) as error:
            _ = self.native.read("fifo")
        self.assertEqual(error.exception.code, Code.FILE)

    def test_schema_and_safety_adversarial_inputs(self) -> None:
        cases: list[tuple[Json, Code]] = [(None, Code.FIELDS), ([], Code.FIELDS)]
        mutations: list[tuple[str, Json, Code]] = [("verdict", "PASS", Code.VERDICT), ("findings", {}, Code.TYPE),
                                     ("limitations", [True], Code.TYPE),
                                     ("checks_run", [{"command": "", "result": "PASS"}], Code.CHECK),
                                     ("findings", [{"line_start": True}], Code.FINDING),
                                     ("limitations", ["z" * 40], Code.UNSAFE),
                                     ("limitations", ["text\u202econcealed"], Code.UNSAFE)]
        for key, value, expected in mutations:
            obj = result()
            obj[key] = value
            cases.append((obj, expected))
        for obj, expected in cases:
            with self.subTest(code=expected):
                diagnostic, _retained = inspect(encode(obj), CONTEXT, TextPolicy())
                self.assertEqual(diagnostic.code, expected)
                self.assertFalse(diagnostic.complete)
        self.assertEqual(inspect(b"\xff", CONTEXT, TextPolicy())[0].code, Code.UTF8)
        self.assertEqual(inspect(b'{"findings": 1e400}', CONTEXT, TextPolicy())[0].code, Code.NUMBER)
        self.assertEqual(inspect(b" " * (128 * 1024 + 1), CONTEXT, TextPolicy())[0].code, Code.OVERSIZE)

    def test_public_exception_requires_exact_parent_attested_manifest(self) -> None:
        path = "src/" + "long-public-file-name-" * 3 + ".py"
        pin = "d" * 40
        obj = result()
        obj["limitations"] = [f"Inspect {path} at {pin}."]
        self.assertEqual(inspect(encode(obj), CONTEXT, TextPolicy())[0].code, Code.UNSAFE)
        source = PublicSource(pin, "e" * 64, (path,), parent_verified=True)
        policy = TextPolicy(sources=(source,))
        self.assertEqual(inspect(encode(obj), CONTEXT, policy)[0].code, Code.OK)
        self.assertEqual(inspect(encode(obj), CONTEXT, TextPolicy(forbidden=(path,), sources=(source,)))[0].code, Code.UNSAFE)
        obj["limitations"] = ["prefix" + path, pin + "suffix"]
        self.assertEqual(inspect(encode(obj), CONTEXT, policy)[0].code, Code.UNSAFE)
        with self.assertRaises(ValueError):
            _ = PublicSource(pin, "e" * 64, (path,))

    def test_final_unsafe_message_never_reuses_prior_safe_message(self) -> None:
        capture = EventCapture(CONTEXT, TextPolicy())
        capture.feed(encode({"type": "turn.started"}))
        capture.feed(encode({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result())}}))
        capture.feed(encode({"type": "item.completed", "item": {"type": "agent_message", "text": "Bearer synthetic-value"}}))
        capture.feed(encode({"type": "turn.completed"}))
        self.assertIsNone(capture.last)
        self.assertEqual(capture.diagnostic.code, Code.JSON)
        self.assertTrue(capture.completed)

    def test_claim_context_and_policy_bind_recovery(self) -> None:
        _ = self.launch("COMMENT")
        for context, policy in ((Context(CONTEXT.head, CONTEXT.base, 2, CONTEXT.requested_model, CONTEXT.job_id), TextPolicy()),
                                (CONTEXT, TextPolicy(sources=(PublicSource("d" * 40, "e" * 64, (), parent_verified=True),)))):
            with self.assertRaises(Rejected) as error:
                _ = recover(self.store, self.native, context, policy)
            self.assertEqual(error.exception.code, Code.CLAIM)

    def snapshot_stores(self) -> dict[str, tuple[bytes | None, tuple[int, ...]]]:
        # Include directory entries and file identity, but not read-induced atime.
        snapshot: dict[str, tuple[bytes | None, tuple[int, ...]]] = {}
        for store in (self.store, self.native):
            snapshot[store.path.name] = (None, PrivateStore.identity(store.path.stat()))
            for path in store.path.iterdir():
                snapshot[store.path.name + "/" + path.name] = (
                    path.read_bytes(), PrivateStore.identity(path.stat()))
        return snapshot

    def assert_invalid_context_preserves_stores(self) -> None:
        before = self.snapshot_stores()
        for field in ("head", "base"):
            for policy in (TextPolicy(), TextPolicy(forbidden=(CONTEXT.head, CONTEXT.job_id, "fixture-only"))):
                with self.subTest(field=field, tightened=bool(policy.forbidden)):
                    wrong = replace(CONTEXT, **{field: "c" * 40})
                    with patch("tools.review_result._finish_cleanup", side_effect=AssertionError("no cleanup")):
                        with patch.object(PrivateStore, "atomic", side_effect=AssertionError("no writes")):
                            with self.assertRaises(Rejected) as caught:
                                _ = recover(self.store, self.native, wrong, policy)
                    self.assertEqual(caught.exception.stage, Stage.RECOVERY)
                    self.assertEqual(caught.exception.code, Code.CLAIM)
                    self.assertEqual(self.snapshot_stores(), before)

    def test_wrong_revisions_preserve_completed_and_rejected_terminal_evidence(self) -> None:
        for mode in ("APPROVE", "REQUEST_CHANGES", "COMMENT", "policy-update",
                     "file-malformed", "file-unsafe", "wrong-type"):
            self.reset_stores()
            with self.subTest(mode=mode):
                original = self.launch(mode)
                self.assert_invalid_context_preserves_stores()
                before = self.snapshot_stores()
                self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()).json(), original.json())
                self.assertEqual(self.snapshot_stores(), before)
                if mode == "policy-update":
                    tightened = recover(self.store, self.native, CONTEXT, TextPolicy(forbidden=("fixture-only",)))
                    self.assertEqual(tightened.code, Code.UNSAFE)
                    self.assertFalse(tightened.complete)
                    for name in ("receipt.json", "turn.json"):
                        self.assertNotIn(b"fixture-only", self.store.read(name)[0])

    def test_wrong_revisions_preserve_pending_and_blocked_native_cleanup(self) -> None:
        for raw, blocked in ((encode(result("APPROVE")), False), (b"not JSON", False),
                             (encode(result("APPROVE")), True)):
            self.reset_stores()
            path = self.prepare_recovery(raw, exited=True)
            with self.subTest(blocked=blocked, malformed=raw == b"not JSON"):
                with patch.object(self.native, "remove", side_effect=RuntimeError("synthetic crash")):
                    with self.assertRaises(RuntimeError):
                        _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                if blocked:
                    path.chmod(0o644)
                    original = recover(self.store, self.native, CONTEXT, TextPolicy())
                else:
                    receipt = parse(self.store.read("receipt.json")[0])
                    assert isinstance(receipt, dict)
                    original = receipt["source_diagnostic"]
                self.assert_invalid_context_preserves_stores()
                recovered = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertEqual(recovered.json(), original.json() if isinstance(original, Diagnostic) else original)
                self.assertEqual(path.exists(), blocked)

    def test_invalid_launch_claim_and_stored_binding_never_trigger_rescreening(self) -> None:
        for corruption in ("missing", "empty", "extra", "bool", "float", "binding", "matching_bool_binding"):
            self.reset_stores()
            with self.subTest(corruption=corruption):
                _ = self.launch("policy-update")
                claim = parse(self.store.read("launch.claim")[0])
                assert isinstance(claim, dict) and isinstance(claim["context"], dict)
                if corruption == "missing":
                    (self.store.path / "launch.claim").unlink()
                elif corruption == "binding":
                    receipt = parse(self.store.read("receipt.json")[0])
                    assert isinstance(receipt, dict)
                    receipt["binding"] = {"context": CONTEXT.json(), "public_policy_sha256": "e" * 64}
                    self.store.atomic("receipt.json", receipt)
                else:
                    if corruption == "empty":
                        claim = {}
                    elif corruption == "extra":
                        claim["untrusted"] = True
                    else:
                        claim["context"]["ordinal"] = 1.0 if corruption == "float" else True
                    self.store.atomic("launch.claim", claim)
                    if corruption == "matching_bool_binding":
                        receipt = parse(self.store.read("receipt.json")[0])
                        assert isinstance(receipt, dict)
                        receipt["binding"] = claim
                        self.store.atomic("receipt.json", receipt)
                before = self.snapshot_stores()
                with self.assertRaises(Rejected) as caught:
                    _ = recover(self.store, self.native, CONTEXT, TextPolicy(forbidden=("fixture-only",)))
                self.assertEqual(caught.exception.code, Code.CLAIM)
                self.assertEqual(self.snapshot_stores(), before)

    def test_queued_claim_is_only_a_discard_transform_of_verified_original(self) -> None:
        for corruption in ("job", "head", "base", "bool", "float", "null_ordinal", "missing", "extra",
                           "fingerprint", "signature", "unknown_queue", "nonobject", "receipt_binding"):
            self.reset_stores()
            with self.subTest(corruption=corruption):
                _ = self.launch("COMMENT")
                receipt = parse(self.store.read("receipt.json")[0])
                claim = parse(self.store.read("launch.claim")[0])
                assert isinstance(receipt, dict) and isinstance(claim, dict) and isinstance(claim["context"], dict)
                diagnostic = receipt["diagnostic"]
                assert isinstance(diagnostic, dict)
                diagnostic.update(stage=Stage.SAFETY, code=Code.UNSAFE, complete=False,
                                  actual_verdict=None, revisions_valid=False, lifecycle="rejected")
                receipt["source_rejection"] = dict(diagnostic)
                receipt["rescreen_pending"] = True
                if corruption in ("job", "head", "base", "bool", "float", "null_ordinal"):
                    field = {"job": "job_id", "head": "head", "base": "base"}.get(corruption, "ordinal")
                    claim["context"][field] = {"job": "substituted-job", "head": "c" * 40, "base": "c" * 40,
                                               "bool": True, "float": 1.0, "null_ordinal": None}[corruption]
                elif corruption == "missing":
                    del claim["context"]["job_id"]
                elif corruption in ("extra", "signature"):
                    claim[corruption] = "synthetic-assertion"
                elif corruption == "fingerprint":
                    claim["public_policy_sha256"] = "e" * 64
                queue: dict[str, Json] = {"launch.claim": claim}
                if corruption == "unknown_queue":
                    queue["receipt.json"] = {}
                elif corruption == "nonobject":
                    queue["launch.claim"] = []
                elif corruption == "receipt_binding":
                    claim["context"]["job_id"] = "substituted-job"
                    receipt["binding"] = claim
                receipt["rescreen_metadata"] = queue
                self.store.atomic("receipt.json", receipt)
                before = self.snapshot_stores()
                with patch.object(PrivateStore, "atomic", side_effect=AssertionError("no writes")):
                    with patch.object(self.native, "remove", side_effect=AssertionError("no disposal")):
                        with self.assertRaises(Rejected) as caught:
                            _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertIn(caught.exception.code, (Code.CLAIM, Code.TYPE))
                self.assertEqual(self.snapshot_stores(), before)

    def test_binding_scrub_write_prefixes_finish_disposal_before_claim_tombstone(self) -> None:
        source = PublicSource("d" * 40, "e" * 64, ("src/fixture.py",), parent_verified=True)
        for public in (False, True):
            original = TextPolicy(sources=(source,) if public else ())
            forbidden = "src/fixture.py" if public else CONTEXT.job_id
            tightened = TextPolicy(forbidden=(forbidden,), sources=original.sources)
            for state in ("completed", "pending", "blocked"):
                for boundary in range(1, 9):
                    for after in (False, True):
                        self.reset_stores()
                        with self.subTest(public=public, state=state, boundary=boundary, after=after):
                            path = self.prepare_recovery(encode(result("APPROVE")), exited=True)
                            self.store.atomic("launch.claim", {"context": CONTEXT.json(),
                                                               "public_policy_sha256": original.binding()})
                            if state == "pending":
                                with patch.object(self.native, "remove", side_effect=RuntimeError("synthetic unlink crash")):
                                    with self.assertRaises(RuntimeError):
                                        _ = recover(self.store, self.native, CONTEXT, original)
                            elif state == "blocked":
                                with patch.object(self.native, "remove", side_effect=PermissionError("synthetic refusal")):
                                    _ = recover(self.store, self.native, CONTEXT, original)
                            else:
                                _ = recover(self.store, self.native, CONTEXT, original)
                            atomic = PrivateStore.atomic
                            writes = 0

                            def crash(store: PrivateStore, name: str, value: Json, *, exclusive: bool = False) -> None:
                                nonlocal writes
                                writes += 1
                                if writes == boundary and not after:
                                    raise RuntimeError("synthetic before write")
                                atomic(store, name, value, exclusive=exclusive)
                                if writes == boundary and after:
                                    raise RuntimeError("synthetic after write")

                            with patch.object(PrivateStore, "atomic", crash):
                                try:
                                    _ = recover(self.store, self.native, CONTEXT, tightened)
                                except (RuntimeError, Rejected):
                                    pass
                            self.store.close()
                            self.native.close()
                            self.store = PrivateStore(self.store.path, self.store.path.parent)
                            self.native = PrivateStore(self.native.path, self.native.path.parent)
                            before = self.snapshot_stores()
                            for field in ("head", "base"):
                                for policy in (original, tightened):
                                    with patch.object(PrivateStore, "atomic", side_effect=AssertionError("no writes")):
                                        with self.assertRaises(Rejected) as caught:
                                            _ = recover(self.store, self.native, replace(CONTEXT, **{field: "c" * 40}), policy)
                                    self.assertEqual(caught.exception.code, Code.CLAIM)
                                    self.assertEqual(self.snapshot_stores(), before)
                            receipt = parse(self.store.read("receipt.json")[0])
                            assert isinstance(receipt, dict)
                            rejection = receipt.get("source_rejection")
                            retry = original if isinstance(rejection, dict) and rejection.get("stage") == Stage.SAFETY else tightened
                            try:
                                recovered = recover(self.store, self.native, CONTEXT, retry)
                            except Rejected as caught:
                                self.assertIn(caught.code, (Code.UNSAFE, Code.CLAIM))
                            else:
                                self.assertEqual(recovered.code, Code.UNSAFE)
                                self.assertFalse(recovered.complete)
                            self.assertFalse(path.exists())
                            for file in self.store.path.iterdir():
                                self.assertNotIn((original.binding() if public else forbidden).encode(), self.store.read(file.name)[0])
                            receipt = parse(self.store.read("receipt.json")[0])
                            assert isinstance(receipt, dict)
                            self.assertNotIn("rescreen_pending", receipt)
                            self.assertNotIn("rescreen_metadata", receipt)
                            with self.assertRaises(Rejected) as duplicate:
                                _ = self.launch("APPROVE", policy=original)
                            self.assertEqual(duplicate.exception.code, Code.LAUNCH)

    def test_binding_scrub_keeps_original_authority_during_blocked_owned_cleanup(self) -> None:
        path = self.prepare_recovery(encode(result("APPROVE")), exited=True)
        with patch.object(self.native, "remove", side_effect=RuntimeError("synthetic interruption")):
            with self.assertRaises(RuntimeError):
                _ = recover(self.store, self.native, CONTEXT, TextPolicy())
        original_claim = self.store.read("launch.claim")[0]
        with patch.object(self.native, "remove", side_effect=PermissionError("synthetic refusal")):
            rejected = recover(self.store, self.native, CONTEXT, TextPolicy(forbidden=(CONTEXT.job_id,)))
        self.assertEqual(rejected.code, Code.UNSAFE)
        self.assertFalse(rejected.complete)
        self.assertTrue(path.exists())
        self.assertEqual(self.store.read("launch.claim")[0], original_claim)
        self.assert_invalid_context_preserves_stores()
        self.store.close()
        self.native.close()
        self.store = PrivateStore(self.store.path, self.store.path.parent)
        self.native = PrivateStore(self.native.path, self.native.path.parent)
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()).code, Code.UNSAFE)
        self.assertFalse(path.exists())
        for file in self.store.path.iterdir():
            self.assertNotIn(CONTEXT.job_id.encode(), self.store.read(file.name)[0])

    def test_recovery_rescreens_new_forbidden_literals_and_scrubs_retained_payloads(self) -> None:
        self.assertTrue(self.launch("policy-update").complete)
        policy = TextPolicy(forbidden=("fixture-only",))
        captured = recover(self.store, self.native, CONTEXT, policy)
        self.assertFalse(captured.complete)
        self.assertEqual(captured.code, Code.UNSAFE)
        self.assertIsNone(captured.actual_verdict)
        for name in ("receipt.json", "turn.json"):
            self.assertNotIn(b"fixture-only", self.store.read(name)[0])
        self.assertEqual(recover(self.store, self.native, CONTEXT, policy).code, Code.UNSAFE)

    def test_policy_collisions_preserve_internal_types_but_reject_reviewer_literals(self) -> None:
        # Exact OK/COMMENT reproducer plus codes, stages, lifecycle, type names,
        # provenance and verdicts. No blanket status-looking prose exemption.
        literals = (*tuple(Code), *tuple(Stage), "validated", "rejected", "dict", "list", "str",
                    "native_last_message", "UNVERIFIED", "pending", "done", "blocked",
                    "APPROVE", "REQUEST_CHANGES", "COMMENT")
        for verdict in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
            for literal in literals:
                self.reset_stores()
                with self.subTest(verdict=verdict, literal=literal):
                    payload = result(verdict)
                    payload["limitations"] = [literal]
                    _ = self.prepare_recovery(encode(payload), exited=True)
                    capture = EventCapture(CONTEXT, TextPolicy())
                    capture.feed(encode({"type": "turn.started"}))
                    capture.feed(encode({"type": "item.completed", "item": {
                        "type": "agent_message", "text": json.dumps(payload)}}))
                    capture.feed(encode({"type": "turn.completed"}))
                    capture.persist(self.store)
                    original = recover(self.store, self.native, CONTEXT, TextPolicy())
                    self.assertTrue(original.complete)
                    self.assert_invalid_context_preserves_stores()
                    for policy in (TextPolicy(forbidden=(literal,)), TextPolicy()):
                        captured = recover(self.store, self.native, CONTEXT, policy)
                        self.assertEqual(captured.code, Code.UNSAFE)
                        self.assertFalse(captured.complete)
                        self.assertIsNone(captured.actual_verdict)
                        self.assertEqual(captured.field_types, original.field_types)
                        self.assertEqual(captured.source, original.source)
                        for name, key in (("receipt.json", "result"), ("turn.json", "last")):
                            retained = parse(self.store.read(name)[0])
                            assert isinstance(retained, dict)
                            self.assertIsNone(retained.get(key))
                    with self.assertRaises(Rejected) as caught:
                        _ = self.launch("APPROVE")
                    self.assertEqual(caught.exception.code, Code.LAUNCH)

    def test_internal_only_collisions_never_change_completed_or_rejected_capture(self) -> None:
        for raw in (encode(result("APPROVE")), b"not JSON", encode({"verdict": 1}),
                    encode({**result("COMMENT"), "limitations": 1}),
                    encode({"limitations": ["Bearer SYNTHETIC_ONLY_VALUE"]})):
            for literal in ("OK", "validated", "rejected", "list", "str",
                            "MISSING_FILE", "FIELD_TYPE", "MALFORMED_JSON", "content_safety",
                            "native_last_message", "UNVERIFIED", "done"):
                self.reset_stores()
                with self.subTest(raw_kind="valid" if raw == encode(result("APPROVE")) else "rejected",
                                  literal=literal):
                    _ = self.prepare_recovery(raw, exited=True)
                    original = recover(self.store, self.native, CONTEXT, TextPolicy())
                    self.assert_invalid_context_preserves_stores()
                    self.assertEqual(recover(self.store, self.native, CONTEXT,
                                             TextPolicy(forbidden=(literal,))).json(), original.json())

    def test_metadata_scrub_journal_survives_every_write_prefix_and_policy_relaxation(self) -> None:
        session = "12345678-1234-1234-1234-123456789abc"
        policy = TextPolicy(forbidden=("fixture-host", "fixture-version", session, "fixture-only", "OK",
                                       "CAPTURE_INTERRUPTED", "process", "started", "exited"))
        for pending in (False, True):
            for boundary in range(1, 7):
                for after in (False, True):
                    self.reset_stores()
                    with self.subTest(pending=pending, boundary=boundary, after=after):
                        payload = result("COMMENT")
                        payload["limitations"] = ["fixture-only", "OK"]
                        _ = self.prepare_recovery(encode(payload), exited=True)
                        self.store.atomic("spawn.json", {"pid": 123, "host": "fixture-host", "version": "fixture-version",
                            "lifecycle": "started", "model_identity": "UNVERIFIED", "version_source": "parent_observed"})
                        self.store.atomic("exit.json", {"exit_code": 0, "capture_code": Code.INTERRUPTED, "lifecycle": "exited"})
                        self.store.atomic("capture.json", {"stage": Stage.PROCESS, "code": Code.INTERRUPTED})
                        capture = EventCapture(CONTEXT, TextPolicy())
                        capture.feed(encode({"type": "thread.started", "thread_id": session}))
                        capture.feed(encode({"type": "turn.started"}))
                        capture.feed(encode({"type": "item.completed", "item": {
                            "type": "agent_message", "text": json.dumps(payload)}}))
                        capture.feed(encode({"type": "turn.completed"}))
                        capture.persist(self.store)
                        if pending:
                            with patch.object(self.native, "remove", side_effect=RuntimeError("synthetic crash")):
                                with self.assertRaises(RuntimeError):
                                    _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                        else:
                            _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                        self.assert_invalid_context_preserves_stores()
                        original_atomic = PrivateStore.atomic
                        writes = 0

                        def crash(store: PrivateStore, name: str, value: Json, *, exclusive: bool = False) -> None:
                            nonlocal writes
                            writes += 1
                            if writes == boundary and not after:
                                raise RuntimeError("synthetic before replacement")
                            original_atomic(store, name, value, exclusive=exclusive)
                            if writes == boundary and after:
                                raise RuntimeError("synthetic after replacement")

                        with patch.object(PrivateStore, "atomic", crash):
                            try:
                                _ = recover(self.store, self.native, CONTEXT, policy)
                            except RuntimeError:
                                pass
                        self.store.close()
                        self.native.close()
                        self.store = PrivateStore(self.store.path, self.store.path.parent)
                        self.native = PrivateStore(self.native.path, self.native.path.parent)
                        self.assert_invalid_context_preserves_stores()
                        receipt = parse(self.store.read("receipt.json")[0])
                        assert isinstance(receipt, dict)
                        retry = TextPolicy() if receipt.get("rescreen_pending") is True else policy
                        captured = recover(self.store, self.native, CONTEXT, retry)
                        self.assertEqual(captured.code, Code.UNSAFE)
                        self.assertFalse(captured.complete)
                        self.assertIsNone(captured.actual_verdict)
                        for path in self.store.path.iterdir():
                            data = self.store.read(path.name)[0]
                            for literal in ("fixture-host", "fixture-version", session, "fixture-only"):
                                self.assertNotIn(literal.encode(), data)
                        capture_record = parse(self.store.read("capture.json")[0])
                        self.assertEqual(capture_record, {"stage": Stage.PROCESS, "code": Code.INTERRUPTED})
                        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)

    def test_collision_rescreen_write_prefixes_are_typed_sticky_and_recoverable(self) -> None:
        # Exercise every actual rescreen/cleanup atomic boundary, before and
        # after replacement, with interrupted, rejected and pending/blocked jobs.
        for state in ("completed", "malformed", "type", "unsafe", "incomplete", "interrupted", "pending", "blocked"):
            for boundary in range(1, 5):
                for after in (False, True):
                    self.reset_stores()
                    with self.subTest(state=state, boundary=boundary, after=after):
                        payload = result("COMMENT")
                        payload["limitations"] = ["OK"]
                        raw = (b"not JSON" if state == "malformed" else encode({**result("COMMENT"), "limitations": 1}) if state == "type"
                               else encode({"limitations": ["Bearer SYNTHETIC_ONLY_VALUE"]}) if state == "unsafe"
                               else encode(payload))
                        path = self.prepare_recovery(raw, exited=state != "incomplete")
                        capture = EventCapture(CONTEXT, TextPolicy())
                        capture.feed(encode({"type": "turn.started"}))
                        capture.feed(encode({"type": "item.completed", "item": {
                            "type": "agent_message", "text": json.dumps(payload)}}))
                        if state != "incomplete":
                            capture.feed(encode({"type": "turn.completed"}))
                        capture.persist(self.store)
                        if state == "interrupted":
                            self.store.atomic("exit.json", {"exit_code": 0, "capture_code": Code.INTERRUPTED})
                        if state in ("pending", "blocked"):
                            with patch.object(self.native, "remove", side_effect=RuntimeError("synthetic crash")):
                                with self.assertRaises(RuntimeError):
                                    _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                            if state == "blocked":
                                with patch.object(self.native, "remove", side_effect=PermissionError("synthetic refusal")):
                                    _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                        else:
                            _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                        self.assert_invalid_context_preserves_stores()
                        original_atomic = PrivateStore.atomic
                        writes = 0

                        def crash(store: PrivateStore, name: str, value: Json, *, exclusive: bool = False) -> None:
                            nonlocal writes
                            writes += 1
                            if writes == boundary and not after:
                                raise RuntimeError("synthetic before atomic replacement")
                            original_atomic(store, name, value, exclusive=exclusive)
                            if writes == boundary and after:
                                raise RuntimeError("synthetic after atomic replacement")

                        policy = TextPolicy(forbidden=("OK",))
                        with patch.object(PrivateStore, "atomic", crash):
                            try:
                                _ = recover(self.store, self.native, CONTEXT, policy)
                            except RuntimeError:
                                pass
                        self.store.close()
                        self.native.close()
                        self.store = PrivateStore(self.store.path, self.store.path.parent)
                        self.native = PrivateStore(self.native.path, self.native.path.parent)
                        self.assert_invalid_context_preserves_stores()
                        # A durable first rejection must survive relaxation even
                        # if the turn write did not happen. No new launch occurs.
                        receipt = parse(self.store.read("receipt.json")[0])
                        assert isinstance(receipt, dict)
                        diagnostic = receipt["diagnostic"]
                        assert isinstance(diagnostic, dict)
                        retry_policy = TextPolicy() if receipt.get("rescreen_pending") is True else policy
                        with patch("tools.review_result.subprocess.Popen", side_effect=AssertionError("no relaunch")):
                            captured = recover(self.store, self.native, CONTEXT, retry_policy)
                            self.assertEqual(captured.code, Code.UNSAFE)
                            self.assertFalse(captured.complete)
                            self.assertIsNone(captured.actual_verdict)
                            self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()).code, Code.UNSAFE)
                        for name, key in (("receipt.json", "result"), ("turn.json", "last")):
                            retained = parse(self.store.read(name)[0])
                            assert isinstance(retained, dict)
                            self.assertNotIn(b'"OK"', encode(retained.get(key)))
                        if state == "incomplete":
                            self.assertTrue(path.exists())
                            self.store.atomic("exit.json", {"exit_code": 0})
                            self.assertEqual(recover(self.store, self.native, CONTEXT, policy).code, Code.UNSAFE)
                            self.assertFalse(path.exists())

    def test_parent_traversal_and_same_directory_handles_are_rejected_before_claim(self) -> None:
        parent = self.store.path.parent
        with self.assertRaises(Rejected):
            _ = PrivateStore(self.native.path / ".." / self.store.path.name, self.native.path / "..")
        self.native.close()
        self.native = PrivateStore(self.store.path, parent)
        # Lexical metadata is not evidence of a different opened directory.
        self.native.path = parent / "declared-other-output"
        with self.assertRaises(ValueError):
            _ = run_prepared(self.store, self.native, CONTEXT, [sys.executable], b"", {},
                             TextPolicy(), executable_version="synthetic")
        self.assertFalse((self.store.path / "launch.claim").exists())

    def test_native_rejection_before_exit_cannot_become_jsonl_approval(self) -> None:
        for mode, expected in (("malformed", Code.JSON), ("unsafe", Code.UNSAFE), ("conflict", Code.CONFLICT)):
            self.reset_stores()
            with self.subTest(mode=mode):
                self.store.atomic("launch.claim", {"context": CONTEXT.json(), "public_policy_sha256": TextPolicy().binding()})
                self.store.atomic("spawn.json", {"pid": 123})
                capture = EventCapture(CONTEXT, TextPolicy())
                capture.feed(encode({"type": "turn.started"}))
                capture.feed(encode({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result("APPROVE"))}}))
                capture.feed(encode({"type": "turn.completed"}))
                capture.persist(self.store)
                raw = b"not JSON" if mode == "malformed" else encode(result("COMMENT"))
                if mode == "unsafe":
                    unsafe = result("COMMENT")
                    unsafe["limitations"] = ["Bearer synthetic-credential-value"]
                    raw = encode(unsafe)
                with (self.native.path / "last-message.json").open("xb") as output:
                    _ = output.write(raw)
                (self.native.path / "last-message.json").chmod(0o600)
                first = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertFalse(first.complete)
                self.assertTrue((self.native.path / "last-message.json").exists())
                self.store.atomic("exit.json", {"exit_code": 0})
                second = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertEqual(second.code, expected)
                self.assertFalse(second.complete)
                self.assertFalse(recover(self.store, self.native, CONTEXT, TextPolicy()).complete)

    def prepare_recovery(self, raw: bytes, *, exited: bool = False) -> Path:
        self.store.atomic("launch.claim", {"context": CONTEXT.json(), "public_policy_sha256": TextPolicy().binding()})
        self.store.atomic("spawn.json", {"pid": 123})
        capture = EventCapture(CONTEXT, TextPolicy())
        capture.feed(encode({"type": "turn.started"}))
        capture.feed(encode({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result("APPROVE"))}}))
        capture.feed(encode({"type": "turn.completed"}))
        capture.persist(self.store)
        path = self.native.path / "last-message.json"
        with path.open("xb") as output:
            _ = output.write(raw)
        path.chmod(0o600)
        if exited:
            self.store.atomic("exit.json", {"exit_code": 0})
        return path

    def test_terminal_native_rejection_cannot_adopt_matching_replacement(self) -> None:
        unsafe = result("APPROVE")
        unsafe["limitations"] = ["Bearer synthetic-credential-value"]
        path = self.prepare_recovery(encode(unsafe), exited=True)
        first = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertEqual(first.code, Code.UNSAFE)
        self.assertFalse(path.exists())
        original = first.json()
        self.native.atomic("last-message.json", result("APPROVE"))
        self.store.close()
        self.store = PrivateStore(self.store.path, self.store.path.parent)
        second = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertEqual(second.json(), original)
        self.assertFalse(second.complete)
        receipt = parse(self.store.read("receipt.json")[0])
        assert isinstance(receipt, dict)
        self.assertEqual(receipt["diagnostic"], original)
        self.assertEqual(receipt["cleanup_failure"], {"stage": Stage.FILE, "code": Code.CHANGED})
        self.assertTrue((self.native.path / "last-message.json").exists())
        self.assertNotIn(b"synthetic-credential-value", self.store.read("receipt.json")[0])

    def test_terminal_capture_rejects_even_identical_late_native_artifact(self) -> None:
        self.assertTrue(self.launch("APPROVE").complete)
        self.native.atomic("last-message.json", result("APPROVE"))
        captured = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertEqual(captured.code, Code.CONFLICT)
        self.assertFalse(captured.complete)
        self.assertIsNone(captured.actual_verdict)
        receipt = parse(self.store.read("receipt.json")[0])
        assert isinstance(receipt, dict)
        original = receipt["source_diagnostic"]
        assert isinstance(original, dict)
        self.assertEqual(original["code"], Code.OK)
        self.assertEqual(original["actual_verdict"], "APPROVE")
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)

    def test_observed_preexit_native_disappearance_cannot_authorize_fallback(self) -> None:
        unsafe = result("APPROVE")
        unsafe["limitations"] = ["Bearer synthetic-credential-value"]
        for raw in (b"not JSON", encode(unsafe), encode(result("COMMENT"))):
            self.reset_stores()
            with self.subTest(raw_kind="malformed" if raw == b"not JSON" else "structured"):
                path = self.prepare_recovery(raw)
                first = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertFalse(first.complete)
                self.assertEqual(first.code, Code.NO_EXIT)
                self.assertIsNone(first.sha256)
                self.assertTrue(path.exists())
                path.unlink()
                self.store.atomic("exit.json", {"exit_code": 0})
                second = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertEqual(second.code, Code.CHANGED)
                self.assertEqual(second.source, "native_last_message")
                self.assertFalse(second.complete)
                self.assertIsNone(second.actual_verdict)

    def test_preexit_partial_native_finalizes_only_after_exit_in_same_inode(self) -> None:
        path = self.prepare_recovery(b'{"reviewed_head":')
        identity = path.stat().st_ino
        claim = self.store.read("launch.claim")[0]
        spawn = self.store.read("spawn.json")[0]
        first = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertEqual(first.code, Code.NO_EXIT)
        self.assertIsNone(first.sha256)
        self.assertEqual(path.read_bytes(), b'{"reviewed_head":')
        with path.open("wb") as output:
            _ = output.write(encode(result("APPROVE")))
        self.assertEqual(path.stat().st_ino, identity)
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()).code, Code.NO_EXIT)
        self.store.atomic("exit.json", {"exit_code": 0})
        final = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertTrue(final.complete)
        self.assertEqual(final.actual_verdict, "APPROVE")
        self.assertFalse(final.parent_approval)
        self.assertEqual(final.model_identity, "UNVERIFIED")
        self.assertEqual(self.store.read("launch.claim")[0], claim)
        self.assertEqual(self.store.read("spawn.json")[0], spawn)

    def test_preexit_native_replacement_cannot_reset_source_identity(self) -> None:
        path = self.prepare_recovery(b"partial")
        first_inode = path.stat().st_ino
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()).code, Code.NO_EXIT)
        self.native.atomic("last-message.json", result("APPROVE"))
        self.assertNotEqual(path.stat().st_ino, first_inode)
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()).code, Code.CHANGED)
        self.store.atomic("exit.json", {"exit_code": 0})
        captured = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertEqual(captured.code, Code.CHANGED)
        self.assertFalse(captured.complete)
        receipt = parse(self.store.read("receipt.json")[0])
        assert isinstance(receipt, dict)
        identity = receipt["native_identity"]
        assert isinstance(identity, list)
        self.assertEqual(identity[1], first_inode)

    def test_rescreened_nonterminal_rejection_survives_policy_relaxation_and_exit(self) -> None:
        _ = self.prepare_recovery(b"partial")
        capture = EventCapture(CONTEXT, TextPolicy())
        payload = result("APPROVE")
        payload["limitations"] = ["fixture-only"]
        capture.feed(encode({"type": "turn.started"}))
        capture.feed(encode({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}))
        capture.feed(encode({"type": "turn.completed"}))
        capture.persist(self.store)
        captured = recover(self.store, self.native, CONTEXT, TextPolicy(forbidden=("fixture-only",)))
        self.assertEqual(captured.code, Code.UNSAFE)
        self.assertFalse(captured.complete)
        self.assertNotIn(b"fixture-only", self.store.read("turn.json")[0])
        self.assertNotIn(b"fixture-only", self.store.read("receipt.json")[0])
        self.store.atomic("exit.json", {"exit_code": 0})
        captured = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertEqual(captured.code, Code.UNSAFE)
        self.assertIsNone(captured.actual_verdict)
        self.assertFalse(captured.complete)

    def test_rescreened_preexit_original_is_removed_after_exit_without_reading(self) -> None:
        payload = result("APPROVE")
        payload["limitations"] = ["fixture-only"]
        path = self.prepare_recovery(encode(payload))
        capture = EventCapture(CONTEXT, TextPolicy())
        capture.feed(encode({"type": "turn.started"}))
        capture.feed(encode({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(payload)}}))
        capture.feed(encode({"type": "turn.completed"}))
        capture.persist(self.store)
        policy = TextPolicy(forbidden=("fixture-only",))
        with patch.object(self.native, "read", side_effect=AssertionError("raw must remain unread")):
            first = recover(self.store, self.native, CONTEXT, policy)
            self.assertEqual(first.code, Code.UNSAFE)
            self.assertTrue(path.exists())
            self.store.atomic("exit.json", {"exit_code": 0})
            second = recover(self.store, self.native, CONTEXT, policy)
        self.assertEqual(second.code, Code.UNSAFE)
        self.assertFalse(second.complete)
        self.assertFalse(path.exists())
        for name in ("receipt.json", "turn.json"):
            self.assertNotIn(b"fixture-only", self.store.read(name)[0])
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()).code, Code.UNSAFE)
        with self.assertRaises(Rejected) as error:
            _ = self.launch("APPROVE")
        self.assertEqual(error.exception.code, Code.LAUNCH)

    def test_cleanup_crash_at_unlink_recovers_same_completed_result(self) -> None:
        path = self.prepare_recovery(encode(result("APPROVE")), exited=True)
        claim, spawn = (self.store.read(name)[0] for name in ("launch.claim", "spawn.json"))
        original_unlink = os.unlink

        def crash(name: str, *, dir_fd: int | None = None) -> None:
            if name == "last-message.json" and dir_fd == self.native.fd:
                self.assertTrue((self.store.path / "receipt.json").exists())
                raise RuntimeError("synthetic capture crash")
            original_unlink(name, dir_fd=dir_fd)

        with patch("tools.review_result.os.unlink", crash):
            with self.assertRaises(RuntimeError):
                _ = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertTrue(path.exists())
        pending = parse(self.store.read("receipt.json")[0])
        assert isinstance(pending, dict)
        diagnostic, cleanup = pending["diagnostic"], pending["cleanup"]
        assert isinstance(diagnostic, dict) and isinstance(cleanup, dict)
        self.assertFalse(diagnostic["complete"])
        self.assertEqual(cleanup["state"], "pending")
        self.assertEqual(cleanup["identity"], list(PrivateStore.identity(path.stat())))
        self.assertEqual(cleanup["sha256"], diagnostic["sha256"])
        self.store.close()
        self.native.close()
        self.store = PrivateStore(self.store.path, self.store.path.parent)
        self.native = PrivateStore(self.native.path, self.native.path.parent)
        with patch("tools.review_result.subprocess.Popen", side_effect=AssertionError("no new child")):
            captured = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertTrue(captured.complete)
        self.assertEqual(captured.actual_verdict, "APPROVE")
        self.assertEqual(status_code(captured), 0)
        self.assertFalse(captured.parent_approval)
        self.assertEqual(captured.model_identity, "UNVERIFIED")
        self.assertTrue(captured.schema_valid)
        self.assertEqual(captured.root_type, "dict")
        self.assertFalse(path.exists())
        self.assertEqual(self.store.read("launch.claim")[0], claim)
        self.assertEqual(self.store.read("spawn.json")[0], spawn)
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)
        receipt = parse(self.store.read("receipt.json")[0])
        assert isinstance(receipt, dict) and isinstance(receipt["cleanup"], dict)
        self.assertEqual(receipt["cleanup"]["state"], "done")

    def test_cleanup_crash_after_unlink_finalizes_missing_original_not_rejection(self) -> None:
        for unsafe in (False, True):
            self.reset_stores()
            with self.subTest(unsafe=unsafe):
                payload = result("APPROVE")
                if unsafe:
                    payload["limitations"] = ["Bearer synthetic-credential-value"]
                path = self.prepare_recovery(encode(payload), exited=True)
                original_unlink = os.unlink

                def crash(name: str, *, dir_fd: int | None = None) -> None:
                    original_unlink(name, dir_fd=dir_fd)
                    if name == "last-message.json" and dir_fd == self.native.fd:
                        raise RuntimeError("synthetic crash after unlink")

                with patch("tools.review_result.os.unlink", crash):
                    with self.assertRaises(RuntimeError):
                        _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertFalse(path.exists())
                self.store.close()
                self.native.close()
                self.store = PrivateStore(self.store.path, self.store.path.parent)
                self.native = PrivateStore(self.native.path, self.native.path.parent)
                captured = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertEqual(captured.code, Code.UNSAFE if unsafe else Code.OK)
                self.assertEqual(captured.complete, not unsafe)
                self.assertEqual(status_code(captured), 2 if unsafe else 0)
                self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)
                receipt = parse(self.store.read("receipt.json")[0])
                assert isinstance(receipt, dict) and isinstance(receipt["cleanup"], dict)
                self.assertEqual(receipt["cleanup"]["state"], "done")
                self.assertNotIn(b"synthetic-credential-value", self.store.read("receipt.json")[0])

    def test_pending_cleanup_rejects_changed_replacement_and_aliased_original(self) -> None:
        for change in ("content", "mtime", "mode", "replacement", "hardlink", "symlink", "owner"):
            self.reset_stores()
            with self.subTest(change=change):
                path = self.prepare_recovery(encode(result("APPROVE")), exited=True)
                original_unlink = os.unlink

                def crash(name: str, *, dir_fd: int | None = None) -> None:
                    if name == "last-message.json" and dir_fd == self.native.fd:
                        raise RuntimeError("synthetic crash before unlink")
                    original_unlink(name, dir_fd=dir_fd)

                with patch("tools.review_result.os.unlink", crash):
                    with self.assertRaises(RuntimeError):
                        _ = recover(self.store, self.native, CONTEXT, TextPolicy())
                pending = parse(self.store.read("receipt.json")[0])
                assert isinstance(pending, dict)
                original = pending["source_diagnostic"]
                if change == "content":
                    _ = path.write_bytes(encode(result("COMMENT")))
                elif change == "mtime":
                    info = path.stat()
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
                elif change == "mode":
                    path.chmod(0o644)
                elif change == "replacement":
                    self.native.atomic("last-message.json", result("APPROVE"))
                elif change == "hardlink":
                    os.link(path, self.native.path / "alias.json")
                elif change == "symlink":
                    path.unlink()
                    self.native.atomic("alias.json", result("APPROVE"))
                    path.symlink_to(self.native.path / "alias.json")
                expected = Code.FILE if change in ("hardlink", "symlink") else Code.CHANGED
                if change == "owner":
                    expected = Code.OWNER
                    fields = list(path.stat())
                    fields[4] = os.getuid() + 1
                    with patch.object(self.native, "info", return_value=os.stat_result(fields)):
                        captured = recover(self.store, self.native, CONTEXT, TextPolicy())
                else:
                    captured = recover(self.store, self.native, CONTEXT, TextPolicy())
                self.assertEqual(captured.code, expected)
                self.assertFalse(captured.complete)
                self.assertTrue(path.exists())
                receipt = parse(self.store.read("receipt.json")[0])
                assert isinstance(receipt, dict) and isinstance(receipt["cleanup"], dict)
                self.assertEqual(receipt["source_diagnostic"], original)
                self.assertEqual(receipt["cleanup_failure"], {"stage": Stage.FILE, "code": expected})
                self.assertEqual(receipt["cleanup"]["state"], "blocked")
                path.unlink()
                self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)

    def test_pending_cleanup_rescreens_current_policy_before_finishing_original(self) -> None:
        payload = result("APPROVE")
        payload["limitations"] = ["fixture-only"]
        path = self.prepare_recovery(encode(payload), exited=True)
        capture = EventCapture(CONTEXT, TextPolicy())
        capture.feed(encode({"type": "turn.started"}))
        capture.feed(encode({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(payload)}}))
        capture.feed(encode({"type": "turn.completed"}))
        capture.persist(self.store)
        original_unlink = os.unlink

        def crash(name: str, *, dir_fd: int | None = None) -> None:
            if name == "last-message.json" and dir_fd == self.native.fd:
                raise RuntimeError("synthetic crash before unlink")
            original_unlink(name, dir_fd=dir_fd)

        with patch("tools.review_result.os.unlink", crash):
            with self.assertRaises(RuntimeError):
                _ = recover(self.store, self.native, CONTEXT, TextPolicy())
        with patch.object(self.native, "read", side_effect=AssertionError("no payload adoption")):
            captured = recover(self.store, self.native, CONTEXT, TextPolicy(forbidden=("fixture-only",)))
        self.assertEqual(captured.code, Code.UNSAFE)
        self.assertFalse(captured.complete)
        self.assertFalse(path.exists())
        for name in ("receipt.json", "turn.json"):
            self.assertNotIn(b"fixture-only", self.store.read(name)[0])
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)

    def test_cleanup_io_failure_is_typed_sticky_and_never_returns_complete(self) -> None:
        path = self.prepare_recovery(encode(result("APPROVE")), exited=True)
        original_unlink = os.unlink

        def refuse(name: str, *, dir_fd: int | None = None) -> None:
            if name == "last-message.json" and dir_fd == self.native.fd:
                raise PermissionError("Bearer synthetic-credential-value")
            original_unlink(name, dir_fd=dir_fd)

        with patch("tools.review_result.os.unlink", refuse):
            captured = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertEqual(captured.code, Code.IO)
        self.assertFalse(captured.complete)
        self.assertEqual(status_code(captured), 2)
        self.assertTrue(path.exists())
        receipt = parse(self.store.read("receipt.json")[0])
        assert isinstance(receipt, dict) and isinstance(receipt["source_diagnostic"], dict)
        self.assertEqual(receipt["source_diagnostic"]["code"], Code.OK)
        self.assertEqual(receipt["cleanup_failure"], {"stage": Stage.FILE, "code": Code.IO})
        self.assertNotIn(b"synthetic-credential-value", self.store.read("receipt.json")[0])
        path.unlink()
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)

    def test_rescreened_original_cleanup_keeps_rejection_and_typed_identity_limitation(self) -> None:
        payload = result("APPROVE")
        payload["limitations"] = ["fixture-only"]
        for change in ("replacement", "hardlink", "owner"):
            self.reset_stores()
            with self.subTest(change=change):
                path = self.prepare_recovery(encode(payload))
                capture = EventCapture(CONTEXT, TextPolicy())
                capture.feed(encode({"type": "turn.started"}))
                capture.feed(encode({"type": "item.completed", "item": {
                    "type": "agent_message", "text": json.dumps(payload)}}))
                capture.feed(encode({"type": "turn.completed"}))
                capture.persist(self.store)
                policy = TextPolicy(forbidden=("fixture-only",))
                self.assertEqual(recover(self.store, self.native, CONTEXT, policy).code, Code.UNSAFE)
                if change == "replacement":
                    self.native.atomic("last-message.json", payload)
                elif change == "hardlink":
                    os.link(path, self.native.path / "alias.json")
                self.store.atomic("exit.json", {"exit_code": 0})
                expected = Code.CHANGED if change == "replacement" else Code.FILE
                with patch.object(self.native, "read", side_effect=AssertionError("no rejected payload read")):
                    if change == "owner":
                        expected = Code.OWNER
                        fields = list(path.stat())
                        fields[4] = os.getuid() + 1
                        with patch.object(self.native, "info", return_value=os.stat_result(fields)):
                            captured = recover(self.store, self.native, CONTEXT, policy)
                    else:
                        captured = recover(self.store, self.native, CONTEXT, policy)
                self.assertEqual(captured.code, Code.UNSAFE)
                self.assertFalse(captured.complete)
                self.assertTrue(path.exists())
                receipt = parse(self.store.read("receipt.json")[0])
                assert isinstance(receipt, dict)
                self.assertEqual(receipt["cleanup_failure"], {"stage": Stage.FILE, "code": expected})
                self.assertNotIn(b"fixture-only", self.store.read("receipt.json")[0])
                path.unlink()
                self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)

    def test_refused_mode_original_is_removed_without_content_read_or_normalization(self) -> None:
        path = self.prepare_recovery(encode(result("APPROVE")), exited=True)
        path.chmod(0o644)
        original_read = PrivateStore.read

        def guarded(store: PrivateStore, name: str) -> tuple[bytes, os.stat_result]:
            if store is self.native:
                with patch("tools.review_result.os.open", side_effect=AssertionError("refused file must not open")):
                    return original_read(store, name)
            return original_read(store, name)

        with patch.object(PrivateStore, "read", guarded):
            captured = recover(self.store, self.native, CONTEXT, TextPolicy())
        self.assertEqual(captured.code, Code.MODE)
        self.assertEqual(captured.observed_mode, "0644")
        self.assertIsNone(captured.sha256)
        self.assertFalse(path.exists())
        self.assertEqual(recover(self.store, self.native, CONTEXT, TextPolicy()), captured)

    def test_current_policy_scrubs_native_present_and_nonterminal_recovery(self) -> None:
        for terminal in (True, False):
            self.reset_stores()
            with self.subTest(terminal=terminal):
                self.assertTrue(self.launch("policy-update").complete)
                payload = result()
                payload["limitations"] = ["fixture-only"]
                self.native.atomic("last-message.json", payload)
                receipt = parse(self.store.read("receipt.json")[0])
                assert isinstance(receipt, dict)
                receipt["terminal"] = terminal
                self.store.atomic("receipt.json", receipt)
                captured = recover(self.store, self.native, CONTEXT, TextPolicy(forbidden=("fixture-only",)))
                self.assertEqual(captured.code, Code.UNSAFE)
                self.assertFalse(captured.complete)
                for name in ("receipt.json", "turn.json"):
                    self.assertNotIn(b"fixture-only", self.store.read(name)[0])

    def test_parse_does_not_include_raw_error_text(self) -> None:
        with self.assertRaises(Rejected) as error:
            _ = parse(b"private invalid input")
        self.assertEqual(str(error.exception), Code.JSON)
        self.assertEqual(error.exception.stage, Stage.EXTRACTION)
