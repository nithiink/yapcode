"""Repository interfaces (spec §4.4). Kept as ABCs so Postgres can replace
SQLite later without touching services.

Every method is sync and is called INLINE, on whatever thread the caller is
already on — including from async service methods and from the tmux runner's
sync observer callbacks. That is a recorded decision, not an oversight: the
store is a local SQLite file in WAL mode, so a read or a write is sub-
millisecond, and hopping to a thread per call would cost more than it saves
while making the services' ordering harder to reason about. The one exception
is the event writer (`EventBus._write_loop`), which persists via
`asyncio.to_thread` because it is a background fan-out, not a caller waiting on
a result. A future Postgres implementation, whose calls cross a network, would
have to revisit this.

`SqliteStore` keeps one connection per thread (threading.local), so inline
calls from threadpool workers are safe."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from yuri.domain.approval import Approval
from yuri.domain.event import YuriEvent
from yuri.domain.mission import Mission, MissionStep
from yuri.domain.project import Project
from yuri.domain.session import AgentSession


class PendingApprovalExists(ValueError):
    """A session already has a pending approval (one decision per prompt)."""


class LiveSessionExists(ValueError):
    """A live row already exists for this native handle (one live row each).

    Named, not a bare IntegrityError, so the caller can treat it as what it
    actually means -- "this handle is already adopted" -- instead of a 500.
    """


class ProjectRepo(ABC):
    @abstractmethod
    def insert(self, p: Project) -> None: ...
    @abstractmethod
    def get(self, id: str) -> Project | None: ...
    @abstractmethod
    def get_by_slug(self, slug: str) -> Project | None: ...
    @abstractmethod
    def get_by_root(self, root_path: str) -> Project | None: ...
    @abstractmethod
    def list(self) -> list[Project]: ...
    @abstractmethod
    def update(self, p: Project) -> None: ...


class MissionRepo(ABC):
    @abstractmethod
    def insert(self, m: Mission) -> None: ...
    @abstractmethod
    def get(self, id: str) -> Mission | None: ...
    @abstractmethod
    def list(self, status: str | None = None, limit: int = 200) -> list[Mission]: ...
    @abstractmethod
    def update(self, m: Mission) -> None: ...
    @abstractmethod
    def insert_step(self, step: MissionStep) -> None: ...
    @abstractmethod
    def steps_for(self, mission_id: str) -> list[MissionStep]: ...
    @abstractmethod
    def update_step(self, step: MissionStep) -> None: ...
    @abstractmethod
    def delete(self, id: str) -> None:
        """Remove a mission and its steps. A missing id is a no-op.

        Deliberately does NOT touch the sessions that pointed at it, nor its
        events. Sessions are detached by SessionRepo.detach_mission (a session
        row records a real agent session, which happened whether or not the
        mission survives), and events are an append-only audit log -- deleting
        log entries to tidy up would be the wrong trade.
        """


class SessionRepo(ABC):
    @abstractmethod
    def detach_mission(self, mission_id: str) -> None:
        """Clear mission_id on every session that pointed at this mission.

        Not a delete: the row is the record of an agent session that really
        ran. Losing its mission should orphan the link, not the history.
        """
    @abstractmethod
    def insert(self, s: AgentSession) -> None: ...
    @abstractmethod
    def get(self, id: str) -> AgentSession | None: ...
    @abstractmethod
    def get_by_native(self, native_id: str) -> AgentSession | None: ...
    @abstractmethod
    def list(self, mission_id: str | None = None, live_only: bool = False) -> list[AgentSession]: ...
    @abstractmethod
    def update(self, s: AgentSession) -> None: ...


class ApprovalRepo(ABC):
    @abstractmethod
    def insert(self, a: Approval) -> None: ...
    @abstractmethod
    def get(self, id: str) -> Approval | None: ...
    @abstractmethod
    def get_by_request(self, request_id: str) -> Approval | None: ...
    @abstractmethod
    def pending_for_session(self, session_id: str) -> Approval | None: ...
    @abstractmethod
    def list(self, status: str | None = None, session_id: str | None = None,
             limit: int = 200) -> list[Approval]: ...
    @abstractmethod
    def update(self, a: Approval) -> None: ...


class EventRepo(ABC):
    @abstractmethod
    def insert(self, e: YuriEvent) -> None: ...
    @abstractmethod
    def list(self, mission_id: str | None = None, session_id: str | None = None,
             since: str | None = None, limit: int = 200) -> list[YuriEvent]: ...


class SettingsRepo(ABC):
    @abstractmethod
    def get(self, key: str, default: Any = None) -> Any: ...
    @abstractmethod
    def set(self, key: str, value: Any) -> None: ...


class Store(ABC):
    projects: ProjectRepo
    missions: MissionRepo
    sessions: SessionRepo
    approvals: ApprovalRepo
    events: EventRepo
    settings: SettingsRepo

    @abstractmethod
    def migrate(self) -> None: ...
    @abstractmethod
    def close(self) -> None: ...
