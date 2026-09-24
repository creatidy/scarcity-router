"""Multi-source worker tests: independent Codex sources on ONE worker (D-053).

Deterministic and CI-safe: the fake App Server (tests/codex_fake_appserver.py)
stands in for the real runtime, exactly like the adapter suite. These tests
pin the D-053 guarantees: adapter KIND vs INSTANCE separation, per-source
controlled-home isolation, runtime-authoritative inventory, exact binding
against discovered models, and the version-2 inventory section on state
reports (absent on v1 sessions, validated and forwarded on v2).
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import override

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scarcity_router import worker_protocol  # noqa: E402
from scarcity_router.gateway_adapters import (  # noqa: E402
    AdapterCall,
    AdapterMessage,
)
from scarcity_router.model_inventory import ModelInventoryReport  # noqa: E402
from scarcity_router.resource_state import ResourceIdentity  # noqa: E402
from scarcity_router.selection_types import ModelIdentity  # noqa: E402
from scarcity_router.worker_client import (  # noqa: E402
    WorkerConfigError,
    build_registry,
)
from scarcity_router.worker_codex_adapter import (  # noqa: E402
    CodexLocalAdapter,
    source_resource_id,
)
from scarcity_router.worker_local_adapters import LocalAdapterRegistry  # noqa: E402
from tests.m10_codex_fixtures import FakeCodexSpawner  # noqa: E402

FAKE = Path(__file__).resolve().parent / "codex_fake_appserver.py"
_ = os.chmod(FAKE, 0o755)


def _bwrap_lookup(name: str) -> str | None:
    if name == "bwrap":
        return "/usr/bin/bwrap"
    return None


def _canonical(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _future_deadline(seconds: float = 30.0) -> str:
    return _canonical(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def _default_scenario() -> dict[str, object]:
    return {
        "init": "ok",
        "account": "chatgpt",
        "models": [
            {
                "id": "gpt-6-luna",
                "model": "gpt-6-luna",
                "displayName": "GPT-6 Luna",
                "hidden": False,
                "isDefault": False,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": e} for e in ("low", "medium", "high")
                ],
            },
            {
                "id": "gpt-6-sol",
                "model": "gpt-6-sol",
                "displayName": "GPT-6 Sol",
                "hidden": False,
                "isDefault": True,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")
                ],
            },
        ],
        "turn": {"deltas": ["Hi"], "usage": {"last": {"inputTokens": 3, "outputTokens": 2}}, "status": "completed"},
    }


class SourceAdapterTests(unittest.TestCase):
    def __init__(self, method_name: str = "runTest") -> None:
        self._tmps: list[TemporaryDirectory[str]] = []
        self._spawners: list[FakeCodexSpawner] = []
        super().__init__(method_name)

    @override
    def setUp(self) -> None:
        self._tmps = []
        self._spawners = []

    @override
    def tearDown(self) -> None:
        for tmp in self._tmps:
            tmp.cleanup()

    def _tmp(self) -> Path:
        tmp = TemporaryDirectory()
        self._tmps.append(tmp)
        return Path(tmp.name)

    def _adapter(
        self,
        source_id: str,
        state_dir: Path,
        scenario: dict[str, object] | None = None,
        **kwargs: object,
    ) -> CodexLocalAdapter:
        spawner = FakeCodexSpawner(
            scenario if scenario is not None else _default_scenario()
        )
        self._spawners.append(spawner)
        return CodexLocalAdapter(
            source_id=source_id,
            state_dir=state_dir,
            pinned_binary=FAKE,
            spawner=spawner,
            path_lookup=_bwrap_lookup,
            platform_name="linux",
            platform_release="6.x-generic",
            **kwargs,  # pyright: ignore[reportArgumentType] - typed keyword helper
        )

    def test_adapter_instance_id_is_kind_prefixed_per_source(self) -> None:
        state = self._tmp()
        a = self._adapter("personal-openai", state)
        b = self._adapter("second-openai", state)
        self.assertEqual("codex:personal-openai", a.adapter_id)
        self.assertEqual("codex:second-openai", b.adapter_id)
        # The legacy single-resource mode keeps the bare kind id.
        legacy = CodexLocalAdapter(
            resource=ResourceIdentity(
                resource_id="r",
                channel="worker_bridged",
                provider="openai",
                model="gpt-5.6-sol",
                entitlement="subscription_included",
            ),
            state_dir=state,
            pinned_binary=FAKE,
        )
        self.assertEqual("codex", legacy.adapter_id)

    def test_two_sources_register_on_one_worker_registry(self) -> None:
        state = self._tmp()
        registry = LocalAdapterRegistry()
        for source_id in ("personal-openai", "second-openai"):
            registry.register(self._adapter(source_id, state))
        self.assertEqual(
            ("codex:personal-openai", "codex:second-openai"),
            tuple(sorted(registry.adapter_ids())),
        )

    def test_controlled_homes_are_isolated_per_source(self) -> None:
        state = self._tmp()
        a = self._adapter("personal-openai", state)
        b = self._adapter("second-openai", state)
        _ = a.observe_inventory()
        _ = b.observe_inventory()
        home_a = state / "codex-sources" / "personal-openai" / "codex-home"
        home_b = state / "codex-sources" / "second-openai" / "codex-home"
        self.assertTrue(home_a.is_dir() and home_b.is_dir())
        self.assertNotEqual(home_a, home_b)
        for home in (home_a, home_b):
            mode = stat.S_IMODE(home.stat().st_mode)
            self.assertEqual(0o700, mode & 0o777)

    def test_inventory_observation_discovers_runtime_models(self) -> None:
        state = self._tmp()
        adapter = self._adapter("personal-openai", state)
        inventory = adapter.observe_inventory()
        self.assertEqual("authenticated", inventory.auth_state)
        self.assertEqual("codex:personal-openai", inventory.adapter_id)
        slugs = [model.slug for model in inventory.models]
        self.assertIn("gpt-6-luna", slugs)
        self.assertIn("gpt-6-sol", slugs)
        sol = next(m for m in inventory.models if m.slug == "gpt-6-sol")
        # The runtime is authoritative: efforts are exactly what the fake
        # runtime advertised (including ultra — no static vocabulary).
        self.assertIn("ultra", sol.reasoning_efforts)
        # Discovered models become served resources deterministically.
        self.assertEqual(
            (
                source_resource_id("personal-openai", "gpt-6-luna"),
                source_resource_id("personal-openai", "gpt-6-sol"),
            ),
            adapter.resource_ids,
        )

    def test_unauthenticated_source_reports_auth_required_with_no_models(self) -> None:
        state = self._tmp()
        adapter = self._adapter(
            "personal-openai", state, scenario=_default_scenario() | {"account": "none"}
        )
        inventory = adapter.observe_inventory()
        self.assertEqual("auth_required", inventory.auth_state)
        self.assertEqual((), inventory.models)
        self.assertEqual((), adapter.resource_ids)
        # And no execution can happen through an unauthenticated source.
        result = adapter.invoke(
            AdapterCall(
                resource=ResourceIdentity(
                    resource_id=source_resource_id("personal-openai", "gpt-6-sol"),
                    channel="worker_bridged",
                    provider="openai",
                    model="gpt-6-sol",
                    entitlement="subscription_included",
                ),
                model=ModelIdentity(provider="openai", model="gpt-6-sol", variant="high"),
                messages=(AdapterMessage(role="user", content="hi"),),
            ),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)

    def test_exact_binding_against_discovered_resources(self) -> None:
        state = self._tmp()
        adapter = self._adapter("personal-openai", state)
        _ = adapter.observe_inventory()
        _ = source_resource_id("personal-openai", "gpt-6-sol")
        # A resource the source did NOT discover is never served.
        result = adapter.invoke(
            AdapterCall(
                resource=ResourceIdentity(
                    resource_id=source_resource_id("personal-openai", "gpt-9-ghost"),
                    channel="worker_bridged",
                    provider="openai",
                    model="gpt-9-ghost",
                    entitlement="subscription_included",
                ),
                model=ModelIdentity(provider="openai", model="gpt-9-ghost", variant="high"),
                messages=(AdapterMessage(role="user", content="hi"),),
            ),
            cancel_event=threading.Event(),
            deadline=_future_deadline(),
            emit=lambda chunk: None,
        )
        self.assertEqual("failed", result.status)
        assert result.calls[0].note is not None
        self.assertEqual("resource_not_served", result.calls[0].note)

    def test_source_failure_isolation_between_two_sources(self) -> None:
        state = self._tmp()
        broken = self._adapter(
            "broken-source", state, scenario=_default_scenario() | {"init": "refuse"}
        )
        healthy = self._adapter("healthy-source", state)
        _ = broken.observe_inventory()
        _ = healthy.observe_inventory()
        broken_report = broken.inventory_report()
        healthy_report = healthy.inventory_report()
        assert broken_report is not None and healthy_report is not None
        self.assertEqual("unverified", broken_report.auth_state)
        self.assertEqual("authenticated", healthy_report.auth_state)

    def test_cadence_is_bounded_and_deterministic(self) -> None:
        state = self._tmp()
        adapter = self._adapter(
            "personal-openai", state, inventory_ttl_seconds=300.0
        )
        ticks = {"now": 1000.0}
        adapter._clock = lambda: ticks["now"]  # pyright: ignore[reportPrivateUsage] - test seam: the mutable test clock replaces the injected one
        _ = adapter.observe_inventory(now=ticks["now"])
        self.assertFalse(adapter.inventory_if_due())
        ticks["now"] += 299.0
        self.assertFalse(adapter.inventory_if_due())
        ticks["now"] += 1.0
        self.assertTrue(adapter.inventory_if_due())


class SourceRegistryBuildTests(unittest.TestCase):
    def test_build_registry_constructs_one_instance_per_source(self) -> None:
        with TemporaryDirectory() as tmp:
            registry = build_registry(
                {
                    "codex_sources": ["personal-openai", "second-openai"],
                    "codex_bin": str(FAKE),
                },
                state_dir=tmp,
            )
            assert registry is not None
            self.assertEqual(
                ("codex:personal-openai", "codex:second-openai"),
                tuple(sorted(registry.adapter_ids())),
            )

    def test_source_and_legacy_flags_are_mutually_exclusive(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(WorkerConfigError):
                _ = build_registry(
                    {
                        "codex_sources": ["a"],
                        "allow_codex": True,
                        "codex_model": "gpt-5.6-sol",
                        "codex_resource": "r",
                    },
                    state_dir=tmp,
                )

    def test_unsafe_source_id_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(WorkerConfigError):
                _ = build_registry(
                    {"codex_sources": ["Not A Source!"]}, state_dir=tmp
                )


class InventoryProtocolTests(unittest.TestCase):
    """The v2 inventory section on state reports (worker + server ends)."""

    def test_v2_message_carries_inventories_roundtrip(self) -> None:
        inventory = ModelInventoryReport(worker_id="w-1")
        message = worker_protocol.StateReportMessage(
            report={"schema_version": 1}, inventories=(inventory.to_dict(),)
        )
        payload = message.to_payload()
        self.assertIn("inventories", payload)
        parsed = worker_protocol.StateReportMessage.from_payload(payload)
        self.assertEqual(1, len(parsed.inventories))
        restored = ModelInventoryReport.from_dict(dict(parsed.inventories[0]))
        self.assertEqual("w-1", restored.worker_id)

    def test_v1_shape_stays_exactly_as_before(self) -> None:
        message = worker_protocol.StateReportMessage(report={"schema_version": 1})
        self.assertEqual(
            {"type": "state_report", "report": {"schema_version": 1}},
            message.to_payload(),
        )
        parsed = worker_protocol.StateReportMessage.from_payload(message.to_payload())
        self.assertEqual((), parsed.inventories)

    def test_oversized_inventory_section_fails_closed(self) -> None:
        payloads: list[dict[str, object]] = [
            {"schema_version": 1, "worker_id": f"w{i}", "sources": []} for i in range(9)
        ]
        with self.assertRaises(worker_protocol.WorkerProtocolError):
            _ = worker_protocol.StateReportMessage.from_payload(
                {"type": "state_report", "report": {}, "inventories": payloads}
            )

    def test_negotiation_accepts_v1_and_v2_preferring_v2(self) -> None:
        self.assertEqual(
            2,
            worker_protocol.negotiate_version((2, 1), (2,)),
        )
        self.assertEqual(
            1,
            worker_protocol.negotiate_version((2, 1), (1,)),
        )
        with self.assertRaises(worker_protocol.WorkerProtocolError):
            _ = worker_protocol.negotiate_version((2,), (1,))


if __name__ == "__main__":
    _ = unittest.main()
