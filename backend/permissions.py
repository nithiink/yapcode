"""Permission policy: which tools auto-approve vs. need a spoken yes/no.

Evaluated inside the Agent SDK's `can_use_tool` callback. Risky tools are NOT
placed in `allowed_tools` — doing so pre-approves them and bypasses the callback
entirely (SDK permission order runs Allow Rules before the callback).
"""
from __future__ import annotations

# Read-only / non-destructive tools: auto-approved silently.
SAFE_TOOLS: frozenset[str] = frozenset({
    "Read", "Grep", "Glob", "LS",
    "TodoWrite", "NotebookRead",
    "WebSearch", "WebFetch",
})

# Always surface to the user as a choice (not a yes/no permission).
QUESTION_TOOL = "AskUserQuestion"

# File-editing tools — auto-approved in the "acceptEdits" permission mode.
EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


def is_edit_tool(tool_name: str) -> bool:
    return tool_name in EDIT_TOOLS

# MCP tool prefixes that are auto-approved (treated as safe). Claude-in-Chrome
# browser control is confined to a browser, so navigate/click/fill run without a
# spoken prompt each time (per product decision). Add more prefixes as needed.
SAFE_MCP_PREFIXES: tuple[str, ...] = ("mcp__claude-in-chrome__",)


def classify(tool_name: str) -> str:
    """Return one of: 'safe', 'question', 'risky'."""
    if tool_name == QUESTION_TOOL:
        return "question"
    if tool_name in SAFE_TOOLS:
        return "safe"
    if any(tool_name.startswith(p) for p in SAFE_MCP_PREFIXES):
        return "safe"
    return "risky"
