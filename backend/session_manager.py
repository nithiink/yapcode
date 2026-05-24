"""Process-wide Claude session registry + terminal-handoff helpers.

Two execution backends share the same ClaudeRunner interface:
  "cli" -> TmuxClaudeRunner (interactive CLI; Max subscription + --chrome)  [default]
  "sdk" -> SDKClaudeRunner  (Claude Agent SDK)
Each session handle is a uuid owned by one backend; `runner_for(handle)` routes
later calls to the owning runner.
"""
from __future__ import annotations

import os
import shlex

from claude_runner import ClaudeRunner, SDKClaudeRunner
from tmux_runner import TmuxClaudeRunner

_runners: dict[str, ClaudeRunner] = {}
_owner: dict[str, str] = {}  # handle -> backend


def get_runner(backend: str = "cli") -> ClaudeRunner:
    backend = (backend or "cli").lower()
    if backend not in ("cli", "sdk"):
        backend = "cli"
    r = _runners.get(backend)
    if r is None:
        r = TmuxClaudeRunner() if backend == "cli" else SDKClaudeRunner()
        _runners[backend] = r
    return r


def register_owner(handle: str, backend: str) -> None:
    _owner[handle] = (backend or "cli").lower()


def runner_for(handle: str) -> ClaudeRunner:
    backend = _owner.get(handle)
    if backend is not None:
        return get_runner(backend)
    # Fallback: locate the handle among already-instantiated backends.
    for r in _runners.values():
        if any(s["handle"] == handle for s in r.list()):
            return r
    raise KeyError(f"unknown session: {handle}")


def list_all_sessions() -> list[dict]:
    out: list[dict] = []
    for backend, r in _runners.items():
        for s in r.list():
            out.append({**s, "backend": backend})
    return out


def cli_pane_for(handle: str) -> str | None:
    """tmux pane to attach a live browser terminal to (CLI backend only)."""
    r = _runners.get("cli")
    pane_for = getattr(r, "pane_for", None)
    return pane_for(handle) if pane_for else None


async def shutdown_all() -> None:
    for r in _runners.values():
        await r.shutdown()
    _runners.clear()
    _owner.clear()


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
