"""Artifact — something a task produced, and the next task consumes (spec §9).

Handoffs are built from these, never from a transcript dump: spec §7.10 is
explicit that an agent must not automatically receive every other agent's
history. An artifact is the deliberate, scoped alternative.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow

ARTIFACT_KINDS: tuple[str, ...] = ("finding", "patch", "test_report", "review",
                                   "summary", "file_list")


@dataclass
class Artifact:
    mission_id: str
    kind: str
    title: str
    body: str
    id: str = field(default_factory=new_id)
    task_id: str | None = None
    created_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.kind not in ARTIFACT_KINDS:
            raise ValueError(
                f"unknown artifact kind: {self.kind!r}; expected one of {list(ARTIFACT_KINDS)}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Artifact":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
