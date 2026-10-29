"""Worker-local private CA trust tests (issue #224 scope 2).

Deterministic and CI-safe: real TLS proofs use the existing trustme
loopback fixtures (ephemeral throwaway certificates, never real
secrets); behavioral pairing/session tests use in-memory transports and
scripted fakes; service-lifecycle tests use the recording ServiceTool
fakes. No live provider, no live CA issuance, no systemd daemon, no
public host.
"""

from __future__ import annotations

import io
import ctypes
import os
import sqlite3
import socket
import ssl
import tempfile
import threading
import traceback
import unittest
import unittest.mock
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override

from tests.m10_fixtures import TlsMaterials
from tests.test_worker_service import SYNTHETIC_ORIGIN as SERVICE_ORIGIN
from tests.test_worker_service import ServiceWorld
from tests.worker_fixtures import SYNTHETIC_CODE, SYNTHETIC_CREDENTIAL, MemoryTransport
from scarcity_router import worker_client
from scarcity_router.worker_ca_trust import (
    WORKER_CA_TRUST_STORE_KEY,
    WorkerCaTrust,
    WorkerCaTrustError,
    effective_stored_ca_path,
    tls_context_for_worker_ca,
    trust_from_json,
    trust_to_json,
    validate_ca_cert_file,
)
from scarcity_router.worker_identity_store import WorkerIdentityStore
from scarcity_router.worker_local_store import (
    WorkerLocalIdentity,
    WorkerLocalStore,
)
from scarcity_router.worker_protocol import (
    ErrorMessage,
    FrameReader,
    FrameWriter,
    HelloAckMessage,
    HelloMessage,
    PairRequestMessage,
    PairResultMessage,
    WORKER_PROTOCOL_VERSION,
    parse_worker_message,
)

PAIR_ORIGIN = "srws://gateway.local:8790"
OTHER_ORIGIN = "srws://other.example:8790"


def _identity(
    origin: str = PAIR_ORIGIN, worker_id: str = "w-ca-1"
) -> WorkerLocalIdentity:
    return WorkerLocalIdentity(
        worker_id=worker_id, credential=SYNTHETIC_CREDENTIAL, server_origin=origin
    )


def _plant_raw_trust_row(db_path: str, document: str) -> None:
    """Plant a raw trust row (the corruption agent for fail-closed tests)."""
    with sqlite3.connect(db_path) as connection:
        _ = connection.execute(
            "INSERT INTO worker_state (key, value, updated_at) VALUES (?, ?, ?) "
            + "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (WORKER_CA_TRUST_STORE_KEY, document, "2026-10-28T00:00:00.000Z"),
        )


# ── CA file validation ────────────────────────────────────────────────────────


class CaFileValidationTests(unittest.TestCase):
    """``validate_ca_cert_file`` accepts only bounded certificates-only PEM."""

    tls: TlsMaterials
    tmp: tempfile.TemporaryDirectory[str]

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.tls = cast(TlsMaterials, object())
        self.tmp = cast("tempfile.TemporaryDirectory[str]", object())

    @override
    def setUp(self) -> None:
        self.tls = TlsMaterials()
        self.addCleanup(self.tls.cleanup)
        self.tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name: str) -> Path:
        return Path(self.tmp.name) / name

    def test_valid_ca_passes_and_normalizes_to_the_canonical_path(self) -> None:
        noncanonical = os.path.join(
            str(self.tls.ca_file.parent), ".", self.tls.ca_file.name
        )
        resolved = validate_ca_cert_file(noncanonical)
        self.assertEqual(Path(os.path.realpath(str(self.tls.ca_file))), resolved)
        self.assertTrue(resolved.is_absolute())

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_PATH"), "Linux symlink compatibility")
    def test_operator_supplied_symlink_resolves_before_anchored_open(self) -> None:
        link = self._path("operator-link.pem")
        link.symlink_to(self.tls.ca_file)
        self.assertEqual(self.tls.ca_file.resolve(), validate_ca_cert_file(link))

    def test_missing_file_fails_closed(self) -> None:
        with self.assertRaises(WorkerCaTrustError):
            _ = validate_ca_cert_file(self._path("absent.pem"))

    def test_directory_fails_closed(self) -> None:
        directory = self._path("dir")
        _ = directory.mkdir()
        with self.assertRaises(WorkerCaTrustError):
            _ = validate_ca_cert_file(directory)

    def test_oversize_file_fails_closed(self) -> None:
        big = self._path("big.pem")
        _ = big.write_bytes(b"A" * 65537)
        with self.assertRaises(WorkerCaTrustError):
            _ = validate_ca_cert_file(big)

    def test_binary_content_fails_closed(self) -> None:
        binary = self._path("binary.pem")
        _ = binary.write_bytes(bytes(range(256)) * 2)
        with self.assertRaises(WorkerCaTrustError):
            _ = validate_ca_cert_file(binary)

    def test_corrupt_pem_fails_closed(self) -> None:
        corrupt = self._path("corrupt.pem")
        _ = corrupt.write_text(
            "-----BEGIN CERTIFICATE-----\nnot-base64!!!\n-----END CERTIFICATE-----\n",
            encoding="ascii",
        )
        with self.assertRaises(WorkerCaTrustError):
            _ = validate_ca_cert_file(corrupt)

    def test_nonempty_der_garbage_fails_actual_openssl_validation(self) -> None:
        garbage = self._path("garbage-x509.pem")
        _ = garbage.write_text(
            "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n",
            encoding="ascii",
        )
        with self.assertRaises(WorkerCaTrustError):
            _ = validate_ca_cert_file(garbage)
        with self.assertRaises(WorkerCaTrustError):
            _ = tls_context_for_worker_ca(garbage)

    def test_certificate_less_file_fails_closed(self) -> None:
        empty = self._path("empty.pem")
        _ = empty.write_text("", encoding="ascii")
        with self.assertRaises(WorkerCaTrustError):
            _ = validate_ca_cert_file(empty)

    def test_private_key_bundle_is_refused_never_adopted(self) -> None:
        bundle = self._path("bundle.pem")
        certificate = self.tls.ca_file.read_text(encoding="ascii")
        _ = bundle.write_text(
            certificate
            + "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
            encoding="ascii",
        )
        with self.assertRaises(WorkerCaTrustError) as caught:
            _ = validate_ca_cert_file(bundle)
        self.assertIn("other than CA certificates", str(caught.exception))

    def test_trailing_garbage_fails_closed(self) -> None:
        dirty = self._path("dirty.pem")
        _ = dirty.write_text(
            self.tls.ca_file.read_text(encoding="ascii") + "trailing garbage\n",
            encoding="ascii",
        )
        with self.assertRaises(WorkerCaTrustError):
            _ = validate_ca_cert_file(dirty)

    def test_error_messages_leak_no_path_or_pem_material(self) -> None:
        cases: list[str] = [
            str(self._path("absent2.pem")),  # missing
            str(self._path("dir2")),  # directory
            str(self._path("big2.pem")),  # oversize
            str(self._path("binary2.pem")),  # binary
            str(self._path("corrupt2.pem")),  # corrupt PEM
        ]
        _ = self._path("dir2").mkdir()
        _ = self._path("big2.pem").write_bytes(b"A" * 65537)
        _ = self._path("binary2.pem").write_bytes(bytes(range(256)) * 2)
        _ = self._path("corrupt2.pem").write_text(
            "-----BEGIN CERTIFICATE-----\n!!\n-----END CERTIFICATE-----\n",
            encoding="ascii",
        )
        messages: list[str] = []
        for case in cases:
            try:
                _ = validate_ca_cert_file(case)
            except WorkerCaTrustError as exc:
                messages.append(str(exc))
        self.assertEqual(len(cases), len(messages))
        for message in messages:
            self.assertNotIn(self.tmp.name, message)
            self.assertNotIn("BEGIN", message)
            self.assertNotIn(SYNTHETIC_CREDENTIAL, message)

    def test_chained_traceback_never_exposes_untrusted_file_path(self) -> None:
        secret = "SYNTHETIC-SECRET-IN-PATH"
        try:
            _ = validate_ca_cert_file(self._path(secret))
        except WorkerCaTrustError as exc:
            rendered = "".join(traceback.format_exception(exc))
        else:
            self.fail("missing CA unexpectedly accepted")
        self.assertNotIn(secret, rendered)

    def test_supplemented_context_keeps_verification_hard_on(self) -> None:
        context = tls_context_for_worker_ca(self.tls.ca_file)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo") and hasattr(os, "O_PATH"), "Linux anchored FIFO test")
    def test_fifo_is_refused_without_opening_it_for_data(self) -> None:
        fifo = self._path("blocking-fifo")
        os.mkfifo(fifo)
        with self.assertRaisesRegex(WorkerCaTrustError, "regular file"):
            _ = validate_ca_cert_file(fifo)

    @unittest.skipUnless(os.name == "posix", "POSIX safety capability test")
    def test_missing_safety_capability_refuses_instead_of_falling_back(self) -> None:
        with unittest.mock.patch("scarcity_router.worker_ca_trust._O_NOFOLLOW", 0):
            with self.assertRaisesRegex(WorkerCaTrustError, "safe private-CA file handling is unavailable"):
                _ = validate_ca_cert_file(self.tls.ca_file)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_PATH"), "Linux parent-race test")
    def test_swapped_parent_symlink_is_never_followed(self) -> None:
        root = self._path("trusted-parent")
        root.mkdir()
        ca = root / "ca.pem"
        _ = ca.write_bytes(self.tls.ca_file.read_bytes())
        alternative = self._path("alternative")
        alternative.mkdir()
        _ = (alternative / "ca.pem").write_bytes(self.tls.ca_file.read_bytes())
        original_open = os.open
        swapped = False

        def swap_parent(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            nonlocal swapped
            if path == "trusted-parent" and dir_fd is not None and not swapped:
                swapped = True
                _ = root.rename(self._path("original-parent"))
                root.symlink_to(alternative, target_is_directory=True)
            return original_open(cast("str", path), flags, mode, dir_fd=dir_fd)

        with unittest.mock.patch("scarcity_router.worker_ca_trust.os.open", side_effect=swap_parent):
            with self.assertRaises(WorkerCaTrustError):
                _ = validate_ca_cert_file(ca)
        self.assertTrue(swapped)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_PATH") and hasattr(os, "mkfifo"), "Linux final-object race test")
    def test_regular_file_replaced_by_fifo_is_refused_before_data_open(self) -> None:
        ca = self._path("raced.pem")
        _ = ca.write_bytes(self.tls.ca_file.read_bytes())
        original_open = os.open
        swapped = False

        def swap_final(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            nonlocal swapped
            if path == "raced.pem" and dir_fd is not None and not swapped:
                swapped = True
                _ = ca.rename(self._path("previous.pem"))
                os.mkfifo(ca)
            return original_open(cast("str", path), flags, mode, dir_fd=dir_fd)

        with unittest.mock.patch("scarcity_router.worker_ca_trust.os.open", side_effect=swap_final):
            with self.assertRaisesRegex(WorkerCaTrustError, "regular file"):
                _ = validate_ca_cert_file(ca)
        self.assertTrue(swapped)


class WindowsHandleValidationTests(unittest.TestCase):
    class Function:
        def __init__(self, callback: Callable[[tuple[object, ...]], object]) -> None:
            self.argtypes: object = None
            self.restype: object = None
            self.callback: Callable[[tuple[object, ...]], object] = callback
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args: object) -> object:
            self.calls.append(args)
            return self.callback(args)

    class Kernel:
        def __init__(self, *, reparse_handle: int | None = None, file_type: int = 1) -> None:
            handles = iter((100, 101, 102))
            self.CreateFileW: WindowsHandleValidationTests.Function = WindowsHandleValidationTests.Function(lambda _args: next(handles))
            self.GetDriveTypeW: WindowsHandleValidationTests.Function = WindowsHandleValidationTests.Function(lambda _args: 3)
            self.GetFileType: WindowsHandleValidationTests.Function = WindowsHandleValidationTests.Function(lambda _args: file_type)
            self.CloseHandle: WindowsHandleValidationTests.Function = WindowsHandleValidationTests.Function(lambda _args: True)
            def info(args: tuple[object, ...]) -> object:
                handle = cast(int, args[0])
                attributes = (0x10 if handle in (100, 101) else 0) | (0x400 if handle == reparse_handle else 0)
                values = (ctypes.c_ulong * 2)(attributes, 0)
                _ = ctypes.memmove(cast("ctypes.c_void_p", args[2]), ctypes.byref(values), ctypes.sizeof(values))
                return True
            self.GetFileInformationByHandleEx: WindowsHandleValidationTests.Function = WindowsHandleValidationTests.Function(info)

    class Runtime:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []
        def open_osfhandle(self, handle: int, flags: int) -> int:
            self.calls.append((handle, flags))
            return 42

    def test_dos_devices_network_and_stream_paths_fail_before_native_open(self) -> None:
        from scarcity_router.worker_ca_windows import WindowsCaOpenError, open_windows_regular

        for value in (r"\\server\share\ca.pem", r"\\.\pipe\worker", r"C:\x\NUL.pem",
                      r"C:\x\COM¹.txt", r"C:\x\ca.pem:stream", r"C:\x\bad?.pem"):
            with self.subTest(value=value), self.assertRaises(WindowsCaOpenError):
                _ = open_windows_regular(value)

    def test_native_open_pins_parents_and_refuses_delete_sharing(self) -> None:
        from scarcity_router.worker_ca_windows import open_windows_regular

        kernel = self.Kernel()
        runtime = self.Runtime()
        with (unittest.mock.patch.object(ctypes, "WinDLL", create=True, return_value=kernel),
              unittest.mock.patch("scarcity_router.worker_ca_windows.importlib.import_module", return_value=runtime)):
            self.assertEqual(42, open_windows_regular(r"C:\certs\root.pem"))
        calls = kernel.CreateFileW.calls
        self.assertEqual(["C:\\", "C:\\certs", "C:\\certs\\root.pem"], [item[0] for item in calls])
        self.assertTrue(all(cast(int, item[2]) & 4 == 0 for item in calls))
        self.assertTrue(all(cast(int, item[5]) & 0x00200000 for item in calls))
        self.assertEqual([(101,), (100,)], kernel.CloseHandle.calls)
        self.assertEqual(1, len(runtime.calls))

    def test_reparse_parent_and_non_disk_object_are_refused(self) -> None:
        from scarcity_router.worker_ca_windows import WindowsCaOpenError, open_windows_regular

        for kernel in (self.Kernel(reparse_handle=101), self.Kernel(file_type=3)):
            runtime = self.Runtime()
            with (unittest.mock.patch.object(ctypes, "WinDLL", create=True, return_value=kernel),
                  unittest.mock.patch("scarcity_router.worker_ca_windows.importlib.import_module", return_value=runtime),
                  self.assertRaises(WindowsCaOpenError)):
                _ = open_windows_regular(r"C:\certs\root.pem")
            self.assertEqual([], runtime.calls)

    def test_platform_dispatch_reports_windows_failures_without_raw_details(self) -> None:
        from scarcity_router import worker_ca_trust
        from scarcity_router.worker_ca_windows import WindowsCaOpenError

        path = Path("/synthetic/ca.pem")
        with (unittest.mock.patch.object(os, "name", "nt"),
              unittest.mock.patch("scarcity_router.worker_ca_windows.open_windows_regular",
                                  side_effect=WindowsCaOpenError("SYNTHETIC-SECRET")),
              self.assertRaises(WorkerCaTrustError) as caught):
            _ = worker_ca_trust._open_anchored_regular(path)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("Windows", str(caught.exception))
        self.assertNotIn("SYNTHETIC-SECRET", str(caught.exception))


# ── Trust record + store persistence ──────────────────────────────────────────


class TrustRecordTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/ca.pem"
        )
        self.assertEqual(trust, trust_from_json(trust_to_json(trust)))

    def test_unknown_fields_fail_closed(self) -> None:
        document = (
            '{"ca_path":"/a.pem","extra":1,"schema_version":1,'
            + f'"server_origin":"{PAIR_ORIGIN}"}}'
        )
        with self.assertRaises(WorkerCaTrustError):
            _ = trust_from_json(document)

    def test_wrong_version_fails_closed(self) -> None:
        document = (
            '{"ca_path":"/a.pem","schema_version":99,'
            + f'"server_origin":"{PAIR_ORIGIN}"}}'
        )
        with self.assertRaises(WorkerCaTrustError) as caught:
            _ = trust_from_json(document)
        self.assertIn("verified run", str(caught.exception))

    def test_malformed_json_and_relative_paths_fail_closed(self) -> None:
        with self.assertRaises(WorkerCaTrustError):
            _ = trust_from_json("{not json")
        with self.assertRaises(WorkerCaTrustError):
            _ = trust_from_json(
                '{"ca_path":"relative.pem","schema_version":1,'
                + f'"server_origin":"{PAIR_ORIGIN}"}}'
            )

    def test_duplicate_fields_and_oversized_stored_records_are_refused(self) -> None:
        duplicate = ('{"schema_version":1,"server_origin":"' + PAIR_ORIGIN
                     + '","ca_path":"/tmp/ca.pem","ca_path":"/tmp/other.pem"}')
        with self.assertRaises(WorkerCaTrustError):
            _ = trust_from_json(duplicate)
        with self.assertRaises(WorkerCaTrustError):
            _ = trust_from_json(" " * 4097)

    def test_persistence_rejects_untrusted_path_material_and_noncanonical_origin(self) -> None:
        for path in ("relative.pem", "/tmp/../ca.pem", "/tmp/-----BEGIN PRIVATE KEY-----", "/tmp/ca\nsecret"):
            with self.subTest(path=path), self.assertRaises(WorkerCaTrustError) as caught:
                _ = trust_to_json(WorkerCaTrust(PAIR_ORIGIN, path))
            self.assertNotIn(path, str(caught.exception))
        for origin in ("srw://localhost:8790", "srws://GATEWAY.local:8790", "srws://user:secret@gateway.local:8790"):
            with self.subTest(origin=origin), self.assertRaises(WorkerCaTrustError) as caught:
                _ = trust_to_json(WorkerCaTrust(origin, "/tmp/ca.pem"))
            self.assertNotIn("secret", str(caught.exception))


class StoreTrustPersistenceTests(unittest.TestCase):
    _tmp: tempfile.TemporaryDirectory[str]
    path: str

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.path = cast(str, object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "worker-state.db")

    def _store(self) -> WorkerLocalStore:
        store = WorkerLocalStore(self.path)
        self.addCleanup(store.close)
        return store

    def _keys(self) -> set[str]:
        with sqlite3.connect(self.path) as connection:
            rows = cast(
                "list[tuple[object]]",
                connection.execute("SELECT key FROM worker_state").fetchall(),
            )
            return {str(row[0]) for row in rows}

    def test_legacy_store_without_ca_reads_unchanged(self) -> None:
        store = self._store()
        store.save_identity(_identity())
        self.assertIsNone(store.load_ca_trust())
        # Opening/reading wrote no new rows.
        self.assertEqual({"schema_version", "identity"}, self._keys())

    def test_invalid_trust_never_changes_identity_or_persisted_record(self) -> None:
        store = self._store()
        original = WorkerCaTrust(PAIR_ORIGIN, "/etc/scarcity/ca.pem")
        store.save_identity_with_ca(_identity(), original)
        invalid = WorkerCaTrust(PAIR_ORIGIN, "/tmp/-----BEGIN PRIVATE KEY-----")
        with self.assertRaises(WorkerCaTrustError):
            store.save_identity_with_ca(_identity(worker_id="replacement"), invalid)
        with self.assertRaises(WorkerCaTrustError):
            store.save_ca_trust(invalid)
        self.assertEqual(_identity(), store.load_identity())
        self.assertEqual(original, store.load_ca_trust())
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute("SELECT value FROM worker_state").fetchall()
        self.assertNotIn("PRIVATE KEY", str(rows))

    def test_stored_trust_survives_reopen_and_credential_rotation(self) -> None:
        trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/ca.pem"
        )
        store = self._store()
        store.save_identity_with_ca(_identity(), trust)
        reopened = self._store()
        self.assertEqual(trust, reopened.load_ca_trust())
        # The rotation path (credential_rotated) rewrites ONLY identity.
        rotated = WorkerLocalIdentity(
            worker_id="w-ca-1",
            credential="SYNTHETIC-ROTATED-CREDENTIAL",
            server_origin=PAIR_ORIGIN,
        )
        reopened.save_identity(rotated)
        self.assertEqual(trust, reopened.load_ca_trust())

    def test_pairing_without_ca_preserves_existing_trust(self) -> None:
        trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/ca.pem"
        )
        store = self._store()
        store.save_identity_with_ca(_identity(worker_id="w-old"), trust)
        store.save_identity_with_ca(_identity(worker_id="w-new"), None)
        loaded = store.load_identity()
        assert loaded is not None
        self.assertEqual("w-new", loaded.worker_id)
        self.assertEqual(trust, store.load_ca_trust())

    def test_pairing_to_a_new_origin_clears_old_trust_atomically(self) -> None:
        old_trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/old.pem"
        )
        store = self._store()
        store.save_identity_with_ca(_identity(), old_trust)
        store.save_identity_with_ca(_identity(origin=OTHER_ORIGIN), None)
        loaded = store.load_identity()
        assert loaded is not None
        self.assertEqual(OTHER_ORIGIN, loaded.server_origin)
        self.assertIsNone(store.load_ca_trust())

    def test_same_origin_rotation_preserves_malformed_raw_trust(self) -> None:
        store = self._store()
        store.save_identity(_identity())
        store.close()
        _plant_raw_trust_row(self.path, "{malformed")
        reopened = self._store()
        reopened.save_identity(
            WorkerLocalIdentity(
                worker_id="w-rotated",
                credential="SYNTHETIC-ROTATED-CREDENTIAL",
                server_origin=PAIR_ORIGIN,
            )
        )
        loaded = reopened.load_identity()
        assert loaded is not None
        self.assertEqual("w-rotated", loaded.worker_id)
        with self.assertRaises(WorkerCaTrustError):
            _ = reopened.load_ca_trust()

    def test_post_auth_trust_write_rejects_a_replaced_identity(self) -> None:
        old_identity = _identity(worker_id="w-old")
        new_identity = _identity(worker_id="w-new")
        old_trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/old.pem"
        )
        store = self._store()
        store.save_identity_with_ca(old_identity, old_trust)
        store.save_identity(new_identity)
        with self.assertRaises(ValueError):
            store.save_ca_trust(
                WorkerCaTrust(
                    server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/new.pem"
                ),
                expected_identity=old_identity,
            )
        self.assertEqual(old_trust, store.load_ca_trust())

    def test_pairing_transaction_is_atomic_under_a_write_failure(self) -> None:
        old_trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/old.pem"
        )
        store = self._store()
        store.save_identity_with_ca(_identity(worker_id="w-old"), old_trust)
        new_trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/new.pem"
        )
        with unittest.mock.patch.object(
            WorkerLocalStore,
            "_write_ca_trust_row",
            side_effect=sqlite3.Error("synthetic local write failure"),
        ):
            with self.assertRaises(sqlite3.Error):
                store.save_identity_with_ca(_identity(worker_id="w-new"), new_trust)
        # Rollback: the old identity AND the old trust are intact.
        loaded = store.load_identity()
        assert loaded is not None
        self.assertEqual("w-old", loaded.worker_id)
        self.assertEqual(old_trust, store.load_ca_trust())

    def test_clear_identity_removes_the_trust_too(self) -> None:
        store = self._store()
        store.save_identity_with_ca(
            _identity(),
            WorkerCaTrust(server_origin=PAIR_ORIGIN, ca_path="/etc/ca.pem"),
        )
        store.clear_identity()
        self.assertIsNone(store.load_identity())
        self.assertIsNone(store.load_ca_trust())
        self.assertEqual({"schema_version"}, self._keys())

    def test_generic_value_writes_cannot_touch_the_trust_row(self) -> None:
        store = self._store()
        with self.assertRaises(ValueError):
            _ = store.save_value(WORKER_CA_TRUST_STORE_KEY, "{}")
        self.assertIsNone(store.load_ca_trust())

    def test_malformed_stored_record_fails_closed(self) -> None:
        store = self._store()
        store.close()
        _plant_raw_trust_row(self.path, "{corrupt")
        reopened = self._store()
        with self.assertRaises(WorkerCaTrustError):
            _ = reopened.load_ca_trust()
        with self.assertRaises(WorkerCaTrustError):
            _ = effective_stored_ca_path(reopened, PAIR_ORIGIN)

    def test_old_software_ignores_the_new_row_safely(self) -> None:
        # Schema stays version 1 and the row is just another bounded
        # worker_state key: a reader that does not know it is unaffected.
        store = self._store()
        store.save_identity_with_ca(
            _identity(),
            WorkerCaTrust(server_origin=PAIR_ORIGIN, ca_path="/etc/ca.pem"),
        )
        with sqlite3.connect(self.path) as connection:
            version_row = cast(
                "tuple[object] | None",
                connection.execute(
                    "SELECT value FROM worker_state WHERE key = 'schema_version'"
                ).fetchone(),
            )
            assert version_row is not None
            version = str(version_row[0])
            pragma_rows = cast(
                "list[tuple[object, ...]]",
                connection.execute("PRAGMA table_info(worker_state)").fetchall(),
            )
            columns = [str(row[1]) for row in pragma_rows]
        self.assertEqual("1", version)
        self.assertEqual(["key", "value", "updated_at"], columns)
        self.assertIsNotNone(store.load_identity())


class EffectiveTrustTests(unittest.TestCase):
    def test_stored_ca_applies_only_to_its_own_origin(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp:
            store = WorkerLocalStore(os.path.join(tmp, "s.db"))
            self.addCleanup(store.close)
            store.save_identity(_identity())
            trust = WorkerCaTrust(
                server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/ca.pem"
            )
            store.save_ca_trust(trust)
            self.assertEqual(
                "/etc/scarcity/ca.pem", effective_stored_ca_path(store, PAIR_ORIGIN)
            )
            # A different explicit target never inherits the old CA.
            self.assertIsNone(effective_stored_ca_path(store, OTHER_ORIGIN))


# ── Runtime: CA pairing + persistence only after validation ──────────────────


class FakeSessionServer:
    """Answers one ``pair_request`` (or one ``hello``) over in-memory transport.

    The same scripted fake for the pairing and session handshakes: it
    either accepts (the conventional result/ack) or rejects with one
    typed fatal error. Records what it saw for assertions.
    """

    reject_code: str | None
    worker_side: MemoryTransport
    saw_pairing_code: str | None
    saw_worker_id: str | None
    _server_side: MemoryTransport

    def __init__(self, *, reject_code: str | None = None) -> None:
        self.reject_code = reject_code
        worker_side, server_side = MemoryTransport.pair()
        self.worker_side = worker_side
        self.saw_pairing_code = None
        self.saw_worker_id = None
        self._server_side = server_side
        _ = threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        reader = FrameReader(self._server_side)
        writer = FrameWriter(self._server_side)
        payload = reader.read_message()
        if payload is None:
            return
        message = parse_worker_message(payload)
        if isinstance(message, PairRequestMessage):
            self.saw_pairing_code = message.pairing_code
            if self.reject_code is not None:
                writer.write_message(
                    ErrorMessage(
                        code=self.reject_code,
                        message="synthetic rejection",
                        fatal=True,
                    ).to_payload()
                )
                return
            writer.write_message(
                PairResultMessage(
                    negotiated_version=WORKER_PROTOCOL_VERSION,
                    worker_id="w-fake-pair",
                    credential=SYNTHETIC_CREDENTIAL,
                    heartbeat_interval_seconds=15,
                ).to_payload()
            )
        elif isinstance(message, HelloMessage):
            self.saw_worker_id = message.worker_id
            if self.reject_code is not None:
                writer.write_message(
                    ErrorMessage(
                        code=self.reject_code,
                        message="synthetic rejection",
                        fatal=True,
                    ).to_payload()
                )
                return
            writer.write_message(
                HelloAckMessage(
                    negotiated_version=WORKER_PROTOCOL_VERSION,
                    heartbeat_interval_seconds=15,
                ).to_payload()
            )


class RuntimeCaPersistenceTests(unittest.TestCase):
    """The pairing/run trust lifecycle over scripted in-memory sessions."""

    _tmp: tempfile.TemporaryDirectory[str]
    store: WorkerLocalStore

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.store = cast(WorkerLocalStore, object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.store = WorkerLocalStore(os.path.join(self._tmp.name, "s.db"))
        self.addCleanup(self.store.close)
        self.store.save_identity(_identity())

    def _runtime(
        self,
        server: FakeSessionServer,
        *,
        accepted: Callable[[], None] | None = None,
    ) -> worker_client.WorkerRuntime:
        return worker_client.WorkerRuntime(
            origin=worker_client.WorkerOrigin.parse(PAIR_ORIGIN),
            store=self.store,
            connect_factory=lambda _origin: server.worker_side,
            reconnect=worker_client.ReconnectPolicy(
                initial_delay_seconds=0.005,
                max_delay_seconds=0.01,
                max_attempts=2,
            ),
            session_accepted=accepted,
        )

    def test_ca_pair_persists_identity_and_trust_together(self) -> None:
        server = FakeSessionServer()
        trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/ca.pem"
        )
        runtime = self._runtime(server)
        identity = runtime.pair(SYNTHETIC_CODE, ca_trust=trust)
        self.assertEqual("w-fake-pair", identity.worker_id)
        self.assertEqual(SYNTHETIC_CODE, server.saw_pairing_code)
        self.assertEqual(trust, self.store.load_ca_trust())

    def test_pair_rejection_preserves_old_identity_and_trust(self) -> None:
        old_trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/old.pem"
        )
        self.store.save_ca_trust(old_trust)
        old_identity = self.store.load_identity()
        server = FakeSessionServer(reject_code="pairing_code_invalid")
        new_trust = WorkerCaTrust(
            server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/new.pem"
        )
        runtime = self._runtime(server)
        with self.assertRaises(worker_client.WorkerRuntimeError):
            _ = runtime.pair(SYNTHETIC_CODE, ca_trust=new_trust)
        self.assertEqual(old_identity, self.store.load_identity())
        self.assertEqual(old_trust, self.store.load_ca_trust())

    def test_pair_refuses_a_trust_record_bound_to_another_origin(self) -> None:
        server = FakeSessionServer()
        runtime = self._runtime(server)
        with self.assertRaises(worker_client.WorkerConfigError):
            _ = runtime.pair(
                SYNTHETIC_CODE,
                ca_trust=WorkerCaTrust(
                    server_origin=OTHER_ORIGIN, ca_path="/etc/ca.pem"
                ),
            )
        # Nothing was persisted by the refused pairing.
        self.assertIsNone(server.saw_pairing_code)
        self.assertIsNone(self.store.load_ca_trust())

    def test_pair_explicit_ca_refuses_plaintext_before_code_send(self) -> None:
        server = FakeSessionServer()
        runtime = worker_client.WorkerRuntime(
            origin=worker_client.WorkerOrigin.parse("srw://127.0.0.1:8790"),
            store=self.store,
            connect_factory=lambda _origin: server.worker_side,
        )
        with self.assertRaises(worker_client.WorkerConfigError):
            _ = runtime.pair(
                SYNTHETIC_CODE,
                ca_trust=WorkerCaTrust(
                    server_origin="srw://127.0.0.1:8790",
                    ca_path="/etc/ca.pem",
                ),
            )
        self.assertIsNone(server.saw_pairing_code)

    def test_ca_factory_refuses_plaintext_before_opening_a_socket(self) -> None:
        factory = worker_client.ca_connect_factory("/etc/scarcity/ca.pem")
        with unittest.mock.patch.object(
            socket, "create_connection",
            side_effect=AssertionError("plaintext CA path must not open a socket"),
        ):
            with self.assertRaises(worker_client.WorkerConfigError):
                _ = factory(worker_client.WorkerOrigin.parse("srw://127.0.0.1:8790"))

    def test_session_accepted_hook_fires_after_auth_and_persists(self) -> None:
        server = FakeSessionServer()
        fired = threading.Event()
        persisted: list[WorkerCaTrust] = []

        def accepted() -> None:
            # The CLI's exact seam: persist the pending trust only now,
            # after TLS verification AND credential acceptance.
            persisted.append(
                WorkerCaTrust(
                    server_origin=PAIR_ORIGIN, ca_path="/etc/scarcity/new.pem"
                )
            )
            self.store.save_ca_trust(persisted[0])
            fired.set()

        runtime = self._runtime(server, accepted=accepted)
        result: list[str] = []
        thread = threading.Thread(
            target=lambda: result.append(runtime.run()), daemon=True
        )
        thread.start()
        self.assertTrue(fired.wait(timeout=10.0), "session was never accepted")
        runtime.request_stop()
        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["requested"], result)
        stored = self.store.load_ca_trust()
        assert stored is not None
        self.assertEqual("/etc/scarcity/new.pem", stored.ca_path)

    def test_failed_authentication_never_fires_the_hook(self) -> None:
        server = FakeSessionServer(reject_code="unauthorized")
        fired = threading.Event()
        runtime = self._runtime(server, accepted=fired.set)
        self.assertEqual("fatal", runtime.run())
        self.assertFalse(fired.is_set())


class ResolveRunTrustTests(unittest.TestCase):
    """The ``run`` trust decision (flag selection, origin binding)."""

    _tmp: tempfile.TemporaryDirectory[str]
    store: WorkerLocalStore

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.store = cast(WorkerLocalStore, object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.store = WorkerLocalStore(os.path.join(self._tmp.name, "s.db"))
        self.addCleanup(self.store.close)
        self.store.save_identity(_identity())

    def _origin(self, text: str = PAIR_ORIGIN) -> worker_client.WorkerOrigin:
        return worker_client.WorkerOrigin.parse(text)

    def _recording_ca_factory(self, recorded: list[str]) -> None:
        def fake_ca_factory(ca_path: str) -> worker_client.ConnectFactory:
            recorded.append(ca_path)
            return worker_client.default_connect_factory

        patcher = unittest.mock.patch.object(
            worker_client, "ca_connect_factory", fake_ca_factory
        )
        _ = patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_ca_anywhere_is_exactly_the_default_factory(self) -> None:
        factory, pending = worker_client.resolve_run_trust(
            self.store, self._origin(), None
        )
        self.assertIs(worker_client.default_connect_factory, factory)
        self.assertIsNone(pending)

    def test_stored_ca_for_this_origin_is_consumed(self) -> None:
        self.store.save_ca_trust(
            WorkerCaTrust(server_origin=PAIR_ORIGIN, ca_path="/etc/ca.pem")
        )
        recorded: list[str] = []
        self._recording_ca_factory(recorded)
        _factory, pending = worker_client.resolve_run_trust(
            self.store, self._origin(), None
        )
        self.assertEqual(["/etc/ca.pem"], recorded)
        self.assertIsNone(pending)

    def test_stored_ca_for_another_origin_is_not_inherited(self) -> None:
        self.store.close()
        _plant_raw_trust_row(
            os.path.join(self._tmp.name, "s.db"),
            trust_to_json(
                WorkerCaTrust(server_origin=OTHER_ORIGIN, ca_path="/etc/ca.pem")
            ),
        )
        self.store = WorkerLocalStore(os.path.join(self._tmp.name, "s.db"))
        self.addCleanup(self.store.close)
        with unittest.mock.patch.object(
            worker_client,
            "ca_connect_factory",
            side_effect=AssertionError("must not build a CA factory"),
        ):
            factory, pending = worker_client.resolve_run_trust(
                self.store, self._origin(), None
            )
        self.assertIs(worker_client.default_connect_factory, factory)
        self.assertIsNone(pending)

    def test_explicit_ca_overrides_stored_and_pends_for_the_paired_origin(self):
        self.store.save_ca_trust(
            WorkerCaTrust(server_origin=PAIR_ORIGIN, ca_path="/etc/old.pem")
        )
        recorded: list[str] = []
        self._recording_ca_factory(recorded)
        _factory, pending = worker_client.resolve_run_trust(
            self.store, self._origin(), "/etc/new.pem"
        )
        self.assertEqual(["/etc/new.pem"], recorded)
        assert pending is not None
        self.assertEqual(PAIR_ORIGIN, pending.server_origin)
        self.assertEqual("/etc/new.pem", pending.ca_path)

    def test_ephemeral_server_override_never_pends_trust(self) -> None:
        # The explicit CA applies to THIS process's connections only; the
        # stored trust/identity for the paired origin is not rebound.
        _factory, pending = worker_client.resolve_run_trust(
            self.store, self._origin(OTHER_ORIGIN), "/etc/new.pem"
        )
        self.assertIsNone(pending)
        self.assertIsNone(effective_stored_ca_path(self.store, OTHER_ORIGIN))

    def test_malformed_stored_record_fails_closed(self) -> None:
        db = os.path.join(self._tmp.name, "s.db")
        self.store.close()
        _plant_raw_trust_row(db, '{"schema_version":1}')
        reopened = WorkerLocalStore(db)
        self.addCleanup(reopened.close)
        with self.assertRaises(WorkerCaTrustError):
            _ = worker_client.resolve_run_trust(reopened, self._origin(), None)

    def test_explicit_ca_can_repair_malformed_record_after_authentication(self) -> None:
        db = os.path.join(self._tmp.name, "s.db")
        self.store.close()
        _plant_raw_trust_row(db, "{malformed")
        reopened = WorkerLocalStore(db)
        self.addCleanup(reopened.close)
        recorded: list[str] = []
        self._recording_ca_factory(recorded)
        _factory, pending = worker_client.resolve_run_trust(
            reopened, self._origin(), "/etc/new.pem"
        )
        self.assertEqual(["/etc/new.pem"], recorded)
        assert pending is not None
        self.assertEqual(PAIR_ORIGIN, pending.server_origin)


# ── CLI wiring ────────────────────────────────────────────────────────────────


class CliCaFlagTests(unittest.TestCase):
    """``--ca-cert`` on pair/run: validation first, safe failures."""

    tls: TlsMaterials

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.tls = cast(TlsMaterials, object())

    @override
    def setUp(self) -> None:
        self.tls = TlsMaterials()
        self.addCleanup(self.tls.cleanup)

    def test_pair_with_invalid_ca_fails_before_network_and_state(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory[str]() as tmp:
            with (
                unittest.mock.patch.object(
                    worker_client,
                    "ca_connect_factory",
                    side_effect=AssertionError("no factory for an invalid file"),
                ),
                redirect_stderr(stderr),
            ):
                code = worker_client.main(
                    [
                        "pair",
                        "--server",
                        PAIR_ORIGIN,
                        "--code",
                        SYNTHETIC_CODE,
                        "--ca-cert",
                        os.path.join(tmp, "absent.pem"),
                        "--state-dir",
                        tmp,
                    ]
                )
            self.assertEqual(2, code)
            message = stderr.getvalue()
            self.assertIn("--ca-cert", message)
            self.assertNotIn(tmp, message)
            self.assertNotIn("Traceback", message)
            store = WorkerLocalStore(os.path.join(tmp, "worker-state.db"))
            try:
                self.assertIsNone(store.load_identity())
                self.assertIsNone(store.load_ca_trust())
            finally:
                store.close()

    def test_empty_ca_flag_is_explicit_and_never_sends_pairing_code(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp, redirect_stderr(io.StringIO()) as stderr:
            with unittest.mock.patch.object(worker_client, "default_connect_factory", side_effect=AssertionError("network must not run")):
                code = worker_client.main([
                    "pair", "--server", PAIR_ORIGIN, "--code", SYNTHETIC_CODE,
                    "--ca-cert=", "--state-dir", tmp,
                ])
            self.assertEqual(2, code)
            self.assertIn("--ca-cert", stderr.getvalue())
            store = WorkerLocalStore(os.path.join(tmp, "worker-state.db"))
            try:
                self.assertIsNone(store.load_identity())
                self.assertIsNone(store.load_ca_trust())
            finally:
                store.close()

    def test_empty_run_ca_flag_fails_before_state_lock(self) -> None:
        with tempfile.TemporaryDirectory[str]() as tmp, redirect_stderr(io.StringIO()) as stderr:
            store = WorkerLocalStore(os.path.join(tmp, "worker-state.db"))
            store.save_identity(_identity())
            store.close()
            code = worker_client.main(["run", "--ca-cert=", "--state-dir", tmp])
            self.assertEqual(2, code)
            self.assertIn("--ca-cert", stderr.getvalue())
            self.assertFalse((Path(tmp) / "worker.lock").exists())

    def test_run_with_invalid_ca_fails_before_the_state_lock(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory[str]() as tmp:
            store = WorkerLocalStore(os.path.join(tmp, "worker-state.db"))
            store.save_identity(_identity())
            store.close()
            with redirect_stderr(stderr):
                code = worker_client.main(
                    [
                        "run",
                        "--ca-cert",
                        os.path.join(tmp, "absent.pem"),
                        "--state-dir",
                        tmp,
                    ]
                )
            self.assertEqual(2, code)
            self.assertIn("--ca-cert", stderr.getvalue())
            # Validation precedes lock acquisition: no lock file exists.
            self.assertFalse(
                (Path(tmp) / "worker.lock").exists(),
                "the state lock must not be taken for an invalid CA file",
            )

    def test_pair_flag_builds_the_ca_factory_and_persists_trust(self) -> None:
        stdout = io.StringIO()
        canonical = str(Path(os.path.realpath(str(self.tls.ca_file))))
        with tempfile.TemporaryDirectory[str]() as tmp:
            server = FakeSessionServer()
            recorded: list[str] = []

            def fake_ca_factory(ca_path: str) -> worker_client.ConnectFactory:
                recorded.append(ca_path)
                return lambda _origin: server.worker_side

            with (
                unittest.mock.patch.object(
                    worker_client, "ca_connect_factory", fake_ca_factory
                ),
                redirect_stdout(stdout),
            ):
                code = worker_client.main(
                    [
                        "pair",
                        "--server",
                        PAIR_ORIGIN,
                        "--code",
                        SYNTHETIC_CODE,
                        "--ca-cert",
                        str(self.tls.ca_file),
                        "--state-dir",
                        tmp,
                    ]
                )
            self.assertEqual(0, code)
            self.assertIn("paired as w-fake-pair", stdout.getvalue())
            self.assertEqual([canonical], recorded)
            store = WorkerLocalStore(os.path.join(tmp, "worker-state.db"))
            try:
                trust = store.load_ca_trust()
                assert trust is not None
                self.assertEqual(PAIR_ORIGIN, trust.server_origin)
                self.assertEqual(canonical, trust.ca_path)
                identity = store.load_identity()
                assert identity is not None
                self.assertEqual("w-fake-pair", identity.worker_id)
            finally:
                store.close()

    def test_run_without_ca_and_without_stored_trust_uses_the_default_factory(
        self,
    ) -> None:
        # The default wiring is untouched: no stored trust means the
        # runtime gets the plain default connect factory.
        with tempfile.TemporaryDirectory[str]() as tmp:
            store = WorkerLocalStore(os.path.join(tmp, "worker-state.db"))
            store.save_identity(_identity())
            store.close()

            class Stop(Exception):
                pass

            captured: dict[str, worker_client.ConnectFactory] = {}

            def stop_runtime(**kwargs: object) -> worker_client.WorkerRuntime:
                captured["factory"] = cast(
                    "worker_client.ConnectFactory", kwargs["connect_factory"]
                )
                raise Stop

            with unittest.mock.patch.object(worker_client, "WorkerRuntime", stop_runtime):
                with self.assertRaises(Stop):
                    _ = worker_client.main(["run", "--state-dir", tmp])
            self.assertIs(worker_client.default_connect_factory, captured["factory"])


# ── Real-TLS proofs (trustme loopback listeners, no public hosts) ────────────


class RealTlsCaTests(unittest.TestCase):
    """Pair/run over REAL verified TLS with an explicit private CA."""

    tls: TlsMaterials

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.tls = cast(TlsMaterials, object())

    @override
    def setUp(self) -> None:
        self.tls = TlsMaterials()
        self.addCleanup(self.tls.cleanup)

    def _tls_listener(
        self, *, cert: Path | None = None, key: Path | None = None
    ) -> tuple[WorkerIdentityStore, int]:
        from tests.gateway_fixtures import build_registry
        from scarcity_router.worker_endpoint import WorkerEndpoint, build_tls_context

        tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(tmp.cleanup)
        identity_store = WorkerIdentityStore(os.path.join(tmp.name, "ids.db"))
        self.addCleanup(identity_store.close)
        endpoint = WorkerEndpoint(
            identity_store=identity_store,
            registry=build_registry(),
            configured_owner=lambda _resource_id: None,
        )
        self.addCleanup(endpoint.close_all_sessions, note="test teardown")
        listener = endpoint.attach_listener(
            host="127.0.0.1",
            port=0,
            tls_context=build_tls_context(
                str(cert if cert is not None else self.tls.server_cert),
                str(key if key is not None else self.tls.server_key),
            ),
        )
        listener.serve_in_background()
        self.addCleanup(listener.shutdown)
        return identity_store, listener.bound_port

    def test_pair_over_real_tls_with_the_private_ca_succeeds(self) -> None:
        identity_store, port = self._tls_listener()
        code = identity_store.begin_pairing(label="ca-test")
        tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(tmp.cleanup)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = worker_client.main(
                [
                    "pair",
                    "--server",
                    f"srws://localhost:{port}",
                    "--code",
                    code.pairing_code,
                    "--ca-cert",
                    str(self.tls.ca_file),
                    "--state-dir",
                    tmp.name,
                ]
            )
        self.assertEqual(0, exit_code, stderr.getvalue())
        store = WorkerLocalStore(os.path.join(tmp.name, "worker-state.db"))
        try:
            trust = store.load_ca_trust()
            assert trust is not None
            self.assertEqual(f"srws://localhost:{port}", trust.server_origin)
            self.assertEqual(
                str(Path(os.path.realpath(str(self.tls.ca_file)))), trust.ca_path
            )
            identity = store.load_identity()
            assert identity is not None
            # The stored identity authenticates against the real server.
            _ = identity_store.authenticate(identity.worker_id, identity.credential)
        finally:
            store.close()

    def test_wrong_ca_is_refused_before_any_pair_frame(self) -> None:
        identity_store, port = self._tls_listener()
        code = identity_store.begin_pairing()
        tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(tmp.cleanup)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = worker_client.main(
                [
                    "pair",
                    "--server",
                    f"srws://localhost:{port}",
                    "--code",
                    code.pairing_code,
                    "--ca-cert",
                    str(self.tls.wrong_ca_file),
                    "--state-dir",
                    tmp.name,
                ]
            )
        self.assertEqual(2, exit_code)
        # TLS verification failed BEFORE the pairing frame: the one-time
        # code was never consumed...
        worker_id, _credential = identity_store.redeem_pairing_code(code.pairing_code)
        self.assertTrue(worker_id.startswith("w-"))
        # ...and nothing was persisted locally.
        store = WorkerLocalStore(os.path.join(tmp.name, "worker-state.db"))
        try:
            self.assertIsNone(store.load_identity())
            self.assertIsNone(store.load_ca_trust())
        finally:
            store.close()

    def test_wrong_hostname_is_refused_before_any_pair_frame(self) -> None:
        # The listener serves a certificate valid for other.example.org;
        # the worker connects as localhost and must fail the handshake.
        _identity_store, port = self._tls_listener(
            cert=self.tls.other_host_cert, key=self.tls.other_host_key
        )
        tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(tmp.cleanup)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = worker_client.main(
                [
                    "pair",
                    "--server",
                    f"srws://localhost:{port}",
                    "--code",
                    SYNTHETIC_CODE,
                    "--ca-cert",
                    str(self.tls.ca_file),
                    "--state-dir",
                    tmp.name,
                ]
            )
        self.assertEqual(2, exit_code)

    def test_runtime_run_with_ca_factory_over_real_tls(self) -> None:
        from tests.gateway_fixtures import build_registry
        from scarcity_router.worker_endpoint import WorkerEndpoint, build_tls_context
        from scarcity_router.worker_identity_store import WorkerIdentityStore

        tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(tmp.cleanup)
        identity_store = WorkerIdentityStore(os.path.join(tmp.name, "ids.db"))
        self.addCleanup(identity_store.close)
        endpoint = WorkerEndpoint(
            identity_store=identity_store,
            registry=build_registry(),
            configured_owner=lambda _resource_id: None,
        )
        self.addCleanup(endpoint.close_all_sessions, note="test teardown")
        listener = endpoint.attach_listener(
            host="127.0.0.1",
            port=0,
            tls_context=build_tls_context(
                str(self.tls.server_cert), str(self.tls.server_key)
            ),
        )
        listener.serve_in_background()
        self.addCleanup(listener.shutdown)
        port = listener.bound_port
        origin = worker_client.WorkerOrigin.parse(f"srws://localhost:{port}")

        pairing = identity_store.begin_pairing()
        worker_id, credential = identity_store.redeem_pairing_code(pairing.pairing_code)
        store = WorkerLocalStore(os.path.join(tmp.name, "worker-state.db"))
        self.addCleanup(store.close)
        store.save_identity(
            WorkerLocalIdentity(
                worker_id=worker_id,
                credential=credential,
                server_origin=worker_client.origin_key(origin),
            )
        )
        accepted = threading.Event()
        runtime = worker_client.WorkerRuntime(
            origin=origin,
            store=store,
            connect_factory=worker_client.ca_connect_factory(str(self.tls.ca_file)),
            session_accepted=accepted.set,
        )
        result: list[str] = []
        thread = threading.Thread(target=lambda: result.append(runtime.run()), daemon=True)
        thread.start()
        self.assertTrue(accepted.wait(timeout=10.0), "session was never accepted")
        runtime.request_stop()
        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["requested"], result)

    def test_ca_factory_revalidates_and_fails_closed_when_the_file_vanishes(self):
        # The CA file is REMOVED after the factory was built: the next
        # connect must refuse (fail closed), never ride a cached context.
        holder = tempfile.TemporaryDirectory[str]()
        self.addCleanup(holder.cleanup)
        copied = Path(holder.name) / "ca.pem"
        _ = copied.write_bytes(self.tls.ca_file.read_bytes())
        factory = worker_client.ca_connect_factory(str(copied))
        _ = copied.unlink()
        with self.assertRaises(WorkerCaTrustError):
            _ = factory(worker_client.WorkerOrigin.parse("srws://localhost:9"))


# ── Service lifecycle: stored trust, unit hygiene ────────────────────────────


class ServiceInstallTrustTests(unittest.TestCase):
    """Stored trust through the service lifecycle (recording tool fakes)."""

    _tmp: tempfile.TemporaryDirectory[str]
    world: ServiceWorld
    tls: TlsMaterials

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.world = cast(ServiceWorld, object())
        self.tls = cast(TlsMaterials, object())

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory[str]()
        self.addCleanup(self._tmp.cleanup)
        self.world = ServiceWorld(Path(self._tmp.name))
        self.tls = TlsMaterials()
        self.addCleanup(self.tls.cleanup)

    def _store_trust(self, ca_path: str) -> None:
        store = worker_client.open_worker_store(str(self.world.state_dir))
        try:
            store.save_ca_trust(
                WorkerCaTrust(server_origin=SERVICE_ORIGIN, ca_path=ca_path)
            )
        finally:
            store.close()

    def test_install_with_valid_stored_trust_writes_a_clean_unit(self) -> None:
        self._store_trust(str(self.tls.ca_file))
        code, _out, err = self.world.install()
        self.assertEqual(0, code, err)
        unit_text = self.world.unit_path.read_text(encoding="utf-8")
        # The unit consumes the trust through `run` + the store: no CA
        # material, path or environment hack appears in it.
        self.assertNotIn("BEGIN CERTIFICATE", unit_text)
        self.assertNotIn("SSL_CERT_FILE", unit_text)
        self.assertNotIn("--ca-cert", unit_text)
        self.assertNotIn(str(self.tls.ca_file), unit_text)

    def test_install_refuses_when_the_stored_ca_file_is_gone(self) -> None:
        self._store_trust(str(self.tls.ca_file))
        _ = self.tls.ca_file.unlink()
        code, _out, err = self.world.install()
        self.assertEqual(2, code)
        self.assertIn("--ca-cert", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_install_refuses_on_a_malformed_trust_record(self) -> None:
        db = str(self.world.state_dir / "worker-state.db")
        _plant_raw_trust_row(db, "{corrupt")
        code, _out, err = self.world.install()
        self.assertEqual(2, code)
        self.assertIn("CA trust record", err)
        self.assertFalse(self.world.unit_path.exists())
        self.assertEqual([], self.world.systemctl_calls)

    def test_install_without_stored_trust_is_unchanged(self) -> None:
        code, out, _err = self.world.install()
        self.assertEqual(0, code)
        self.assertIn("unit written", out)
        unit_text = self.world.unit_path.read_text(encoding="utf-8")
        self.assertNotIn("CERTIFICATE", unit_text)
        self.assertNotIn("SSL_CERT_FILE", unit_text)
        self.assertNotIn("--ca-cert", unit_text)

    def test_status_and_restart_never_touch_worker_state_files(self) -> None:
        from scarcity_router import worker_service

        self._store_trust(str(self.tls.ca_file))
        before = sorted(path.name for path in self.world.state_dir.rglob("*"))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status_code = worker_service.run_service_command(
                {"service_command": "status"},
                tools=self.world.tools(),
                unit_path=self.world.unit_path,
                env=self.world.environment,
                open_store=worker_client.open_worker_store,
                build_registry=worker_client.build_registry,
            )
            restart_code = worker_service.run_service_command(
                {"service_command": "restart"},
                tools=self.world.tools(),
                unit_path=self.world.unit_path,
                env=self.world.environment,
                open_store=worker_client.open_worker_store,
                build_registry=worker_client.build_registry,
            )
        self.assertEqual((0, 0), (status_code, restart_code))
        after = sorted(path.name for path in self.world.state_dir.rglob("*"))
        self.assertEqual(before, after)


# ── Setup / tray compatibility ────────────────────────────────────────────────


class SetupAndTrayCompatTests(unittest.TestCase):
    """GUI pairing keeps working beside stored trust; tray shares the stack."""

    def test_complete_setup_pairing_preserves_existing_trust(self) -> None:
        from scarcity_router import worker_setup

        with tempfile.TemporaryDirectory[str]() as tmp:
            store = WorkerLocalStore(os.path.join(tmp, "s.db"))
            self.addCleanup(store.close)
            store.save_identity(_identity(origin="srws://127.0.0.1:8790"))
            existing = WorkerCaTrust(
                server_origin="srws://127.0.0.1:8790", ca_path="/etc/ca.pem"
            )
            store.save_ca_trust(existing)
            server = FakeSessionServer()
            fields = worker_setup.SetupFields(
                server_url="srws://127.0.0.1:8790",
                pairing_code=SYNTHETIC_CODE,
            )
            outcome = worker_setup.complete_setup(
                fields, store=store, connect_factory=lambda _o: server.worker_side
            )
            self.assertTrue(outcome.ok, outcome.message)
            self.assertEqual(existing, store.load_ca_trust())
            self.assertIsNotNone(store.load_identity())

    def test_windows_tray_build_runtime_consumes_stored_trust(self) -> None:
        from scarcity_router import windows_tray

        with tempfile.TemporaryDirectory[str]() as tmp:
            store = WorkerLocalStore(os.path.join(tmp, "s.db"))
            self.addCleanup(store.close)
            store.save_identity(_identity())
            store.save_ca_trust(
                WorkerCaTrust(server_origin=PAIR_ORIGIN, ca_path="/etc/ca.pem")
            )
            recorded: list[str] = []

            def fake_ca_factory(ca_path: str) -> worker_client.ConnectFactory:
                recorded.append(ca_path)
                return worker_client.default_connect_factory

            origin = worker_client.WorkerOrigin.parse(PAIR_ORIGIN)
            with unittest.mock.patch.object(
                worker_client, "ca_connect_factory", fake_ca_factory
            ):
                runtime = windows_tray.build_runtime(
                    origin=origin,
                    store=store,
                    cli_arguments={},
                    state_dir=None,
                )
            self.assertEqual(["/etc/ca.pem"], recorded)
            self.assertIs(store, runtime.store)

    def test_windows_tray_build_runtime_without_trust_keeps_the_default(self):
        from scarcity_router import windows_tray

        with tempfile.TemporaryDirectory[str]() as tmp:
            store = WorkerLocalStore(os.path.join(tmp, "s.db"))
            self.addCleanup(store.close)
            store.save_identity(_identity())
            with unittest.mock.patch.object(
                windows_tray,
                "ca_connect_factory",
                side_effect=AssertionError("no CA factory without stored trust"),
            ):
                _ = windows_tray.build_runtime(
                    origin=worker_client.WorkerOrigin.parse(PAIR_ORIGIN),
                    store=store,
                    cli_arguments={},
                    state_dir=None,
                )

    def test_windows_tray_run_ca_flag_uses_the_shared_run_resolver(self) -> None:
        from scarcity_router import windows_tray

        tls = TlsMaterials()
        self.addCleanup(tls.cleanup)
        with tempfile.TemporaryDirectory[str]() as tmp:
            store = WorkerLocalStore(os.path.join(tmp, "s.db"))
            self.addCleanup(store.close)
            store.save_identity(_identity())
            captured: list[str | None] = []

            def fake_resolver(
                _store: WorkerLocalStore,
                _origin: worker_client.WorkerOrigin,
                explicit_ca: str | None,
            ) -> tuple[worker_client.ConnectFactory, WorkerCaTrust | None]:
                captured.append(explicit_ca)
                return worker_client.default_connect_factory, None

            with unittest.mock.patch.object(
                windows_tray, "resolve_run_trust", fake_resolver
            ):
                _ = windows_tray.build_runtime(
                    origin=worker_client.WorkerOrigin.parse(PAIR_ORIGIN),
                    store=store,
                    cli_arguments={"ca_cert": str(tls.ca_file)},
                    state_dir=None,
                )
            self.assertEqual(
                [str(Path(os.path.realpath(str(tls.ca_file))))], captured
            )

    def test_empty_ca_flag_is_refused_by_tray_and_packaged_pair(self) -> None:
        from scarcity_router import windows_tray

        with tempfile.TemporaryDirectory[str]() as tmp:
            store = WorkerLocalStore(os.path.join(tmp, "s.db"))
            self.addCleanup(store.close)
            store.save_identity(_identity())
            with unittest.mock.patch.object(windows_tray, "resolve_run_trust", side_effect=AssertionError("must not resolve")):
                with self.assertRaises(WorkerCaTrustError):
                    _ = windows_tray.build_runtime(
                        origin=worker_client.WorkerOrigin.parse(PAIR_ORIGIN), store=store,
                        cli_arguments={"ca_cert": ""}, state_dir=None,
                    )
            with (unittest.mock.patch.object(windows_tray, "_attach_parent_console", return_value=True),
                  unittest.mock.patch.object(windows_tray.worker_setup, "pair_worker", side_effect=AssertionError("must not pair")),
                  redirect_stderr(io.StringIO())):
                result = windows_tray._packaged_pair(  # pyright: ignore[reportPrivateUsage]
                    ["--server", PAIR_ORIGIN, "--code", SYNTHETIC_CODE, "--ca-cert=", "--state-dir", tmp],
                    connect_factory=None,
                )
            self.assertEqual(2, result)
            self.assertEqual(_identity(), store.load_identity())

    def test_describe_pairing_failure_maps_ca_errors_safely(self) -> None:
        from scarcity_router import worker_setup

        message = worker_setup.describe_pairing_failure(
            WorkerCaTrustError("--ca-cert: the file could not be read; check it")
        )
        self.assertIn("--ca-cert", message)


if __name__ == "__main__":
    _ = unittest.main()
