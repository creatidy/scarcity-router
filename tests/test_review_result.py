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
