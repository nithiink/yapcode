"""Parse a Claude session's on-disk .jsonl into a display timeline.

Both backends (SDK and interactive CLI) write the same transcript at
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, so this one reader serves
both. We surface user prompts, assistant text, tool calls (with a short summary
and whether they were permission-gated), and tool results. Internal "thinking"
blocks are omitted.
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any

from claude_runner import _summarize_tool
from permissions import classify


def _find(handle: str) -> str | None:
    matches = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{handle}.jsonl"))
    return matches[0] if matches else None


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)
    return ""


def read_timeline(handle: str, limit: int = 300) -> dict[str, Any]:
    """Return {found, events:[...]} where each event is one of:
    {kind:'user', text} | {kind:'assistant', text}
    | {kind:'tool', name, summary, risky} | {kind:'tool_result', ok, text}"""
    # `handle` is interpolated into the transcript glob in _find, so require it to be
    # a safe single path component first (CodeQL py/path-injection). Callers already
    # pass a resolve_session'd handle; an invalid one simply has no transcript.
    from tmux_runner import validate_session_id
    try:
        handle = validate_session_id(handle)
    except ValueError:
        return {"found": False, "events": []}
    path = _find(handle)
    if not path or not os.path.exists(path):
        return {"found": False, "events": []}

    events: list[dict[str, Any]] = []
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            msg = o.get("message", {}) or {}
            if t == "user":
                c = msg.get("content")
                if isinstance(c, str):
                    if c.strip():
                        events.append({"kind": "user", "text": c})
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            txt = _result_text(b.get("content"))
                            events.append({
                                "kind": "tool_result",
                                "ok": not b.get("is_error", False),
                                "text": txt[:600],
                            })
            elif t == "assistant":
                for b in msg.get("content", []) or []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and b.get("text", "").strip():
                        events.append({"kind": "assistant", "text": b["text"]})
                    elif bt == "tool_use":
                        name = b.get("name", "")
                        events.append({
                            "kind": "tool",
                            "name": name,
                            "summary": _summarize_tool(name, b.get("input", {}) or {}),
                            "risky": classify(name) == "risky",
                        })

    return {"found": True, "events": events[-limit:]}
