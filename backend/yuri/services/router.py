"""Which agent runs a mission's work.

Extracted from SessionService.start, where the choice was inlined. It exists so
the routing rules plan section 18 lists as future — task type, model
availability, cost, latency, capability matching, current workload — have one
home when there is a second agent to route to. Adding them now would be a
router with nothing to choose between.
"""
from __future__ import annotations

import logging

from yuri.domain.project import Project
from yuri.providers.base import AgentProvider
from yuri.providers.registry import AgentRegistry

log = logging.getLogger("yuri.router")


class AgentRouter:
    def __init__(self, registry: AgentRegistry, default_agent: str = "claude-code"):
        self.registry = registry
        self.default_agent = default_agent

    def select(self, project: Project, requested: str | None = None) -> AgentProvider:
        """Explicit request, then the project's default, then the global default.

        An unknown *requested* id raises KeyError naming what exists — tools.py
        turns that into a soft error the voice model recovers from. An unknown
        *project* default only warns and falls back: the user did not ask for it
        now, and a retired agent id in a stored row must not make that project's
        sessions unstartable.
        """
        wanted = (requested or "").strip()
        if wanted:
            return self.registry.get(wanted)     # raises KeyError naming known ids
        if project.default_agent:
            try:
                return self.registry.get(project.default_agent)
            except KeyError:
                log.warning("project %s prefers agent %r, which is not registered; "
                            "falling back to %s", project.slug, project.default_agent,
                            self.default_agent)
        return self.registry.get(self.default_agent)
