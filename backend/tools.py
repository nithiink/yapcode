"""Tools exposed to the realtime voice model (OpenAI Realtime function calls).

Each tool has an OpenAI function definition (sent to the session via
`session.update`) and an async handler. Handlers drive the Claude session
through Yuri's SessionService (`container().sessions`), which owns the domain
side effects — missions, session rows, approvals, events — and forwards the
provider call unchanged in shape. `tell_claude` / `answer_prompt` still return
immediately with status "working" so the voice model stays responsive.

The result dicts here are a CONTRACT with the voice model (and with
frontend/lib/operating.ts): tests/test_tools_dispatch.py snapshots every key.
Changing one is a user-visible regression, not a refactor.

Two things deliberately stay in this module rather than moving into the domain:
the `_last_start` duplicate-start guard (a voice-model quirk, not a rule about
sessions) and `_require_session`'s sole-session fallback plus its soft-error
ValueError texts (recovery instructions written for the model to read).
"""
from __future__ import annotations

import time
from typing import Any

from slash_commands import list_slash_commands
from yuri.app import container
from yuri.domain.mission import InvalidTransition
from yuri.services.missions import MISSION_LIST_MAX, TITLE_SPEECH_MAX, clip_speech

# start_session duplicate guard (see the handler): the most recent session
# creation, so a rapid second call can be redirected to it instead of silently
# spawning a twin. {"ts": monotonic, "handle": str, "name": str} or None.
START_GUARD_SECS = 15.0
_last_start: dict[str, Any] | None = None

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
        "description": "List the Claude Code sessions currently running on this machine, with their human-readable name, project directory, and status. Each session also reports its work pipeline: `running` (a turn is executing right now), `queued` (how many follow-up turns are waiting behind the current one), and `pending` (finished turns not yet narrated). Use these to answer 'is it still working?' or 'what's queued on the billing session?'. Use the names here to refer to sessions in other calls. A session whose status is needs_permission or needs_choice includes the full pending prompt under `prompt` (for plan approvals this contains the entire plan) — use it to tell the user what's being asked, then respond with answer_prompt.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "start_session",
        "description": "Start a new interactive Claude Code session in a project directory. Returns the session's name and id — you can refer to it by either in later calls. The project_path may be a folder name (e.g. 'Development' or a project name) — it's resolved against the allowed roots. If the user is vague about location, omit it to use the default root, or call list_projects first.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "Project directory: an absolute path, a '~' path, or a folder/project name to resolve against allowed roots. Omit or leave empty to use the default project root.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional human-readable name for the session (e.g. 'jarvis', 'billing fix') so it's easy to refer to later. If the user names it, pass that. If omitted, a friendly name is auto-generated from the folder. Must be unique among active sessions.",
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
                "agent": {
                    "type": "string",
                    "description": "Which coding agent runs this session. Omit for the default ('claude-code'). Pass 'opencode' only when the user asks for OpenCode ('use OpenCode', 'have OpenCode do it'). The AGENTS list in your context says which are configured; asking for one that is not returns an error naming the ones that are.",
                },
                "mode": {
                    "type": "string",
                    "description": "Initial permission mode. 'default' (asks before risky actions — recommended), 'plan' (only plans, makes no changes), 'acceptEdits' (auto-applies file edits), or 'auto' (runs everything without asking).",
                    "enum": ["default", "plan", "acceptEdits", "auto"],
                },
                "another": {
                    "type": "boolean",
                    "description": "Set true ONLY when the user explicitly wants an additional session right after one was just created (e.g. 'start two sessions'). Without it, a second start_session within a few seconds is rejected as an accidental duplicate — if the user merely added a name or detail after the session was created, use rename_session/set_mode on the existing session instead.",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "rename_session",
        "description": "Give a Claude session a new human-readable name (or rename one) so it's easy to identify and refer to. Use when the user says things like 'call this one jarvis', 'rename the billing session', or 'name it X'. The name must be unique among active sessions.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session to rename — its current name or id."},
                "name": {"type": "string", "description": "The new human-readable name."},
            },
            "required": ["session_id", "name"],
        },
    },
    {
        "type": "function",
        "name": "list_slash_commands",
        "description": "List the slash commands available in a Claude session — built-ins (/init, /review, /clear, /model, /compact, …), user skills and plugin skills (e.g. /kb-query, /search-chat-history, /frontend-design), and any project-local commands. Call this when the user asks 'what commands are available?' or you need to find the right command for a request. If you pass session_id, the result also includes that session's project-local commands.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Optional: a session (name or id) to also include its project's local .claude/commands/."},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "run_slash_command",
        "description": "Invoke a Claude Code slash command in a session — a skill or built-in like /init, /review, /security-review, /verify, /compact, /clear, or any user/plugin/project command. Use when the user asks for something that maps cleanly to a command, e.g. 'initialize this project' (/init), 'review the diff' (/review), 'do a security review' (/security-review), 'compact the context' (/compact), 'call /kb-query about X'. For freeform engineering work prefer tell_claude. Use list_slash_commands first if unsure what's available. Returns immediately with status 'working'; you'll be told the result automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session (name or id) to run the command in."},
                "command": {"type": "string", "description": "Command name, with or without the leading slash (e.g. 'init' or '/init'). Use the FULL name — prefixes can be ambiguous."},
                "args": {"type": "string", "description": "Optional space-separated arguments that follow the command (e.g. a PR number for /review)."},
            },
            "required": ["session_id", "command"],
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
        "description": "Answer a pending permission or question prompt from Claude (after you were told Claude needs permission or is asking a question). For permissions pass 'allow' or 'deny'. For questions pass the chosen option text. Call it at most ONCE per prompt — it fails if the prompt was already answered or resolved by a mode switch (that's fine, don't retry). If the user wants to allow AND switch to auto/acceptEdits, call only set_mode — it approves the covered pending prompt itself. Returns 'working' immediately; Claude resumes in the background and you'll be told the result automatically.",
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
        "name": "set_mode",
        "description": "Change a Claude session's permission mode when the user asks (e.g. 'switch to plan mode', 'turn on auto', 'accept edits', 'go back to normal'). Modes: 'default' (Claude asks before risky actions and you relay allow/deny by voice), 'plan' (Claude only plans, makes NO edits or commands), 'acceptEdits' (file edits auto-apply, other risky actions still asked), 'auto' (Claude runs everything without asking — no voice approval). Returns the mode now in effect. If a permission prompt is pending and the new mode would auto-approve that tool (auto: anything; acceptEdits: file edits), the prompt is approved automatically and the session continues — the result message says which happened, so relay it. So when the user asks to allow a prompt AND switch modes, call ONLY set_mode (no answer_prompt); only answer_prompt separately if the result says the prompt is still pending.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["default", "plan", "acceptEdits", "auto"]},
            },
            "required": ["session_id", "mode"],
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
        "description": "Get the terminal command(s) to take a Claude session over by keyboard. Returns two options: attach_command (`tmux attach …`) to co-drive the SAME live session — the user types while you keep talking, both at once — and resume_command (`claude --resume …`) to take it over solo in a separate terminal. Offer attach_command when the user wants to type alongside voice; resume_command when they want to leave voice and continue by keyboard only.",
        "parameters": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "type": "function",
        "name": "send_keys",
        "description": (
            "ESCAPE HATCH — send raw keystrokes straight to a Claude session's terminal. "
            "Use only when the dedicated tools (tell_claude, answer_prompt, "
            "interrupt_session, set_mode) can't control the session: it's stuck on an "
            "unexpected prompt or menu, a tool didn't work, or Claude Code's interface "
            "changed and there's no dedicated tool for what's on screen. Pass 'items', an "
            "ordered list where each entry is either {\"key\": \"<name>\"} for a named "
            "key/movement (Escape, Enter, Up, Down, Left, Right, Tab, BTab, Space, C-c, "
            "C-d, ...) or {\"text\": \"<literal text>\"} to type text. They're sent in "
            "order. Examples: [{\"key\":\"Escape\"}] to back out; [{\"key\":\"C-c\"}] to "
            "cancel; [{\"key\":\"Down\"},{\"key\":\"Down\"},{\"key\":\"Enter\"}] to pick a "
            "menu item; [{\"text\":\"yes\"},{\"key\":\"Enter\"}] to type an answer and "
            "submit. Returns a snapshot of the screen so you can see what happened. "
            "CLI sessions only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session to send keys to."},
                "items": {
                    "type": "array",
                    "description": "Ordered list of keys/text to send.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "A tmux key name or chord, e.g. Escape, Enter, Up, Down, Left, Right, Tab, BTab, Space, C-c."},
                            "text": {"type": "string", "description": "Literal text to type at the cursor."},
                        },
                    },
                },
            },
            "required": ["session_id", "items"],
        },
    },
    {
        "type": "function",
        "name": "mute",
        "description": (
            "Mute your own microphone in the user interface so you stop listening to "
            "the user. Call this ONLY when the user asks you to stop LISTENING — "
            "'mute', 'mute yourself', 'stop listening'. A request to TALK less or "
            "narrate less belongs to set_narration, never here: muting cannot be "
            "undone by voice. While muted you can't hear the user, so you can't "
            "unmute by voice — the user unmutes with the on-screen button. Give a "
            "brief spoken acknowledgement before or as you mute."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "remember",
        "description": "Store a durable fact in Yuri's memory (~/Yuri/memory). Use it when the user states a preference, corrects you, or says 'remember this'. Pass project to file it under that project's notes instead of the user's.",
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "One sentence, in the user's terms."},
                "project": {"type": "string", "description": "Optional project folder name the fact is about."},
            },
            "required": ["fact"],
        },
    },
    {
        "type": "function",
        "name": "set_narration",
        "description": "Change how much you narrate. 'quiet' = only problems and things needing the user's answer; 'normal' = meaningful progress; 'verbose' = every tool and cost update too. Call this when the user says 'be quiet', 'stop narrating', 'less', 'tell me everything', or 'go back to normal'. 'Be quiet' means talk less, NOT stop listening — never call mute for it. The setting is remembered.",
        "parameters": {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["quiet", "normal", "verbose"]}},
            "required": ["mode"],
        },
    },
    {
        "type": "function",
        "name": "list_missions",
        "description": "List Yuri's missions — the units of work. Call this when the user asks what's running, what you're working on, or what happened. Omit status for the active ones; pass a status to filter (running, waiting_for_approval, paused, completed, failed, cancelled). Only the most recently updated missions are returned.",
        "parameters": {
            "type": "object",
            "properties": {"status": {"type": "string", "description": "Optional status filter."}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "mission_status",
        "description": "Details of one mission: its goal, status, which agents are on it, its sessions and any pending approval. Omit mission to mean the one active mission.",
        "parameters": {
            "type": "object",
            "properties": {"mission": {"type": "string", "description": "Mission title, id, or a phrase from its title. Omit for the current one."}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "pause_mission",
        "description": "Pause a mission, interrupting any agent currently working on it. Use this for 'pause that', 'hold on', or 'stop the payment one'. Omit mission to mean the one active mission. Resume it later with resume_mission.",
        "parameters": {
            "type": "object",
            "properties": {"mission": {"type": "string", "description": "Mission title, id, or a phrase from its title. Omit for the current one."}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "resume_mission",
        "description": "Resume a paused mission. Omit mission to mean the one active mission. Resuming does not itself give the agent new instructions — use tell_claude for that.",
        "parameters": {
            "type": "object",
            "properties": {"mission": {"type": "string", "description": "Mission title, id, or a phrase from its title. Omit for the current one."}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "cancel_mission",
        "description": "Cancel a mission and stop its agents. This ends the work — confirm with the user first, naming the mission you are about to cancel. Omit mission to mean the one active mission.",
        "parameters": {
            "type": "object",
            "properties": {"mission": {"type": "string", "description": "Mission title, id, or a phrase from its title. Omit for the current one."}},
            "required": [],
        },
    },
]


def _svc():
    """The live SessionService. Fetched per call, never cached at import time:
    the container is built during app startup (and rebuilt per test), so a
    module-level binding would pin a dead one."""
    return container().sessions


def _require_session(args: dict[str, Any], action: str) -> str:
    """Resolve the session a tool should act on.

    The voice model sometimes omits session_id ('close the session', 'read it')
    — especially when only one session is open. Rather than letting
    `args["session_id"]` raise a bare KeyError (which the endpoint maps to a
    confusing HTTP 404), this:

      * falls back to the sole active session when session_id is omitted and
        exactly one is open — the unambiguous, expected case;
      * otherwise raises ValueError, a SOFT error the model can recover from
        (the endpoint returns {ok: false, error} and the model can ask which
        session / start one), listing the open sessions by name.

    A non-empty but unknown session_id also becomes a ValueError here instead of
    a 404, for the same recoverable behavior."""
    ref = (args.get("session_id") or "").strip()
    svc = _svc()
    if not ref:
        sessions = svc.list()
        if len(sessions) == 1:
            return sessions[0]["handle"]
        if not sessions:
            raise ValueError(f"there are no active sessions to {action}. Start one first.")
        names = ", ".join(s.get("name") or s["handle"][:8] for s in sessions)
        raise ValueError(
            f"which session should I {action}? {len(sessions)} are open: {names}. "
            "Ask the user which one, then pass its session_id.")
    try:
        return svc.resolve(ref)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc


async def dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "list_projects":
        return container().projects.list()

    if name == "list_sessions":
        return {"sessions": _svc().list()}

    if name == "start_session":
        # Duplicate guard: in voice flows the model sometimes re-calls
        # start_session when the user's answer to "what should we name it?"
        # lands while the first call is still executing — creating two sessions
        # (then denying it, since the first result never got narrated). A second
        # start within the window needs an explicit another=true; otherwise we
        # point the model at the session it just created.
        global _last_start
        now = time.monotonic()
        recent = _last_start
        if recent and now - recent["ts"] < START_GUARD_SECS and not args.get("another"):
            return {
                "duplicate_guard": True,
                "existing_session": {"session_id": recent["handle"], "name": recent["name"]},
                "message": (
                    f"NOT creating another session: '{recent['name']}' was created "
                    f"{int(now - recent['ts'])}s ago and is probably the session the user means. "
                    "If the user was adding a name or detail for it, apply that with "
                    "rename_session/set_mode (or just use the session). Only if the user "
                    "explicitly wants a SECOND separate session, call start_session again "
                    "with another=true."
                ),
            }
        backend = args.get("backend") or "cli"
        mode = args.get("mode") or "default"
        # Record before the (slow) start so a concurrent duplicate call also trips
        # the guard rather than racing past it.
        _last_start = {"ts": now, "handle": "", "name": "(starting…)"}
        # Which agent runs it. Omitted -> AgentRouter's default (claude-code),
        # which is what every unqualified request has always got.
        requested_agent = (args.get("agent") or "").strip() or None
        try:
            out = await _svc().start(args.get("project_path", ""), backend=backend, mode=mode,
                                     model=args.get("model"), name=args.get("name"),
                                     created_by="voice", agent_id=requested_agent)
        except KeyError as exc:
            # An agent id that is not registered. Soft (like _require_session
            # above), not the bare KeyError the endpoint would turn into a
            # confusing 404: the message names the agents that ARE up, which is
            # exactly what the voice model needs to offer one instead of
            # silently switching. Narrowed to the case where an agent was
            # actually requested, so no other KeyError changes shape.
            _last_start = recent
            if requested_agent:
                raise ValueError(str(exc)) from exc
            raise
        except BaseException:
            _last_start = recent  # failed start shouldn't block a retry
            raise
        _last_start = {"ts": time.monotonic(), "handle": out["session_id"], "name": out["name"]}
        return out

    if name == "rename_session":
        return _svc().rename(_require_session(args, "rename"), args["name"])

    if name == "set_mode":
        # SessionService snapshots the pending permission BEFORE the switch (the
        # runner resolves a covered prompt asynchronously) and resolves the
        # matching Approval row; `prompt_resolved` in the result is what the
        # frontend uses to dismiss the stale prompt card.
        return await _svc().set_mode(_require_session(args, "change the mode for"), args["mode"])

    if name == "list_slash_commands":
        cwd: str | None = None
        sid_arg = args.get("session_id")
        if sid_arg:
            try:
                sid = _svc().resolve(sid_arg)
                sess = next((s for s in _svc().list() if s["handle"] == sid), None)
                if sess:
                    cwd = sess["cwd"]
            except KeyError:
                pass
        return {"commands": list_slash_commands(cwd)}

    if name == "run_slash_command":
        sid = _require_session(args, "run that command in")
        cmd = str(args.get("command", "")).strip().lstrip("/")
        if not cmd:
            raise ValueError("command is required (e.g. 'init' or '/init')")
        extra = (args.get("args") or "").strip()
        text = f"/{cmd}" + (f" {extra}" if extra else "")
        try:
            # The provider races the Stop hook (for skills / commands that drive
            # a real Claude turn) against screen-settle detection (for UI-only
            # built-ins that never fire Stop), so we don't maintain a list of
            # which built-ins fire Stop. A backend with no TUI says so instead.
            return _svc().run_slash(sid, text)
        except NotImplementedError:
            return {"ok": False, "error": "slash commands run in the interactive CLI; this session uses the SDK backend."}

    if name == "tell_claude":
        # Non-blocking: Claude turns can run for minutes. Kick it off in the
        # background and return immediately so the voice model stays responsive;
        # the frontend polls poll_session and narrates the result when ready.
        return _svc().send(_require_session(args, "send that to"), args["message"])

    if name == "answer_prompt":
        return _svc().answer(_require_session(args, "answer for"), args["choice"])

    if name == "poll_session":
        # App-level poll (not exposed to the voice model — not in TOOL_DEFINITIONS).
        # An unknown session_id raises KeyError (svc.poll resolves internally),
        # which /tools/execute maps to a 404 — unchanged from the shim path.
        return _svc().poll(args["session_id"])

    if name == "read_transcript":
        # Provider-owned: Claude Code reads its on-disk jsonl, OpenCode reads
        # its server. Calling read_timeline here meant every OpenCode
        # transcript came back empty.
        return await _svc().transcript(args["session_id"])

    if name == "interrupt_session":
        return await _svc().interrupt(_require_session(args, "interrupt"))

    if name == "close_session":
        return await _svc().stop(_require_session(args, "close"))

    if name == "peek_screen":
        return await _svc().peek(_require_session(args, "peek at"))

    if name == "read_session":
        return await _svc().read(_require_session(args, "read"))

    if name == "get_handoff":
        # Two commands, deliberately: attach_command joins the SAME running
        # process (keyboard + voice at once, CLI/tmux only), resume_command
        # takes it over solo in a separate process. `command` is a
        # backward-compat alias for resume_command.
        return _svc().handoff_info(_require_session(args, "hand off"))

    if name == "send_keys":
        sid = _require_session(args, "send keys to")
        items = args.get("items") or []
        if not isinstance(items, list) or not items:
            raise ValueError("items is required (a non-empty list of {key} or {text} objects)")
        try:
            return await _svc().send_keys(sid, items)
        except NotImplementedError:
            return {"ok": False, "error": "send_keys controls the interactive CLI; this session uses the SDK backend."}

    if name == "mute":
        # Muting is a client-side action (it disables the mic track in the
        # browser). The backend just acknowledges; the frontend reacts to the
        # tool_call event and flips the actual mute state.
        return {"muted": True, "message": "Microphone muted. The user can unmute with the on-screen button."}

    if name == "remember":
        c = container()
        slug = None
        project = (args.get("project") or "").strip()
        if project:
            slug = c.projects.resolve_or_create(project).slug     # ValueError → soft error
        path = c.memory.remember(args.get("fact", ""), project_slug=slug)
        from yuri.domain.event import EventType, YuriEvent
        c.bus.publish(YuriEvent.make(EventType.MEMORY_REMEMBERED, payload={"fact": args.get("fact", ""),
                                                                            "project": slug}))
        c.journal.append(f"remembered{' for ' + slug if slug else ''}: {args.get('fact', '')}")
        return {"ok": True, "path": path,
                "message": "Remembered." if not slug else f"Noted under {slug}."}

    if name == "set_narration":
        from yuri.app import set_narration_mode
        mode = set_narration_mode(args.get("mode"))
        blurb = {"quiet": "Going quiet — I'll only speak up for problems and anything needing your answer.",
                 "normal": "Back to normal narration.",
                 "verbose": "I'll narrate everything, including each tool call."}[mode]
        return {"mode": mode, "message": blurb}

    if name == "list_missions":
        # Shaping (and the store access it needs) belongs to MissionService,
        # next to speech_detail's identical clipping rules.
        status = (args.get("status") or "").strip() or None
        return {"missions": container().missions.speech_list(status, limit=MISSION_LIST_MAX)}

    if name == "mission_status":
        c = container()
        # resolve() raises ValueError on an unknown or ambiguous reference —
        # a soft error the model reads back to the user (see main.py).
        return c.missions.speech_detail(c.missions.resolve(args.get("mission", "")).id)

    if name in ("pause_mission", "resume_mission", "cancel_mission"):
        c = container()
        m = c.missions.resolve(args.get("mission", ""))
        verb = name.split("_")[0]
        try:
            m = await getattr(c.missions, verb)(m.id, by="voice")
        except InvalidTransition as exc:
            raise ValueError(str(exc)) from exc     # soft error the model can recover from
        title = clip_speech(m.title, TITLE_SPEECH_MAX)
        return {"mission_id": m.id, "title": title, "status": m.status,
                "message": f'Mission "{title}" is now {m.status}.'}

    raise KeyError(f"unknown tool: {name}")
