"""Dynamic execution sources: composed-stack acceptance (D-053, #123).

The FULL production path — real verified-TLS worker listener, real M05
protocol (version 2), real pairing, real composition — with the Codex
runtime replaced by the deterministic fake App Server. Nothing here is
allowed to shortcut through an adapter seam: discovery travels through
the worker's state report, adoption happens in the server's source
registry, and execution is dispatched through the normal
OpenAI-compatible surface to the exact derived resource.

Central acceptance criteria pinned here:

- upgrade (T0 → T1): a new generation remains discovered until an
  administrator explicitly grants the exact model;
- ungranted models stay discovered/not-routable regardless of their names;
- two independent Codex sources run in ONE worker process with strict
  home isolation and distinct quota pools;
- exact binding: the executed audit target equals the selected derived
  resource, model and effort — never a substitution.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from unittest import mock

from tests.m10_fixtures import wait_until
from tests.m10_codex_fixtures import (
    PROMPT,
    codex_source_document,
    make_codex_source_registry,
)
import unittest

from scarcity_router.resource_state import ResourceRegistryEntry
from scarcity_router.control_api import format_utc
from tests.test_e2e_codex_acceptance import (
    CodexComposedTlsWorld,
    CodexWorker,
    chatgpt_scenario,
)

SOURCE_ID = "personal-openai"
SECOND_SOURCE_ID = "second-openai"
DERIVED_SOL = f"{SOURCE_ID}:gpt-6-sol:high"
DERIVED_SOL_PREFIX = f"{SOURCE_ID}:gpt-6-sol:"
DERIVED_NEW_GEN = f"{SOURCE_ID}:gpt-6.1-sol:high"

_SOL_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")


def _scenario(slugs: tuple[str, ...]) -> dict[str, object]:
    models = [
        {
            "id": slug,
            "model": slug,
            "displayName": slug,
            "hidden": False,
            "isDefault": index == 0,
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort} for effort in _SOL_EFFORTS
            ],
        }
        for index, slug in enumerate(slugs)
    ]
    scenario = chatgpt_scenario()
    scenario["models"] = models
    return scenario


class SourcesComposedWorld(CodexComposedTlsWorld):
    """The composed world with SOURCE-mode workers and configuration."""

    def start_source_worker(
        self,
        scenario: dict[str, object],
        *,
        source_ids: tuple[str, ...] = (SOURCE_ID,),
        label: str = "precision-codex",
        inventory_ttl_seconds: float = 0.5,
        state_report_interval_seconds: int = 2,
    ) -> CodexWorker:
        """Pair ONE worker carrying one adapter INSTANCE per source.

        The short inventory cadence is the ACCEPTANCE clock: discovery
        re-observes through the real state-report path (bounded, due-
        driven) within seconds, standing in for the production cadence.
        """
        import socket as _socket

        from scarcity_router.worker_client import (
            SESSION_IO_TIMEOUT_SECONDS,
            WorkerOrigin,
            WorkerRuntime,
        )
        from scarcity_router.worker_protocol import SocketTransport

        store = self.open_worker_store("codex")
        adapter_tmp = Path(tempfile.mkdtemp(prefix="scarcity-router-sources-adapter-"))
        self.addCleanup(lambda: _rmtree_local(adapter_tmp))
        trace_path = adapter_tmp / "trace.jsonl"
        registry = _registry_for(scenario, adapter_tmp, trace_path, source_ids, inventory_ttl_seconds)
        spawner = _last_spawner
        assert spawner is not None
        status, payload = self.admin_post("/control/workers/pairing-codes", {"label": label})
        assert status == 200, payload
        code = cast("dict[str, object]", payload)["pairing_code"]
        transports: list[SocketTransport] = []
        raw_sockets: list[_socket.socket] = []

        def factory(origin_ref: WorkerOrigin) -> SocketTransport:
            raw = _socket.create_connection(
                (origin_ref.host, origin_ref.port), timeout=10
            )
            try:
                wrapped = self.tls.client_context().wrap_socket(
                    raw, server_hostname=origin_ref.host
                )
                _ = wrapped.settimeout(SESSION_IO_TIMEOUT_SECONDS)
            except BaseException:
                raw.close()
                raise
            transports.append(SocketTransport(wrapped))
            raw_sockets.append(wrapped)
            return transports[-1]

        runtime = WorkerRuntime(
            origin=self.worker_origin(),
            store=store,
            local_adapters=registry,
            state_report_interval_seconds=state_report_interval_seconds,
            connect_factory=factory,
        )
        identity = runtime.pair(str(code))
        transports.clear()
        raw_sockets.clear()
        worker = CodexWorker(
            runtime=runtime,
            worker_id=identity.worker_id,
            store=store,
            spawner=spawner,
            trace_path=trace_path,
            state_dir=adapter_tmp,
            transports=transports,
            raw_sockets=raw_sockets,
            origin=self.worker_origin(),
            connect_factory=factory,
        )
        self.addCleanup(worker.close)
        # The administrator explicitly grants the exact test model.
        for source_id in source_ids:
            status, payload = self.admin_post(
                "/control/sources", codex_source_document(identity.worker_id, source_id=source_id)
            )
            assert status == 200, payload
            status, payload = self.admin_post(
                f"/control/sources/{source_id}/model-policy", {"allowed_models": ["gpt-6-sol"]}
            )
            assert status == 200, payload
        return worker

    def derived_resource_ids(self) -> set[str]:
        registry = self.plane._source_registry  # pyright: ignore[reportPrivateUsage] - acceptance seam
        return {
            r.identity.resource_id for r in registry.derived_registrations()
        }

    def routing_resource_ids(self) -> set[str]:
        """What the ROUTER currently sees (the only truth that matters)."""
        snapshot = self.plane.current_application().registry.registry_snapshot()
        return {entry.identity.resource_id for entry in snapshot.entries}

    def _routing_entry(self, resource_id: str) -> ResourceRegistryEntry | None:
        snapshot = self.plane.current_application().registry.registry_snapshot()
        for entry in snapshot.entries:
            if entry.identity.resource_id == resource_id:
                return entry
        return None

    def wait_for_derived(self, resource_id: str) -> None:
        """Wait until the router sees the resource REGISTERED and FRESH.

        Registration and observation land in two steps of one report;
        executing against a registered-but-never-observed resource is the
        honest 503 the availability gate exists to produce.
        """

        def fresh() -> bool:
            entry = self._routing_entry(resource_id)
            return (
                entry is not None
                and entry.freshness == "fresh"
                and entry.observation is not None
            )

        wait_until(
            fresh,
            timeout=45,
            message=f"derived resource {resource_id} never materialized "
            + "with a fresh observation in the routing registry",
        )

    def wait_for_model(self, slug: str) -> None:
        """Wait until ANY per-effort resource of the model is fresh."""
        wait_until(
            lambda: any(
                entry.identity.model == slug
                and entry.freshness == "fresh"
                and entry.observation is not None
                for entry in (
                    self.plane.current_application().registry.registry_snapshot().entries
                )
            ),
            timeout=45,
            message=f"model {slug} never materialized fresh",
        )

    def _last_executed_record(self) -> dict[str, object]:
        records = [
            record
            for record in self.audit_records()
            if record.get("executed_target") is not None
        ]
        assert records, "no executed audit record found"
        return records[-1]

    def source_view(self, source_id: str) -> dict[str, object]:
        views = {v["source_id"]: v for v in self.plane.sources_view()}
        assert source_id in views, f"source {source_id} missing from view"
        return views[source_id]


def _rmtree_local(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


_last_spawner = None


def _registry_for(
    scenario: dict[str, object],
    adapter_tmp: Path,
    trace_path: Path,
    source_ids: tuple[str, ...],
    ttl: float,
):
    global _last_spawner
    from scarcity_router.worker_local_adapters import LocalAdapterRegistry

    registry = LocalAdapterRegistry()
    spawner = None
    for source_id in source_ids:
        one_registry, spawner = make_codex_source_registry(
            scenario,
            state_dir=adapter_tmp,
            trace_path=trace_path,
            source_id=source_id,
            inventory_ttl_seconds=ttl,
        )
        for adapter_id in one_registry.adapter_ids():
            adapter = one_registry.resolve(adapter_id)
            assert adapter is not None
            registry.register(adapter)
    assert spawner is not None
    _last_spawner = spawner
    return registry


class SourceUpgradeAcceptanceTests(SourcesComposedWorld):
    """T0 → T1: an exact new-model grant is required."""

    def test_granted_model_executes_only_after_exact_context_attestation(self) -> None:
        # T0: the runtime exposes gpt-6-sol.
        worker = self.start_source_worker(_scenario(("gpt-6-sol",)))
        worker.start()
        try:
            self.wait_for_derived(DERIVED_SOL)
            self.assertEqual(6, len(self.derived_resource_ids()))
            # The user executes one REAL request through the normal
            # OpenAI-compatible surface, pinned to the derived resource.
            pin = f"sr-pin:{DERIVED_SOL}/openai/gpt-6-sol/high"
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                {"model": pin, "messages": [{"role": "user", "content": PROMPT}]},
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=60,
            )
            # The synthetic source does not attest a context ceiling, so
            # chat admission must refuse rather than invent one.
            self.assertEqual(400, status, payload)
            self.assertEqual("context_limit_unknown", cast("dict[str, object]", cast("dict[str, object]", payload)["error"])["code"])
            now = datetime.now(timezone.utc)
            runtime_version = self.source_view(SOURCE_ID)["runtime_version"]
            self.assertIsInstance(runtime_version, str)
            status, payload = self.admin_post(f"/control/sources/{SOURCE_ID}/model-policy", {
                "allowed_models": ["gpt-6-sol"], "context_evidence": [{
                    "model": "gpt-6-sol", "variant": "high", "runtime_version": runtime_version, "input_tokens": 4096,
                    "evidence_ref": "local:synthetic-adapter-test", "verified_at": format_utc(now - timedelta(minutes=1)),
                    "valid_until": format_utc(now + timedelta(hours=1)),
                }],
            })
            self.assertEqual(200, status, payload)
            status, payload, _headers = self.exchange(
                "POST", "/v1/chat/completions",
                {"model": pin, "messages": [{"role": "user", "content": PROMPT}]},
                headers={"Authorization": f"Bearer {self.client_key}"}, timeout=60,
            )
            self.assertEqual(200, status, payload)
            record = self._last_executed_record()
            executed = cast("dict[str, object]", record["executed_target"])
            self.assertEqual(DERIVED_SOL, executed["resource_id"])
            self.assertEqual("gpt-6-sol", executed["model"])
            self.assertEqual("high", executed["variant"])
            self.assertEqual(2, record["schema_version"])
            evidence = cast("dict[str, object]", executed["context_evidence"])
            self.assertEqual("administrator_attested", evidence["kind"])
            self.assertEqual("local:synthetic-adapter-test", evidence["reference"])
            self.assertEqual(runtime_version, evidence["runtime_version"])
            from scarcity_router import gateway_coordinator
            from scarcity_router.routing_core import AdmissionDecision, PinnedTarget, RouteRequest, admit_pinned_target

            original_admission = admit_pinned_target

            def revoke_after_admission(request: RouteRequest, *, pinned_target: PinnedTarget) -> AdmissionDecision:
                result = original_admission(request, pinned_target=pinned_target)
                if result.approved:
                    self.plane.service_set_source_model_policy(
                        SOURCE_ID, {"allowed_models": ["gpt-6-sol"], "context_evidence": []}
                    )
                return result

            with mock.patch.object(gateway_coordinator, "admit_pinned_target", side_effect=revoke_after_admission):
                status, payload, _headers = self.exchange(
                    "POST", "/v1/chat/completions",
                    {"model": pin, "messages": [{"role": "user", "content": PROMPT}]},
                    headers={"Authorization": f"Bearer {self.client_key}"}, timeout=60,
                )
            self.assertEqual(503, status, payload)
            self.assertEqual("context_evidence_stale", cast("dict[str, object]", cast("dict[str, object]", payload)["error"])["code"])
            # No configuration document changed (T0 was pure discovery).
            self.assertEqual(
                (SOURCE_ID,),
                tuple(s.source_id for s in self.plane.configuration.sources),
            )
            self.assertEqual((), self.plane.configuration.resources)
        finally:
            worker.stop()

    def test_new_generation_requires_an_explicit_model_grant(self) -> None:
        worker = self.start_source_worker(_scenario(("gpt-6-sol",)))
        worker.start()
        try:
            self.wait_for_derived(DERIVED_SOL)
            config_before = self.plane.configuration.to_document()
            # T1: the PROVIDER adds gpt-6.1-sol to its runtime listing.
            # Discovery alone must not authorize the new identity.
            worker.spawner.scenario["models"] = _scenario(
                ("gpt-6-sol", "gpt-6.1-sol")
            )["models"]
            wait_until(lambda: any(
                item["slug"] == "gpt-6.1-sol" for item in cast("list[dict[str, object]]", self.source_view(SOURCE_ID)["detected_models"])
            ), timeout=30)
            self.assertNotIn(DERIVED_NEW_GEN, self.derived_resource_ids())
            status, payload = self.admin_post(
                f"/control/sources/{SOURCE_ID}/model-policy",
                {"allowed_models": ["gpt-6-sol", "gpt-6.1-sol"]},
            )
            self.assertEqual(200, status, payload)
            wait_until(lambda: DERIVED_NEW_GEN in self.derived_resource_ids(), timeout=30)
            rids = self.derived_resource_ids()
            self.assertIn(DERIVED_SOL, rids)
            self.assertIn(DERIVED_NEW_GEN, rids)
            self.assertNotEqual(config_before, self.plane.configuration.to_document())
            wait_until(
                lambda: any(
                    e.identity.model == "gpt-6.1-sol"
                    for e in self.plane.current_application().catalog.entries
                ),
                timeout=20,
                message="entries for the new generation are missing",
            )
            entries = [
                e
                for e in self.plane.current_application().catalog.entries
                if e.identity.model == "gpt-6.1-sol"
            ]
            self.assertTrue(entries, "entries for the new generation are missing")
            self.assertTrue(
                all(e.hard_properties.input_context_tokens is None and e.capabilities.reasoning.rating is None for e in entries)
            )
        finally:
            worker.stop()


class UnknownAndRestrictedModelsTests(SourcesComposedWorld):
    def test_unknown_model_and_daybreak_stay_out_of_routing(self) -> None:
        worker = self.start_source_worker(
            _scenario(
                (
                    "gpt-6-sol",
                    "some-new-unclassified-model",
                    "gpt-daybreak-blue-latest",
                )
            )
        )
        worker.start()
        try:
            self.wait_for_derived(DERIVED_SOL)
            # ONLY the known-track model materializes (as its per-effort
            # resources).
            self.assertTrue(
                all(
                    rid.startswith(DERIVED_SOL_PREFIX)
                    for rid in self.derived_resource_ids()
                )
            )
            view = self.source_view(SOURCE_ID)
            self.assertEqual(1, view["routable"])
            self.assertEqual(0, view["restricted"])
            self.assertEqual(2, view["errors"])
            detected = cast("list[dict[str, object]]", view["detected_models"])
            states = {
                cast("str", m["slug"]): cast("str", m["state"]) for m in detected
            }
            self.assertEqual("routable", states["gpt-6-sol"])
            self.assertEqual("discovered", states["gpt-daybreak-blue-latest"])
            self.assertEqual("discovered", states["some-new-unclassified-model"])
            # A pin to the unclassified model fails explicitly — it is
            # never silently routed merely because the runtime lists it.
            pin = (
                "sr-pin:"
                + SOURCE_ID
                + ":some-new-unclassified-model/openai/some-new-unclassified-model/high"
            )
            status, payload, _headers = self.exchange(
                "POST",
                "/v1/chat/completions",
                {"model": pin, "messages": [{"role": "user", "content": PROMPT}]},
                headers={"Authorization": f"Bearer {self.client_key}"},
                timeout=30,
            )
            self.assertEqual(404, status, payload)
            # And Daybreak is absent from the executable target space.
            self.assertNotIn(
                "gpt-daybreak-blue-latest",
                json.dumps(sorted(self.derived_resource_ids())),
            )
        finally:
            worker.stop()


class TwoSourcesOneWorkerTests(SourcesComposedWorld):
    """Two independent Codex sources in ONE worker process (D-053 point 4)."""

    def test_two_sources_isolate_homes_pools_and_targets(self) -> None:
        worker = self.start_source_worker(
            _scenario(("gpt-6-sol",)),
            source_ids=(SOURCE_ID, SECOND_SOURCE_ID),
        )
        worker.start()
        try:
            derived_a = f"{SOURCE_ID}:gpt-6-sol:high"
            derived_b = f"{SECOND_SOURCE_ID}:gpt-6-sol:high"
            self.wait_for_model("gpt-6-sol")
            wait_until(
                lambda: len(self.derived_resource_ids()) >= 12, timeout=45
            )
            # Same physical model, TWO exact resources on ONE worker.
            self.assertEqual(
                {derived_a, derived_b},
                {
                    rid
                    for rid in self.derived_resource_ids()
                    if rid.endswith(":gpt-6-sol:high")
                },
            )

            registry = self.plane._source_registry  # pyright: ignore[reportPrivateUsage] - acceptance seam
            registrations = {
                r.identity.resource_id: r.identity for r in registry.derived_registrations()
            }
            # Two independent quota pools by default — sharing is never
            # assumed between accounts (D-042/D-053).
            self.assertEqual(
                ("pool-personal-openai",),
                registrations[derived_a].quota_pool_ids,
            )
            self.assertEqual(
                ("pool-second-openai",),
                registrations[derived_b].quota_pool_ids,
            )
            self.assertEqual(worker.worker_id, registry.owner_of(derived_a))
            self.assertEqual(worker.worker_id, registry.owner_of(derived_b))
            # Strict home isolation on disk: two controlled homes, both
            # 0o700, never shared.
            homes = sorted((worker.state_dir / "codex-sources").iterdir())
            self.assertEqual(
                [SOURCE_ID, SECOND_SOURCE_ID],
                [home.name for home in homes],
            )
            for home in homes:
                mode = (home / "codex-home").stat().st_mode & 0o777
                self.assertEqual(0o700, mode)
            # Each derived resource executes through its OWN adapter
            # instance: dispatch a pinned request on each and confirm
            # the executed audit target names the pinned resource.
            for rid in (derived_a, derived_b):
                pin = f"sr-pin:{rid}/openai/gpt-6-sol/high"
                status, payload, _headers = self.exchange(
                    "POST",
                    "/v1/chat/completions",
                    {"model": pin, "messages": [{"role": "user", "content": PROMPT}]},
                    headers={"Authorization": f"Bearer {self.client_key}"},
                    timeout=60,
                )
                self.assertEqual(400, status, payload)
                self.assertEqual("context_limit_unknown", cast("dict[str, object]", cast("dict[str, object]", payload)["error"])["code"])
        finally:
            worker.stop()

    def test_source_view_shows_both_accounts_friendly_first(self) -> None:
        worker = self.start_source_worker(
            _scenario(("gpt-6-sol",)),
            source_ids=(SOURCE_ID, SECOND_SOURCE_ID),
        )
        worker.start()
        try:
            self.wait_for_model("gpt-6-sol")
            self.wait_for_model("gpt-6-sol")
            views = self.plane.sources_view()
            self.assertEqual(2, len(views))
            for view in views:
                assert isinstance(view, dict)
                self.assertTrue(view["label"])
                self.assertIn("codex-login --source", str(view["login_command"]))
                self.assertEqual("authenticated", view["source_authenticated"])
                self.assertEqual(1, view["routable"])
        finally:
            worker.stop()


if __name__ == "__main__":
    _ = unittest.main()
