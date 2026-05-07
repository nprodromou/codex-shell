# codex-shell

Multi-agent browser shell image. One Dockerfile, two image variants —
`codex` (OpenAI's [codex-cli](https://github.com/openai/codex)) and
`claude` (Anthropic's [Claude Code](https://github.com/anthropics/claude-code))
— each exposed over ttyd as a single, browser-accessible terminal on
Kubernetes. Identity is locked to a dedicated GitHub user per agent;
home directory persists on a PVC; canonical Nate-org instructions
(from [`nprodromou/agent-config`](https://github.com/nprodromou/agent-config))
are pulled at boot and symlinked into the agent's expected path.

The repo name is a historical artifact — it started as codex-only and
gained the claude variant later. Image content is cluster-agnostic; the
canonical deploy lives in [`nprodromou/apk8s`](https://github.com/nprodromou/apk8s)
under `kubernetes/apps/agents/{codex,claude}-cli`.

`code-server` (VS Code in the browser) is intentionally **not** in this
image — that's a separate concern tracked by WOVED-35.

## Images

| Variant | Tag                                                | Agent CLI                |
| ------- | -------------------------------------------------- | ------------------------ |
| codex   | `ghcr.io/nprodromou/codex-shell:codex-latest`      | `@openai/codex`          |
| claude  | `ghcr.io/nprodromou/codex-shell:claude-latest`     | `@anthropic-ai/claude-code` |

Both are built from the same `Dockerfile` via the `AGENT` build arg
(`codex` or `claude`). The build matrix in `.github/workflows/build.yml`
publishes both variants on every push to `main`. Per-commit tags follow
the pattern `sha-XXXXX-{codex,claude}` for pinning.

## Runtime contract

The entrypoint requires the following environment variables. They are
mounted into the pod by an `ExternalSecret` that pulls from the deploy's
1Password vault (typically `Kubernetes`) per the canonical
[Agent Secret Naming Convention](https://prodromou.atlassian.net/wiki/spaces/Operations/pages/63438850).

### Common (both agents)

| Env var          | 1Password reference                                   | Purpose                                            |
| ---------------- | ----------------------------------------------------- | -------------------------------------------------- |
| `GH_TOKEN`       | `op://Kubernetes/${agent}-github-pat/pat`             | GitHub PAT (`${agent}-prodromou`); used by `gh`    |
| `GIT_USER_NAME`  | `op://Kubernetes/${agent}-github-pat/git_user_name`   | Defaults to `${Agent} CoWork`                      |
| `GIT_USER_EMAIL` | `op://Kubernetes/${agent}-github-pat/git_user_email`  | Defaults to `${agent}@prodromou.com`               |
| `PLANE_TOKEN`    | `op://Kubernetes/${agent}-plane-token/token`          | Plane API key for the agent's workspace user       |

### Codex-specific

| Env var          | 1Password reference                       | Purpose                                                                          |
| ---------------- | ----------------------------------------- | -------------------------------------------------------------------------------- |
| `CODEX_SESSION`  | `op://Kubernetes/codex-session/session`   | OpenAI Codex CLI auth blob. Optional. Seeds `~/.codex/auth.json` on first boot. |

### Claude-specific

Claude Code uses interactive `/login` on first connect — no env-var
session seed. Credentials persist on the PVC at `~/.claude/`.

### Optional (both agents)

| Env var             | Default                                                 |
| ------------------- | ------------------------------------------------------- |
| `PLANE_GATEWAY_URL` | `https://n8n.prodromou.com/webhook/plane-gateway-v21`   |

## How a connect works

1. ttyd accepts the browser connection and runs the configured shell command.
2. Entrypoint has already wired `gh`, `git`, the agent's auth state, and
   pulled the latest `nprodromou/agent-config` into `~/.agent-config`,
   symlinking `instructions/CLAUDE.md` into:
   - **codex:** `~/.codex/AGENTS.md`
   - **claude:** `~/.claude/CLAUDE.md`
3. The shell command attempts to resume the most recent session:
   - **codex:** `codex resume --last`
   - **claude:** `claude --continue`
4. If no prior session exists, falls through to a fresh agent run.
5. If the agent exits or crashes, drops to an interactive bash login so
   the pod isn't bricked.

## Ports

| Port | Purpose            |
| ---- | ------------------ |
| 7681 | ttyd (HTTP / WS)   |

## Persistence

The pod's `/home/${AGENT}` is backed by a Longhorn `ReadWriteOnce` PVC
declared in the apk8s manifests. That gives you durable shell history,
agent session state, persisted auth tokens, and any cloned repos under
`~/workspace`.

## Developing locally

```sh
# Build the codex variant.
docker build -t codex-shell:codex --build-arg AGENT=codex .

# Or the claude variant.
docker build -t codex-shell:claude --build-arg AGENT=claude .

# Run with the env vars the entrypoint expects.
docker run --rm -it -p 7681:7681 \
  -e GH_TOKEN="$(gh auth token)" \
  -e GIT_USER_NAME="Local Test" \
  -e GIT_USER_EMAIL="$(git config user.email)" \
  codex-shell:codex
```

Then open <http://localhost:7681>.

## Notes

- Base is `debian:bookworm-slim`; Node 22 from NodeSource; `npm@latest`
  installed on top because NodeSource lags upstream.
- The image runs as non-root with the user named after the agent
  (`codex` or `claude`), uid/gid 1000. Matches the Longhorn PVC owner so
  volume mounts work cleanly.
- `tini` is PID 1 so zombie reaping is handled.
- `tmux`, `bubblewrap` (codex sandbox prereq), `ripgrep`-equivalents,
  `jq`, `vim`, etc. are preinstalled.
- `gh` uses `GH_TOKEN` automatically; no interactive `gh auth login`
  needed. HTTPS clones via `gh` are seamless because `gh auth setup-git`
  runs at boot.
- Updates to `nprodromou/agent-config` reach the pod on next restart
  (entrypoint pulls and resets to `origin/main`); no image rebuild
  needed for instruction changes.
