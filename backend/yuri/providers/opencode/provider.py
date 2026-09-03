"""OpenCode as an AgentProvider — the second real implementation of the
contract, and therefore the first evidence that the abstraction is one.

`client.py` owns the envelope, the auth header and the error translation;
`server.py` owns attach-or-spawn and the never-stop-what-you-didn't-start
rule. What is left here reads as contract methods plus one piece of real
design: the cursor.

## The cursor

Per handle the provider remembers the highest `durable.seq` it has actually
consumed from `GET /history?after=N`. Two properties follow, both tested:

  * **A completed turn is reported exactly once.** The cursor advances only
    past events that were genuinely returned, so a second `poll()` sees an
    empty page — the bug the tmux backend needs a `_pending_results` FIFO to
    avoid, avoided here by the server's own sequence numbers.
  * **An event type nobody has mapped is ignored, not fatal.** A successful
    turn's event vocabulary was never captured during the live probe (design
    spec section 2.1), so completion is read from an assistant message's
    `finish` field — never from an event type we have not observed — and an
    unrecognised type advances the cursor without raising. The failure mode is
    silence, which the live check closes, rather than a wrong claim.

`/message` has no cursor of its own, so the handle carries a second
high-water mark (`msg_seen`): the number of message entries already reported.
Without it the *previous* turn's finished assistant message would be found the
instant the next turn's first event arrived and reported as that turn's reply.

## Asks outrank history, and must not consume it

A pending permission or question is read on every `poll` and reported ahead of
anything in history: a blocked turn has to surface the ask, because the user
is the only thing that can unblock it. The early return therefore leaves
**both** marks exactly where they were, so a completion or a failure sitting
behind the ask is *deferred*, not swallowed — the next poll after the ask
clears reports it, exactly once, from the same unmoved marks.

`answer` maps a decision onto OpenCode's reply endpoints under one rule that
is not negotiable: **allow → `once`, deny → `reject`, and `always` is never
sent for any phrasing.** `decide_permission` answers a single question;
`always` would turn one spoken "yes" into a standing grant the user never
agreed to, and granting standing permission is a mode change OpenCode does not
even expose. Anything `decide_permission` cannot read cleanly raises a
`ValueError` and reaches OpenCode not at all.

## The sync/async bridge — the one real design choice

`send_message`, `answer`, `poll`, `list_native`, `run_slash` and `backend_of`
are synchronous by contract (the voice model polls; awaiting a turn would
stall speech for minutes), but every OpenCode operation is async HTTP. Of the
two available mechanisms this uses **(a): the provider owns one background
thread running its own event loop**, and every method — sync *and* async —
submits its coroutine there with `asyncio.run_coroutine_threadsafe`. Sync
callers block on the returned `concurrent.futures.Future`; async callers
`await asyncio.wrap_future(...)`.

Why (a) and not a bridge onto the caller's running loop: Yuri's sync provider
methods are called *from inside* her event loop (`tools.py` → `SessionService`
→ `provider.poll`), and `run_coroutine_threadsafe(...).result()` onto that
same loop would deadlock — the thread that must run the coroutine is the
thread blocked waiting for it. (`asyncio.run()` is worse still: it raises
outright inside a running loop.) A separate loop also keeps every
`httpx.AsyncClient` and every `asyncio.Lock` in `server.py` bound to exactly
one loop for the provider's whole life, which is what those objects require;
splitting the async methods onto the caller's loop would use a pooled HTTP
client from two loops at once.

The thread is created lazily on first use — constructing a provider that is
never used costs nothing, which matters because `build_container` constructs
every configured provider at startup — and `shutdown()` cancels whatever is
left on it, stops the loop, **joins the thread** and closes the loop, so a
suite run leaks no threads, loops or warnings. It is a daemon thread purely as
a backstop against a caller who forgets to call `shutdown()`.

`send_message` stays effectively non-blocking because `POST …/prompt` is:
it returns `admittedSeq` immediately rather than awaiting the turn. `poll`
issues its two reads (history, and messages only while a turn is in flight)
concurrently on the provider's loop, so it costs one round trip, not two.

The honest cost of a synchronous contract: a sync method blocks the *caller's*
thread — Yuri's event loop — for that round trip, where `ClaudeCodeProvider`'s
equivalent is a pure in-memory read. Local, that is a millisecond or two. The
worst case is bounded by the HTTP client's own 30s timeout (CALL_TIMEOUT_S is
only the backstop above it), so a hung OpenCode stalls the loop rather than
losing work. Removing that entirely means an async `poll` in `base.py`, which
is a contract change for every provider and nobody's to make here.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Coroutine

from ..base import (AgentCapabilities, AgentHealth, AgentProvider, Observer,
                    ProjectContext, SessionOptions)
from ..consent import decide_permission
from .client import OpenCodeClient, OpenCodeError
from .server import OpenCodeServer

log = logging.getLogger("yuri.providers.opencode")

HEALTH_TTL_S = 30.0          # same shape as ClaudeCodeProvider.health()
CALL_TIMEOUT_S = 60.0        # a bridged call is one HTTP round trip; this is the backstop
TEARDOWN_TIMEOUT_S = 5.0
MAX_ASSISTANT_TEXT = 2000    # matches the Claude path's cap (sessions.py, claude_code.py)
MAX_PROMPT_TEXT = 2000       # an ask is spoken and stored as Approval.description

# OpenCode's two ask endpoints. The values are the URL segment AND the kind
# recorded in the reply, so one word does both jobs.
PERMISSION, QUESTION = "permission", "question"

# PermissionV2Reply is `once | always | reject` (design spec section 2). Only
# two of the three are reachable from here, and that is the point: see
# `_permission_reply` and design spec section 7.
REPLY_ONCE, REPLY_REJECT = "once", "reject"

# The one event type the live probe actually observed for a failure. Everything
# else is deliberately unmapped: see the module docstring and design spec 2.1.
FAILED_STEP = "session.next.step.failed"


@dataclass(frozen=True)
class _Pending:
    """The ask `poll` last surfaced, so `answer` knows where to reply.

    Deliberately NOT durable: OpenCode owns the pending list, so after a
    restart the first `poll` re-derives this from the server. Persisting it
    would let a remembered id outlive the request it names.
    """
    kind: str                   # PERMISSION | QUESTION
    request_id: str


@dataclass
class _Handle:
    """Everything the provider remembers about one OpenCode session.

    `cursor` and `msg_seen` are the durable part — Task 6 persists them into
    the session row's `runtime_metadata` so a Yuri restart resumes reading
    where she stopped instead of re-narrating history.
    """
    cwd: str
    model: str | None = None
    cursor: int = 0             # highest durable.seq consumed from /history
    in_flight: bool = False     # a turn we started and have not reported
    msg_seen: int = 0           # /message entries already reported
    pending: _Pending | None = None   # the ask poll surfaced; answer replies to it


def _surface(h: _Handle, kind: str, status: str,
             prompt: dict[str, Any]) -> dict[str, Any]:
    """Report an ask, with the prompt only the FIRST time it is surfaced.

    OpenCode keeps a request in its pending list until it is answered, so a
    naive poll re-reports the same ask on every 1.5s tick. The status has to
    keep coming -- SessionService reads it to hold the row at
    needs_permission and the mission at waiting_for_approval -- but the prompt
    must not, because the prompt is what the narration layer speaks and what
    the frontend injects.

    Both Claude backends pop each result off a queue, so poll hands a given
    result back exactly once; VoiceAgent.tsx says in writing that it relies on
    that, and enqueueInjection deliberately never evicts a blocking item. So
    re-reporting the prompt grew the injection queue without bound while the
    user was still deciding, and Yuri would keep reading the backlog aloud
    after they had already answered.

    A different request_id is a genuinely new ask and surfaces again.
    """
    if h.pending is not None and h.pending.request_id == prompt["request_id"]:
        return {"status": status}
    h.pending = _Pending(kind, prompt["request_id"])
    return {"status": status, "prompt": prompt}


def _model_ref(model: str | None) -> dict[str, str] | None:
    """`"provider/model"` → OpenCode's `ModelRef`.

    Probe-verified as `{providerID, id}` — the source plan's `modelID` is
    wrong and OpenCode rejects it. A bare name with no provider sends `id`
    alone rather than guessing a provider, because `id` is the only key
    OpenCode requires.
    """
    name = (model or "").strip()
    if not name:
        return None
    provider, _, ident = name.partition("/")
    if ident:
        return {"providerID": provider, "id": ident}
    return {"id": provider}


def _restored_mark(stored: Any, now: int) -> int:
    """One high-water mark, restored across a restart.

    **No stored value means start from NOW, never from zero.** A row that was
    never polled — or one written before these marks existed — has nothing to
    restore, and zero would re-read a whole session and re-narrate everything
    the user already heard.

    **A stored mark above what the server still holds is clamped to it.** That
    can only mean the session was truncated or replaced and its numbering
    restarted; honouring the higher mark would make Yuri permanently deaf to
    the renumbered stream, because nothing would ever exceed it again.
    Clamping is the same "start from now" rule: she skips nothing that exists
    and re-reads nothing that was already consumed.

    Anything unreadable is treated as no mark at all rather than raising — a
    corrupt number in `runtime_metadata` must not cost the session its
    re-adoption.
    """
    try:
        mark = int(stored)
    except (TypeError, ValueError):
        return now
    return now if mark < 0 else min(mark, now)


def _seq_of(event: dict[str, Any]) -> int:
    durable = event.get("durable")
    if not isinstance(durable, dict):
        return 0
    try:
        return int(durable.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _failure_in(events: list[dict[str, Any]]) -> str | None:
    """The message of the last failed step, or None. Probe-verified shape:
    `data.error.message` carries a narratable sentence."""
    message: str | None = None
    for event in events:
        if event.get("type") != FAILED_STEP:
            continue
        data = event.get("data")
        error = data.get("error") if isinstance(data, dict) else None
        raw = error.get("message") if isinstance(error, dict) else error
        message = str(raw) if raw else "OpenCode reported a failed step"
    return message


def _text_of(message: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in message.get("content") or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    if not parts and isinstance(message.get("text"), str):
        parts.append(message["text"])
    return "".join(parts)


def _assistant_text(messages: list[dict[str, Any]]) -> str:
    return "".join(_text_of(m) for m in messages if m.get("type") == "assistant")


def _first_request(requests: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The oldest pending request that actually has an id.

    An entry without an id could not be replied to, and surfacing it would ask
    the user to approve something Yuri can never forward an answer for.
    """
    for request in requests:
        if isinstance(request, dict) and str(request.get("id") or ""):
            return request
    return None


def _permission_prompt(request: dict[str, Any]) -> dict[str, Any]:
    """OpenCode's pending permission → the `Prompt` shape the domain speaks.

    `tool_name`/`tool_input` are not decoration: `ApprovalService.record_request`
    feeds them to `risk_for`, which is what makes an OpenCode approval carry
    the same "that's a destructive action" labelling as a Claude one.
    """
    tool = str(request.get("tool") or "")
    metadata = request.get("metadata")
    title = str(request.get("title") or "").strip()
    return {
        "kind": PERMISSION,
        # Narration renders "needs permission to {text}" and drops the line
        # entirely when the text is empty, so a titleless request still has to
        # say something speakable — the same fallback the Claude path's
        # _summarize_tool ends on.
        "text": (title or f"use the {tool or 'agent'} tool")[:MAX_PROMPT_TEXT],
        "tool_name": tool,
        # Only a mapping: risk_for indexes it, and json.dumps stores it.
        "tool_input": metadata if isinstance(metadata, dict) else {},
        "options": ["allow", "deny"],
        "request_id": str(request.get("id")),
        "multi_select": False,
    }


def _question_prompt(request: dict[str, Any]) -> dict[str, Any]:
    """OpenCode's pending question → `needs_choice`.

    No tool and no multi-select: OpenCode's question carries `{id, text,
    options}` and nothing that says "pick several" (design spec section 2), so
    claiming multi_select would be inventing a capability.
    """
    text = str(request.get("text") or "").strip()
    return {
        "kind": "choice",
        "text": (text or "OpenCode has a question")[:MAX_PROMPT_TEXT],
        "tool_name": "",
        "tool_input": {},
        "options": [str(o) for o in (request.get("options") or [])],
        "request_id": str(request.get("id")),
        "multi_select": False,
    }


def _permission_reply(choice: str) -> str:
    """allow → "once", deny → "reject". **Never "always".**

    OpenCode's PermissionV2Reply also accepts `always`, and nothing here may
    reach for it: `decide_permission` resolves the answer to ONE question, so
    upgrading an enthusiastic "yes always" into a standing grant would hand
    out a permission the user was never asked for — a mode change made on
    their behalf, and one OpenCode gives no way to revoke by voice.

    Ambiguity raises rather than guessing, so nothing is sent at all and
    tools.py's ValueError path re-asks the user.
    """
    decision = decide_permission(choice)
    if decision == "allow":
        return REPLY_ONCE
    if decision == "deny":
        return REPLY_REJECT
    raise ValueError(
        "I couldn't tell if that means allow or deny — please say allow or deny.")


def _question_reply(choice: str, options: list[str]) -> str:
    """The offered option the user picked, or their own words.

    Matched case-insensitively so a spoken "mobile" becomes the "Mobile"
    OpenCode offered. `decide_permission` must never gate this: "no" is a
    legitimate answer to "Ship it?", and running an answer through a
    permission gate would turn it into a refusal to answer. Free text falls
    through unchanged, exactly as the Claude path lets the user answer a
    question in their own words.
    """
    text = (choice or "").strip()
    if not text:
        # Nothing to forward. A ValueError so the model re-asks, rather than
        # sending an empty answer OpenCode would have to interpret.
        raise ValueError("That answer was empty — please say which option you want.")
    for option in options:
        if str(option).strip().lower() == text.lower():
            return str(option)
    return text


async def _gather(*coros: Coroutine) -> list[Any]:
    """`asyncio.gather` that cannot leave an unretrieved exception behind.

    Plain `gather` raises the first failure but does not cancel its siblings,
    so a second failure surfaces later as an "exception was never retrieved"
    warning on a thread nobody is watching. Collect everything, then re-raise.
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return list(results)


async def _cancel_pending() -> None:
    """Cancel everything a timed-out bridged call left running, so stopping the
    loop cannot produce a "Task was destroyed but it is pending" warning."""
    me = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not me]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class OpenCodeProvider(AgentProvider):
    id = "opencode"
    name = "OpenCode"
    THREAD_NAME = "opencode-provider-loop"

    def __init__(self, server: OpenCodeServer, *, default_model: str | None = None) -> None:
        self._server = server
        self._default_model = default_model or None
        self._handles: dict[str, _Handle] = {}
        self._observer: Observer | None = None
        self._health: tuple[float, AgentHealth] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._guard = threading.Lock()      # guards loop creation, not the loop

    @property
    def server(self) -> OpenCodeServer:
        """The server this provider acquires through. Read-only.

        Public so that a caller checking whether construction stayed lazy
        (`provider.server.client is None`) does not have to reach into a
        private attribute to do it. Nothing here acquires.
        """
        return self._server

    # --- the sync/async bridge -------------------------------------------
    #
    # One mechanism, used by every method. See the module docstring for why it
    # is a private loop on a private thread rather than the caller's loop.

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._loop is not None:
                return self._loop
            if self._closed:
                raise OpenCodeError("the OpenCode provider has been shut down")
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            thread = threading.Thread(target=_run, name=self.THREAD_NAME, daemon=True)
            thread.start()
            if not ready.wait(TEARDOWN_TIMEOUT_S):
                raise OpenCodeError("the OpenCode provider's event loop did not start")
            self._loop, self._thread = loop, thread
            return loop

    def _submit(self, coro: Coroutine):
        """Hand a coroutine to the provider's loop. Closes it on failure so a
        rejected submission cannot leave a never-awaited coroutine warning."""
        if self._thread is not None and threading.current_thread() is self._thread:
            # Would deadlock: the thread that must run this is the one waiting.
            coro.close()
            raise OpenCodeError("an OpenCode provider call re-entered its own event loop")
        try:
            loop = self._ensure_loop()
        except BaseException:
            coro.close()
            raise
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def _run(self, coro: Coroutine, timeout: float = CALL_TIMEOUT_S) -> Any:
        """Sync entry point: block on one bridged call."""
        future = self._submit(coro)
        try:
            return future.result(timeout)
        except TimeoutError as exc:
            future.cancel()
            raise OpenCodeError(
                f"OpenCode did not respond within {timeout:.0f}s") from exc

    async def _arun(self, coro: Coroutine, timeout: float = CALL_TIMEOUT_S) -> Any:
        """Async entry point: the same loop, awaited instead of blocked on."""
        future = self._submit(coro)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            future.cancel()
            raise OpenCodeError(
                f"OpenCode did not respond within {timeout:.0f}s") from exc

    def _teardown_loop(self) -> None:
        with self._guard:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
            self._closed = True
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                _cancel_pending(), loop).result(TEARDOWN_TIMEOUT_S)
        except Exception:
            log.debug("cancelling the OpenCode provider's pending work failed",
                      exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=TEARDOWN_TIMEOUT_S)
            if thread.is_alive():
                # Closing a running loop raises, and leaving it open is the
                # lesser of the two leaks. Say so rather than mask it.
                log.error("the OpenCode provider's event loop thread did not stop")
                return
        loop.close()

    # --- HTTP, all of it on the provider's loop ---------------------------

    async def _client(self) -> OpenCodeClient:
        return await self._server.acquire()

    async def _history(self, client: OpenCodeClient, handle: str,
                       after: int) -> list[dict[str, Any]]:
        data = await client.get(f"/api/session/{handle}/history", after=after)
        return [e for e in (data or []) if isinstance(e, dict)]

    async def _messages(self, client: OpenCodeClient,
                        handle: str) -> list[dict[str, Any]]:
        data = await client.get(f"/api/session/{handle}/message")
        return [m for m in (data or []) if isinstance(m, dict)]

    async def _asks(self, client: OpenCodeClient, handle: str,
                    kind: str) -> list[dict[str, Any]]:
        """The session's pending permissions or questions, oldest first."""
        data = await client.get(f"/api/session/{handle}/{kind}")
        return [r for r in (data or []) if isinstance(r, dict)]

    # --- contract ---------------------------------------------------------

    def capabilities(self) -> AgentCapabilities:
        # Every field is a claim the shared contract now checks against
        # behaviour: permission_modes=() is what makes set_mode's
        # NotImplementedError honest rather than a lie, and
        # supports_events=False routes narration down the poll-owned path.
        return AgentCapabilities(
            interactive_terminal=False, slash_commands=False, send_keys=False,
            permission_modes=(), supports_interrupt=True,
            supports_rehydrate=True, supports_resume=False,
            supports_events=False, cost_tracking=True)

    async def health(self) -> AgentHealth:
        """A pure probe, deliberately — never `acquire()`.

        A UI that merely rendered an agent list polls health every 30s. If this
        acquired the server, that poll would run `opencode serve` for a user
        who never asked for a session, destroying the laziness server.py exists
        to give. `ClaudeCodeProvider.health()` is a bare `claude --version` for
        the same reason. The 30s cache is the second guard.
        """
        now = time.monotonic()
        if self._health is not None and now - self._health[0] < HEALTH_TTL_S:
            return self._health[1]
        url = self._server.url
        try:
            online = bool(await self._arun(self._server.is_reachable()))
        except Exception as exc:
            # is_reachable swallows its own failures; this catches a bridge
            # that could not start. Offline, never a crash: plan 41 keeps a
            # broken provider from degrading anything else.
            log.warning("the OpenCode health probe could not run: %s", exc)
            online, detail = False, f"OpenCode could not be probed at {url}: {exc}"
        else:
            if online:
                how = ("a server Yuri started" if self._server.owned
                       else "an existing server Yuri attached to" if self._server.client
                       else "reachable; not acquired yet")
                detail = f"OpenCode at {url} answered · {how}"
            else:
                # Not answering is not the same as unable to serve. With
                # spawning allowed and the binary present, the next session
                # starts one — so `online` (which is what the connect-time
                # AGENTS block and the voice prompt gate on) has to say yes.
                # Reporting offline here made Yuri refuse the first "use
                # OpenCode" of every boot and offer Claude Code instead, in
                # the DEFAULT configuration, for an agent that works.
                spawnable, why = self._server.can_spawn
                online = spawnable
                detail = (f"OpenCode is not running at {url} yet · {why} · Yuri "
                          "will start one when a session needs it" if spawnable
                          else f"OpenCode did not answer at {url} — {why}")
        health = AgentHealth(online=online, version=None, detail=detail)
        self._health = (now, health)
        return health

    async def create_session(self, project: ProjectContext, opts: SessionOptions) -> str:
        body: dict[str, Any] = {"location": {"directory": project.root_path}}
        model = opts.model or self._default_model
        ref = _model_ref(model)
        if ref is not None:
            body["model"] = ref
        data = await self._arun(self._create(body))
        handle = str(data.get("id") or "")
        if not handle:
            raise OpenCodeError("OpenCode created a session without an id")
        location = data.get("location") or {}
        self._handles[handle] = _Handle(
            cwd=str(location.get("directory") or project.root_path), model=model)
        return handle

    async def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        client = await self._client()
        data = await client.post("/api/session", body)
        return data if isinstance(data, dict) else {}

    def send_message(self, handle: str, message: str) -> None:
        """Queue a prompt. Non-blocking in the sense that matters: OpenCode's
        `/prompt` admits the message and returns immediately rather than
        awaiting the turn, so this costs one round trip, not minutes."""
        h = self._get(handle)
        data = self._run(self._prompt(handle, message))
        admitted = data.get("admittedSeq")
        if isinstance(admitted, int) and admitted - 1 < h.cursor:
            # Rewind just far enough that the admitted event itself is read
            # back. Only ever backwards: a forward jump would skip events.
            h.cursor = admitted - 1
        h.in_flight = True

    async def _prompt(self, handle: str, message: str) -> dict[str, Any]:
        client = await self._client()
        # delivery "queue" (never "steer"): a rapid second tell_claude must not
        # drop the first, which is what the voice prompt already promises.
        data = await client.post(f"/api/session/{handle}/prompt",
                                 {"prompt": {"text": message}, "delivery": "queue"})
        return data if isinstance(data, dict) else {}

    def answer(self, handle: str, choice: str) -> None:
        """Reply to the ask `poll` last surfaced.

        Every refusal here is a `ValueError`, which tools.py already turns
        into a soft error the voice model recovers from by re-asking — the
        right shape for "I could not tell what you meant" and for "that
        request is gone", neither of which is a crash.
        """
        h = self._get(handle)
        pending = h.pending
        if pending is None:
            # Fails closed on purpose. There may well be a request pending on
            # the server that this provider has not surfaced yet (a poll away,
            # or lost to a restart); answering it anyway would apply a spoken
            # "yes" to something the user was never actually asked about.
            raise ValueError(
                "OpenCode has no pending question or permission for this session to answer.")
        self._run(self._answer(handle, h, pending, choice))

    async def _answer(self, handle: str, h: _Handle, pending: _Pending,
                      choice: str) -> None:
        client = await self._client()
        # Re-read the pending list first: the remembered id can die between
        # poll and answer (answered in OpenCode's own UI, expired, or the
        # session moved on). OpenCode's behaviour for a reply to a dead id is
        # not something the live probe pinned down, and the two plausible ones
        # are both bad — a 404 that surfaces as a hard OpenCodeError, or a
        # cheerful 200 that reports success for an approval nobody applied.
        # One extra round trip, once per approval, buys a soft re-ask instead.
        live = await self._asks(client, handle, pending.kind)
        request = next((r for r in live
                        if str(r.get("id") or "") == pending.request_id), None)
        if request is None:
            h.pending = None
            raise ValueError(
                f"That {pending.kind} is no longer waiting on an answer — "
                "it was already handled or it expired.")

        if pending.kind == PERMISSION:
            # THE RULE (design spec section 7): allow -> "once", deny ->
            # "reject", never "always". Raises on anything ambiguous, before
            # any request is sent.
            body = {"reply": _permission_reply(choice)}
        else:
            options = [str(o) for o in (request.get("options") or [])]
            body = {"reply": _question_reply(choice, options)}

        await client.post(
            f"/api/session/{handle}/{pending.kind}/{pending.request_id}/reply", body)
        h.pending = None
        # Answered means the agent is moving again, so the next poll reports
        # "working" rather than "idle" — and any completion that was deferred
        # behind the ask is read on that same poll, because neither mark moved.
        h.in_flight = True

    def poll(self, handle: str) -> dict[str, Any]:
        """Advance the cursor and map what arrived.

        Synchronous by contract (the voice model polls; a turn can take
        minutes), but the work is HTTP — so it runs on the provider's own loop
        the same way every other method does.

        Two properties this keeps, both tested: the cursor advances only past
        events actually returned, so a completed turn is never reported twice;
        and an unrecognised event type advances the cursor without being
        fatal, because a successful turn's vocabulary is only partly known
        (design spec section 2.1).
        """
        h = self._get(handle)
        return self._run(self._poll(handle, h))

    async def _poll(self, handle: str, h: _Handle) -> dict[str, Any]:
        client = await self._client()
        # Every read at once: poll runs on Yuri's own 1.5s tick, so the two
        # ask endpoints must cost latency, not round trips. Messages are only
        # worth fetching while a turn is in flight — that gate is also what
        # stops a rehydrated session's old replies being read as new ones.
        reads: list[Coroutine] = [self._asks(client, handle, PERMISSION),
                                  self._asks(client, handle, QUESTION),
                                  self._history(client, handle, h.cursor)]
        if h.in_flight:
            reads.append(self._messages(client, handle))
        permissions, questions, events, *rest = await _gather(*reads)
        # None means NOT FETCHED, which is not the same claim as "the session
        # has no messages". Conflating them let the failure branch below read
        # len([]) == 0 as the true count and rewind msg_seen to zero.
        messages = rest[0] if rest else None

        out: dict[str, Any] = {"session_id": handle}
        ask = self._ask(h, permissions, questions)
        if ask is not None:
            # Deliberately BEFORE the cursor advances and without touching
            # msg_seen: an ask outranks history but must not consume it. Both
            # marks are high-water marks over state the server still holds, so
            # a completion or a failure waiting behind this ask is deferred to
            # the poll after it clears — reported once, from the same marks —
            # rather than swallowed by an early return.
            return {**out, **ask}

        # Only past what actually came back. This one line is the exactly-once
        # property; an unmapped type is carried past by it, not by a branch.
        highest = max((_seq_of(e) for e in events), default=0)
        if highest > h.cursor:
            h.cursor = highest

        failure = _failure_in(events)
        if failure is not None:
            h.in_flight = False
            # Consume the messages too: a half-written reply must not resurface
            # as the next turn's completion. Only when we actually read them --
            # a failure arriving while no turn is in flight (an interrupted
            # step, or the first poll after a restart) fetched nothing, and
            # setting msg_seen to 0 there would mark every reply the user has
            # already heard as unread, re-narrating the whole session on the
            # next completion. And because this branch persists, it would
            # survive the restart too.
            if messages is not None:
                h.msg_seen = len(messages)
            return {**out, "status": "error", "error": failure}

        if messages is None:            # no turn in flight: nothing to complete
            return {**out, "status": "idle"}

        fresh = messages[h.msg_seen:]
        if any(m.get("type") == "assistant" and m.get("finish") for m in fresh):
            h.in_flight = False
            h.msg_seen = len(messages)
            return {**out, "status": "completed",
                    "assistant_text": _assistant_text(fresh)[:MAX_ASSISTANT_TEXT]}

        return {**out, "status": "working" if h.in_flight else "idle"}

    @staticmethod
    def _ask(h: _Handle, permissions: list[dict[str, Any]],
             questions: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The pending request to report, and remember for `answer`.

        Permissions outrank questions when both are waiting. Pinned in that
        order because a permission gates a side effect the agent is blocked
        on, carries the risk label, and holds the domain's one-pending-
        approval slot; a question only shapes what happens next, and it is
        still there — surfaced by the very next poll — once the permission is
        answered.
        """
        request = _first_request(permissions)
        if request is not None:
            prompt = _permission_prompt(request)
            return _surface(h, PERMISSION, "needs_permission", prompt)
        request = _first_request(questions)
        if request is not None:
            prompt = _question_prompt(request)
            return _surface(h, QUESTION, "needs_choice", prompt)
        # Nothing pending server-side: forget whatever we were holding. A
        # request answered in OpenCode's own UI must not stay answerable here.
        h.pending = None
        return None

    async def interrupt(self, handle: str) -> None:
        h = self._get(handle)
        seen = await self._arun(self._interrupt(handle))
        # Only after OpenCode accepted it: a failed interrupt leaves the turn
        # running, and reporting idle for a live turn is the worse lie.
        h.in_flight = False
        # Consume the abandoned turn's messages, exactly as the error path
        # does. An interrupt is when a half-written reply is MOST likely to be
        # sitting there unfinished, and without this it survives to be glued
        # onto the next turn's completion -- so Yuri would narrate a sentence
        # the agent never finished saying, attributed to a different question.
        if seen is not None:
            h.msg_seen = seen

    async def _interrupt(self, handle: str) -> int | None:
        client = await self._client()
        await client.post(f"/api/session/{handle}/interrupt", {})
        # After the interrupt lands, so anything written while it was in
        # flight is consumed too.
        try:
            return len(await self._messages(client, handle))
        except OpenCodeError:
            # The interrupt itself succeeded; failing to read the count back
            # is not a reason to report the turn still running.
            return None

    async def stop(self, handle: str) -> None:
        """Forget the handle. Deliberately no delete: OpenCode sessions are
        durable and the user may resume one, so destroying it is not ours to
        do — the same instinct as never stopping a server Yuri did not start."""
        self._get(handle)
        self._handles.pop(handle, None)

    async def set_mode(self, handle: str, mode: str) -> str:
        raise NotImplementedError(
            "OpenCode has no permission modes, so there is nothing to switch — "
            "unlike Claude Code it has no plan/acceptEdits equivalent.")

    async def send_keys(self, handle: str, items: list[dict]) -> dict[str, Any]:
        raise NotImplementedError(
            "OpenCode runs headless over HTTP, with no terminal to send keys to.")

    def run_slash(self, handle: str, text: str) -> None:
        raise NotImplementedError(
            "OpenCode has no slash commands; say what you want instead.")

    async def resume(self, native_session_id: str, project: ProjectContext,
                     opts: SessionOptions) -> str:
        raise NotImplementedError(
            "OpenCode sessions are re-adopted at startup rather than resumed on "
            "demand; ask to rehydrate instead.")

    async def read(self, handle: str) -> str:
        self._get(handle)
        messages = await self._arun(self._read(handle))
        return _assistant_text(messages)

    async def _read(self, handle: str) -> list[dict[str, Any]]:
        client = await self._client()
        return await self._messages(client, handle)

    def resume_command(self, handle: str) -> str | None:
        """None -- OpenCode 1.18.25 offers no way to reopen ONE session.

        This looked solved: `opencode attach <url> --session <id>` is
        documented, and the frontend used to render `claude --resume <id>` for
        every provider, which handed an OpenCode user a Claude command. But
        measured against 1.18.25, `--session` does not open that session's
        conversation -- not via `attach`, not via the root TUI, not with
        --mini, --continue, --dir, or a longer wait. Every route lands in a NEW
        session (its own rename dialog says so). A command that claims to
        reopen this session and quietly opens a different one is worse than no
        command, so the panel offers none. See the verification doc.
        """
        return None

    async def peek(self, handle: str, lines: int = 40) -> str | None:
        """None: there is no TUI to snapshot, exactly as the SDK backend does."""
        return None

    def list_native(self) -> list[dict[str, Any]]:
        if not self._handles:
            # Nothing registered means nothing to report, and no reason to hit
            # the network — which also makes this safe after shutdown().
            return []
        sessions = self._run(self._sessions())
        by_id = {str(s.get("id")): s for s in sessions if isinstance(s, dict)}
        out: list[dict[str, Any]] = []
        for handle, h in self._handles.items():
            session = by_id.get(handle)
            if session is None:
                # The server no longer has it. SessionService marks the row
                # lost from its own view; the provider simply does not claim it.
                continue
            location = session.get("location") or {}
            model = session.get("model")
            out.append({
                "handle": handle, "session_id": handle,
                "cwd": str(location.get("directory") or h.cwd),
                "model": _model_name(model) or (h.model or ""),
                "mode": "",                     # OpenCode has no modes
                "status": "working" if h.in_flight else "idle",
                "cost_usd": round(float(session.get("cost") or 0.0), 4),
                "queued": 0,                    # /prompt queues server-side
                "backend": self.id,
            })
        return out

    async def _sessions(self) -> list[Any]:
        client = await self._client()
        data = await client.get("/api/session")
        return list(data or [])

    # --- restart ----------------------------------------------------------

    async def rehydrate(self, known: dict[str, dict] | None = None) -> list[dict[str, Any]]:
        """Re-adopt the durable sessions Yuri was actually running.

        OpenCode sessions outlive her — they are the server's, not the
        process's — so unlike an SDK session they can be picked back up. Two
        rules govern that, and both are the point of this method:

        **1. A session the server has and Yuri has no row for is left alone.**
        `known` is what she has rows for; anything else on that server may be
        the user's own OpenCode work, and adopting it would put her in charge
        of something she was never asked to run — the same instinct as never
        stopping a server she did not start. So this iterates the server's
        list and keeps only the intersection, and a caller with nothing known
        does not even reach the network.

        **2. Both marks come back with the session, or she re-narrates.**
        `cursor` makes events exactly-once and `msg_seen` makes completions
        exactly-once; a handle restored with `msg_seen = 0` reports the reply
        the user already heard as the *next* turn's completion. See
        `_restored_mark` for what a missing or impossible mark restores to.

        `pending` is deliberately not restored. OpenCode owns the pending
        list, so the first poll re-derives any unanswered ask from the server;
        a remembered request id could outlive the request it names.

        An unreachable server logs and returns nothing rather than raising: a
        dead OpenCode must not break Yuri's startup (design spec section 41).
        """
        if not known:
            return []
        # Attach only, never spawn. main.py awaits this inside the lifespan,
        # before the app serves anything, and _client() would otherwise
        # acquire -- so a spawn-enabled OpenCode that is merely not running
        # would be STARTED by Yuri's own startup, blocking the whole boot for
        # the readiness timeout if it never came up. Design spec section 4 is
        # explicit that nothing runs `opencode serve` at startup, and Task 4
        # kept health() out of acquire for the same reason.
        if not await self._arun(self._server.is_reachable()):
            log.info("OpenCode is not running; nothing re-adopted (it will be "
                     "started when a session needs it)")
            return []
        try:
            sessions = await self._arun(self._sessions())
        except Exception as exc:
            log.warning("OpenCode could not be enumerated for rehydrate: %s", exc)
            return []
        restored: list[dict[str, Any]] = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            handle = str(session.get("id") or "")
            if not handle or handle not in known:
                continue                      # RULE 1: not hers; leave it alone.
            try:
                restored.append(await self._arun(
                    self._readopt(handle, known[handle] or {}, session)))
            except OpenCodeError as exc:
                # One unreadable session must not cost the others their marks.
                # Narrow on purpose: OpenCodeError is what a failed read raises,
                # and swallowing anything wider here would turn a bug in this
                # method into a session that is quietly never re-adopted.
                log.warning("OpenCode session %s could not be re-adopted: %s",
                            handle[:12], exc)
        return restored

    async def _readopt(self, handle: str, meta: dict,
                       session: dict[str, Any]) -> dict[str, Any]:
        """Register one restored handle, and work out where to resume reading.

        Both reads are full ones — `after=0` and the whole message list — and
        both are needed even when a mark was stored, because the ceiling a
        stored mark is clamped against is exactly what the server still holds.
        The cost is one round trip per session Yuri owns, once, at startup.
        """
        client = await self._client()
        events, messages = await _gather(self._history(client, handle, 0),
                                         self._messages(client, handle))
        location = session.get("location") or {}
        # The row's cwd first: it is the path Yuri already validated against her
        # allowed roots. The server's own answer is the fallback.
        cwd = str(meta.get("cwd") or location.get("directory") or "")
        model = _model_name(session.get("model")) or None
        self._handles[handle] = _Handle(
            cwd=cwd, model=model,
            cursor=_restored_mark(meta.get("opencode_cursor"),
                                  max((_seq_of(e) for e in events), default=0)),
            msg_seen=_restored_mark(meta.get("opencode_msg_seen"), len(messages)))
        # Runner-shaped, like list_native: SessionService reads cwd/backend off
        # this for any handle it turns out to have no row for.
        return {"handle": handle, "session_id": handle, "cwd": cwd,
                "model": model or "", "mode": "", "status": "idle",
                "cost_usd": round(float(session.get("cost") or 0.0), 4),
                "queued": 0, "backend": self.id}

    def cursor_for(self, handle: str) -> int:
        """The highest `durable.seq` consumed for this session. KeyError for a
        handle this provider does not hold, exactly as `poll` does."""
        return self._get(handle).cursor

    def runtime_metadata_for(self, handle: str) -> dict[str, Any]:
        """Both marks, for `SessionService.poll` to merge onto the session row.

        This is the write half of rehydration: without it the marks would live
        only in memory, every restart would fall back to "from now", and the
        exactly-once properties the cursor exists for would survive a poll but
        not a reboot.

        An unknown handle answers `{}` rather than raising: this is read on
        every poll tick, and a session the provider no longer holds must not
        turn a poll into an exception.
        """
        h = self._handles.get(handle)
        if h is None:
            return {}
        return {"opencode_cursor": h.cursor, "opencode_msg_seen": h.msg_seen}

    def set_observer(self, cb: Observer | None) -> None:
        """Stored and never invoked: `supports_events=False`, because we poll
        the cursor. Storing rather than raising keeps build_container's uniform
        wiring working across providers."""
        self._observer = cb

    async def shutdown(self) -> None:
        self._handles.clear()
        self._health = None
        if self._loop is None and self._server.client is None:
            # Never used: no loop to tear down, and nothing was acquired, so
            # there is nothing to release either.
            self._closed = True
            return
        try:
            # release() stops the process only when Yuri started it. An
            # attached server survives her shutdown, her restart and her crash.
            await self._arun(self._server.release(), timeout=TEARDOWN_TIMEOUT_S * 2)
        except Exception:
            log.warning("releasing the OpenCode server failed", exc_info=True)
        finally:
            self._teardown_loop()

    # --- internals ---------------------------------------------------------

    def _get(self, handle: str) -> _Handle:
        h = self._handles.get(handle)
        if h is None:
            # KeyError, not ValueError: the contract requires it, and callers
            # distinguish "no such session" from "bad request".
            raise KeyError(f"unknown OpenCode session: {handle}")
        return h


def _model_name(model: Any) -> str:
    """`{providerID, id}` back to `"provider/model"` for display."""
    if isinstance(model, dict):
        provider, ident = model.get("providerID"), model.get("id")
        if provider and ident:
            return f"{provider}/{ident}"
        return str(ident or provider or "")
    return str(model or "")
