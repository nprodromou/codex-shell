#!/bin/sh
set -e

# Configure git identity from env vars injected by the ExternalSecret.
# Without this, commits from the integrated terminal would land with no
# author identity and git would refuse to commit.
if [ -n "$GIT_USER_NAME" ]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [ -n "$GIT_USER_EMAIL" ]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

# Authenticate gh with the GitHub PAT. The token is provided via the
# GH_TOKEN env var from the ExternalSecret, which is the same mechanism
# used by the claude-cli and codex-cli pods.
if [ -n "$GH_TOKEN" ]; then
    echo "$GH_TOKEN" | gh auth login --with-token 2>/dev/null || true
fi

# Start code-server:
#   --auth none        WARP/CF Access handles auth at the edge
#   --disable-telemetry  no usage reporting
#   /home/coder/workspace  default folder to open (mounted from PVC)
exec /usr/bin/code-server \
    --bind-addr 0.0.0.0:8080 \
    --auth none \
    --disable-telemetry \
    /home/coder/workspace \
    "$@"
