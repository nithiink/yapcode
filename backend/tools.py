"""Tools exposed to the realtime voice model (OpenAI Realtime function calls).

Each tool has an OpenAI function definition (sent to the session via
`session.update`) and an async handler. Handlers drive the Claude session
through the ClaudeRunner. `tell_claude` / `answer_prompt` return the runner's
AdvanceResult so the voice model can narrate progress and surface prompts.
"""
from __future__ import annotations

from typing import Any

from session_manager import (
    close_session,
    get_runner,
    handoff_command,
    list_all_sessions,
    list_projects,
    peek_session,
    register_owner,
    resolve_project_path,
    runner_for,
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
                "backend": {
                    "type": "string",
                    "description": "Execution backend (set by the app, not the user): 'cli' (interactive CLI) or 'sdk'.",
                    "enum": ["cli", "sdk"],
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "tell_claude",
        "description": "Send a message/instruction to a Claude session. Returns immediately with status 'working' — Claude runs in the background, which can take minutes. Do NOT wait silently: give a brief spoken acknowledgement ('On it, I'll let you know') and stay available to chat. You will be told automatically when Claude finishes, asks a question, or needs permission — do not call this again to check progress.",
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
        "description": "Answer a pending permission or question prompt from Claude (after you were told Claude needs permission or is asking a question). For permissions pass 'allow' or 'deny'. For questions pass the chosen option text. Returns 'working' immediately; Claude resumes in the background and you'll be told the result automatically.",
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
        "name": "close_session",
        "description": "Permanently end and close a Claude session — kills its terminal/process and frees it. Use when the user says they're done with a session, says 'close it' / 'end the session' / 'shut it down', or wants to clean up. This is different from interrupt_session (which only stops the current task but keeps the session open). The session_id becomes unusable afterward.",
        "parameters": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
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
        "name": "peek_screen",
        "description": "Look at what's currently on the Claude session's terminal screen right now — the live view, including menus, prompts, spinners, and in-progress output. Use this when you're unsure what state the session is in, the user asks 'what's on the screen?' or 'what is it doing?', or a prompt/answer didn't go through as expected. This is a visual snapshot (older output may have scrolled off); for the full conversation use read_session instead.",
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
    if name == "list_projects":
        return list_projects()

    if name == "list_sessions":
        return {"sessions": list_all_sessions()}

    if name == "start_session":
        backend = args.get("backend") or "cli"
        path = resolve_project_path(args.get("project_path", ""))
        runner = get_runner(backend)
        handle = await runner.start(path, args.get("model"))
        register_owner(handle, backend)
        return {"session_id": handle, "project_path": path, "backend": backend,
                "message": f"Started a Claude session in {path}."}

    if name == "tell_claude":
        # Non-blocking: Claude turns can run for minutes. Kick it off in the
        # background and return immediately so the voice model stays responsive;
        # the frontend polls poll_session and narrates the result when ready.
        runner_for(args["session_id"]).start_advance(args["session_id"], args["message"])
        return {"status": "working", "session_id": args["session_id"]}

    if name == "answer_prompt":
        runner_for(args["session_id"]).start_answer(args["session_id"], args["choice"])
        return {"status": "working", "session_id": args["session_id"]}

    if name == "poll_session":
        # App-level poll (not exposed to the voice model — not in TOOL_DEFINITIONS).
        return runner_for(args["session_id"]).poll_status(args["session_id"])

    if name == "read_transcript":
        # App-level: full session timeline from the on-disk jsonl (both backends).
        from transcript import read_timeline
        return read_timeline(args["session_id"])

    if name == "interrupt_session":
        await runner_for(args["session_id"]).interrupt(args["session_id"])
        return {"status": "interrupted", "session_id": args["session_id"]}

    if name == "close_session":
        await close_session(args["session_id"])
        return {"status": "closed", "session_id": args["session_id"]}

    if name == "peek_screen":
        return await peek_session(args["session_id"])

    if name == "read_session":
        text = await runner_for(args["session_id"]).read(args["session_id"])
        return {"session_id": args["session_id"], "text": text}

    if name == "get_handoff":
        sid = args["session_id"]
        sess = next((s for s in list_all_sessions() if s["handle"] == sid), None)
        if not sess:
            raise KeyError(f"unknown session: {sid}")
        cmd = handoff_command(sess["cwd"], sess["session_id"])
        return {"session_id": sid, "cwd": sess["cwd"], "command": cmd}

    raise KeyError(f"unknown tool: {name}")
