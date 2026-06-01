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
_names: dict[str, str] = {}  # handle -> human-readable display name


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


def backend_of(handle: str) -> str | None:
    """Which backend owns this handle ('cli' or 'sdk'), or None if unknown."""
    return _owner.get(handle)


def runner_for(handle: str) -> ClaudeRunner:
    backend = _owner.get(handle)
    if backend is not None:
        return get_runner(backend)
    # Fallback: locate the handle among already-instantiated backends.
    for r in _runners.values():
        if any(s["handle"] == handle for s in r.list()):
            return r
    raise KeyError(f"unknown session: {handle}")


def _raw_sessions() -> list[dict]:
    """Backend-tagged session dicts straight from each runner (no name added)."""
    out: list[dict] = []
    for backend, r in _runners.items():
        for s in r.list():
            out.append({**s, "backend": backend})
    return out


def list_all_sessions() -> list[dict]:
    return [{**s, "name": _names.get(s["handle"])} for s in _raw_sessions()]


# --- human-readable session names -------------------------------------------

def default_name_for(cwd: str) -> str:
    """A friendly default name for a new session: the folder basename, de-duped
    against names already in use (e.g. 'Development', 'Development 2')."""
    base = os.path.basename(os.path.normpath(cwd)) or "session"
    taken = {n.lower() for n in _names.values()}
    if base.lower() not in taken:
        return base
    i = 2
    while f"{base} {i}".lower() in taken:
        i += 1
    return f"{base} {i}"


def set_session_name(handle: str, name: str) -> str:
    """Assign a display name to a session. Names must be unique (case-insensitive)
    among live sessions; raises ValueError listing current names on a clash."""
    handle = resolve_session(handle)
    name = " ".join((name or "").split())  # collapse whitespace
    if not name:
        raise ValueError("name cannot be empty")
    for h, existing in _names.items():
        if h != handle and existing.lower() == name.lower():
            raise ValueError(
                f"the name '{name}' is already used by another session; pick a different one"
            )
    _names[handle] = name
    # Let the owning backend persist the name (CLI writes it to meta.json so it
    # survives a restart); SDK has no persistence and simply lacks the method.
    persist = getattr(runner_for(handle), "persist_name", None)
    if persist is not None:
        try:
            persist(handle, name)
        except Exception:
            pass
    return name


def resolve_session(ref: str) -> str:
    """Resolve a session reference — a display name, a full handle, or an 8-char
    handle prefix — to the canonical handle. Exact handle wins, then a
    case-insensitive name match, then a unique handle prefix."""
    ref = (ref or "").strip()
    handles = {s["handle"] for s in _raw_sessions()}
    if ref in handles:
        return ref
    low = ref.lower()
    for h, name in _names.items():
        if h in handles and name.lower() == low:
            return h
    prefix_hits = [h for h in handles if h.startswith(ref)]
    if len(prefix_hits) == 1:
        return prefix_hits[0]
    names = sorted(_names[h] for h in handles if h in _names)
    raise KeyError(
        f"no session matches '{ref}'. Active session names: {names or '(none named yet)'}."
    )


def cli_pane_for(handle: str) -> str | None:
    """tmux pane to attach a live browser terminal to (CLI backend only)."""
    r = _runners.get("cli")
    pane_for = getattr(r, "pane_for", None)
    return pane_for(handle) if pane_for else None


async def set_session_mode(handle: str, mode: str) -> str:
    """Switch a session's permission mode (plan/auto/acceptEdits/default).
    Returns the mode actually in effect afterward."""
    return await runner_for(handle).set_mode(handle, mode)


async def close_session(handle: str) -> None:
    """End a single session (kill its CLI/tmux pane or disconnect its SDK client)
    and forget it. Leaves other sessions running."""
    r = runner_for(handle)
    await r.close(handle)
    _owner.pop(handle, None)
    _names.pop(handle, None)


async def peek_session(handle: str, lines: int = 40) -> dict:
    """Snapshot the live screen of a session. CLI backend returns the raw tmux
    pane; SDK has no TUI, so it falls back to the accumulated assistant text."""
    r = runner_for(handle)
    peek = getattr(r, "peek", None)
    if peek is not None:
        return {"session_id": handle, "screen": await peek(handle, lines)}
    text = await r.read(handle)
    return {"session_id": handle, "screen": text or "(no output yet)",
            "note": "SDK backend has no live screen; showing accumulated text."}


async def rehydrate_cli_sessions() -> list[dict]:
    """On startup, re-adopt interactive CLI sessions that survived a previous
    backend (their tmux panes keep running independently). Repopulates ownership
    and names. Best-effort: failures are logged by the caller, never fatal.
    Only the CLI backend can rehydrate — SDK subprocesses die with the backend."""
    runner = get_runner("cli")
    rehydrate = getattr(runner, "rehydrate", None)
    if rehydrate is None:
        return []
    restored = await rehydrate()
    for s in restored:
        handle = s["handle"]
        register_owner(handle, "cli")
        name = s.get("name")
        if name:
            # Defensive de-dupe in case two restored metas carried the same name.
            taken = {n.lower() for h, n in _names.items() if h != handle}
            base, candidate, i = name, name, 2
            while candidate.lower() in taken:
                candidate = f"{base} {i}"
                i += 1
            _names[handle] = candidate
    return restored


async def shutdown_all() -> None:
    for r in _runners.values():
        await r.shutdown()
    _runners.clear()
    _owner.clear()
    _names.clear()


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
    # Fail closed: with no roots configured nothing is "under root". The directory
    # sandbox is mandatory (see resolve_project_path), so this never returns True
    # for an unconfigured server.
    return bool(roots) and any(p == r or p.startswith(r + os.sep) for r in roots)


def resolve_project_path(name: str) -> str:
    """Best-effort resolve a spoken/fuzzy folder reference to an allowed dir.

    Handles: absolute paths, '~', a bare folder name matching a root or one of
    its subdirectories (case-insensitive), and an empty/vague value (defaults to
    the first allowed root). Raises ValueError listing real options on failure.

    SECURITY: every candidate path — including the fuzzy-match branches — is
    realpath-normalized and checked for containment under an allowed root via
    `_contained()` before it can be returned, so inputs like ".." (which
    `os.path.basename` collapses) cannot escape ALLOWED_PROJECT_ROOTS. Roots are
    themselves realpath'd so symlinked roots compare correctly.

    The directory sandbox is MANDATORY: if ALLOWED_PROJECT_ROOTS is not
    configured this raises rather than letting a session start anywhere on the
    filesystem (fail closed — defense in depth alongside the backend's auth).
    """
    roots = [os.path.realpath(r) for r in _allowed_roots()]
    if not roots:
        raise ValueError(
            "No project directories are configured, so I can't start a session. "
            "Set ALLOWED_PROJECT_ROOTS in backend/.env (e.g. "
            "ALLOWED_PROJECT_ROOTS=/Users/you/Development) and restart the backend."
        )
    name = (name or "").strip()

    def _contained(p: str) -> str | None:
        """Realpath `p`; return it iff it's an existing directory under an
        allowed root. Otherwise None."""
        real = os.path.realpath(os.path.abspath(os.path.expanduser(p)))
        if not os.path.isdir(real):
            return None
        return real if _under_root(real, roots) else None

    # Vague / empty -> default to the primary project root.
    if not name or name.lower() in {"anywhere", "any", "home", "default"}:
        if roots and os.path.isdir(roots[0]):
            return roots[0]

    # 1. Direct absolute / ~ path that exists and is allowed.
    hit = _contained(name)
    if hit:
        return hit

    # 2. Fuzzy match against roots and their subdirectories (case-insensitive).
    low = name.lower().strip("/").split("/")[-1]
    for r in roots:
        if os.path.basename(r).lower() == low:
            return r  # the root itself is inherently contained
        hit = _contained(os.path.join(r, os.path.basename(name)))
        if hit:
            return hit
        if os.path.isdir(r):
            for sub in os.listdir(r):
                if sub.lower() == low:
                    hit = _contained(os.path.join(r, sub))
                    if hit:
                        return hit

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
