"""Offline validation of administrator-owned MI trust configuration."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import cast, override
from unittest import mock

from scarcity_router.control_api import ControlPlane, GenerationTester
from scarcity_router.mi_server_config import load_mi_server_config
from scarcity_router.mi_consumer import canonical
from scarcity_router.server_store import ServerStore, ServerStoreError
from scarcity_router.status import StatusCollectors
from tests import test_mi_consumer as fixture
from tests.server_fixtures import ServerHarness


def configuration(publication: Path) -> dict[str, object]:
    subject = asdict(fixture.SUBJECT)
    subject["configuration"] = dict(fixture.SUBJECT.configuration or ())
    return {
        "schema_version": 1,
        "publication": str(publication),
        "reference": asdict(fixture.encoded(fixture.public())[1]),
        "scope": {
            "scope_id": fixture.SCOPE.scope_id,
            "channel": fixture.SCOPE.channel,
            "configuration": dict(fixture.SCOPE.configuration or ()),
            "source_ids": sorted(fixture.SCOPE.source_ids),
        },
        "bindings": [{
            "identity": fixture.ENTRY.identity.to_dict(), "subject": subject,
            "dimension": fixture.BINDING.dimension, "meaning": fixture.BINDING.meaning,
            "basis": fixture.BINDING.basis,
        }],
        "max_bytes": fixture.BOUND,
    }


class MIServerConfigTests(unittest.TestCase):
    def test_explicit_trust_configuration_and_publication_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            publication = directory / "publication.json"
            _ = publication.write_bytes(fixture.encoded(fixture.public())[0])
            path = directory / "trust.json"
            _ = path.write_text(json.dumps(configuration(publication)))
            config = load_mi_server_config(path)
            config.consumer.load(config.publication, config.reference)
            cut, _ = config.consumer.capture()
            assert cut is not None
            self.assertEqual(config.reference, cut.reference)

    def test_duplicate_and_unbounded_configuration_refused_without_reflection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trust.json"
            for data in ('{"secret":"not echoed","secret":"again"}', " " * (256 * 1024 + 1)):
                _ = path.write_text(data)
                with self.assertRaisesRegex(ValueError, "^mi_server_configuration_invalid$"):
                    _ = load_mi_server_config(path)

    def test_scope_identity_channel_and_effort_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trust.json"
            document = configuration(Path(tmp) / "publication.json")
            document["scope"] = {"scope_id": "owned-scope", "channel": None,
                                 "configuration": {}, "source_ids": []}
            _ = path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "^mi_server_configuration_invalid$"):
                _ = load_mi_server_config(path)

    def test_each_binding_mismatch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trust.json"
            baseline = configuration(Path(tmp) / "publication.json")
            for field, value in (("provider_model_id", "other-model"), ("provider_id", "other"),
                                 ("channel", "other-channel"), ("configuration", {"reasoning_effort": "low"}),
                                 ("configuration", {"reasoning_effort": None})):
                document = copy.deepcopy(baseline)
                binding = cast(list[dict[str, object]], document["bindings"])[0]
                subject = cast(dict[str, object], binding["subject"])
                subject[field] = value
                _ = path.write_text(json.dumps(document))
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "^mi_server_configuration_invalid$"):
                    _ = load_mi_server_config(path)

    def test_activation_survives_restart_and_refuses_untrusted_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            publication = directory / "publication.json"
            data, _ = fixture.encoded(fixture.public())
            _ = publication.write_bytes(data)
            path = directory / "trust.json"
            _ = path.write_text(json.dumps(configuration(publication)))
            store = ServerStore.open(directory / "state")
            first = load_mi_server_config(path).activate(store)
            self.assertIsNotNone(first.admission(fixture.T).captured[0])
            store.close()
            store = ServerStore.open(directory / "state")
            second = load_mi_server_config(path).activate(store)
            self.assertEqual(first.reference, second.reference)
            _ = publication.write_bytes(b"untrusted replacement")
            with self.assertRaisesRegex(ValueError, "^mi_publication_activation_failed$"):
                _ = load_mi_server_config(path).activate(store)
            _ = publication.write_bytes(data)
            self.assertEqual(first.reference, load_mi_server_config(path).activate(store).reference)
            store.close()

    def test_retention_is_atomic_bounded_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ServerStore.open(Path(tmp) / "state")
            self.addCleanup(store.close)
            receipt_id = "1" * 64
            cut_id = hashlib.sha256(b"cut").hexdigest()
            store.retain_mi_receipt(receipt_id, b"receipt", cut_id, b"cut", "request-1", "client-1")
            store.retain_mi_receipt(receipt_id, b"receipt", cut_id, b"cut")
            self.assertEqual(receipt_id, store.get_mi_receipt_for_request("client-1", "request-1"))
            with self.assertRaises(ServerStoreError):
                store.retain_mi_receipt(receipt_id, b"different", cut_id, b"cut")
            with self.assertRaises(ServerStoreError):
                store.retain_mi_receipt("3" * 64, b"different", cut_id, b"cut", "request-1", "client-1")
            self.assertIsNone(store.get_mi_receipt("3" * 64))
            with mock.patch("scarcity_router.server_store._MI_RETAINED_BYTES", 12):
                with self.assertRaisesRegex(ServerStoreError, "storage limit"):
                    store.retain_mi_receipt("3" * 64, b"new", hashlib.sha256(b"other-cut").hexdigest(), b"other-cut")
            self.assertEqual(b"receipt", store.get_mi_receipt(receipt_id))
            self.assertIsNone(store.get_mi_receipt("3" * 64))
            self.assertIsNone(store.get_retained_mi_cut(hashlib.sha256(b"other-cut").hexdigest()))


class MIServerHTTPTests(ServerHarness):
    @override
    def make_plane(self, data_dir: Path, *, collectors: StatusCollectors | None = None,
                   generation_tester: GenerationTester | None = None) -> ControlPlane:
        store = ServerStore.open(data_dir)
        publication = data_dir / "publication.json"
        _ = publication.write_bytes(fixture.encoded(fixture.public())[0])
        path = data_dir / "trust.json"
        _ = path.write_text(json.dumps(configuration(publication)))
        mi = load_mi_server_config(path).activate(store)
        return ControlPlane(store=store, clock=lambda: fixture.T, collectors=collectors,
                            generation_tester=generation_tester, pbkdf2_iterations=1000,
                            mi_server=mi, mi_config_path=path)

    def test_authenticated_decision_retains_opaque_provenance_across_rebuild(self) -> None:
        self.onboard()
        request: dict[str, object] = {"requirement": {"task_level": "L0", "capability_minima": {}, "hard_constraints": {}}}
        status, _, _ = self.exchange("POST", "/v1/select", request, with_session=False)
        self.assertEqual(401, status)
        status, response, _ = self.exchange("POST", "/v1/select", request,
                                            headers={"Authorization": f"Bearer {self.client_key}"}, with_session=False)
        self.assertEqual(200, status)
        body = cast(dict[str, object], response)
        self.assertEqual("no_solution", cast(dict[str, object], body["route"])["status"])
        reference = cast(dict[str, str], body["mi_utilization"])
        receipt_id = reference["identifier"].removeprefix("urn:mi:utilization:sha256:")
        receipt = self.plane.store.get_mi_receipt(receipt_id)
        self.assertIsNotNone(receipt)
        second_store = ServerStore.open(self.data_dir)
        try:
            self.assertEqual(receipt, second_store.get_mi_receipt(receipt_id))
        finally:
            second_store.close()
        assert receipt is not None
        retained = cast(dict[str, object], json.loads(receipt))
        cut = self.plane.store.get_retained_mi_cut(cast(str, retained["cut_blob_sha256"]))
        assert cut is not None
        reference_data = cast(dict[str, object], retained["cut_reference"])
        from model_intelligence.publication import CutReference
        cut_ref = CutReference(cast(int, reference_data["format_version"]),
                               cast(str, reference_data["payload_schema"]), cast(str, reference_data["sha256"]))
        from model_intelligence.contract import decode_public_evidence
        from model_intelligence.utilization import decode_decision_utilization
        self.assertEqual(fixture.public(), decode_public_evidence(cut, cut_ref, max_bytes=fixture.BOUND))
        decision = base64.b64decode(cast(str, retained["decision"]))
        _ = decode_decision_utilization(base64.b64decode(cast(str, retained["utilization"])),
            response=canonical({"schema_version": 1, "decision": json.loads(decision), "mi_utilization": reference}),
            expected_cut=cut_ref, evaluated_at=fixture.T, decision=decision,
            context=base64.b64decode(cast(str, retained["context"])), max_bytes=fixture.BOUND)
        self.assertNotIn("context", body)

    def test_reload_requires_admin_and_preserves_captured_cut(self) -> None:
        self.onboard()
        server = self.plane.current_application().mi_server
        assert server is not None
        captured = server.admission(fixture.T)
        second_store = ServerStore.open(self.data_dir)
        self.addCleanup(second_store.close)
        second = load_mi_server_config(self.data_dir / "trust.json").activate(second_store)
        original = fixture.public()
        claim = original.evidence.claims[0]
        from model_intelligence.evidence import ModelClaim
        assert isinstance(claim, ModelClaim)
        revised = replace(original, evidence=original.evidence.extend(claims=(
            replace(claim, claim_id="new-claim", revises=claim.claim_id,
                    payload=replace(fixture.LIMIT, amount=Decimal(2048))),
        )))
        data, reference = fixture.encoded(revised)
        publication = self.data_dir / "publication.json"
        _ = publication.write_bytes(data)
        document = configuration(publication)
        document["reference"] = asdict(reference)
        _ = (self.data_dir / "trust.json").write_text(json.dumps(document))
        status, _, _ = self.exchange("POST", "/control/mi/reload", {},
            headers={"Authorization": f"Bearer {self.client_key}"}, with_session=False)
        self.assertEqual(401, status)
        status, _ = self.admin_post("/control/mi/reload", {})
        self.assertEqual(200, status)
        self.assertNotEqual(captured.expected, server.admission(fixture.T).expected)
        with self.assertRaisesRegex(ValueError, "mi_cut_changed_restart_or_reload"):
            _ = second.admission(fixture.T)
        self.assertEqual(fixture.encoded(original)[1], captured.expected)
        _ = publication.write_bytes(b"invalid")
        status, _ = self.admin_post("/control/mi/reload", {})
        self.assertEqual(400, status)
        self.assertEqual(reference, server.admission(fixture.T).expected)


if __name__ == "__main__":
    _ = unittest.main()
