"""Control-API tests for the M09 server component (issue #94).

Covers: first-run state and forced onboarding; identity-class separation;
authenticated machine-interface control endpoints with exact v1 envelopes
(M08 remote-bridge parity through ``scarcity_router.remote`` itself);
provider/resource/alias/client/worker administration; hash-only key
storage; redaction of diagnostics and secret-free export; persistence
across restart; and the single configuration source of truth.

Deterministic: synthetic collectors, injected clock, loopback listener.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import unittest
from pathlib import Path
from typing import cast, override

from scarcity_router.control_api import ControlPlane
from scarcity_router.errors import RemoteBridgeError
from scarcity_router.machine_api import (
    ENVELOPE_SCHEMA_VERSION,
    invalid_request_payload,
)
from scarcity_router.remote import RemoteScarcityClient, RemoteServerConfig
from scarcity_router.server_config import ResourceConfig
from scarcity_router.status import collect_status

from tests.server_fixtures import (
    FAKE_ADMIN_PASSWORD,
    FAKE_PROVIDER_SECRET,
    T_EVAL,
    ServerHarness,
    synthetic_collectors,
)


def _csrf_headers(plane: ControlPlane, cookie: str) -> dict[str, str]:
    csrf = plane.csrf_token_for_cookie(cookie)
    assert csrf is not None
    return {"X-Scarcity-CSRF": csrf}


class FirstRunTests(ServerHarness):
    def test_no_admin_configured_forces_onboarding(self) -> None:
        status, _payload, _headers = self.exchange("GET", "/control/state")
        self.assertEqual(401, status)
        status, _payload, headers = self.exchange("GET", "/")
        self.assertEqual(303, status)
        locations = [value for name, value in headers if name.lower() == "location"]
        self.assertEqual(["/admin/onboarding"], locations)
        # The default configuration is the neutral one.
        self.assertEqual(
            {"schema_version": 1}, self.plane.configuration.to_document()
        )

    def test_bootstrap_sets_up_and_second_bootstrap_conflicts(self) -> None:
        status, _payload, headers = self.exchange(
            "POST",
            "/control/bootstrap/admin",
            {"password": FAKE_ADMIN_PASSWORD, "confirm": True},
        )
        self.assertEqual(200, status)
        cookies = [value for name, value in headers if name.lower() == "set-cookie"]
        self.assertTrue(cookies)
        status, _payload, _headers = self.exchange(
            "POST",
            "/control/bootstrap/admin",
            {"password": FAKE_ADMIN_PASSWORD, "confirm": True},
        )
        self.assertEqual(409, status)

    def test_bootstrap_rejects_weak_passwords(self) -> None:
        for password in ("short", "", None, "x" * 200, " padded-long-password-x "):
            with self.subTest(password=password):
                status, _payload, _headers = self.exchange(
                    "POST",
                    "/control/bootstrap/admin",
                    {"password": password, "confirm": True},
                )
                self.assertEqual(400, status)

    def test_bootstrap_requires_explicit_confirmation(self) -> None:
        status, _payload, _headers = self.exchange(
            "POST",
            "/control/bootstrap/admin",
            {"password": FAKE_ADMIN_PASSWORD},
        )
        self.assertEqual(400, status)

    def test_login_logout_cycle(self) -> None:
        self.onboard(login=False)
        status, _payload, _headers = self.exchange(
            "POST", "/control/session", {"password": "wrong-password-entirely"}
        )
        self.assertEqual(401, status)
        status, _payload, headers = self.exchange(
            "POST", "/control/session", {"password": FAKE_ADMIN_PASSWORD}
        )
        self.assertEqual(200, status)
        cookies = [value for name, value in headers if name.lower() == "set-cookie"]
        self.assertTrue(cookies)


class IdentitySeparationTests(ServerHarness):
    def test_admin_session_cannot_call_machine_endpoints(self) -> None:
        self.onboard(login=False)
        status, _payload, _headers = self.exchange("GET", "/v1/status")
        self.assertEqual(401, status)

    def test_client_key_cannot_call_administration(self) -> None:
        self.onboard()
        client_headers = {"Authorization": f"Bearer {self.client_key}"}
        # Present the client key WITHOUT the administrator session cookie.""
        for method, path in (
            ("GET", "/control/state"),
            ("POST", "/control/providers"),
            ("DELETE", "/control/clients/whatever"),
        ):
            with self.subTest(path=path):
                status, _payload, _headers = self.exchange(
                    method,
                    path,
                    {} if method == "POST" else None,
                    headers=client_headers,
                    with_session=False,
                )
                self.assertEqual(401, status, path)
        # HTML navigation redirects to the login page instead of ever
        # rendering administration content for a client key.
        for path in ("/admin", "/admin/diagnostics"):
            with self.subTest(path=path):
                status, _payload, headers = self.exchange(
                    "GET",
                    path,
                    None,
                    headers=client_headers,
                    with_session=False,
                )
                self.assertEqual(303, status, path)
                locations = [
                    value for name, value in headers if name.lower() == "location"
                ]
                self.assertEqual(["/admin/login"], locations)

    def test_unknown_control_paths_are_404_after_authentication(self) -> None:
        self.onboard()
        status, _payload, _headers = self.exchange("GET", "/control/nope")
        self.assertEqual(404, status)


class UnauthenticatedMutationTests(ServerHarness):
    def test_every_mutation_requires_a_session(self) -> None:
        self.onboard(login=False)
        for method, path, payload in (
            ("POST", "/control/providers", {"provider_id": "x"}),
            ("POST", "/control/resources", {}),
            ("PUT", "/control/aliases/anything", {"profile_id": "deep_coding"}),
            ("POST", "/control/clients", {"label": "x"}),
            ("DELETE", "/control/clients/client-a", None),
            ("POST", "/control/workers/pairing-codes", {"label": "x"}),
            ("DELETE", "/control/workers/worker-a", None),
            ("POST", "/control/resources/r1/enabled", {"enabled": True}),
            ("DELETE", "/control/session", None),
        ):
            with self.subTest(path=path):
                status, _payload, _headers = self.exchange(
                    method, path, payload, with_session=False
                )
                self.assertEqual(401, status, path)

    def test_session_mutations_without_csrf_are_forbidden(self) -> None:
        self.onboard(login=False)
        status, _payload, _headers = self.exchange(
            "POST", "/control/clients", {"label": "x"}
        )
        self.assertEqual(403, status)


class MachineEndpointTests(ServerHarness):
    def test_machine_endpoints_require_a_client_key(self) -> None:
        self.onboard(login=False)
        for path, method in (
            ("/v1/status", "GET"),
            ("/v1/select", "POST"),
            ("/v1/simulate", "POST"),
        ):
            status, _payload, _headers = self.exchange(
                method, path, {} if method == "POST" else None
            )
            self.assertEqual(401, status, path)

    def test_select_rejects_unknown_profile_with_frozen_payload(self) -> None:
        self.onboard()
        status, payload, _headers = self.exchange(
            "POST",
            "/v1/select",
            {"profile_id": "not-a-profile"},
            headers={"Authorization": f"Bearer {self.client_key}"},
        )
        self.assertEqual(400, status)
        self.assertEqual(invalid_request_payload(), payload)


class RemoteBridgeParityTests(ServerHarness):
    """remote.py against the REAL server: bearer auth + v1 envelopes."""

    cookie: str = ""

    def _client(self) -> RemoteScarcityClient:
        config = RemoteServerConfig(
            base_url=f"http://127.0.0.1:{self.port}",
            api_key=self.client_key,
        )
        return RemoteScarcityClient(config)

    def test_remote_status_matches_locally_collected_envelope(self) -> None:
        self.onboard()
        remote = self._client().status()
        self.assertEqual(ENVELOPE_SCHEMA_VERSION, remote["schema_version"])
        observation = collect_status(
            collectors=synthetic_collectors(), clock=lambda: T_EVAL
        )
        remote_snapshots = remote["snapshots"]
        assert isinstance(remote_snapshots, list)
        self.assertEqual(
            [snapshot.to_dict() for snapshot in observation.snapshots],
            remote_snapshots,
        )
        self.assertTrue(remote_snapshots)
        # The additive D-039 eligibility field rides along.
        self.assertIn("eligibility", remote)

    def test_remote_select_and_simulate_round_trip(self) -> None:
        self.onboard()
        client = self._client()
        decision = client.select({"profile_id": "deep_coding"})
        self.assertIn("decision", decision)
        self.assertIsNotNone(decision["decision"])
        result = client.simulate({"profile_id": "deep_coding", "overrides": {}})
        self.assertIn("result", result)

    def test_remote_select_surfaces_server_rejections(self) -> None:
        self.onboard()
        with self.assertRaises(RemoteBridgeError) as caught:
            _ = self._client().select({"profile_id": "no-such-profile"})
        self.assertIn("invalid request", str(caught.exception))

    def test_wrong_key_is_an_explicit_authentication_failure(self) -> None:
        self.onboard()
        config = RemoteServerConfig(
            base_url=f"http://127.0.0.1:{self.port}",
            api_key="sk-sr-completely-wrong-key",
        )
        with self.assertRaises(RemoteBridgeError) as caught:
            _ = RemoteScarcityClient(config).status()
        self.assertIn("authentication failed", str(caught.exception))

    def test_revoked_key_stops_working_immediately(self) -> None:
        self.onboard()
        client = self._client()
        _ = client.status()
        self.plane.service_revoke_client_key(self.client_id)
        with self.assertRaises(RemoteBridgeError) as caught:
            _ = client.status()
        self.assertIn("authentication failed", str(caught.exception))


class ClientKeyAdministrationTests(ServerHarness):
    def test_issued_key_is_hashed_in_the_store_and_shown_once(self) -> None:
        self.onboard(login=False)
        status, payload = self.admin_post("/control/clients", {"label": "openai sdk"})
        self.assertEqual(200, status)
        issued = cast("dict[str, object]", payload)
        api_key = cast(str, issued["api_key"])
        self.assertTrue(api_key.startswith("sk-sr-"))
        raw_store = self.plane.store.path.read_bytes()
        # The plaintext key never touches the disk; the hash does.
        self.assertNotIn(api_key.encode("utf-8"), raw_store)
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest().encode("utf-8")
        self.assertIn(digest, raw_store)

    def test_duplicate_client_id_conflicts(self) -> None:
        self.onboard()
        status, _payload = self.admin_post(
            "/control/clients", {"label": "dup", "client_id": "client-b"}
        )
        self.assertEqual(200, status)
        status, _payload = self.admin_post(
            "/control/clients", {"label": "dup", "client_id": "client-b"}
        )
        self.assertEqual(409, status)

    def test_revocation_is_listed_and_effective(self) -> None:
        self.onboard()
        status, _payload, _headers = self.exchange(
            "DELETE",
            f"/control/clients/{self.client_id}",
            headers=_csrf_headers(self.plane, self.cookie),
        )
        self.assertEqual(200, status)
        status, payload = self.admin_get("/control/clients")
        clients = cast("list[object]", cast("dict[str, object]", payload)["clients"])
        entry = cast("dict[str, object]", clients[0])
        self.assertIsNotNone(entry["revoked_at"])
        with self.assertRaises(RemoteBridgeError):
            _ = RemoteScarcityClient(
                RemoteServerConfig(
                    base_url=f"http://127.0.0.1:{self.port}",
                    api_key=self.client_key,
                )
            ).status()


class WorkerPairingTests(ServerHarness):
    """Pairing admin over the ONE pairing system: M09 issues, M05 redeems.

    The control API ISSUES the pairing code; REDEMPTION happens inside
    the M05 worker protocol's TLS handshake — there is no HTTP redeem
    endpoint. These tests drive the same in-process endpoint the
    composed server uses through an injected in-memory transport.
    """

    def _pair_worker_over_protocol(self, code: str) -> tuple[str, str]:
        import threading

        from scarcity_router.worker_protocol import PairResultMessage
        from tests.worker_fixtures import MemoryTransport, ScriptedWorker

        server_side, worker_side = MemoryTransport.pair()
        endpoint = self.plane.worker_endpoint
        session = endpoint.attach_transport(server_side)
        endpoint.register_attached(session)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        worker = ScriptedWorker(worker_side)
        result = worker.send_pair(code)
        assert isinstance(result, PairResultMessage), result
        identity_worker_id = result.worker_id
        credential = result.credential
        worker.transport.close()
        _ = thread.join(timeout=5)
        return identity_worker_id, credential

    def test_issue_then_protocol_pair_hello_and_revoke(self) -> None:
        self.onboard()
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "lab rig"}
        )
        self.assertEqual(200, status)
        pairing = cast("dict[str, object]", payload)
        code = cast(str, pairing["pairing_code"])
        # No worker id exists yet: identity is created at protocol pairing.
        self.assertNotIn("worker_id", pairing)
        # The code never reaches any disk in plaintext.
        from scarcity_router.worker_identity_store import default_worker_store_path

        raw_store = self.plane.store.path.read_bytes() + Path(
            default_worker_store_path(self.data_dir)
        ).read_bytes()
        self.assertNotIn(code.encode("utf-8"), raw_store)

        worker_id, credential = self._pair_worker_over_protocol(code)
        self.assertTrue(worker_id.startswith("w-"))
        # The M05 store now holds the identity; the workers view shows it.
        status, payload = self.admin_get("/control/workers")
        self.assertEqual(200, status)
        workers = cast("list[object]", cast("dict[str, object]", payload)["workers"])
        self.assertEqual(
            (worker_id,),
            tuple(
                cast("dict[str, object]", entry)["worker_id"]
                for entry in workers
            ),
        )

        # Revocation takes effect on the NEXT connection.
        status, _payload, _headers = self.exchange(
            "DELETE",
            f"/control/workers/{worker_id}",
            headers=_csrf_headers(self.plane, self.cookie),
        )
        self.assertEqual(200, status)
        import threading

        from scarcity_router.worker_protocol import ErrorMessage
        from tests.worker_fixtures import MemoryTransport, ScriptedWorker

        server_side, worker_side = MemoryTransport.pair()
        endpoint = self.plane.worker_endpoint
        session = endpoint.attach_transport(server_side)
        endpoint.register_attached(session)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        rejected = ScriptedWorker(worker_side)
        answer = rejected.send_hello(worker_id, credential)
        assert isinstance(answer, ErrorMessage), answer
        self.assertEqual("credential_revoked", answer.code)
        rejected.transport.close()
        _ = thread.join(timeout=5)

    def test_http_redeem_endpoint_is_retired(self) -> None:
        self.onboard()
        status, payload, _headers = self.exchange(
            "POST",
            "/control/worker-pairing/redeem",
            {"code": "0000-0000"},
            headers=_csrf_headers(self.plane, self.cookie),
        )
        self.assertEqual(404, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertIn("unknown control endpoint", str(error))

    def test_rotate_returns_a_new_credential_once(self) -> None:
        self.onboard()
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "rot"}
        )
        self.assertEqual(200, status)
        code = cast("dict[str, object]", payload)["pairing_code"]
        worker_id, credential = self._pair_worker_over_protocol(cast(str, code))
        status, payload = self.admin_post(f"/control/workers/{worker_id}/rotate", {})
        self.assertEqual(200, status)
        rotated = cast("dict[str, object]", payload)
        new_credential = cast(str, rotated["worker_credential"])
        self.assertNotEqual(credential, new_credential)
        status, _payload = self.admin_post("/control/workers/no-such/rotate", {})
        self.assertEqual(404, status)

    def test_pairing_label_validation_is_a_client_error(self) -> None:
        self.onboard()
        # A label beyond the store's bounded length is a 400 with a safe
        # structural message, never a bare 500 (the store's ValueError).
        status, payload = self.admin_post(
            "/control/workers/pairing-codes", {"label": "x" * 500}
        )
        self.assertEqual(400, status)
        error = cast("dict[str, object]", payload)["error"]
        self.assertEqual("invalid_request", cast("dict[str, object]", error)["code"])

    def test_malformed_worker_id_is_not_found_never_a_500(self) -> None:
        self.onboard()
        # "Bad.Id" is a URL-safe path segment that FAILS the safe-id
        # grammar (uppercase): it must answer 404, never a bare 500 from
        # the store's ValueError.
        for method, path in (
            ("POST", "/control/workers/Bad.Id/rotate"),
            ("DELETE", "/control/workers/Bad.Id"),
        ):
            with self.subTest(path=path):
                status, _payload, _headers = self.exchange(
                    method,
                    path,
                    {} if method == "POST" else None,
                    headers=_csrf_headers(self.plane, self.cookie),
                )
                self.assertEqual(404, status, path)


def _provider_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "provider_id": "zai-http",
        "adapter_id": "zai-coding-plan",
        "base_url": "https://api.z.ai",
    }
    document.update(overrides)
    return document


def _resource_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "registration": {
            "identity": {
                "resource_id": "zai-plan-1",
                "channel": "server_direct_http",
                "provider": "zai",
                "model": "glm-5",
                "entitlement": "subscription_included",
            },
            "freshness_ttl_seconds": 3600,
        },
        "endpoint_id": "zai-http",
    }
    document.update(overrides)
    return document


class ProviderAndResourceTests(ServerHarness):
    def test_provider_add_with_secret_then_list_without_it(self) -> None:
        self.onboard()
        status, _payload = self.admin_post(
            "/control/providers",
            _provider_document(secret=FAKE_PROVIDER_SECRET),
        )
        self.assertEqual(200, status)
        status, payload = self.admin_get("/control/providers")
        self.assertEqual(200, status)
        providers = cast("list[object]", cast("dict[str, object]", payload)["providers"])
        self.assertEqual(1, len(providers))
        document = cast("dict[str, object]", providers[0])
        self.assertTrue(document["credential_configured"])
        self.assertNotIn(FAKE_PROVIDER_SECRET, str(payload))

    def test_provider_router_loop_refused(self) -> None:
        self.onboard()
        status, _payload = self.admin_post(
            "/control/providers",
            _provider_document(
                provider_id="self-loop", base_url="http://127.0.0.1:8787"
            ),
        )
        self.assertEqual(400, status)

    def test_plain_http_provider_off_loopback_refused(self) -> None:
        self.onboard()
        status, _payload = self.admin_post(
            "/control/providers",
            _provider_document(
                provider_id="insecure", base_url="http://api.insecure.example"
            ),
        )
        self.assertEqual(400, status)

    def test_resource_crud_and_enable_disable(self) -> None:
        self.onboard()
        status, _payload = self.admin_post(
            "/control/providers", _provider_document()
        )
        self.assertEqual(200, status)
        status, _payload = self.admin_post("/control/resources", _resource_document())
        self.assertEqual(200, status)
        snapshot = self.plane.current_application().registry.registry_snapshot()
        self.assertEqual(
            ("zai-plan-1",), tuple(entry.identity.resource_id for entry in snapshot.entries)
        )
        status, _payload = self.admin_post(
            "/control/resources/zai-plan-1/enabled", {"enabled": False}
        )
        self.assertEqual(200, status)
        snapshot = self.plane.current_application().registry.registry_snapshot()
        self.assertEqual((), tuple(snapshot.entries))
        status, payload = self.admin_get("/control/resources")
        resources = cast("list[object]", cast("dict[str, object]", payload)["resources"])
        entry = cast("dict[str, object]", resources[0])
        self.assertFalse(entry["enabled"])
        self.assertIsNotNone(entry["ladder"])
        status, _payload, _headers = self.exchange(
            "DELETE",
            "/control/resources/zai-plan-1",
            headers=_csrf_headers(self.plane, self.cookie),
        )
        self.assertEqual(200, status)
        status, payload = self.admin_get("/control/resources")
        self.assertEqual(
            [], cast("list[object]", cast("dict[str, object]", payload)["resources"])
        )

    def test_resource_with_unknown_endpoint_is_rejected(self) -> None:
        self.onboard()
        status, _payload = self.admin_post(
            "/control/resources", _resource_document(endpoint_id="missing-endpoint")
        )
        self.assertEqual(400, status)

    def test_resource_duplicate_conflicts(self) -> None:
        self.onboard()
        _status, _first = self.admin_post("/control/providers", _provider_document())
        status, _payload = self.admin_post("/control/resources", _resource_document())
        self.assertEqual(200, status)
        status, _payload = self.admin_post("/control/resources", _resource_document())
        self.assertEqual(409, status)


class AliasTests(ServerHarness):
    def test_alias_crud_reflects_in_models_listing(self) -> None:
        self.onboard()
        csrf = _csrf_headers(self.plane, self.cookie)
        status, _payload, _headers = self.exchange(
            "PUT",
            "/control/aliases/deep-coding",
            {"profile_id": "deep_coding"},
            headers=csrf,
        )
        self.assertEqual(200, status)
        status, _payload, _headers = self.exchange(
            "PUT",
            "/control/aliases/broken",
            {"profile_id": "no-such-profile"},
            headers=csrf,
        )
        self.assertEqual(400, status)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(
                "GET",
                "/v1/models",
                headers={"Authorization": f"Bearer {self.client_key}"},
            )
            response = connection.getresponse()
            listing = cast(object, json.loads(response.read()))
        finally:
            connection.close()
        document = cast("dict[str, object]", listing)
        entries = cast("list[dict[str, object]]", document["data"])
        self.assertEqual(["deep-coding"], [entry["id"] for entry in entries])
        status, _payload, _headers = self.exchange(
            "DELETE", "/control/aliases/deep-coding", headers=csrf
        )
        self.assertEqual(200, status)


class ConnectionAndGenerationTests(ServerHarness):
    def _configure_resource(self, *, with_secret: bool) -> None:
        document = _provider_document()
        if with_secret:
            document["secret"] = FAKE_PROVIDER_SECRET
        status, _payload = self.admin_post("/control/providers", document)
        assert status == 200
        status, _payload = self.admin_post("/control/resources", _resource_document())
        assert status == 200

    def test_connection_test_fails_with_remediation_and_no_quota(self) -> None:
        self.onboard()
        self._configure_resource(with_secret=False)
        status, payload = self.admin_post(
            "/control/resources/zai-plan-1/connection-test", {}
        )
        self.assertEqual(200, status)
        document = cast("dict[str, object]", payload)
        self.assertEqual("failed", document["result"])
        self.assertIs(False, document["quota_consumed"])
        checks = cast("list[object]", document["checks"])
        remediations = [
            cast("dict[str, object]", check).get("remediation")
            for check in checks
            if isinstance(check, dict)
            and cast("dict[str, object]", check).get("passed") is not True
        ]
        self.assertTrue(any(remediations))

    def test_connection_test_passes_when_configured(self) -> None:
        self.onboard()
        self._configure_resource(with_secret=True)
        status, payload = self.admin_post(
            "/control/resources/zai-plan-1/connection-test", {}
        )
        self.assertEqual(200, status)
        self.assertEqual("passed", cast("dict[str, object]", payload)["result"])

    def test_generation_test_requires_confirmation_then_refuses_without_adapter(
        self,
    ) -> None:
        self.onboard()
        self._configure_resource(with_secret=True)
        status, _payload = self.admin_post(
            "/control/resources/zai-plan-1/generation-test", {}
        )
        self.assertEqual(400, status)
        status, _payload = self.admin_post(
            "/control/resources/zai-plan-1/generation-test", {"confirm": True}
        )
        self.assertEqual(409, status)


class GenerationTestWithTesterTests(ServerHarness):
    """The explicit generation-test path with an injected M04-style tester."""

    calls: list[str] = []

    @override
    def make_plane(
        self,
        data_dir: Path,
        *,
        collectors: object = None,
        generation_tester: object = None,
    ) -> ControlPlane:
        calls: list[str] = []

        def tester(resource: ResourceConfig) -> dict[str, object]:
            calls.append(resource.registration.identity.resource_id)
            return {"status": "completed", "resource_id": calls[-1]}

        self.calls = calls
        return super().make_plane(data_dir, generation_tester=tester)

    def test_confirmed_generation_test_runs_through_the_tester(self) -> None:
        self.onboard()
        status, _payload = self.admin_post("/control/providers", _provider_document())
        self.assertEqual(200, status)
        status, _payload = self.admin_post("/control/resources", _resource_document())
        self.assertEqual(200, status)
        status, payload = self.admin_post(
            "/control/resources/zai-plan-1/generation-test", {"confirm": True}
        )
        self.assertEqual(200, status)
        self.assertEqual(["zai-plan-1"], self.calls)
        self.assertEqual(
            "completed", cast("dict[str, object]", payload)["status"]
        )


class DiagnosticsRedactionAndExportTests(ServerHarness):
    def test_diagnostics_and_export_never_leak_secrets(self) -> None:
        self.onboard()
        _ = self.admin_post(
            "/control/providers",
            _provider_document(secret=FAKE_PROVIDER_SECRET),
        )
        for path in (
            "/control/diagnostics",
            "/control/export",
            "/control/state",
            "/control/providers",
            "/admin/diagnostics",
            "/admin/providers",
        ):
            status, payload, _headers = self.exchange("GET", path)
            self.assertEqual(200, status, path)
            self.assertNotIn(FAKE_PROVIDER_SECRET, str(payload), path)
            self.assertNotIn(self.client_key, str(payload), path)
        status, payload = self.admin_get("/control/export")
        document = cast("dict[str, object]", payload)
        self.assertIn("secret_policy", document)
        self.assertNotIn(FAKE_PROVIDER_SECRET, str(document))

    def test_diagnostics_reports_resource_ladder_states(self) -> None:
        self.onboard()
        _ = self.admin_post(
            "/control/providers", _provider_document(secret=FAKE_PROVIDER_SECRET)
        )
        _ = self.admin_post("/control/resources", _resource_document())
        status, payload = self.admin_get("/control/diagnostics")
        self.assertEqual(200, status)
        document = cast("dict[str, object]", payload)
        resources = cast("list[object]", document["resources"])
        ladder = cast("dict[str, object]", resources[0])
        self.assertTrue(ladder["detected"])
        self.assertTrue(ladder["authenticated"])
        # The server_direct_http adapter is composed from configuration
        # (provider preset + stored credential), so the ladder reflects
        # that the channel is runnable — then honestly stops at
        # "available": no observation exists yet.
        self.assertTrue(ladder["protocol_compatible"])
        self.assertFalse(ladder["available"])
        self.assertEqual("available", ladder["first_blocked_stage"])
        self.assertIsNotNone(ladder["remediation"])

    def test_export_matches_the_single_stored_configuration(self) -> None:
        """Exactly one source of truth: export == stored document."""
        self.onboard()
        _ = self.admin_post("/control/providers", _provider_document())
        _status, export = self.admin_get("/control/export")
        configuration = cast("dict[str, object]", export)["configuration"]
        self.assertEqual(
            configuration, self.plane.store.load_configuration_document()
        )


class PersistenceRestartTests(ServerHarness):
    def test_state_survives_a_full_reopen(self) -> None:
        self.onboard()
        _ = self.admin_post(
            "/control/providers",
            _provider_document(secret=FAKE_PROVIDER_SECRET),
        )
        status, _payload, _headers = self.exchange(
            "PUT",
            "/control/aliases/deep-coding",
            {"profile_id": "deep_coding"},
            headers=_csrf_headers(self.plane, self.cookie),
        )
        self.assertEqual(200, status)
        data_dir = self.data_dir
        store = self.plane.store
        self.server.shutdown()
        self.server.server_close()
        _ = self.thread.join(timeout=10)
        store.close()
        # "Restart": a fresh control plane over the same store.
        revived = self.make_plane(data_dir)
        try:
            self.assertTrue(revived.admin_configured())
            self.assertEqual(
                ("zai-http",),
                tuple(
                    provider.provider_id
                    for provider in revived.configuration.providers
                ),
            )
            self.assertEqual(("deep-coding",), tuple(revived.configuration.aliases))
            self.assertEqual(
                (self.client_id,), tuple(revived.store.active_client_key_hashes())
            )
            self.assertEqual(1, revived.state_document()["providers"])
        finally:
            revived.store.close()


if __name__ == "__main__":
    _ = unittest.main()
