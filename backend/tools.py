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

import secrets
import time
from typing import Any

from slash_commands import list_slash_commands
from yuri.app import container, stamp_last_spoke
from yuri.domain.mission import InvalidTransition
from yuri.services.missions import MISSION_LIST_MAX, TITLE_SPEECH_MAX, clip_speech

# start_session duplicate guard (see the handler): the most recent session
# creation, so a rapid second call can be redirected to it instead of silently
# spawning a twin. {"ts": monotonic, "handle": str, "name": str} or None.
START_GUARD_SECS = 15.0
_last_start: dict[str, Any] | None = None

# cancel_mission's confirmation gate. Cancelling ends work and stops running
# agents, and the only thing that stood between a misheard phrase and that
# happening was a sentence in the tool's description telling the model to
# confirm first. A prompt instruction is not a guard — this is the same
# reasoning that kept mission DELETE off the voice surface entirely
# (MissionService.delete): a speech recogniser must not fire a destructive
# action on one utterance.
#
# So the FIRST call never cancels. It arms, server-side, and hands back a
# token; only a second call carrying that exact token, for that same mission,
# inside the window, goes through. A model that calls twice blindly does not
# have the token, and one that invents a token is refused — the arm lives here,
# not in anything the model can assert.
CANCEL_CONFIRM_SECS = 120.0
_pending_cancel: dict[str, Any] | None = None


def _arm_cancel(mission_id: str) -> str:
    global _pending_cancel
    token = secrets.token_hex(3)
    _pending_cancel = {"ts": time.monotonic(), "mission_id": mission_id, "token": token}
    return token


def _cancel_is_confirmed(mission_id: str, token: str | None) -> bool:
    """True iff `token` is the live arm for exactly this mission. Single use:
    consumed whether or not it matched, so a wrong guess cannot be retried
    against a still-valid arm."""
    global _pending_cancel
    pending, _pending_cancel = _pending_cancel, None
    if not pending or not token:
        return False
    if time.monotonic() - pending["ts"] > CANCEL_CONFIRM_SECS:
        return False
    return pending["mission_id"] == mission_id and secrets.compare_digest(
        str(pending["token"]), str(token))

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
        "description": "List the Claude Code sessions currently running on this machine, with their human-readable name, project directory, and status. Each session also reports its work pipeline: `running` (a turn is executing right now), `queued` (how many follow-up turns are waiting behind the current one), and `pending` (finished turns not yet narrated). Use these to answer 'is it still working?' or 'what's queued on the billing session?'. Use the names here to refer to sessions in other calls. A session whose status is needs_permission or needs_choice includes the full pending prompt under `prompt` (for plan approvals this contains the entire plan) — use it to tell the user what's being asked, then respond with answer_prompt. Sessions open when you connected are listed in your context, but that list goes stale — call this for live status or when time has passed. IN QUIET MODE there is no update when a session merely finishes (only problems, permissions and questions arrive), so do not say \"I'll let you know\" and then wait for something that will not come: acknowledge, then call this or read_session when you or the user want to know where the work got to.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "start_session",
        "description": "Start a new interactive Claude Code session in a project directory. Returns the session's name and id — you can refer to it by either in later calls. The project_path may be a folder name (e.g. 'Development' or a project name) — it's resolved against the allowed roots. If the user is vague about location, omit it to use the default root, or call list_projects first. CALL THIS ONCE per session the user asked for. If you asked them for details (project, name) and the answer arrives after you already called this, do NOT call it again — apply the answer to the session you just made (rename_session for a name, set_mode for a mode). Only call it again when the user explicitly wants an additional separate session, and pass another=true if that is within a few seconds of the last one. If a session is already running for what they want, reuse it with tell_claude instead. NAME IT: every session has a short human-readable name (\"jarvis\", \"billing fix\"); if the user names the work, pass a fitting name, and always refer to sessions by name rather than by id. PROJECT PATH: never repeatedly ask for an absolute path. Pass a plain folder name (\"Development\", a project name) and it is resolved against the allowed roots; if the user is vague (\"anywhere\", \"my dev folder\") omit project_path to use the default root, or call list_projects and pick the likeliest. Only if resolution fails, read back the names from list_projects and ask them to choose. AGENTS: more than one coding agent may be available; the AGENTS list in your context says which were online at connect and there is no tool to re-check, so answer from it and say it was accurate then. Claude Code is the default. \"Use OpenCode\" means passing agent=\"opencode\". Never silently switch agents: if the one they asked for was offline, say so and offer the one that is up.",
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
        "description": "Give a Claude session a new human-readable name (or rename one) so it's easy to identify and refer to. Use when the user says things like 'call this one jarvis', 'rename the billing session', or 'name it X'. The name must be unique among active sessions. You can pass a name anywhere a session_id is expected, so a renamed session stays addressable by its new name.",
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
        "description": "Invoke a Claude Code slash command in a session — a skill or built-in like /init, /review, /security-review, /verify, /compact, /clear, or any user/plugin/project command. Use when the user asks for something that maps cleanly to a command, e.g. 'initialize this project' (/init), 'review the diff' (/review), 'do a security review' (/security-review), 'compact the context' (/compact), 'call /kb-query about X'. For freeform engineering work prefer tell_claude. Use list_slash_commands first if unsure what's available. Returns immediately with status 'working'; you'll be told the result automatically. Prefer this over freeform tell_claude when the request maps cleanly to a command — \"initialize this project\" (init), \"review the diff\" (review), \"do a security review\", \"compact the context\", \"verify this change works\", or a user/plugin command like /kb-query. Pass the command WITHOUT the leading slash. If you are unsure what exists, call list_slash_commands first.",
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
        "description": "Send a message/instruction to a Claude session. Returns immediately with status 'working' — Claude runs in the background, which can take minutes. Do NOT wait silently: give a brief spoken acknowledgement ('On it, I'll let you know') and stay available to chat. You will be told automatically when Claude finishes, asks a question, or needs permission — do not call this again to check progress. Reuse an existing session with this rather than starting a second one for the same work.",
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
        "description": "Answer a pending permission or question prompt from Claude (after you were told Claude needs permission or is asking a question). For permissions pass 'allow' or 'deny'. For questions pass the chosen option text. Call it at most ONCE per prompt — it fails if the prompt was already answered or resolved by a mode switch (that's fine, don't retry). If the user wants to allow AND switch to auto/acceptEdits, call only set_mode — it approves the covered pending prompt itself. Returns 'working' immediately; Claude resumes in the background and you'll be told the result automatically. Before calling for a permission request, TELL the user what the agent wants (\"Claude wants to run rm hello.txt — approve?\") and wait for their answer. Never answer on their behalf.",
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
        "description": "Stop Claude mid-task in a session (like pressing Escape). Use when the user says 'stop' or 'cancel'. If they are done with the session entirely (\"close it\", \"end that session\"), use close_session instead.",
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
        "description": "Change a Claude session's permission mode when the user asks (e.g. 'switch to plan mode', 'turn on auto', 'accept edits', 'go back to normal'). Modes: 'default' (Claude asks before risky actions and you relay allow/deny by voice), 'plan' (Claude only plans, makes NO edits or commands), 'acceptEdits' (file edits auto-apply, other risky actions still asked), 'auto' (Claude runs everything without asking — no voice approval). Returns the mode now in effect. If a permission prompt is pending and the new mode would auto-approve that tool (auto: anything; acceptEdits: file edits), the prompt is approved automatically and the session continues — the result message says which happened, so relay it. So when the user asks to allow a prompt AND switch modes, call ONLY set_mode (no answer_prompt); only answer_prompt separately if the result says the prompt is still pending. If a permission prompt is pending AND they want to allow it and switch to auto/acceptEdits (\"allow that and switch to auto\"), call ONLY this — it auto-approves the covered prompt; do NOT also call answer_prompt, unless this tool's result says the prompt is still pending. In auto/acceptEdits you get fewer permission prompts by design; that is expected, not a fault. If you are unsure what is on screen, call peek_screen.",
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
        "description": "Look at what's currently on the Claude session's terminal screen right now — the live view, including menus, prompts, spinners, and in-progress output. Use this when you're unsure what state the session is in, the user asks 'what's on the screen?' or 'what is it doing?', or a prompt/answer didn't go through as expected. This is a visual snapshot (older output may have scrolled off); for the full conversation use read_session instead. A session may be driven by the user typing in their own terminal at the same time as you — you are not necessarily the only input. If a reply seems out of sync, or you are unsure what just happened, call this or read_session to see the current state rather than talking over them mid-action.",
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
        
        " If the snapshot is not enough to tell what happened, follow with peek_screen or read_session."),
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
        
        " A request to TALK or narrate less is NOT this — that is set_narration. Muting cannot be undone by voice (you will not hear them), so the user unmutes with the on-screen button: never promise to unmute yourself, and never reach for this when they only asked you to say less."),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "remember",
        "description": "Store a durable fact in Yuri's memory (~/Yuri/memory). Use it when the user states a preference, corrects you, or says 'remember this'. Pass project to file it under that project's notes instead of the user's. One sentence; add project when it is about a specific project. Do not ask permission to remember ordinary preferences — do it and say so briefly (\"Noted.\").",
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
        "description": "Change how much you narrate. 'quiet' = only problems and things needing the user's answer; 'normal' = meaningful progress; 'verbose' = every tool and cost update too. Call this when the user says 'be quiet', 'stop narrating', 'less', 'tell me everything', or 'go back to normal'. 'Be quiet' means talk less, NOT stop listening — never call mute for it. The setting is remembered. \"Be quiet\", \"stop narrating\", \"less\" → quiet. \"Tell me everything\", \"more detail\" → verbose. \"Normal\" → normal. It is remembered between conversations, so if it is already quiet do not apologise for being quiet — that is what they asked for.",
        "parameters": {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["quiet", "normal", "verbose"]}},
            "required": ["mode"],
        },
    },
    {
        "type": "function",
        "name": "list_missions",
        "description": "List Yuri's missions — the units of work. Call this when the user asks what's running, what you're working on, or what happened. Omit status for the active ones; pass a status to filter (running, waiting_for_approval, paused, completed, failed, cancelled). Only the most recently updated missions are returned. Prefer this over list_sessions: a mission is the unit of work and a session is one agent inside it, and missions are what the user means. Refer to them by title.",
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
        "description": ("Cancel a mission and stop its agents. This ends the work. It takes TWO "
                        "calls: call it once WITHOUT confirm to find out exactly what would be "
                        "cancelled, read that back to the user and wait for them to agree, then "
                        "call it again passing the confirm token from the first call. The first "
                        "call never cancels anything, so it is always safe. Omit mission to mean "
                        "the one active mission."
                        " \"Pause that\" or \"stop the payment one\" map to pause_mission instead; this one ends the work. Refer to missions by title, and if a reference is ambiguous the tool tells you which matched — read those back and ask which, never guess."),
        "parameters": {
            "type": "object",
            "properties": {
                "mission": {"type": "string", "description": "Mission title, id, or a phrase from its title. Omit for the current one."},
                "confirm": {"type": "string", "description": "The confirm token returned by the first call. Only pass it after the user has agreed out loud."},
            },
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
    # Every voice tool call is evidence Yuri and the user were talking. Stamped
    # here, at the one place they all pass through, so "when did we last
    # speak" cannot silently go stale — see yuri/app.py's SETTINGS_LAST_SPOKE
    # for why this rather than a disconnect hook.
    stamp_last_spoke()

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

    if name == "cancel_mission":
        c = container()
        m = c.missions.resolve(args.get("mission", ""))
        if not _cancel_is_confirmed(m.id, args.get("confirm")):
            live = c.missions.live_sessions(m.id)
            token = _arm_cancel(m.id)
            title = clip_speech(m.title, TITLE_SPEECH_MAX)
            agents = (f" and stop {len(live)} running agent"
                      f"{'s' if len(live) != 1 else ''}") if live else ""
            return {"mission_id": m.id, "title": title, "status": m.status,
                    "confirm": token, "cancelled": False,
                    "message": (f'This would end "{title}"{agents}. Nothing has been '
                                f"cancelled yet — tell the user exactly that, and only call "
                                f"cancel_mission again with confirm={token} once they agree.")}
        try:
            m = await c.missions.cancel(m.id, by="voice")
        except InvalidTransition as exc:
            raise ValueError(str(exc)) from exc
        title = clip_speech(m.title, TITLE_SPEECH_MAX)
        return {"mission_id": m.id, "title": title, "status": m.status, "cancelled": True,
                "message": f'Mission "{title}" is cancelled.'}

    if name in ("pause_mission", "resume_mission"):
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
