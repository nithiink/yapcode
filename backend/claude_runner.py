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
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from uuid import uuid4

import claude_agent_sdk as sdk

import config
from permissions import classify, is_plan_file_write, mode_covers
from event_log import log_event

log = logging.getLogger("yapcode.runner")

Status = Literal["running", "needs_permission", "needs_choice", "completed", "error"]

_ALLOW_WORDS = {
    "allow", "yes", "approve", "approved", "y", "ok", "okay", "sure",
    "go", "go ahead", "do it", "yep", "yeah", "confirm", "accept",
    "proceed",  # natural "yes" for the ExitPlanMode plan-approval prompt
}

# Bare declines (no feedback attached) for the plan-approval dialog.
_DENY_WORDS = {"deny", "no", "nope", "decline", "declined", "cancel", "reject", "rejected", "n"}

# Negations that aren't single tokens (caught as substrings, not word matches).
_DENY_PHRASES = ("don't", "do not", "stop")


def decide_permission(choice: str) -> Optional[str]:
    """Resolve a binary permission answer to "allow", "deny", or None (ambiguous).

    A SECURITY gate that fails CLOSED: matching is word-level (not the old
    `startswith`, which let "y" match "your"), any negation wins, and anything
    that isn't a clean allow/deny returns None so the caller re-asks.
    """
    c = (choice or "").strip().lower()
    if not c:
        return None
    tokens = set(re.findall(r"[a-z']+", c))
    if tokens & _DENY_WORDS or any(p in c for p in _DENY_PHRASES):
        return "deny"
    if c in _ALLOW_WORDS or (tokens & _ALLOW_WORDS):
        return "allow"
    return None


@dataclass
class Prompt:
    kind: Literal["permission", "choice"]
    text: str                 # spoken summary of what Claude wants
    options: list[str]
    tool_name: str
    multi_select: bool = False  # AskUserQuestion: select many vs. one
    request_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class AdvanceResult:
    status: Status
    assistant_text: str
    prompt: Optional[Prompt] = None
    error: Optional[str] = None
    session_id: Optional[str] = None
    cost_usd: float = 0.0  # cumulative Claude API-equivalent cost for this session
    request: Optional[str] = None  # the user message this result answers (narration attribution)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "assistant_text": self.assistant_text,
            "session_id": self.session_id,
            "cost_usd": round(self.cost_usd, 4),
        }
        if self.request:
            d["request"] = self.request
        if self.error:
            d["error"] = self.error
        if self.prompt:
            d["prompt"] = {
                "kind": self.prompt.kind,
                "text": self.prompt.text,
                "options": self.prompt.options,
                "tool_name": self.prompt.tool_name,
                "multi_select": self.prompt.multi_select,
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
    async def answer(self, handle: str, choice: str, seq: int) -> AdvanceResult: ...
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

    # Escape hatch for sending raw terminal keys/text. Only the interactive CLI
    # (tmux) backend can honor this; the SDK has no TUI. Non-abstract so SDK
    # runners inherit this backstop — dispatch gates it out before calling.
    async def send_keys(self, handle: str, items: list[dict]) -> dict[str, Any]:
        raise NotImplementedError("send_keys requires the CLI (tmux) backend")

    # Reopen an existing Claude Code session in a hooked tmux pane (terminal →
    # voice handoff). CLI-only; the SDK has no tmux pane to attach to.
    async def resume(self, session_id: str, cwd: str, model: str | None = None,
                     mode: str = "default", name: str | None = None) -> str:
        raise NotImplementedError("resume requires the CLI (tmux) backend")


class _Session:
    def __init__(self, handle: str, cwd: str, model: str):
        self.handle = handle
        self.cwd = cwd
        self.model = model
        self.mode = "default"
        self.client: sdk.ClaudeSDKClient | None = None
        self.session_id: str | None = None        # real on-disk id (for handoff)
        self.status: Status = "running"
        self.turn_prompt: str | None = None        # message of the in-flight turn
        self.error: str | None = None
        self.cost_usd: float = 0.0                 # cumulative API-equivalent cost
        self._delta: list[str] = []               # assistant text since last collect
        self._transcript: list[str] = []          # full assistant text (for read())
        self.tools_used: list[str] = []
        self._stop = asyncio.Event()
        self.pending: Prompt | None = None
        self._decision: asyncio.Future[str] | None = None
        # A second answer for the same prompt must fail fast, not double-resolve:
        # a stale allow could decide a newer prompt (see TmuxClaudeRunner).
        self.prompt_seq = 0       # bumps each time a prompt parks
        self.answer_claimed = -1  # prompt_seq an in-flight answer has claimed
        self._consumer: asyncio.Task | None = None
        self._perm_lock = asyncio.Lock()          # one pending prompt at a time
        self._turn_lock = asyncio.Lock()          # serialize advance/answer
        # FIFO of completed turn results poll_status hasn't read yet. Keeps a
        # finished turn's reply from being lost when a new start_advance fires
        # before poll_status drains it.
        self._pending_results: list[AdvanceResult] = []


class SDKClaudeRunner(ClaudeRunner):
    def __init__(self, default_model: str | None = None):
        self._default_model = default_model or os.getenv("CLAUDE_MODEL", "opus")
        self._sessions: dict[str, _Session] = {}
        # In-flight background advance/answer tasks, keyed by session handle.
        self._bg: dict[str, asyncio.Task[AdvanceResult]] = {}

    # --- lifecycle --------------------------------------------------------

    async def start(self, cwd: str, model: str | None = None, mode: str = "default") -> str:
        # Re-assert the directory sandbox at the sink so a session can't start outside
        # ALLOWED_PROJECT_ROOTS even if a caller bypasses resolve_project_path.
        cwd = config.resolve_within_roots(cwd)
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
            s.turn_prompt = message
            assert s.client is not None
            log_event("backend", "claude", "send", message,
                      session=s.session_id or s.handle[:8],
                      detail={"handle": s.handle, "text": message})
            await s.client.query(message)
            s._consumer = asyncio.create_task(self._consume(s))
            await s._stop.wait()
            return self._collect(s)

    async def answer(self, handle: str, choice: str, seq: int) -> AdvanceResult:
        s = self._get(handle)
        async with s._turn_lock:
            if s._decision is None or s.prompt_seq != seq:
                return self._err(s, "that prompt was already answered — nothing to do")
            fut = s._decision
            s._delta.clear()
            s._stop.clear()
            s.status = "running"
            fut.set_result(choice)        # resume the parked can_use_tool callback
            await s._stop.wait()
            return self._collect(s)

    # --- non-blocking driving (background + poll) -------------------------

    def _harvest_finished(self, handle: str) -> None:
        """If the current bg task is done, move its result into the pending queue
        so a new start_advance/start_answer doesn't drop it. See the matching
        TmuxClaudeRunner._harvest_finished for the bug history. Note: SDK
        backend doesn't (yet) carry an extras-queue — the bug surface is
        smaller because the SDK call itself is more synchronous, but if rapid
        consecutive start_advance becomes an issue here too, mirror the
        _extra_tasks pattern from the tmux runner."""
        task = self._bg.get(handle)
        if task is None or not task.done():
            return
        s = self._sessions.get(handle)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            self._bg.pop(handle, None)
            return
        if s is not None:
            if exc is not None:
                res = AdvanceResult(status="error", assistant_text="",
                                    error=str(exc), session_id=s.session_id)
            else:
                res = task.result()
            s._pending_results.append(res)
            if len(s._pending_results) > 8:
                s._pending_results = s._pending_results[-8:]
        self._bg.pop(handle, None)

    def start_advance(self, handle: str, message: str) -> None:
        self._get(handle)  # validate handle
        self._harvest_finished(handle)
        if handle in self._bg and not self._bg[handle].done():
            log.warning("start_advance for %s while previous task still running", handle)
        task = asyncio.create_task(self.advance(handle, message))
        task.set_name(message)  # carries the message into list()'s `queue`
        self._bg[handle] = task

    def start_answer(self, handle: str, choice: str) -> None:
        s = self._get(handle)
        self._harvest_finished(handle)
        if s._decision is None:
            raise ValueError("no pending prompt to answer — it was already "
                             "resolved (don't answer it again)")
        if s.answer_claimed == s.prompt_seq:
            raise ValueError("that prompt is already being answered — "
                             "don't answer it again")
        s.answer_claimed = s.prompt_seq
        if handle in self._bg and not self._bg[handle].done():
            log.warning("start_answer for %s while previous task still running", handle)
        task = asyncio.create_task(self.answer(handle, choice, s.prompt_seq))
        task.set_name(f"answer: {choice}")
        self._bg[handle] = task

    def poll_status(self, handle: str) -> dict[str, Any]:
        """Report the in-flight background turn for a session.

        Returns the oldest queued completed result first (so a new start_advance
        can't drop a previous turn's reply), then the live task status, then
        {"status":"idle"} when nothing is in flight."""
        sid = self._sessions[handle].session_id if handle in self._sessions else None
        s = self._sessions.get(handle)
        if s is not None and s._pending_results:
            return s._pending_results.pop(0).to_dict()
        task = self._bg.get(handle)
        if task is None:
            return {"status": "idle", "session_id": sid}
        if not task.done():
            return {"status": "working", "session_id": sid}
        self._bg.pop(handle, None)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return {"status": "idle", "session_id": sid}
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
        # set_permission_mode affects FUTURE tool calls only — a can_use_tool
        # callback already parked on s._decision keeps waiting. If the new mode
        # would have auto-approved that tool, resolve the prompt now so
        # 'switch to auto mode' doesn't leave it hanging. Skip if an
        # answer_prompt already claimed it — one decision per prompt.
        if (s.pending and s.pending.kind == "permission"
                and mode_covers(mode, s.pending.tool_name)
                and s.answer_claimed != s.prompt_seq):
            self.start_answer(handle, "allow")
        return mode

    async def read(self, handle: str) -> str:
        return "".join(self._get(handle)._transcript)

    def _queue_counts(self, s: _Session) -> dict[str, Any]:
        """Live work-pipeline view for the UI, mirroring the tmux runner. The SDK
        backend has no extras queue (a turn runs to completion before the next
        start_advance), so at most one turn is in flight and `queued` is always 0;
        see TmuxClaudeRunner for the queued path. A finished-but-unharvested task
        counts toward `pending` so the number is continuous across the harvest
        boundary. Cancelled tasks never become results, so they're excluded. Pure
        read — never harvests."""
        bg = self._bg.get(s.handle)
        running = bg is not None and not bg.done()
        done = 1 if (bg is not None and bg.done() and not bg.cancelled()) else 0
        queue = []
        if running:
            name = bg.get_name()
            queue.append({"text": "" if name.startswith("Task-") else name, "state": "running"})
        return {
            "running": running,
            "queued": 0,
            "pending": len(s._pending_results) + done,
            "queue": queue,
        }

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for s in self._sessions.values():
            d = {
                "handle": s.handle,
                "session_id": s.session_id,
                "cwd": s.cwd,
                "model": s.model,
                "mode": s.mode,
                "status": s.status,
                "cost_usd": round(s.cost_usd, 4),
                **self._queue_counts(s),
            }
            # Mirror the tmux runner: surface the live pending prompt so an
            # agent that wasn't connected when it fired can still see it.
            if s.pending is not None:
                d["prompt"] = {"kind": s.pending.kind, "text": s.pending.text,
                               "options": list(s.pending.options or []),
                               "tool_name": s.pending.tool_name}
            out.append(d)
        return out

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
                            log_event("claude", "backend", "hook", f"tool: {b.name}",
                                      session=s.session_id or s.handle[:8],
                                      detail={"handle": s.handle, "tool_name": b.name,
                                              "tool_input": b.input})
                elif isinstance(msg, sdk.ResultMessage):
                    if not s.session_id:
                        s.session_id = msg.session_id
                    if msg.total_cost_usd:
                        # total_cost_usd is already cumulative for the whole
                        # ClaudeSDKClient session (verified empirically: turn 2
                        # reports turn-1 cost + delta), so assign — summing it
                        # per turn double-counts.
                        s.cost_usd = msg.total_cost_usd
                        log.info(
                            "session %s cumulative cost $%.4f",
                            s.session_id, s.cost_usd,
                        )
                    if msg.is_error:
                        s.status = "error"
                        s.error = (msg.errors or ["unknown error"])[0]
                        log_event("backend", "voice", "error", s.error or "error",
                                  session=s.session_id or s.handle[:8])
                    else:
                        s.status = "completed"
                        txt = "".join(s._delta)
                        if txt:
                            log_event("claude", "backend", "assistant", txt,
                                      session=s.session_id or s.handle[:8],
                                      detail={"handle": s.handle, "text": txt})
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
        if kind == "safe" or is_plan_file_write(tool_name, tool_input):
            return sdk.PermissionResultAllow()

        async with s._perm_lock:           # serialize concurrent risky/question asks
            loop = asyncio.get_running_loop()
            while True:
                fut: asyncio.Future[str] = loop.create_future()
                s._decision = fut
                s.pending = self._build_prompt(kind, tool_name, tool_input)
                # Each loop iteration re-parks, so an ambiguous re-ask bumps
                # prompt_seq again — that alone makes any prior answer_claimed
                # stale, so this runner never resets answer_claimed explicitly
                # (unlike tmux, whose re-ask keeps the same prompt_seq).
                s.prompt_seq += 1
                s.status = "needs_choice" if kind == "question" else "needs_permission"
                log_event("claude", "backend", "hook",
                          f"needs {'choice' if kind == 'question' else 'permission'}: {s.pending.text}",
                          session=s.session_id or s.handle[:8],
                          detail={"handle": s.handle, "tool_name": tool_name})
                s._stop.set()              # let advance/answer return with the prompt
                choice = await fut         # parked until answer() resolves
                # Re-ask an ambiguous binary permission (fail closed); questions
                # and the plan dialog have their own non-binary handling.
                if (kind != "permission" or tool_name == "ExitPlanMode"
                        or decide_permission(choice) is not None):
                    break
                log_event("backend", "claude", "decision",
                          f"ambiguous, re-asking: {choice}",
                          session=s.session_id or s.handle[:8],
                          detail={"handle": s.handle, "tool_name": tool_name,
                                  "choice": choice})
            s._decision = None
            s.pending = None
            log_event("backend", "claude", "decision", str(choice),
                      session=s.session_id or s.handle[:8],
                      detail={"handle": s.handle, "tool_name": tool_name, "choice": choice})
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
            request=s.turn_prompt,
        )

    def _err(self, s: _Session, msg: str) -> AdvanceResult:
        return AdvanceResult(status="error", assistant_text="", error=msg,
                             session_id=s.session_id)

    def _build_prompt(self, kind: str, tool_name: str,
                      tool_input: dict[str, Any]) -> Prompt:
        if kind == "question":
            text, options, multi = _parse_question(tool_input)
            return Prompt(kind="choice", text=text, options=options,
                          tool_name=tool_name, multi_select=multi)
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
        if tool_name == "ExitPlanMode":
            # Plan approval isn't binary: "auto"/"manual"/"proceed" leave plan
            # mode; anything else keeps planning and forwards the text as feedback.
            # Fail closed: an explicit decline wins even when the phrase also
            # contains an intent word — "do not proceed" / "don't approve" must
            # NOT be read as approval just because they contain "proceed"/"approve".
            if decide_permission(choice) == "deny":
                return sdk.PermissionResultDeny(message=f"Keep planning: {choice}")
            if (decide_permission(choice) == "allow"
                    or any(w in c for w in ("auto", "manual", "proceed", "approve"))):
                return sdk.PermissionResultAllow()
            return sdk.PermissionResultDeny(message=f"Keep planning: {choice}")
        # Binary permission: approve only on a clean allow (ambiguity re-asked above).
        if decide_permission(choice) == "allow":
            return sdk.PermissionResultAllow()
        return sdk.PermissionResultDeny(message=f"User declined: {choice}")


def _summarize_tool(tool_name: str, ti: dict[str, Any]) -> str:
    """Human, speakable description of a tool request."""
    if tool_name == "ExitPlanMode":
        # The plan-mode "how do you want to proceed?" approval. Phrase it as the
        # plan decision so the voice agent recognizes it and the user can answer
        # naturally ("proceed" / "keep planning") rather than seeing an opaque
        # "use the ExitPlanMode tool" permission. Approve -> allow (leave plan
        # mode, start making changes); decline -> deny (stay in plan mode).
        text = ("decide how to proceed with the plan it just laid out (now shown "
                "in the session's terminal). The choices: say 'auto' to approve "
                "and let it run without further prompts; 'manual' to approve but "
                "keep approving each edit by voice; or decline to keep it in plan "
                "mode — and if you give a reason or change, that feedback is "
                "passed to Claude to revise the plan. Offer these to the user and "
                "pass their pick (or their feedback) to answer_prompt")
        # The prompt text is the only channel the voice agent reliably gets —
        # the plan scrolls off the pane before it can peek.
        plan = str(ti.get("plan") or "").strip()
        if plan:
            if len(plan) > 8000:
                plan = plan[:8000] + "\n…(plan truncated)"
            text += (". The full plan follows — summarize it for the user "
                     f"before asking:\n\n{plan}")
        return text
    if tool_name == "Bash":
        cmd = ti.get("command", "")
        return f"run the command: {cmd}"
    if tool_name in ("Write",):
        return f"write the file {ti.get('file_path', '?')}"
    if tool_name in ("Edit", "NotebookEdit"):
        return f"edit the file {ti.get('file_path', ti.get('notebook_path', '?'))}"
    return f"use the {tool_name} tool"


def _parse_questions(ti: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize every question in an AskUserQuestion input. The tool can ask up
    to 4 questions in one call, rendered as a sequential form, so callers that
    drive the live menu need all of them, not just the first."""
    out: list[dict[str, Any]] = []
    for q in (ti.get("questions") or []):
        out.append({
            "header": q.get("header"),
            "question": q.get("question") or q.get("header") or "Claude has a question",
            "options": [o.get("label", str(o)) if isinstance(o, dict) else str(o)
                        for o in (q.get("options") or [])],
            "multi": bool(q.get("multiSelect")),
        })
    return out


def _parse_question(ti: dict[str, Any]) -> tuple[str, list[str], bool]:
    """The first question only (text, option labels, multi-select flag). Kept for
    the SDK path, which answers one question per tool result."""
    qs = _parse_questions(ti)
    if qs:
        return qs[0]["question"], qs[0]["options"], qs[0]["multi"]
    return ("Claude has a question", [], False)
