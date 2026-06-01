"""Backend runtime configuration.

Single place where environment-driven settings are resolved: each setting reads
its environment variable when present, otherwise falls back to the default
defined here. `.env` is loaded first so values placed there are honored
regardless of which module imports config first.
"""
from __future__ import annotations

import os
import re
import secrets

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


# --- Access control ---------------------------------------------------------
#
# The backend turns voice/tool calls into real command execution on this
# machine, so its endpoints (and the live-terminal WebSocket) must not be open
# to anyone who can reach the port. Two layers gate access:
#
#   1. A shared-secret token (VC_AUTH_TOKEN). When set, every sensitive endpoint
#      and the terminal WebSocket require it (header `X-VC-Token` / `Authorization:
#      Bearer` / `?token=`). When UNSET, only loopback (localhost) clients are
#      allowed and any remote caller is refused — so plain `run.sh` on localhost
#      needs zero config, while exposing the server to the LAN (run-network.sh)
#      forces a token to be set first. The token is required even from loopback
#      once configured, so the same-origin Next proxy can't be used to launder a
#      remote attacker's request into a trusted loopback call.
#
#   2. A CORS / WebSocket-Origin allowlist (replaces the old `*`) so a malicious
#      web page in the user's browser can't drive the backend cross-origin.
AUTH_TOKEN: str = (os.getenv("VC_AUTH_TOKEN") or "").strip()


def token_matches(provided: str | None) -> bool:
    """Constant-time compare of a presented token against VC_AUTH_TOKEN.
    Always False when no token is configured (callers fall back to loopback)."""
    if not AUTH_TOKEN or not provided:
        return False
    return secrets.compare_digest(provided, AUTH_TOKEN)


def _default_allowed_origins() -> list[str]:
    """Exact frontend origins that may call the backend cross-origin (the live
    terminal WS / debug stream are browser-direct). Localhost dev ports by
    default; extend with VC_ALLOWED_ORIGINS (comma-separated)."""
    base = [
        "http://localhost:3000", "http://127.0.0.1:3000",
        "https://localhost:3000", "https://127.0.0.1:3000",
    ]
    extra = [o.strip() for o in (os.getenv("VC_ALLOWED_ORIGINS") or "").split(",") if o.strip()]
    # De-dupe, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for o in [*base, *extra]:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


ALLOWED_ORIGINS: list[str] = _default_allowed_origins()

# Private-LAN origins (any port) are allowed by default so the phone/laptop can
# reach the backend over the LAN in network mode — mirrors next.config.mjs's
# allowedDevOrigins. The token is still required there, so this regex is a
# convenience for legitimate same-network devices, not the security boundary.
# Override or disable with VC_ALLOWED_ORIGIN_REGEX (set it empty to disable).
_DEFAULT_ORIGIN_REGEX = (
    r"^https?://("
    r"localhost|127\.0\.0\.1|\[::1\]|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")(:\d{1,5})?$"
)
_origin_regex_raw = os.getenv("VC_ALLOWED_ORIGIN_REGEX")
ALLOWED_ORIGIN_REGEX: str | None = (
    _DEFAULT_ORIGIN_REGEX if _origin_regex_raw is None else (_origin_regex_raw.strip() or None)
)
_ORIGIN_RE = re.compile(ALLOWED_ORIGIN_REGEX) if ALLOWED_ORIGIN_REGEX else None


def origin_allowed(origin: str | None) -> bool:
    """Whether a browser Origin header is permitted (used for the WebSocket
    handshake, where CORS middleware doesn't apply). An empty Origin (non-browser
    clients) returns False here; the WS path treats a missing Origin separately."""
    if not origin:
        return False
    if origin in ALLOWED_ORIGINS:
        return True
    # fullmatch (not match) so a trailing newline / extra suffix can't sneak past
    # the `$` anchor.
    return bool(_ORIGIN_RE and _ORIGIN_RE.fullmatch(origin))
