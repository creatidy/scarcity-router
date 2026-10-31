"""Administrator-supplied MI trust configuration for the composed server.

The configuration and publication are distinct inputs: a digest embedded only
in a publication is never a trust anchor. Importing this module requires the
optional pinned MI extra; the default server does not import it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import cast

from model_intelligence.contract import Applicability
from model_intelligence.evidence import Model
from model_intelligence.publication import CutReference

from .mi_consumer import MIAdmission, MIConsumer, MIUse, ModelBinding, canonical
from .selection_types import ModelIdentity
from .server_store import ServerStore

_CONFIG_LIMIT = 256 * 1024


def _read_regular(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("not-regular")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("changed-file")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("changed-file")
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError("size")
    return data


def _object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid-object")
    result = cast(dict[str, object], value)
    if set(result) != set(keys):
        raise ValueError("invalid-object")
    return result


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("invalid-text")
    return value


def _configuration(value: object) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(value, dict):
        raise ValueError("invalid-configuration")
    values = cast(dict[str, object], value)
    if not values or len(values) > 64:
        raise ValueError("invalid-configuration")
    return tuple(sorted((_text(key), None if item is None else _text(item)) for key, item in values.items()))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate-key")
        result[key] = value
    return result


@dataclass(frozen=True)
class MIServerConfig:
    consumer: MIConsumer
    reference: CutReference
    publication: Path
    max_bytes: int

    def activate(self, store: ServerStore) -> MIServer:
        """Validate a candidate against the durable prior cut before publishing it."""
        try:
            data = _read_regular(self.publication, self.max_bytes)
            record = canonical({"reference": asdict(self.reference), "data": base64.b64encode(data).decode("ascii")})

            def validate(previous: bytes | None) -> None:
                if previous is not None:
                    old = _object(cast(object, json.loads(previous)), frozenset({"reference", "data"}))
                    ref = _object(old["reference"], frozenset({"format_version", "payload_schema", "sha256"}))
                    version = ref["format_version"]
                    if type(version) is not int:
                        raise ValueError("version")
                    previous_ref = CutReference(version, _text(ref["payload_schema"]), _text(ref["sha256"]))
                    encoded = old["data"]
                    if not isinstance(encoded, str) or len(encoded) > (self.max_bytes + 2) // 3 * 4:
                        raise ValueError("prior-cut-size")
                    previous_data = base64.b64decode(encoded, validate=True)
                    self.consumer.adopt(previous_data, previous_ref)
                self.consumer.adopt(data, self.reference)

            digest = hashlib.sha256(record).hexdigest()
            store.replace_mi_cut(record, digest, validate)
            return MIServer(self.consumer, self.reference, store, digest)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
            raise ValueError("mi_publication_activation_failed") from None


@dataclass
class MIServer:
    consumer: MIConsumer
    reference: CutReference
    store: ServerStore
    digest: str
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def admission(self, at: datetime) -> MIAdmission:
        with self._lock:
            if self.store.current_mi_cut_digest() != self.digest:
                raise ValueError("mi_cut_changed_restart_or_reload")
            return self.consumer.admission(self.reference, evaluated_at=at)

    def reload(self, path: Path) -> None:
        """Reload trusted files explicitly; failed updates keep the active cut."""
        with self._lock:
            replacement = load_mi_server_config(path).activate(self.store)
            self.consumer = replacement.consumer
            self.reference = replacement.reference
            self.digest = replacement.digest

    def retain(self, use: MIUse, request_id: str | None = None, client_id: str | None = None) -> dict[str, object]:
        """Keep private inputs in the protected store, return only the reference."""
        decision = cast(dict[str, object], json.loads(use.decision))
        _ = use.envelope(decision)
        cut_data = use.cut.data if use.cut is not None else None
        cut_id = hashlib.sha256(cut_data).hexdigest() if cut_data is not None else None
        payload = canonical({
            "cut_blob_sha256": cut_id,
            "cut_reference": asdict(use.cut.reference) if use.cut is not None else None,
            "evaluated_at": use.evaluated_at.isoformat(),
            "context": base64.b64encode(use.context).decode("ascii"),
            "decision": base64.b64encode(use.decision).decode("ascii"),
            "utilization": base64.b64encode(use.utilization).decode("ascii"),
        })
        self.store.retain_mi_receipt(hashlib.sha256(use.utilization).hexdigest(), payload, cut_id, cut_data, request_id, client_id)
        return use.reference.to_dict()


def load_mi_server_config(path: Path) -> MIServerConfig:
    """Load separately trusted, strictly bounded administrator configuration."""
    try:
        raw = _read_regular(path, _CONFIG_LIMIT)
        value = cast(object, json.loads(raw, object_pairs_hook=_unique_object))
        root = _object(value, frozenset({"schema_version", "publication", "reference", "scope", "bindings", "max_bytes"}))
        if type(root["schema_version"]) is not int or root["schema_version"] != 1:
            raise ValueError("schema")
        reference_data = _object(root["reference"], frozenset({"format_version", "payload_schema", "sha256"}))
        version = reference_data["format_version"]
        if type(version) is not int:
            raise ValueError("version")
        reference = CutReference(version, _text(reference_data["payload_schema"]), _text(reference_data["sha256"]))
        scope_data = _object(root["scope"], frozenset({"scope_id", "channel", "configuration", "source_ids"}))
        sources = scope_data["source_ids"]
        if not isinstance(sources, list):
            raise ValueError("sources")
        source_values = cast(list[object], sources)
        if not source_values or len(source_values) > 128:
            raise ValueError("sources")
        source_ids = tuple(_text(source) for source in source_values)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("duplicate-source")
        scope = Applicability(_text(scope_data["scope_id"]), _text(scope_data["channel"]),
                              _configuration(scope_data["configuration"]), frozenset(source_ids))
        raw_bindings = root["bindings"]
        if not isinstance(raw_bindings, list):
            raise ValueError("bindings")
        binding_values = cast(list[object], raw_bindings)
        if not binding_values or len(binding_values) > 1024:
            raise ValueError("bindings")
        bindings: list[ModelBinding] = []
        for raw_binding in binding_values:
            item = _object(raw_binding, frozenset({"identity", "subject", "dimension", "meaning", "basis"}))
            identity_data = _object(item["identity"], frozenset({"provider", "model", "variant"}))
            identity = ModelIdentity(_text(identity_data["provider"]), _text(identity_data["model"]), _text(identity_data["variant"]))
            subject_data = _object(item["subject"], frozenset({"model_id", "provider_id", "provider_model_id", "physical_model_id", "revision", "channel", "interface_id", "interface_provider_id", "interface_version", "configuration"}))
            def optional(name: str) -> str | None:
                return None if subject_data[name] is None else _text(subject_data[name])
            subject = Model(_text(subject_data["model_id"]), _text(subject_data["provider_id"]),
                _text(subject_data["provider_model_id"]), physical_model_id=optional("physical_model_id"),
                revision=optional("revision"), channel=_text(subject_data["channel"]),
                interface_id=optional("interface_id"), interface_provider_id=optional("interface_provider_id"),
                interface_version=optional("interface_version"), configuration=_configuration(subject_data["configuration"]))
            if (subject.provider_id != identity.provider or subject.provider_model_id != identity.model
                or subject.channel != scope.channel or subject.configuration != scope.configuration
                or not dict(subject.configuration or ()).get("reasoning_effort")):
                raise ValueError("binding-mismatch")
            bindings.append(ModelBinding(identity, subject, _text(item["dimension"]), _text(item["meaning"]), _text(item["basis"])))
        max_bytes = root["max_bytes"]
        if type(max_bytes) is not int:
            raise ValueError("size")
        consumer = MIConsumer(expected_scope=scope, bindings=tuple(bindings), max_bytes=max_bytes)
        publication = Path(_text(root["publication"]))
        if not publication.is_absolute():
            raise ValueError("publication-path")
        return MIServerConfig(consumer, reference, publication, max_bytes)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("mi_server_configuration_invalid") from None
