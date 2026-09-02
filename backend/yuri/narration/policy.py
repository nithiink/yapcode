"""Which carrier narrates which event, and what each mode speaks.

Yuri has two ways to learn something happened: the per-session poll result the
frontend already drains, and the domain event stream. Both are correct, and all
four events the poll loop narrates are ALSO marked speakable in
domain/event.py's DEFAULTS — so without a declared split she says everything
twice. This module is that declaration.

Poll owns the session-turn events because its result is the only carrier that
sees EVERY sub-question of a multi-question AskUserQuestion (the tmux runner
notifies only from its hook path — see docs/yuri/follow-ups.md). The stream owns
mission-level state, which poll cannot see at all.
"""
from __future__ import annotations

from typing import Literal

from yuri.domain.event import EventType

Mode = Literal["quiet", "normal", "verbose"]
MODES: tuple[Mode, ...] = ("quiet", "normal", "verbose")
DEFAULT_MODE: Mode = "normal"

Owner = Literal["poll", "stream", "stream_verbose", "none"]

# Exactly one owner per EventType. Enforced by test_narration_policy.
NARRATION_OWNER: dict[str, Owner] = {
    # Poll owns these: it carries them reliably, including sub-questions.
    EventType.APPROVAL_REQUESTED: "poll",
    EventType.SESSION_QUESTION: "poll",
    EventType.SESSION_TURN_COMPLETED: "poll",
    EventType.AGENT_ERROR: "poll",
    # Stream owns mission-level state and lost contact — poll cannot see them.
    EventType.MISSION_CREATED: "stream",
    EventType.MISSION_STATUS_CHANGED: "stream",
    EventType.SESSION_LOST: "stream",
    # Texture: only when the user asked to hear everything.
    EventType.TOOL_STARTED: "stream_verbose",
    EventType.COST_UPDATED: "stream_verbose",
    # Never narrated: the user caused these, so saying them is telling them
    # what they just did. session.created also fires on a rehydration REVIVAL
    # (payload.revived) and must never be announced as new work starting.
    EventType.SESSION_CREATED: "none",
    EventType.SESSION_MESSAGE_SENT: "none",
    EventType.APPROVAL_RESOLVED: "none",
    EventType.SESSION_INTERRUPTED: "none",
    EventType.SESSION_STOPPED: "none",
    EventType.PROJECT_REGISTERED: "none",
    EventType.MEMORY_REMEMBERED: "none",
}

# Blocks on the user: never suppressed, whatever the mode. "Be quiet" means
# stop chattering, not stop asking — a suppressed permission request would
# strand the agent waiting on an answer the user was never asked for.
ALWAYS_SPEAK: frozenset[str] = frozenset({
    EventType.APPROVAL_REQUESTED, EventType.SESSION_QUESTION})

_LOUD_SEVERITIES = frozenset({"warning", "error"})


def normalize_mode(value: object) -> Mode:
    """Coerce anything to a valid mode; unknown input falls back to the default."""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in MODES:
            return v  # type: ignore[return-value]
    return DEFAULT_MODE


def owner_of(event_type: str) -> Owner:
    """Which carrier narrates this type. An unrecognized type is never narrated
    rather than being a crash — a new EventType is caught by the policy test,
    not by a 500 at runtime."""
    return NARRATION_OWNER.get(event_type, "none")


def speaks(event_type: str, severity: str, mode: Mode) -> bool:
    """Whether this event is spoken in this mode."""
    own = owner_of(event_type)
    if own == "none":
        return False
    if event_type in ALWAYS_SPEAK:
        return True
    if own == "stream_verbose":
        return mode == "verbose"
    if mode == "quiet":
        return severity in _LOUD_SEVERITIES
    return True
