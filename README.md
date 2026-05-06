# codex-apk8s

Container image for running [OpenAI codex-cli](https://github.com/openai/codex)
as a single, browser-accessible instance on the `apk8s` Kubernetes cluster.

## What it is

A long-running pod that exposes a `bash` shell with `codex` (and `gh`, `git`,
`tmux`, etc.) on `PATH` over [ttyd](https://github.com/tsl0922/ttyd). Hit it
from a WARP-enrolled browser at `codex.prodromou.com` and you get a terminal.
Identity is locked to `codex-prodromou` so commits, PRs, and Plane tickets
attribute deterministically — no more `gh auth` collisions with whichever
identity a developer machine logged in last.

This image is the runtime; the cluster manifests live in
[`nprodromou/apk8s` → `kubernetes/apps/agents/codex-cli`](https://github.com/nprodromou/apk8s).

`code-server` (VS Code in the browser) is intentionally **not** in this image
— see WOVED-35 for that.

## Image

```
ghcr.io/nprodromou/codex-apk8s:latest
```

Built by `.github/workflows/build.yml` on push to `main` or version tag.

## Runtime contract

The entrypoint requires the following environment variables. They are mounted
into the pod by an `ExternalSecret` that pulls from the deploy's 1Password
vault (typically `Kubernetes`) per the canonical [Agent Secret Naming
Convention](https://prodromou.atlassian.net/wiki/spaces/Operations/pages/63438850).

| Env var          | 1Password reference                              | Purpose                                            |
| ---------------- | ------------------------------------------------ | -------------------------------------------------- |
| `GH_TOKEN`       | `op://Kubernetes/codex-github-pat/pat`           | GitHub PAT (`codex-prodromou`); used by `gh`       |
| `CODEX_SESSION`  | `op://Kubernetes/codex-session/session`          | OpenAI Codex CLI auth blob                         |
| `PLANE_TOKEN`    | `op://Kubernetes/codex-plane-token/token`        | Plane API key for `codex-prodromou` workspace user |
| `GIT_USER_NAME`  | `op://Kubernetes/codex-github-pat/git_user_name` | Defaults to `Codex CoWork`                         |
| `GIT_USER_EMAIL` | `op://Kubernetes/codex-github-pat/git_user_email` | Defaults to `codex@prodromou.com`                 |

Optional:

| Env var             | Default                                                 |
| ------------------- | ------------------------------------------------------- |
| `PLANE_GATEWAY_URL` | `https://n8n.prodromou.com/webhook/plane-gateway-v21`   |

## Ports

| Port | Purpose            |
| ---- | ------------------ |
| 7681 | ttyd (HTTP / WS)   |

## Persistence

The pod's `/home/codex` is backed by a Longhorn `ReadWriteOnce` PVC declared in
the apk8s manifests. That gives you durable shell history, codex-cli session
state, and any cloned repos under `~/workspace`.

## Developing locally

```sh
# Build
docker build -t codex-apk8s:dev .

# Run with the env vars the entrypoint expects.
docker run --rm -it -p 7681:7681 \
  -e GH_TOKEN="$(gh auth token)" \
  -e GIT_USER_NAME="Local Test" \
  -e GIT_USER_EMAIL="$(git config user.email)" \
  codex-apk8s:dev
```

Then open <http://localhost:7681>.

## Notes

- The image runs as non-root `codex` (uid 1000).
- `tini` is PID 1 so zombie reaping is handled.
- `tmux` is preinstalled — start a session with `tmux` and your shell survives
  closing the browser tab; `tmux attach` to reconnect.
- `gh` uses `GH_TOKEN` automatically; no interactive `gh auth login` needed.
- HTTPS clones via `gh` are seamless because `gh auth setup-git` runs at boot.
