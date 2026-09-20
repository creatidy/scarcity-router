"""Diagnostics and doctor tests (M09, issue #94).

The shared diagnostics logic is used by BOTH the web UI and the ``doctor``
CLI command; these tests pin the resource ladder, the redaction
discipline, the never-consume-quota guarantee and the doctor CLI's local
and server modes.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from scarcity_router import get_version
from scarcity_router.cli import main as cli_main
from scarcity_router.diagnostics import (
    LadderInputs,
    ServerDiagnosticsInputs,
    collect_local_diagnostics,
    collect_server_diagnostics,
    compute_resource_ladder,
    render_report_human,
)
from scarcity_router.resource_state import (
    ExecutionCapabilities,
    PromotionObservation,
    ResourceHealth,
    ResourceIdentity,
    ResourceRegistration,
    ResourceRegistry,
    ResourceStateSnapshot,
)
from scarcity_router.routing_core import AdministratorConstraints
from scarcity_router.server_config import (
    ProviderEndpointConfig,
    ResourceConfig,
    ServerConfiguration,
)

_NO_WORKERS: frozenset[str] = frozenset()
_NO_CHANNELS: frozenset[str] = frozenset()
_CREDENTIALED: frozenset[str] = frozenset({"zai-http"})

T_EVAL = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
T_NOW = "2026-09-16T12:00:00.000Z"
T_OBSERVED = "2026-09-16T11:59:00.000Z"


def _identity(resource_id: str = "zai-plan-1") -> ResourceIdentity:
    return ResourceIdentity(
        resource_id=resource_id,
        channel="server_direct_http",
        provider="zai",
        model="glm-5",
        entitlement="subscription_included",
    )


def _registration(resource_id: str = "zai-plan-1") -> ResourceRegistration:
    return ResourceRegistration(
        identity=_identity(resource_id),
        freshness_ttl_seconds=3600,
        capabilities=ExecutionCapabilities(streaming=True),
    )


def _observation(
    identity: ResourceIdentity,
    *,
    status: str = "ok",
    promotions: tuple[PromotionObservation, ...] = (),
) -> ResourceStateSnapshot:
    from scarcity_router.capacity import CapacityDiagnostic

    diagnostics: tuple[CapacityDiagnostic, ...] = (
        () if status == "ok" else (CapacityDiagnostic(code=status),)
    )
    return ResourceStateSnapshot(
        schema_version=1,
        identity=identity,
        observed_at=T_OBSERVED,
        health=ResourceHealth(status=status, diagnostics=diagnostics),
        promotions=promotions,
    )


def _config(
    *,
    resources: tuple[ResourceConfig, ...] = (),
) -> ServerConfiguration:
    return ServerConfiguration(
        providers=(
            ProviderEndpointConfig(
                provider_id="zai-http",
                adapter_id="openai_http",
                base_url="https://api.z.ai",
            ),
        ),
        resources=resources,
    )


def _resource(resource_id: str = "zai-plan-1") -> ResourceConfig:
    return ResourceConfig(
        registration=_registration(resource_id), endpoint_id="zai-http"
    )


_DEFAULT_RESOURCES: tuple[ResourceConfig, ...] = (_resource(),)


class LadderTests(unittest.TestCase):
    def _inputs(
        self,
        *,
        resources: tuple[ResourceConfig, ...] = _DEFAULT_RESOURCES,
        registry: ResourceRegistry | None = None,
        paired_workers: frozenset[str] = _NO_WORKERS,
        channels: frozenset[str] = _NO_CHANNELS,
        credentials: frozenset[str] = _CREDENTIALED,
        promotions: tuple[PromotionObservation, ...] = (),
        health: str = "ok",
        constraints: AdministratorConstraints | None = None,
    ) -> LadderInputs:
        if registry is None:
            registry = ResourceRegistry(clock=lambda: T_NOW)
        for resource in resources:
            if resource.registration.identity.resource_id not in {
                entry.identity.resource_id
                for entry in registry.registry_snapshot().entries
            }:
                registry.register(resource.registration)
        if resources:
            registry.apply_snapshot(
                _observation(
                    identity=resources[0].registration.identity,
                    promotions=promotions,
                    status=health,
                )
            )
        return LadderInputs(
            configuration=_config(resources=resources),
            registry_snapshot=registry.registry_snapshot(),
            constraints=constraints or AdministratorConstraints(),
            paired_worker_ids=paired_workers,
            channels_with_adapters=channels,
            endpoints_with_credentials=credentials,
            now=T_EVAL,
        )

    def test_ladder_stops_at_protocol_compatible_without_adapter(self) -> None:
        ladder = compute_resource_ladder(self._inputs())[0]
        self.assertTrue(ladder.detected)
        self.assertTrue(ladder.authenticated)
        self.assertFalse(ladder.protocol_compatible)
        self.assertFalse(ladder.available)
        self.assertFalse(ladder.eligible)
        self.assertFalse(ladder.promotion_confirmed)
        self.assertEqual("protocol_compatible", ladder.first_blocked_stage)
        self.assertIsNotNone(ladder.remediation)

    def test_full_ladder_passes_with_adapter_and_fresh_observation(self) -> None:
        registry = ResourceRegistry(clock=lambda: T_NOW)
        ladder = compute_resource_ladder(
            self._inputs(
                registry=registry,
                channels=frozenset({"server_direct_http"}),
                promotions=(
                    PromotionObservation(source="vendor-page", observed_at=T_OBSERVED),
                ),
            )
        )[0]
        self.assertTrue(ladder.protocol_compatible)
        self.assertTrue(ladder.available)
        self.assertTrue(ladder.eligible)
        self.assertTrue(ladder.promotion_confirmed)
        self.assertIsNone(ladder.first_blocked_stage)

    def test_promotion_confirmed_requires_a_current_window(self) -> None:
        registry = ResourceRegistry(clock=lambda: T_NOW)
        expired = compute_resource_ladder(
            self._inputs(
                registry=registry,
                channels=frozenset({"server_direct_http"}),
                promotions=(
                    PromotionObservation(
                        source="vendor-page",
                        observed_at=T_OBSERVED,
                        valid_until="2026-09-16T11:00:00.000Z",
                    ),
                ),
            )
        )[0]
        self.assertTrue(expired.eligible)
        self.assertFalse(expired.promotion_confirmed)

    def test_degraded_health_blocks_availability(self) -> None:
        ladder = compute_resource_ladder(
            self._inputs(
                channels=frozenset({"server_direct_http"}),
                health="auth_required",
            )
        )[0]
        self.assertTrue(ladder.protocol_compatible)
        self.assertFalse(ladder.available)
        self.assertEqual("available", ladder.first_blocked_stage)
        self.assertIsNotNone(ladder.remediation)

    def test_administrator_block_blocks_eligibility(self) -> None:
        registry = ResourceRegistry(clock=lambda: T_NOW)
        ladder = compute_resource_ladder(
            self._inputs(
                registry=registry,
                channels=frozenset({"server_direct_http"}),
                constraints=AdministratorConstraints(
                    blocked_resource_ids=("zai-plan-1",)
                ),
            )
        )[0]
        self.assertTrue(ladder.available)
        self.assertFalse(ladder.eligible)
        self.assertEqual("eligible", ladder.first_blocked_stage)

    def test_disabled_resource_is_blocked_at_detection(self) -> None:
        resource = ResourceConfig(
            registration=_registration(), enabled=False
        )
        ladder = compute_resource_ladder(self._inputs(resources=(resource,)))[0]
        self.assertFalse(ladder.enabled)
        self.assertEqual("detected", ladder.first_blocked_stage)
        self.assertIn("disable", (ladder.remediation or "").lower())

    def test_missing_credential_blocks_authentication(self) -> None:
        ladder = compute_resource_ladder(
            self._inputs(credentials=frozenset())
        )[0]
        self.assertFalse(ladder.authenticated)
        self.assertEqual("authenticated", ladder.first_blocked_stage)

    def test_worker_bridged_resource_needs_a_paired_worker(self) -> None:
        registration = ResourceRegistration(
            identity=ResourceIdentity(
                resource_id="local-1",
                channel="worker_bridged",
                provider="ollama",
                model="llama4",
                entitlement="local_ungated",
            ),
            freshness_ttl_seconds=600,
        )
        resource = ResourceConfig(
            registration=registration, worker_id="worker-a"
        )
        ladder = compute_resource_ladder(
            self._inputs(
                resources=(resource,),
                paired_workers=frozenset({"worker-a"}),
            )
        )[0]
        self.assertTrue(ladder.authenticated)
        self.assertFalse(ladder.protocol_compatible)


class ServerReportTests(unittest.TestCase):
    def test_report_is_redacted_and_complete(self) -> None:
        report = collect_server_diagnostics(
            ServerDiagnosticsInputs(
                configuration=_config(resources=(_resource(),)),
                registry_snapshot=None,
                constraints=AdministratorConstraints(),
                paired_worker_ids=frozenset(),
                channels_with_adapters=frozenset(),
                endpoints_with_credentials=frozenset({"zai-http"}),
                pairings=(),
                store_schema_version=1,
                store_error=None,
                admin_configured=True,
                active_client_keys=2,
                revoked_client_keys=1,
                now=T_EVAL,
                version="0.1.0.test",
            )
        )
        document = report.to_dict()
        serialized = json.dumps(document)
        self.assertNotIn("SYNTHETIC", serialized)
        self.assertNotIn("secret", serialized.replace("secret_policy", ""))
        self.assertEqual("server", document["mode"])
        checks = cast("list[object]", document["checks"])
        check_ids = [
            cast("dict[str, object]", check)["check_id"]
            for check in checks
        ]
        self.assertIn("store", check_ids)
        self.assertIn("admin_identity", check_ids)
        self.assertIn("quota_safety", check_ids)

    def test_human_rendering_lists_stages_and_remediation(self) -> None:
        report = collect_server_diagnostics(
            ServerDiagnosticsInputs(
                configuration=_config(resources=(_resource(),)),
                registry_snapshot=None,
                constraints=AdministratorConstraints(),
                paired_worker_ids=frozenset(),
                channels_with_adapters=frozenset(),
                endpoints_with_credentials=frozenset(),
                pairings=({"worker_id": "w1", "label": "rig", "status": "paired"},),
                store_schema_version=1,
                store_error=None,
                admin_configured=True,
                active_client_keys=1,
                revoked_client_keys=0,
                now=T_EVAL,
                version="0.1.0.test",
            )
        )
        text = render_report_human(report)
        self.assertIn("resource zai-plan-1", text)
        self.assertIn("blocked at authenticated", text)
        self.assertIn("remediation:", text)
        self.assertIn("worker w1", text)


class DoctorCliTests(unittest.TestCase):
    def _run(self, arguments: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        code = cli_main(arguments, stdout=output)
        return code, output.getvalue()

    def test_local_mode_reports_artifacts_and_never_calls_collectors(self) -> None:
        def exploding_collector(**_: object) -> object:
            raise AssertionError("doctor must never invoke a collector")

        code, output = self._run(["doctor"])
        self.assertEqual(0, code)
        self.assertIn("Catalog and policy artifacts", output)
        self.assertIn("[ok]", output)
        self.assertIn("doctor report (local mode)", output)
        _ = exploding_collector

    def test_local_mode_json(self) -> None:
        code, output = self._run(["doctor", "--json"])
        self.assertEqual(0, code)
        document = cast("dict[str, object]", json.loads(output))
        self.assertEqual("local", document["mode"])
        raw_checks = cast("list[object]", document["checks"])
        checks = [
            cast("dict[str, object]", check)["check_id"] for check in raw_checks
        ]
        self.assertIn("artifacts", checks)
        self.assertIn("server_mode", checks)
        self.assertIn("versions", document)

    def test_server_mode_reports_store_state(self) -> None:
        from scarcity_router.control_api import ControlPlane
        from scarcity_router.server_store import ServerStore

        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "server"
            store = ServerStore.open(data_dir)
            plane = ControlPlane(
                store=store,
                clock=lambda: T_EVAL,
                pbkdf2_iterations=1000,
                version="0.1.0.test",
            )
            plane.service_bootstrap_admin(
                password="synthetic-admin-password-NOT-A-SECRET-01", confirm=True
            )
            _ = plane.service_add_provider(
                {
                    "provider_id": "zai-http",
                    "adapter_id": "openai_http",
                    "base_url": "https://api.z.ai",
                }
            )
            store.close()
            code, output = self._run(
                ["doctor", "--server-data-dir", str(data_dir)]
            )
            self.assertEqual(0, code)
            self.assertIn("Administrator identity", output)
            self.assertIn("provider endpoint(s)", output)
            self.assertIn("store schema version 1", output)

    def test_server_mode_json_and_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            missing = Path(parent) / "absent"
            code, output = self._run(
                ["doctor", "--server-data-dir", str(missing), "--json"]
            )
            self.assertEqual(0, code)
            document = cast("dict[str, object]", json.loads(output))
            self.assertEqual("server", document["mode"])
            store_check = cast(
                "dict[str, object]",
                cast("list[object]", document["checks"])[0],
            )
            self.assertEqual("store", store_check["check_id"])
            self.assertIn("remediation", store_check)


class VersionAndStatesTests(unittest.TestCase):
    def test_report_version_pins_the_package_version(self) -> None:
        report = collect_local_diagnostics(
            now=T_EVAL,
            version=get_version(),
            artifact_error=None,
            policy_error=None,
            policy_configured=True,
        )
        self.assertEqual(get_version(), report.versions["scarcity_router"])

    def test_local_report_names_docker_free_remediation_for_broken_policy(
        self,
    ) -> None:
        report = collect_local_diagnostics(
            now=T_EVAL,
            version=get_version(),
            artifact_error=None,
            policy_error="user policy file is malformed",
            policy_configured=True,
        )
        rendered = render_report_human(report)
        _ = rendered
        self.assertIn("[fail]", render_report_human(report))
        self.assertIn("malformed", render_report_human(report))


if __name__ == "__main__":
    _ = unittest.main()
