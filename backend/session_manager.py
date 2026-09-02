"""The project-directory sandbox, plus the process's one ClaudeCodeProvider.

What is left here after Phase 3:

* `resolve_project_path` / `list_projects` — the MANDATORY directory sandbox
  every session start goes through. `ProjectService` calls them, so this is the
  single place the allowed-roots rule is implemented (spec §5.2).
* `provider()` / `set_provider()` / `reset()` — the "exactly one
  ClaudeCodeProvider per process" guard. `yuri.app.build_container` installs
  the registry's instance here on startup and `yuri.app.shutdown` clears it;
  two live TmuxClaudeRunners would compete over the same tmux control dirs and
  both rehydrate the same panes.

Everything else that used to live here is GONE. Session lookup, naming, modes,
peeking, closing, rehydration and terminal handoff are owned by
`yuri.services.sessions.SessionService`, which routes per-handle through the
provider — there is no `_runners` map, no `runner_for()` routing and no
`_names` dict any more. Reach for `container().sessions`, not this module.
"""
from __future__ import annotations

import logging
import os

import config
from yuri.providers.claude_code import ClaudeCodeProvider

log = logging.getLogger("yapcode.session_manager")

_provider: ClaudeCodeProvider | None = None


def provider() -> ClaudeCodeProvider:
    """The one ClaudeCodeProvider `build_container` installed.

    There is deliberately NO lazy fallback. Minting one here would give the
    process a SECOND provider — two TmuxClaudeRunners fighting over the same
    tmux control dirs, each rehydrating the same panes — which is precisely the
    hazard this indirection exists to prevent. Production always installs one
    during app startup, so an empty slot means the caller ran outside the app
    lifespan: that is a bug in the caller, and it should say so."""
    if _provider is None:
        raise RuntimeError(
            "session_manager has no provider installed: yuri.app.build_container installs one "
            "during app startup and yuri.app.shutdown clears it, so this call ran outside the "
            "app lifespan. Use container().sessions (or install a provider explicitly in a test) "
            "— a second ClaudeCodeProvider would compete over the same tmux panes.")
    return _provider


def set_provider(p: ClaudeCodeProvider | None) -> None:
    """Install the provider (app startup) or a test double. None resets."""
    global _provider
    _provider = p


def reset() -> None:
    """Drop the process-wide provider slot (app shutdown). Kept separate from
    set_provider so the intent at each call site reads clearly."""
    set_provider(None)


def _allowed_roots() -> list[str]:
    """Delegate to config.allowed_project_roots() so both sandbox entry points
    (this module and config.resolve_within_roots) see the same roots — the
    same env parsing, the same realpath normalization, and Yuri's home."""
    return config.allowed_project_roots()


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
        for root in roots:
            # Kept as a bare `real.startswith(root)` (no `or`, no wrapping the
            # receiver) so CodeQL recognizes it as an allowed-prefix path barrier.
            if real.startswith(root):
                tail = real[len(root):]
                if tail and not tail.startswith(os.sep):
                    continue  # sibling like "<root>-evil", not under the root
                return real if os.path.isdir(real) else None
        return None

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
