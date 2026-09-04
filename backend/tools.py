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

import logging
import secrets
import time
from typing import Any

from slash_commands import list_slash_commands
from yuri.app import container, container_or_none, stamp_last_spoke
from yuri.mcp import naming as mcp_naming
from yuri.mcp.jsonrpc import McpError
from yuri.mcp.manager import CONFIRM_ARG
from yuri.own import search as own_search
from yuri.domain.mission import InvalidTransition
from yuri.services.missions import MISSION_LIST_MAX, TITLE_SPEECH_MAX, clip_speech

# start_session duplicate guard (see the handler): the most recent session
# creation, so a rapid second call can be redirected to it instead of silently
# spawning a twin. {"ts": monotonic, "handle": str, "name": str} or None.
log = logging.getLogger("yuri.tools")

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
# --- the confirmation gate -------------------------------------------------
#
# A tool declares `"tier": "confirm"` and the gate below is what makes that
# declaration mean something. Borrowed from ~/projects/project-yuri, which
# declares a permissionTier on all ~35 of its tools — and enforces it nowhere:
# the whole gate there is a console.log reminding the daemon that the model
# SHOULD have asked (apps/daemon/src/agents/tool-agent.ts:728-732), so the
# protection is a sentence in a prompt. That is the exact mechanism that
# failed here on 2026-09-04, when cancel_mission's description said "confirm
# with the user first" and Yuri cancelled a mission nobody had named.
#
# ON PLACEMENT, honestly: this is not a central interceptor and cannot be.
# The arm has to be keyed on the tool's RESOLVED TARGET — a token armed for
# one mission must not cancel another — and only the tool knows how to resolve
# a spoken reference into an id. So the mechanism is shared and the call site
# is inside the tool, after resolution. What IS central is the enforcement
# that the call happened at all: `_confirm_consulted` is set by the gate and
# checked by dispatch_tool, so a confirm-tier tool that forgets to consult it
# raises instead of quietly running ungated.
CONFIRM_SECS = 120.0
_pending_confirm: dict[str, Any] | None = None
_confirm_consulted: bool = False


def _declined_without_acting() -> None:
    """Satisfy the central gate check for a confirm-tier tool that REFUSED.

    dispatch_tool raises if a confirm-tier tool returns without consulting the
    gate, because that would mean it acted ungated. A tool that returns having
    done NOTHING — start_mission finding the mission already running — has
    nothing to gate, and the alternative is that a correct refusal comes back
    to the model as "the tool failed unexpectedly". Same reasoning that moved
    the check off the `finally`: the gate protects actions, not returns.
    """
    global _confirm_consulted
    _confirm_consulted = True


def _confirm_gate(tool: str, target: str, token: str | None) -> str | None:
    """Consult the gate for `tool` acting on `target`.

    Returns None when the caller may proceed, or a fresh token to read back to
    the user when it may not. Single use: the pending arm is consumed whether
    or not it matched, so a wrong guess cannot be retried against a still-live
    arm.
    """
    global _pending_confirm, _confirm_consulted
    _confirm_consulted = True
    pending, _pending_confirm = _pending_confirm, None

    if (token and pending
            and time.monotonic() - pending["ts"] <= CONFIRM_SECS
            and pending["tool"] == tool
            and pending["target"] == target
            and secrets.compare_digest(str(pending["token"]), str(token))):
        return None

    fresh = secrets.token_hex(3)
    _pending_confirm = {"ts": time.monotonic(), "tool": tool,
                        "target": target, "token": fresh}
    return fresh


# The keys a realtime provider's function schema actually accepts. `tier` and
# `category` are OURS — the gate reads one and the capability map reads the
# other — and both are unknown properties to the API. Azure bakes tools in at
# mint time and OpenAI takes them in session.update; either would be sending
# fields it did not ask for, so every path to a provider strips them here.
# Gemini is unaffected (frontend/lib/gemini.ts maps name/description/parameters
# explicitly), but it goes through the same helper so there is one answer.
_MODEL_KEYS = ("type", "name", "description", "parameters")


def mcp_definitions() -> list[dict[str, Any]]:
    """Tools from connected MCP servers, derived live and never cached.

    Derived on every call on purpose: a server that has gone down must stop
    being advertised the moment it does, because the capability map is built
    from this list and a stale entry makes her offer something that will fail.
    Empty when there is no container (a test importing tools.py, or startup
    having failed) rather than raising — MCP is additive, so its absence is
    never the reason a tool call breaks.
    """
    c = container_or_none()
    if c is None:
        return []
    try:
        return c.mcp.tool_definitions()
    except Exception:      # pragma: no cover - defensive
        log.exception("listing MCP tools failed")
        return []


def all_tools() -> list[dict[str, Any]]:
    """Every tool she has right now: the native ones plus MCP's.

    Native first, so an MCP server can never take precedence in a
    first-match lookup — though the namespace already makes a collision
    impossible (see yuri/mcp/naming.py).
    """
    return list(TOOL_DEFINITIONS) + mcp_definitions()


def tools_for_model() -> list[dict[str, Any]]:
    """Every tool with our own bookkeeping fields removed."""
    return [{k: v for k, v in d.items() if k in _MODEL_KEYS} for d in all_tools()]


def tier_of(name: str) -> str:
    """A tool's declared tier. Absent means "safe" — the common case, and the
    right default for a field being added to 23 existing tools."""
    for d in all_tools():
        if d.get("name") == name:
            return str(d.get("tier") or "safe")
    return "safe"


def confirm_tools() -> list[str]:
    return [str(d["name"]) for d in all_tools() if (d.get("tier") or "safe") == "confirm"]

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "list_projects",
        "category": "orchestration",
        "description": "List the project directories available to work in (allowed roots and their subfolders). Call this when the user names a folder vaguely or you don't know the absolute path — then pick or confirm one of these instead of asking the user for a full path.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "list_sessions",
        "category": "orchestration",
        "description": "List the Claude Code sessions currently running on this machine, with their human-readable name, project directory, and status. Each session also reports its work pipeline: `running` (a turn is executing right now), `queued` (how many follow-up turns are waiting behind the current one), and `pending` (finished turns not yet narrated). Use these to answer 'is it still working?' or 'what's queued on the billing session?'. Use the names here to refer to sessions in other calls. A session whose status is needs_permission or needs_choice includes the full pending prompt under `prompt` (for plan approvals this contains the entire plan) — use it to tell the user what's being asked, then respond with answer_prompt. Sessions open when you connected are listed in your context, but that list goes stale — call this for live status or when time has passed. IN QUIET MODE there is no update when a session merely finishes (only problems, permissions and questions arrive), so do not say \"I'll let you know\" and then wait for something that will not come: acknowledge, then call this or read_session when you or the user want to know where the work got to.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "start_session",
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "herself",
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
        "name": "web_search",
        "tier": "safe",
        "category": "own",
        "description": ("Look something up on the web and get a short answer with its sources. "
                        "Use this for anything you don't know or can't be sure is current — "
                        "news, prices, versions, whether something still exists. Do NOT use it "
                        "for something you already know, and do NOT re-search to double-check "
                        "an answer you just gave; each call costs time the user is waiting "
                        "through. One search per call: it will not follow up on its own. "
                        "CITE BY NAME, never by URL — say \"Wikipedia says\", because a spoken "
                        "URL is unusable and cannot be checked. If `grounded` comes back false "
                        "there were no sources, so say you couldn't find one rather than "
                        "presenting the answer as looked-up."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for, in plain words."},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "remember",
        "category": "herself",
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
        "category": "herself",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        "category": "orchestration",
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
        # Irreversible-ish: it ends work and stops running agents. The tier is
        # what wires the gate; see _confirm_gate.
        "tier": "confirm",
        "category": "orchestration",
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
    # --- the workflow tools (spec §14.1) ------------------------------------
    #
    # NOTHING here creates, edits or archives a specialist. A system prompt
    # dictated through a speech recogniser is a persona nobody reviewed, and
    # it would then run with tool access on the user's machine. Creation is
    # UI/API only, deliberately, and that omission is asserted by a test
    # rather than left to this comment.
    {
        "type": "function",
        "name": "start_mission",
        "category": "orchestration",
        "tier": "confirm",
        "description": (
            "Plan a multi-agent mission and, once the user agrees, run it. Use this when the "
            "work has several steps or needs more than one specialist — \"fix the login bug "
            "and get it reviewed\", \"look into the slow query then patch it\". For a single "
            "conversation with one agent, use start_session instead.\n"
            "TWO CALLS, always. The first call BUILDS THE PLAN AND RUNS NOTHING: it returns "
            "the steps and who would do each one. Read that plan back in one short sentence "
            "(\"the researcher looks, then Claude fixes it, then a review — start?\") and WAIT. "
            "Only when the user agrees, call it again with the confirm token from the first "
            "call. A misheard plan that runs unseen is the one failure this tool exists to "
            "prevent, so never skip the read-back, and never claim work started after the "
            "first call."),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What the mission has to achieve, in the user's own words."},
                "project": {"type": "string", "description": "The project folder to work in. Omit to use the one active project."},
                "template": {"type": "string", "description": "Which plan shape to use. Omit and one is chosen for you; call list_templates if the user asks what shapes exist."},
                "title": {"type": "string", "description": "A short name for the mission. Omit to derive one from the goal."},
                "confirm": {"type": "string", "description": "The confirm token from the first call. Only pass it after the user has agreed to the plan out loud."},
            },
            "required": ["goal"],
        },
    },
    {
        "type": "function",
        "name": "describe_roster",
        "category": "orchestration",
        "description": (
            "Who Yuri has to work with: the specialists, what each one is for, and which "
            "engine runs it. Call this for \"who do you have?\", \"who could review this?\" or "
            "before assigning work by name. You cannot create or change a specialist by "
            "voice — if the user wants a new one, say they can add it in the Agents view."),
        "parameters": {
            "type": "object",
            "properties": {"role": {"type": "string", "description": "Optional: only specialists for this role (researcher, developer, tester, reviewer, verifier, documenter)."}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "workflow_status",
        "category": "orchestration",
        "description": (
            "Where a mission's plan has got to: each step, who has it, and what is waiting. "
            "Call this for \"how's it going?\" about a multi-step mission. mission_status is "
            "the shorter answer about the mission itself; this one is the steps."),
        "parameters": {
            "type": "object",
            "properties": {"mission": {"type": "string", "description": "Mission title, id, or a phrase from its title. Omit for the current one."}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "assign_task",
        "category": "orchestration",
        "description": (
            "Give one step of a mission to a named specialist — \"let Claude do the review\", "
            "\"give the fix to the developer\". Only works before that step starts. Name the "
            "step by a phrase from its title; if it is ambiguous the tool tells you which "
            "matched — read those back and ask which, never guess."),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A phrase from the step's title, or its id."},
                "specialist": {"type": "string", "description": "The specialist's name, as describe_roster reports it."},
                "mission": {"type": "string", "description": "Mission title, id, or a phrase. Omit for the current one."},
            },
            "required": ["task", "specialist"],
        },
    },
    {
        "type": "function",
        "name": "retry_task",
        "category": "orchestration",
        "description": (
            "Run a failed or stuck step again, after the user has decided to. Say what failed "
            "first — the reason is on the step — because retrying without changing anything "
            "usually fails the same way."),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A phrase from the step's title, or its id."},
                "mission": {"type": "string", "description": "Mission title, id, or a phrase. Omit for the current one."},
            },
            "required": ["task"],
        },
    },
    {
        "type": "function",
        "name": "skip_task",
        "category": "orchestration",
        "tier": "confirm",
        "description": (
            "Drop a step and let the rest of the plan carry on. Needs the user's agreement: "
            "call it once to hear what would be skipped, tell them exactly that, then call it "
            "again with the confirm token once they agree. Skipping a test or a review means "
            "the mission finishes without that check ever having run, so say so."),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A phrase from the step's title, or its id."},
                "mission": {"type": "string", "description": "Mission title, id, or a phrase. Omit for the current one."},
                "confirm": {"type": "string", "description": "The confirm token from the first call. Only pass it after the user has agreed out loud."},
            },
            "required": ["task"],
        },
    },
    {
        "type": "function",
        "name": "list_templates",
        "category": "orchestration",
        "description": (
            "The plan shapes available for a mission, and the steps each one has. Call this "
            "when the user asks what kinds of mission you can run, or when choosing a "
            "template for start_mission."),
        "parameters": {"type": "object", "properties": {}, "required": []},
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


async def _dispatch_mcp(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool that came from a configured MCP server.

    Two things this does that the native tools get for free:

    * The gate, for a `confirm`-tier server. The arm is keyed on the tool
      name, which IS the resolved target here — an MCP tool acts on whatever
      its own arguments say, and we cannot read a third party's schema well
      enough to know what that is. So the confirmation says which tool, not
      which object; that is weaker than cancel_mission's per-mission arm and
      is the honest limit of what we know.
    * The attribution. `manager.call` returns text prefixed with the server's
      own name, because a third party's answer is something that was SAID, not
      something verified — the same rule her prompt already applies to what an
      agent claims.
    """
    c = container_or_none()
    if c is None:
        raise ValueError("MCP servers aren't available right now.")
    payload = {k: v for k, v in (args or {}).items() if k != CONFIRM_ARG}
    if tier_of(name) == "confirm":
        token = _confirm_gate(name, name, (args or {}).get(CONFIRM_ARG))
        if token is not None:
            server, tool = mcp_naming.split(name)
            return {"tool": name, "server": server, "ran": False, CONFIRM_ARG: token,
                    "message": (f"This would run {tool} on the {server} service with "
                                f"{payload or 'no arguments'}. Nothing has happened yet — "
                                f"tell the user exactly that, and only call {name} again "
                                f"with {CONFIRM_ARG}={token} once they agree.")}
    try:
        text = await c.mcp.call(name, payload)
    except McpError as exc:
        # A soft error: the model reads it back and can offer to reconnect,
        # rather than the turn dying on an exception.
        raise ValueError(str(exc)) from exc
    server, tool = mcp_naming.split(name)
    return {"tool": name, "server": server, "ran": True, "result": text,
            "message": text}


# --- the workflow tools' helpers ------------------------------------------
#
# These live here rather than in WorkflowEngine for the same reason
# `_require_session` does: they are recovery instructions written for the
# voice model to READ, and the ambiguity rules are about spoken references,
# not about task graphs.

ERROR_SPEECH_MAX = 200
# The most recent mission start, so a rapid second call is redirected to it
# instead of silently starting a twin. Mirrors _last_start (start_session) --
# same voice-model quirk, same shape. Advisory only: the durable check is
# _running_mission_for(), which reads the store and so survives the reload
# uvicorn does while the user is mid-sentence.
_last_mission: dict[str, Any] | None = None
# Words that carry no identifying information in a step title. Without this,
# "run the tests" overlapped every title in the bug-fix template through
# "the", and every spoken reference came back ambiguous.
STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "into", "that", "this", "from", "step",
    "task", "one", "its", "our", "any", "all", "out",
})
STEPS_SPEECH_MAX = 8
# Which plan an ask gets, when the user did not name one.
#
# There was a single default ("bug-fix"), so "create a single-page website"
# was planned as a bug hunt and a researcher was told to "Investigate the
# bug" for a landing page. The `feature` template existed the whole time; the
# default simply never looked at the goal.
#
# First match wins, so order matters: "fix the failing test" is a bug before
# it is a test. The fallback is `single` -- one task, one specialist -- because
# guessing a multi-step plan for an ask we did not understand is exactly how
# "Investigate the bug" happened. A wrong guess here is also recoverable: the
# plan is read back before anything runs, and it now NAMES the plan it chose.
TEMPLATE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bug-fix", ("bug", "broken", "crash", "crashes", "crashing", "hang", "hangs",
                 "hanging", "fail", "failing", "fails", "error", "regression", "wrong",
                 "fix", "fixing", "repair", "debug")),
    ("code-review", ("review", "critique", "look over", "check over")),
    ("refactor", ("refactor", "clean up", "tidy", "restructure", "rename", "extract",
                  "simplify", "reorganise", "reorganize")),
    ("research", ("investigate", "research", "find out", "look into", "why", "how does",
                  "understand", "compare", "explore")),
    ("feature", ("build", "create", "add", "make", "write", "design", "implement",
                 "new", "website", "page", "feature", "support")),
)
FALLBACK_TEMPLATE = "single"


def template_for(goal: str, available: dict) -> str:
    """Pick a plan shape from what the user asked for.

    Pure and separate from the tool so it can be tested against every phrasing
    without a container.
    """
    # Padded both ends, so a bare `in` is a whole-word match: "fixture" must
    # not read as "fix", and "newsletter" must not read as "new".
    low = f" {' '.join((goal or '').lower().split())} ".replace(",", " ").replace(".", " ")
    for name, words in TEMPLATE_HINTS:
        if name in available and any(f" {word} " in low for word in words):
            return name
    return FALLBACK_TEMPLATE if FALLBACK_TEMPLATE in available else next(iter(available), "")


def _live_workflow(m):
    """The mission's live workflow, or a soft error saying there isn't one."""
    w = container().workflow.live_for_mission(m.id)
    if w is None:
        raise ValueError(
            f'"{clip_speech(m.title, TITLE_SPEECH_MAX)}" has no plan — it is a single '
            "session, not a multi-step mission. Use the session tools for it, or start a "
            "new mission with a plan.")
    return w


def _resolve_task(m, ref: str):
    """Resolve a spoken step reference, refusing to guess.

    Exact id, then a unique word-overlap match against the plan's titles.
    Ambiguity raises and LISTS what matched, because picking one would run
    the wrong step — the same rule missions.resolve() follows.
    """
    c = container()
    w = _live_workflow(m)
    tasks = c.workflow.tasks_of(w.id)
    ref = " ".join((ref or "").split()).strip()
    if not ref:
        raise ValueError("which step? Name it, or call workflow_status and read the steps back.")
    for t in tasks:
        if t.id == ref:
            return t
    low = ref.lower()
    # Narrowest match that works, in order, so a spoken title that IS a step
    # resolves cleanly before the fuzzy pass ever runs.
    exact = [t for t in tasks if t.title.lower() == low]
    if len(exact) == 1:
        return exact[0]
    substring = [t for t in tasks if low in t.title.lower()]
    if len(substring) == 1:
        return substring[0]
    # Only now, and only on words that carry meaning: "run the tests" shares
    # "the" with every title in the bug-fix template, so an unfiltered overlap
    # called every reference ambiguous.
    words = {w_ for w_ in low.split() if len(w_) > 2 and w_ not in STOPWORDS}
    hits = substring or [t for t in tasks
                         if words and words & set(t.title.lower().split())]
    if not hits:
        titles = ", ".join(t.title for t in tasks)
        raise ValueError(f"no step matches {ref!r}. The plan is: {titles}.")
    if len(hits) > 1:
        titles = ", ".join(t.title for t in hits)
        raise ValueError(
            f"{ref!r} matches several steps: {titles}. Read those back and ask which one, "
            "then pass the exact title.")
    return hits[0]


def _workflow_speech(m) -> dict:
    """A plan shaped for speaking."""
    c = container()
    w = _live_workflow(m)
    tasks = c.workflow.tasks_of(w.id)
    people = {s.id: s.name for s in c.roster.list(include_archived=True)}
    steps = [{"step": clip_speech(t.title, TITLE_SPEECH_MAX),
              "status": t.status,
              "who": people.get(t.specialist_id or "") or None,
              "problem": clip_speech(t.error, ERROR_SPEECH_MAX) or None}
             for t in tasks[:STEPS_SPEECH_MAX]]
    waiting = [x["step"] for x in steps if x["status"] in ("failed", "blocked")]
    running = [x["step"] for x in steps if x["status"] in ("dispatched", "running", "verifying")]
    return {"mission": clip_speech(m.title, TITLE_SPEECH_MAX), "status": w.status,
            "steps": steps, "running": running, "needs_you": waiting,
            "more_steps": max(0, len(tasks) - len(steps))}


def _running_mission_for(goal: str):
    """A mission that is ALREADY doing this, or None.

    Read from the store rather than from `_last_mission`, deliberately: the
    in-memory note is lost every time uvicorn --reload restarts the process,
    which happens while the user is mid-sentence. A duplicate that survives a
    reload is exactly the one nobody would catch.

    Matched on the goal, because that is what identity means here: the title
    is derived from the goal, and the same request phrased once produces the
    same goal string every time the model repeats the call.
    """
    want = " ".join((goal or "").lower().split())
    if not want:
        return None
    for m in container().missions.active():
        if " ".join((m.goal or m.title or "").lower().split()) == want:
            return m
    return None


async def _start_mission(args: dict[str, Any]) -> dict[str, Any]:
    """Plan first, run only on the second call (spec §14.1).

    The gate is not politeness: a plan assembled from a misheard goal, run
    unseen, is the one failure spoken authoring can cause that the user
    cannot undo. So the first call builds a REAL workflow (born `draft`, which
    dispatches nothing) and returns its steps to be read back; the second
    releases it.
    """
    c = container()
    goal = " ".join(str(args.get("goal") or "").split())
    if not goal:
        raise ValueError("what should the mission achieve? Ask the user, then call this again.")
    template = (str(args.get("template") or "").strip()
                or template_for(goal, c.workflow.templates))
    if template not in c.workflow.templates:
        known = ", ".join(sorted(c.workflow.templates))
        raise ValueError(f"there is no {template!r} plan. The ones you have are: {known}.")

    # BEFORE the gate, and this is the whole fix for the loop observed on
    # 2026-09-04. The model fires start_mission two or three times within
    # seconds carrying the same token (the quirk _last_start exists for). The
    # first call consumed it and started the mission; every later call found
    # it spent, so the gate armed a FRESH token and answered "NOTHING HAS
    # STARTED. Read this plan back and wait." That was false AND it was an
    # instruction, so the plan got read back, the user said yes, and a second
    # mission started. Five missions and five live agents from one request.
    #
    # The gate was working as designed. What it cannot know is that a mission
    # already exists for this goal — so that is checked here, and checked
    # against the store so a reload cannot forget it.
    running = None if args.get("another") else _running_mission_for(goal)
    if running is not None:
        _declined_without_acting()
        # No `confirm` key in this result, deliberately: handing back a fresh
        # token is what let the next "yes" start another one.
        title = clip_speech(running.title, TITLE_SPEECH_MAX)
        return {"started": False, "already_running": True, "mission_id": running.id,
                "title": title, "status": running.status,
                "message": (
                    f'"{title}" is ALREADY RUNNING — this exact mission is underway, so '
                    "nothing new was started and you must NOT plan it again. Tell the user "
                    "it is already going, and use workflow_status if they want to know where "
                    "it has got to. Only if they explicitly want a SECOND, SEPARATE mission "
                    "for the same thing, call start_mission again with another=true.")}

    # Keyed on the goal, not on a mission id: the first call has not created
    # anything yet, so the goal IS the identity of what is being confirmed —
    # and that means a token armed for one goal cannot start a different one.
    token = _confirm_gate("start_mission", f"{template}:{goal}", args.get("confirm"))
    if token is not None:
        tpl = c.workflow.templates[template]
        people = {}
        for task in tpl.tasks:
            try:
                people[task.id] = c.roster.resolve(task.role or "").name
            except Exception:                               # noqa: BLE001
                # A role nobody can fill is worth saying NOW, while the user is
                # deciding, rather than as a failed task later.
                people[task.id] = None
        plan = [{"step": task.title, "who": people.get(task.id)} for task in tpl.tasks]
        unfillable = [p["step"] for p in plan if not p["who"]]
        return {"started": False, "confirm": token, "goal": goal, "template": template,
                "plan": plan, "no_one_for": unfillable,
                "message": (
                    f"NOTHING HAS STARTED. This is the {template!r} plan. Read it back in one "
                    "short sentence — who does what, in order — and wait for the user to "
                    "agree. "
                    + (f"Say that no one can do: {', '.join(unfillable)}. " if unfillable else "")
                    + f"Only then call start_mission again with confirm={token}.")}

    project = c.projects.resolve_or_create(str(args.get("project") or ""))
    title = " ".join(str(args.get("title") or "").split()) or goal[:60]
    # Recorded BEFORE the (slow) create, so a concurrent duplicate call trips
    # the guard rather than racing past it — the same reason start_session
    # records its guard before starting.
    global _last_mission
    _last_mission = {"ts": time.monotonic(), "goal": goal, "mission_id": "", "title": title}
    m = c.missions.create(project, title, created_by="voice", goal=goal)
    _last_mission = {"ts": time.monotonic(), "goal": goal, "mission_id": m.id, "title": title}
    w = await c.workflow.create(m, template, goal)
    await c.workflow.resume(w.id, by="voice")
    started = await c.workflow.advance(w.id)
    return {"started": True, "mission_id": m.id,
            "title": clip_speech(m.title, TITLE_SPEECH_MAX), "template": template,
            "steps": [t.title for t in c.workflow.tasks_of(w.id)][:STEPS_SPEECH_MAX],
            "now_running": [clip_speech(t.title, TITLE_SPEECH_MAX) for t in started],
            "message": (f'Mission "{clip_speech(m.title, TITLE_SPEECH_MAX)}" is running.'
                        + (f" {started[0].title} has started." if started else ""))}


async def dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    # Every voice tool call is evidence Yuri and the user were talking. Stamped
    # here, at the one place they all pass through, so "when did we last
    # speak" cannot silently go stale — see yuri/app.py's SETTINGS_LAST_SPOKE
    # for why this rather than a disconnect hook.
    stamp_last_spoke()

    global _confirm_consulted
    _confirm_consulted = False
    out = await _dispatch(name, args)
    # The central half of the gate. A tool declaring tier="confirm" that
    # RETURNS without consulting _confirm_gate has run ungated, which is the
    # whole failure this mechanism exists to prevent — and a silent version is
    # worse than none, because the declaration reads as protection.
    #
    # On a normal return only, deliberately. This was a `finally`, so a
    # confirm-tier tool that REFUSED to act — cancel_mission for a mission
    # that does not exist, start_mission with no goal — had its soft
    # ValueError replaced by this AssertionError, and the model was handed
    # "the tool failed unexpectedly" instead of "which mission?". A tool that
    # raised did not act, so there is nothing to have gated.
    if tier_of(name) == "confirm" and not _confirm_consulted:
        raise AssertionError(
            f"{name} declares tier=\"confirm\" but did not consult "
            "_confirm_gate — it ran without a confirmation")
    return out


async def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:

    if mcp_naming.is_mcp(name):
        return await _dispatch_mcp(name, args)

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
        token = _confirm_gate("cancel_mission", m.id, args.get("confirm"))
        if token is not None:
            live = c.missions.live_sessions(m.id)
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

    # --- the workflow tools (spec §14.1) ------------------------------------

    if name == "describe_roster":
        c = container()
        role = str(args.get("role") or "").strip().lower()
        people = [s for s in c.roster.list() if not role or s.role == role]
        if role and not people:
            # Not an empty list: "nobody does that" and "there is no such
            # role" need different things from the user.
            known = ", ".join(sorted({s.role for s in c.roster.list()}))
            raise ValueError(f"no specialist has the role {role!r}. The roles in use are: {known}.")
        return {"specialists": [{"name": s.name, "role": s.role,
                                 "engine": s.provider_id,
                                 "what_for": clip_speech(s.description, TITLE_SPEECH_MAX) or None}
                                for s in people],
                "can_create_by_voice": False,
                "message": ("Read these back by name and role. You cannot create or change a "
                            "specialist by voice — if the user wants one, tell them they can "
                            "add it in the Agents view.")}

    if name == "list_templates":
        c = container()
        return {"templates": [{"name": t.name, "description": t.description,
                               "steps": [task.title for task in t.tasks]}
                              for t in sorted(c.workflow.templates.values(), key=lambda t: t.name)]}

    if name == "start_mission":
        return await _start_mission(args)

    if name == "workflow_status":
        c = container()
        m = c.missions.resolve(args.get("mission", ""))
        return _workflow_speech(m)

    if name == "assign_task":
        c = container()
        m = c.missions.resolve(args.get("mission", ""))
        t = _resolve_task(m, str(args.get("task") or ""))
        who = str(args.get("specialist") or "").strip()
        s = c.roster.by_name(who)
        if s is None:
            names = ", ".join(x.name for x in c.roster.list())
            raise ValueError(f"there is no specialist called {who!r}. You have: {names}.")
        try:
            t = await c.workflow.assign(t.id, s.id, by="voice")
        except Exception as exc:                            # noqa: BLE001
            # NoSpecialist and the transition errors are all soft: the model
            # reads the reason back, which already names the fix.
            raise ValueError(str(exc)) from exc
        return {"task": clip_speech(t.title, TITLE_SPEECH_MAX), "specialist": s.name,
                "message": f'{s.name} will do "{clip_speech(t.title, TITLE_SPEECH_MAX)}".'}

    if name == "retry_task":
        c = container()
        m = c.missions.resolve(args.get("mission", ""))
        t = _resolve_task(m, str(args.get("task") or ""))
        was = t.error
        try:
            t = await c.workflow.retry(t.id, by="voice")
        except Exception as exc:                            # noqa: BLE001
            raise ValueError(str(exc)) from exc
        await c.workflow.advance(t.workflow_id)
        title = clip_speech(t.title, TITLE_SPEECH_MAX)
        return {"task": title, "status": c.workflow.get_task(t.id).status,
                "previous_error": clip_speech(was, ERROR_SPEECH_MAX) or None,
                "message": f'"{title}" is running again.'}

    if name == "skip_task":
        c = container()
        m = c.missions.resolve(args.get("mission", ""))
        t = _resolve_task(m, str(args.get("task") or ""))
        token = _confirm_gate("skip_task", t.id, args.get("confirm"))
        title = clip_speech(t.title, TITLE_SPEECH_MAX)
        if token is not None:
            checks = ", ".join(t.verification)
            loses = (f" Nothing will check {checks} for this mission." if checks else "")
            return {"task": title, "skipped": False, "confirm": token,
                    "message": (f'This would drop "{title}" and let the rest of the plan '
                                f"carry on.{loses} Nothing has been skipped yet — tell the "
                                f"user exactly that, and only call skip_task again with "
                                f"confirm={token} once they agree.")}
        try:
            t = await c.workflow.skip(t.id, by="voice")
        except Exception as exc:                            # noqa: BLE001
            raise ValueError(str(exc)) from exc
        await c.workflow.advance(t.workflow_id)
        return {"task": title, "skipped": True,
                "message": f'"{title}" is skipped; the rest of the plan continues.'}

    if name == "web_search":
        # SearchUnavailable is a soft error: main.py hands its message to the
        # model, which reads it out. That is the whole point of the named type
        # — "I can't search, GEMINI_API_KEY isn't set" is actionable, and
        # "the tool failed unexpectedly" is not.
        res = await own_search.search(str(args.get("query") or ""))
        return res.to_dict()

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
