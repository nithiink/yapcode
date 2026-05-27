"""Backend runtime configuration.

Single place where environment-driven settings are resolved: each setting reads
its environment variable when present, otherwise falls back to the default
defined here. `.env` is loaded first so values placed there are honored
regardless of which module imports config first.
"""
from __future__ import annotations

import os

try:  # dotenv is present in the venv; stay importable without it (e.g. in tests)
    from dotenv import load_dotenv

    # override=True so .env is authoritative: empty/stale vars exported in the
    # shell environment must NOT shadow the values configured in .env.
    load_dotenv(override=True)
except Exception:
    pass


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var. Missing/empty -> default; otherwise truthy values
    are 1/true/yes/on (case-insensitive), everything else is False."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Repo root = parent of this backend/ directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Where interactive CLI session control dirs live (meta.json, events.jsonl,
# mode, settings.json, decisions/) — the on-disk storage rehydration reads.
# Defaults to a folder inside the project so it's easy to find, inspect, and
# edit (gitignored). Override with VC_SESSION_STORE to point anywhere, e.g.
# back to ~/.voice-claude/tmux.
SESSION_STORE_DIR: str = os.path.abspath(os.path.expanduser(
    os.getenv("VC_SESSION_STORE") or os.path.join(_REPO_ROOT, ".voice-claude", "tmux")
))


# On backend shutdown, whether to KILL interactive CLI sessions (the old
# destroy-on-exit behavior) or DETACH and leave them running so the next startup
# can rehydrate them.
#
# Default: False -> detach ("let go, don't destroy"), so a restart preserves
# sessions. Set VC_KILL_SESSIONS_ON_SHUTDOWN=1 to restore the old behavior where
# stopping the backend kills every CLI session and removes its control dir.
KILL_SESSIONS_ON_SHUTDOWN: bool = _env_bool("VC_KILL_SESSIONS_ON_SHUTDOWN", False)
