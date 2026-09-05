"""Risk classification for approvals (spec §4.3). Reuses permissions.classify
for the safe set; adds a small destructive-pattern list for Bash. Not a policy
engine — a tuple of regexes with tests."""
from __future__ import annotations

import re

from permissions import EDIT_TOOLS, classify

DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p, re.I) for p in (
    # `rm` with a short-flag cluster containing r or f (-f, -r, -rf, -Rf, -vfr…)
    # or the destructive long forms. Short and long flags MUST be separate
    # alternatives: one pattern like `-[a-z]*[rf]` cannot match `--force` (the
    # second dash is not `[a-z]`), which is how `rm --force x` came out
    # `dangerous` while `rm -f x` came out `confirm` — under-flagging
    # destruction, the one direction this classifier must never fail.
    # Deliberately NOT `(-[a-z]+\s+)*-[a-z]*[rf]` (which would also catch
    # `rm -i -f x`): a repeated group wrapping a quantifier is the nested-
    # quantifier shape CodeQL flags as a ReDoS risk, and one extra flag order
    # is not worth reintroducing that. Both patterns below are linear.
    r"\brm\s+-[a-z]*[rf]",
    r"\brm\s+--(recursive|force)\b",
    r"\bsudo\s+rm\b",
    r"\bgit\s+push\b.*(--force|-f)\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bdrop\s+(table|database|schema)\b",
    r"\bmkfs(\.\w+)?\b",
    r">\s*/dev/(sd|nvme|disk|hd)",
    r"\bchmod\s+-R\s+777\b",
    r"\bdd\s+if=",
))


def risk_for(tool_name: str, tool_input: dict | None) -> str:
    kind = classify(tool_name)
    if kind in ("safe", "question"):
        return "safe"
    if tool_name in EDIT_TOOLS:
        return "confirm"
    if tool_name == "Bash":
        cmd = str((tool_input or {}).get("command") or "")
        if any(p.search(cmd) for p in DANGEROUS_PATTERNS):
            return "dangerous"
    return "confirm"
