"""Offline stdlib subprocess capture tests; no inference or live identity claims."""

from pathlib import Path
from dataclasses import dataclass
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
    Rejected, Stage, TextPolicy, encode, inspect, parse, recover, run_prepared, status_code,
)

CONTEXT = Context("a" * 40, "b" * 40, 1, "synthetic-model", "synthetic-job")


def result(verdict: str = "COMMENT") -> dict[str, Json]:
    return {"reviewed_head": CONTEXT.head, "reviewed_base": CONTEXT.base,
            "verdict": verdict, "findings": [], "limitations": [], "checks_run": []}


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
               progress: list[dict[str, Json]] | None = None) -> Diagnostic:
        # No owner's ambient environment or home reaches the child.
        env = {"HOME": str(self.native.path), "TMPDIR": str(self.native.path),
               "PYTHONPATH": str(ROOT), "SYNTHETIC_VALUE": "fixture-only"}
        return run_prepared(self.store, self.native, CONTEXT,
                            [sys.executable, str(ROOT / "tests/review_result_fixture.py"), mode,
                             CONTEXT.head, CONTEXT.base], b"synthetic prompt", env, TextPolicy(),
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

    def test_parse_does_not_include_raw_error_text(self) -> None:
        with self.assertRaises(Rejected) as error:
            _ = parse(b"private invalid input")
        self.assertEqual(str(error.exception), Code.JSON)
        self.assertEqual(error.exception.stage, Stage.EXTRACTION)
