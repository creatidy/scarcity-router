"""Worker identity/pairing store tests (M05): pairing, rotation, revocation.

Deterministic: injected clock, temporary directories, synthetic codes.
Also locks the reason-code alignment with the worker protocol vocabulary
and the permissioned-store posture (0o700 directory, 0o600 file).
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from typing import cast, override
from datetime import datetime, timedelta, timezone

from scarcity_router.worker_identity_store import (
    MAX_OUTSTANDING_PAIRING_CODES,
    WorkerAdminService,
    WorkerIdentityError,
    WorkerIdentityStore,
)
from scarcity_router.worker_protocol import (
    PROTOCOL_ERROR_CODES,
    ERR_CREDENTIAL_REVOKED,
    ERR_PAIRING_EXPIRED,
    ERR_PAIRING_INVALID,
    ERR_PAIRING_USED,
    ERR_UNAUTHORIZED,
)

STORE_ERR_UNAUTHORIZED = "unauthorized"


class _MutableClock:
    def __init__(self) -> None:
        self.now: datetime = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class PairingLifecycleTests(unittest.TestCase):
    _tmp: tempfile.TemporaryDirectory[str]
    clock: _MutableClock
    store: WorkerIdentityStore

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        # Placeholders; setUp replaces them before each test body runs.
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.clock = cast("_MutableClock", object())
        self.store = cast("WorkerIdentityStore", object())


    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = _MutableClock()
        self.store = WorkerIdentityStore(
            f"{self._tmp.name}/identity/identities.db", clock=self.clock
        )
        self.addCleanup(self.store.close)

    def test_initial_pairing_issues_working_identity(self) -> None:
        code = self.store.begin_pairing(label="laptop")
        worker_id, credential = self.store.redeem_pairing_code(code.pairing_code)
        self.assertTrue(worker_id.startswith("w-"))
        record = self.store.get_identity(worker_id)
        assert record is not None
        self.assertEqual("laptop", record.label)
        _ = self.store.authenticate(worker_id, credential)

    def test_one_time_code_reuse_is_rejected(self) -> None:
        code = self.store.begin_pairing()
        _ = self.store.redeem_pairing_code(code.pairing_code)
        with self.assertRaises(WorkerIdentityError) as caught:
            _ = self.store.redeem_pairing_code(code.pairing_code)
        self.assertEqual(ERR_PAIRING_USED, caught.exception.code)

    def test_concurrent_redemption_admits_exactly_one(self) -> None:
        code = self.store.begin_pairing()
        outcomes: list[str] = []
        lock = threading.Lock()

        def redeem() -> None:
            try:
                _ = self.store.redeem_pairing_code(code.pairing_code)
                result = "ok"
            except WorkerIdentityError as exc:
                result = exc.code
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=redeem) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, outcomes.count("ok"))
        self.assertEqual(3, outcomes.count(ERR_PAIRING_USED))

    def test_code_expiry_is_rejected(self) -> None:
        code = self.store.begin_pairing()
        self.clock.advance(seconds=601)
        with self.assertRaises(WorkerIdentityError) as caught:
            _ = self.store.redeem_pairing_code(code.pairing_code)
        self.assertEqual(ERR_PAIRING_EXPIRED, caught.exception.code)

    def test_unknown_code_is_invalid(self) -> None:
        with self.assertRaises(WorkerIdentityError) as caught:
            _ = self.store.redeem_pairing_code("SYNTHETIC-NOT-ISSUED")
        self.assertEqual(ERR_PAIRING_INVALID, caught.exception.code)

    def test_credentials_and_codes_survive_store_reopen(self) -> None:
        path = f"{self._tmp.name}/identity/identities.db"
        outstanding = self.store.begin_pairing(label="outstanding")
        code = self.store.begin_pairing(label="persist")
        worker_id, credential = self.store.redeem_pairing_code(code.pairing_code)
        _ = self.store.authenticate(worker_id, credential)
        self.store.close()
        # A server restart reopens the SAME database file: the pepper salt
        # must come back from store_meta so stored hashes stay verifiable
        # and outstanding admin-issued codes stay redeemable.
        reopened = WorkerIdentityStore(path, clock=self.clock)
        self.addCleanup(reopened.close)
        _ = reopened.authenticate(worker_id, credential)
        second_id, second_credential = reopened.redeem_pairing_code(
            outstanding.pairing_code
        )
        _ = reopened.authenticate(second_id, second_credential)
        # Still hashes only: the raw credential never gained a plaintext copy.
        with open(path, "rb") as handle:
            raw = handle.read()
        self.assertNotIn(credential.encode("utf-8"), raw)
        self.assertNotIn(second_credential.encode("utf-8"), raw)

    def test_corrupt_salt_row_fails_closed(self) -> None:
        path = f"{self._tmp.name}/identity/identities.db"
        code = self.store.begin_pairing()
        _ = self.store.redeem_pairing_code(code.pairing_code)
        self.store.close()
        import sqlite3

        connection = sqlite3.connect(path)
        try:
            _ = connection.execute(
                "UPDATE store_meta SET value = 'not-hex' WHERE key = 'salt'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(WorkerIdentityError):
            _ = WorkerIdentityStore(path, clock=self.clock)

    def test_pairing_backlog_is_bounded(self) -> None:
        for _ in range(MAX_OUTSTANDING_PAIRING_CODES):
            _ = self.store.begin_pairing()
        with self.assertRaises(WorkerIdentityError) as caught:
            _ = self.store.begin_pairing()
        self.assertEqual("pairing_unavailable", caught.exception.code)


class AuthenticationTests(unittest.TestCase):
    _tmp: tempfile.TemporaryDirectory[str]
    clock: _MutableClock
    store: WorkerIdentityStore
    worker_id: str
    credential: str

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        # Placeholders; setUp replaces them before each test body runs.
        self._tmp = cast("tempfile.TemporaryDirectory[str]", object())
        self.clock = cast("_MutableClock", object())
        self.store = cast("WorkerIdentityStore", object())
        self.worker_id = cast("str", object())
        self.credential = cast("str", object())


    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = _MutableClock()
        self.store = WorkerIdentityStore(
            f"{self._tmp.name}/identity/identities.db", clock=self.clock
        )
        self.addCleanup(self.store.close)
        code = self.store.begin_pairing()
        self.worker_id, self.credential = self.store.redeem_pairing_code(
            code.pairing_code
        )

    def test_unknown_identity_is_rejected(self) -> None:
        with self.assertRaises(WorkerIdentityError) as caught:
            self.store.authenticate("w-doesnotexist", self.credential)
        self.assertEqual(ERR_UNAUTHORIZED, caught.exception.code)

    def test_invalid_credential_is_rejected(self) -> None:
        with self.assertRaises(WorkerIdentityError) as caught:
            self.store.authenticate(self.worker_id, "SYNTHETIC-WRONG")
        self.assertEqual(ERR_UNAUTHORIZED, caught.exception.code)

    def test_rotation_invalidates_old_and_accepts_new(self) -> None:
        rotated = self.store.rotate_credential(self.worker_id)
        with self.assertRaises(WorkerIdentityError):
            _ = self.store.authenticate(self.worker_id, self.credential)
        _ = self.store.authenticate(self.worker_id, rotated)

    def test_rotation_of_unknown_identity_fails(self) -> None:
        with self.assertRaises(WorkerIdentityError):
            _ = self.store.rotate_credential("w-missing")

    def test_revocation_takes_effect_without_redeployment(self) -> None:
        self.store.revoke(self.worker_id)
        with self.assertRaises(WorkerIdentityError) as caught:
            _ = self.store.authenticate(self.worker_id, self.credential)
        self.assertEqual(ERR_CREDENTIAL_REVOKED, caught.exception.code)

    def test_revoked_identity_cannot_re_rotate(self) -> None:
        self.store.revoke(self.worker_id)
        with self.assertRaises(WorkerIdentityError):
            _ = self.store.rotate_credential(self.worker_id)

    def test_listing_is_safe_and_ordered(self) -> None:
        records = self.store.list_identities()
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual(self.worker_id, record.worker_id)
        self.assertEqual("active", record.status)
        # The credential value must never appear in any safe projection.
        self.assertNotIn(self.credential, str(record.to_dict()))


class ReasonCodeAlignmentTests(unittest.TestCase):
    def test_store_reason_codes_are_protocol_error_codes(self) -> None:
        for code in (
            ERR_UNAUTHORIZED,
            ERR_PAIRING_INVALID,
            ERR_PAIRING_EXPIRED,
            ERR_PAIRING_USED,
            ERR_CREDENTIAL_REVOKED,
        ):
            self.assertIn(code, PROTOCOL_ERROR_CODES)


class AdminServiceTests(unittest.TestCase):
    def test_admin_seam_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkerIdentityStore(f"{tmp}/identity/identities.db")
            service = WorkerAdminService(store)
            code = service.begin_pairing(label="tower")
            worker_id, credential = store.redeem_pairing_code(code.pairing_code)
            self.assertEqual((worker_id,), tuple(
                r.worker_id for r in service.list_workers()
            ))
            rotated = service.rotate_worker_credential(worker_id)
            with self.assertRaises(WorkerIdentityError):
                store.authenticate(worker_id, credential)
            _ = store.authenticate(worker_id, rotated)
            service.revoke_worker(worker_id)
            self.assertEqual("revoked", service.list_workers()[0].status)
            store.close()


class PermissionedStoreTests(unittest.TestCase):
    def test_store_directory_and_file_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/nested/deeper/identities.db"
            store = WorkerIdentityStore(path)
            self.addCleanup(store.close)
            parent_mode = os.stat(f"{tmp}/nested").st_mode & 0o777
            file_mode = os.stat(path).st_mode & 0o777
            self.assertEqual(0o700, parent_mode)
            self.assertEqual(0o600, file_mode)

    def test_pairing_code_hashed_not_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/identity/identities.db"
            store = WorkerIdentityStore(path)
            code = store.begin_pairing()
            _ = store.redeem_pairing_code(code.pairing_code)
            worker_id = store.list_identities()[0].worker_id
            store.close()
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertNotIn(code.pairing_code.encode("utf-8"), raw)
            self.assertNotIn(b"SYNTHETIC", raw)
            _ = worker_id


if __name__ == "__main__":
    _ = unittest.main()
