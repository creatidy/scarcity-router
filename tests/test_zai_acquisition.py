"""Security and integration tests for the Z.ai acquisition layer.

The transport seam (``_open_response``) is replaced with fake openers and
fake response objects; no test in this module performs network I/O. The
credential discovery path runs against temporary auth files containing only
conspicuous synthetic fake secrets; the real Kilo credential is never read.

Every failure class asserts that the synthetic secrets appear in no
serialized snapshot, repr, diagnostic or captured stdout/stderr output.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
import urllib.error
import urllib.request
from collections.abc import Mapping
from http.client import (
    BadStatusLine,
    HTTPException,
    HTTPMessage,
    LineTooLong,
)
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast, override
from unittest import mock

from scarcity_router import CapacitySnapshot, CapacityValidationError
from scarcity_router.providers import zai_acquisition
from scarcity_router.providers.zai import parse_zai_quota_response

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "zai-coding-plan"
RETRIEVED_AT = "2026-09-01T22:49:51.000Z"

# Conspicuous synthetic-only secrets; never realistic production shapes.
SECRET = "TEST_ONLY_ZAI_SECRET_NEVER_REAL"
SPACED_SECRET = "  TEST_ONLY_ZAI_SECRET_SENT_EXACTLY_AS_STORED  "
OTHER_SECRET = "TEST_ONLY_UNRELATED_PROVIDER_SECRET_ALSO_FAKE"
SECRET_A = "TEST_ONLY_ZAI_SECRET_A_FAKE"
SECRET_B = "TEST_ONLY_ZAI_SECRET_B_FAKE"

# Synthetic credentials that are unsafe as HTTP header values.
CR_SECRET = "TEST_ONLY_CR\rSECRET"
LF_SECRET = "TEST_ONLY_LF\nSECRET"
CRLF_SECRET = "TEST_ONLY_CRLF\r\nSECRET"
NUL_SECRET = "TEST_ONLY_NUL\x00SECRET"
CONTROL_SECRET = "TEST_ONLY_CTRL\x1fSECRET"
HTAB_SECRET = "TEST_ONLY_TAB\tSECRET"
DEL_SECRET = "TEST_ONLY_DEL\x7fSECRET"
NON_LATIN1_SECRET = "TEST_ONLY_NON_LATIN1_\u4f60_SECRET"

# Every synthetic secret; the non-leak sweep asserts each is absent from
# every scenario output.
ALL_FAKE_SECRETS: tuple[str, ...] = (
    SECRET,
    SPACED_SECRET,
    OTHER_SECRET,
    SECRET_A,
    SECRET_B,
    CR_SECRET,
    LF_SECRET,
    CRLF_SECRET,
    NUL_SECRET,
    CONTROL_SECRET,
    HTAB_SECRET,
    DEL_SECRET,
    NON_LATIN1_SECRET,
)

ENDPOINT = "https://api.z.ai/api/monitor/usage/quota/limit"
FAKE_REDIRECT_TARGET = "https://redirect.example.test/steal"
INVALID_RETRIEVED_AT = "2026-09-01T22:49:51Z"  # missing milliseconds


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_payload(name: str) -> object:
    return cast("object", json.loads(_fixture_bytes(name)))


def _valid_auth_payload() -> dict[str, object]:
    return {"zai-coding-plan": {"type": "api", "key": SECRET}}


def _http_error(code: int, location: str | None = None) -> urllib.error.HTTPError:
    headers = HTTPMessage()
    if location is not None:
        headers["Location"] = location
    return urllib.error.HTTPError(
        ENDPOINT, code, "synthetic", headers, io.BytesIO(b"")
    )


def _serialized(snapshot: CapacitySnapshot) -> str:
    return json.dumps(snapshot.to_dict(), sort_keys=True) + repr(snapshot)


class _FakeResponse:
    """Minimal successful-response fake for the transport seam."""

    _body: bytes
    _read_error: Exception | None
    closed: bool

    def __init__(self, body: bytes, read_error: Exception | None = None) -> None:
        self._body = body
        self._read_error = read_error
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        _ = size
        if self._read_error is not None:
            raise self._read_error
        return self._body

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True


class _FakeOpener:
    """Fake transport: records every call and yields a fixed outcome."""

    _outcome: _FakeResponse | Exception
    calls: list[tuple[urllib.request.Request, float]]

    def __init__(self, outcome: _FakeResponse | Exception) -> None:
        self._outcome = outcome
        self.calls = []

    def __call__(
        self, request: urllib.request.Request, timeout: float
    ) -> _FakeResponse:
        self.calls.append((request, timeout))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _AcquisitionCase(unittest.TestCase):
    """Shared harness: temp auth file plus a patched transport seam."""

    tmp: Path = Path("/")
    opener: _FakeOpener | None = None
    stdout: io.StringIO = io.StringIO()
    stderr: io.StringIO = io.StringIO()

    @override
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.opener = None
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def _refresh_capture(self) -> None:
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def _write_auth(self, content: bytes | Mapping[str, object]) -> Path:
        path = self.tmp / "auth.json"
        if isinstance(content, bytes):
            _ = path.write_bytes(content)
        else:
            _ = path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def _install_opener(self, outcome: _FakeResponse | Exception) -> _FakeOpener:
        opener = _FakeOpener(outcome)
        self.opener = opener
        patcher = mock.patch.object(zai_acquisition, "open_response", opener)
        _ = patcher.start()
        self.addCleanup(patcher.stop)
        return opener

    def _collect(
        self, auth_file: Path | None, retrieved_at: str = RETRIEVED_AT
    ) -> CapacitySnapshot:
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return zai_acquisition.collect_zai_capacity(
                retrieved_at=retrieved_at, auth_file=auth_file
            )

    def _single_request(self) -> tuple[urllib.request.Request, float]:
        assert self.opener is not None
        self.assertEqual(len(self.opener.calls), 1)
        request, timeout = self.opener.calls[0]
        return request, timeout

    def _assert_no_request(self) -> None:
        if self.opener is not None:
            self.assertEqual(self.opener.calls, [])

    def _assert_no_output(self) -> None:
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "")


# ═══════════════════════ credential discovery ════════════════════════════════


class CredentialDiscovery(_AcquisitionCase):
    def test_valid_target_provider_selects_exact_credential(self) -> None:
        _ = self._install_opener(
            _FakeResponse(_fixture_bytes("quota-200-known-windows.json"))
        )
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))

        self.assertEqual(snapshot.status, "ok")
        request, _ = self._single_request()
        self.assertEqual(request.get_header("Authorization"), SECRET)

    def test_target_credential_preserved_exactly_not_mutated(self) -> None:
        payload = {
            "zai-coding-plan": {"type": "api", "key": SPACED_SECRET}
        }
        _ = self._install_opener(_FakeResponse(b"{}"))
        _ = self._collect(self._write_auth(payload))

        request, _ = self._single_request()
        self.assertEqual(request.get_header("Authorization"), SPACED_SECRET)

    def test_unrelated_providers_never_selected_or_sent(self) -> None:
        payload: dict[str, object] = {
            "openai-codex": {"type": "api", "key": OTHER_SECRET},
            "anthropic": {"type": "api", "key": OTHER_SECRET},
        }
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(self._write_auth(payload))

        self.assertEqual(snapshot.status, "auth_required")
        self.assertEqual(
            [d.code for d in snapshot.diagnostics], ["auth_required"]
        )
        self.assertEqual(snapshot.windows, ())
        self._assert_no_request()

    def test_target_found_among_unrelated_providers(self) -> None:
        payload: dict[str, object] = {
            "openai-codex": {"type": "api", "key": OTHER_SECRET},
            "zai-coding-plan": {"type": "api", "key": SECRET},
        }
        _ = self._install_opener(_FakeResponse(b"{}"))
        _ = self._collect(self._write_auth(payload))

        request, _ = self._single_request()
        self.assertEqual(request.get_header("Authorization"), SECRET)

    def test_wrong_credential_type_rejected(self) -> None:
        for wrong in ("bearer", "api_key", 3, None):
            with self.subTest(wrong=wrong):
                payload = {"zai-coding-plan": {"type": wrong, "key": SECRET}}
                snapshot = self._collect(self._write_auth(payload))
                self.assertEqual(snapshot.status, "auth_required")
                self._assert_no_request()

    def test_missing_key_rejected(self) -> None:
        snapshot = self._collect(
            self._write_auth({"zai-coding-plan": {"type": "api"}})
        )
        self.assertEqual(snapshot.status, "auth_required")

    def test_empty_key_rejected(self) -> None:
        snapshot = self._collect(
            self._write_auth({"zai-coding-plan": {"type": "api", "key": ""}})
        )
        self.assertEqual(snapshot.status, "auth_required")

    def test_non_string_key_rejected(self) -> None:
        for bad in (12345, None, ["x"], {"v": 1}, True):
            with self.subTest(bad=bad):
                payload = {"zai-coding-plan": {"type": "api", "key": bad}}
                snapshot = self._collect(self._write_auth(payload))
                self.assertEqual(snapshot.status, "auth_required")
                self._assert_no_request()

    def test_malformed_json_rejected(self) -> None:
        snapshot = self._collect(
            self._write_auth(b'{"zai-coding-plan": {"type": "api"')
        )
        self.assertEqual(snapshot.status, "auth_required")

    def test_invalid_utf8_rejected(self) -> None:
        snapshot = self._collect(
            self._write_auth(b'\xff\xfe{"zai-coding-plan": {}}')
        )
        self.assertEqual(snapshot.status, "auth_required")

    def test_missing_file_rejected(self) -> None:
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(self.tmp / "does-not-exist.json")
        self.assertEqual(snapshot.status, "auth_required")
        self._assert_no_request()

    @unittest.skipIf(
        os.name == "posix" and os.getuid() == 0,
        "permission test cannot run as root",
    )
    def test_unreadable_file_rejected(self) -> None:
        path = self._write_auth(_valid_auth_payload())
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(path)
        self.assertEqual(snapshot.status, "auth_required")
        self._assert_no_request()

    def test_oversized_auth_file_rejected(self) -> None:
        oversized = (
            b'{"zai-coding-plan": {"type": "api", "key": "'
            + b"A" * (zai_acquisition.MAX_AUTH_FILE_BYTES + 1)
            + b'"}}'
        )
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(self._write_auth(oversized))
        self.assertEqual(snapshot.status, "auth_required")
        self._assert_no_request()

    def test_non_object_top_level_rejected(self) -> None:
        snapshot = self._collect(self._write_auth(b"[1, 2, 3]"))
        self.assertEqual(snapshot.status, "auth_required")

    def test_non_object_target_entry_rejected(self) -> None:
        snapshot = self._collect(
            self._write_auth({"zai-coding-plan": "api-key-like-string"})
        )
        self.assertEqual(snapshot.status, "auth_required")


class AmbiguousCredentialDocument(_AcquisitionCase):
    """Duplicate JSON keys make the credential source ambiguous.

    The parser must fail closed on any duplicate object key instead of
    trusting last-key-wins overwrite behavior; neither candidate value may
    reach the transport or any output.
    """

    def _assert_auth_required_without_request(
        self, snapshot: CapacitySnapshot, *absent: str
    ) -> None:
        self.assertEqual(snapshot.status, "auth_required")
        self.assertEqual(
            [d.code for d in snapshot.diagnostics], ["auth_required"]
        )
        self.assertEqual(snapshot.windows, ())
        self._assert_no_request()
        self._assert_no_output()
        text = _serialized(snapshot)
        for secret in absent:
            self.assertNotIn(secret, text)

    def test_duplicate_top_level_target_fails_closed(self) -> None:
        duplicated = (
            b'{"zai-coding-plan": {"type": "api", "key": "'
            + SECRET_A.encode()
            + b'"}, "zai-coding-plan": {"type": "api", "key": "'
            + SECRET_B.encode()
            + b'"}}'
        )
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(self._write_auth(duplicated))
        self._assert_auth_required_without_request(snapshot, SECRET_A, SECRET_B)

    def test_duplicate_key_field_in_target_fails_closed(self) -> None:
        duplicated = (
            b'{"zai-coding-plan": {"type": "api", "key": "'
            + SECRET_A.encode()
            + b'", "key": "'
            + SECRET_B.encode()
            + b'"}}'
        )
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(self._write_auth(duplicated))
        self._assert_auth_required_without_request(snapshot, SECRET_A, SECRET_B)

    def test_duplicate_type_field_in_target_fails_closed(self) -> None:
        duplicated = (
            b'{"zai-coding-plan": {"type": "api", "type": "bearer", "key": "'
            + SECRET.encode()
            + b'"}}'
        )
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(self._write_auth(duplicated))
        self._assert_auth_required_without_request(snapshot, SECRET)

    def test_duplicate_unrelated_provider_key_fails_closed(self) -> None:
        # Documented conservative choice: the duplicate-rejecting hook is
        # document-wide, so even a duplicate unrelated-provider key rejects
        # the whole credential source rather than parsing selectively.
        duplicated = (
            b'{"openai-codex": {"type": "api", "key": "x"},'
            + b' "openai-codex": {"type": "api", "key": "y"},'
            + b' "zai-coding-plan": {"type": "api", "key": "'
            + SECRET.encode()
            + b'"}}'
        )
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(self._write_auth(duplicated))
        self._assert_auth_required_without_request(snapshot, SECRET)

    def test_document_without_duplicates_still_selects_target(self) -> None:
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "schema_changed")  # b"{}" body
        request, _ = self._single_request()
        self.assertEqual(request.get_header("Authorization"), SECRET)


class UnsafeCredentialRejection(_AcquisitionCase):
    """Credentials unsafe as HTTP header values never reach the transport.

    The standard-library transport can format a malformed header value into
    an exception; unsafe values are therefore rejected during credential
    discovery, before any request object or transport exists.
    """

    def _assert_unsafe(self, secret: str) -> None:
        _ = self._install_opener(_FakeResponse(b"{}"))
        snapshot = self._collect(
            self._write_auth({"zai-coding-plan": {"type": "api", "key": secret}})
        )
        self.assertEqual(snapshot.status, "auth_required")
        self.assertEqual(
            [d.code for d in snapshot.diagnostics], ["auth_required"]
        )
        self.assertEqual(snapshot.windows, ())
        self._assert_no_request()
        self._assert_no_output()
        self.assertNotIn(secret, _serialized(snapshot))

    def test_control_characters_rejected_before_transport(self) -> None:
        for name, secret in (
            ("CR", CR_SECRET),
            ("LF", LF_SECRET),
            ("CRLF", CRLF_SECRET),
            ("NUL", NUL_SECRET),
            ("control-1f", CONTROL_SECRET),
            ("HTAB", HTAB_SECRET),
            ("DEL", DEL_SECRET),
        ):
            with self.subTest(case=name):
                self._refresh_capture()
                self._assert_unsafe(secret)

    def test_non_latin1_value_rejected_before_transport(self) -> None:
        # The stdlib header transport encodes values as latin-1; a value it
        # cannot encode must be refused before the transport can raise with
        # the character embedded in its message.
        self._assert_unsafe(NON_LATIN1_SECRET)

    def test_printable_symbol_credential_transmitted_exactly(self) -> None:
        symbol_secret = "TEST_ONLY_ABC-123+/=._~"
        _ = self._install_opener(_FakeResponse(b"{}"))
        _ = self._collect(
            self._write_auth(
                {"zai-coding-plan": {"type": "api", "key": symbol_secret}}
            )
        )
        request, _ = self._single_request()
        self.assertEqual(request.get_header("Authorization"), symbol_secret)

    def test_spaced_credential_still_sent_byte_for_value(self) -> None:
        _ = self._install_opener(_FakeResponse(b"{}"))
        _ = self._collect(
            self._write_auth(
                {"zai-coding-plan": {"type": "api", "key": SPACED_SECRET}}
            )
        )
        request, _ = self._single_request()
        self.assertEqual(request.get_header("Authorization"), SPACED_SECRET)


# ═══════════════════════ endpoint destination policy ═════════════════════════


class EndpointPolicy(unittest.TestCase):
    def test_exact_approved_endpoint_accepted(self) -> None:
        self.assertTrue(zai_acquisition.is_approved_destination(ENDPOINT))
        self.assertTrue(zai_acquisition.is_approved_destination(zai_acquisition.ENDPOINT))

    def test_http_downgrade_rejected(self) -> None:
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "http://api.z.ai/api/monitor/usage/quota/limit"
            )
        )

    def test_wrong_host_rejected(self) -> None:
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "https://evil.example/api/monitor/usage/quota/limit"
            )
        )

    def test_host_suffix_attack_rejected(self) -> None:
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "https://api.z.ai.example.com/api/monitor/usage/quota/limit"
            )
        )
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "https://api.z.ai.evil.test/api/monitor/usage/quota/limit"
            )
        )

    def test_userinfo_rejected(self) -> None:
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "https://user@api.z.ai/api/monitor/usage/quota/limit"
            )
        )
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "https://user:pass@api.z.ai/api/monitor/usage/quota/limit"
            )
        )

    def test_unexpected_port_rejected(self) -> None:
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "https://api.z.ai:8443/api/monitor/usage/quota/limit"
            )
        )

    def test_explicit_default_port_and_implicit_port_accepted(self) -> None:
        self.assertTrue(
            zai_acquisition.is_approved_destination(
                "https://api.z.ai:443/api/monitor/usage/quota/limit"
            )
        )

    def test_query_rejected(self) -> None:
        self.assertFalse(
            zai_acquisition.is_approved_destination(ENDPOINT + "?debug=1")
        )

    def test_fragment_rejected(self) -> None:
        self.assertFalse(
            zai_acquisition.is_approved_destination(ENDPOINT + "#x")
        )

    def test_wrong_path_rejected(self) -> None:
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "https://api.z.ai/api/monitor/usage/other"
            )
        )
        self.assertFalse(
            zai_acquisition.is_approved_destination(ENDPOINT + "/")
        )
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "https://api.z.ai/api/monitor/usage/quota/limit/extra"
            )
        )

    def test_malformed_url_fails_closed(self) -> None:
        self.assertFalse(
            zai_acquisition.is_approved_destination(
                "https://api.z.ai:not-a-port/api/monitor/usage/quota/limit"
            )
        )


class DestinationValidationOrdering(_AcquisitionCase):
    def test_destination_validated_before_authorization_attached(self) -> None:
        _ = self._install_opener(_FakeResponse(b"{}"))
        calls: list[str] = []

        def fake_validate(destination: str) -> bool:
            _ = destination
            calls.append("validate")
            return True

        def fake_add_header(
            self: urllib.request.Request, name: str, value: str
        ) -> None:
            _ = self, name, value
            calls.append(f"add_header:{name}")

        with (
            mock.patch.object(
                zai_acquisition, "is_approved_destination", fake_validate
            ),
            mock.patch.object(
                urllib.request.Request, "add_header", fake_add_header
            ),
        ):
            snapshot = self._collect(self._write_auth(_valid_auth_payload()))

        self.assertEqual(snapshot.status, "schema_changed")  # b"{}" body
        self.assertEqual(
            calls, ["validate", "add_header:Authorization", "add_header:Accept"]
        )

    def test_unapproved_destination_never_receives_authorization(self) -> None:
        _ = self._install_opener(_FakeResponse(b"{}"))
        with (
            mock.patch.object(
                zai_acquisition,
                "ENDPOINT",
                "http://api.z.ai/api/monitor/usage/quota/limit",
            ),
            mock.patch.object(
                urllib.request.Request, "add_header", autospec=True
            ) as add_header,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _ = self._collect(self._write_auth(_valid_auth_payload()))
            add_header.assert_not_called()

        self.assertNotIn(SECRET, str(ctx.exception))
        self._assert_no_request()


class RequestShape(_AcquisitionCase):
    def _prepare(self) -> tuple[urllib.request.Request, float]:
        _ = self._install_opener(_FakeResponse(b"{}"))
        _ = self._collect(self._write_auth(_valid_auth_payload()))
        return self._single_request()

    def test_method_is_get(self) -> None:
        request, _timeout = self._prepare()
        self.assertEqual(request.get_method(), "GET")

    def test_target_is_exactly_the_fixed_endpoint(self) -> None:
        request, _timeout = self._prepare()
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.full_url, zai_acquisition.ENDPOINT)

    def test_authorization_is_the_synthetic_stored_value(self) -> None:
        request, _timeout = self._prepare()
        self.assertEqual(request.get_header("Authorization"), SECRET)

    def test_accept_is_application_json(self) -> None:
        request, _timeout = self._prepare()
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_timeout_is_finite_and_forwarded(self) -> None:
        _request, timeout = self._prepare()
        self.assertGreater(zai_acquisition.TIMEOUT_SECONDS, 0)
        self.assertEqual(timeout, zai_acquisition.TIMEOUT_SECONDS)

    def test_no_bearer_prefix_anywhere_in_headers(self) -> None:
        request, _timeout = self._prepare()
        for _name, value in request.header_items():
            self.assertFalse(value.startswith("Bearer "))


# ═══════════════════════════ redirect policy ═════════════════════════════════


class RedirectPolicy(_AcquisitionCase):
    def _assert_redirect(self, code: int) -> None:
        _ = self._install_opener(_http_error(code, FAKE_REDIRECT_TARGET))
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))

        self.assertEqual(snapshot.status, "unknown")
        self.assertEqual(
            [d.code for d in snapshot.diagnostics], ["telemetry_unknown"]
        )
        self.assertEqual(snapshot.windows, ())
        # Zero second request: no Authorization can have been forwarded.
        request, _ = self._single_request()
        self.assertEqual(request.get_header("Authorization"), SECRET)
        text = _serialized(snapshot)
        self.assertNotIn(FAKE_REDIRECT_TARGET, text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Location", text)

    def test_301_rejected(self) -> None:
        self._assert_redirect(301)

    def test_302_rejected(self) -> None:
        self._assert_redirect(302)

    def test_303_rejected(self) -> None:
        self._assert_redirect(303)

    def test_307_rejected(self) -> None:
        self._assert_redirect(307)

    def test_308_rejected(self) -> None:
        self._assert_redirect(308)


class _OpenerRecorder:
    """Typed fake opener recording exactly one ``open`` call."""

    sentinel: object
    opened: list[tuple[urllib.request.Request, float]]

    def __init__(self, sentinel: object) -> None:
        self.sentinel = sentinel
        self.opened = []

    def open(self, request: urllib.request.Request, timeout: float) -> object:
        self.opened.append((request, timeout))
        return self.sentinel


class RedirectMechanism(unittest.TestCase):
    """The production redirect boundary itself, without any network I/O."""

    def test_no_redirect_handler_declines_and_constructs_nothing(self) -> None:
        handler = zai_acquisition.NoRedirect()
        original_request = urllib.request.Request(ENDPOINT, method="GET")
        body = io.BytesIO(b"")
        headers = HTTPMessage()
        for code in (301, 302, 303, 307, 308):
            with (
                self.subTest(code=code),
                mock.patch.object(urllib.request, "Request") as request_factory,
            ):
                result = handler.redirect_request(
                    original_request,
                    body,
                    code,
                    "Redirect",
                    headers,
                    FAKE_REDIRECT_TARGET,
                )
                self.assertIsNone(result)
                request_factory.assert_not_called()

    def test_open_response_builds_no_redirect_opener_and_single_request(self) -> None:
        request = urllib.request.Request(ENDPOINT, method="GET")
        recorder = _OpenerRecorder(sentinel=object())

        with mock.patch.object(
            urllib.request, "build_opener", return_value=recorder
        ) as build_opener:
            returned = zai_acquisition.open_response(
                request, zai_acquisition.TIMEOUT_SECONDS
            )

        self.assertIs(returned, recorder.sentinel)
        build_opener.assert_called_once()
        handler_argument = cast("object", build_opener.call_args.args[0])
        self.assertIsInstance(handler_argument, zai_acquisition.NoRedirect)
        # Exactly one call, with the original authenticated request object.
        self.assertEqual(
            recorder.opened, [(request, zai_acquisition.TIMEOUT_SECONDS)]
        )
        self.assertIs(recorder.opened[0][0], request)


# ═══════════════════════ response handling and mapping ═══════════════════════


class ResponseHandling(_AcquisitionCase):
    def _collect_fixture(self, name: str) -> CapacitySnapshot:
        _ = self._install_opener(_FakeResponse(_fixture_bytes(name)))
        return self._collect(self._write_auth(_valid_auth_payload()))

    def test_known_windows_fixture_matches_parser_exactly(self) -> None:
        name = "quota-200-known-windows.json"
        snapshot = self._collect_fixture(name)
        expected = parse_zai_quota_response(
            _fixture_payload(name), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snapshot, expected)
        self.assertEqual(snapshot.status, "ok")
        self.assertEqual(len(snapshot.windows), 2)

    def test_unknown_window_fixture_matches_parser_exactly(self) -> None:
        name = "quota-200-unknown-window.json"
        snapshot = self._collect_fixture(name)
        expected = parse_zai_quota_response(
            _fixture_payload(name), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snapshot, expected)

    def test_degraded_values_fixture_matches_parser_exactly(self) -> None:
        name = "quota-200-degraded-values.json"
        snapshot = self._collect_fixture(name)
        expected = parse_zai_quota_response(
            _fixture_payload(name), retrieved_at=RETRIEVED_AT
        )
        self.assertEqual(snapshot, expected)

    def test_http_401_maps_to_auth_required(self) -> None:
        _ = self._install_opener(_http_error(401))
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "auth_required")
        self.assertEqual(
            [d.code for d in snapshot.diagnostics], ["auth_required"]
        )
        self.assertEqual(snapshot.windows, ())

    def test_no_bearer_retry_after_401(self) -> None:
        _ = self._install_opener(_http_error(401))
        _ = self._collect(self._write_auth(_valid_auth_payload()))
        request, _ = self._single_request()
        self.assertEqual(request.get_header("Authorization"), SECRET)
        self.assertNotIn("Bearer", request.get_header("Authorization") or "")

    def test_unevidenced_http_errors_map_to_unknown(self) -> None:
        for code in (400, 403, 404, 429, 500, 503):
            with self.subTest(code=code):
                _ = self._install_opener(_http_error(code))
                snapshot = self._collect(self._write_auth(_valid_auth_payload()))
                self.assertEqual(snapshot.status, "unknown")
                self.assertEqual(
                    [d.code for d in snapshot.diagnostics],
                    ["telemetry_unknown"],
                )
                self.assertEqual(snapshot.windows, ())

    def test_oversized_response_never_parsed(self) -> None:
        _ = self._install_opener(
            _FakeResponse(b"A" * (zai_acquisition.MAX_BODY_BYTES + 1))
        )
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "unknown")
        self.assertEqual(
            [d.code for d in snapshot.diagnostics], ["telemetry_unknown"]
        )

    def test_malformed_utf8_maps_to_schema_changed(self) -> None:
        _ = self._install_opener(_FakeResponse(b'{"code": \xff}'))
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "schema_changed")
        self.assertEqual(
            [d.code for d in snapshot.diagnostics], ["schema_changed"]
        )
        self.assertEqual(snapshot.windows, ())

    def test_malformed_json_maps_to_schema_changed(self) -> None:
        _ = self._install_opener(_FakeResponse(b'{"code": 200,'))
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "schema_changed")

    def test_empty_success_body_maps_to_schema_changed(self) -> None:
        _ = self._install_opener(_FakeResponse(b""))
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "schema_changed")

    def test_network_error_maps_to_unavailable(self) -> None:
        _ = self._install_opener(urllib.error.URLError("name resolution failed"))
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual(
            [d.code for d in snapshot.diagnostics], ["source_unavailable"]
        )
        self.assertEqual(snapshot.windows, ())

    def test_http_protocol_failures_map_to_unavailable(self) -> None:
        for error in (
            BadStatusLine("TEST_ONLY_MALFORMED_STATUS"),
            LineTooLong("TEST_ONLY_OVERLONG_LINE"),
        ):
            with self.subTest(error=type(error).__name__):
                self._refresh_capture()
                _ = self._install_opener(error)
                snapshot = self._collect(
                    self._write_auth(_valid_auth_payload())
                )
                self.assertEqual(snapshot.status, "unavailable")
                self.assertEqual(
                    [d.code for d in snapshot.diagnostics],
                    ["source_unavailable"],
                )
                self.assertEqual(snapshot.windows, ())
                # Exactly one attempted transport call; no retry.
                request, _ = self._single_request()
                self.assertEqual(request.get_header("Authorization"), SECRET)
                self.assertNotIn(SECRET, _serialized(snapshot))
                self._assert_no_output()

    def test_timeout_maps_to_unavailable(self) -> None:
        _ = self._install_opener(TimeoutError())
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "unavailable")

    def test_connection_error_maps_to_unavailable(self) -> None:
        _ = self._install_opener(ConnectionRefusedError())
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "unavailable")

    def test_read_failure_mid_body_maps_to_unavailable(self) -> None:
        for error in (ConnectionResetError(), HTTPException("truncated")):
            with self.subTest(error=type(error).__name__):
                _ = self._install_opener(
                    _FakeResponse(b"", read_error=error)
                )
                snapshot = self._collect(
                    self._write_auth(_valid_auth_payload())
                )
                self.assertEqual(snapshot.status, "unavailable")
                self.assertEqual(
                    [d.code for d in snapshot.diagnostics],
                    ["source_unavailable"],
                )
                self._assert_no_output()


class ParserIntegration(_AcquisitionCase):
    def test_invalid_retrieved_at_fails_through_contract_validation(self) -> None:
        _ = self._install_opener(
            _FakeResponse(_fixture_bytes("quota-200-known-windows.json"))
        )
        with self.assertRaises(CapacityValidationError):
            _ = self._collect(
                self._write_auth(_valid_auth_payload()),
                retrieved_at=INVALID_RETRIEVED_AT,
            )
        self._assert_no_output()


# ═══════════════════════ secret non-leak sweep ═══════════════════════════════


class SecretNonLeak(_AcquisitionCase):
    def _build_scenario(
        self, name: str
    ) -> tuple[Path, _FakeResponse | Exception]:
        auth = self._write_auth(_valid_auth_payload())
        mixed = self._write_auth(
            {
                "openai-codex": {"type": "api", "key": OTHER_SECRET},
                "zai-coding-plan": {"type": "api", "key": SECRET},
            }
        )
        bearer = self._write_auth(
            {"zai-coding-plan": {"type": "bearer", "key": SECRET}}
        )
        unrelated = self._write_auth(
            {"openai-codex": {"type": "api", "key": OTHER_SECRET}}
        )
        absent = self.tmp / "absent.json"

        def auth_with(key_value: str) -> Path:
            return self._write_auth(
                {"zai-coding-plan": {"type": "api", "key": key_value}}
            )

        def raw_auth(content: bytes) -> Path:
            return self._write_auth(content)

        duplicate_top_level = (
            b'{"zai-coding-plan": {"type": "api", "key": "'
            + SECRET_A.encode()
            + b'"}, "zai-coding-plan": {"type": "api", "key": "'
            + SECRET_B.encode()
            + b'"}}'
        )
        duplicate_key_field = (
            b'{"zai-coding-plan": {"type": "api", "key": "'
            + SECRET_A.encode()
            + b'", "key": "'
            + SECRET_B.encode()
            + b'"}}'
        )
        duplicate_type_field = (
            b'{"zai-coding-plan": {"type": "api", "type": "bearer", "key": "'
            + SECRET.encode()
            + b'"}}'
        )
        scenarios: dict[str, tuple[Path, _FakeResponse | Exception]] = {
            "valid-200": (
                auth,
                _FakeResponse(_fixture_bytes("quota-200-known-windows.json")),
            ),
            "schema-body": (auth, _FakeResponse(b'{"code": 200,')),
            "malformed-utf8": (auth, _FakeResponse(b"\xff\xfe")),
            "oversized": (
                auth,
                _FakeResponse(b"A" * (zai_acquisition.MAX_BODY_BYTES + 1)),
            ),
            "empty-body": (auth, _FakeResponse(b"")),
            "http-401": (auth, _http_error(401)),
            "http-403": (auth, _http_error(403)),
            "redirect-302": (auth, _http_error(302, FAKE_REDIRECT_TARGET)),
            "network": (auth, urllib.error.URLError("unreachable")),
            "timeout": (auth, TimeoutError()),
            "bad-status-line": (
                auth,
                BadStatusLine("TEST_ONLY_MALFORMED_STATUS"),
            ),
            "line-too-long": (
                auth,
                LineTooLong("TEST_ONLY_OVERLONG_LINE"),
            ),
            "credential-missing": (absent, _FakeResponse(b"{}")),
            "credential-wrong-type": (bearer, _FakeResponse(b"{}")),
            "unrelated-only": (unrelated, _FakeResponse(b"{}")),
            "mixed-file-401": (mixed, _http_error(401)),
            "credential-CR": (auth_with(CR_SECRET), _FakeResponse(b"{}")),
            "credential-LF": (auth_with(LF_SECRET), _FakeResponse(b"{}")),
            "credential-CRLF": (auth_with(CRLF_SECRET), _FakeResponse(b"{}")),
            "credential-NUL": (auth_with(NUL_SECRET), _FakeResponse(b"{}")),
            "credential-control": (auth_with(CONTROL_SECRET), _FakeResponse(b"{}")),
            "credential-HTAB": (auth_with(HTAB_SECRET), _FakeResponse(b"{}")),
            "credential-DEL": (auth_with(DEL_SECRET), _FakeResponse(b"{}")),
            "credential-nonlatin1": (
                auth_with(NON_LATIN1_SECRET),
                _FakeResponse(b"{}"),
            ),
            "duplicate-top-level": (
                raw_auth(duplicate_top_level),
                _FakeResponse(b"{}"),
            ),
            "duplicate-key-field": (
                raw_auth(duplicate_key_field),
                _FakeResponse(b"{}"),
            ),
            "duplicate-type-field": (
                raw_auth(duplicate_type_field),
                _FakeResponse(b"{}"),
            ),
        }
        return scenarios[name]

    def test_no_scenario_leaks_any_synthetic_secret(self) -> None:
        names = [
            "valid-200",
            "schema-body",
            "malformed-utf8",
            "oversized",
            "empty-body",
            "http-401",
            "http-403",
            "redirect-302",
            "network",
            "timeout",
            "bad-status-line",
            "line-too-long",
            "credential-missing",
            "credential-wrong-type",
            "unrelated-only",
            "mixed-file-401",
            "credential-CR",
            "credential-LF",
            "credential-CRLF",
            "credential-NUL",
            "credential-control",
            "credential-HTAB",
            "credential-DEL",
            "credential-nonlatin1",
            "duplicate-top-level",
            "duplicate-key-field",
            "duplicate-type-field",
        ]
        for name in names:
            with self.subTest(scenario=name):
                self._refresh_capture()
                auth_file, outcome = self._build_scenario(name)
                _ = self._install_opener(outcome)
                snapshot = self._collect(auth_file)
                text = _serialized(snapshot)
                for secret in ALL_FAKE_SECRETS:
                    self.assertNotIn(secret, text)
                self.assertNotIn(FAKE_REDIRECT_TARGET, text)
                for diagnostic in snapshot.diagnostics:
                    for secret in ALL_FAKE_SECRETS:
                        self.assertNotIn(secret, repr(diagnostic))
                self._assert_no_output()

    def test_success_path_emits_no_stdout_or_stderr(self) -> None:
        _ = self._install_opener(
            _FakeResponse(_fixture_bytes("quota-200-known-windows.json"))
        )
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "ok")
        self._assert_no_output()

    def test_failure_path_emits_no_stdout_or_stderr(self) -> None:
        _ = self._install_opener(_http_error(401))
        snapshot = self._collect(self._write_auth(_valid_auth_payload()))
        self.assertEqual(snapshot.status, "auth_required")
        self._assert_no_output()
