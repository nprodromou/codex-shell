#!/usr/bin/env python3
"""Smoke test entrypoint mode (WOVED-147).

Slot worker pods boot with a long-lived OAuth credential file inherited
from the slot's PersistentVolumeClaim. The slot model assumes those
credentials remain usable across image rotations — but four things can
silently break that assumption:

  1. Refresh token expired on the wall clock.
  2. CLI auth format changed in a backward-incompatible way.
  3. uid/gid mismatch between the image that wrote the file and the
     image now reading it (defended at build time by the WOVED-147
     pin to uid 10001 in codex-shell + worker images).
  4. Stricter cred-format check on a newer CLI version.

This script is the first-boot probe that catches #1, #2, and #4 before
the slot starts accepting tasks. Used as a kubernetes startupProbe on
slot worker pods (WOVED-152 wires that). Fast-fail with a structured
exit code so the Manager can decide whether to enqueue a re-auth
ticket vs treat as transient.

Exit codes:
  0   — credentials present + parseable + CLI binary works. Slot ready.
  64  — CLI binary missing or non-executable. Image-level failure;
        not recoverable by re-auth. Manager should escalate.
  65  — credentials file missing. Slot not yet initialized OR PVC
        ownership mismatch (uid pin failed). Manager enqueues
        slot-init ticket.
  66  — credentials file present but unreadable / unparseable / empty.
        Suspect refresh-token expiry or format change. Manager enqueues
        re-auth ticket with [for-nate] + decision-needed.

Why these specific codes: 64-78 are the conventional "user-defined"
range in sysexits.h; we pick three contiguous slots so the Manager
can map them to a single dispatch table. Exit 1 stays reserved for
"crashed before a check could fire" so the Manager treats it as a
transient probe failure rather than an auth issue.

Stdlib only — same constraint as worker.py + auth_init.py. The slot
pod's smoke probe runs early in boot, before any pip install would
have a chance to land.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# ---- Per-agent paths + binaries ----

# Where each agent CLI persists its OAuth credentials. These are the
# paths the upstream CLIs write at /login time; nothing in this script
# manipulates the format itself, just verifies presence + parseability.
_CRED_PATHS: dict[str, Path] = {
    "claude": Path.home() / ".claude" / "credentials.json",
    # Codex CLI (OpenAI) writes to ~/.codex/auth.json, set by the
    # entrypoint's CODEX_SESSION → file write at first boot.
    "codex": Path.home() / ".codex" / "auth.json",
}

# CLI binary name on PATH for each agent. `--version` is the cheapest
# call that proves the binary loads (exits 0 with a version string).
# Not the same as exercising the credential — that's the file check.
_CLI_BINARIES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
}

# Exit code constants. Mirror sysexits.h conventions where possible
# (codes 64–78 are user-defined). Manager-side dispatch table keys
# off these values, so do NOT renumber without bumping the Manager
# side in lockstep.
_EXIT_OK = 0
_EXIT_CLI_BROKEN = 64
_EXIT_CREDS_MISSING = 65
_EXIT_CREDS_INVALID = 66


def _log(msg: str) -> None:
    """Single-line prefixed log to stderr — matches auth_init.py /
    worker.py style. Manager parses these to surface in the slot's
    diagnostic panel; keep them tight + unambiguous."""
    print(f"smoke-test: {msg}", file=sys.stderr, flush=True)


def _emit_result(status: str, **fields: object) -> None:
    """Single JSON line on stdout summarizing the check outcome.
    Manager-side startupProbe parser consumes this; the Plane
    re-auth ticket (when filed) quotes the fields verbatim."""
    payload = {"status": status, **fields}
    print(json.dumps(payload), flush=True)


def _check_cli_binary(agent: str) -> tuple[bool, str]:
    """Run `<binary> --version` with a short timeout. The binary name
    comes from the agent → binary map; PATH resolution is handled by
    the subprocess invocation. A non-zero exit OR a missing binary
    OR a hang past the timeout all map to "broken"."""
    binary = _CLI_BINARIES.get(agent)
    if binary is None:
        return False, f"no CLI mapped for agent {agent!r}"
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, f"binary {binary!r} not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"{binary} --version timed out"
    if proc.returncode != 0:
        # Trim stderr — it's free-form and could be long.
        return False, f"{binary} --version exited {proc.returncode}: {proc.stderr.strip()[:200]}"
    return True, proc.stdout.strip().splitlines()[0] if proc.stdout else ""


def _check_credentials(agent: str) -> tuple[int, str, dict[str, object]]:
    """Return (exit_code, message, diagnostic_fields).

    The credential file's exact schema is the upstream CLI's concern
    — we only verify it's present, non-empty, and parses as JSON. The
    JSON parse is the cheapest "format sanity" check; if the upstream
    CLI ever switches to a binary format we can rev this, but that's
    exactly the kind of change WOVED-147 case #4 is designed to catch.

    Returns 0 on healthy, _EXIT_CREDS_MISSING / _EXIT_CREDS_INVALID
    otherwise, with `diagnostic_fields` carrying any signal worth
    surfacing to the operator in the re-auth ticket (file size, mtime,
    parse error message)."""
    cred_path = _CRED_PATHS.get(agent)
    if cred_path is None:
        return _EXIT_CREDS_MISSING, f"no credential path mapped for agent {agent!r}", {}

    if not cred_path.exists():
        return _EXIT_CREDS_MISSING, f"credentials file missing at {cred_path}", {
            "path": str(cred_path),
        }

    try:
        size = cred_path.stat().st_size
    except OSError as exc:
        # Permission denied here usually means uid mismatch (the WOVED-147
        # case #3 the build-time pin is supposed to prevent). Surface
        # that distinction in the diagnostic.
        return _EXIT_CREDS_INVALID, f"stat({cred_path}) failed: {exc}", {
            "path": str(cred_path),
            "errno": getattr(exc, "errno", None),
        }
    if size == 0:
        return _EXIT_CREDS_INVALID, f"credentials file at {cred_path} is empty", {
            "path": str(cred_path),
            "size": 0,
        }

    try:
        raw = cred_path.read_bytes()
    except OSError as exc:
        return _EXIT_CREDS_INVALID, f"read({cred_path}) failed: {exc}", {
            "path": str(cred_path),
            "errno": getattr(exc, "errno", None),
        }
    try:
        json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _EXIT_CREDS_INVALID, f"credentials at {cred_path} not valid JSON: {exc}", {
            "path": str(cred_path),
            "size": size,
            "parse_error": str(exc)[:200],
        }
    return _EXIT_OK, f"credentials at {cred_path} parse OK ({size} bytes)", {
        "path": str(cred_path),
        "size": size,
    }


def main() -> int:
    agent = os.environ.get("WOVED_TASK_AGENT", "").strip()
    if not agent:
        _log("FATAL: WOVED_TASK_AGENT env is unset")
        _emit_result("error", reason="missing-env", env="WOVED_TASK_AGENT")
        return _EXIT_CLI_BROKEN

    cli_ok, cli_msg = _check_cli_binary(agent)
    if not cli_ok:
        _log(f"CLI check failed: {cli_msg}")
        _emit_result("cli-broken", agent=agent, detail=cli_msg)
        return _EXIT_CLI_BROKEN

    cred_code, cred_msg, cred_fields = _check_credentials(agent)
    if cred_code != _EXIT_OK:
        status_label = "creds-missing" if cred_code == _EXIT_CREDS_MISSING else "creds-invalid"
        _log(f"credentials check failed: {cred_msg}")
        _emit_result(status_label, agent=agent, detail=cred_msg, **cred_fields)
        return cred_code

    _log(f"OK — {cli_msg}; {cred_msg}")
    _emit_result("ok", agent=agent, cli=cli_msg, **cred_fields)
    return _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
