#!/usr/bin/env python3
"""PreToolUse hook: enforce the voice permission policy without the TUI menu.

- safe tools  -> emit "allow" immediately (also log a `tool` event so the runner
                 can track tool usage and learn the transcript path early).
- question    -> emit "allow" (so AskUserQuestion's menu renders) + a needs_choice
                 event; the runner drives the menu via send-keys.
- risky tools -> emit a needs_permission event, then BLOCK polling for the
                 runner's decision file, then emit allow/deny. Times out -> deny.

Imported `classify` comes from permissions.py via PYTHONPATH set at launch.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from permissions import classify, is_edit_tool, is_plan_file_write  # noqa: E402
from _common import append_event, decision_path, read_input, read_mode  # noqa: E402

POLL_SECONDS = 590.0  # stay under the 600s hook timeout
POLL_INTERVAL = 0.1


def emit(decision: str, reason: str = "") -> None:
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision}}
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def main() -> None:
    data = read_input()
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    tool_use_id = data.get("tool_use_id") or data.get("tool_use_id", "")
    transcript_path = data.get("transcript_path", "")
    session_id = data.get("session_id", "")
    kind = classify(tool_name)

    base = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
        "transcript_path": transcript_path,
        "session_id": session_id,
    }

    if kind == "safe" or is_plan_file_write(tool_name, tool_input):
        append_event({"event": "tool", **base})
        emit("allow")
        return

    if kind == "question":
        # A question to the user, not a permission — always surfaced, even in
        # auto/acceptEdits (those modes skip permission prompts, not questions).
        append_event({"event": "needs_choice", **base})
        emit("allow")  # let the question menu render; runner drives it
        return

    # risky: honor the session's permission mode so we don't ask for things the
    # mode already auto-approves (this hook runs independently of the CLI's own
    # mode handling, so without this it would prompt by voice even in auto).
    mode = read_mode()
    if mode == "auto" or (mode == "acceptEdits" and is_edit_tool(tool_name)):
        append_event({"event": "tool", **base})
        emit("allow")
        return

    # default / plan (or acceptEdits for non-edit tools): park until the runner
    # writes a decision (voice yes/no).
    append_event({"event": "needs_permission", **base})
    path = decision_path(tool_use_id)
    deadline = time.time() + POLL_SECONDS
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    dec = json.load(f)
            except Exception:
                dec = {"decision": "deny", "reason": "unreadable decision"}
            try:
                os.remove(path)
            except OSError:
                pass
            emit("allow" if dec.get("decision") == "allow" else "deny", dec.get("reason", ""))
            return
        time.sleep(POLL_INTERVAL)

    emit("deny", "timed out waiting for user decision")


if __name__ == "__main__":
    main()
