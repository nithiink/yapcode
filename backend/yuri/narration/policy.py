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
  by == "system"   NOT sufficient on its own — `by` says who, not whether
                   anyone else already said it. There are exactly TWO producers
                   of a system transition, and they differ:

                     1. `SessionService._mission_to` — every transition it makes
                        RESTATES a session-level event poll owns and has already
                        spoken: failed ← agent.error (same `reason` string),
                        paused ← the session the user just closed,
                        waiting_for_approval ← the approval request, running ←
                        a completed turn (no line exists). Nothing is lost by
                        staying silent: poll is the reliable carrier for those
                        facts — every provider surfaces an error through
                        `poll_status` whether or not it streams events — which
                        is why poll owns them in the first place.
                     2. `SessionService.start`'s provider-failure path
                        (sessions.py, the `except` around `create_session`) —
                        ORIGINAL news. No session row was ever inserted, so no
                        poll can ever happen; the `agent.error` published beside
                        it is poll-owned, so `line_for` returns None for it on
                        the stream; and `start_session`'s exception is neither
                        ValueError nor KeyError, so main.py hands the voice
                        model only the generic "the tool failed unexpectedly".
                        If this event is silent, a mission failing to start is
                        narrated by NOBODY.

                   So the marker, not the origin, decides: `set_status`'s
                   `derived=True` (passed only by `_mission_to`) means "this
                   restates something already delivered". A system transition
                   with no marker is spoken.
  anything else    News. "ui" and "api" mean someone clicked a button or called
                   the API, and no spoken line covered it. Unknown origins, and
                   a system change with no `derived` marker, deliberately fail
                   OPEN (spoken): a rare repeat beats a silently swallowed line,
                   the same trade the frontend's spoken gate makes on a frame
                   with no id.

If you add a new mission transition, the question to answer is not "what is
`by`" but "does any other carrier already tell the user this fact". Only pass
`derived=True` when the answer is yes, and name the carrier in a comment.

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
    # The same reasoning covers every workflow/task type: `line_for_poll` takes
    # SessionService.poll()'s RESULT dict, not an event, so a fact the engine
    # publishes on the bus can never reach the poll carrier. Declaring one
    # `poll`-owned is not a routing choice, it is silence — which for
    # workflow.deadlocked is exactly the stall spec §12 forbids.
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
    # Delete has no voice tool by design (see MissionService.delete), so the
    # user is always looking at the screen it happened on and watching the row
    # go. The event exists for the audit log, not to be read back.
    EventType.MISSION_DELETED: "none",
    # The mission finishing is ALREADY spoken by mission.status_changed, which
    # names it ('"the billing fix" is done.') — a strictly better sentence than
    # anything this event can say, since it has the title. One owner per FACT,
    # so this one stays quiet rather than both firing.
    EventType.WORKFLOW_COMPLETED: "none",
    # RosterService.create/update/archive are UI/API-only (spec: voice tools
    # never create, edit or delete a specialist), so the user is always
    # looking at the screen where it just happened -- narrating it back would
    # be telling them what they themselves did.
    EventType.SPECIALIST_CREATED: "none",
    EventType.SPECIALIST_UPDATED: "none",
    EventType.SPECIALIST_ARCHIVED: "none",
    # --- Phase 7: WorkflowEngine (spec §11) ---------------------------------
    # Workflow-level state, exactly like mission-level state: the stream is the
    # only carrier that can see it, because a session poll knows nothing about
    # a task graph.
    EventType.WORKFLOW_CREATED: "stream",
    EventType.TASK_DISPATCHED: "stream",
    # Texture in a long workflow: worth hearing when the user asked for
    # everything, noise otherwise — the same judgement tool.started got.
    EventType.TASK_COMPLETED: "stream_verbose",
    # Poll owns the three that need the user, because in every case the same
    # fact is ALSO on its way through the session that produced it: a task
    # fails because its agent errored (agent.error, poll-owned, carrying the
    # identical reason string), and blocked/deadlocked are that same failure
    # having run out of attempts. Duplicating it on the stream is the
    # `_fail_if_alone` double-speak the module docstring opens with.
    EventType.TASK_FAILED: "stream",
    EventType.TASK_BLOCKED: "stream",
    EventType.WORKFLOW_DEADLOCKED: "stream",
    # Internal: `verifying` is the state, the verdict is the news. Task 9's
    # verification.failed is what gets spoken.
    EventType.TASK_VERIFYING: "none",
}

# Blocks on the user: never suppressed, whatever the mode. "Be quiet" means
# stop chattering, not stop asking — a suppressed permission request would
# strand the agent waiting on an answer the user was never asked for.
ALWAYS_SPEAK: frozenset[str] = frozenset({
    EventType.APPROVAL_REQUESTED, EventType.SESSION_QUESTION})

_LOUD_SEVERITIES = frozenset({"warning", "error"})

# Origins whose facts another carrier already delivered — see "ONE OWNER PER
# FACT" above. A voice-commanded change is reported by the tool result for both
# event types; "system" is NOT in either set, because whether a system change
# was already told depends on the `derived` marker, not on the origin.
VOICE = "voice"
SYSTEM = "system"
HANDOFF = "handoff"
ALREADY_TOLD_ON_CREATE: frozenset[str] = frozenset({VOICE})


def origin(value: object) -> str:
    """Normalize a payload `by` / `created_by` field. Anything unrecognizable
    becomes "", which is not in any suppression set — so it is spoken."""
    return value.strip().lower() if isinstance(value, str) else ""


def mission_created_is_news(created_by: object) -> bool:
    """Whether a mission.created event is the first carrier of its fact."""
    return origin(created_by) not in ALREADY_TOLD_ON_CREATE


def mission_status_change_is_news(by: object, derived: object = False) -> bool:
    """Whether a mission.status_changed event is the first carrier of its fact.

    `derived` is the payload marker MissionService.set_status writes: True means
    this transition restates a session-level event another carrier already
    delivered. A `system` change WITHOUT it is original news (start's
    provider-failure path) and must be spoken."""
    o = origin(by)
    if o == VOICE:
        return False
    return not (o == SYSTEM and bool(derived))


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
