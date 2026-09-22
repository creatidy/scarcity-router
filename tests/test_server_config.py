"""Administrator configuration-model tests (M09, issue #94).

The configuration document is the single source of truth and is
secret-free by construction; parsing is fail-closed. These tests pin the
neutral default, round-tripping, rejection behavior, the D-044 origin
discipline and router-loop refusal.
"""

from __future__ import annotations

import unittest
from typing import cast

from scarcity_router.routing_core import (
    AdministratorConstraints,
    ClientRoutingProfile,
)
from scarcity_router.server_config import (
    CONFIG_SCHEMA_VERSION,
    SERVER_DIRECT_DEFAULT_POLL_INTERVAL_SECONDS,
    ProviderEndpointConfig,
    ServerConfigError,
    ServerConfiguration,
)


def full_document() -> dict[str, object]:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "providers": [
            {
                "provider_id": "zai-http",
                "adapter_id": "openai_http",
                "base_url": "https://api.z.ai",
                "label": "Z.ai",
            }
        ],
        "resources": [
            {
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
                "enabled": True,
                "endpoint_id": "zai-http",
            }
        ],
        "aliases": {"deep-coding": {"profile_id": "deep_coding"}},
        "client_authorizations": {
            "client-a": {"allowed_providers": ["zai"]},
        },
        "admin_constraints": {"allowed_channels": ["server_direct_http"]},
        "limits": {"max_concurrent_executions": 2},
    }


class NeutralDefaultTests(unittest.TestCase):
    def test_neutral_configuration_is_empty_everywhere(self) -> None:
        """No author-private defaults: neutral ships empty, by test."""
        config = ServerConfiguration.neutral()
        self.assertEqual((), config.providers)
        self.assertEqual((), config.resources)
        self.assertEqual({}, config.aliases)
        self.assertEqual({}, config.client_authorizations)
        self.assertEqual(
            AdministratorConstraints().to_dict(),
            config.admin_constraints.to_dict(),
        )
        document = config.to_document()
        self.assertEqual({"schema_version": CONFIG_SCHEMA_VERSION}, document)
        # The document round-trips through the strict parser.
        self.assertEqual(
            document, ServerConfiguration.from_document(document).to_document()
        )


class RoundTripTests(unittest.TestCase):
    def test_full_document_round_trips(self) -> None:
        document = full_document()
        config = ServerConfiguration.from_document(document)
        self.assertEqual(1, len(config.providers))
        self.assertEqual("https://api.z.ai", config.providers[0].base_url)
        self.assertEqual(
            ("zai-plan-1",),
            tuple(
                registration.identity.resource_id
                for registration in config.enabled_registrations()
            ),
        )
        self.assertEqual(
            ClientRoutingProfile(profile_id="deep_coding").to_dict(),
            config.aliases["deep-coding"].to_dict(),
        )
        # The canonical form round-trips through the strict parser.
        canonical = config.to_document()
        self.assertEqual(
            canonical,
            ServerConfiguration.from_document(canonical).to_document(),
        )
        # Partial limit documents keep their stated value under defaults.
        self.assertEqual(2, config.limits.max_concurrent_executions)

    def test_disabled_resources_are_not_registered_but_kept(self) -> None:
        document = full_document()
        resources = cast("list[dict[str, object]]", document["resources"])
        resources[0]["enabled"] = False
        config = ServerConfiguration.from_document(document)
        self.assertEqual((), config.enabled_registrations())
        self.assertIsNotNone(config.resource_by_id("zai-plan-1"))


class FailClosedTests(unittest.TestCase):
    def test_unknown_keys_are_rejected(self) -> None:
        document = full_document()
        document["mystery"] = True
        with self.assertRaises(ValueError):
            _config = ServerConfiguration.from_document(document)
            _ = _config

    def test_wrong_schema_version_is_rejected(self) -> None:
        document = full_document()
        document["schema_version"] = CONFIG_SCHEMA_VERSION + 1
        with self.assertRaises(ValueError):
            _config = ServerConfiguration.from_document(document)
            _ = _config

    def test_duplicate_provider_ids_are_rejected(self) -> None:
        document = full_document()
        providers = cast("list[dict[str, object]]", document["providers"])
        providers.append(dict(providers[0]))
        with self.assertRaises(ServerConfigError):
            _config = ServerConfiguration.from_document(document)
            _ = _config

    def test_duplicate_resource_ids_are_rejected(self) -> None:
        document = full_document()
        resources = cast("list[dict[str, object]]", document["resources"])
        resources.append(dict(resources[0]))
        with self.assertRaises(ServerConfigError):
            _config = ServerConfiguration.from_document(document)
            _ = _config

    def test_unknown_endpoint_binding_is_rejected(self) -> None:
        document = full_document()
        resources = cast("list[dict[str, object]]", document["resources"])
        resources[0]["endpoint_id"] = "no-such-endpoint"
        with self.assertRaises(ServerConfigError):
            _config = ServerConfiguration.from_document(document)
            _ = _config

    def test_a_secret_key_in_the_document_is_rejected(self) -> None:
        """Secrets never enter the document: unknown keys fail closed."""
        document = full_document()
        providers = cast("list[dict[str, object]]", document["providers"])
        providers[0]["secret"] = "SYNTHETIC-SECRET"
        with self.assertRaises(ValueError):
            _config = ServerConfiguration.from_document(document)
            _ = _config

    def test_registration_violations_fail_closed(self) -> None:
        document = full_document()
        resources = cast("list[dict[str, object]]", document["resources"])
        registration = cast("dict[str, object]", resources[0]["registration"])
        identity = cast("dict[str, object]", registration["identity"])
        identity["channel"] = "teleportation"
        with self.assertRaises(ServerConfigError):
            _config = ServerConfiguration.from_document(document)
            _ = _config


class PollingDefaultTests(unittest.TestCase):
    """Server-side availability policy is authoritative (M01/U-003).

    Server-direct HTTP resources get the bounded default polling cadence
    so the server's readiness probes can observe them; an explicit
    administrator cadence always wins.
    """

    def test_server_direct_registration_gets_the_default_cadence(self) -> None:
        document = full_document()
        config = ServerConfiguration.from_document(document)
        resource = config.resource_by_id("zai-plan-1")
        assert resource is not None
        self.assertEqual(
            SERVER_DIRECT_DEFAULT_POLL_INTERVAL_SECONDS,
            resource.registration.poll_interval_seconds,
        )

    def test_explicit_cadence_is_preserved(self) -> None:
        document = full_document()
        resources = cast("list[dict[str, object]]", document["resources"])
        registration = cast("dict[str, object]", resources[0]["registration"])
        registration["poll_interval_seconds"] = 60
        config = ServerConfiguration.from_document(document)
        resource = config.resource_by_id("zai-plan-1")
        assert resource is not None
        self.assertEqual(60, resource.registration.poll_interval_seconds)


class ProviderOriginDisciplineTests(unittest.TestCase):
    def test_plain_http_off_loopback_is_refused(self) -> None:
        with self.assertRaises(ServerConfigError):
            _config = ProviderEndpointConfig(
                provider_id="acme",
                adapter_id="openai_http",
                base_url="http://api.acme.example",
            )
            _ = _config

    def test_loopback_http_and_https_are_allowed(self) -> None:
        local = ProviderEndpointConfig(
            provider_id="local-ollama",
            adapter_id="openai_http",
            base_url="http://127.0.0.1:11434",
        )
        remote = ProviderEndpointConfig(
            provider_id="acme",
            adapter_id="openai_http",
            base_url="https://api.acme.example",
        )
        _ = (local, remote)

    def test_urls_with_credentials_or_query_are_refused(self) -> None:
        for base_url in (
            "https://user:secret@api.acme.example",
            "https://api.acme.example/path?x=1",
            "not a url",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ServerConfigError):
                    _config = ProviderEndpointConfig(
                        provider_id="acme",
                        adapter_id="openai_http",
                        base_url=base_url,
                    )
                    _ = _config


class RouterLoopTests(unittest.TestCase):
    def test_own_origin_as_provider_is_refused(self) -> None:
        config = ServerConfiguration(
            providers=(
                ProviderEndpointConfig(
                    provider_id="self",
                    adapter_id="openai_http",
                    base_url="http://127.0.0.1:8787",
                ),
            ),
        )
        with self.assertRaises(ServerConfigError):
            config.validate_no_router_loop(("http://127.0.0.1:8787",))

    def test_other_origins_pass(self) -> None:
        config = ServerConfiguration(
            providers=(
                ProviderEndpointConfig(
                    provider_id="acme",
                    adapter_id="openai_http",
                    base_url="https://api.acme.example",
                ),
            ),
        )
        config.validate_no_router_loop(("http://127.0.0.1:8787",))


if __name__ == "__main__":
    _ = unittest.main()
