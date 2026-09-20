"""Typed outbound HTTP origin and credential configuration (M04, D-044).

The administrator owns every outbound provider origin and credential (M09
owns the storage/UX); adapters never accept a URL, endpoint or credential
from client request content (SSRF protection, D-044). This module is the
typed configuration boundary those administrators (and the tests) build:

- :class:`ProviderOrigin` — one bare ``https://host[:port]`` origin,
  validated exactly like the remote-bridge client's origin discipline:
  verified TLS everywhere by default, plain HTTP only for explicit
  loopback hosts (the bounded D-044 localhost exception, e.g. a loopback
  Ollama), no path prefix, query, fragment or embedded credentials.
- :class:`ProviderCredential` — one provider credential held as transient
  configuration input. Its ``repr`` redacts the value; it travels only
  into the ``Authorization`` header of requests to its bound origin and
  is never logged, returned, serialized or attached to any other origin.

There is deliberately no TLS-verification bypass option anywhere (D-044):
``ssl.create_default_context()`` is the only TLS configuration this
program constructs. Redirect handling is a transport concern
(:mod:`scarcity_router.providers.openai_http_adapter`): redirects are
never followed, so an origin can never be silently changed after
configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import override
from urllib.parse import urlsplit

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

_REDACTED_CREDENTIAL = "<redacted>"

_MAX_HOST_LENGTH = 253
_MAX_CREDENTIAL_LENGTH = 4096


@dataclass(frozen=True)
class ProviderOrigin:
    """One administrator-configured provider origin (D-044 origin rule).

    ``base_url`` must be a bare origin: scheme ``https`` (plain ``http``
    is accepted only for explicit loopback hosts — the bounded D-044
    exception for loopback/LAN-trusted Ollama endpoints), a hostname, an
    optional port, and NO path prefix, query, fragment or embedded
    credentials. Adapters append the preset's evidenced endpoint path;
    nothing else can ever be reached with the bound credential.
    """

    base_url: str
    scheme: str
    hostname: str
    port: int

    @classmethod
    def parse(cls, base_url: object) -> "ProviderOrigin":
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("provider_origin: base_url is required")
        try:
            parts = urlsplit(base_url)
            port = parts.port
        except ValueError as exc:
            raise ValueError("provider_origin: base_url is not a valid origin") from exc
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                "provider_origin: scheme must be https (plain http is accepted "
                + "only for explicit loopback origins)"
            )
        hostname = parts.hostname
        if not hostname:
            raise ValueError("provider_origin: base_url must include a hostname")
        hostname = hostname.lower()
        if len(hostname) > _MAX_HOST_LENGTH:
            raise ValueError("provider_origin: hostname exceeds the maximum length")
        if parts.username is not None or parts.password is not None:
            raise ValueError(
                "provider_origin: base_url must not embed credentials; "
                + "pass the credential separately"
            )
        if parts.query or parts.fragment:
            raise ValueError(
                "provider_origin: base_url must be a bare origin without "
                + "query or fragment"
            )
        if parts.path not in ("", "/"):
            raise ValueError(
                "provider_origin: base_url must be a bare origin without a "
                + "path prefix; the adapter appends the preset's endpoint path"
            )
        if scheme == "http" and hostname not in _LOOPBACK_HOSTS:
            raise ValueError(
                "provider_origin: plain HTTP is permitted only for explicit "
                + "loopback origins (the bounded D-044 exception); non-local "
                + "providers require verified HTTPS"
            )
        resolved_port = port if port is not None else (443 if scheme == "https" else 80)
        return cls(base_url=base_url, scheme=scheme, hostname=hostname, port=resolved_port)

    @property
    def is_loopback(self) -> bool:
        return self.hostname in _LOOPBACK_HOSTS

    @property
    def origin(self) -> str:
        """The safe origin for diagnostics (carries no credential)."""
        return f"{self.scheme}://{self.hostname}:{self.port}"


@dataclass(frozen=True)
class ProviderCredential:
    """One provider credential (transient configuration input, D-044).

    The value is held only in memory by the process that was given it as
    administrator configuration; it travels exclusively into the
    ``Authorization: Bearer`` header of requests to the origin its binding
    configures. The repr redacts the value, and the validation rejects
    values that can never be a usable single header token (whitespace,
    newlines, non-ASCII), so a credential can never leak through a header
    split or a log line by accident. There is deliberately no serialization
    or export of this value anywhere in the program.
    """

    secret: str

    def __post_init__(self) -> None:
        value = self.secret
        if value.__class__ is not str or not value:
            raise ValueError("provider_credential: a credential value is required")
        if len(value) > _MAX_CREDENTIAL_LENGTH:
            raise ValueError("provider_credential: exceeds the maximum length")
        if not value.isascii() or any(character.isspace() for character in value):
            raise ValueError(
                "provider_credential: must be a single ASCII token without "
                + "whitespace"
            )

    @property
    def authorization_header(self) -> str:
        """The ``Authorization`` header value for the bound origin only."""
        return f"Bearer {self.secret}"

    @override
    def __repr__(self) -> str:
        return f"ProviderCredential({_REDACTED_CREDENTIAL!r})"

    @override
    def __str__(self) -> str:
        return _REDACTED_CREDENTIAL


__all__ = ["ProviderCredential", "ProviderOrigin"]
