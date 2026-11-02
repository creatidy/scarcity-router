"""Web-UI flow tests for the M09 server component (issue #94).

Black-box HTTP tests of the server-rendered administration UI: forced
onboarding, login/logout, the provider/resource/alias/client/worker
flows, one-time secret reveals, CSRF enforcement and redaction.

Deterministic: synthetic collectors, injected clock, loopback listener.
"""

from __future__ import annotations

import http.client
import re
import unittest
import urllib.parse
from dataclasses import replace

from scarcity_router.model_inventory import DiscoveredModel, ModelInventoryReport, SourceInventory
from scarcity_router.server_ui import _source_guidance  # pyright: ignore[reportPrivateUsage]

from tests.server_fixtures import (
    FAKE_ADMIN_PASSWORD,
    FAKE_ADMIN_CLAIM,
    FAKE_PROVIDER_SECRET,
    ServerHarness,
    resource_document,
)


class _UiSession:
    """A cookie jar for one browser-like client."""

    def __init__(self) -> None:
        self.cookie: str | None = None

    def adopt(self, headers: list[tuple[str, str]]) -> None:
        for name, value in headers:
            if name.lower() == "set-cookie":
                self.cookie = value.split(";", 1)[0]


def _form(port: int, path: str, fields: dict[str, str], cookie: str | None) -> tuple[int, str, list[tuple[str, str]]]:
    if path == "/admin/onboarding":
        fields = {"claim": FAKE_ADMIN_CLAIM, **fields}
    body = urllib.parse.urlencode(fields).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if path == "/admin/onboarding":
        headers["Origin"] = f"http://127.0.0.1:{port}"
    if cookie:
        headers["Cookie"] = cookie
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        response_headers = response.getheaders()
        status = response.status
    finally:
        connection.close()
    return status, raw, response_headers


def _get(port: int, path: str, cookie: str | None) -> tuple[int, str, list[tuple[str, str]]]:
    headers = {"Cookie": cookie} if cookie else {}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        response_headers = response.getheaders()
        status = response.status
    finally:
        connection.close()
    return status, raw, response_headers


def _csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match is not None, "page did not embed a CSRF token"
    return match.group(1)


class OnboardingFlowTests(ServerHarness):
    def test_first_run_identifies_same_store_requirement(self) -> None:
        status, html, _headers = _get(self.port, "/admin/onboarding", None)
        self.assertEqual(200, status)
        self.assertIn("--data-dir /path/to/server-data", html)
        self.assertIn("different directory will not work", html)
        self.assertNotIn(str(self.data_dir), html)

    def test_cross_origin_onboarding_refused_without_session(self) -> None:
        body = urllib.parse.urlencode({"claim": FAKE_ADMIN_CLAIM,
            "password": FAKE_ADMIN_PASSWORD, "confirm": "yes"}).encode()
        status, _body, headers = self.exchange("POST", "/admin/onboarding", raw_body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://attacker.invalid"})
        self.assertEqual(403, status)
        self.assertFalse(any(name.lower() == "set-cookie" for name, _ in headers))
        self.assertFalse(self.plane.admin_configured())

    def test_first_run_renders_onboarding_then_dashboard(self) -> None:
        status, html, _headers = _get(self.port, "/", None)
        self.assertEqual(303, status)
        status, html, _headers = _get(self.port, "/admin/onboarding", None)
        self.assertEqual(200, status)
        self.assertIn("First-run setup", html)
        self.assertIn("type=\"password\"", html)
        # A weak password renders an error page, not a session.
        status, html, headers = _form(
            self.port,
            "/admin/onboarding",
            {"password": "weak", "confirm": "yes"},
            None,
        )
        self.assertEqual(400, status)
        self.assertFalse(
            any(name.lower() == "set-cookie" for name, _ in headers)
        )
        # The real onboarding sets the session and reaches the dashboard.
        status, _html, headers = _form(
            self.port,
            "/admin/onboarding",
            {"password": FAKE_ADMIN_PASSWORD, "confirm": "yes"},
            None,
        )
        self.assertEqual(303, status)
        session = _UiSession()
        session.adopt(headers)
        self.assertIsNotNone(session.cookie)
        status, html, _headers = _get(self.port, "/admin", session.cookie)
        self.assertEqual(200, status)
        self.assertIn("Overview", html)
        # Onboarding is closed afterwards; the page redirects to login.
        status, _html, headers = _get(self.port, "/admin/onboarding", None)
        self.assertEqual(303, status)

    def test_login_logout(self) -> None:
        self.onboard(login=False)
        status, html, _headers = _get(self.port, "/admin/login", None)
        self.assertEqual(200, status)
        status, _html, headers = _form(
            self.port, "/admin/login", {"password": FAKE_ADMIN_PASSWORD}, None
        )
        self.assertEqual(303, status)
        session = _UiSession()
        session.adopt(headers)
        status, html, _headers = _get(self.port, "/admin", session.cookie)
        self.assertEqual(200, status)
        csrf = _csrf_of(html)
        status, _html, headers = _form(
            self.port, "/admin/logout", {"csrf": csrf}, session.cookie
        )
        self.assertEqual(303, status)
        status, _html, headers = _get(self.port, "/admin", session.cookie)
        self.assertEqual(303, status)
        locations = [v for n, v in headers if n.lower() == "location"]
        self.assertEqual(["/admin/login"], locations)


class UiMutationFlowTests(ServerHarness):
    def _session(self) -> tuple[str, str]:
        self.onboard(login=False)
        status, _html, headers = _form(
            self.port, "/admin/login", {"password": FAKE_ADMIN_PASSWORD}, None
        )
        assert status == 303
        jar = _UiSession()
        jar.adopt(headers)
        cookie = jar.cookie
        assert cookie is not None
        status, html, _headers = _get(self.port, "/admin/providers", cookie)
        assert status == 200
        return cookie, _csrf_of(html)

    def test_worker_deletion_refusals_and_success(self) -> None:
        cookie, csrf = self._session()
        worker_id = self._pair_worker(cookie, csrf, "Worker")
        fields = {"csrf": csrf, "worker_id": worker_id}
        status, html, _ = _form(self.port, "/admin/workers/delete", fields, cookie)
        self.assertEqual(409, status)
        self.assertIn("Revoke", html)
        _ = self.plane.service_add_resource(resource_document(
            "worker-resource", channel="worker_bridged", endpoint_id=None, worker_id=worker_id,
        ))
        self.plane.service_revoke_worker(worker_id)
        status, html, _ = _get(self.port, "/admin/workers", cookie)
        self.assertEqual(200, status)
        self.assertNotIn('action="/admin/workers/delete"', html)
        status, html, _ = _form(self.port, "/admin/workers/delete", fields, cookie)
        self.assertEqual(409, status)
        self.assertIn("references", html)
        self.plane.service_remove_resource("worker-resource")
        self.plane.store.append_audit_record(
            payload={"worker_id": worker_id}, recorded_at="2026-09-16T12:00:00.000Z",
            max_records=100, max_age_seconds=86400,
        )
        audit_count = self.plane.store.audit_record_count()
        status, html, _ = _get(self.port, "/admin/workers", cookie)
        self.assertIn('action="/admin/workers/delete"', html)
        status, _, _ = _form(self.port, "/admin/workers/delete", {"worker_id": worker_id}, cookie)
        self.assertEqual(403, status)
        status, _, _ = _form(self.port, "/admin/workers/delete", fields, cookie)
        self.assertEqual(303, status)
        self.assertFalse(any(w["worker_id"] == worker_id for w in self.plane.workers_view()))
        self.assertEqual(audit_count, self.plane.store.audit_record_count())

    def test_resources_show_effective_limits(self) -> None:
        cookie, _ = self._session()
        status, html, _ = _get(self.port, "/admin/resources", cookie)
        self.assertEqual(200, status)
        self.assertIn("Execution limits", html)
        self.assertIn("16777216 bytes", html)
        self.assertIn("does not guarantee capacity", html)

    def test_derived_resources_and_source_reference(self) -> None:
        cookie, csrf = self._session()
        worker_id = self._pair_worker(cookie, csrf, "Worker")
        status, _, _ = _form(self.port, "/admin/sources/add", {
            "csrf": csrf, "label": "Source", "worker_id": worker_id,
        }, cookie)
        self.assertEqual(303, status)
        source = self.plane.configuration.sources[0]
        self.plane.service_set_source_model_policy(source.source_id, {"allowed_models": ["synthetic-model"]})
        self.plane._apply_source_inventory(ModelInventoryReport(worker_id=worker_id, sources=(  # pyright: ignore[reportPrivateUsage]
            SourceInventory(source_id=source.source_id, adapter_id=f"codex:{source.source_id}",
                            kind="codex_subscription", observed_at="2026-09-16T12:00:00.000Z",
                            auth_state="authenticated", runtime_name="codex", runtime_version="1.2.3",
                            models=(DiscoveredModel("synthetic-model", ("high",)),)),
        )))
        _ = self.plane.service_add_resource(resource_document("configured", endpoint_id=None))
        status, html, _ = _get(self.port, "/admin/resources", cookie)
        self.assertEqual(200, status)
        self.assertIn("configured", html)
        self.assertIn("synthetic-model", html)
        self.assertIn("Managed by source", html)
        self.assertNotIn(f'name="resource_id" value="{source.source_id}:synthetic-model:high"', html)
        self.plane.service_revoke_worker(worker_id)
        status, html, _ = _form(self.port, "/admin/workers/delete", {"csrf": csrf, "worker_id": worker_id}, cookie)
        self.assertEqual(409, status)
        self.assertIn("references", html)

    def test_source_guidance_states(self) -> None:
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"connected": False}, "disconnected"),
            ({"connected": True, "source_authenticated": "auth_required", "kind": "codex_subscription", "login_command": "supported-login"}, "supported-login"),
            ({"connected": True, "source_authenticated": "unverified"}, "not been verified"),
            ({"connected": True, "source_authenticated": "unavailable"}, "runtime unavailable"),
            ({"connected": True, "source_authenticated": "unverified", "kind": "zcode_subscription", "observed_at": "time"}, "cannot be verified without execution"),
            ({"connected": True, "source_authenticated": "unverified", "kind": "zcode_subscription", "observed_at": "time", "discovered": 1}, "not admitted"),
            ({"connected": True, "source_authenticated": "authenticated", "restricted": 1}, "restricted"),
            ({"connected": True, "source_authenticated": "authenticated", "discovered": 1}, "not admitted"),
            ({"connected": True, "source_authenticated": "authenticated", "observed_at": "time"}, "freshness is not established"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertIn(expected, _source_guidance(source))

    def test_provider_add_form_flow_and_redaction(self) -> None:
        cookie, csrf = self._session()
        status, _html, _headers = _form(
            self.port,
            "/admin/providers/add",
            {
                "csrf": csrf,
                "provider_id": "zai-http",
                "adapter_id": "zai-coding-plan",
                "base_url": "https://api.z.ai",
                "label": "Z.ai",
                "secret": FAKE_PROVIDER_SECRET,
            },
            cookie,
        )
        self.assertEqual(303, status)
        status, html, _headers = _get(self.port, "/admin/providers", cookie)
        self.assertEqual(200, status)
        self.assertIn("zai-http", html)
        self.assertIn("credential stored", html)
        self.assertNotIn(FAKE_PROVIDER_SECRET, html)

    def test_resource_add_form_flow(self) -> None:
        cookie, csrf = self._session()
        _status, _html, _headers = _form(
            self.port,
            "/admin/providers/add",
            {
                "csrf": csrf,
                "provider_id": "zai-http",
                "adapter_id": "zai-coding-plan",
                "base_url": "https://api.z.ai",
                "secret": FAKE_PROVIDER_SECRET,
            },
            cookie,
        )
        status, _html, _headers = _form(
            self.port,
            "/admin/resources/add",
            {
                "csrf": csrf,
                "resource_id": "zai-plan-1",
                "channel": "server_direct_http",
                "provider": "zai",
                "model": "glm-5",
                "entitlement": "subscription_included",
                "freshness_ttl_seconds": "3600",
                "endpoint_id": "zai-http",
                "enabled": "yes",
            },
            cookie,
        )
        self.assertEqual(303, status)
        status, html, _headers = _get(self.port, "/admin/resources", cookie)
        self.assertEqual(200, status)
        self.assertIn("zai-plan-1", html)
        self.assertIn("authenticated", html)
        self.assertIn("protocol_compatible", html)

    def test_client_issue_shows_key_exactly_once(self) -> None:
        cookie, csrf = self._session()
        status, html, _headers = _form(
            self.port,
            "/admin/clients/issue",
            {"csrf": csrf, "label": "openai sdk"},
            cookie,
        )
        self.assertEqual(200, status)
        match = re.search(r"<pre>(sk-sr-[A-Za-z0-9_\-]+)</pre>", html)
        assert match is not None, "issuance page did not render the key"
        api_key = match.group(1)
        status, html, _headers = _get(self.port, "/admin/clients", cookie)
        self.assertEqual(200, status)
        self.assertNotIn(api_key, html)
        self.assertIn("active", html)
        status, _payload, _headers = self.exchange(
            "GET", "/v1/status", None, headers={"Authorization": f"Bearer {api_key}"}
        )
        self.assertEqual(200, status)

    def test_read_only_connection_issuance_and_revocation(self) -> None:
        cookie, csrf = self._session()
        status, html, _ = _get(self.port, "/admin/clients", cookie)
        self.assertEqual(200, status)
        self.assertIn("Create read-only key", html)
        self.assertIn("does not verify connectivity", html)
        status, html, _ = _form(self.port, "/admin/clients/issue", {
            "csrf": csrf, "label": "synthetic console", "role": "observer",
        }, cookie)
        self.assertEqual(200, status)
        match = re.search(r"<pre>(sk-sr-[A-Za-z0-9_\-]+)</pre>", html)
        assert match is not None
        key = match.group(1)
        records = [record for record in self.plane.store.list_client_keys() if record.label == "synthetic console"]
        self.assertEqual(1, len(records))
        self.assertEqual("observer", records[0].role)
        status, listing, _ = _get(self.port, "/admin/clients", cookie)
        self.assertEqual(200, status)
        self.assertIn("Read-only observer", listing)
        self.assertIn("synthetic console", listing)
        self.assertNotIn(key, listing)
        status, _, _ = self.exchange("GET", "/observer/v1/snapshot", headers={"Authorization": f"Bearer {key}"}, with_session=False)
        self.assertEqual(200, status)
        status, _, _ = _form(self.port, "/admin/clients/revoke", {
            "csrf": csrf, "client_id": records[0].client_id,
        }, cookie)
        self.assertEqual(303, status)
        status, _, _ = self.exchange("GET", "/observer/v1/snapshot", headers={"Authorization": f"Bearer {key}"}, with_session=False)
        self.assertEqual(401, status)

    def _pair_worker(self, cookie: str, csrf: str, label: str) -> str:
        """Initiate pairing in the UI and redeem it over the M05 protocol."""
        import re as _re
        import threading

        from scarcity_router.worker_protocol import PairResultMessage
        from tests.worker_fixtures import MemoryTransport, ScriptedWorker

        status, html, _headers = _form(
            self.port, "/admin/workers/initiate", {"csrf": csrf, "label": label}, cookie
        )
        assert status == 200
        match = _re.search(r"<pre>([A-Za-z0-9_\-]{16,})</pre>", html)
        assert match is not None, "pairing page did not render the code"
        code = match.group(1)
        server_side, worker_side = MemoryTransport.pair()
        endpoint = self.plane.worker_endpoint
        session = endpoint.attach_transport(server_side)
        endpoint.register_attached(session)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        worker = ScriptedWorker(worker_side)
        result = worker.send_pair(code)
        worker.transport.close()
        _ = thread.join(timeout=5)
        assert isinstance(result, PairResultMessage), result
        return result.worker_id

    def test_sources_page_flow_and_label_first_workers(self) -> None:
        cookie, csrf = self._session()
        worker_id = self._pair_worker(cookie, csrf, "Precision Codex")
        # The workers page shows the FRIENDLY label first, the technical
        # id beneath it (D-053 point 10).
        status, html, _headers = _get(self.port, "/admin/workers", cookie)
        self.assertEqual(200, status)
        self.assertLess(
            html.index("Precision Codex"), html.index(worker_id),
            "the technical worker id must not precede the friendly label",
        )
        # Add a source through the form: label + worker, NO model slug.
        status, _html, _headers = _form(
            self.port,
            "/admin/sources/add",
            {"csrf": csrf, "label": "Personal ChatGPT Pro", "worker_id": worker_id},
            cookie,
        )
        self.assertEqual(303, status)
        status, html, _headers = _get(self.port, "/admin/sources", cookie)
        self.assertEqual(200, status)
        self.assertIn("Personal ChatGPT Pro", html)
        self.assertIn("codex-login --source", html)
        self.assertIn("not connected", html)
        self.assertIn("source auth:", html)
        # The source view never asks for or shows a model slug input.
        self.assertNotIn('name="model"', html)
        # The source is removable.
        source_id = self.plane.configuration.sources[0].source_id
        status, _html, _headers = _form(
            self.port,
            "/admin/sources/delete",
            {"csrf": csrf, "source_id": source_id},
            cookie,
        )
        self.assertEqual(303, status)
        status, html, _headers = _get(self.port, "/admin/sources", cookie)
        self.assertIn("No execution sources configured", html)

    def test_source_grants_and_context_attestation_are_explicit_and_preserved(self) -> None:
        cookie, csrf = self._session()
        worker_id = self._pair_worker(cookie, csrf, "Synthetic worker")
        status, _, _ = _form(self.port, "/admin/sources/add", {
            "csrf": csrf, "label": "Synthetic source", "worker_id": worker_id,
        }, cookie)
        self.assertEqual(303, status)
        source = self.plane.configuration.sources[0]
        legacy = replace(source, allowed_tracks=("openai/old-track",))
        self.plane._save_config(replace(self.plane.configuration, sources=(legacy,)))  # pyright: ignore[reportPrivateUsage]
        self.plane._apply_source_inventory(ModelInventoryReport(worker_id=worker_id, sources=(  # pyright: ignore[reportPrivateUsage]
            SourceInventory(source_id=source.source_id, adapter_id=f"codex:{source.source_id}",
                            kind="codex_subscription", observed_at="2026-09-16T12:00:00.000Z",
                            auth_state="authenticated", runtime_name="codex", runtime_version="1.2.3",
                            models=(DiscoveredModel("synthetic-model", ("high",)),), runtime_version_exact=True),
        )))
        status, html, _ = _get(self.port, "/admin/sources", cookie)
        self.assertEqual(200, status)
        self.assertIn("Legacy track policy is inactive", html)
        self.assertIn("Allow model", html)
        self.assertIn("legacy track policy", html.lower())
        status, _, _ = _get(self.port, "/admin/sources/policy", cookie)
        self.assertEqual(405, status)
        grant = {"csrf": csrf, "source_id": source.source_id, "action": "grant", "model": "synthetic-model"}
        status, _, _ = _form(self.port, "/admin/sources/policy", grant, cookie)
        self.assertEqual(400, status)
        self.assertEqual(("openai/old-track",), self.plane.configuration.sources[0].allowed_tracks)
        status, _, _ = _form(self.port, "/admin/sources/policy", {**grant, "csrf": "wrong", "ack_legacy": "yes"}, cookie)
        self.assertEqual(403, status)
        status, _, _ = _form(self.port, "/admin/sources/policy", {**grant, "ack_legacy": "yes"}, cookie)
        self.assertEqual(303, status)
        current = self.plane.configuration.sources[0]
        self.assertEqual(("synthetic-model",), current.allowed_models)
        self.assertEqual((), current.allowed_tracks)
        status, html, _ = _get(self.port, "/admin/sources", cookie)
        self.assertIn("Context evidence for high", html)
        self.assertIn("awaiting inventory refresh", html)
        self.assertNotIn("model not allowed", html)
        context = {"csrf": csrf, "source_id": source.source_id, "model": "synthetic-model", "variant": "high",
                   "runtime_version": "1.2.3", "input_tokens": "4096", "evidence_ref": "local:synthetic-ui",
                   "verified_at": "2026-09-16T11:00:00.000Z", "valid_until": "2026-10-16T11:00:00.000Z"}
        status, _, _ = _form(self.port, "/admin/sources/context", {**context, "runtime_version": "9.9.9"}, cookie)
        self.assertEqual(400, status)
        status, _, _ = _form(self.port, "/admin/sources/context", context, cookie)
        self.assertEqual(303, status)
        self.assertEqual("local:synthetic-ui", self.plane.configuration.sources[0].context_evidence[0].evidence_ref)
        status, _, _ = _form(self.port, "/admin/sources/policy", {**grant, "action": "revoke"}, cookie)
        self.assertEqual(303, status)
        self.assertEqual((), self.plane.configuration.sources[0].allowed_models)
        self.assertEqual(1, len(self.plane.configuration.sources[0].context_evidence))
        status, html, _ = _get(self.port, "/admin/sources", cookie)
        self.assertIn("Dormant attestations", html)
        status, _, _ = _form(self.port, "/admin/sources/context", {
            "csrf": csrf, "source_id": source.source_id, "model": "synthetic-model", "variant": "high", "action": "remove",
        }, cookie)
        self.assertEqual(303, status)
        self.assertEqual((), self.plane.configuration.sources[0].context_evidence)

    def test_worker_pairing_shows_code_once(self) -> None:
        import threading

        from scarcity_router.worker_protocol import PairResultMessage
        from tests.worker_fixtures import MemoryTransport, ScriptedWorker

        cookie, csrf = self._session()
        status, html, _headers = _form(
            self.port, "/admin/workers/initiate", {"csrf": csrf, "label": "rig"}, cookie
        )
        self.assertEqual(200, status)
        match = re.search(r"<pre>([A-Za-z0-9_\-]{16,})</pre>", html)
        assert match is not None, "pairing page did not render the code"
        code = match.group(1)
        status, html, _headers = _get(self.port, "/admin/workers", cookie)
        self.assertEqual(200, status)
        # The code is never shown again anywhere in the UI.
        self.assertNotIn(code, html)
        self.assertIn("No workers configured", html)
        # The code redeems over the M05 worker protocol handshake, not HTTP.
        server_side, worker_side = MemoryTransport.pair()
        endpoint = self.plane.worker_endpoint
        session = endpoint.attach_transport(server_side)
        endpoint.register_attached(session)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        worker = ScriptedWorker(worker_side)
        result = worker.send_pair(code)
        self.assertTrue(hasattr(result, "worker_id"), f"pairing failed: {result!r}")
        worker.transport.close()
        _ = thread.join(timeout=5)
        # The workers page now shows the paired device from the M05 store.
        assert isinstance(result, PairResultMessage), result
        status, html, _headers = _get(self.port, "/admin/workers", cookie)
        self.assertEqual(200, status)
        self.assertIn(result.worker_id, html)
        self.assertIn("active", html)

    def test_missing_csrf_renders_a_forbidden_error(self) -> None:
        cookie, _csrf = self._session()
        status, html, _headers = _form(
            self.port,
            "/admin/clients/issue",
            {"label": "no-csrf"},
            cookie,
        )
        self.assertEqual(403, status)
        self.assertIn("CSRF", html)

    def test_diagnostics_page_renders_report(self) -> None:
        cookie, _csrf = self._session()
        status, html, _headers = _get(self.port, "/admin/diagnostics", cookie)
        self.assertEqual(200, status)
        self.assertIn("Diagnostics", html)
        self.assertIn("no collector call", html)
        self.assertNotIn(FAKE_PROVIDER_SECRET, html)

    def test_client_config_page_shows_base_url_not_secrets(self) -> None:
        cookie, _csrf = self._session()
        status, html, _headers = _get(self.port, "/admin/client-config", cookie)
        self.assertEqual(200, status)
        self.assertIn(f"http://127.0.0.1:{self.port}", html)
        self.assertNotIn("sk-sr-", html)


if __name__ == "__main__":
    _ = unittest.main()
