#!/usr/bin/env python3
"""Slot OAuth init mode for codex-shell pods (WOVED-126).

One-shot init pod that drives the Claude Code device-code OAuth flow
under operator supervision via the woveD Manager. This is the agent-pod
half of the dance described in Confluence page 65961985 and implemented
on the Manager side in `slot_auth.py` + the `/slots/<id>/auth-init/*`
callback endpoints.

Lifecycle:

    1. Spawn `claude` (or codex) in a pseudo-terminal so it behaves
       interactively the way it does at a real shell.
    2. Watch its stdout for the OAuth device-code URL pattern.
       Anthropic's CLI prints something like:
           To continue, please visit: https://...
           and enter the code: ABCD-1234
       (Exact format may vary across CLI versions; the regex below is
       lenient and accepts any https URL plus an optional short code.)
    3. POST the URL + user_code to Manager at
       /slots/<SLOT_ID>/auth-init/url.
    4. Poll /slots/<SLOT_ID>/auth-init/code until the operator submits
       the code via the dashboard. Backoff: 2s between polls; cap at
       AUTH_INIT_TIMEOUT_S total.
    5. Type the code into the PTY so the running `claude` process can
       complete the login.
    6. Wait for the agent process to exit. Verify ~/.claude/ now has
       auth state. Exit 0 on success.

Stdlib only — no extra deps. Uses `pty` + `os.read`/`os.write` because
that's what works portably; `pexpect` would be slightly nicer but adds
a dep that's not worth pinning into the image just for one script.

Operator failure modes:
    - Timeout — operator never pastes the code. Init pod exits
      non-zero; Manager reconciler marks the slot ERROR.
    - Bad code — claude rejects, prints the URL again. The script
      treats this as a fresh URL surface and re-posts it (idempotent).
    - Agent crashes — stdout closes; script exits with the agent's
      return code.

This script is the FIRST DRAFT — `claude /login`'s exact CLI shape +
prompt patterns may need adjustment after a real-pod test pass. The
TODO markers below call out the parts most likely to need iteration.
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Env contract — set by the chart's slot-init Pod manifest at spawn time.
SLOT_ID = os.environ.get("WOVED_SLOT_ID", "")
TASK_AGENT = os.environ.get("WOVED_TASK_AGENT", os.environ.get("AGENT", ""))
CALLBACK_URL = os.environ.get("WOVED_MANAGER_CALLBACK_URL", "").rstrip("/")
AUTH_INIT_TIMEOUT_S = int(os.environ.get("WOVED_AUTH_INIT_TIMEOUT_S", "1800"))  # 30 min default
POLL_INTERVAL_S = int(os.environ.get("WOVED_AUTH_INIT_POLL_S", "2"))

# OAuth URL extraction. Anthropic's flow prints the device URL as a
# plain https link; we accept any https URL on a line that mentions
# `code` or `visit` or `verify` to stay tolerant of CLI version drift.
# TODO(WOVED-126): tighten this once we've seen real `claude /login`
# output and know the exact format.
URL_PATTERN = re.compile(rb"(https://[^\s\"'<>)]+)", re.IGNORECASE)
USER_CODE_PATTERN = re.compile(rb"\b([A-Z0-9]{4,8}-?[A-Z0-9]{4,8})\b")

# Per-agent invocation. Empty for codex — codex doesn't use OAuth, so
# this script is claude-only. If/when Gemini gets an OAuth path, add it
# here with its CLI shape.
AGENT_INIT_CMDS = {
    # TODO(WOVED-126): confirm this is the right invocation. If
    # `claude /login` is a slash command (REPL only), spawn `claude`
    # bare and type `/login\n` after the REPL prompt appears.
    "claude": ["claude"],
}


def _die(msg: str, code: int = 1) -> None:
    print(f"auth-init: FATAL: {msg}", file=sys.stderr)
    sys.exit(code)


def _validate_env() -> None:
    missing = [
        name
        for name, val in (
            ("WOVED_SLOT_ID", SLOT_ID),
            ("WOVED_TASK_AGENT", TASK_AGENT),
            ("WOVED_MANAGER_CALLBACK_URL", CALLBACK_URL),
        )
        if not val
    ]
    if missing:
        _die(f"missing required env vars: {', '.join(missing)}")
    if TASK_AGENT not in AGENT_INIT_CMDS:
        _die(
            f"agent {TASK_AGENT!r} does not need OAuth init "
            f"(supported: {sorted(AGENT_INIT_CMDS)})"
        )


def _http_post(url: str, body: dict) -> int:  # type: ignore[type-arg]
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError as exc:
        print(f"auth-init: POST {url} → transport error: {exc}", file=sys.stderr)
        return 0


def _http_get(url: str) -> tuple[int, dict | None]:  # type: ignore[type-arg]
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except (json.JSONDecodeError, ValueError):
            return exc.code, None
    except urllib.error.URLError as exc:
        print(f"auth-init: GET {url} → transport error: {exc}", file=sys.stderr)
        return 0, None


def _post_url(url: str, user_code: str | None) -> None:
    body: dict = {"url": url}  # type: ignore[type-arg]
    if user_code:
        body["user_code"] = user_code
    target = f"{CALLBACK_URL}/slots/{urllib.parse.quote(SLOT_ID, safe='')}/auth-init/url"
    rc = _http_post(target, body)
    if rc not in (200, 204):
        print(f"auth-init: WARN: POST URL → HTTP {rc}", file=sys.stderr)
    else:
        print(f"auth-init: posted URL to {target}", file=sys.stderr)


def _poll_for_code(deadline: float) -> str | None:
    """Long-poll the Manager for the operator-submitted auth code.

    Returns the code string when received. Returns None on timeout —
    caller should kill the agent process + exit non-zero so the
    Manager reconciler marks the slot ERROR.
    """
    target = f"{CALLBACK_URL}/slots/{urllib.parse.quote(SLOT_ID, safe='')}/auth-init/code"
    while time.time() < deadline:
        status, body = _http_get(target)
        if status == 200 and body and "code" in body:
            print("auth-init: received code from Manager", file=sys.stderr)
            return str(body["code"])
        if status == 404:
            time.sleep(POLL_INTERVAL_S)
            continue
        # Any other status: log + back off + retry.
        print(f"auth-init: WARN: GET code → HTTP {status}, retrying", file=sys.stderr)
        time.sleep(POLL_INTERVAL_S)
    print(f"auth-init: timed out waiting for code after {AUTH_INIT_TIMEOUT_S}s", file=sys.stderr)
    return None


def _drive_login() -> int:
    """Spawn `claude` under a PTY, walk the OAuth dance, return its
    exit code. Long function on purpose — the state machine is
    inherently sequential and splitting it makes the flow harder to
    follow than the inline form."""
    cmd = AGENT_INIT_CMDS[TASK_AGENT]
    print(f"auth-init: spawning {cmd}", file=sys.stderr)

    pid, fd = pty.fork()
    if pid == 0:  # child
        # exec the agent CLI; if it fails, the parent reads EOF.
        os.execvp(cmd[0], cmd)
        return 0  # unreachable

    # Parent: drive the dance.
    deadline = time.time() + AUTH_INIT_TIMEOUT_S
    url_posted = False
    code_typed = False
    buffer = b""
    rep_prompt_seen = False

    try:
        while True:
            # Has the child died?
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
                print(f"auth-init: agent process exited rc={rc}", file=sys.stderr)
                return rc

            # Bound the wait so we can re-check waitpid + deadline.
            ready, _, _ = select.select([fd], [], [], 0.5)
            if fd in ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    # PTY closed; child exited.
                    continue
                # Tee to our stderr so the Job log captures it.
                sys.stderr.buffer.write(chunk)
                sys.stderr.flush()
                buffer += chunk

                if not url_posted:
                    # Look for the URL.
                    url_match = URL_PATTERN.search(buffer)
                    if url_match:
                        url = url_match.group(1).decode("utf-8", errors="replace")
                        # Look for an adjacent user code (best-effort).
                        code_match = USER_CODE_PATTERN.search(buffer)
                        user_code = (
                            code_match.group(1).decode("utf-8", errors="replace")
                            if code_match
                            else None
                        )
                        _post_url(url, user_code)
                        url_posted = True
                        buffer = b""  # clear so we don't re-match the same URL

                # If claude /login is a REPL slash command, look for the
                # first prompt and inject /login. TODO(WOVED-126):
                # confirm whether this is needed; remove the branch if
                # `claude` auto-prompts OAuth on no-auth-state startup.
                if not rep_prompt_seen and not url_posted and (b">" in buffer or b"$" in buffer):
                    print("auth-init: REPL prompt detected, sending /login", file=sys.stderr)
                    os.write(fd, b"/login\n")
                    rep_prompt_seen = True
                    buffer = b""

            if url_posted and not code_typed:
                code = _poll_for_code(deadline)
                if code is None:
                    # Timed out. Kill the agent and bubble up.
                    try:
                        os.kill(pid, 15)  # SIGTERM
                        os.waitpid(pid, 0)
                    except ProcessLookupError:
                        pass
                    return 124  # conventional timeout exit
                # Type the code into the PTY. Trailing \n delivers it.
                os.write(fd, code.encode("utf-8") + b"\n")
                code_typed = True
                print("auth-init: code piped into agent stdin", file=sys.stderr)

            if time.time() > deadline:
                print("auth-init: deadline exceeded", file=sys.stderr)
                try:
                    os.kill(pid, 15)
                    os.waitpid(pid, 0)
                except ProcessLookupError:
                    pass
                return 124
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _verify_auth_landed() -> bool:
    """Sanity-check that ~/.claude/ now has auth state. The exact
    file(s) Claude Code writes vary across versions; we accept any
    non-empty presence under ~/.claude/ that wasn't there before init.

    TODO(WOVED-126): tighten this once we know the exact filenames
    (likely `credentials.json` or similar). For now, presence of any
    file under ~/.claude/ that isn't the symlinked CLAUDE.md
    instructions file is treated as success."""
    claude_dir = os.path.expanduser("~/.claude")
    if not os.path.isdir(claude_dir):
        return False
    for entry in os.listdir(claude_dir):
        path = os.path.join(claude_dir, entry)
        if os.path.islink(path):
            continue  # CLAUDE.md is a symlink to agent-config
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True
        if os.path.isdir(path):
            for _ in os.listdir(path):
                return True
    return False


def main() -> int:
    _validate_env()
    rc = _drive_login()
    if rc != 0:
        print(f"auth-init: agent exited non-zero ({rc})", file=sys.stderr)
        return rc
    if not _verify_auth_landed():
        print(
            "auth-init: agent exited 0 but no auth state found under ~/.claude/ — "
            "treating as failure",
            file=sys.stderr,
        )
        return 1
    print("auth-init: success — slot is ready for tasks", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
