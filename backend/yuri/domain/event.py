"""YuriEvent — the normalized event every subsystem emits (spec §11).

`severity` IS load-bearing: narration/policy.py's `speaks()` reads it to decide
what quiet mode still says out loud (warnings and errors get through).

`speakable` is NOT read by the narration layer. It survived as the design's
first sketch of "would we ever say this", and the DEFAULTS below are what
narration/policy.py's ownership table was derived FROM — but the table is the
authority now, and it is finer-grained (it also names the carrier, and the
mode). Treat `speakable` as a persisted hint for consumers of /yuri/events, not
as a narration switch."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow


class EventType:
    MISSION_CREATED = "mission.created"
    MISSION_STATUS_CHANGED = "mission.status_changed"
    MISSION_DELETED = "mission.deleted"
    SESSION_CREATED = "session.created"
    SESSION_MESSAGE_SENT = "session.message_sent"
    SESSION_TURN_COMPLETED = "session.turn_completed"
    SESSION_QUESTION = "session.question"
    SESSION_INTERRUPTED = "session.interrupted"
    SESSION_STOPPED = "session.stopped"
    SESSION_LOST = "session.lost"
    TOOL_STARTED = "tool.started"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    COST_UPDATED = "cost.updated"
    AGENT_ERROR = "agent.error"
    PROJECT_REGISTERED = "project.registered"
    MEMORY_REMEMBERED = "memory.remembered"


# type -> (severity, speakable)   (spec §6.1)
DEFAULTS: dict[str, tuple[str, bool]] = {
    EventType.TOOL_STARTED: ("debug", False),
    EventType.SESSION_MESSAGE_SENT: ("debug", False),
    EventType.COST_UPDATED: ("debug", False),
    EventType.SESSION_CREATED: ("info", False),
    EventType.MISSION_CREATED: ("info", True),
    EventType.MISSION_STATUS_CHANGED: ("info", True),
    EventType.MISSION_DELETED: ("info", True),
    EventType.SESSION_TURN_COMPLETED: ("info", True),
    EventType.SESSION_QUESTION: ("notice", True),
    EventType.APPROVAL_REQUESTED: ("notice", True),
    EventType.APPROVAL_RESOLVED: ("info", False),
    EventType.SESSION_INTERRUPTED: ("info", False),
    EventType.SESSION_STOPPED: ("info", False),
    EventType.SESSION_LOST: ("warning", True),
    EventType.AGENT_ERROR: ("error", True),
    EventType.PROJECT_REGISTERED: ("info", False),
    EventType.MEMORY_REMEMBERED: ("info", False),
}


@dataclass
class YuriEvent:
    type: str
    id: str = field(default_factory=new_id)
    ts: str = field(default_factory=utcnow)
    mission_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    project_id: str | None = None
    severity: str = "info"
    speakable: bool = False
    payload: dict = field(default_factory=dict)

    @classmethod
    def make(cls, type: str, **fields) -> "YuriEvent":
        sev, speak = DEFAULTS.get(type, ("info", False))
        fields.setdefault("severity", sev)
        fields.setdefault("speakable", speak)
        return cls(type=type, **fields)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "YuriEvent":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
