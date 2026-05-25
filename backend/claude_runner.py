"""Claude execution engine behind a swappable interface.

`ClaudeRunner` is the contract the voice tools call. `SDKClaudeRunner` implements
it with the Claude Agent SDK. A future `TmuxClaudeRunner` (drives the interactive
TUI to stay on the Max subscription after 2026-06-15) can implement the same
interface without touching the voice layer.

Core model: each `advance`/`answer` call drives Claude until its **next decision
point or completion**. A risky tool triggers the SDK's `can_use_tool` callback,
which parks on a Future; `advance` returns `needs_permission` so the voice agent
can ask the user, and `answer` resolves the Future to resume. Validated: the
callback can block and be resolved from a different coroutine without deadlock.
"""
from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from uuid import uuid4

import claude_agent_sdk as sdk

from permissions import classify

log = logging.getLogger("voice-claude.runner")

Status = Literal["running", "needs_permission", "needs_choice", "completed", "error"]

_ALLOW_WORDS = {
    "allow", "yes", "approve", "approved", "y", "ok", "okay", "sure",
    "go", "go ahead", "do it", "yep", "yeah", "confirm", "accept",
}


@dataclass
class Prompt:
    kind: Literal["permission", "choice"]
    text: str                 # spoken summary of what Claude wants
    options: list[str]
    tool_name: str
    request_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class AdvanceResult:
    status: Status
    assistant_text: str
    prompt: Optional[Prompt] = None
    error: Optional[str] = None
    session_id: Optional[str] = None
    cost_usd: float = 0.0  # cumulative Claude API-equivalent cost for this session

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "assistant_text": self.assistant_text,
            "session_id": self.session_id,
            "cost_usd": round(self.cost_usd, 4),
        }
        if self.error:
            d["error"] = self.error
        if self.prompt:
            d["prompt"] = {
                "kind": self.prompt.kind,
                "text": self.prompt.text,
                "options": self.prompt.options,
                "tool_name": self.prompt.tool_name,
                "request_id": self.prompt.request_id,
            }
        return d


# Permission modes the UI/voice can switch between (subset of the CLI's full set).
# Order matters for the CLI: it's the Shift+Tab cycle order verified live
# (default -> acceptEdits -> plan -> auto -> default).
MODE_CYCLE = ["default", "acceptEdits", "plan", "auto"]
VALID_MODES = set(MODE_CYCLE)


def normalize_mode(mode: str | None) -> str:
    m = (mode or "default").strip()
    return m if m in VALID_MODES else "default"


class ClaudeRunner(ABC):
    @abstractmethod
    async def start(self, cwd: str, model: str | None = None, mode: str = "default") -> str: ...
    @abstractmethod
    async def advance(self, handle: str, message: str) -> AdvanceResult: ...
    @abstractmethod
    async def answer(self, handle: str, choice: str) -> AdvanceResult: ...
    # Non-blocking variants: kick off advance/answer in the background so the
    # voice model can keep talking while Claude works; the caller polls.
    @abstractmethod
    def start_advance(self, handle: str, message: str) -> None: ...
    @abstractmethod
    def start_answer(self, handle: str, choice: str) -> None: ...
    @abstractmethod
    def poll_status(self, handle: str) -> dict[str, Any]: ...
    @abstractmethod
    async def interrupt(self, handle: str) -> None: ...
    @abstractmethod
    async def close(self, handle: str) -> None: ...
    @abstractmethod
    async def set_mode(self, handle: str, mode: str) -> str: ...
    @abstractmethod
    async def read(self, handle: str) -> str: ...
    @abstractmethod
    def list(self) -> list[dict[str, Any]]: ...
    @abstractmethod
    async def shutdown(self) -> None: ...


class _Session:
    def __init__(self, handle: str, cwd: str, model: str):
        self.handle = handle
        self.cwd = cwd
        self.model = model
        self.mode = "default"
        self.client: sdk.ClaudeSDKClient | None = None
        self.session_id: str | None = None        # real on-disk id (for handoff)
        self.status: Status = "running"
        self.error: str | None = None
        self.cost_usd: float = 0.0                 # cumulative API-equivalent cost
        self._delta: list[str] = []               # assistant text since last collect
        self._transcript: list[str] = []          # full assistant text (for read())
        self.tools_used: list[str] = []
        self._stop = asyncio.Event()
        self.pending: Prompt | None = None
        self._decision: asyncio.Future[str] | None = None
        self._consumer: asyncio.Task | None = None
        self._perm_lock = asyncio.Lock()          # one pending prompt at a time
        self._turn_lock = asyncio.Lock()          # serialize advance/answer


class SDKClaudeRunner(ClaudeRunner):
    def __init__(self, default_model: str | None = None):
        self._default_model = default_model or os.getenv("CLAUDE_MODEL", "opus")
        self._sessions: dict[str, _Session] = {}
        # In-flight background advance/answer tasks, keyed by session handle.
        self._bg: dict[str, asyncio.Task[AdvanceResult]] = {}

    # --- lifecycle --------------------------------------------------------

    async def start(self, cwd: str, model: str | None = None, mode: str = "default") -> str:
        cwd = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"not a directory: {cwd}")
        handle = str(uuid4())
        s = _Session(handle, cwd, model or self._default_model)
        s.mode = normalize_mode(mode)

        async def _cb(tool_name: str, tool_input: dict[str, Any], context: Any):
            return await self._can_use_tool(s, tool_name, tool_input, context)

        opts = sdk.ClaudeAgentOptions(
            model=s.model,
            cwd=cwd,
            permission_mode=s.mode,      # risky tools route to can_use_tool in default/plan
            can_use_tool=_cb,
            session_id=handle,           # ask SDK to use our id (verify; we also capture)
        )
        client = sdk.ClaudeSDKClient(opts)
        await client.connect()
        s.client = client
        self._sessions[handle] = s
        return handle

    async def shutdown(self) -> None:
        for t in self._bg.values():
            if not t.done():
                t.cancel()
        self._bg.clear()
        for s in list(self._sessions.values()):
            try:
                if s._consumer and not s._consumer.done():
                    s._consumer.cancel()
                if s.client:
                    await s.client.disconnect()
            except Exception:
                pass
        self._sessions.clear()

    # --- driving ----------------------------------------------------------

    async def advance(self, handle: str, message: str) -> AdvanceResult:
        s = self._get(handle)
        async with s._turn_lock:
            if s._decision is not None:
                return self._err(s, "a prompt is pending; call answer first")
            s._delta.clear()
            s._stop.clear()
            s.status = "running"
            assert s.client is not None
            await s.client.query(message)
            s._consumer = asyncio.create_task(self._consume(s))
            await s._stop.wait()
            return self._collect(s)

    async def answer(self, handle: str, choice: str) -> AdvanceResult:
        s = self._get(handle)
        async with s._turn_lock:
            if s._decision is None:
                return self._err(s, "no pending prompt to answer")
            fut = s._decision
            s._delta.clear()
            s._stop.clear()
            s.status = "running"
            fut.set_result(choice)        # resume the parked can_use_tool callback
            await s._stop.wait()
            return self._collect(s)

    # --- non-blocking driving (background + poll) -------------------------

    def start_advance(self, handle: str, message: str) -> None:
        self._get(handle)  # validate handle
        self._bg[handle] = asyncio.create_task(self.advance(handle, message))

    def start_answer(self, handle: str, choice: str) -> None:
        self._get(handle)
        self._bg[handle] = asyncio.create_task(self.answer(handle, choice))

    def poll_status(self, handle: str) -> dict[str, Any]:
        """Report the in-flight background turn for a session.

        Returns {"status":"working"} while Claude runs, the full AdvanceResult
        once it stops (completed/needs_permission/needs_choice/error), or
        {"status":"idle"} when nothing is in flight."""
        task = self._bg.get(handle)
        sid = self._sessions[handle].session_id if handle in self._sessions else None
        if task is None:
            return {"status": "idle", "session_id": sid}
        if not task.done():
            return {"status": "working", "session_id": sid}
        self._bg.pop(handle, None)
        exc = task.exception()
        if exc is not None:
            return {"status": "error", "error": str(exc), "session_id": sid}
        return task.result().to_dict()

    async def interrupt(self, handle: str) -> None:
        s = self._get(handle)
        bg = self._bg.pop(handle, None)
        if bg and not bg.done():
            bg.cancel()
        if s._decision is not None and not s._decision.done():
            s._decision.set_result("deny")
        if s.client:
            try:
                await s.client.interrupt()
            except Exception:
                pass
        s.status = "completed"
        s._stop.set()

    async def close(self, handle: str) -> None:
        s = self._get(handle)
        bg = self._bg.pop(handle, None)
        if bg and not bg.done():
            bg.cancel()
        if s._decision is not None and not s._decision.done():
            s._decision.set_result("deny")
        try:
            if s._consumer and not s._consumer.done():
                s._consumer.cancel()
            if s.client:
                await s.client.disconnect()
        except Exception:
            pass
        self._sessions.pop(handle, None)

    async def set_mode(self, handle: str, mode: str) -> str:
        s = self._get(handle)
        mode = normalize_mode(mode)
        if s.client:
            await s.client.set_permission_mode(mode)
        s.mode = mode
        return mode

    async def read(self, handle: str) -> str:
        return "".join(self._get(handle)._transcript)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "handle": s.handle,
                "session_id": s.session_id,
                "cwd": s.cwd,
                "model": s.model,
                "mode": s.mode,
                "status": s.status,
                "cost_usd": round(s.cost_usd, 4),
            }
            for s in self._sessions.values()
        ]

    # --- internals --------------------------------------------------------

    def _get(self, handle: str) -> _Session:
        s = self._sessions.get(handle)
        if s is None:
            raise KeyError(f"unknown session: {handle}")
        return s

    async def _consume(self, s: _Session) -> None:
        """Drain one Claude turn; append text, stop at completion."""
        try:
            assert s.client is not None
            async for msg in s.client.receive_response():
                if isinstance(msg, sdk.SystemMessage):
                    if not s.session_id:
                        s.session_id = msg.data.get("session_id")
                elif isinstance(msg, sdk.AssistantMessage):
                    if not s.session_id and msg.session_id:
                        s.session_id = msg.session_id
                    for b in msg.content:
                        if isinstance(b, sdk.TextBlock):
                            s._delta.append(b.text)
                            s._transcript.append(b.text)
                        elif isinstance(b, sdk.ToolUseBlock):
                            s.tools_used.append(b.name)
                elif isinstance(msg, sdk.ResultMessage):
                    if not s.session_id:
                        s.session_id = msg.session_id
                    if msg.total_cost_usd:
                        s.cost_usd += msg.total_cost_usd
                        log.info(
                            "session %s turn cost $%.4f (cumulative $%.4f)",
                            s.session_id, msg.total_cost_usd, s.cost_usd,
                        )
                    if msg.is_error:
                        s.status = "error"
                        s.error = (msg.errors or ["unknown error"])[0]
                    else:
                        s.status = "completed"
                    s._stop.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            s.status = "error"
            s.error = str(e)
            s._stop.set()

    async def _can_use_tool(self, s: _Session, tool_name: str,
                            tool_input: dict[str, Any], context: Any):
        kind = classify(tool_name)
        if kind == "safe":
            return sdk.PermissionResultAllow()

        async with s._perm_lock:           # serialize concurrent risky/question asks
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str] = loop.create_future()
            s._decision = fut
            s.pending = self._build_prompt(kind, tool_name, tool_input)
            s.status = "needs_choice" if kind == "question" else "needs_permission"
            s._stop.set()                  # let advance/answer return with the prompt
            choice = await fut             # parked until answer() resolves
            s._decision = None
            s.pending = None
            return self._map_decision(kind, tool_name, tool_input, choice)

    def _collect(self, s: _Session) -> AdvanceResult:
        text = "".join(s._delta)
        s._delta.clear()
        return AdvanceResult(
            status=s.status,
            assistant_text=text,
            prompt=s.pending,
            error=s.error,
            session_id=s.session_id,
            cost_usd=s.cost_usd,
        )

    def _err(self, s: _Session, msg: str) -> AdvanceResult:
        return AdvanceResult(status="error", assistant_text="", error=msg,
                             session_id=s.session_id)

    def _build_prompt(self, kind: str, tool_name: str,
                      tool_input: dict[str, Any]) -> Prompt:
        if kind == "question":
            text, options = _parse_question(tool_input)
            return Prompt(kind="choice", text=text, options=options, tool_name=tool_name)
        return Prompt(
            kind="permission",
            text=_summarize_tool(tool_name, tool_input),
            options=["allow", "deny"],
            tool_name=tool_name,
        )

    def _map_decision(self, kind: str, tool_name: str,
                      tool_input: dict[str, Any], choice: str):
        c = (choice or "").strip().lower()
        if kind == "question":
            # Best-effort: allow the tool with the selection injected.
            # AskUserQuestion's exact answer schema is verified in the e2e step.
            return sdk.PermissionResultAllow(updated_input={**tool_input, "answer": choice})
        if c in _ALLOW_WORDS or any(c.startswith(w) for w in _ALLOW_WORDS):
            return sdk.PermissionResultAllow()
        return sdk.PermissionResultDeny(message=f"User declined: {choice}")


def _summarize_tool(tool_name: str, ti: dict[str, Any]) -> str:
    """Human, speakable description of a tool request."""
    if tool_name == "Bash":
        cmd = ti.get("command", "")
        return f"run the command: {cmd}"
    if tool_name in ("Write",):
        return f"write the file {ti.get('file_path', '?')}"
    if tool_name in ("Edit", "NotebookEdit"):
        return f"edit the file {ti.get('file_path', ti.get('notebook_path', '?'))}"
    return f"use the {tool_name} tool"


def _parse_question(ti: dict[str, Any]) -> tuple[str, list[str]]:
    """Pull a question + option labels out of an AskUserQuestion input."""
    questions = ti.get("questions") or []
    if questions:
        q = questions[0]
        text = q.get("question") or q.get("header") or "Claude has a question"
        options = [o.get("label", str(o)) if isinstance(o, dict) else str(o)
                   for o in (q.get("options") or [])]
        return text, options
    return ("Claude has a question", [])
