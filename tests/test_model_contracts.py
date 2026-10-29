"""Bounded, credential-free runtime inventory wire contracts."""

import json
import unittest

from scarcity_router.model_inventory import (
    MAX_MODELS_PER_SOURCE, MAX_SOURCES_PER_REPORT, MODEL_INVENTORY_SCHEMA_VERSION,
    DiscoveredModel, ModelInventoryError, ModelInventoryReport, SourceInventory,
    source_resource_id,
)

OBSERVED = "2026-09-24T12:00:00.000Z"


def source(source_id: str = "source", *, models: tuple[DiscoveredModel, ...] = ()) -> SourceInventory:
    return SourceInventory(source_id=source_id, adapter_id=f"codex:{source_id}",
                           kind="codex_subscription", observed_at=OBSERVED,
                           auth_state="authenticated", runtime_name="codex",
                           runtime_version="1", models=models)


class InventoryContractTests(unittest.TestCase):
    def test_model_shape_and_runtime_efforts(self) -> None:
        model = DiscoveredModel("synthetic", ("minimal", "ultra"))
        self.assertEqual({"slug": "synthetic", "reasoning_efforts": ["minimal", "ultra"]}, model.to_dict())
        with self.assertRaises(ModelInventoryError):
            _ = DiscoveredModel("not safe", ("high",))
        with self.assertRaises(ModelInventoryError):
            _ = DiscoveredModel("synthetic", ("high", "high"))
        with self.assertRaises(ModelInventoryError):
            _ = DiscoveredModel("synthetic", ("low",) * 9)

    def test_source_round_trip_order_bounds_and_closed_fields(self) -> None:
        entry = source(models=(DiscoveredModel("z", ()), DiscoveredModel("a", ())))
        self.assertEqual(("a", "z"), tuple(m.slug for m in entry.models))
        self.assertEqual(entry.to_dict(), SourceInventory.from_dict(entry.to_dict()).to_dict())
        with self.assertRaises(ModelInventoryError):
            _ = source(models=tuple(DiscoveredModel(f"m{i}", ()) for i in range(MAX_MODELS_PER_SOURCE + 1)))
        with self.assertRaises(ModelInventoryError):
            _ = source(models=(DiscoveredModel("same", ()), DiscoveredModel("same", ())))
        document = entry.to_dict()
        document["account_email"] = "synthetic@example.invalid"
        with self.assertRaises(ModelInventoryError):
            _ = SourceInventory.from_dict(document)

    def test_report_round_trip_and_fail_closed(self) -> None:
        report = ModelInventoryReport(worker_id="worker", sources=(source(),))
        document = report.to_dict()
        self.assertEqual(document, ModelInventoryReport.from_dict(document).to_dict())
        document["schema_version"] = MODEL_INVENTORY_SCHEMA_VERSION + 1
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport.from_dict(document)
        document = report.to_dict()
        document["raw_provider_payload"] = {}
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport.from_dict(document)
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport(worker_id="worker", sources=(source(), source()))
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport(worker_id="worker", sources=tuple(
                source(f"s{i}") for i in range(MAX_SOURCES_PER_REPORT + 1)
            ))
        wire = json.dumps(report.to_dict())
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport.parse(wire[:-1] + ', "worker_id":"other"}')
        with self.assertRaises(ModelInventoryError):
            _ = ModelInventoryReport.parse("not json")

    def test_resource_id_bounds(self) -> None:
        with self.assertRaises(ModelInventoryError):
            _ = DiscoveredModel("a" * 41, ("low",))
        self.assertEqual("b" * 35, DiscoveredModel("b" * 35, ("low",)).slug)
        with self.assertRaises(ValueError):
            _ = source_resource_id("a" * 20, "b" * 45)
        self.assertEqual("s:" + "c" * 40, source_resource_id("s", "c" * 40))

    def test_resource_id_components_are_unambiguous(self) -> None:
        self.assertEqual("s:model:tag:high", source_resource_id("s", "model:tag", "high"))
        with self.assertRaises(ModelInventoryError):
            _ = source_resource_id("s", "model:tag")
        with self.assertRaises(ModelInventoryError):
            _ = source_resource_id("s", "model", "tag:high")
        with self.assertRaises(ModelInventoryError):
            _ = source_resource_id("s:other", "model", "high")


if __name__ == "__main__":
    _ = unittest.main()
