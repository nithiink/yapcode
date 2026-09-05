"""Mission — the unit of work (spec §8). A deterministic state machine.

There is no orchestrator. Phase 4 ruled it out: missions are created implicitly
by SessionService.start/adopt and driven by the callers that already exist —
`_mission_to` (derived from session events), MissionService.pause/resume/cancel
(voice tools and the HTTP API), and nothing else. The services enforce the
table; this module only defines it."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from .ids import new_id, utcnow


class MissionStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({"completed", "failed", "cancelled"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"queued", "running", "cancelled"}),
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"waiting_for_approval", "paused", "completed", "failed", "cancelled"}),
    "waiting_for_approval": frozenset({"running", "paused", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "completed": frozenset(), "failed": frozenset(), "cancelled": frozenset(),
}


class InvalidTransition(ValueError):
    pass


@dataclass
class Mission:
    title: str
    project_id: str
    id: str = field(default_factory=new_id)
    goal: str | None = None
    status: str = "running"
    priority: int = 0
    current_step: str | None = None
    created_by: str = "voice"          # voice | ui | api | handoff | system
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        # The store's `status` column is plain TEXT (Task 11's contract), not
        # an Enum. MissionStatus subclasses str only so `MissionStatus.X ==
        # "x"` reads naturally — it does NOT make `str(MissionStatus.X)`
        # yield "x" (it yields "MissionStatus.X" on this Python), so a caller
        # who constructs `Mission(status=MissionStatus.DRAFT, ...)` would
        # otherwise leave an Enum instance sitting in `self.status`, which
        # survives `asdict()` untouched. Coerce to the plain `.value` here so
        # `type(status) is str` holds no matter how the Mission was built.
        if isinstance(self.status, MissionStatus):
            self.status = self.status.value

    def transition(self, to: str) -> bool:
        """Move to `to`. Returns False (no-op) for same-state; raises
        InvalidTransition for anything the table forbids."""
        if isinstance(to, MissionStatus):
            to = to.value
        if to == self.status:
            return False
        allowed = TRANSITIONS.get(self.status)
        if allowed is None or to not in allowed:
            raise InvalidTransition(f"mission {self.id[:8]}: {self.status} → {to} is not allowed")
        self.status = to
        self.updated_at = utcnow()
        return True

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Mission":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class MissionStep:
    mission_id: str
    ordinal: int
    title: str
    id: str = field(default_factory=new_id)
    agent_id: str | None = None
    status: str = "pending"            # pending | running | done | failed | skipped
    session_id: str | None = None
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MissionStep":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
