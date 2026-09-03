"""AgentProvider — the contract every coding-agent backend implements.

Shaped to what the existing runners actually do: `send_message`/`answer` are
NON-BLOCKING (they kick off a turn and return) and `poll` returns the runner's
result dict — the voice model depends on "returns working instantly, poll later"
(see frontend/lib/operating.ts). Awaiting a turn to completion here would stall
the voice for minutes.
"""
from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


def utcnow_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


@dataclass(frozen=True)
class AgentCapabilities:
    interactive_terminal: bool = False
    slash_commands: bool = False
    send_keys: bool = False
    permission_modes: tuple[str, ...] = ("default",)
    supports_interrupt: bool = True
    supports_rehydrate: bool = False
    supports_resume: bool = False
    supports_events: bool = False
    cost_tracking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"interactive_terminal": self.interactive_terminal,
                "slash_commands": self.slash_commands, "send_keys": self.send_keys,
                "permission_modes": list(self.permission_modes),
                "supports_interrupt": self.supports_interrupt,
                "supports_rehydrate": self.supports_rehydrate,
                "supports_resume": self.supports_resume,
                "supports_events": self.supports_events,
                "cost_tracking": self.cost_tracking}


@dataclass(frozen=True)
class AgentHealth:
    online: bool
    version: str | None
    detail: str
    checked_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict[str, Any]:
        return {"online": self.online, "version": self.version, "detail": self.detail,
                "checked_at": self.checked_at}


@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    root_path: str


@dataclass(frozen=True)
class SessionOptions:
    backend: str = "cli"
    mode: str = "default"
    model: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ProviderEvent:
    """Provider-neutral runtime signal. kind ∈ tool_started | needs_permission |
    needs_choice | turn_completed | cost_updated | error. The provider never sees
    Yuri ids, missions, or the store — SessionService turns these into YuriEvents."""
    kind: str
    payload: dict[str, Any]


Observer = Callable[[str, ProviderEvent], None]


class AgentProvider(ABC):
    id: str = ""
    name: str = ""

    @abstractmethod
    def capabilities(self) -> AgentCapabilities: ...

    @abstractmethod
    async def health(self) -> AgentHealth: ...

    @abstractmethod
    async def create_session(self, project: ProjectContext, opts: SessionOptions) -> str:
        """Start a new native session in project.root_path; returns the native handle."""

    @abstractmethod
    def send_message(self, handle: str, message: str) -> None: ...

    @abstractmethod
    def answer(self, handle: str, choice: str) -> None: ...

    @abstractmethod
    def poll(self, handle: str) -> dict[str, Any]:
        """Oldest unread turn result, or {"status": "working"|"idle", "session_id": handle}."""

    @abstractmethod
    async def interrupt(self, handle: str) -> None: ...

    @abstractmethod
    async def stop(self, handle: str) -> None: ...

    @abstractmethod
    async def set_mode(self, handle: str, mode: str) -> str: ...

    @abstractmethod
    async def read(self, handle: str) -> str: ...

    @abstractmethod
    async def peek(self, handle: str, lines: int = 40) -> str | None:
        """Live screen snapshot, or None when the backend has no TUI."""

    @abstractmethod
    def list_native(self) -> list[dict[str, Any]]:
        """Runner-shaped session dicts (handle, cwd, model, mode, status, cost_usd, prompt?,
        queued counts) tagged with "backend"."""

    @abstractmethod
    def set_observer(self, cb: Observer | None) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    # Optional surface — default "unsupported". Callers check capabilities() or catch.
    async def send_keys(self, handle: str, items: list[dict]) -> dict[str, Any]:
        raise NotImplementedError(f"{self.id} does not support send_keys")

    def run_slash(self, handle: str, text: str) -> None:
        raise NotImplementedError(f"{self.id} does not support slash commands")

    async def resume(self, native_session_id: str, project: ProjectContext,
                     opts: SessionOptions) -> str:
        raise NotImplementedError(f"{self.id} does not support resume")

    def native_pane(self, handle: str) -> str | None:
        return None

    def backend_of(self, handle: str) -> str | None:
        return None

    async def rehydrate(self, known: dict[str, dict] | None = None) -> list[dict[str, Any]]:
        """Re-adopt what survived a restart.

        `known` is what Yuri already has a row for, per native session id:
        `{native_session_id: {**runtime_metadata, "cwd": working_directory}}`.
        A provider whose sessions die with the process ignores it and
        enumerates its own survivors; a provider whose sessions are durable
        server-side needs it to tell *hers* from sessions the user started
        themselves, which are never adopted.
        """
        return []

    def runtime_metadata_for(self, handle: str) -> dict[str, Any]:
        """Provider state that must survive a restart, merged onto the session
        row on every poll.

        `{}` for every provider whose state dies with the process — which is
        all of them but OpenCode, whose read cursors are the only thing
        standing between a restart and re-narrating history. Sync, because
        `poll` is: this is read on the same tick.
        """
        return {}
