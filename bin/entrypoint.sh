#!/usr/bin/env bash
# Multi-agent shell pod entrypoint.
# 1. Wires gh + git identity from env (mounted by k8s Secret from 1Password).
# 2. Bootstraps per-agent config + auth.
# 3. Pulls nprodromou/agent-config and symlinks the shared instructions
#    file into the agent's expected path (~/.codex/AGENTS.md or
#    ~/.claude/CLAUDE.md), so every fresh pod starts with the canonical
#    Nate-org instructions in place.
# 4. Exposes the configured agent shell over ttyd on port 7681,
#    auto-resuming the last session (or starting a fresh one).
set -euo pipefail

# AGENT is set by the Dockerfile based on the build-time AGENT arg.
: "${AGENT:?AGENT must be set by the image (build bug if missing)}"

# Common env-var contract — required for every agent.
: "${GH_TOKEN:?GH_TOKEN must be set (1Password: op://Kubernetes/${AGENT}-github-pat/pat)}"
: "${GIT_USER_NAME:=${AGENT^} CoWork}"
: "${GIT_USER_EMAIL:=${AGENT}@prodromou.com}"

# Workspace defense — if the PVC's contents have stale ownership
# (e.g. pre-fsGroup pod created files as root), the agent user can't
# clone or write here. We can't chown across uids without root, but we
# can ensure ~/workspace exists, is owned by us, and is writable. Any
# stale subdirs will still error if the agent tries to write under them
# — the manifest carries fsGroupChangePolicy=Always to recursively
# repair on next mount; this block is defense-in-depth.
mkdir -p "${HOME}/workspace" 2>/dev/null || true
if [ -w "${HOME}/workspace" ]; then
    chmod u+rwX "${HOME}/workspace" || true
else
    echo "WARNING: ${HOME}/workspace is not writable. Falling back to /tmp/workspace." >&2
    mkdir -p /tmp/workspace
    cd /tmp/workspace
fi

# git identity + gh credential helper — set up early so the
# agent-config clone below can use it for private-repo HTTPS auth.
git config --global user.name  "${GIT_USER_NAME}"
git config --global user.email "${GIT_USER_EMAIL}"
git config --global init.defaultBranch main
git config --global pull.rebase false
git config --global url."https://github.com/".insteadOf "git@github.com:"
gh auth setup-git

# Plane Gateway env (consumed by tools/scripts the user runs in-shell).
# v3 routes per-agent so Plane attribution stays correct; agents must
# include `user: $AGENT` in every gateway request body. v2.1 stays as a
# fallback for ~1 week post-rollout — set PLANE_GATEWAY_URL explicitly
# in the pod env to override.
export PLANE_GATEWAY_URL="${PLANE_GATEWAY_URL:-https://n8n.prodromou.com/webhook/plane-gateway-v3}"

# Plane API key alias — the m365-mcp repos' wrapper scripts (and the
# agent-config plane MCP) read PLANE_API_KEY; our ExternalSecret names
# it PLANE_TOKEN per the WOVED-36 secret-naming convention. Bridge the
# two so scripts work without per-repo retrofits.
if [ -n "${PLANE_TOKEN:-}" ] && [ -z "${PLANE_API_KEY:-}" ]; then
    export PLANE_API_KEY="${PLANE_TOKEN}"
fi

# Per-agent config + auth bootstrap. Each agent variant declares:
#   AGENT_CONFIG_DIR    — where the agent CLI looks for config (~/.codex, ~/.claude)
#   AGENT_CONFIG_SOURCE — ConfigMap mount path for managed config
#   AGENT_LAUNCH_CMD    — what ttyd runs on connect (auto-resume + bash fallback)
#   INSTRUCTIONS_LINK   — agent-specific path where the shared CLAUDE.md
#                         from agent-config gets symlinked
case "$AGENT" in
codex)
    AGENT_CONFIG_DIR="${HOME}/.codex"
    AGENT_CONFIG_SOURCE="/etc/codex-config"
    AGENT_AUTH_FILE="${AGENT_CONFIG_DIR}/auth.json"
    # Codex looks for AGENTS.md as the global instructions file.
    INSTRUCTIONS_LINK="${AGENT_CONFIG_DIR}/AGENTS.md"
    # Resume the last session; fall back to a fresh codex if there is
    # none, then drop to bash if codex exits.
    AGENT_LAUNCH_CMD='codex resume --last 2>/dev/null || codex; exec bash -l'

    mkdir -p "${AGENT_CONFIG_DIR}"

    # Optional: seed auth.json from CODEX_SESSION on first boot.
    if [ ! -f "${AGENT_AUTH_FILE}" ] && [ -n "${CODEX_SESSION:-}" ]; then
        printf '%s' "${CODEX_SESSION}" > "${AGENT_AUTH_FILE}"
        chmod 600 "${AGENT_AUTH_FILE}"
    fi
    ;;
claude)
    AGENT_CONFIG_DIR="${HOME}/.claude"
    AGENT_CONFIG_SOURCE="/etc/claude-config"
    INSTRUCTIONS_LINK="${AGENT_CONFIG_DIR}/CLAUDE.md"
    # Continue the most recent session; fall back to fresh claude if
    # none exists, then bash if claude exits.
    AGENT_LAUNCH_CMD='claude --continue 2>/dev/null || claude; exec bash -l'

    mkdir -p "${AGENT_CONFIG_DIR}"
    # Claude Code uses interactive `/login` on first connect; credentials
    # persist on the PVC at ~/.claude/. No env-var session seed.
    ;;
esac

# Sync managed config from a ConfigMap mounted at /etc/<agent>-config/.
# The ConfigMap (apk8s repo) is the source of truth for model/MCP config;
# in-pod edits get blown away on restart. Stakater Reloader restarts the
# pod when the ConfigMap changes.
if [ -d "${AGENT_CONFIG_SOURCE}" ]; then
    # cp -L follows symlinks (configmap mounts are symlink farms).
    cp -fL "${AGENT_CONFIG_SOURCE}/." "${AGENT_CONFIG_DIR}/" 2>/dev/null || true
    chmod -R u+w "${AGENT_CONFIG_DIR}" 2>/dev/null || true
fi

# Pull nprodromou/agent-config for the canonical Nate-org instructions
# file (CLAUDE.md). Symlinked into the agent's expected location so
# updates to agent-config reach the pod on next restart without an
# image rebuild.
AGENT_CONFIG_REPO_DIR="${HOME}/.agent-config"
if [ ! -d "${AGENT_CONFIG_REPO_DIR}/.git" ]; then
    git clone --depth=1 https://github.com/nprodromou/agent-config "${AGENT_CONFIG_REPO_DIR}" \
        || echo "warning: could not clone agent-config (continuing without shared instructions)" >&2
else
    git -C "${AGENT_CONFIG_REPO_DIR}" fetch --depth=1 origin main 2>/dev/null || true
    git -C "${AGENT_CONFIG_REPO_DIR}" reset --hard origin/main 2>/dev/null || true
fi

if [ -f "${AGENT_CONFIG_REPO_DIR}/instructions/CLAUDE.md" ]; then
    ln -sf "${AGENT_CONFIG_REPO_DIR}/instructions/CLAUDE.md" "${INSTRUCTIONS_LINK}"
fi

# Identity banner — surfaced by the bash prompt on login.
AGENT_CONFIG_SHA="$(git -C "${AGENT_CONFIG_REPO_DIR}" rev-parse --short HEAD 2>/dev/null || echo missing)"
cat > "${HOME}/.${AGENT}-identity" <<EOF
Agent        : ${AGENT}
GitHub user  : ${AGENT}-prodromou (token loaded)
Git author   : ${GIT_USER_NAME} <${GIT_USER_EMAIL}>
Plane gw     : ${PLANE_GATEWAY_URL}
${AGENT^} CLI    : $(${AGENT} --version 2>/dev/null || echo unknown)
agent-config : ${AGENT_CONFIG_SHA}
EOF

# ttyd flags:
#   --writable             : input enabled
#   --port 7681            : listen port
#   titleFixed             : avoids leaking shell pid/host into the title
#   --terminal-type        : sane terminal
#
# AGENT_LAUNCH_CMD auto-resumes the agent's last session. If no session
# exists, falls back to a fresh agent run. If the agent exits or
# crashes, drops to an interactive bash login so the pod isn't bricked.
exec ttyd \
    --writable \
    --port 7681 \
    --terminal-type xterm-256color \
    --client-option titleFixed="${AGENT}-cli" \
    --client-option fontSize=14 \
    bash -lc "${AGENT_LAUNCH_CMD}"
