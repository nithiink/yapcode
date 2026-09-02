"""Deterministic in-memory AgentProvider for tests (spec §45). Records every
call, lets tests script poll results and fire observer events."""
from __future__ import annotations

from typing import Any

from .base import (AgentCapabilities, AgentHealth, AgentProvider, Observer, ProjectContext,
                   ProviderEvent, SessionOptions)


class FakeAgentProvider(AgentProvider):
    id = "fake"
    name = "Fake Agent"

    def __init__(self, *, online: bool = True, supports_terminal: bool = True,
                 supports_events: bool = True):
        self.online = online
        self.supports_terminal = supports_terminal
        # Default True (an observer-streaming provider). Set False to test
        # the other half of SessionService's single-emitter rule: the poll
        # path emits turn/question/error itself for a provider that does
        # not stream them.
        self.supports_events = supports_events
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple] = []
        self._scripted: dict[str, list[dict[str, Any]]] = {}
        self._observer: Observer | None = None
        self._n = 0

    # --- test controls ----------------------------------------------------
    def script(self, handle: str, result: dict[str, Any]) -> None:
        self._scripted.setdefault(handle, []).append(result)

    def emit(self, handle: str, ev: ProviderEvent) -> None:
        if self._observer:
            self._observer(handle, ev)

    # --- contract ---------------------------------------------------------
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(interactive_terminal=self.supports_terminal,
                                 slash_commands=self.supports_terminal,
                                 send_keys=self.supports_terminal,
                                 permission_modes=("default", "acceptEdits", "plan", "auto"),
                                 supports_interrupt=True, supports_rehydrate=True,
                                 supports_resume=True,
                                 supports_events=self.supports_events,
                                 cost_tracking=True)

    async def health(self) -> AgentHealth:
        return AgentHealth(online=self.online, version="fake-1", detail="ok" if self.online else "down")

    async def create_session(self, project: ProjectContext, opts: SessionOptions) -> str:
        self._n += 1
        h = f"fake-{self._n}"
        self.sessions[h] = {"handle": h, "session_id": h, "cwd": project.root_path,
                            "model": opts.model or "fake-model", "mode": opts.mode,
                            "status": "idle", "cost_usd": 0.0, "queued": 0,
                            "backend": opts.backend}
        self.calls.append(("create_session", h, project.root_path, opts))
        return h

    async def resume(self, native_session_id: str, project: ProjectContext,
                     opts: SessionOptions) -> str:
        self.sessions[native_session_id] = {"handle": native_session_id,
                                            "session_id": native_session_id,
                                            "cwd": project.root_path, "model": "fake-model",
                                            "mode": opts.mode, "status": "idle",
                                            "cost_usd": 0.0, "queued": 0, "backend": opts.backend}
        self.calls.append(("resume", native_session_id))
        return native_session_id

    def send_message(self, handle: str, message: str) -> None:
        self._get(handle)["status"] = "working"
        self.calls.append(("send_message", handle, message))

    def answer(self, handle: str, choice: str) -> None:
        self._get(handle)["status"] = "working"
        self.calls.append(("answer", handle, choice))

    def poll(self, handle: str) -> dict[str, Any]:
        self._get(handle)
        q = self._scripted.get(handle) or []
        if q:
            res = q.pop(0)
            self.sessions[handle]["status"] = res.get("status", "idle")
            return {**res, "session_id": handle}
        return {"status": self.sessions[handle]["status"] if self.sessions[handle]["status"] == "working" else "idle",
                "session_id": handle}

    async def interrupt(self, handle: str) -> None:
        self._get(handle)["status"] = "idle"
        self.calls.append(("interrupt", handle))

    async def stop(self, handle: str) -> None:
        self._get(handle)
        self.sessions.pop(handle)
        self.calls.append(("stop", handle))

    async def set_mode(self, handle: str, mode: str) -> str:
        self._get(handle)["mode"] = mode
        self.calls.append(("set_mode", handle, mode))
        return mode

    async def read(self, handle: str) -> str:
        self._get(handle)
        return "fake assistant text"

    async def peek(self, handle: str, lines: int = 40) -> str | None:
        self._get(handle)
        return "fake screen" if self.supports_terminal else None

    async def send_keys(self, handle: str, items: list[dict]) -> dict[str, Any]:
        if not self.supports_terminal:
            raise NotImplementedError("no terminal")
        self._get(handle)
        self.calls.append(("send_keys", handle, items))
        return {"session_id": handle, "screen": "fake screen", "sent": items}

    def run_slash(self, handle: str, text: str) -> None:
        if not self.supports_terminal:
            raise NotImplementedError("no terminal")
        self._get(handle)["status"] = "working"
        self.calls.append(("run_slash", handle, text))

    def list_native(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions.values()]

    def native_pane(self, handle: str) -> str | None:
        return f"fake_{handle}" if self.supports_terminal and handle in self.sessions else None

    def backend_of(self, handle: str) -> str | None:
        s = self.sessions.get(handle)
        return s["backend"] if s else None

    def set_observer(self, cb: Observer | None) -> None:
        self._observer = cb

    async def rehydrate(self) -> list[dict[str, Any]]:
        return []

    async def shutdown(self) -> None:
        self.sessions.clear()

    def _get(self, handle: str) -> dict[str, Any]:
        s = self.sessions.get(handle)
        if s is None:
            raise KeyError(f"unknown session: {handle}")
        return s
