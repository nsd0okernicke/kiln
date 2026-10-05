# syntax=docker/dockerfile:1
# Phase 3: Kiln container image for running one swarm headless.
#
# Build:  docker build -t kiln-runner -f docker/Dockerfile .
# Use:    kiln --docker --run exp-auth --ref feature/auth
#
# The container receives two mounts:
#   /seed   — the host repo, read-only (git clone --local source)
#   /run    — the run directory, read-write (messages.db, status/, logs/)
#
# Entrypoint clones from /seed into /run/workspace, checks out <ref>,
# and launches the Kiln swarm headless.

FROM python:3.11-slim AS base

# Prevent apt from prompting and prevent pip cache layers.
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1

# Install system deps: git, xz (for node tarball), ca-certificates.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        xz-utils \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 22.x from the official binary (NodeSource doesn't support Trixie).
ENV NODE_VERSION=22.14.0
RUN curl -fsSL https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz \
    | tar -xJ -C /usr/local --strip-components=1

# Kiln itself: pip install the framework from the seed mount at runtime.
# The framework is installed from /seed (mounted at container start), not baked in,
# so the container picks up whatever version the host has checked out.

# Trust the seed mount's git directory (owned by a different UID on the host).
RUN git config --global --add safe.directory /seed
RUN git config --global --add safe.directory /run/workspace
RUN git config --global user.email "kiln@container"
RUN git config --global user.name "Kiln Container"

# Install optional agent CLIs that can run headless.
# Claude Code: use pip (the npm approach needs node in the image).
# Codex CLI: pip install openai-codex.
# Grok: pip install grok-cli (if available).
# Copilot is unavailable headless (requires interactive device flow).
ARG INSTALL_CLAUDE=1
ARG INSTALL_CODEX=1
ARG INSTALL_GROK=1

RUN if [ "$INSTALL_CLAUDE" = "1" ]; then \
        pip install --no-cache-dir anthropic-claude 2>/dev/null || true; \
    fi

RUN if [ "$INSTALL_CODEX" = "1" ]; then \
        pip install --no-cache-dir openai-codex 2>/dev/null || true; \
    fi

RUN if [ "$INSTALL_GROK" = "1" ]; then \
        pip install --no-cache-dir grok-cli 2>/dev/null || true; \
    fi

# Install pi (the defacto agent CLI for Kiln roles) globally via npm.
ARG INSTALL_PI=1
RUN if [ "$INSTALL_PI" = "1" ]; then \
        npm install -g @earendil-works/pi-coding-agent \
        && echo "pi installed successfully at $(which pi)" \
        || echo "pi install FAILED"; \
    fi

# Default entrypoint: clone the seed repo, checkout ref, install kiln, launch.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
