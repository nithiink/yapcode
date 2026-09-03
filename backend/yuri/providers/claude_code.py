"""Claude Code as an AgentProvider. Wraps the two existing runners — the
interactive CLI in tmux ("cli") and the Agent SDK ("sdk") — behind one
provider id. This adapter is the ONLY place that knows both runners exist;
above it, Yuri sees "claude-code" sessions with a per-handle backend tag.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Callable

from claude_runner import ClaudeRunner
from .base import (AgentCapabilities, AgentHealth, AgentProvider, Observer, ProjectContext,
                   ProviderEvent, SessionOptions)

log = logging.getLogger("yuri.providers.claude")

HEALTH_TTL_S = 30.0
BACKENDS = ("cli", "sdk")


def default_runner_factory(backend: str) -> ClaudeRunner:
    # Imported lazily: tmux_runner/claude_runner pull in the SDK and tmux
    # constants, which tests avoid by injecting a factory.
    if backend == "sdk":
        from claude_runner import SDKClaudeRunner
        return SDKClaudeRunner()
    from tmux_runner import TmuxClaudeRunner
    return TmuxClaudeRunner()


async def _version(cmd: list[str], timeout: float = 5.0) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode != 0:
            return None
        return out.decode(errors="replace").strip().splitlines()[0][:80] if out else ""
    except Exception:
        return None


class ClaudeCodeProvider(AgentProvider):
    id = "claude-code"
    name = "Claude Code"

    def __init__(self, runner_factory: Callable[[str], ClaudeRunner] | None = None):
        self._factory = runner_factory or default_runner_factory
        self._runners: dict[str, ClaudeRunner] = {}
        self._owner: dict[str, str] = {}      # native handle -> backend
        self._observer: Observer | None = None
        self._health: tuple[float, AgentHealth] | None = None

    # --- runner plumbing ----------------------------------------------------

    def runner(self, backend: str = "cli") -> ClaudeRunner:
        backend = (backend or "cli").lower()
        if backend not in BACKENDS:
            backend = "cli"
        r = self._runners.get(backend)
        if r is None:
            r = self._factory(backend)
            r.on_event = functools.partial(self._on_runner_event, backend)
            self._runners[backend] = r
        return r

    def register(self, handle: str, backend: str) -> None:
        self._owner[handle] = (backend or "cli").lower()

    def backend_of(self, handle: str) -> str | None:
        b = self._owner.get(handle)
        if b is not None:
            return b
        for backend, r in self._runners.items():
            if any(s["handle"] == handle for s in r.list()):
                self._owner[handle] = backend
                return backend
        return None

    def runner_for(self, handle: str) -> ClaudeRunner:
        b = self.backend_of(handle)
        if b is None:
            raise KeyError(f"unknown session: {handle}")
        return self.runner(b)

    def _cli_only(self, handle: str, what: str) -> ClaudeRunner:
        if self.backend_of(handle) != "cli":
            raise NotImplementedError(f"{what} controls the interactive CLI; this session uses the SDK backend.")
        return self.runner("cli")

    # --- contract -----------------------------------------------------------

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(interactive_terminal=True, slash_commands=True, send_keys=True,
                                 permission_modes=("default", "acceptEdits", "plan", "auto"),
                                 supports_interrupt=True, supports_rehydrate=True,
                                 supports_resume=True, supports_events=True, cost_tracking=True)

    async def health(self) -> AgentHealth:
        now = time.monotonic()
        if self._health and now - self._health[0] < HEALTH_TTL_S:
            return self._health[1]
        claude_v, tmux_v = await asyncio.gather(_version(["claude", "--version"]),
                                                _version(["tmux", "-V"]))
        online = claude_v is not None
        parts = [f"claude: {claude_v or 'missing'}", f"tmux: {tmux_v or 'missing (cli backend unavailable)'}"]
        h = AgentHealth(online=online, version=claude_v or None, detail=" · ".join(parts))
        self._health = (now, h)
        return h

    async def create_session(self, project: ProjectContext, opts: SessionOptions) -> str:
        backend = opts.backend if opts.backend in BACKENDS else "cli"
        handle = await self.runner(backend).start(project.root_path, opts.model, opts.mode)
        self.register(handle, backend)
        return handle

    async def resume(self, native_session_id: str, project: ProjectContext,
                     opts: SessionOptions) -> str:
        handle = await self.runner("cli").resume(native_session_id, project.root_path,
                                                 opts.model, opts.mode, opts.name)
        self.register(handle, "cli")
        return handle

    def send_message(self, handle: str, message: str) -> None:
        self.runner_for(handle).start_advance(handle, message)

    def answer(self, handle: str, choice: str) -> None:
        self.runner_for(handle).start_answer(handle, choice)

    def poll(self, handle: str) -> dict[str, Any]:
        return self.runner_for(handle).poll_status(handle)

    async def interrupt(self, handle: str) -> None:
        await self.runner_for(handle).interrupt(handle)

    async def stop(self, handle: str) -> None:
        await self.runner_for(handle).close(handle)
        self._owner.pop(handle, None)

    async def set_mode(self, handle: str, mode: str) -> str:
        return await self.runner_for(handle).set_mode(handle, mode)

    async def read(self, handle: str) -> str:
        return await self.runner_for(handle).read(handle)

    async def peek(self, handle: str, lines: int = 40) -> str | None:
        r = self.runner_for(handle)
        peek = getattr(r, "peek", None)
        return await peek(handle, lines) if peek else None

    async def send_keys(self, handle: str, items: list[dict]) -> dict[str, Any]:
        return await self._cli_only(handle, "send_keys").send_keys(handle, items)

    def run_slash(self, handle: str, text: str) -> None:
        r = self._cli_only(handle, "slash commands")
        start = getattr(r, "start_builtin_slash", None)
        if start:
            start(handle, text)
        else:
            r.start_advance(handle, text)

    def list_native(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for backend, r in self._runners.items():
            for s in r.list():
                out.append({**s, "backend": backend})
        return out

    def resume_command(self, handle: str) -> str | None:
        row = {s["handle"]: s for s in self.list_native()}.get(handle)
        if not row or not row.get("session_id"):
            return None
        return f"cd {row['cwd']} && claude --resume {row['session_id']}"

    def native_pane(self, handle: str) -> str | None:
        if self.backend_of(handle) != "cli":
            return None
        pane_for = getattr(self.runner("cli"), "pane_for", None)
        return pane_for(handle) if pane_for else None

    def persist_name(self, handle: str, name: str) -> None:
        persist = getattr(self.runner_for(handle), "persist_name", None)
        if persist:
            try:
                persist(handle, name)
            except Exception:
                log.debug("persist_name failed for %s", handle, exc_info=True)

    def set_observer(self, cb: Observer | None) -> None:
        self._observer = cb

    async def rehydrate(self, **_ignored) -> list[dict[str, Any]]:
        # `known` is deliberately unused: the tmux runner enumerates the panes
        # that actually survived and reads each one's meta file, which is a
        # stronger claim than anything Yuri's own rows could assert.
        r = self.runner("cli")
        rehydrate = getattr(r, "rehydrate", None)
        if rehydrate is None:
            return []
        restored = await rehydrate()
        for s in restored:
            self.register(s["handle"], "cli")
        return restored

    async def shutdown(self) -> None:
        for r in self._runners.values():
            await r.shutdown()
        self._runners.clear()
        self._owner.clear()

    # --- runner events -> ProviderEvent ---------------------------------------

    def _on_runner_event(self, backend: str, handle: str, kind: str, raw: dict[str, Any]) -> None:
        cb = self._observer
        if cb is None:
            return
        ev = self._map(kind, raw)
        if ev is None:
            return
        try:
            cb(handle, ev)
        except Exception:
            log.exception("provider observer failed")

    @staticmethod
    def _map(kind: str, raw: dict[str, Any]) -> ProviderEvent | None:
        if kind == "tool":
            return ProviderEvent("tool_started", {"tool_name": raw.get("tool_name", ""),
                                                  "tool_input": raw.get("tool_input") or {}})
        if kind == "needs_permission":
            return ProviderEvent("needs_permission", {
                "request_id": raw.get("request_id"), "tool_name": raw.get("tool_name", ""),
                "tool_input": raw.get("tool_input") or {}, "text": raw.get("text", ""),
                "options": ["allow", "deny"]})
        if kind == "needs_choice":
            return ProviderEvent("needs_choice", {
                "request_id": raw.get("request_id"), "tool_name": raw.get("tool_name", ""),
                "text": raw.get("text", ""), "options": list(raw.get("options") or []),
                "multi_select": bool(raw.get("multi_select"))})
        if kind == "turn_complete":
            return ProviderEvent("turn_completed", {
                "assistant_text": (raw.get("assistant_text") or "")[:2000],
                "tools_used": list(raw.get("tools_used") or [])})
        if kind == "cost":
            return ProviderEvent("cost_updated", {
                "model": raw.get("model"), "input_tokens": raw.get("input_tokens"),
                "output_tokens": raw.get("output_tokens"), "cost_usd": raw.get("cost_usd")})
        if kind == "error":
            return ProviderEvent("error", {"message": str(raw.get("message") or "unknown error")})
        return None
