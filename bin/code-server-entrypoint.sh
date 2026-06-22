#!/usr/bin/env bash
# code-server entrypoint.
#
# Runs as the `coder` user (uid 1000).  On every pod start:
#   1. Wires git identity from GIT_USER_NAME / GIT_USER_EMAIL env vars.
#   2. Logs gh CLI in with GH_TOKEN (if present).
#   3. Execs the upstream code-server binary (reads ~/.config/code-server/
#      config.yaml — mounted from ConfigMap with auth: none).
#
# Identity env vars come from the code-server-secret ExternalSecret in apk8s
# (backed by the `agents-code-server` 1Password item).

set -euo pipefail

# ── Git identity ────────────────────────────────────────────────────────────
if [[ -n "${GIT_USER_NAME:-}" ]]; then
    git config --global user.name "${GIT_USER_NAME}"
fi
if [[ -n "${GIT_USER_EMAIL:-}" ]]; then
    git config --global user.email "${GIT_USER_EMAIL}"
fi

# ── gh auth ────────────────────────────────────────────────────────────────
if [[ -n "${GH_TOKEN:-}" ]]; then
    echo "${GH_TOKEN}" | gh auth login --with-token 2>/dev/null || true
fi

# ── Start code-server ───────────────────────────────────────────────────────
# Config is mounted at ~/.config/code-server/config.yaml (auth: none,
# bind-addr: 0.0.0.0:8080).  Passing the workspace dir as the positional arg
# opens it as the default folder on first load.
exec /usr/bin/code-server /home/coder/workspace
