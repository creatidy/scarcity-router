#!/usr/bin/env python3
"""Bounded M1 reconnaissance: Z.ai Coding Plan quota endpoint (read-only).

This is an evidence-acquisition helper for docs/poc-evidence.md, not a
production adapter. It performs exactly one read-only request family:

    GET https://api.z.ai/api/monitor/usage/quota/limit

Credential boundary (enforced in-process):
- Only the `zai-coding-plan` entry of the Kilo auth file is read; no other
  entry is touched and the file contents are never printed.
- The credential value never appears in stdout, stderr, exceptions, arguments
  or persisted output. Every candidate output string is scrubbed in-process
  against the credential before printing (defense in depth).
- Scheme (https) and exact host (api.z.ai) are validated before the
  Authorization header is attached.
- Redirects are never followed; a 3xx is reported by its Location scheme/host.

Output: a single JSON object of allowlisted, redacted facts on stdout.
Attempt cap: two authenticated attempts — first the raw stored value (the
documented PoC header shape), then a `Bearer ` prefix fallback. A 2xx stops
the loop.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROVIDER_ENTRY = "zai-coding-plan"
DEFAULT_AUTH_FILE = Path.home() / ".local" / "share" / "kilo" / "auth.json"
URL = "https://api.z.ai/api/monitor/usage/quota/limit"
ALLOWED_HOST = "api.z.ai"
TIMEOUT_S = 15
MAX_SAFE_STRING = 48
TEXT_SNIPPET_LEN = 160
TOKEN_MIN_LEN = 8
MIN_SUBSTRING_SCRUB = 8
MAX_BODY_BYTES = 65536
API_KINDS = ("api", "apikey", "api_key", "token")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def type_name(node: object) -> str:
    return type(node).__name__


def shape_of(node: object) -> object:
    """Recursive type/shape description; carries no values."""
    if isinstance(node, list):
        return {"kind": "list", "length": len(node)}
    if isinstance(node, dict):
        return {
            "kind": "dict",
            "size": len(node),
            "fields": {str(k): type_name(v) for k, v in node.items()},
        }
    return type_name(node)


def find_entry(data: object, provider: str) -> object:
    """Locate the dict for one provider id in a plausible auth-file layout."""
    if isinstance(data, dict):
        if provider in data and isinstance(data[provider], dict):
            return data[provider]
        for key in ("entries", "providers", "auth", "credentials", "accounts"):
            if key in data:
                found = find_entry(data[key], provider)
                if found is not None:
                    return found
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                for key in ("name", "id", "provider", "providerId", "provider_id"):
                    if item.get(key) == provider:
                        return item
                found = find_entry(item, provider)
                if found is not None:
                    return found
    return None


def extract_token(entry: object) -> tuple[str | None, str]:
    """Pull (value, credential_type) out of an entry layout we recognize."""
    if not isinstance(entry, dict):
        return None, "unknown"
    cred = entry.get("credential")
    if isinstance(cred, dict):
        kind = cred.get("type")
        kind = kind if isinstance(kind, str) and kind else "unknown"
        for key in ("value", "token", "apiKey", "api_key", "key"):
            val = cred.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip(), kind
    for key in ("value", "token", "apiKey", "api_key", "key", "tokenValue"):
        val = entry.get(key)
        if isinstance(val, str) and len(val.strip()) >= TOKEN_MIN_LEN:
            kind = entry.get("credentialType") or entry.get("authType")
            kind = kind if isinstance(kind, str) and kind else "unknown"
            return val.strip(), kind
    return None, "unknown"


def load_credential(auth_file: Path) -> tuple[str, str]:
    try:
        raw = auth_file.read_text(encoding="utf-8")
    except OSError:
        sys.stderr.write("AUTH_ERROR auth file not readable\n")
        sys.exit(2)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("AUTH_ERROR auth file is not valid JSON\n")
        sys.exit(2)
    entry = find_entry(data, PROVIDER_ENTRY)
    if entry is None:
        sys.stderr.write(f"AUTH_ERROR provider entry {PROVIDER_ENTRY!r} not found\n")
        sys.exit(2)
    value, kind = extract_token(entry)
    if value is None:
        sys.stderr.write("AUTH_ERROR credential value not recognized in entry\n")
        sys.exit(2)
    return value, kind


def clean_string(value: str, token: str) -> str:
    if token:
        if token in value:
            return "<REDACTED>"
        if len(token) >= MIN_SUBSTRING_SCRUB:
            for probe in (token[:16], token[-16:]):
                if probe in value:
                    return "<REDACTED>"
    return value[:MAX_SAFE_STRING]


def clean(node: object, token: str) -> object:
    if isinstance(node, str):
        return clean_string(node, token)
    if isinstance(node, list):
        return [clean(v, token) for v in node]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            key = clean_string(str(k), token) if isinstance(k, str) else str(k)
            out[key] = clean(v, token)
        return out
    return node


def location_info(url: str) -> dict:
    parts = urllib.parse.urlsplit(url)
    info: dict[str, object] = {}
    if parts.scheme:
        info["scheme"] = parts.scheme
    if parts.hostname:
        info["host"] = parts.hostname
    if parts.path:
        info["path"] = parts.path[:MAX_SAFE_STRING]
    return info


def try_json(body: bytes) -> object | None:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8", errors="strict"))
    except Exception:
        return None


def do_request(token: str, header_shape: str) -> dict:
    parsed = urllib.parse.urlsplit(URL)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        sys.stderr.write("REFUSED_URL_INVALID scheme/host not in provider allowlist\n")
        sys.exit(3)
    header_value = token if header_shape == "raw" else f"Bearer {token}"
    req = urllib.request.Request(
        URL,
        data=None,
        headers={
            "Authorization": header_value,
            "Accept": "application/json",
            "User-Agent": "scarcity-router-m1-recon/0.1",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(NoRedirect())
    started = time.perf_counter()
    try:
        resp = opener.open(req, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        headers = {k: str(v) for k, v in (exc.headers.items() if exc.headers else [])}
        body = b""
        try:
            body = exc.read(MAX_BODY_BYTES + 1)
        except Exception:
            pass
        return {
            "http_code": exc.code,
            "final_url": exc.url,
            "headers": headers,
            "body": body,
            "body_json": try_json(body),
            "duration_ms": duration_ms,
            "header_shape": header_shape,
        }
    except (urllib.error.URLError, OSError) as exc:
        detail = exc.__class__.__name__
        reason = getattr(exc, "reason", None)
        if isinstance(reason, BaseException):
            detail += f" ({reason.__class__.__name__})"
        sys.stderr.write(f"NETWORK_ERROR {detail}\n")
        sys.exit(4)
    duration_ms = int((time.perf_counter() - started) * 1000)
    headers = {k: str(v) for k, v in resp.headers.items()}
    body = resp.read(MAX_BODY_BYTES + 1)
    try:
        resp.close()
    except Exception:
        pass
    return {
        "http_code": resp.status,
        "final_url": resp.url,
        "headers": headers,
        "body": body,
        "body_json": try_json(body),
        "duration_ms": duration_ms,
        "header_shape": header_shape,
    }


def build_report(
    token: str,
    credential_kind: str,
    attempts: int,
    result: dict,
) -> dict:
    code = result["http_code"]
    headers = result["headers"]
    body = result["body"]
    body_json = result["body_json"]
    report: dict[str, object] = {
        "recon": "zai-coding-plan-quota",
        "generated_at": now_iso(),
        "request": {
            "method": "GET",
            "scheme": "https",
            "host": ALLOWED_HOST,
            "path": "/api/monitor/usage/quota/limit",
            "host_validated_before_auth": True,
            "followed_redirect": False,
        },
        "auth": {
            "provider_entry": PROVIDER_ENTRY,
            "credential_type": credential_kind,
            "value_length": len(token),
            "value_redacted": True,
            "header_shape_used": result["header_shape"],
        },
        "attempts_executed": attempts,
        "status": code if isinstance(code, int) else str(code),
        "content_type_header": str(headers.get("Content-Type", ""))[:MAX_SAFE_STRING],
        "duration_ms": result["duration_ms"],
    }
    if 300 <= code <= 399 and "Location" in headers:
        report["redirect"] = location_info(headers["Location"])
        report["redirect"]["auth_forwarded"] = False
    if body_json is not None:
        report["response"] = {
            "kind": "json",
            "shape": shape_of(body_json),
            "redacted": clean(body_json, token),
        }
    elif body:
        text = body.decode("utf-8", errors="replace")[:TEXT_SNIPPET_LEN]
        report["response"] = {"kind": "text", "snippet": clean_string(text, token)}
    else:
        report["response"] = {"kind": "note", "note": "empty body"}
    return report


def main() -> None:
    token, kind = load_credential(DEFAULT_AUTH_FILE)
    if kind not in API_KINDS and len(token) < 16 and not token.startswith("sk"):
        sys.stderr.write("AUTH_REFUSED credential shape not recognized; not sending\n")
        sys.exit(5)
    already_prefixed = token.lower().startswith("bearer ")
    if already_prefixed:
        shapes = ["raw"]
    else:
        shapes = ["raw", "bearer"]
    result: dict | None = None
    executed = 0
    for shape in shapes:
        executed += 1
        result = do_request(token, shape)
        code = result["http_code"]
        if isinstance(code, int) and 200 <= code < 300:
            break
    if result is None:
        sys.stderr.write("NO_ATTEMPT no request performed\n")
        sys.exit(6)
    report = build_report(token, kind, executed, result)
    out = json.dumps(report, ensure_ascii=True, indent=2)
    if (
        token
        and (
            token in out
            or (
                len(token) >= MIN_SUBSTRING_SCRUB
                and (token[:16] in out or token[-16:] in out)
            )
        )
    ):
        sys.stderr.write("LEAK_DETECTED aborting without printing\n")
        sys.exit(7)
    sys.stdout.write(out + "\n")


if __name__ == "__main__":
    main()
