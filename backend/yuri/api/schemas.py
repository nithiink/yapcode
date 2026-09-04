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


class SpecialistBody(BaseModel):
    """Create or update a specialist. Every field optional on PUT; `name` and
    `role` are required on POST, enforced by RosterService rather than here so
    there is one validator."""
    name: str | None = None
    role: str | None = None
    provider_id: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    color: str | None = None
    permission_mode: str | None = None
    capabilities: list[str] | None = None
    tools: list[str] | None = None


class WorkflowBody(BaseModel):
    """Build a mission's task graph, from a template or from explicit tasks.

    Both may not be given: a graph that claims a template it did not come
    from would make the timeline lie about where the plan came from.
    """
    template: str | None = None
    goal: str | None = None
    tasks: list[dict] | None = None
    overrides: dict[str, dict] | None = None


class AssignBody(BaseModel):
    specialist_id: str
