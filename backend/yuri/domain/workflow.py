"""Workflow — how a mission gets done (spec §7.1).

One live workflow per mission, enforced by a partial unique index in
migration 0003 rather than by convention — matching `approvals_one_pending`
and `sessions_one_live`, both of which exist because the invariant was
violated in practice before it was indexed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow

TERMINAL_WORKFLOW: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
# Mirrored by 0003's `workflows_one_live` partial index. sqlite cannot import a
# Python constant, so migrate() asserts the two still agree.
LIVE_WORKFLOW: tuple[str, ...] = ("draft", "running", "paused", "waiting_for_human")

WORKFLOW_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "waiting_for_human", "completed", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    # A bound (spec §12) or a deadlock lands here. It is deliberately NOT
    # `failed`: nothing is broken, a decision is needed — so `running` has to
    # be reachable back out of it, or hitting a retry limit would kill a
    # mission the user only needed to look at.
    "waiting_for_human": frozenset({"running", "cancelled", "failed"}),
    "completed": frozenset(), "failed": frozenset(), "cancelled": frozenset(),
}


class InvalidWorkflowTransition(ValueError):
    pass


@dataclass
class Workflow:
    mission_id: str
    id: str = field(default_factory=new_id)
    version: int = 1
    template: str | None = None
    status: str = "draft"
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def transition(self, to: str) -> bool:
        if to == self.status:
            return False
        allowed = WORKFLOW_TRANSITIONS.get(self.status)
        if allowed is None or to not in allowed:
            raise InvalidWorkflowTransition(
                f"workflow {self.id[:8]}: {self.status} → {to} is not allowed")
        self.status = to
        self.updated_at = utcnow()
        return True

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_WORKFLOW

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Workflow":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
