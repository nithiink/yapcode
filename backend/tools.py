"""Tools exposed to the realtime voice model (OpenAI Realtime function calls).

Each tool has an OpenAI function definition (sent to the session via
`session.update`) and an async handler. Handlers drive the Claude session
through the ClaudeRunner. `tell_claude` / `answer_prompt` return the runner's
AdvanceResult so the voice model can narrate progress and surface prompts.
"""
from __future__ import annotations

from typing import Any

from session_manager import (
    get_runner,
    handoff_command,
    list_projects,
    resolve_project_path,
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "list_projects",
        "description": "List the project directories available to work in (allowed roots and their subfolders). Call this when the user names a folder vaguely or you don't know the absolute path — then pick or confirm one of these instead of asking the user for a full path.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "list_sessions",
        "description": "List the Claude Code sessions currently running on this machine, with their project directory and status.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "start_session",
        "description": "Start a new interactive Claude Code session in a project directory. Returns a session_id used for all later calls. The project_path may be a folder name (e.g. 'Development' or a project name) — it's resolved against the allowed roots. If the user is vague about location, omit it to use the default root, or call list_projects first.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "Project directory: an absolute path, a '~' path, or a folder/project name to resolve against allowed roots. Omit or leave empty to use the default project root.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional Claude model: 'opus' (default, most capable) or 'sonnet' (cheaper).",
                    "enum": ["opus", "sonnet"],
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "tell_claude",
        "description": "Send a message/instruction to a Claude session and run until Claude finishes OR needs a decision. Returns status: 'completed' (with Claude's reply), 'needs_permission' (Claude wants to use a risky tool — read the prompt aloud and call answer_prompt), 'needs_choice' (Claude is asking a question), or 'error'.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session to send to."},
                "message": {"type": "string", "description": "What to tell Claude."},
            },
            "required": ["session_id", "message"],
        },
    },
    {
        "type": "function",
        "name": "answer_prompt",
        "description": "Answer a pending permission or question prompt from Claude (after tell_claude returned needs_permission or needs_choice). For permissions pass 'allow' or 'deny'. For questions pass the chosen option text. Claude then continues until its next stop or completion.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session with a pending prompt."},
                "choice": {
                    "type": "string",
                    "description": "'allow'/'deny' for a permission, or the selected option text for a question.",
                },
            },
            "required": ["session_id", "choice"],
        },
    },
    {
        "type": "function",
        "name": "interrupt_session",
        "description": "Stop Claude mid-task in a session (like pressing Escape). Use when the user says 'stop' or 'cancel'.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    },
    {
        "type": "function",
        "name": "read_session",
        "description": "Re-read the latest accumulated text from a Claude session (e.g. if the user asks 'what did it say again?').",
        "parameters": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "type": "function",
        "name": "get_handoff",
        "description": "Get the exact terminal command to take over a Claude session by keyboard (cd into its project and resume it). Speak it or note it when the user wants to continue in their terminal.",
        "parameters": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
]


async def dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    runner = get_runner()

    if name == "list_projects":
        return list_projects()

    if name == "list_sessions":
        return {"sessions": runner.list()}

    if name == "start_session":
        path = resolve_project_path(args.get("project_path", ""))
        handle = await runner.start(path, args.get("model"))
        return {"session_id": handle, "project_path": path,
                "message": f"Started a Claude session in {path}."}

    if name == "tell_claude":
        res = await runner.advance(args["session_id"], args["message"])
        return res.to_dict()

    if name == "answer_prompt":
        res = await runner.answer(args["session_id"], args["choice"])
        return res.to_dict()

    if name == "interrupt_session":
        await runner.interrupt(args["session_id"])
        return {"status": "interrupted", "session_id": args["session_id"]}

    if name == "read_session":
        text = await runner.read(args["session_id"])
        return {"session_id": args["session_id"], "text": text}

    if name == "get_handoff":
        sid = args["session_id"]
        sess = next((s for s in runner.list() if s["handle"] == sid), None)
        if not sess:
            raise KeyError(f"unknown session: {sid}")
        cmd = handoff_command(sess["cwd"], sess["session_id"])
        return {"session_id": sid, "cwd": sess["cwd"], "command": cmd}

    raise KeyError(f"unknown tool: {name}")
