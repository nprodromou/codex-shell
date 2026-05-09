#!/usr/bin/env python3
"""Headless worker mode for codex-shell pods (WOVED-126).

Spawned by the woveD Manager as a one-shot Job (chart/templates/worker-job.yaml).
Reads a single task from the Manager callback API, invokes the agent CLI in
print mode with permission bypass, posts the result back, and exits.

Lifecycle:
    1. Fetch task details from Manager callback (WOVED-45 endpoint).
    2. Build prompt from task title + description.
    3. Run the agent CLI:
         claude  →  claude -p "<prompt>" --dangerously-skip-permissions
         codex   →  codex exec "<prompt>" --dangerously-bypass-approvals-and-sandbox
       Permission bypass is required because no human is in the loop to approve
       per-action prompts; print mode is required because bare `claude` /
       `codex` drops into a REPL waiting on stdin and never exits.
    4. POST the agent's stdout back as a Plane comment via Manager callback.
    5. POST the lifecycle state transition (Done on success, needs-followup
       handled by the Manager reconciler on non-zero exit).
    6. Exit with the agent's return code.

Stdlib only — no extra deps to vendor into the image.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# These env vars are set by the chart's worker-job.yaml at spawn time.
# See manager/src/woved_manager/spawn.py:Spawner.build_manifest.
TASK_ID = os.environ.get("WOVED_TASK_ID", "")
TASK_SOURCE = os.environ.get("WOVED_TASK_SOURCE_NAME", "")
TASK_AGENT = os.environ.get("WOVED_TASK_AGENT", "")
CALLBACK_URL = os.environ.get("WOVED_MANAGER_CALLBACK_URL", "").rstrip("/")

# Per-CLI invocation. The Manager doesn't pass these through env — the
# entrypoint knows the right shape per AGENT.
AGENT_CMDS = {
    "claude": ["claude", "-p", "{prompt}", "--dangerously-skip-permissions"],
    "codex": ["codex", "exec", "{prompt}", "--dangerously-bypass-approvals-and-sandbox"],
}


def _die(msg: str, code: int = 1) -> None:
    print(f"worker: FATAL: {msg}", file=sys.stderr)
    sys.exit(code)


def _http_get(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        _die(f"GET {url} → {exc.code}: {body[:500]}")
    except urllib.error.URLError as exc:
        _die(f"GET {url} → transport error: {exc}")
    return {}  # unreachable; _die exits


def _http_post(url: str, body: dict[str, Any]) -> None:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # Manager replies 204 No Content on success; just drain.
            resp.read()
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        # Don't die on a callback failure — log and continue. The pod
        # exit code still reflects the agent's exit; the Manager
        # reconciler (WOVED-46) will surface a missing callback as a
        # needs-followup if state never transitions.
        print(f"worker: POST {url} → {exc.code}: {body_text[:500]}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"worker: POST {url} → transport error: {exc}", file=sys.stderr)


def _validate_env() -> None:
    missing = [
        name
        for name, val in (
            ("WOVED_TASK_ID", TASK_ID),
            ("WOVED_TASK_SOURCE_NAME", TASK_SOURCE),
            ("WOVED_TASK_AGENT", TASK_AGENT),
            ("WOVED_MANAGER_CALLBACK_URL", CALLBACK_URL),
        )
        if not val
    ]
    if missing:
        _die(f"missing required env vars: {', '.join(missing)}")
    if TASK_AGENT not in AGENT_CMDS:
        _die(
            f"unknown agent {TASK_AGENT!r}; supported: {sorted(AGENT_CMDS)}"
        )


def _fetch_task() -> dict[str, Any]:
    qs = urllib.parse.urlencode({"agent": TASK_AGENT})
    url = f"{CALLBACK_URL}/tasks/{urllib.parse.quote(TASK_ID, safe='')}?{qs}"
    print(f"worker: fetching task {TASK_ID} from {url}", file=sys.stderr)
    return _http_get(url)


def _build_prompt(task: dict[str, Any]) -> str:
    """Combine task title + description into a single prompt string.

    Keeps it minimal — the agent has CLAUDE.md / AGENTS.md as the
    standing instruction file (symlinked by the entrypoint from
    nprodromou/agent-config). The prompt just delivers the task itself.
    """
    title = (task.get("title") or "").strip()
    desc = (task.get("description") or "").strip()
    if not title and not desc:
        _die(f"task {TASK_ID} has no title or description")
    parts = []
    if title:
        parts.append(f"# {title}")
    if desc:
        parts.append(desc)
    return "\n\n".join(parts)


def _run_agent(prompt: str) -> int:
    """Invoke the agent CLI. Streams its stdout to ours so the Job log
    captures it; returns the agent's exit code."""
    template = AGENT_CMDS[TASK_AGENT]
    cmd = [arg.replace("{prompt}", prompt) for arg in template]
    print(f"worker: invoking {cmd[0]} (prompt {len(prompt)} chars)", file=sys.stderr)
    # Capture stdout for the comment callback while also letting the Job
    # log see it (tee). The agent's stderr passes through unchanged so
    # debugging output isn't mixed into the comment.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        cwd=os.path.expanduser("~/workspace"),
    )
    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    rc = proc.wait()
    _post_comment("".join(captured), rc)
    return rc


def _post_comment(stdout_text: str, exit_code: int) -> None:
    """Post the agent's stdout as an HTML-encoded comment on the task.

    Wrap in <pre> to preserve formatting. Truncate at 60KB so a runaway
    agent doesn't hit Plane's comment-size limit (Plane caps at ~64KB).
    """
    body = stdout_text[:60_000]
    if len(stdout_text) > 60_000:
        body += "\n\n[... output truncated at 60KB]"
    # Minimal HTML escape — Plane's comment_html field accepts arbitrary
    # HTML, but we want to preserve agent-emitted code blocks etc.
    safe = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    header = (
        f"<p><strong>Worker {TASK_AGENT}</strong> exited {exit_code}.</p>"
        if exit_code != 0
        else f"<p><strong>Worker {TASK_AGENT}</strong> completed.</p>"
    )
    body_html = f"{header}<pre>{safe}</pre>"
    url = f"{CALLBACK_URL}/tasks/{urllib.parse.quote(TASK_ID, safe='')}/comment"
    _http_post(
        url,
        {"source": TASK_SOURCE, "agent": TASK_AGENT, "body_html": body_html},
    )


def _post_transition(state: str) -> None:
    url = f"{CALLBACK_URL}/tasks/{urllib.parse.quote(TASK_ID, safe='')}/transition"
    _http_post(
        url,
        {"source": TASK_SOURCE, "agent": TASK_AGENT, "state": state},
    )


def main() -> int:
    _validate_env()
    task = _fetch_task()
    prompt = _build_prompt(task)
    rc = _run_agent(prompt)
    # On clean exit, transition to done. On non-zero, leave the state
    # alone — the Manager reconciler (WOVED-46) will catch the failed
    # Job and apply needs-followup with diagnostic context. Doing the
    # transition here on failure would race with the reconciler.
    if rc == 0:
        _post_transition("done")
    return rc


if __name__ == "__main__":
    sys.exit(main())
