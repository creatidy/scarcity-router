"""Representative-client boundary tests (program #132, child #133).

The 2026-09-26 first real acceptance test (ZCode → ``gpt-5.6-luna``)
passed a simple pinned smoke request but failed at five successive gates
once the real client request shape was sent. This module pins that
CURRENT behavior at the public HTTP boundary — the authenticated
execution server, not parser helpers — so the compatibility problems
cannot reappear independently and each program child flips exactly its
own pin:

- #134 flips the model-resolution pins (bare logical model, models list),
- #135 flips the reasoning-dialect pin,
- #136 flips the output-ceiling pin,
- #137 flips the client-tools pin.

The control tests prove the same harness executes the representative
shape today whenever the not-yet-supported fields are absent, and that
the strict exact-pin path keeps working throughout the program.
"""

from __future__ import annotations

import json
import threading
from typing import cast

from scarcity_router.gateway_server import GatewayHTTPServer, make_gateway_server
from scarcity_router.routing_core import CompatibilityCell
from tests.gateway_fixtures import (
    CLIENT_KEY,
    GatewayApplication,
    ScriptedAdapter,
    build_cells,
    make_application,
)
from tests.representative_client_fixture import (
    REPRESENTATIVE_MODEL,
    representative_request,
)
from tests.test_gateway_server import ServerHarness, as_dict, as_list

PINNED_LUNA_MAX = "sr-pin:openai-http/openai/gpt-5.6-luna/max"


class RepresentativeHarness(ServerHarness):
    """ServerHarness plus a cell-configured server for compatibility pins."""

    application: GatewayApplication
    server: GatewayHTTPServer
    thread: threading.Thread

    def make_server_with_cells(self, cells: tuple[CompatibilityCell, ...]) -> int:
        application = make_application(cells=cells, adapters=[ScriptedAdapter()])
        server = make_gateway_server(application, host="127.0.0.1", port=0)
        self.application = application
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        _, port = server.server_address[:2]
        return int(port)


class CurrentStateFailurePins(RepresentativeHarness):
    """One pin per observed acceptance failure; each child flips its own."""

    def test_reasoning_dialect_fields_are_currently_unknown_parameters(self) -> None:
        """FAILURE 2 of the acceptance report — flipped by child #135.

        The real client emitted ``thinking``, ``enable_thinking`` and
        ``reasoning`` alongside ``reasoning_effort``; the strict top-level
        allowlist currently rejects the request with
        ``unknown_parameter``.
        """
        port = self.make_server()
        response = self.post_chat(port, representative_request())
        self.assertEqual(response.status, 400)
        payload = cast("dict[str, object]", json.loads(response.read()))
        error = as_dict(payload["error"])
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["code"], "unknown_parameter")
        # Deterministic: the first extra key in sorted order.
        self.assertEqual(error["param"], "enable_thinking")

    def test_bare_logical_model_is_currently_not_resolvable(self) -> None:
        """FAILURE 4 of the acceptance report — flipped by child #134.

        A discovered, routable physical model cannot be selected by its
        normal OpenAI model id: resolution understands only administrator
        aliases and ``sr-pin:`` references, so the bare logical id is a
        404 ``model_not_found`` today.
        """
        port = self.make_server()
        response = self.post_chat(
            port,
            representative_request(
                include_reasoning_dialects=False, include_output_limit=False
            ),
        )
        self.assertEqual(response.status, 404)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(as_dict(payload["error"])["code"], "model_not_found")

    def test_models_listing_currently_exposes_aliases_only(self) -> None:
        """FAILURE 4 counterpart — flipped by child #134.

        ``GET /v1/models`` lists configured routing aliases only; adopted
        source-derived logical models are invisible to OpenAI-compatible
        discovery.
        """
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        ids = [as_dict(entry)["id"] for entry in as_list(payload["data"])]
        self.assertEqual(ids, ["deep-coding", "zai-only"])
        self.assertNotIn(REPRESENTATIVE_MODEL, ids)

    def test_representative_output_ceiling_is_currently_rejected(self) -> None:
        """FAILURE 3 of the acceptance report — flipped by child #136.

        The client's model-default ``max_completion_tokens: 128000``
        exceeds the hidden ``GatewayLimits.max_output_tokens = 16384``
        default even though the catalog evidences 128k output for the
        model; admission rejects instead of clipping.
        """
        port = self.make_server()
        response = self.post_chat(
            port, representative_request(include_reasoning_dialects=False)
        )
        self.assertEqual(response.status, 400)
        payload = cast("dict[str, object]", json.loads(response.read()))
        error = as_dict(payload["error"])
        self.assertEqual(error["code"], "output_limit_exceeded")
        self.assertEqual(error["param"], "max_completion_tokens")

    def test_pinned_request_with_client_tools_is_currently_incompatible(
        self,
    ) -> None:
        """FAILURE 5 of the acceptance report — the hard blocker, flipped
        by child #137 only to the evidenced level.

        With an exact pinned Codex-path target whose compatibility matrix
        marks ``tool_calls`` UNSUPPORTED (the shipped Codex evidence), the
        representative request fails closed with
        ``compatibility_unsupported``; the router never executes the
        client's tools. The output ceiling is absent here because the
        limits gate runs first (that ordering is exactly what the real
        acceptance test observed: failure 3 masks failure 5 until the
        operator lowers the ceiling).
        """
        cells = build_cells(
            overrides={
                ("server_direct_http", "openai", "gpt-5.6-luna", "tool_calls"):
                    "UNSUPPORTED",
            }
        )
        port = self.make_server_with_cells(cells)
        response = self.post_chat(
            port,
            representative_request(
                model=PINNED_LUNA_MAX,
                include_reasoning_dialects=False,
                include_output_limit=False,
            ),
        )
        self.assertEqual(response.status, 400)
        payload = cast("dict[str, object]", json.loads(response.read()))
        error = as_dict(payload["error"])
        self.assertEqual(error["code"], "compatibility_unsupported")


class RepresentativeControls(ServerHarness):
    """The harness executes; the strict exact-pin path keeps working."""

    def test_representative_shape_without_new_fields_executes(self) -> None:
        """Positive control: tools + streaming + usage, no dialects/limit.

        Proves the fixture harness end to end: the representative body
        minus the four reasoning fields and the output ceiling streams
        through the default surface today.
        """
        port = self.make_server()
        response = self.post_chat(
            port,
            representative_request(
                model="deep-coding",
                include_reasoning_dialects=False,
                include_output_limit=False,
            ),
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(
            response.getheader("Content-Type", "").startswith("text/event-stream")
        )
        frames = self.read_sse_frames(response)
        self.assertEqual(frames[-1], "[DONE]")
        chunks = [cast("dict[str, object]", json.loads(frame)) for frame in frames[:-1]]
        usage_chunks = [chunk for chunk in chunks if not as_list(chunk["choices"])]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(as_dict(usage_chunks[0]["usage"])["prompt_tokens"], 11)

    def test_canonical_reasoning_effort_max_with_alias_executes(self) -> None:
        """Control for the #135 flip: the canonical field already works.

        ``reasoning_effort: "max"`` alone (no dialect fields) parses,
        requires ``reasoning_controls`` and executes today; child #135
        must make the dialect-carrying request behave identically.
        """
        port = self.make_server()
        body = representative_request(
            model="deep-coding", include_output_limit=False
        )
        del body["thinking"]
        del body["enable_thinking"]
        del body["reasoning"]
        response = self.post_chat(port, body)
        self.assertEqual(response.status, 200)
        frames = self.read_sse_frames(response)
        self.assertEqual(frames[-1], "[DONE]")

    def test_exact_pin_remains_admissible_and_streams(self) -> None:
        """Regression guard: the proven ``SR LIVE OK`` strict path.

        The exact pinned reference — the auditable escape hatch that must
        survive the whole program unchanged — admits and streams through
        the default surface.
        """
        port = self.make_server()
        response = self.post_chat(
            port,
            {
                "model": PINNED_LUNA_MAX,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        self.assertEqual(response.status, 200)
        frames = self.read_sse_frames(response)
        self.assertEqual(frames[-1], "[DONE]")


if __name__ == "__main__":
    import unittest

    _ = unittest.main()
