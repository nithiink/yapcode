"""Turning a spoken answer into allow / deny / ask-again.

This lives beside `base.py`, in the one package every provider may import,
because it is a SECURITY gate and every provider needs the same one. It was
originally inside `claude_runner.py`, which imports the Claude Agent SDK,
`config`, `permissions` and `event_log` at module scope -- so a second
provider could only reuse it by dragging all of that in with it, which would
make an OpenCode-only deployment depend on the Claude SDK being importable.

The alternative was a copy per provider. That is worse than it looks: this is
an allow/deny word list behind a gate that fails closed, and two copies of it
would drift silently, in the direction of one provider quietly accepting a
word the other refuses. One source of truth, stdlib only.
"""
from __future__ import annotations

import re

_ALLOW_WORDS = {
    "allow", "yes", "approve", "approved", "y", "ok", "okay", "sure",
    "go", "go ahead", "do it", "yep", "yeah", "confirm", "accept",
    "proceed",  # natural "yes" for the ExitPlanMode plan-approval prompt
}

# Bare declines (no feedback attached) for the plan-approval dialog.
_DENY_WORDS = {"deny", "no", "nope", "decline", "declined", "cancel", "reject", "rejected", "n"}

# Negations that aren't single tokens (caught as substrings, not word matches).
_DENY_PHRASES = ("don't", "do not", "stop")


def decide_permission(choice: str) -> str | None:
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
