"""Process-wide Claude session registry + terminal-handoff helpers."""
from __future__ import annotations

import os
import shlex

from claude_runner import ClaudeRunner, SDKClaudeRunner

_runner: ClaudeRunner | None = None


def get_runner() -> ClaudeRunner:
    global _runner
    if _runner is None:
        _runner = SDKClaudeRunner()
    return _runner


def _allowed_roots() -> list[str]:
    raw = os.getenv("ALLOWED_PROJECT_ROOTS", "")
    return [os.path.abspath(os.path.expanduser(p)) for p in raw.split(",") if p.strip()]


def list_projects() -> dict:
    """Allowed roots + their immediate subdirectories, so the voice model can
    discover real paths instead of guessing."""
    roots = _allowed_roots()
    projects: list[dict] = []
    for r in roots:
        if not os.path.isdir(r):
            continue
        for name in sorted(os.listdir(r)):
            p = os.path.join(r, name)
            if os.path.isdir(p) and not name.startswith("."):
                projects.append({"name": name, "path": p})
    return {"roots": roots, "projects": projects}


def _under_root(p: str, roots: list[str]) -> bool:
    return (not roots) or any(p == r or p.startswith(r + os.sep) for r in roots)


def resolve_project_path(name: str) -> str:
    """Best-effort resolve a spoken/fuzzy folder reference to an allowed dir.

    Handles: absolute paths, '~', a bare folder name matching a root or one of
    its subdirectories (case-insensitive), and an empty/vague value (defaults to
    the first allowed root). Raises ValueError listing real options on failure.
    """
    roots = _allowed_roots()
    name = (name or "").strip()

    # Vague / empty -> default to the primary project root.
    if not name or name.lower() in {"anywhere", "any", "home", "default"}:
        if roots and os.path.isdir(roots[0]):
            return roots[0]

    # 1. Direct absolute / ~ path that exists and is allowed.
    direct = os.path.abspath(os.path.expanduser(name))
    if os.path.isdir(direct) and _under_root(direct, roots):
        return direct

    # 2. Fuzzy match against roots and their subdirectories (case-insensitive).
    low = name.lower().strip("/").split("/")[-1]
    for r in roots:
        if os.path.basename(r).lower() == low:
            return r
        cand = os.path.join(r, os.path.basename(name))
        if os.path.isdir(cand):
            return cand
        if os.path.isdir(r):
            for sub in os.listdir(r):
                if sub.lower() == low and os.path.isdir(os.path.join(r, sub)):
                    return os.path.join(r, sub)

    info = list_projects()
    names = [p["name"] for p in info["projects"]][:25]
    raise ValueError(
        f"Could not resolve '{name}'. Allowed roots: {info['roots']}. "
        f"Available projects: {names}. Ask the user to pick one of these names."
    )


def handoff_command(cwd: str, session_id: str | None) -> str | None:
    """The exact line to paste in a terminal to take over this session."""
    if not session_id:
        return None
    return f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"
