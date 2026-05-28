"""Append-only cost log for later analysis.

One JSONL file at the repo root (`<repo>/cost-log.jsonl`) holds every cost
event the UI emits: connection start/end and periodic snapshots while a voice
session is active. Each line is a self-contained record so the file can be
tailed, grepped, or fed to a notebook without parsing state.

Path is overridable via `VC_COST_LOG_PATH`. Writes are serialized with an
async lock + a POSIX advisory lock so multiple workers / multiple POSTs in
flight don't tear lines.

Record shapes (all wrapped in `{"ts": iso, ...}` by `append_cost_event`):

  {"kind": "connection_start",
   "connectionId": "...", "provider": "...", "model": "...",
   "backend": "cli|sdk", "costSaver": bool}

  {"kind": "snapshot",
   "connectionId": "...", "provider": "...", "model": "...",
   "voice": {"costUsd", "audioInTokens", "audioCachedTokens", "audioOutTokens",
             "textInTokens", "textCachedTokens", "textOutTokens",
             "cacheHitRate"},
   "claudeSessions": [{"handle", "name", "cwd", "backend", "model", "mode",
                       "status", "cost_usd"}, ...],
   "claudeTotalUsd": float}

  {"kind": "connection_end", ...same as snapshot plus "durationSec": float}
"""
from __future__ import annotations

import asyncio
import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("voice-claude.cost_log")

# Repo root = backend/.. (this file lives in backend/).
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "cost-log.jsonl"
COST_LOG_PATH = Path(os.getenv("VC_COST_LOG_PATH", str(_DEFAULT_PATH)))

_lock = asyncio.Lock()


def _utcnow_iso() -> str:
    # Z suffix instead of +00:00 to match how the frontend emits timestamps elsewhere.
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def append_cost_event(record: dict[str, Any]) -> None:
    """Append one event to the cost log as a single JSONL line.

    A `ts` field is added server-side if absent, so client clock skew can't
    reorder records relative to the file's append order.
    """
    rec = dict(record)
    rec.setdefault("ts", _utcnow_iso())
    line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n"

    async with _lock:
        try:
            COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Open in append mode and take an exclusive advisory lock so concurrent
            # writers (separate processes, e.g. dev autoreload race) don't interleave.
            with open(COST_LOG_PATH, "a", encoding="utf-8") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass  # advisory only — proceed anyway
                try:
                    f.write(line)
                    f.flush()
                finally:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        except Exception:
            log.exception("failed to append cost log entry")
