# code-server image — VS Code in the browser for apk8s.
#
# Extends the upstream codercom/code-server with:
#   - gh CLI for GitHub operations from the integrated terminal
#   - tmux for persistent terminal sessions
#   - claude (Claude Code) and codex for integrated AI assistance
#   - entrypoint that configures git identity + gh auth from env vars on boot
#
# Deployed at code-server.prodromou.com via WARP private routing
# (envoy-internal gateway, WOVED-35).
#
# Authentication is handled at the edge by WARP/Cloudflare Access;
# code-server itself runs with --auth none inside the cluster.
#
# Build (from repo root):
#   docker build -t ghcr.io/nprodromou/codex-shell:code-server-dev \
#     -f code-server/Dockerfile code-server/

FROM codercom/code-server:latest

USER root

# tmux for persistent terminal sessions.
# git and curl are already present in the upstream image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tmux \
    && rm -rf /var/lib/apt/lists/*

# gh CLI — pinned for reproducibility, bump in lockstep with codex-shell Dockerfile.
# renovate: datasource=github-releases depName=cli/cli extractVersion=^v(?<version>.+)$
ARG GH_VERSION=2.87.3
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
        | tar -xz -C /tmp; \
    mv "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf "/tmp/gh_${GH_VERSION}_linux_${arch}"; \
    gh --version | head -1

# Node.js — required for npm / claude-code.
# codercom/code-server bundles its own Node.js for the IDE but does not
# expose npm on PATH. Install a system-level LTS Node.js so the integrated
# terminal can use npm/npx like any dev machine.
# renovate: datasource=node-version depName=node versioning=node
ARG NODE_VERSION=22
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code + Codex CLI — AI coding assistants for the integrated terminal.
# Versions pinned in lockstep with the main codex-shell Dockerfile.
# DISABLE_AUTOUPDATER: pin is managed by Renovate, not at runtime.
# renovate: datasource=npm depName=@anthropic-ai/claude-code
ARG CLAUDE_CODE_VERSION=2.1.170
# renovate: datasource=npm depName=@openai/codex
ARG OPENAI_CODEX_VERSION=0.130.0
ENV DISABLE_AUTOUPDATER=true
RUN npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${OPENAI_CODEX_VERSION}" \
    && npm cache clean --force

# Startup script: configure git identity + gh auth from env vars, then start code-server.
COPY entrypoint.sh /usr/local/bin/code-server-entrypoint.sh
RUN chmod +x /usr/local/bin/code-server-entrypoint.sh

USER coder

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/code-server-entrypoint.sh"]
