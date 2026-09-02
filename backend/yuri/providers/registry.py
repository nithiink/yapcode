"""Which agent providers exist and whether they're actually reachable (spec §7).
Configured by YURI_AGENTS (comma list; default "claude-code"). A provider being
configured does not make it "online" — health() decides that."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .base import AgentHealth, AgentProvider

log = logging.getLogger("yuri.registry")

KNOWN = ("claude-code",)


class AgentRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, AgentProvider] = {}

    def register(self, provider: AgentProvider) -> None:
        self._providers[provider.id] = provider

    def get(self, agent_id: str) -> AgentProvider:
        p = self._providers.get(agent_id)
        if p is None:
            raise KeyError(f"unknown agent: {agent_id!r}. Known: {self.ids()}")
        return p

    def all(self) -> list[AgentProvider]:
        return list(self._providers.values())

    def ids(self) -> list[str]:
        return list(self._providers)

    async def health_all(self) -> dict[str, AgentHealth]:
        ps = self.all()
        results = await asyncio.gather(*(p.health() for p in ps), return_exceptions=True)
        out: dict[str, AgentHealth] = {}
        for p, r in zip(ps, results):
            out[p.id] = r if isinstance(r, AgentHealth) else AgentHealth(
                online=False, version=None, detail=f"health check failed: {r}")
        return out

    async def shutdown(self) -> None:
        for p in self.all():
            try:
                await p.shutdown()
            except Exception:
                log.exception("provider %s shutdown failed", p.id)


def build_registry(agents_csv: str, claude_factory: Callable | None = None) -> AgentRegistry:
    from .claude_code import ClaudeCodeProvider
    reg = AgentRegistry()
    wanted = [a.strip() for a in (agents_csv or "").split(",") if a.strip()] or ["claude-code"]
    for a in wanted:
        if a == "claude-code":
            reg.register(ClaudeCodeProvider(runner_factory=claude_factory))
        else:
            log.warning("YURI_AGENTS: unknown agent %r skipped (known: %s)", a, KNOWN)
    return reg
