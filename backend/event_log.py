"""Unified pipeline event bus for live debugging.

Captures every communication across the three hops — the voice model, this
backend, and Claude — into one ordered stream so misbehavior can be traced
end-to-end. Each event feeds three consumers:

  * a bounded in-memory ring buffer (replayed to new SSE subscribers)
  * any number of live SSE subscribers (the in-app "Activity" panel)
  * an append-only JSONL file (debug-log.jsonl) for after-the-fact analysis

`log_event()` is synchronous and non-blocking — safe to call from the tmux
runner's sync code, async handlers, anywhere — so instrumenting a call site is
a one-liner that can never wedge the pipeline. File writes drain on a background
task; a slow SSE consumer drops events rather than blocking the producer.

Event shape (one JSONL line / one SSE `data:` frame):
  {seq, ts, source, dest, kind, session, summary, detail}
    source/dest : voice | backend | claude | user
    kind        : tool_call | tool_result | send | decision | hook |
                  assistant | inject | transcript | error | poll | info
    session     : session handle or name (nullable)
    summary     : short human-readable line
    detail      : arbitrary JSON payload (args, full text, raw hook event, ...)

Env:
  VC_DEBUG_LOG_FILE=0   disable file persistence (stream + buffer only)
  VC_DEBUG_LOG_PATH     override the JSONL path (default <repo>/debug-log.jsonl)
  VC_DEBUG_BUFFER       ring-buffer size (default 3000)
"""
from __future__ import annotations

import asyncio
import datetime
import fcntl
import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("yapcode.events")

# Repo root = backend/.. (this file lives in backend/), matching cost_log.py.
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "debug-log.jsonl"
DEBUG_LOG_PATH = Path(os.getenv("VC_DEBUG_LOG_PATH", str(_DEFAULT_PATH)))
PERSIST = os.getenv("VC_DEBUG_LOG_FILE", "1") != "0"
BUFFER_MAX = int(os.getenv("VC_DEBUG_BUFFER", "3000"))

_buffer: deque[dict] = deque(maxlen=BUFFER_MAX)
_subscribers: set[asyncio.Queue] = set()
_seq = 0
# Bounded so a stalled writer can't grow without limit; oldest writes drop first.
_file_q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=20000)
_writer_task: Optional[asyncio.Task] = None


def _utcnow_iso() -> str:
    # Z suffix to match how the frontend/cost log stamp timestamps elsewhere.
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def log_event(source: str, dest: str, kind: str, summary: str, *,
              session: str | None = None, detail: Any = None) -> dict:
    """Record one pipeline event. Sync + non-blocking and never raises: appends
    to the ring buffer, fans out to live subscribers (dropping on a full/slow
    consumer queue), and enqueues a file write. Returns the record."""
    global _seq
    _seq += 1
    rec = {
        "seq": _seq,
        "ts": _utcnow_iso(),
        "source": source,
        "dest": dest,
        "kind": kind,
        "session": session,
        "summary": (summary if isinstance(summary, str) else str(summary))[:600],
        "detail": detail,
    }
    _buffer.append(rec)
    for q in list(_subscribers):
        try:
            q.put_nowait(rec)
        except asyncio.QueueFull:
            pass  # slow consumer — drop rather than stall the pipeline
    if PERSIST:
        try:
            _file_q.put_nowait(rec)
        except asyncio.QueueFull:
            pass
    return rec


def recent(limit: int = 500) -> list[dict]:
    """The most recent `limit` events from the ring buffer (oldest-first).
    limit <= 0 returns the whole buffer."""
    if limit <= 0 or limit >= len(_buffer):
        return list(_buffer)
    return list(_buffer)[-limit:]


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


async def _writer() -> None:
    """Drain queued events to the JSONL file. fcntl-locked append, mirroring
    cost_log.py, so concurrent writers (dev autoreload race) don't tear lines."""
    while True:
        rec = await _file_q.get()
        try:
            DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False, default=str) + "\n"
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass  # advisory only
                try:
                    f.write(line)
                    f.flush()
                finally:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        except Exception:
            log.exception("debug log file write failed")


def start_writer() -> None:
    """Start the background file writer (idempotent). Call from app startup."""
    global _writer_task
    if PERSIST and (_writer_task is None or _writer_task.done()):
        _writer_task = asyncio.create_task(_writer())


async def stop_writer() -> None:
    global _writer_task
    if _writer_task is not None:
        _writer_task.cancel()
        _writer_task = None
