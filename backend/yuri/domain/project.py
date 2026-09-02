"""A registered working directory (spec §9). Roots are validated by the service
layer against the sandbox; this is data only."""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", (name or "").lower()).strip("-")
    return s[:64] or "project"


@dataclass
class Project:
    slug: str
    name: str
    root_path: str
    id: str = field(default_factory=new_id)
    kind: str = "user"                 # "user" | "home"
    default_agent: str | None = None
    auto_approve_edits: bool = False
    repo_url: str | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    @staticmethod
    def for_path(root_path: str, name: str | None = None, **kw) -> "Project":
        name = name or os.path.basename(os.path.normpath(root_path)) or "project"
        return Project(slug=slugify(name), name=name, root_path=root_path, **kw)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
