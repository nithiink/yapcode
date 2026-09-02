"""Yuri's domain EventBus (spec §6.1): one producer, three sinks — the event
repo (persisted via a background writer), live subscribers (SSE), and a bridge
into the existing debug bus so the Activity panel shows Yuri events today.
publish() is sync, non-blocking and never raises — same discipline as
event_log.log_event, so the tmux runner's sync paths can call into it safely."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import event_log
from yuri.domain.event import EventType, YuriEvent
from yuri.store.base import EventRepo

log = logging.getLogger("yuri.events")


def summarize(e: YuriEvent) -> str:
    p = e.payload or {}
    t = e.type
    if t == EventType.MISSION_CREATED:
        return f"mission created: {p.get('title', '')}"
    if t == EventType.MISSION_STATUS_CHANGED:
        return f"mission {p.get('from', '?')} → {p.get('to', '?')}"
    if t == EventType.SESSION_CREATED:
        return f"session created: {p.get('name') or p.get('native_session_id', '')}"
    if t == EventType.SESSION_MESSAGE_SENT:
        return f"→ agent: {str(p.get('message', ''))[:120]}"
    if t == EventType.SESSION_TURN_COMPLETED:
        return f"turn completed: {str(p.get('assistant_text', ''))[:160]}"
    if t == EventType.SESSION_QUESTION:
        return f"question: {p.get('text', '')}"
    if t == EventType.SESSION_INTERRUPTED:
        return "session interrupted"
    if t == EventType.SESSION_STOPPED:
        return "session stopped"
    if t == EventType.TOOL_STARTED:
        return f"tool: {p.get('tool_name', '')}"
    if t == EventType.APPROVAL_REQUESTED:
        return f"needs approval [{p.get('risk', '?')}]: {p.get('description', '')}"
    if t == EventType.APPROVAL_RESOLVED:
        return f"approval {p.get('status', '?')} by {p.get('by', '?')}: {p.get('description', '')}"
    if t == EventType.COST_UPDATED:
        c = p.get("cost_usd")
        return f"cost ${c:.4f}" if isinstance(c, (int, float)) else "cost updated"
    if t == EventType.AGENT_ERROR:
        return f"agent error: {p.get('message', '')}"
    if t == EventType.SESSION_LOST:
        return "session lost (agent process did not survive the restart)"
    if t == EventType.PROJECT_REGISTERED:
        return f"project registered: {p.get('name') or p.get('slug', '')}"
    if t == EventType.MEMORY_REMEMBERED:
        return f"remembered: {str(p.get('fact', ''))[:120]}"
    return t


def bridge_to_event_log(e: YuriEvent) -> None:
    """Mirror a domain event into the debug bus that feeds the Activity panel.

    Only events whose severity is above `debug`. The runners still log their own
    lines for every runtime signal (`tool: Bash` from the hook, the permission
    prompt, the turn complete, the error) with provider-specific detail, so
    mirroring the domain event for the same signal printed EVERY row twice in
    the Activity feed and in the debug JSONL. `debug` is exactly the set that is
    pure duplication — tool.started, session.message_sent, cost.updated (see
    event.DEFAULTS) — and dropping it leaves each signal with one honest row.
    """
    if e.severity == "debug":
        return
    p = e.payload or {}
    session = p.get("session_name") or p.get("native_session_id") or e.session_id
    event_log.log_event("yuri", "ui", e.type, summarize(e), session=session, detail=e.to_dict())


class EventBus:
    def __init__(self, repo: EventRepo | None = None,
                 bridge: Callable[[YuriEvent], None] | None = None):
        self._repo = repo
        self._bridge = bridge
        self._subs: set[asyncio.Queue] = set()
        self._persist_q: "asyncio.Queue[YuriEvent]" = asyncio.Queue(maxsize=20000)
        self._writer: Optional[asyncio.Task] = None

    def publish(self, e: YuriEvent) -> YuriEvent:
        for q in list(self._subs):
            try:
                q.put_nowait(e)
            except asyncio.QueueFull:
                pass
        if self._repo is not None:
            try:
                self._persist_q.put_nowait(e)
            except asyncio.QueueFull:
                log.warning("event persist queue full; dropping %s", e.type)
        if self._bridge is not None:
            try:
                self._bridge(e)
            except Exception:
                log.exception("event bridge failed")
        return e

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def start_writer(self) -> None:
        if self._repo is not None and (self._writer is None or self._writer.done()):
            self._writer = asyncio.create_task(self._write_loop())

    def writer_running(self) -> bool:
        """True when a live writer is consuming the persist queue. Callers must
        check this before `drain()`: with a repo set and no writer, nothing ever
        calls task_done() and `drain()` blocks forever."""
        return self._writer is not None and not self._writer.done()

    async def stop_writer(self) -> None:
        w, self._writer = self._writer, None
        if w is None:
            return
        w.cancel()
        # `asyncio.wait` never re-raises the awaited task's own CancelledError,
        # so the only CancelledError that can escape this await is OUR caller
        # being cancelled — and discarding that would swallow the shutdown's
        # cancellation (the sibling of the bug fixed in app.shutdown's drain).
        # The old `except (asyncio.CancelledError, Exception)` was a bare except
        # in disguise: it also hid a writer that died of a real error.
        await asyncio.wait({w})
        if not w.cancelled() and w.exception() is not None:
            log.error("the event writer exited with an error", exc_info=w.exception())

    async def drain(self) -> None:
        await self._persist_q.join()

    async def _write_loop(self) -> None:
        assert self._repo is not None
        while True:
            e = await self._persist_q.get()
            try:
                await asyncio.to_thread(self._repo.insert, e)
            except Exception:
                log.exception("event persist failed")
            finally:
                self._persist_q.task_done()
