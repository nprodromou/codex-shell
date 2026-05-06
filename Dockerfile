# Codex CLI agent image — single-instance codex-cli runtime for apk8s.
#
# Runs ttyd → bash with codex-cli on PATH (codex-cli in the browser).
# Identity is provided at runtime via env vars sourced from a k8s Secret
# backed by 1Password (deploy vault, typically `Kubernetes`). gh + git
# are configured by the entrypoint script so commits/PRs from inside the
# pod attribute to codex-prodromou.
#
# code-server (VS Code in browser) is intentionally NOT installed here —
# that is a separate concern tracked by WOVED-35.

ARG NODE_VERSION=22
FROM node:${NODE_VERSION}-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=America/Los_Angeles \
    HOME=/home/codex \
    PATH=/home/codex/.local/bin:/usr/local/bin:/usr/bin:/bin

# System deps + ttyd + gh + tmux + standard CLI utilities.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg jq less vim sudo tini \
        bash-completion locales tmux unzip zip openssh-client \
        build-essential python3 python3-pip \
        ttyd; \
    # GitHub CLI from official apt repo.
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*

# Codex CLI (OpenAI's terminal coding agent).
RUN npm install -g @openai/codex && npm cache clean --force

# Non-root user. Same uid/gid as bjw-s defaults so PVCs work cleanly.
RUN groupadd -g 1000 codex \
    && useradd -m -u 1000 -g 1000 -s /bin/bash codex \
    && mkdir -p /home/codex/.config /home/codex/workspace \
    && chown -R codex:codex /home/codex

# Entrypoint + bash profile.
COPY --chmod=0755 bin/entrypoint.sh        /usr/local/bin/entrypoint.sh
COPY --chown=codex:codex profile/.bashrc   /home/codex/.bashrc
COPY --chown=codex:codex profile/.tmux.conf /home/codex/.tmux.conf

USER codex
WORKDIR /home/codex/workspace

EXPOSE 7681

# tini reaps zombies; entrypoint sets up identity then exec's ttyd.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
