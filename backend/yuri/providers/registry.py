"""Which agent providers exist and whether they're actually reachable (spec §7).
Configured by YURI_AGENTS (comma list; default "claude-code"). A provider being
configured does not make it "online" — health() decides that."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

from .base import AgentHealth, AgentProvider

log = logging.getLogger("yuri.registry")

KNOWN = ("claude-code", "opencode")


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


def _opencode_provider() -> AgentProvider:
    """The OpenCode provider, wired from config and holding nothing yet.

    Imported inside this branch (as the Claude one is) so a registry without
    OpenCode never imports the httpx-dependent code paths it does not need —
    an OpenCode-less deployment pays nothing for it.

    **Nothing here touches the network or the filesystem beyond reading
    config.** `OpenCodeServer` acquires lazily on the first call that needs it,
    so a dead OpenCode cannot hang Yuri's startup; `build_container` constructs
    every configured provider, so this has to stay free.

    `env` is the design spec §4 requirement — the child inherits no Yuri
    secrets — and this is the layer that knows which names those are; see
    `config.opencode_child_env`. `cwd` is the first allowed project root, also
    §4. The password reaches the server, never a log line.
    """
    import config
    from .opencode.provider import OpenCodeProvider
    from .opencode.server import OpenCodeServer
    roots = config.allowed_project_roots()
    server = OpenCodeServer(
        config.OPENCODE_URL,
        spawn=config.OPENCODE_SPAWN,
        binary=config.OPENCODE_BIN,
        # "" is not a password: None means "send no auth header".
        password=config.OPENCODE_SERVER_PASSWORD or None,
        cwd=roots[0] if roots else None,
        # Its stdout/stderr belong in a log, not in the terminal the voice UI
        # owns. Home.ensure() makes no logs/ dir; _spawn() creates it.
        log_path=os.path.join(config.YURI_HOME, "logs", "opencode.log"),
        env=config.opencode_child_env(),
    )
    return OpenCodeProvider(server, default_model=config.OPENCODE_MODEL or None)


def build_registry(agents_csv: str, claude_factory: Callable | None = None) -> AgentRegistry:
    from .claude_code import ClaudeCodeProvider
    reg = AgentRegistry()
    wanted = [a.strip() for a in (agents_csv or "").split(",") if a.strip()] or ["claude-code"]
    for a in wanted:
        if a == "claude-code":
            reg.register(ClaudeCodeProvider(runner_factory=claude_factory))
        elif a == "opencode":
            reg.register(_opencode_provider())
        else:
            log.warning("YURI_AGENTS: unknown agent %r skipped (known: %s)", a, KNOWN)
    return reg
