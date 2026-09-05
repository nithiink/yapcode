"""Task — one unit of work in a mission's workflow (spec §7.2).

The same shape as domain/mission.py: the table below is the single source of
truth and every service enforces it rather than re-deriving it. mission.py's
docstring says "there is no orchestrator"; Phase 7 adds one, and this is the
machine it drives.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow
from .specialist import ROLES

TASK_KINDS: tuple[str, ...] = ("agent_task", "approval", "verification", "human_input")

# Done for good. `blocked` deliberately is NOT here: it means "attempts
# exhausted, a human is needed", and the human retrying it is the entire
# reason the state exists.
TERMINAL_TASK: frozenset[str] = frozenset({"completed", "skipped", "cancelled"})

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    # `ready` is the ONLY gate that checks dependencies, so nothing may reach
    # `dispatched` without passing through it. Allowing pending → dispatched
    # is exactly how a task would start before its dependency finished.
    "pending": frozenset({"ready", "skipped", "cancelled"}),
    "ready": frozenset({"dispatched", "skipped", "cancelled"}),
    # `dispatched` → `ready` is not a mistake: it is reconciliation after a
    # crash, where `dispatched` with no session means the work never actually
    # started and must be re-dispatchable (spec §13).
    "dispatched": frozenset({"running", "ready", "failed", "cancelled"}),
    "running": frozenset({"verifying", "waiting_approval", "failed", "cancelled"}),
    "waiting_approval": frozenset({"running", "failed", "cancelled"}),
    "verifying": frozenset({"completed", "failed", "cancelled"}),
    "failed": frozenset({"ready", "blocked", "cancelled"}),
    "blocked": frozenset({"ready", "cancelled"}),
    "completed": frozenset(), "skipped": frozenset(), "cancelled": frozenset(),
}


class InvalidTaskTransition(ValueError):
    pass


@dataclass
class Task:
    workflow_id: str
    ordinal: int                       # authoring order, NOT execution order
    title: str
    id: str = field(default_factory=new_id)
    kind: str = "agent_task"
    role: str | None = None
    specialist_id: str | None = None   # pinned by the user, or chosen at dispatch
    session_id: str | None = None
    status: str = "pending"
    instruction: str = ""
    requires: tuple[str, ...] = ()     # task capabilities
    verification: tuple[str, ...] = ()
    read_only: bool = False            # eligible for parallel execution
    attempts: int = 0
    max_attempts: int = 2
    result: dict = field(default_factory=dict)
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.kind not in TASK_KINDS:
            raise ValueError(f"unknown task kind: {self.kind!r}; expected one of {list(TASK_KINDS)}")
        if self.role is not None and self.role not in ROLES:
            raise ValueError(f"unknown role: {self.role!r}; expected one of {list(ROLES)}")
        # Tuples, never sets — see the note in specialist.py's __post_init__.
        self.requires = tuple(self.requires)
        self.verification = tuple(self.verification)
        # An agent_task with neither a role nor a specialist cannot be
        # dispatched to anything. Rejecting it here means a bad workflow is
        # refused when it is authored; without this the failure surfaces at
        # run time as "no candidates", long after the user was told it was fine.
        if self.kind == "agent_task" and not (self.role or self.specialist_id):
            raise ValueError("an agent_task needs a role or a specialist_id")

    def transition(self, to: str) -> bool:
        """Move to `to`. Returns False (no-op) for same-state; raises
        InvalidTaskTransition for anything the table forbids."""
        if to == self.status:
            return False
        allowed = TASK_TRANSITIONS.get(self.status)
        if allowed is None or to not in allowed:
            raise InvalidTaskTransition(
                f"task {self.id[:8]}: {self.status} → {to} is not allowed")
        self.status = to
        self.updated_at = utcnow()
        if to == "running" and self.started_at is None:
            self.started_at = self.updated_at
        # `blocked` sets ended_at too: the work stopped, even though a human
        # can restart it. The timeline needs a duration either way.
        if to in TERMINAL_TASK or to == "blocked":
            self.ended_at = self.updated_at
        return True

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK

    @property
    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
