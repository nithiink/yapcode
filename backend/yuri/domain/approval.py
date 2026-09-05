"""Approval — a first-class record of an agent asking permission (spec §20)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow


@dataclass
class Approval:
    session_id: str
    agent_id: str
    action: str
    tool_name: str
    request_id: str
    id: str = field(default_factory=new_id)
    mission_id: str | None = None
    tool_input: dict = field(default_factory=dict)
    risk: str = "confirm"              # safe | confirm | dangerous
    description: str = ""
    status: str = "pending"            # pending | allowed | denied | expired | superseded
    requested_at: str = field(default_factory=utcnow)
    resolved_at: str | None = None
    resolved_by: str | None = None     # voice | ui | api | mode_switch

    def resolve(self, decision: str, by: str) -> None:
        if decision not in ("allowed", "denied", "expired", "superseded"):
            raise ValueError(f"bad decision {decision!r}")
        self.status = decision
        self.resolved_at = utcnow()
        self.resolved_by = by

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Approval":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
