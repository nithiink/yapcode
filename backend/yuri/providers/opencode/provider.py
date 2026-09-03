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
from .client import OpenCodeClient, OpenCodeError
from .server import OpenCodeServer

log = logging.getLogger("yuri.providers.opencode")

HEALTH_TTL_S = 30.0          # same shape as ClaudeCodeProvider.health()
CALL_TIMEOUT_S = 60.0        # a bridged call is one HTTP round trip; this is the backstop
TEARDOWN_TIMEOUT_S = 5.0
MAX_ASSISTANT_TEXT = 2000    # matches the Claude path's cap (sessions.py, claude_code.py)

# The one event type the live probe actually observed for a failure. Everything
# else is deliberately unmapped: see the module docstring and design spec 2.1.
FAILED_STEP = "session.next.step.failed"


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


def _server_url(server: OpenCodeServer) -> str:
    """The base URL, for diagnostics.

    `OpenCodeServer` exposes `client` (None until acquired) and `owned`, but no
    public URL — and `health()` must not acquire, so there is usually no client
    to ask. Naming the address is most of a health message's value ("did not
    answer at http://127.0.0.1:4096" tells the user what to fix), so read the
    private attribute here rather than change server.py, which this task may
    not touch. Follow-up: give OpenCodeServer a public `url` property.
    """
    client = server.client
    if client is not None:
        return client.base_url
    return str(getattr(server, "_url", "") or "the configured URL")


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
        url = _server_url(self._server)
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
                detail = (f"OpenCode did not answer at {url} — start it with "
                          "`opencode serve`, or check OPENCODE_URL")
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
        """Reply to a pending permission or question.

        `poll` surfaces no pending requests yet — it returns only
        working/idle/completed/error — so by construction there is nothing to
        answer. A ValueError, not a NotImplementedError: OpenCode does have
        the reply endpoints, and tools.py already turns a ValueError into a
        soft error the voice model recovers from by re-asking. Task 5 (design
        spec section 7) adds the pending tracking and the allow→"once",
        deny→"reject" mapping above this.
        """
        self._get(handle)
        raise ValueError(
            "OpenCode has no pending question or permission for this session to answer.")

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
        # Both reads at once: poll must cost one round trip, not two. Messages
        # are only worth fetching while a turn is in flight — that gate is also
        # what stops a rehydrated session's old replies being read as new ones.
        if h.in_flight:
            events, messages = await _gather(self._history(client, handle, h.cursor),
                                             self._messages(client, handle))
        else:
            events = await self._history(client, handle, h.cursor)
            messages = []

        # Only past what actually came back. This one line is the exactly-once
        # property; an unmapped type is carried past by it, not by a branch.
        highest = max((_seq_of(e) for e in events), default=0)
        if highest > h.cursor:
            h.cursor = highest

        out: dict[str, Any] = {"session_id": handle}
        failure = _failure_in(events)
        if failure is not None:
            h.in_flight = False
            # Consume the messages too: a half-written reply must not resurface
            # as the next turn's completion.
            h.msg_seen = len(messages)
            return {**out, "status": "error", "error": failure}

        fresh = messages[h.msg_seen:]
        if any(m.get("type") == "assistant" and m.get("finish") for m in fresh):
            h.in_flight = False
            h.msg_seen = len(messages)
            return {**out, "status": "completed",
                    "assistant_text": _assistant_text(fresh)[:MAX_ASSISTANT_TEXT]}

        return {**out, "status": "working" if h.in_flight else "idle"}

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
