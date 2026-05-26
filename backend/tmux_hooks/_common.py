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
import sys
import time

CTRL = os.environ.get("VC_CTRL", "")


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
    return os.path.join(CTRL, "decisions", f"{tool_use_id or 'none'}.json")


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
