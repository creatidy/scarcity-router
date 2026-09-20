"""Execution-server ingress tests (M03, issue #88, D-044/D-045).

HTTP-level tests of the NEW authenticated execution server on an ephemeral
loopback port: authentication, the disjoint execution-surface routes,
non-streaming and SSE streaming round-trips, transport rejections
(oversized bodies, transfer-encoding, duplicate JSON keys), the Host
boundary, router-loop protection, key-file permission discipline and the
non-loopback/TLS refusal. Everything runs against the deterministic
synthetic adapter; no live provider, no TLS deployment, no secrets.
"""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import cast, override

from scarcity_router.gateway_adapters import (
    CHUNK_TEXT_DELTA,
    AdapterCall,
    AdapterResult,
    AdapterStreamChunk,
    CallObservation,
    ExecutionContext,
)
from scarcity_router.gateway_contracts import GatewayLimits, hash_client_key
from scarcity_router.gateway_server import (
    GATEWAY_ORIGIN_HEADER,
    GatewayHTTPServer,
    build_parser,
    load_client_key_directory,
    make_gateway_server,
)
from tests.gateway_fixtures import (
    CLIENT_KEY,
    GatewayApplication,
    ScriptedAdapter,
    audit_records,
    chunkless_behavior,
    make_application,
    tool_call_behavior,
)

AUTH_HEADERS = {
    "Authorization": f"Bearer {CLIENT_KEY}",
    "Content-Type": "application/json",
}


def as_dict(value: object) -> dict[str, object]:
    """Narrow one parsed JSON value to a mapping for assertions."""
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def as_list(value: object) -> list[object]:
    """Narrow one parsed JSON value to an array for assertions."""
    assert isinstance(value, list)
    return cast("list[object]", value)


class ServerHarness(unittest.TestCase):
    """One ephemeral gateway server per test."""

    application: GatewayApplication
    server: GatewayHTTPServer
    thread: threading.Thread

    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        # Placeholders; make_server replaces them before each test body runs.
        self.application = cast("GatewayApplication", object())
        self.server = cast("GatewayHTTPServer", object())
        self.thread = cast("threading.Thread", object())

    def make_server(
        self,
        *,
        adapters: list[ScriptedAdapter] | None = None,
        limits: GatewayLimits | None = None,
    ) -> int:
        application = make_application(adapters=adapters, limits=limits)
        server = make_gateway_server(application, host="127.0.0.1", port=0)
        self.application = application
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        _, port = server.server_address[:2]
        return int(port)

    @override
    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        _ = self.thread.join(timeout=10)

    # ── Clients ───────────────────────────────────────────────────────────

    def client(self, port: int) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", port, timeout=30)

    def post_chat(
        self,
        port: int,
        document: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> http.client.HTTPResponse:
        connection = self.client(port)
        merged = dict(AUTH_HEADERS)
        if headers:
            merged.update(headers)
        body = json.dumps(document)
        connection.request(
            "POST", "/v1/chat/completions", body=body, headers=merged
        )
        return connection.getresponse()

    def read_sse_frames(self, response: http.client.HTTPResponse) -> list[str]:
        frames: list[str] = []
        buffer = b""
        while True:
            piece: bytes = response.read(1)
            if not piece:
                break
            buffer += piece
            while b"\n\n" in buffer:
                partitioned = buffer.partition(b"\n\n")
                frame: bytes = partitioned[0]
                buffer = partitioned[2]
                text = frame.decode("utf-8")
                if text.startswith("data: "):
                    frames.append(text[len("data: ") :])
        return frames


class AuthenticationTests(ServerHarness):
    def test_missing_key_is_401(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request("GET", "/v1/models")
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(as_dict(payload["error"])["type"], "authentication_error")

    def test_wrong_key_is_401(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models", headers={"Authorization": "Bearer sk-sr-wrong"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 401)

    def test_non_bearer_scheme_is_401(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models", headers={"Authorization": f"Basic {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 401)

    def test_unknown_path_is_404_after_authentication(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request("GET", "/v1/secret", headers={"Authorization": f"Bearer {CLIENT_KEY}"})
        response = connection.getresponse()
        self.assertEqual(response.status, 404)

    def test_unknown_path_without_key_is_401(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request("GET", "/v1/secret")
        response = connection.getresponse()
        self.assertEqual(response.status, 401)


class SurfaceTests(ServerHarness):
    def test_models_listing_with_valid_key(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(payload["object"], "list")
        ids = [as_dict(entry)["id"] for entry in as_list(payload["data"])]
        self.assertEqual(ids, ["deep-coding", "zai-only"])

    def test_models_listing_ignores_query_string(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models?limit=10", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)

    def test_unsupported_method_is_405(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "DELETE", "/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 405)

    def test_nonstreaming_round_trip(self) -> None:
        port = self.make_server()
        response = self.post_chat(
            port,
            {"model": "deep-coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["model"], "deep-coding")
        choices = as_list(payload["choices"])
        choice = as_dict(choices[0])
        message = as_dict(choice["message"])
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], "synthetic reply")
        self.assertEqual(choice["finish_reason"], "stop")
        usage = as_dict(payload["usage"])
        self.assertEqual(usage["prompt_tokens"], 11)
        self.assertEqual(usage["completion_tokens"], 7)
        extension = as_dict(payload["x_scarcity_router"])
        self.assertEqual(extension["usage_source"], "provider_reported")
        self.assertIsNotNone(extension["decision_id"])

    def test_parser_rejections_are_400_openai_errors(self) -> None:
        port = self.make_server()
        cases: list[dict[str, object]] = [
            {"model": "deep-coding", "messages": [], "bogus_key": 1},
            {"model": "deep-coding", "messages": [{"role": "user"}]},
        ]
        for document in cases:
            response = self.post_chat(port, document)
            self.assertEqual(response.status, 400, document)
            body = cast("dict[str, object]", json.loads(response.read()))
            self.assertEqual(as_dict(body["error"])["type"], "invalid_request_error")

    def test_streaming_round_trip_emits_well_formed_sse(self) -> None:
        port = self.make_server()
        response = self.post_chat(
            port,
            {
                "model": "deep-coding",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type", "").startswith("text/event-stream"))
        frames = self.read_sse_frames(response)
        self.assertEqual(frames[-1], "[DONE]")
        chunks = [cast("dict[str, object]", json.loads(frame)) for frame in frames[:-1]]
        # First chunk carries the assistant role delta.
        first_choices = as_list(chunks[0]["choices"])
        first_delta = as_dict(first_choices[0])["delta"]
        self.assertEqual(as_dict(first_delta).get("role"), "assistant")
        text = ""
        for chunk in chunks[1:-1]:
            chunk_choices = as_list(chunk["choices"])
            if not chunk_choices:
                continue
            delta = as_dict(as_dict(chunk_choices[0])["delta"])
            content = delta.get("content")
            text += content if isinstance(content, str) else ""
        self.assertEqual(text, "synthetic reply")
        finish = [
            c
            for c in chunks
            if as_list(c["choices"]) and as_dict(as_list(c["choices"])[0])["finish_reason"]
        ]
        self.assertTrue(finish)
        self.assertEqual(
            as_dict(as_list(finish[-1]["choices"])[0])["finish_reason"], "stop"
        )
        # include_usage: a final chunk with empty choices carries usage.
        usage_chunks = [c for c in chunks if not as_list(c["choices"])]
        self.assertEqual(len(usage_chunks), 1)
        final_usage = as_dict(usage_chunks[0])
        self.assertEqual(as_dict(final_usage["usage"])["prompt_tokens"], 11)

    def test_chunkless_completed_stream_is_synthesized_never_silent(self) -> None:
        """A whole-message result for stream:true still yields full SSE.

        An adapter may answer a streaming call with a completed result and
        no chunks (exactly what a first whole-message adapter does). The
        promised synthesized sequence must go out -- headers, role chunk,
        content, finish, usage when requested, ``[DONE]`` -- so the client
        can never hang on a zero-byte 200 (review 1, finding B1).
        """
        adapter = ScriptedAdapter(behavior=chunkless_behavior())
        port = self.make_server(adapters=[adapter])
        response = self.post_chat(
            port,
            {
                "model": "deep-coding",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        self.assertEqual(response.status, 200)
        frames = self.read_sse_frames(response)
        self.assertTrue(frames, "the stream must not be empty")
        self.assertEqual(frames[-1], "[DONE]")
        chunks = [cast("dict[str, object]", json.loads(frame)) for frame in frames[:-1]]
        first_choices = as_list(chunks[0]["choices"])
        first_delta = as_dict(as_dict(first_choices[0])["delta"])
        self.assertEqual(first_delta.get("role"), "assistant")
        text = ""
        for chunk in chunks[1:-1]:
            chunk_choices = as_list(chunk["choices"])
            if not chunk_choices:
                continue
            delta = as_dict(as_dict(chunk_choices[0])["delta"])
            content = delta.get("content")
            text += content if isinstance(content, str) else ""
        self.assertEqual(text, "synthetic reply")
        finish_frames = [
            c
            for c in chunks
            if as_list(c["choices"])
            and as_dict(as_list(c["choices"])[0])["finish_reason"]
        ]
        self.assertTrue(finish_frames)
        self.assertEqual(
            as_dict(as_list(finish_frames[-1]["choices"])[0])["finish_reason"], "stop"
        )
        usage_chunks = [c for c in chunks if not as_list(c["choices"])]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(as_dict(usage_chunks[0]["usage"])["prompt_tokens"], 11)

    def test_streaming_without_include_usage_has_no_usage_chunk(self) -> None:
        port = self.make_server()
        response = self.post_chat(
            port,
            {
                "model": "deep-coding",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        frames = self.read_sse_frames(response)
        chunks = [cast("dict[str, object]", json.loads(frame)) for frame in frames[:-1]]
        self.assertEqual(frames[-1], "[DONE]")
        self.assertTrue(all(as_list(chunk["choices"]) for chunk in chunks))

    def test_pre_dispatch_failure_before_stream_start_is_a_clean_error(self) -> None:
        port = self.make_server()
        response = self.post_chat(
            port,
            {
                "model": "does-not-exist",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        self.assertEqual(response.status, 404)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(as_dict(payload["error"])["code"], "model_not_found")

    def test_multi_turn_tool_round_trip_over_http(self) -> None:
        adapter = ScriptedAdapter(behavior=tool_call_behavior())
        port = self.make_server(adapters=[adapter])
        tools: list[dict[str, object]] = [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "list files",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        response = self.post_chat(
            port,
            {
                "model": "deep-coding",
                "messages": [{"role": "user", "content": "list the files"}],
                "tools": tools,
            },
        )
        self.assertEqual(response.status, 200)
        payload = cast("dict[str, object]", json.loads(response.read()))
        message = as_dict(as_dict(as_list(payload["choices"])[0])["message"])
        tool_calls = as_list(message["tool_calls"])
        first_call = as_dict(tool_calls[0])
        self.assertEqual(
            as_dict(first_call["function"])["name"], "list_files"
        )
        # The client-side tool came back to the CLIENT; the router executed
        # nothing local.
        response = self.post_chat(
            port,
            {
                "model": "deep-coding",
                "messages": [
                    {"role": "user", "content": "list the files"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls,
                    },
                    {
                        "role": "tool",
                        "tool_call_id": first_call["id"],
                        "content": "a.txt",
                    },
                ],
                "tools": tools,
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(adapter.dispatch_count, 2)


class TransportBoundaryTests(ServerHarness):
    def test_oversized_body_is_413(self) -> None:
        port = self.make_server(limits=GatewayLimits(max_request_body_bytes=64))
        response = self.post_chat(
            port,
            {"model": "deep-coding", "messages": [{"role": "user", "content": "x" * 4096}]},
        )
        self.assertEqual(response.status, 413)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(as_dict(payload["error"])["code"], "request_too_large")

    def test_transfer_encoding_is_rejected(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body="{}",
            headers={**AUTH_HEADERS, "Transfer-Encoding": "chunked"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 400)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        raw = (
            b'{"model": "deep-coding", "model": "zai-only", '
            + b'"messages": [{"role": "user", "content": "hi"}]}'
        )
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=raw,
            headers={**AUTH_HEADERS, "Content-Length": str(len(raw))},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        payload = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(as_dict(payload["error"])["code"], "invalid_json")

    def test_non_json_content_type_is_rejected(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body="{}",
            headers={**AUTH_HEADERS, "Content-Type": "text/plain"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 400)


class SecurityBoundaryTests(ServerHarness):
    def test_missing_host_header_is_rejected(self) -> None:
        port = self.make_server()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(
                b"GET /v1/models HTTP/1.1\r\n"
                + b"Authorization: Bearer " + CLIENT_KEY.encode() + b"\r\n\r\n"
            )
            response = sock.recv(4096).decode("utf-8", "replace")
        self.assertTrue(response.startswith("HTTP/1.1 400"), response)

    def test_foreign_host_header_is_rejected_on_loopback(self) -> None:
        port = self.make_server()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(
                b"GET /v1/models HTTP/1.1\r\n"
                + b"Host: evil.example\r\n"
                + b"Authorization: Bearer " + CLIENT_KEY.encode() + b"\r\n\r\n"
            )
            response = sock.recv(4096).decode("utf-8", "replace")
        self.assertTrue(response.startswith("HTTP/1.1 400"), response)

    def test_router_loop_marker_fails_loudly(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "GET",
            "/v1/models",
            headers={
                "Authorization": f"Bearer {CLIENT_KEY}",
                GATEWAY_ORIGIN_HEADER: "1",
            },
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        body = cast("dict[str, object]", json.loads(response.read()))
        self.assertEqual(as_dict(body["error"])["code"], "router_loop_detected")

    def test_no_cors_headers_are_ever_emitted(self) -> None:
        port = self.make_server()
        connection = self.client(port)
        connection.request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
        )
        response = connection.getresponse()
        _ = response.read()
        for header, _value in response.getheaders():
            self.assertFalse(header.lower().startswith("access-control"))

    def test_client_disconnect_mid_stream_propagates_cancellation(self) -> None:
        """The client hangs up mid-stream; the backend sees the cancel.

        The client half-closes its side (the TCP FIN of a disconnect) after
        the first frame; the server's disconnect detection must surface the
        hangup to the coordinator, which cancels the dispatch and audits
        the request as ``cancelled`` with no fabricated completion.
        """
        release = threading.Event()

        def behavior(
            call: AdapterCall, context: ExecutionContext
        ) -> AdapterResult:
            _ = call
            observation = _observation_stub()
            if context.emit_chunk is None:
                return _completed(observation)
            context.emit_chunk(
                AdapterStreamChunk(kind=CHUNK_TEXT_DELTA, text="first")
            )
            _ = release.wait(timeout=10)
            # The client is gone; the very next emission must surface the
            # disconnect to the coordinator (never a fabricated stream).
            context.emit_chunk(
                AdapterStreamChunk(kind=CHUNK_TEXT_DELTA, text="after hangup")
            )
            raise AssertionError("test behavior: the disconnect never surfaced")

        adapter = ScriptedAdapter(behavior=behavior)
        port = self.make_server(adapters=[adapter])
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        body = json.dumps(
            {
                "model": "deep-coding",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }
        )
        sock.sendall(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"Authorization: Bearer {CLIENT_KEY}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
            ).encode()
            + body.encode()
        )
        buffer = b""
        while b"\n\n" not in buffer:
            piece = sock.recv(4096)
            if not piece:
                break
            buffer += piece
        self.assertTrue(buffer.startswith(b"HTTP/1.1 200"))
        # The disconnect: the client's side of the TCP connection closes.
        sock.shutdown(socket.SHUT_WR)
        release.set()
        # The coordinator must audit the cancelled request once the server
        # notices the disconnect; poll briefly and deterministically.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            records = audit_records(self.application)
            if records and records[-1].result_status == "cancelled":
                break
            time.sleep(0.02)
        records = audit_records(self.application)
        self.assertTrue(records, "expected an audit record")
        self.assertEqual(records[-1].result_status, "cancelled")
        self.assertEqual(records[-1].reason_codes, ("client_disconnected",))
        _ = sock.close()


def _observation_stub() -> CallObservation:
    from scarcity_router.gateway_adapters import CallObservation
    from tests.gateway_fixtures import canonical, T_EVAL
    from datetime import timedelta

    return CallObservation(
        call_index=0,
        started_at=canonical(T_EVAL),
        ended_at=canonical(T_EVAL + timedelta(seconds=1)),
        status="completed",
    )


def _completed(observation: CallObservation) -> AdapterResult:
    from scarcity_router.gateway_adapters import AdapterMessage, AdapterResult

    return AdapterResult(
        status="completed",
        calls=(observation,),
        message=AdapterMessage(role="assistant", content="done"),
        finish_reason="stop",
    )


class KeyFileTests(unittest.TestCase):
    def test_owner_only_file_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            _ = path.write_text(
                json.dumps({"client-a": hash_client_key("sk-sr-x")}), encoding="utf-8"
            )
            path.chmod(0o600)
            directory = load_client_key_directory(path)
            self.assertEqual(directory.authenticate("sk-sr-x"), "client-a")

    def test_group_or_world_readable_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            _ = path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                _ = load_client_key_directory(path)

    def test_missing_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _ = load_client_key_directory(Path(tmp) / "absent.json")

    def test_key_material_is_refused_in_place_of_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            _ = path.write_text(
                json.dumps({"client-a": "sk-sr-raw-key"}), encoding="utf-8"
            )
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                _ = load_client_key_directory(path)


class StartupBoundaryTests(unittest.TestCase):
    def test_non_loopback_bind_requires_tls(self) -> None:
        application = make_application()
        with self.assertRaises(ValueError):
            _ = make_gateway_server(application, host="0.0.0.0", port=0)

    def test_loopback_bind_without_tls_is_allowed(self) -> None:
        application = make_application()
        server = make_gateway_server(application, host="127.0.0.1", port=0)
        try:
            self.assertTrue(server.server_address[0] in ("127.0.0.1",))
        finally:
            server.server_close()

    def test_parser_requires_client_keys(self) -> None:
        with self.assertRaises(SystemExit):
            _ = build_parser().parse_args([])


if __name__ == "__main__":
    _ = unittest.main()
