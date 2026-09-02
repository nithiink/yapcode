"""Request bodies for the /yuri routes. Responses are the domain dataclasses'
to_dict() output — one shape everywhere (UI, CLI, tests)."""
from __future__ import annotations

from pydantic import BaseModel


class ProjectCreate(BaseModel):
    path: str
    name: str | None = None
    default_agent: str | None = None


class NarrationUpdate(BaseModel):
    mode: str
