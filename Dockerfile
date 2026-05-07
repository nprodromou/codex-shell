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
    PATH=/home/${AGENT}/.local/bin:/home/${AGENT}/.agent-config/scripts:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# System deps + gh + tmux + Node + bubblewrap + standard CLI utilities.
# ttyd is fetched separately below — Debian Bookworm doesn't carry it.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg less vim sudo tini \
        bash-completion locales tmux unzip zip openssh-client \
        build-essential python3 python3-pip python3-venv \
        ripgrep fd-find dnsutils iputils-ping \
        bubblewrap \
        passwd; \
    # Node.js from NodeSource (pinned major version). NodeSource ships
    # npm slightly behind upstream; we keep what they bundle since
    # `npm install -g npm@latest` triggers a self-upgrade module-resolution
    # bug at build time, and the bundled version works fine for installing
    # the agent CLIs.
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}.x" | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
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

# Debian renames fd → fdfind; expose canonical name codex/claude expect.
RUN ln -sf /usr/bin/fdfind /usr/local/bin/fd

# ----------------------------------------------------------------------
# Toolchain pinned to apk8s/.mise.toml — the canonical version source.
# When apk8s bumps a tool there, bump the matching ARG here in lockstep.
# Direct-install rather than mise-runtime so versions are reproducible
# from the image itself (no per-pod tool downloads, no PVC bloat, no
# trust prompts).
# ----------------------------------------------------------------------

# Python ecosystem (uv-managed; system python3 stays as Debian default
# for tools that hard-code /usr/bin/python3).
ARG UV_VERSION=0.10.7
ARG PYTHON_VERSION=3.14.3
ARG PIPX_VERSION=1.8.0
ARG MAKEJINJA_VERSION=2.8.2
RUN set -eux; \
    arch="$(uname -m)"; \
    curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${arch}-unknown-linux-gnu.tar.gz" \
        | tar -xz -C /tmp; \
    mv "/tmp/uv-${arch}-unknown-linux-gnu/uv"  /usr/local/bin/uv; \
    mv "/tmp/uv-${arch}-unknown-linux-gnu/uvx" /usr/local/bin/uvx; \
    rm -rf "/tmp/uv-${arch}-unknown-linux-gnu"; \
    uv --version; \
    uvx --version; \
    # Pinned Python via uv (system-wide install path); symlink the
    # binary somewhere predictable so pipx / others can `--python` it.
    UV_PYTHON_INSTALL_DIR=/opt/python uv python install "${PYTHON_VERSION}"; \
    PYTHON_BIN="$(find /opt/python -path '*/bin/python3.14' -type f -executable | head -1)"; \
    test -x "${PYTHON_BIN}" || (echo "did not find python3.14 binary under /opt/python" >&2; exit 1); \
    ln -sf "${PYTHON_BIN}" /usr/local/bin/python3.14; \
    # pipx (kept on PATH for any user / script that wants it; uses system
    # python3 by default — pass --python /usr/local/bin/python3.14 for
    # tools that require >=3.12).
    pip3 install --no-cache-dir --break-system-packages "pipx==${PIPX_VERSION}"; \
    pipx --version; \
    # makejinja via uv (pipx's default 3.11 doesn't satisfy
    # makejinja>=3.12 requirement; uv tool install + --python pins it
    # cleanly without needing pipx flag dance).
    UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
        uv tool install --python "${PYTHON_VERSION}" "makejinja==${MAKEJINJA_VERSION}"; \
    makejinja --version

# Infra CLIs — apk8s/.mise.toml versions. Single-binary github releases
# unless noted. Grouped into one RUN to keep layers tight; each tool
# version-prints at the end so build logs catch a bad URL fast.
ARG KUBECTL_VERSION=v1.35.2
ARG HELM_VERSION=v4.1.1
ARG FLUX_VERSION=2.8.1
ARG SOPS_VERSION=v3.12.1
ARG AGE_VERSION=v1.3.1
ARG CUE_VERSION=v0.15.4
ARG TASK_VERSION=v3.48.0
ARG KUSTOMIZE_VERSION=v5.7.1
ARG YQ_VERSION=v4.52.4
ARG JQ_VERSION=jq-1.8.1
ARG TALOSCTL_VERSION=v1.12.4
ARG KUBECONFORM_VERSION=v0.7.0
ARG HELMFILE_VERSION=v1.3.2
ARG TALHELPER_VERSION=v3.1.5
ARG CILIUM_CLI_VERSION=v0.19.2
ARG GH_VERSION=v2.87.3
ARG CLOUDFLARED_VERSION=2026.2.0
ARG OP_VERSION=2.31.1
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    # ----- raw single-binary downloads -----
    # kubectl
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" -o /usr/local/bin/kubectl; \
    chmod +x /usr/local/bin/kubectl; kubectl version --client=true --output=yaml | head -2; \
    # cloudflared
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${arch}" -o /usr/local/bin/cloudflared; \
    chmod +x /usr/local/bin/cloudflared; cloudflared --version | head -1; \
    # sops
    curl -fsSL "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${arch}" -o /usr/local/bin/sops; \
    chmod +x /usr/local/bin/sops; sops --version | head -1; \
    # jq (pinned, replaces apt-installed jq if any).
    curl -fsSL "https://github.com/jqlang/jq/releases/download/${JQ_VERSION}/jq-linux-${arch}" -o /usr/local/bin/jq; \
    chmod +x /usr/local/bin/jq; jq --version; \
    # yq
    curl -fsSL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${arch}" -o /usr/local/bin/yq; \
    chmod +x /usr/local/bin/yq; yq --version; \
    # talosctl
    curl -fsSL "https://github.com/siderolabs/talos/releases/download/${TALOSCTL_VERSION}/talosctl-linux-${arch}" -o /usr/local/bin/talosctl; \
    chmod +x /usr/local/bin/talosctl; talosctl version --client | head -2; \
    # ----- tar.gz extractions -----
    # helm (4.x layout: linux-${arch}/helm)
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" | tar -xz -C /tmp; \
    mv "/tmp/linux-${arch}/helm" /usr/local/bin/helm; rm -rf "/tmp/linux-${arch}"; \
    helm version --short; \
    # flux
    curl -fsSL "https://github.com/fluxcd/flux2/releases/download/v${FLUX_VERSION}/flux_${FLUX_VERSION}_linux_${arch}.tar.gz" | tar -xz -C /tmp; \
    mv /tmp/flux /usr/local/bin/flux; flux --version; \
    # age + age-keygen
    curl -fsSL "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-${arch}.tar.gz" | tar -xz -C /tmp; \
    mv /tmp/age/age /tmp/age/age-keygen /usr/local/bin/; rm -rf /tmp/age; \
    age --version; age-keygen --version 2>&1 | head -1 || true; \
    # cue
    curl -fsSL "https://github.com/cue-lang/cue/releases/download/${CUE_VERSION}/cue_${CUE_VERSION}_linux_${arch}.tar.gz" | (mkdir -p /tmp/cue.d && tar -xz -C /tmp/cue.d); \
    mv /tmp/cue.d/cue /usr/local/bin/cue; rm -rf /tmp/cue.d; \
    cue version | head -2; \
    # task
    curl -fsSL "https://github.com/go-task/task/releases/download/${TASK_VERSION}/task_linux_${arch}.tar.gz" | (mkdir -p /tmp/task.d && tar -xz -C /tmp/task.d); \
    mv /tmp/task.d/task /usr/local/bin/task; rm -rf /tmp/task.d; \
    task --version; \
    # kustomize
    curl -fsSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_${arch}.tar.gz" | tar -xz -C /tmp; \
    mv /tmp/kustomize /usr/local/bin/kustomize; \
    kustomize version; \
    # kubeconform
    curl -fsSL "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-${arch}.tar.gz" | (mkdir -p /tmp/kc.d && tar -xz -C /tmp/kc.d); \
    mv /tmp/kc.d/kubeconform /usr/local/bin/kubeconform; rm -rf /tmp/kc.d; \
    kubeconform -v; \
    # helmfile (releases tag is vX.Y.Z, asset name is helmfile_X.Y.Z_linux_amd64.tar.gz)
    HELMFILE_VER="${HELMFILE_VERSION#v}"; \
    curl -fsSL "https://github.com/helmfile/helmfile/releases/download/${HELMFILE_VERSION}/helmfile_${HELMFILE_VER}_linux_${arch}.tar.gz" | (mkdir -p /tmp/hf.d && tar -xz -C /tmp/hf.d); \
    mv /tmp/hf.d/helmfile /usr/local/bin/helmfile; rm -rf /tmp/hf.d; \
    helmfile --version; \
    # talhelper
    curl -fsSL "https://github.com/budimanjojo/talhelper/releases/download/${TALHELPER_VERSION}/talhelper_linux_${arch}.tar.gz" | (mkdir -p /tmp/th.d && tar -xz -C /tmp/th.d); \
    mv /tmp/th.d/talhelper /usr/local/bin/talhelper; rm -rf /tmp/th.d; \
    talhelper --version; \
    # cilium-cli
    case "${arch}" in amd64) cilium_arch=amd64 ;; arm64) cilium_arch=arm64 ;; esac; \
    curl -fsSL "https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-${cilium_arch}.tar.gz" | tar -xz -C /tmp; \
    mv /tmp/cilium /usr/local/bin/cilium; \
    cilium version --client; \
    # gh CLI (pinned; replaces the apt cli/cli source we used earlier).
    GH_VER="${GH_VERSION#v}"; \
    curl -fsSL "https://github.com/cli/cli/releases/download/${GH_VERSION}/gh_${GH_VER}_linux_${arch}.tar.gz" | tar -xz -C /tmp; \
    mv "/tmp/gh_${GH_VER}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf "/tmp/gh_${GH_VER}_linux_${arch}"; \
    gh --version | head -1; \
    # ----- 1Password CLI (zip) -----
    curl -fsSL "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_VERSION}/op_linux_${arch}_v${OP_VERSION}.zip" -o /tmp/op.zip; \
    unzip -d /tmp/op /tmp/op.zip; mv /tmp/op/op /usr/local/bin/op; rm -rf /tmp/op /tmp/op.zip; \
    op --version

# Per-agent CLI install. Both are npm packages; the global install puts
# `codex` or `claude` on PATH for the non-root user. Versions are pinned
# so a rebuild from the same commit produces the same agent CLI behavior.
# Bump these in lockstep with intentional CLI upgrades.
ARG CODEX_CLI_VERSION=0.129.0
ARG CLAUDE_CLI_VERSION=2.1.132
RUN case "$AGENT" in \
      codex)  npm install -g "@openai/codex@${CODEX_CLI_VERSION}" ;; \
      claude) npm install -g "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}" ;; \
    esac && npm cache clean --force

# Record the resolved CLI versions in image metadata so a built image
# advertises which agent CLI it shipped with — visible via
# `docker inspect` and surfaced in the entrypoint startup banner.
LABEL org.opencontainers.image.title="codex-shell-${AGENT}" \
      com.prodromou.codex-shell.codex-cli-version="${CODEX_CLI_VERSION}" \
      com.prodromou.codex-shell.claude-cli-version="${CLAUDE_CLI_VERSION}"
ENV CODEX_CLI_VERSION=${CODEX_CLI_VERSION} \
    CLAUDE_CLI_VERSION=${CLAUDE_CLI_VERSION}

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
