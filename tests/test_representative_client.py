"""Representative-client boundary tests (program #132, child #133).

The 2026-09-26 first real acceptance test (ZCode → ``gpt-5.6-luna``)
passed a simple pinned smoke request but failed at five successive gates
once the real client request shape was sent. This module pins that
CURRENT behavior at the public HTTP boundary — the authenticated
execution server, not parser helpers — so the compatibility problems
cannot reappear independently and each program child flips exactly its
own pin:

- #134 flips the model-resolution pins (bare logical model, models list),
- #135 FIXED the reasoning-dialect pin (dialects normalize to max),
- #136 FIXED the output-ceiling pin (honest defaults + effective limits),
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
    audit_records,
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
    """One test per observed acceptance failure; each child flips its own.

    Flipped so far: #134 (model resolution), #135 (reasoning dialects),
    #136 (output ceiling / effective limits). Still pinned as
    current-state: #137 (client tools, only to the evidenced level).
    """

    def test_reasoning_dialect_fields_normalize_and_execute(self) -> None:
        """FAILURE 2 of the acceptance report — FIXED by child #135.

        The real client emitted ``thinking``, ``enable_thinking`` and
        ``reasoning`` alongside ``reasoning_effort``; the bounded
        normalization layer folds them into the single canonical ``max``
        intent and the request streams exactly like the canonical
        control (parser semantics live in tests/test_reasoning_dialects.py).
        """
        port = self.make_server()
        body = representative_request(model="deep-coding", include_output_limit=False)
        self.assertEqual(body["thinking"], {"type": "enabled"})
        response = self.post_chat(port, body)
        self.assertEqual(response.status, 200)
        frames = self.read_sse_frames(response)
        self.assertEqual(frames[-1], "[DONE]")
        chunks = [cast("dict[str, object]", json.loads(frame)) for frame in frames[:-1]]
        usage_chunks = [chunk for chunk in chunks if not as_list(chunk["choices"])]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(as_dict(usage_chunks[0]["usage"])["prompt_tokens"], 11)

    def test_bare_logical_model_resolves_to_the_exact_identity(self) -> None:
        """FAILURE 4 of the acceptance report — FIXED by child #134 (D-055).

        A discovered, routable physical model is selected by its normal
        OpenAI model id: the bare logical id resolves to the EXACT
        ``(provider, model, effort)`` identity, routes among the resources
        that provide exactly it, and the audit shows selected == executed.
        """
        port = self.make_server()
        body = representative_request(
            include_reasoning_dialects=False, include_output_limit=False
        )
        body["reasoning_effort"] = "max"
        response = self.post_chat(port, body)
        self.assertEqual(response.status, 200)
        frames = self.read_sse_frames(response)
        self.assertEqual(frames[-1], "[DONE]")
        audit = audit_records(self.application)[-1]
        self.assertEqual(audit.selected_target, audit.executed_target)
        selected = audit.selected_target
        assert selected is not None
        self.assertEqual(selected.provider, "openai")
        self.assertEqual(selected.model, REPRESENTATIVE_MODEL)
        self.assertEqual(selected.variant, "max")

    def test_genuinely_unknown_model_remains_model_not_found(self) -> None:
        """The other half of the #134 contract: resolution never guesses."""
        port = self.make_server()
        response = self.post_chat(
            port,
            {
                "model": "never-heard-of-it",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(response.status, 404)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(as_dict(payload["error"])["code"], "model_not_found")

    def test_models_listing_exposes_aliases_and_adopted_logical_models(self) -> None:
        """FAILURE 4 counterpart — FIXED by child #134 (D-055).

        ``GET /v1/models`` lists the configured aliases first, then the
        adopted logical models bound to registered resources, each with
        the additive ``x_scarcity_router`` metadata.
        """
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        entries = [as_dict(entry) for entry in as_list(payload["data"])]
        ids = [entry["id"] for entry in entries]
        self.assertEqual(ids, ["deep-coding", "zai-only", "glm-5.3", "gpt-5.6-luna"])
        luna = next(entry for entry in entries if entry["id"] == REPRESENTATIVE_MODEL)
        metadata = as_dict(luna["x_scarcity_router"])
        self.assertEqual(metadata["kind"], "logical_model")
        self.assertEqual(metadata["provider"], "openai")
        self.assertEqual(metadata["reasoning_efforts"], ["max", "medium"])
        self.assertEqual(metadata["effective_context_limit_tokens"], 272_000)
        self.assertEqual(metadata["max_output_tokens"], 128_000)

    def test_representative_output_ceiling_passes_the_limits_gate(self) -> None:
        """FAILURE 3 of the acceptance report — FIXED by child #136.

        The real client's ``max_completion_tokens: 128000`` no longer
        hits a hidden generic ceiling: the #136 honest defaults
        (``GatewayLimits.max_output_tokens = 131072``) sit above the
        model's evidenced 128k output, the effective-limits gate admits
        the representative request (the limit equals the model's hard
        maximum), and the full representative shape — logical model,
        dialects, tools, streaming with usage — executes end to end.
        """
        port = self.make_server()
        response = self.post_chat(port, representative_request())
        self.assertEqual(response.status, 200)
        frames = self.read_sse_frames(response)
        self.assertEqual(frames[-1], "[DONE]")

    def test_output_request_above_effective_capability_is_still_rejected(
        self,
    ) -> None:
        """The #136 counterpart: raising the defaults removed the hidden
        ceiling, not the rejection semantics. A request above the model's
        evidenced 128k output is never admitted: the routing core's
        calibrated-capability requirement (the request's output minimum
        vs the model's hard maximum) finds no eligible target — the
        honest 503, never clipping, never a silent downgrade. (Requests
        between the capability and administrator ceilings — or above a
        channel's own ceiling — take the typed 400 limits rejections
        pinned in tests/test_effective_limits.py.)
        """
        port = self.make_server()
        body = representative_request()
        body["max_completion_tokens"] = 128_001
        response = self.post_chat(port, body)
        self.assertEqual(response.status, 503)
        payload = cast("dict[str, object]", json.loads(response.read()))
        error = as_dict(payload["error"])
        self.assertEqual(error["code"], "no_eligible_target")

    def test_pinned_request_with_client_tools_is_currently_incompatible(
        self,
    ) -> None:
        """FAILURE 5 of the acceptance report — the hard blocker, flipped
        by child #137 only to the evidenced level.

        With a pinned target whose compatibility matrix marks
        ``tool_calls`` UNSUPPORTED for its exact (channel, provider,
        model) identity — the shipped Codex source evidence, applied here
        to the fixture's server-direct identity with the codex capacity
        scope — the representative request fails closed with
        ``compatibility_unsupported``; the router never executes the
        client's tools. (At acceptance time the hidden 16384 output
        ceiling masked this gate first; since #136's honest defaults the
        compatibility gate is the first failure, which is the ordering
        this pin exercises.)
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
