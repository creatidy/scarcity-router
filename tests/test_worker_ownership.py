"""Resource→worker ownership enforcement tests (D-049 amendment).

The frozen invariant under correction: a worker may observe and execute
a ``worker_bridged`` resource ONLY when current administrator
configuration assigns that exact resource id to that exact authenticated
worker identity. State reports prove liveness/observations; they can
never establish or change ownership. Every scenario below drives public
APIs only (the M09 control API, the in-process M05 worker endpoint, the
worker protocol) and is old-shape runnable — on the pre-correction shape
the ownership scenarios FAIL (the old shape accepts foreign reports and
dispatches to any reporting worker) instead of erroring.

Deterministic: in-memory transports, injected clocks in the harness,
conspicuous synthetic secrets only.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import cast

from scarcity_router.gateway_adapters import AdapterCall
from scarcity_router.resource_state import ResourceIdentity
from scarcity_router.selection_types import ModelIdentity
from scarcity_router.worker_endpoint import (
    AttemptOutcome,
    PendingAttempt,
    WorkerDispatchError,
    WorkerSession,
)
from scarcity_router.worker_protocol import (
    ErrorMessage,
    ExecuteMessage,
    PairResultMessage,
    StateReportAckMessage,
)

from tests.server_fixtures import ServerHarness
from tests.worker_fixtures import (
    MemoryTransport,
    ScriptedWorker,
    build_worker_report,
)

RESOURCE_ID = "owned-resource"
ADAPTER_ID = "synthetic"


def _ownership_resource_document(
    *,
    worker_id: str | None,
    local_adapter_id: str | None = "synthetic",
    resource_id: str = RESOURCE_ID,
) -> dict[str, object]:
    """A worker_bridged resource document; None binding keys are omitted."""
    registration: dict[str, object] = {
        "identity": {
            "resource_id": resource_id,
            "channel": "worker_bridged",
            "provider": "synthetic",
            "model": "syn-model",
            "entitlement": "local_ungated",
        },
        "freshness_ttl_seconds": 3600,
        "capabilities": {"context_limit_tokens": 272_000},
    }
    document: dict[str, object] = {"registration": registration, "enabled": True}
    if worker_id is not None:
        document["worker_id"] = worker_id
    if local_adapter_id is not None:
        document["local_adapter_id"] = local_adapter_id
    return document


class OwnershipWorld(ServerHarness):
    """Harness helpers: paired workers, admin config, protocol reports."""

    def _csrf(self) -> dict[str, str]:
        token = self.plane.csrf_token_for_cookie(self.cookie)
        assert token is not None
        return {"X-Scarcity-CSRF": token}

    def configure_resource(
        self,
        *,
        worker_id: str | None,
        local_adapter_id: str | None = "synthetic",
        resource_id: str = RESOURCE_ID,
    ) -> None:
        """The administrator's authoritative assignment (add or replace)."""
        if self.plane.configuration.resource_by_id(resource_id) is not None:
            status, _payload, _headers = self.exchange(
                "DELETE",
                f"/control/resources/{resource_id}",
                headers=self._csrf(),
            )
            assert status == 200
        document = _ownership_resource_document(
            worker_id=worker_id,
            local_adapter_id=local_adapter_id,
            resource_id=resource_id,
        )
        status, payload = self.admin_post("/control/resources", document)
        assert status == 200, payload

    def set_enabled(self, resource_id: str, enabled: bool) -> None:
        status, _payload = self.admin_post(
            f"/control/resources/{resource_id}/enabled", {"enabled": enabled}
        )
        assert status == 200

    def issue_pairing_code(self) -> str:
        """The administrator issues one pairing code (control API)."""
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "ownership-test"}
        )
        assert status == 200, payload
        return cast(str, cast("dict[str, object]", payload)["pairing_code"])

    def connect_and_pair(self) -> tuple[ScriptedWorker, str, str]:
        """One paired worker over an in-memory transport (protocol redeem)."""
        server_side, worker_side = MemoryTransport.pair()
        endpoint = self.plane.worker_endpoint
        session = endpoint.attach_transport(server_side)
        endpoint.register_attached(session)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(worker_side.close)
        worker = ScriptedWorker(worker_side)
        answer = worker.send_pair(self.issue_pairing_code())
        assert isinstance(answer, PairResultMessage), answer
        return worker, answer.worker_id, answer.credential

    def report(
        self,
        worker: ScriptedWorker,
        worker_id: str,
        resource_id: str = RESOURCE_ID,
    ) -> object:
        return worker.send_state_report(
            build_worker_report(worker_id=worker_id, resource_id=resource_id)
        )

    def dispatch_session(self, resource_id: str = RESOURCE_ID) -> WorkerSession:
        return self.plane.worker_endpoint.session_for_resource(resource_id)

    def dispatch_execute(
        self,
        resource_id: str = RESOURCE_ID,
        *,
        attempt_id: str = "wa-own000001",
    ) -> PendingAttempt:
        call = AdapterCall(
            resource=ResourceIdentity(
                resource_id=resource_id,
                channel="worker_bridged",
                provider="synthetic",
                model="syn-model",
                entitlement="local_ungated",
            ),
            model=ModelIdentity(provider="openai", model="syn-model", variant="max"),
            messages=(),
        )
        message = ExecuteMessage(
            request_id="chatcmpl-own-1",
            attempt_id=attempt_id,
            adapter_id=ADAPTER_ID,
            deadline="2030-01-01T00:00:00.000Z",
            call=call,
        )
        return self.plane.worker_endpoint.submit_execute_for_resource(
            message, resource_id
        )

    def observed_entry(self, resource_id: str = RESOURCE_ID) -> object:
        """The registry's observation slot for one resource (or None)."""
        snapshot = self.plane.current_application().registry.registry_snapshot()
        for entry in snapshot.entries:
            if entry.identity.resource_id == resource_id:
                return entry.observation
        return None


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class ConfiguredOwnerTests(OwnershipWorld):
    """Scenario 1: the configured owner reports and executes."""

    def test_configured_owner_reports_and_executes(self) -> None:
        self.onboard()
        worker_a, worker_a_id, _cred_a = self.connect_and_pair()
        self.configure_resource(worker_id=worker_a_id)
        answer = self.report(worker_a, worker_a_id)
        assert isinstance(answer, StateReportAckMessage), answer
        # The owner's own report is the availability evidence...
        self.assertIsNotNone(self.observed_entry())
        # ...and dispatch goes to exactly the configured owner, end to end.
        pending = self.dispatch_execute()
        execute = worker_a.next_execute()
        self.assertEqual("wa-own000001", execute.attempt_id)
        worker_a.complete(execute, stream=False)
        kind, payload = pending.take(5.0)
        self.assertEqual("outcome", kind)
        outcome = cast(AttemptOutcome, payload)
        self.assertEqual("completed", outcome.status)


class ForeignReportTests(OwnershipWorld):
    """Scenarios 2-4: telemetry can never claim, overwrite, or acquire."""

    def test_wrong_worker_cannot_claim_a_configured_resource(self) -> None:
        self.onboard()
        _worker_a, worker_a_id, _cred_a = self.connect_and_pair()
        worker_b, worker_b_id, _cred_b = self.connect_and_pair()
        self.configure_resource(worker_id=worker_a_id)
        # B reports a perfectly valid snapshot for a resource configured
        # to A: the ENTIRE report is rejected, atomically.
        answer = self.report(worker_b, worker_b_id)
        assert isinstance(answer, ErrorMessage), answer
        self.assertFalse(answer.fatal)
        # Registry observations changed nothing...
        self.assertIsNone(self.observed_entry())
        # ...and nothing is executable (the owner never reported; B's
        # telemetry must not have acquired authority).
        with self.assertRaises(WorkerDispatchError):
            _ = self.dispatch_execute()
        self.assertEqual(0, worker_b.received_execute_count)

    def test_wrong_worker_cannot_overwrite_the_owner(self) -> None:
        self.onboard()
        worker_a, worker_a_id, _cred_a = self.connect_and_pair()
        worker_b, worker_b_id, _cred_b = self.connect_and_pair()
        self.configure_resource(worker_id=worker_a_id)
        answer = self.report(worker_a, worker_a_id)
        assert isinstance(answer, StateReportAckMessage), answer
        # B then reports the same resource: rejected; the authoritative
        # session is unchanged and a dispatch still targets A.
        intruder_answer = self.report(worker_b, worker_b_id)
        assert isinstance(intruder_answer, ErrorMessage), intruder_answer
        session = self.dispatch_session()
        self.assertEqual(worker_a_id, session.worker_id)
        pending = self.dispatch_execute(attempt_id="wa-own000003")
        execute = worker_a.next_execute()
        self.assertEqual("wa-own000003", execute.attempt_id)
        worker_a.complete(execute, stream=False)
        _kind, _payload = pending.take(5.0)
        self.assertEqual(0, worker_b.received_execute_count)

    def test_unassigned_resource_is_not_claimable(self) -> None:
        self.onboard()
        worker_b, worker_b_id, _cred_b = self.connect_and_pair()
        # No worker_id: the resource has NO owner; nothing may acquire it.
        self.configure_resource(worker_id=None)
        answer = self.report(worker_b, worker_b_id)
        assert isinstance(answer, ErrorMessage), answer
        with self.assertRaises(WorkerDispatchError) as caught:
            _ = self.dispatch_execute()
        self.assertEqual("resource_unbound", caught.exception.code)
        self.assertEqual(0, worker_b.received_execute_count)


class ReassignmentTests(OwnershipWorld):
    """Scenario 5: configuration changes invalidate stale ownership."""

    def test_reassignment_moves_authority_only_through_configuration(self) -> None:
        self.onboard()
        worker_a, worker_a_id, _cred_a = self.connect_and_pair()
        worker_b, worker_b_id, _cred_b = self.connect_and_pair()
        self.configure_resource(worker_id=worker_a_id)
        answer = self.report(worker_a, worker_a_id)
        assert isinstance(answer, StateReportAckMessage), answer
        self.assertEqual(worker_a_id, self.dispatch_session().worker_id)
        # Reassign to B: A can no longer report the resource...
        self.configure_resource(worker_id=worker_b_id)
        stale_answer = self.report(worker_a, worker_a_id)
        assert isinstance(stale_answer, ErrorMessage), stale_answer
        # ...A's old observed binding does NOT authorize dispatch, and B
        # is not executable until its OWN valid report arrives (no
        # intermediate authority for anyone).
        with self.assertRaises(WorkerDispatchError):
            _ = self.dispatch_execute(attempt_id="wa-own000005")
        # B reports: exactly one authoritative worker, and it is B.
        own_answer = self.report(worker_b, worker_b_id)
        assert isinstance(own_answer, StateReportAckMessage), own_answer
        self.assertEqual(worker_b_id, self.dispatch_session().worker_id)
        # A stays out: reporting is still rejected afterwards.
        again = self.report(worker_a, worker_a_id)
        assert isinstance(again, ErrorMessage), again


class AuthorityRemovalTests(OwnershipWorld):
    """Scenario 6: disable / unassign removes execution authority."""

    def test_disable_removes_report_and_dispatch_authority(self) -> None:
        self.onboard()
        worker_a, worker_a_id, _cred_a = self.connect_and_pair()
        self.configure_resource(worker_id=worker_a_id)
        answer = self.report(worker_a, worker_a_id)
        assert isinstance(answer, StateReportAckMessage), answer
        self.set_enabled(RESOURCE_ID, False)
        stale_answer = self.report(worker_a, worker_a_id)
        assert isinstance(stale_answer, ErrorMessage), stale_answer
        with self.assertRaises(WorkerDispatchError):
            _ = self.dispatch_session()

    def test_unassign_does_not_leave_a_stale_authoritative_binding(self) -> None:
        self.onboard()
        worker_a, worker_a_id, _cred_a = self.connect_and_pair()
        self.configure_resource(worker_id=worker_a_id)
        answer = self.report(worker_a, worker_a_id)
        assert isinstance(answer, StateReportAckMessage), answer
        # Unassign: the configured map no longer names ANY worker; A's
        # previously valid report must not keep executing.
        self.configure_resource(worker_id=None)
        stale_answer = self.report(worker_a, worker_a_id)
        assert isinstance(stale_answer, ErrorMessage), stale_answer
        with self.assertRaises(WorkerDispatchError) as caught:
            _ = self.dispatch_execute()
        self.assertEqual("resource_unbound", caught.exception.code)


class NoFallbackTests(OwnershipWorld):
    """Scenario 7: an offline configured owner is an explicit failure."""

    def test_offline_owner_never_routes_to_another_worker(self) -> None:
        self.onboard()
        worker_a, worker_a_id, _cred_a = self.connect_and_pair()
        self.configure_resource(worker_id=worker_a_id)
        answer = self.report(worker_a, worker_a_id)
        assert isinstance(answer, StateReportAckMessage), answer
        # The configured owner goes offline: its session closes...
        worker_a.transport.close()
        deadline = _monotonic() + 5.0
        while worker_a_id in self.plane.worker_endpoint.connected_worker_ids():
            assert _monotonic() < deadline, "owner session did not close"
            _sleep(0.01)
        worker_b, worker_b_id, _cred_b = self.connect_and_pair()
        # Even a connected worker can never claim or receive the
        # resource: its report is rejected and the dispatch fails
        # explicitly on the offline owner — never a fallback.
        foreign_answer = self.report(worker_b, worker_b_id)
        assert isinstance(foreign_answer, ErrorMessage), foreign_answer
        with self.assertRaises(WorkerDispatchError) as caught:
            _ = self.dispatch_execute()
        self.assertEqual("worker_offline", caught.exception.code)
        self.assertEqual(0, worker_b.received_execute_count)


class CompositionHonestyTests(OwnershipWorld):
    """A worker_bridged resource missing either binding is non-dispatchable."""

    def test_missing_worker_id_is_not_dispatchable(self) -> None:
        self.onboard()
        self.configure_resource(worker_id=None)
        application = self.plane.current_application()
        self.assertNotIn(
            "worker_bridged", application.adapters.registered_channels()
        )

    def test_missing_local_adapter_id_is_not_dispatchable(self) -> None:
        self.onboard()
        _worker_a, worker_a_id, _cred_a = self.connect_and_pair()
        document = _ownership_resource_document(
            worker_id=worker_a_id, local_adapter_id=None
        )
        status, payload = self.admin_post("/control/resources", document)
        assert status == 200, payload
        application = self.plane.current_application()
        self.assertNotIn(
            "worker_bridged", application.adapters.registered_channels()
        )


if __name__ == "__main__":
    _ = unittest.main()
