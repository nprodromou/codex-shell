# code-server image — VS Code in the browser for apk8s.
#
# Extends the upstream codercom/code-server with:
#   - gh CLI for GitHub operations from the integrated terminal
#   - tmux for persistent terminal sessions
#   - claude (Claude Code) for integrated AI assistance
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

# Claude Code — AI coding assistant accessible from the integrated terminal.
# Pinned version kept in lockstep with the main codex-shell Dockerfile.
# DISABLE_AUTOUPDATER: pin is managed by Renovate, not the runtime.
# renovate: datasource=npm depName=@anthropic-ai/claude-code
ARG CLAUDE_CODE_VERSION=2.1.170
ENV DISABLE_AUTOUPDATER=true
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# Startup script: configure git identity + gh auth from env vars, then start code-server.
COPY entrypoint.sh /usr/local/bin/code-server-entrypoint.sh
RUN chmod +x /usr/local/bin/code-server-entrypoint.sh

USER coder

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/code-server-entrypoint.sh"]
