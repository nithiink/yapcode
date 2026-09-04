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


class McpServerBody(BaseModel):
    """A candidate MCP server, for POST /yuri/mcp and POST /yuri/mcp/test.

    `tier` has no default here for the same reason it has none in the config
    file: a default would be a security decision made by whoever left the
    field alone. An omitted tier is a 400 that names the problem.
    """
    name: str
    transport: str = "stdio"
    tier: str | None = None
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str | None = None
    enabled: bool = True


class McpEnabled(BaseModel):
    enabled: bool
