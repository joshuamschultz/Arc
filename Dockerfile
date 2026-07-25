# Arc — single-image deployment (local, single node, or cloud VM).
#
# One process serves the dashboard, the web-chat WebSocket, and every enabled
# remote platform in-process (the embedded-gateway pattern, SPEC-023). The
# image is immutable: the repo and its venv live at /opt/arc, and every byte of
# customer state lives on the /data volume.
#
# HOME=/data is load-bearing, not cosmetic. Arc resolves its config dir
# (${ARC_CONFIG_DIR:-~/.arc}), its store (~/.arc/store), and its identity keys
# (~/.arcagent/keys) from the home directory, so pointing HOME at the volume
# puts all three on persistent storage without threading a separate env var
# through each one.
#
# Build (both architectures — DGX Spark is aarch64, cloud VMs are x86_64):
#   docker buildx build --platform linux/amd64,linux/arm64 -t arc:latest .

# ---------------------------------------------------------------------------
# Builder — assembles /opt/arc (repo + venv + model weights).
#
# This stage exists purely to collapse layers. Installing the workspace, then
# torch, then sentence-transformers, then the model rewrites files inside the
# venv on each step, and every rewritten file is stored again in the next
# layer: ~4 GB of layers for a 1.4 GB result. Copying the finished tree into a
# clean stage ships it once. That difference is paid on every `docker pull`, on
# every customer VM, inside the provisioning time budget.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/arc/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/opt/arc/models

WORKDIR /opt/arc
COPY . /opt/arc

RUN uv sync --frozen --no-dev

# arcmemory's default embed backend is "local" (arcmemory/provider.py) on
# all-MiniLM-L6-v2 (arcllm/embeddings.py), which needs sentence-transformers.
# Without it, semantic recall and consolidation dedup degrade to a silent
# no-op — the single most expensive install failure this project has hit. It
# ships in the image so a working default is not something anyone can forget.
#
# Torch comes from PyTorch's CPU index on every architecture. The default PyPI
# wheel drags the CUDA runtime — ~3 GB of nvidia-* packages that never execute
# on a CPU-only VM — and it does so on aarch64 as well as x86_64, so this is
# not an amd64-only concern. Installing torch first means sentence-transformers
# resolves against the CPU build instead of pulling the CUDA one back in.
RUN uv pip install --python "$UV_PROJECT_ENVIRONMENT" \
      torch --index-url https://download.pytorch.org/whl/cpu \
 && uv pip install --python "$UV_PROJECT_ENVIRONMENT" "sentence-transformers>=3.0"

# Bake the model weights in rather than pulling them on first boot: it removes
# minutes from cold start, removes HuggingFace from the provisioning critical
# path, and is the only version that works air-gapped.
RUN "$UV_PROJECT_ENVIRONMENT/bin/python" -c \
      "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# TARGETARCH is supplied by buildx as "amd64"/"arm64" — the exact spelling
# nats-io uses for its release artifacts.
ARG TARGETARCH
ARG NATS_VERSION=2.14.3

# curl backs the HEALTHCHECK below; git is needed by the capability loader.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# arcteam needs a JetStream broker for the entity registry, signed audit chain,
# and messaging streams. It is not a Python dependency: arcteam auto-spawns
# `nats-server -js` as a supervised child of `arc ui start`, but only if the
# binary is on PATH. Verified against the release's own SHA256SUMS — an
# unverified broker binary is a supply-chain hole (LLM03).
RUN set -eux; \
    tarball="nats-server-v${NATS_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    base="https://github.com/nats-io/nats-server/releases/download/v${NATS_VERSION}"; \
    curl -fsSLO "${base}/${tarball}"; \
    curl -fsSLO "${base}/SHA256SUMS"; \
    grep " ${tarball}\$" SHA256SUMS | sha256sum -c -; \
    tar xzf "${tarball}"; \
    mv nats-server-*/nats-server /usr/local/bin/nats-server; \
    rm -rf "${tarball}" SHA256SUMS nats-server-*; \
    nats-server --version

ENV HOME=/data \
    PATH=/opt/arc/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HF_HOME=/opt/arc/models \
    HF_HUB_OFFLINE=1 \
    ARC_UI_PORT=8420 \
    PYTHONUNBUFFERED=1

RUN useradd --uid 1000 --home-dir /data --shell /usr/sbin/nologin arc \
 && mkdir -p /data \
 && chown arc:arc /data

COPY --from=builder --chown=arc:arc /opt/arc /opt/arc
COPY docker/entrypoint.sh /usr/local/bin/arc-entrypoint
RUN chmod +x /usr/local/bin/arc-entrypoint

USER arc
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8420

# /api/health is deliberately auth-exempt (arcui/auth.py) so liveness probes
# work without credentials.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${ARC_UI_PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/local/bin/arc-entrypoint"]
