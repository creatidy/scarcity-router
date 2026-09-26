# The Scarcity Router server container (M10, issue #95; D-040/D-041).
#
# ONE container running ONE process: the composed server component
# (`python -m scarcity_router.control_server`), which serves the
# OpenAI-compatible execution surface, the authenticated control API, the
# web UI and — when asked — the optional worker-protocol listener, all in
# the same process (D-041). No external database, cache or queue: the
# durable store is one embedded SQLite file under /data.
#
# Security defaults (D-044):
# - runs as a non-root user;
# - binds loopback inside the container by default (LAN exposure requires
#   explicit TLS certificates — see examples/docker-compose.yml);
# - no baked secrets, no Docker socket, no privileged mode;
# - the only write path is the /data volume;
# - the startup banner and `docker logs` carry the program version.
#
# Build (from a checkout):
#   docker build -t scarcity-router .
#
# Run (loopback-only personal deployment, host networking on Linux):
#   docker run --rm --network host -v scarcity-router-data:/data scarcity-router
#   # then open http://127.0.0.1:8787/admin and complete onboarding
#
# The image is not published to any registry yet. The GHCR publication
# contract (ghcr.io/creatidy/scarcity-router, tags X.Y.Z / X.Y / latest)
# is frozen in docs/release-engineering.md and stays a recorded future
# contract until its release-workflow job lands.

# ── Stage 1: build the wheel from the exact source ───────────────────────────
FROM python:3.12-slim AS build

WORKDIR /build

# The minimal wheel build set (mirrors the sdist contract): the package, the
# root-authoritative artifacts hatchling force-includes, and packaging metadata.
# tests/test_docker_packaging.py pins this parity: every wheel force-include
# source must be present in this build stage.
COPY pyproject.toml README.md LICENSE ./
COPY model-catalog.json model-policy.json model-tracks.json ./
COPY examples/selector-policy.json examples/selector-policy.json
COPY scarcity_router/ scarcity_router/

# Build the one wheel; hatchling is fetched from PyPI as the build backend.
RUN pip wheel --no-deps --wheel-dir /dist .

# ── Stage 2: the runtime image ────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="scarcity-router"
LABEL org.opencontainers.image.description="Local decision service that recommends the least scarce capable subscription-backed AI model; optional execution gateway server component"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/creatidy/scarcity-router"

# Non-root runtime identity (regular user, uid/gid 10001); the data
# directory is its only write path.
RUN groupadd --gid 10001 scarcity \
    && useradd --uid 10001 --gid scarcity --create-home \
       --home-dir /var/lib/scarcity-router --shell /usr/sbin/nologin scarcity \
    && mkdir -p /data \
    && chown scarcity:scarcity /data /var/lib/scarcity-router

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

ENV PYTHONUNBUFFERED=1

USER scarcity
WORKDIR /var/lib/scarcity-router

VOLUME ["/data"]

# The composed server: execution surface + control API + web UI in one
# process. Default bind is loopback INSIDE the container; a LAN-facing
# deployment passes --host/--tls-certfile/--tls-keyfile (verified TLS is
# required for any non-loopback bind) and enables the worker listener with
# --worker-listen-port. Nothing else is configured by default: the honest
# empty deployment.
EXPOSE 8787
ENTRYPOINT ["python", "-m", "scarcity_router.control_server"]
CMD ["--host", "127.0.0.1", "--port", "8787", "--data-dir", "/data"]

# Liveness against the one unauthenticated endpoint (GET /healthz returns
# {"status":"ok"}); it invokes no collector and reads no store. The probe
# targets the container's fixed internal port: keep the internal listener
# on 8787 and adjust exposure on the host side (port publishing, TLS bind).
# TLS deployments (any non-loopback bind requires TLS) set SR_HEALTHCHECK_URL
# to the https:// URL and SR_HEALTHCHECK_CA to the mounted CA certificate, so
# the probe speaks VERIFIED TLS with the same CA the clients use — the
# healthcheck never weakens certificate verification. SR_HEALTHCHECK_URL must
# name a host or IP that appears in the certificate's SAN list (e.g. point it
# at the certificate's DNS name, or issue the certificate with a 127.0.0.1 IP
# SAN when probing the loopback URL).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, ssl, urllib.request; url = os.environ.get('SR_HEALTHCHECK_URL', 'http://127.0.0.1:8787/healthz'); ca = os.environ.get('SR_HEALTHCHECK_CA'); context = ssl.create_default_context(cafile=ca) if ca else None; response = urllib.request.urlopen(url, timeout=4, context=context); raise SystemExit(0 if response.status == 200 else 1)"]
