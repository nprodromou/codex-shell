# Multi-agent browser shell image — single-instance ttyd-fronted
# terminal that auto-launches an LLM coding agent. Built once per agent
# via the AGENT build arg (codex|claude). Each agent variant gets its
# own image tag (codex-latest, claude-latest) and is deployed as a
# separate pod in apk8s under kubernetes/apps/agents/<agent>-cli.
#
# Identity is provided at runtime via env vars sourced from a k8s Secret
# backed by 1Password (deploy vault, typically `Kubernetes`). gh + git
# are configured by the entrypoint so commits/PRs from inside the pod
# attribute to <agent>-prodromou.
#
# code-server (VS Code in browser) is intentionally NOT installed here —
# that is a separate concern tracked by WOVED-35.

FROM debian:bookworm-slim

ARG NODE_VERSION=22
ARG AGENT=codex

# Validate AGENT early so an unsupported value fails the build cleanly.
RUN case "$AGENT" in codex|claude) ;; \
      *) echo "Unsupported AGENT: $AGENT (expected codex|claude)" >&2; exit 1 ;; \
    esac

ENV AGENT=${AGENT} \
    DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=America/Los_Angeles \
    HOME=/home/${AGENT} \
    PATH=/home/${AGENT}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# System deps + gh + tmux + Node + bubblewrap + standard CLI utilities.
# ttyd is fetched separately below — Debian Bookworm doesn't carry it.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg jq less vim sudo tini \
        bash-completion locales tmux unzip zip openssh-client \
        build-essential python3 python3-pip \
        bubblewrap \
        passwd; \
    # Node.js from NodeSource (pinned major version). Then upgrade
    # npm to latest — NodeSource lags behind upstream by months.
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}.x" | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    npm install -g npm@latest; \
    # GitHub CLI from official apt repo.
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*

# ttyd — fetch the upstream static binary release.
ARG TTYD_VERSION=1.7.7
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) ttyd_arch="x86_64" ;; \
      arm64) ttyd_arch="aarch64" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${ttyd_arch}" \
      -o /usr/local/bin/ttyd; \
    chmod +x /usr/local/bin/ttyd; \
    ttyd --version

# Per-agent CLI install. Both are npm packages; the global install puts
# `codex` or `claude` on PATH for the non-root user.
RUN case "$AGENT" in \
      codex)  npm install -g @openai/codex ;; \
      claude) npm install -g @anthropic-ai/claude-code ;; \
    esac && npm cache clean --force

# Non-root user. uid/gid 1000, name = AGENT. Matching the AGENT name to
# the user keeps PVC ownership obvious and avoids shell prompts that
# lie about which agent is running.
RUN groupadd -g 1000 ${AGENT} \
    && useradd -m -u 1000 -g 1000 -s /bin/bash ${AGENT} \
    && mkdir -p /home/${AGENT}/.config /home/${AGENT}/workspace \
    && chown -R ${AGENT}:${AGENT} /home/${AGENT}

# Entrypoint + bash profile.
COPY --chmod=0755 bin/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=${AGENT}:${AGENT} profile/.bashrc    /home/${AGENT}/.bashrc
COPY --chown=${AGENT}:${AGENT} profile/.tmux.conf /home/${AGENT}/.tmux.conf

USER ${AGENT}
WORKDIR /home/${AGENT}/workspace

EXPOSE 7681

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
