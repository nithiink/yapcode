"""AgentSession — Yuri's record of one agent runtime (spec §10). `id` is
Yuri's; `native_session_id` is the provider's handle and is never the primary key."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow

LIVE_STATUSES = frozenset({"starting", "running", "needs_permission", "needs_choice", "idle"})


@dataclass
class AgentSession:
    project_id: str
    agent_id: str
    native_session_id: str
    backend: str
    working_directory: str
    id: str = field(default_factory=new_id)
    mission_id: str | None = None
    status: str = "starting"           # LIVE_STATUSES | stopped | lost
    name: str | None = None
    mode: str = "default"
    model: str | None = None
    started_at: str = field(default_factory=utcnow)
    last_activity_at: str = field(default_factory=utcnow)
    runtime_metadata: dict = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    def touch(self) -> None:
        self.last_activity_at = utcnow()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentSession":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
