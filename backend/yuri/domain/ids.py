"""Shared id/time helpers for domain entities (spec §7). Pure, no I/O."""
from __future__ import annotations

import datetime
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


def utcnow() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
