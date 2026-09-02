"""Who narrates which fact, and what each mode speaks.

Yuri has THREE ways the user learns something happened: the per-session poll
result the frontend already drains, the domain event stream, and the result of a
voice tool she just called (which she speaks). The first two both carry facts
that are ALSO marked speakable in domain/event.py's DEFAULTS — so without a
declared split she says everything twice. This module is that declaration.

Poll owns the session-turn events because its result is the only carrier that
sees EVERY sub-question of a multi-question AskUserQuestion (the tmux runner
notifies only from its hook path — see docs/yuri/follow-ups.md). The stream owns
mission-level state, which poll cannot see at all.

ONE OWNER PER FACT, NOT PER EVENT TYPE
--------------------------------------
The NARRATION_OWNER table below is necessary but NOT sufficient. The invariant
we actually want is "the user hears each fact once", and per-type ownership is
strictly weaker than that: two different event types can carry the same fact.
Two counterexamples were confirmed on this branch —

  one agent error   poll:   'Fake Agent hit an error: tmux pane died.'
                  + stream: '"billing" failed: tmux pane died.'
                    (`_fail_if_alone` turns the error into a mission failure
                     carrying the SAME reason string)

  pause_mission     tool:   'Mission "payments" is now paused.'
                  + stream: '"payments" is paused.'
                    (the tool result Yuri speaks is itself a carrier)

— so a mission event is narrated only when it is the FIRST carrier of its fact.
The payload's origin field decides that, and it is the only input needed:

  by == "voice"    The user commanded the change; the voice tool's own result is
                   spoken in the same breath and IS the report. Saying it again
                   is telling the user what they just did — verbatim the
                   rationale the `none` bucket already rests on.
  by == "system"   The change is derived, never original. `_mission_to` is the
                   sole producer of a system transition, and every transition it
                   makes echoes a session-level event poll owns and has already
                   spoken: failed ← agent.error (same `reason` string), paused ←
                   the session the user just closed, waiting_for_approval ← the
                   approval request. Nothing is lost by staying silent: poll is
                   the reliable carrier for those facts — every provider
                   surfaces an error through `poll_status` whether or not it
                   streams events — which is why poll owns them in the first
                   place.
  anything else    News. "ui" and "api" mean someone clicked a button or called
                   the API, and no spoken line covered it. Unknown origins
                   deliberately fail OPEN (spoken): a rare repeat beats a
                   silently swallowed line, the same trade the frontend's spoken
                   gate makes on a frame with no id.

`mission.created` is judged the same way but on `created_by`, and suppresses
ONLY "voice": a mission that Yuri or a script creates unprompted IS news, and
"handoff" is news with a different verb — `adopt()` picks up a session that was
already running, so asserting it is "starting" is the honesty class spec §5.2
forbids (see narration/service.py).
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

# Origins whose facts another carrier already delivered — see "ONE OWNER PER
# FACT" above. Split per event type because the reasoning differs: a
# voice-commanded change is reported by the tool result for both, but only a
# *status change* is derived-and-therefore-silent when it comes from "system".
VOICE = "voice"
HANDOFF = "handoff"
ALREADY_TOLD_ON_CREATE: frozenset[str] = frozenset({VOICE})
ALREADY_TOLD_ON_STATUS_CHANGE: frozenset[str] = frozenset({VOICE, "system"})


def origin(value: object) -> str:
    """Normalize a payload `by` / `created_by` field. Anything unrecognizable
    becomes "", which is not in either suppression set — so it is spoken."""
    return value.strip().lower() if isinstance(value, str) else ""


def mission_created_is_news(created_by: object) -> bool:
    """Whether a mission.created event is the first carrier of its fact."""
    return origin(created_by) not in ALREADY_TOLD_ON_CREATE


def mission_status_change_is_news(by: object) -> bool:
    """Whether a mission.status_changed event is the first carrier of its fact."""
    return origin(by) not in ALREADY_TOLD_ON_STATUS_CHANGE


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
