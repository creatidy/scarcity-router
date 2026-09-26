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

from tests.server_fixtures import (
    FAKE_ADMIN_PASSWORD,
    FAKE_PROVIDER_SECRET,
    ServerHarness,
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
    body = urllib.parse.urlencode(fields).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
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
        # The issuance page shows the key; the listing page never does.
        status, html, _headers = _get(self.port, "/admin/clients", cookie)
        self.assertEqual(200, status)
        self.assertNotIn(api_key, html)
        self.assertIn("active", html)
        # The shown key authenticates over the machine interface.
        status, _payload, _headers = self.exchange(
            "GET", "/v1/status", None, headers={"Authorization": f"Bearer {api_key}"}
        )
        self.assertEqual(200, status)

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
