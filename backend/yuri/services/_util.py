"""Shared helper for journal.py and memory.py: both cap a read to the tail of
a file so the most recent lines survive a context budget."""
from __future__ import annotations


def _tail(text: str, cap: int) -> str:
    if cap <= 0:
        return ""
    return text if len(text) <= cap else text[-cap:]
