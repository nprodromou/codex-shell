#!/usr/bin/env bash
# Codex CLI pod entrypoint.
# 1. Wires gh + git identity from env (mounted by k8s Secret from 1Password).
# 2. Exposes the configured codex-cli shell over ttyd on port 7681.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN must be set (1Password: agents-codex/github_pat)}"
: "${GIT_USER_NAME:=Codex CoWork}"
: "${GIT_USER_EMAIL:=codex@prodromou.com}"

# Codex CLI config — sourced from a k8s ConfigMap mounted at
# /etc/codex-config/. The ConfigMap (managed in the apk8s repo) is the
# source of truth; we copy its contents into ~/.codex/ on every boot,
# overwriting any in-pod edits. To add MCPs or tweak config, edit the
# ConfigMap and push — Stakater Reloader will restart this pod.
if [ -d /etc/codex-config ]; then
    mkdir -p "${HOME}/.codex"
    # cp -L follows symlinks (configmap mounts are symlink farms).
    cp -fL /etc/codex-config/. "${HOME}/.codex/" 2>/dev/null || true
    chmod -R u+w "${HOME}/.codex" 2>/dev/null || true
fi

# git identity — applies to every commit made inside the pod.
git config --global user.name  "${GIT_USER_NAME}"
git config --global user.email "${GIT_USER_EMAIL}"
git config --global init.defaultBranch main
git config --global pull.rebase false

# Prefer HTTPS over SSH so the gh-managed token is used.
git config --global url."https://github.com/".insteadOf "git@github.com:"

# Make `gh` the credential helper for HTTPS clones (uses GH_TOKEN automatically).
gh auth setup-git

# Plane Gateway env (consumed by tools/scripts the user runs in-shell).
# PLANE_TOKEN is the codex-prodromou Plane API key.
# PLANE_GATEWAY_URL points at the n8n Plane Gateway v2.1 webhook.
export PLANE_GATEWAY_URL="${PLANE_GATEWAY_URL:-https://n8n.prodromou.com/webhook/plane-gateway-v21}"

# Identity banner — surfaced by the bash prompt on login.
cat > "${HOME}/.codex-identity" <<EOF
GitHub user : codex-prodromou (token loaded)
Git author  : ${GIT_USER_NAME} <${GIT_USER_EMAIL}>
Plane gw    : ${PLANE_GATEWAY_URL}
Codex CLI   : $(codex --version 2>/dev/null || echo unknown)
EOF

# ttyd flags:
#   -W           : writable (input enabled)
#   -p 7681      : listen port
#   -t titleFixed: avoids leaking shell pid/host into the title
#   -T xterm-256color : sane terminal
#   bash -l      : login shell so .bashrc runs
exec ttyd \
    --writable \
    --port 7681 \
    --terminal-type xterm-256color \
    --client-option titleFixed='codex-cli' \
    --client-option fontSize=14 \
    bash -l
