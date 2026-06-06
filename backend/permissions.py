"""Permission policy: which tools auto-approve vs. need a spoken yes/no.

Evaluated inside the Agent SDK's `can_use_tool` callback. Risky tools are NOT
placed in `allowed_tools` — doing so pre-approves them and bypasses the callback
entirely (SDK permission order runs Allow Rules before the callback).
"""
from __future__ import annotations

import os

# Read-only / non-destructive tools: auto-approved silently.
SAFE_TOOLS: frozenset[str] = frozenset({
    "Read", "Grep", "Glob", "LS",
    "TodoWrite", "NotebookRead",
    "WebSearch", "WebFetch",
    # Loads tool schemas into context — pure metadata, runs nothing. The vanilla
    # CLI never prompts for it; parking it by voice was pointless friction.
    "ToolSearch",
})

# Always surface to the user as a choice (not a yes/no permission).
QUESTION_TOOL = "AskUserQuestion"

# File-editing tools — auto-approved in the "acceptEdits" permission mode.
EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


def is_edit_tool(tool_name: str) -> bool:
    return tool_name in EDIT_TOOLS


# Plan mode saves its plan markdown to ~/.claude/plans before presenting it.
# The vanilla CLI does this silently (the plan file is session state, not
# project files); asking the user "may I write create-a-random-plan-zippy-
# moth.md?" by voice is confusing noise. Resolved once at import.
_PLANS_DIR = os.path.realpath(os.path.expanduser("~/.claude/plans"))


def is_plan_file_write(tool_name: str, tool_input: dict | None) -> bool:
    """True when an edit tool is writing inside ~/.claude/plans."""
    if tool_name not in EDIT_TOOLS:
        return False
    fp = str((tool_input or {}).get("file_path")
             or (tool_input or {}).get("notebook_path") or "")
    if not fp:
        return False
    real = os.path.realpath(os.path.expanduser(fp))
    return real.startswith(_PLANS_DIR + os.sep)

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
