"""Missions (spec §5.3). In this phase missions are created implicitly by
SessionService.start; explicit start/routing arrives with the orchestrator."""
from __future__ import annotations

from typing import Awaitable, Callable

from yuri.domain.event import EventType, YuriEvent
from yuri.domain.mission import Mission, MissionStep
from yuri.domain.project import Project
from yuri.domain.session import AgentSession
from yuri.events.bus import EventBus
from yuri.services.journal import Journal
from yuri.store.base import Store

GOAL_MAX = 500


class MissionService:
    def __init__(self, store: Store, bus: EventBus, journal: Journal):
        self.store = store
        self.bus = bus
        self.journal = journal
        # Injected by the container (SessionService.stop_many) to avoid a cycle.
        self.stop_sessions: Callable[[list[AgentSession]], Awaitable[None]] | None = None

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

    def set_status(self, mission: Mission, to: str, by: str, reason: str | None = None) -> bool:
        frm = mission.status
        if not mission.transition(to):      # raises InvalidTransition on bad edges
            return False
        self.store.missions.update(mission)
        self.bus.publish(YuriEvent.make(EventType.MISSION_STATUS_CHANGED, mission_id=mission.id,
                                        project_id=mission.project_id,
                                        payload={"from": frm, "to": to, "by": by, "reason": reason,
                                                 "title": mission.title}))
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

    async def pause(self, mission_id: str, by: str) -> Mission:
        m = self.get(mission_id)
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
