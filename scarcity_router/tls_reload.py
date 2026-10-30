"""Verified preflight for externally managed TLS certificate replacement.

This module does not obtain certificates, change key files, or schedule work.
It validates the exact loaded server context with a local TLS handshake before
any listener may adopt it.
"""

from __future__ import annotations

import ssl


def verified_server_context(
    certfile: str, keyfile: str, *, hostname: str, ca_file: str | None = None
) -> ssl.SSLContext:
    """Load a key pair and verify its chain, hostname and validity offline.

    ``ca_file`` supplements the system roots; it is explicitly configured by
    the operator, not discovered or transferred from a worker. The handshake
    is entirely in memory and never transmits credentials or contacts a host.
    """
    if not hostname:
        raise ValueError("TLS reload requires a verification hostname")
    try:
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.load_default_certs()
        if ca_file is not None:
            client_context.load_verify_locations(cafile=ca_file)
        client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        client = client_context.wrap_bio(
            client_in, client_out, server_hostname=hostname
        )
        server = server_context.wrap_bio(server_in, server_out, server_side=True)
        client_done = False
        server_done = False
        for _ in range(32):
            if not client_done:
                try:
                    client.do_handshake()
                    client_done = True
                except ssl.SSLWantReadError:
                    pass
            outgoing = client_out.read()
            if outgoing:
                _ = server_in.write(outgoing)
            if not server_done:
                try:
                    server.do_handshake()
                    server_done = True
                except ssl.SSLWantReadError:
                    pass
            outgoing = server_out.read()
            if outgoing:
                _ = client_in.write(outgoing)
            if client_done and server_done:
                return server_context
        raise ValueError("TLS replacement verification did not complete")
    except ssl.SSLCertVerificationError as exc:
        reason = {
            9: "certificate not yet valid",
            10: "certificate expired",
            18: "certificate chain is untrusted",
            19: "certificate chain is untrusted",
            20: "certificate chain is untrusted",
            21: "certificate chain is untrusted",
            62: "certificate hostname mismatch",
            64: "certificate IP address mismatch",
        }.get(exc.verify_code, "certificate verification failed")
        raise ValueError(f"TLS replacement failed: {reason}") from None
    except (OSError, ssl.SSLError, ValueError):
        raise ValueError("TLS replacement failed certificate, key or trust verification") from None
