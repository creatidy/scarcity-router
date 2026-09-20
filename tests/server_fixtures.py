"""Shared deterministic fixtures for the M09 server-component tests.

Everything is synthetic and self-contained: no live providers, no network
beyond the loopback test listener, no wall-clock reads (the clock is
injected), and conspicuous fake secrets that tests assert never appear in
any output. One temporary data directory per fixture; the XDG user config
is isolated so host state is never read or provisioned.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override

from scarcity_router.capacity import (
    CapacityDiagnostic,
    CapacitySnapshot,
    CapacityWindow,
)
from scarcity_router.cli import main as cli_main
from scarcity_router.config import user_config_dir
from scarcity_router.control_api import (
    SESSION_COOKIE_NAME,
    ControlPlane,
    GenerationTester,
)
from scarcity_router.gateway_server import GatewayHTTPServer, make_gateway_server
from scarcity_router.server_store import ServerStore, canonical_store_path
from scarcity_router.status import StatusCollectors

# ── Deterministic instants ────────────────────────────────────────────────────

T_EVAL = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
T_NOW = "2026-09-16T12:00:00.000Z"

#: Conspicuous SYNTHETIC credentials; tests assert these never leak into
#: any response, export, diagnostic or log.
FAKE_ADMIN_PASSWORD = "synthetic-admin-password-NOT-A-SECRET-01"
FAKE_PROVIDER_SECRET = "SYNTHETIC-PROVIDER-SECRET-MARKER-0002"

PBKDF2_TEST_ITERATIONS = 1000


def fixed_clock() -> datetime:
    return T_EVAL


def _isolate_user_config() -> None:
    """Point XDG_CONFIG_HOME at a throwaway directory (D-036 isolation)."""
    base = Path(tempfile.mkdtemp(prefix="scarcity-router-m09-config-"))
    os.environ["XDG_CONFIG_HOME"] = str(base)
    _ = user_config_dir().mkdir(parents=True, exist_ok=True, mode=0o700)


_isolate_user_config()


# ── Synthetic recommendation collectors (telemetry only, no inference) ───────


def _snap(
    provider: str,
    five: int,
    weekly: int,
    *,
    status: str = "ok",
) -> CapacitySnapshot:
    scope = "codex" if provider == "openai" else "coding_plan"
    windows: list[CapacityWindow] = []
    for kind, remaining, duration in (
        ("five_hour", five, 18_000),
        ("weekly", weekly, 604_800),
    ):
        windows.append(
            CapacityWindow(
                resource="tokens",
                kind=kind,
                scope_id=scope,
                duration_seconds=duration,
                used_percent=100 - remaining,
                remaining_percent=remaining,
                window_id=f"{provider}-{kind}",
            )
        )
    diagnostics: tuple[CapacityDiagnostic, ...] = (
        () if status == "ok" else (CapacityDiagnostic(code="telemetry_unknown"),)
    )
    return CapacitySnapshot(
        schema_version=3,
        provider=provider,
        source="synthetic_test",
        retrieved_at=T_NOW,
        status=status,
        windows=tuple(windows),
        diagnostics=diagnostics,
    )


def synthetic_collectors(
    *,
    openai_five: int = 40,
    openai_weekly: int = 40,
    zai_five: int = 80,
    zai_weekly: int = 80,
    calls: list[str] | None = None,
) -> StatusCollectors:
    """Synthetic OpenAI/Z.ai telemetry collectors with a call recorder."""
    from scarcity_router.providers.openai_codex_acquisition import (
        OpenAICodexObservation,
    )
    from tests.observation import paired_observation

    def openai(*, retrieved_at: str) -> OpenAICodexObservation:
        _ = retrieved_at
        if calls is not None:
            calls.append("openai")
        return paired_observation(_snap("openai", openai_five, openai_weekly))

    def zai(*, retrieved_at: str) -> CapacitySnapshot:
        _ = retrieved_at
        if calls is not None:
            calls.append("zai")
        return _snap("zai", zai_five, zai_weekly)

    return StatusCollectors(openai=openai, zai=zai)


# ── Control-plane + server harness ────────────────────────────────────────────


class ServerHarness(unittest.TestCase):
    """One temporary store, control plane and loopback server per test."""

    plane: ControlPlane
    server: GatewayHTTPServer
    thread: threading.Thread
    data_dir: Path
    client_key: str
    client_id: str
    cookie: str

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.plane = cast(ControlPlane, object())
        self.server = cast(GatewayHTTPServer, object())
        self.thread = cast(threading.Thread, object())
        self.data_dir = Path(".")
        self.client_key = ""
        self.client_id = ""
        self.cookie = ""

    @override
    def setUp(self) -> None:
        self.data_dir = self._make_data_dir()
        self.plane = self.make_plane(self.data_dir)
        self._start_server()

    @override
    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        _ = self.thread.join(timeout=10)
        self.plane.store.close()
        _ = shutil.rmtree(self.data_dir, ignore_errors=True)

    def _make_data_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="scarcity-router-m09-store-"))

    def make_plane(
        self,
        data_dir: Path,
        *,
        collectors: StatusCollectors | None = None,
        generation_tester: GenerationTester | None = None,
    ) -> ControlPlane:
        from scarcity_router.server_ui import dispatch_ui

        store = ServerStore.open(data_dir)
        return ControlPlane(
            store=store,
            clock=fixed_clock,
            collectors=(
                synthetic_collectors() if collectors is None else collectors
            ),
            pbkdf2_iterations=PBKDF2_TEST_ITERATIONS,
            version="0.1.0.test",
            own_origins=("http://127.0.0.1:8787",),
            generation_tester=generation_tester,
            ui_dispatcher=dispatch_ui,
        )

    def _start_server(self) -> None:
        self.server = make_gateway_server(
            self.plane.current_application(),
            host="127.0.0.1",
            port=0,
            control_plane=self.plane,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    @property
    def port(self) -> int:
        return cast("tuple[str, int]", self.server.server_address)[1]

    # ── Onboarding convenience ───────────────────────────────────────────

    def onboard(self, *, login: bool = True) -> None:
        """Complete first-run setup and hold an administrator session."""
        status, payload, headers = self.exchange(
            "POST",
            "/control/bootstrap/admin",
            {"password": FAKE_ADMIN_PASSWORD, "confirm": True},
        )
        assert status == 200, payload
        cookies = [value for name, value in headers if name.lower() == "set-cookie"]
        assert cookies, "bootstrap did not set a session cookie"
        self.cookie = _cookie_value(cookies[0])
        if login:
            issued = self.plane.service_issue_client_key({"label": "test client"})
            self.client_key = cast(str, issued["api_key"])
            self.client_id = cast(str, issued["client_id"])

    # ── HTTP helpers ─────────────────────────────────────────────────────

    def exchange(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        raw_body: bytes | None = None,
        with_session: bool = True,
    ) -> tuple[int, object, list[tuple[str, str]]]:
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        merged: dict[str, str] = {}
        if body is not None:
            merged["Content-Type"] = "application/json"
        if headers:
            merged.update(headers)
        if self.cookie and with_session:
            merged["Cookie"] = f"{SESSION_COOKIE_NAME}={self.cookie}"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=merged)
            response = connection.getresponse()
            raw = response.read()
            response_headers = response.getheaders()
            status = response.status
        finally:
            connection.close()
        parsed: object = None
        if raw:
            try:
                parsed = cast(object, json.loads(raw))
            except json.JSONDecodeError:
                parsed = raw.decode("utf-8", errors="replace")
        return status, parsed, response_headers

    def admin_post(
        self, path: str, payload: object | None = None
    ) -> tuple[int, object]:
        csrf = self.plane.csrf_token_for_cookie(self.cookie)
        assert csrf is not None
        status, parsed, _headers = self.exchange(
            "POST", path, payload, headers={"X-Scarcity-CSRF": csrf}
        )
        return status, parsed

    def admin_get(self, path: str) -> tuple[int, object]:
        status, parsed, _headers = self.exchange("GET", path)
        return status, parsed


def _cookie_value(set_cookie: str) -> str:
    return set_cookie.split(";", 1)[0].split("=", 1)[1]


def resource_document(
    resource_id: str = "zai-plan-1",
    *,
    channel: str = "server_direct_http",
    provider: str = "zai",
    model: str = "glm-5",
    entitlement: str = "subscription_included",
    endpoint_id: str | None = "zai-http",
    worker_id: str | None = None,
    enabled: bool = True,
    freshness_ttl_seconds: int = 3600,
) -> dict[str, object]:
    """One valid ResourceConfig document for control-API tests."""
    document: dict[str, object] = {
        "registration": {
            "identity": {
                "resource_id": resource_id,
                "channel": channel,
                "provider": provider,
                "model": model,
                "entitlement": entitlement,
            },
            "freshness_ttl_seconds": freshness_ttl_seconds,
        },
        "enabled": enabled,
    }
    if endpoint_id is not None:
        document["endpoint_id"] = endpoint_id
    if worker_id is not None:
        document["worker_id"] = worker_id
    return document


__all__ = [
    "FAKE_ADMIN_PASSWORD",
    "FAKE_PROVIDER_SECRET",
    "PBKDF2_TEST_ITERATIONS",
    "T_EVAL",
    "T_NOW",
    "GenerationTester",
    "ServerHarness",
    "canonical_store_path",
    "cli_main",
    "fixed_clock",
    "resource_document",
    "synthetic_collectors",
]
