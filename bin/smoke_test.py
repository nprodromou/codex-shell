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

# Each agent has its own credential-detection strategy because the
# upstream CLIs disagree about how stable their credential filenames
# are:
#
#   claude  — Claude Code's exact credential filename is NOT stable
#             across CLI versions. auth_init.py deliberately uses a
#             snapshot-diff over the entire ~/.claude/ tree to detect
#             "auth happened" without pinning a path. Smoke test
#             matches that model: walk ~/.claude/ for any non-symlink
#             regular file outside the pre-populated baseline. Codex
#             cross-review of the first cut (codex-shell#21) caught
#             that pinning `~/.claude/credentials.json` would
#             permanently false-fail healthy slots whose CLI wrote to
#             e.g. `~/.claude/.credentials/session.json`.
#   codex   — Codex CLI pins ~/.codex/auth.json; the entrypoint also
#             writes there from CODEX_SESSION at first boot. Stable
#             contract; check the path directly.

_CLAUDE_HOME: Path = Path.home() / ".claude"
_CODEX_AUTH_PATH: Path = Path.home() / ".codex" / "auth.json"

# Files under ~/.claude/ that the entrypoint pre-populates BEFORE the
# OAuth flow runs. Their presence does NOT prove the slot is auth'd;
# only artifacts beyond this set count. Sourced empirically from
# codex-shell's entrypoint (CLAUDE.md is a symlink to agent-config;
# config.toml comes from /etc/claude-defaults/ when present).
# Symlinks are filtered separately — this set guards file artifacts
# that the entrypoint may copy in even when the symlink path is taken.
_CLAUDE_BASELINE_NAMES: frozenset[str] = frozenset({
    "CLAUDE.md",
    "config.toml",
    "settings.json",
})

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


def _check_codex_credentials() -> tuple[int, str, dict[str, object]]:
    """Codex CLI pins ~/.codex/auth.json. The entrypoint also writes
    it from CODEX_SESSION at first boot. Stable contract — check the
    path directly, parse as JSON, surface size + parse-error in the
    diagnostic so the operator's re-auth ticket has signal."""
    cred_path = _CODEX_AUTH_PATH

    if not cred_path.exists():
        return _EXIT_CREDS_MISSING, f"credentials file missing at {cred_path}", {
            "path": str(cred_path),
        }

    try:
        size = cred_path.stat().st_size
    except OSError as exc:
        # Permission denied here usually means uid mismatch (the
        # WOVED-147 case #3 the build-time pin is supposed to prevent).
        # Surface that distinction in the diagnostic.
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


def _check_claude_credentials() -> tuple[int, str, dict[str, object]]:
    """Claude Code's credential filename is NOT stable across CLI
    versions — auth_init.py uses a snapshot-diff over the entire
    ~/.claude/ tree to detect "auth happened" without pinning a path.
    Smoke test matches that model: walk the tree for any non-symlink
    regular file outside the entrypoint's pre-populated baseline. If
    any candidate exists, the slot has been initialized.

    Codex cross-review of codex-shell#21 caught the original pinned-
    path implementation false-failing on slots whose CLI wrote to e.g.
    `~/.claude/.credentials/session.json` — the exact failure mode the
    auth_init.py snapshot-diff comment warns about.

    No JSON parse here: the snapshot-diff approach in auth_init.py
    deliberately doesn't parse either, because the format may differ
    across CLI versions and a parse-failure on a real-but-unfamiliar
    artifact would be a worse failure than a false-pass on a corrupt
    one (which the next real task would catch immediately)."""
    if not _CLAUDE_HOME.is_dir():
        return _EXIT_CREDS_MISSING, f"credentials dir missing at {_CLAUDE_HOME}", {
            "path": str(_CLAUDE_HOME),
        }

    candidates: list[Path] = []
    try:
        for root, _dirs, files in os.walk(_CLAUDE_HOME, followlinks=False):
            for name in files:
                full = Path(root) / name
                # Skip symlinks — CLAUDE.md is a symlink to agent-config
                # that the entrypoint pre-populates. Same skip the
                # auth_init.py snapshot-diff does, same reason.
                try:
                    st = full.lstat()
                except OSError:
                    continue
                import stat as _stat

                if _stat.S_ISLNK(st.st_mode):
                    continue
                # Skip well-known baseline names that the entrypoint
                # may copy in even without auth-init having ever run.
                if name in _CLAUDE_BASELINE_NAMES:
                    continue
                candidates.append(full)
    except OSError as exc:
        # Permission denied at the directory level usually means uid
        # mismatch (WOVED-147 case #3) — same diagnostic shape as the
        # Codex stat() failure path so the Manager's dispatch table
        # treats them uniformly.
        return _EXIT_CREDS_INVALID, f"walk({_CLAUDE_HOME}) failed: {exc}", {
            "path": str(_CLAUDE_HOME),
            "errno": getattr(exc, "errno", None),
        }

    if not candidates:
        return _EXIT_CREDS_MISSING, (
            f"no non-baseline files found under {_CLAUDE_HOME} — "
            "slot has not run auth-init yet"
        ), {
            "path": str(_CLAUDE_HOME),
            "baseline_skipped": sorted(_CLAUDE_BASELINE_NAMES),
        }

    # At least one credential-bearing artifact exists. Surface the
    # candidate paths in the diagnostic so the Manager + operator can
    # see what's there without needing to kubectl exec into the pod.
    relpaths = sorted(str(p.relative_to(_CLAUDE_HOME)) for p in candidates)
    return _EXIT_OK, (
        f"{len(candidates)} non-baseline file(s) under {_CLAUDE_HOME}: "
        f"{relpaths[:5]}"
        + (f" (+{len(candidates) - 5} more)" if len(candidates) > 5 else "")
    ), {
        "path": str(_CLAUDE_HOME),
        "artifact_count": len(candidates),
        "artifacts": relpaths[:10],
    }


def _check_credentials(agent: str) -> tuple[int, str, dict[str, object]]:
    """Per-agent dispatch. claude uses snapshot-diff-style walk;
    codex uses pinned path. See module docstring + each helper."""
    if agent == "claude":
        return _check_claude_credentials()
    if agent == "codex":
        return _check_codex_credentials()
    return _EXIT_CREDS_MISSING, f"no credential check mapped for agent {agent!r}", {}


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
