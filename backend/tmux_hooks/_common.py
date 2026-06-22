"""Shared helpers for the per-session Claude Code hook scripts.

These run as short-lived subprocesses spawned by the interactive `claude` CLI
(configured via --settings). They talk to TmuxClaudeRunner through files in the
control dir given by the VC_CTRL env var:
  events.jsonl          - append-only event stream the runner tails
  decisions/<id>.json   - the runner writes a permission decision here; the
                          PreToolUse hook polls for it
Stdlib only. stdout must contain ONLY the hook's JSON result.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

CTRL = os.environ.get("VC_CTRL", "")

# tool_use_id is interpolated into a decisions/<id>.json path that reaches
# open()/os.remove()/os.replace(). It is normally a CLI-minted `toolu_*` token,
# but — like every other externally-derived path component in this codebase
# (session_id/handle, validated via tmux_runner._SESSION_ID_RE) — it must be
# validated so it can never escape the decisions/ dir via traversal or
# separators. Same charset/length bound as validate_session_id.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def safe_id(value: str) -> str:
    """Return value if it is a safe single path component, else 'none'."""
    return value if value and _SAFE_ID_RE.match(value) else "none"


def read_input() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def append_event(event: dict) -> None:
    """Atomically append one JSON line to events.jsonl (O_APPEND line write)."""
    if not CTRL:
        return
    event.setdefault("ts", time.time())
    line = json.dumps(event) + "\n"
    path = os.path.join(CTRL, "events.jsonl")
    with open(path, "a") as f:
        f.write(line)
        f.flush()


def decision_path(tool_use_id: str) -> str:
    return os.path.join(CTRL, "decisions", f"{safe_id(tool_use_id)}.json")


def read_mode() -> str:
    """The session's current permission mode, written by the runner. Defaults to
    'default' if missing/unreadable."""
    if not CTRL:
        return "default"
    try:
        with open(os.path.join(CTRL, "mode")) as f:
            return f.read().strip() or "default"
    except Exception:
        return "default"
