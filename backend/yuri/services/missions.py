"""Missions (spec §5.3). Missions are created implicitly by
SessionService.start/adopt — there is no orchestrator (Phase 4 ruled one out),
so this service is the whole of mission lifecycle: spoken resolution, the
speech shapes the voice tools read back, and pause/resume/cancel."""
from __future__ import annotations

import re
from typing import Awaitable, Callable

from yuri.domain.event import EventType, YuriEvent
from yuri.domain.mission import TRANSITIONS, InvalidTransition, Mission, MissionStep
from yuri.domain.project import Project
from yuri.domain.session import AgentSession
from yuri.events.bus import EventBus
from yuri.services.journal import Journal
from yuri.store.base import Store

GOAL_MAX = 500

# Caps on every string this module hands to the voice model. None of these
# fields is bounded upstream (a mission title comes straight from a session
# name; an approval description can be an entire plan), and the model reads
# them aloud, so clip here rather than trusting the producer.
TITLE_SPEECH_MAX = 80
GOAL_SPEECH_MAX = 240
APPROVAL_SPEECH_MAX = 300
# Session names are titles by another route (SessionService._pick_name only
# whitespace-normalizes, and rename_session takes whatever it is given), and a
# mission can hold arbitrarily many sessions, so cap both the name and the count.
SESSION_NAME_SPEECH_MAX = 60
SESSIONS_SPEECH_MAX = 12
# How many candidate titles a refusal names. Reading out 200 titles is not a
# question the user can answer; the model asks with the newest few.
CANDIDATES_MAX = 6
# Ceiling on speech_list (and so on the list_missions voice tool). The store's
# own cap is 200 rows, and each row carries a title, a goal and its session
# names — a list that long is not something the voice model can read back, and
# it is text the user never asked for. Newest first, so the cap drops the least
# relevant end. tools.py re-exports and passes it explicitly, which is also the
# patch point its test narrows.
MISSION_LIST_MAX = 40
# Ceiling on the id/title scan in resolve(). store.missions.list() orders by
# updated_at DESC, so this keeps the most recent work reachable by name; a full
# id still resolves via the indexed get() regardless of the cap.
SCAN_MAX = 500

# Spoken references to "the mission" rather than a name. Resolution treats these
# as "the one obvious mission" and refuses when more than one is active — but
# only when no mission is actually NAMED one of them (see resolve): several are
# ordinary English words, and a mission titled "current" must stay reachable.
_DEICTIC = frozenset({"", "it", "that", "this", "this one", "that one", "the current one",
                      "current", "the mission", "mission", "the current mission",
                      "the one", "the active one"})
_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that carry no identifying information. Without this, "the docs one"
# matches every active title containing "the" and resolution refuses a
# reference that was actually unambiguous.
_NOISE = frozenset({"a", "an", "and", "at", "be", "for", "from", "in", "into", "is", "it",
                    "me", "my", "of", "on", "one", "ones", "or", "our", "please", "that",
                    "the", "then", "there", "these", "this", "those", "to", "with",
                    "mission", "missions", "task", "tasks", "job", "jobs", "work",
                    "current", "active", "thing", "stuff", "about", "just", "now"})
# A spoken phrase must never enter the id-prefix branch: uuid4 ids are hex
# with dashes, so anything outside that alphabet cannot be an id fragment.
# Most words fail on one letter ("cashfree" has s/h/r) — but "cafe", "dead",
# "added" and "facade" are all valid hex, and at four characters one of those
# prefixes a real uuid roughly once in 65k missions. So require a full uuid
# segment (8), which no English word satisfies, and match exact titles BEFORE
# prefixes anyway. Below 8 a hex-spelled word falls through to the fuzzy step,
# where it is compared against titles instead of ids.
_ID_FRAGMENT_RE = re.compile(r"^[0-9a-f][0-9a-f-]{7,}$")


def clip_speech(text: str | None, cap: int) -> str:
    """Whitespace-normalize and cap. Everything spoken goes through here."""
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


class MissionService:
    #: Statuses that mean "live work". Fuzzy resolution and the deictic
    #: ("it", "that one") path are scoped to these, so a spoken reference can
    #: never land on finished work the user has stopped thinking about.
    ACTIVE: tuple[str, ...] = ("running", "waiting_for_approval", "paused", "queued")

    def __init__(self, store: Store, bus: EventBus, journal: Journal):
        self.store = store
        self.bus = bus
        self.journal = journal
        # Injected by the container (SessionService.stop_many) to avoid a cycle.
        self.stop_sessions: Callable[[list[AgentSession]], Awaitable[None]] | None = None
        # Injected by the container (SessionService.interrupt_many) to avoid a
        # circular import, same as stop_sessions.
        self.interrupt_sessions: Callable[[list[AgentSession]], Awaitable[None]] | None = None

    def get(self, mission_id: str) -> Mission:
        m = self.store.missions.get(mission_id)
        if m is None:
            raise KeyError(f"unknown mission: {mission_id}")
        return m

    def list(self, status: str | None = None) -> list[Mission]:
        return self.store.missions.list(status=status)

    def create(self, project: Project, title: str, created_by: str, goal: str | None = None,
               agent_id: str | None = None) -> Mission:
        m = Mission(title=title, project_id=project.id, goal=(goal or None) and goal[:GOAL_MAX],
                    status="running", created_by=created_by)
        self.store.missions.insert(m)
        step = MissionStep(mission_id=m.id, ordinal=1, title="work", agent_id=agent_id, status="running")
        self.store.missions.insert_step(step)
        m.current_step = step.id
        self.store.missions.update(m)
        self.bus.publish(YuriEvent.make(EventType.MISSION_CREATED, mission_id=m.id,
                                        project_id=project.id, agent_id=agent_id,
                                        payload={"title": title, "goal": m.goal,
                                                 "project": project.name, "created_by": created_by}))
        self.journal.append(f"mission created: {title} ({project.name})")
        return m

    def set_goal_if_empty(self, mission: Mission, goal: str) -> None:
        if mission.goal or not goal:
            return
        mission.goal = " ".join(goal.split())[:GOAL_MAX]
        self.store.missions.update(mission)

    def set_status(self, mission: Mission, to: str, by: str, reason: str | None = None,
                   *, derived: bool = False) -> bool:
        """Transition and publish. `derived=True` marks a change that merely
        RESTATES a session-level event another carrier already delivered, so
        narration can stay silent without swallowing an original fact — see
        yuri/narration/policy.py. Only SessionService._mission_to sets it;
        defaulting to False fails OPEN (spoken), matching the rest of the rule.
        """
        frm = mission.status
        if not mission.transition(to):      # raises InvalidTransition on bad edges
            return False
        self.store.missions.update(mission)
        self.bus.publish(YuriEvent.make(EventType.MISSION_STATUS_CHANGED, mission_id=mission.id,
                                        project_id=mission.project_id,
                                        payload={"from": frm, "to": to, "by": by, "reason": reason,
                                                 "title": mission.title, "derived": derived}))
        self.journal.append(f"mission '{mission.title}': {frm} → {to}" + (f" ({reason})" if reason else ""))
        return True

    def detail(self, mission_id: str) -> dict:
        m = self.get(mission_id)
        sessions = self.store.sessions.list(mission_id=m.id)
        # ApprovalRepo has no mission_id filter (approvals are keyed off
        # session_id), and its `list()` caps at the N most recently
        # *requested* rows store-wide — filtering that in Python would
        # silently drop this mission's approvals once enough other missions'
        # approvals outnumbered the cap. Scope correctly instead: an
        # approval belongs to this mission iff its session does, so fetch
        # per-session (indexed on session_id) and merge. This equivalence
        # holds only under today's implicit invariants: a session's
        # mission_id is fixed at creation and never reparented, and neither
        # SessionRepo nor ApprovalRepo exposes a delete. If either operation
        # is added later, revisit this merge.
        approvals = [a for s in sessions for a in self.store.approvals.list(session_id=s.id)]
        approvals.sort(key=lambda a: a.requested_at)
        return {"mission": m.to_dict(),
                "steps": [s.to_dict() for s in self.store.missions.steps_for(m.id)],
                "sessions": [s.to_dict() for s in sessions],
                "approvals": [a.to_dict() for a in approvals],
                "events": [e.to_dict() for e in self.store.events.list(mission_id=m.id, limit=50)]}

    # --- spoken references ----------------------------------------------------

    def active(self) -> list[Mission]:
        """Missions that are live work, newest first."""
        out: list[Mission] = []
        for status in self.ACTIVE:
            out.extend(self.store.missions.list(status=status))
        return sorted(out, key=lambda m: m.updated_at, reverse=True)

    @staticmethod
    def _refuse(candidates: list[Mission], lead: str) -> ValueError:
        """Build the refusal. Titles are clipped and the list is capped: the
        message is read aloud, and neither field is bounded upstream."""
        shown = candidates[:CANDIDATES_MAX]
        names = ", ".join(f'"{clip_speech(m.title, TITLE_SPEECH_MAX)}"' for m in shown)
        extra = len(candidates) - len(shown)
        if extra > 0:
            names += f", and {extra} more"
        return ValueError(f"{lead} {names}. Ask the user which one.")

    def resolve(self, ref: str) -> Mission:
        """Resolve a spoken mission reference, refusing to guess.

        Order: exact id, exact title (any status), unique id prefix, then word
        overlap against ACTIVE titles. A deictic phrase ("it", "the current
        one") means the sole active mission. Ambiguity raises ValueError listing
        the candidates — cancelling the wrong mission is worse than asking.

        Exact titles are matched BEFORE id prefixes, inverting design §8.1's
        order: uuid4 ids are hex, and hex-spelled words ("cafe", "added",
        "facade") are plausible titles, so a title said in full could otherwise
        be read as some *other* mission's id prefix — a silent wrong pick. The
        prefix branch is additionally gated on the ref looking like an id
        fragment at all (see _ID_FRAGMENT_RE). Nothing reachable before is lost:
        a full id is still matched first, by primary key.

        The deictic step yields to a real name for the same reason. Half of
        _DEICTIC is ordinary English ("current", "mission", "that"), so a
        mission genuinely titled "current" would otherwise be unreachable by
        name AND `resolve("current")` would return a different mission — a
        silent wrong pick. A deictic phrase is only deictic when nothing bears
        it as a title.
        """
        ref = " ".join((ref or "").strip().split())
        low = ref.lower()
        active = self.active()
        # Fetched at most once, and lazily: the deictic branch needs it only to
        # rule out a mission actually named "current", and the hottest path of
        # all — tools.py passing "" because the model omitted the argument —
        # short-circuits before the query, since no mission can be titled "".
        scanned: list[Mission] | None = None

        def all_missions() -> list[Mission]:
            nonlocal scanned
            if scanned is None:
                scanned = self.store.missions.list(limit=SCAN_MAX)
            return scanned

        def named(title: str) -> list[Mission]:
            return [m for m in all_missions() if m.title.lower() == title]

        if low in _DEICTIC and not (low and named(low)):
            if len(active) == 1:
                return active[0]
            if not active:
                raise ValueError("There are no active missions right now.")
            raise self._refuse(active, f"Which mission? {len(active)} are active:")

        exact = self.store.missions.get(ref)
        if exact is not None:
            return exact

        titled = named(low)
        if len(titled) == 1:
            return titled[0]
        if len(titled) > 1:
            # Same title twice: prefer a live one, else refuse.
            live = [m for m in titled if m.status in self.ACTIVE]
            if len(live) == 1:
                return live[0]
            raise self._refuse(titled, "Several missions have that name:")

        if _ID_FRAGMENT_RE.match(low):
            prefix = [m for m in all_missions() if m.id.startswith(low)]
            if len(prefix) == 1:
                return prefix[0]
            if len(prefix) > 1:
                raise self._refuse(prefix, "That id prefix matches several missions:")

        # Fuzzy: scoped to ACTIVE, so "the payment one" means live work.
        wanted = _words(low) - _NOISE
        if wanted:
            hits = [m for m in active if wanted & _words(m.title)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise self._refuse(hits, f"{len(hits)} active missions match that:")
        elif len(active) == 1:
            # An all-noise ref ("the task", "the work one") names nothing, but
            # it is still a reference to *a* mission — the same thing "it"
            # means. Refusing here was safe but needlessly obtuse. Ambiguity
            # still refuses below, exactly as the deictic branch does.
            return active[0]

        if not all_missions():
            raise ValueError("There are no missions yet.")
        if active:
            raise self._refuse(active,
                               f"I could not match '{clip_speech(ref, TITLE_SPEECH_MAX)}'. "
                               "Active missions:")
        raise ValueError(f"I could not match '{clip_speech(ref, TITLE_SPEECH_MAX)}', "
                         "and nothing is active right now.")

    def speech_list(self, status: str | None = None, limit: int = MISSION_LIST_MAX) -> list[dict]:
        """Missions shaped for speaking, one row each. The list counterpart of
        `speech_detail`, and the reason tools.py no longer reaches into the
        store: the same clipping rules live in one place, applied by the layer
        that owns missions.

        `status` filters; omitting it means live work (`active()`), which is
        what the voice model means by "what's running".

        The per-mission session query is deliberate: `sessions_mission` is an
        index, so `limit` indexed lookups beat one unbounded scan of the whole
        sessions table (which is what grouping a single `sessions.list()` in
        Python would cost).
        """
        missions = (self.list(status=status) if status else self.active())[:limit]
        projects = {p.id: p.name for p in self.store.projects.list()}
        out = []
        for m in missions:
            sessions = self.store.sessions.list(mission_id=m.id)
            out.append({"id": m.id, "title": clip_speech(m.title, TITLE_SPEECH_MAX),
                        "goal": clip_speech(m.goal, GOAL_SPEECH_MAX) or None,
                        "status": m.status, "project": projects.get(m.project_id),
                        "agents": sorted({s.agent_id for s in sessions}),
                        "sessions": [clip_speech(s.name, SESSION_NAME_SPEECH_MAX)
                                     for s in sessions if s.name][:SESSIONS_SPEECH_MAX]})
        return out

    def speech_detail(self, mission_id: str) -> dict:
        """A mission shaped for speaking, not the full detail() dump."""
        m = self.get(mission_id)
        project = self.store.projects.get(m.project_id)
        sessions = self.store.sessions.list(mission_id=m.id)
        pending = None
        for s in sessions:
            a = self.store.approvals.pending_for_session(s.id)
            if a is not None:
                pending = clip_speech(a.description or a.tool_name, APPROVAL_SPEECH_MAX)
                break
        events = self.store.events.list(mission_id=m.id, limit=1)
        return {"mission_id": m.id, "title": clip_speech(m.title, TITLE_SPEECH_MAX),
                "goal": clip_speech(m.goal, GOAL_SPEECH_MAX) or None, "status": m.status,
                "project": project.name if project else None,
                "agents": sorted({s.agent_id for s in sessions}),
                "sessions": [{"name": clip_speech(s.name, SESSION_NAME_SPEECH_MAX) or None,
                              "status": s.status} for s in sessions[:SESSIONS_SPEECH_MAX]],
                "pending_approval": pending,
                "last_event": events[-1].type if events else None}

    # --- lifecycle ------------------------------------------------------------

    def _require_edge(self, m: Mission, to: str) -> None:
        """Reject an illegal transition BEFORE any side effect. `transition()`
        raises on its own, but only after the caller has already interrupted or
        stopped the mission's agents — and a `queued` mission is ACTIVE (so
        resolvable by voice) while `queued → paused` is not in TRANSITIONS, so
        "pause that" would kill the agents and then fail."""
        if to != m.status and to not in TRANSITIONS.get(m.status, frozenset()):
            raise InvalidTransition(f"mission {m.id[:8]}: {m.status} → {to} is not allowed")

    async def pause(self, mission_id: str, by: str) -> Mission:
        m = self.get(mission_id)
        self._require_edge(m, "paused")
        live = self.store.sessions.list(mission_id=m.id, live_only=True)
        if live and self.interrupt_sessions is not None:
            # Interrupt BEFORE transitioning so a stop-triggered status change
            # cannot race the pause (same ordering cancel() already uses).
            await self.interrupt_sessions(live)
        self.set_status(m, "paused", by)
        return m

    async def resume(self, mission_id: str, by: str) -> Mission:
        m = self.get(mission_id)
        self.set_status(m, "running", by)
        return m

    async def cancel(self, mission_id: str, by: str) -> Mission:
        m = self.get(mission_id)
        live = [s for s in self.store.sessions.list(mission_id=m.id, live_only=True)]
        if live and self.stop_sessions is not None:
            await self.stop_sessions(live)
        self.set_status(m, "cancelled", by)
        return m
