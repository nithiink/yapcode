"""Backend runtime configuration.

Single place where environment-driven settings are resolved: each setting reads
its environment variable when present, otherwise falls back to the default
defined here. The `.env` files are loaded first so values placed there are
honored regardless of which module imports config first.

Two .env files, in precedence order:

  1. backend/.env            — repo-local developer override (gitignored).
  2. ~/.config/yapcode/.env  — the wizard's canonical config, as a fill-gaps
                               fallback, so the key the user entered in the
                               setup wizard works no matter how the backend
                               was started (run.sh, bare uvicorn, launcher).
"""
from __future__ import annotations

import os
import re
import secrets

# backend/.env lives next to this file — resolved explicitly instead of by
# CWD discovery so `uvicorn main:app` from any directory behaves the same.
_BACKEND_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# The wizard's config dir. Mirrors bin/yapcode's CONF_DIR resolution
# (YAPCODE_CONFIG_DIR > XDG_CONFIG_HOME > ~/.config).
_CONFIG_DIR = os.path.expanduser(
    os.getenv("YAPCODE_CONFIG_DIR")
    or os.path.join(os.getenv("XDG_CONFIG_HOME") or "~/.config", "yapcode"))
_CONFIG_ENV = os.path.join(_CONFIG_DIR, ".env")
_CONFIG_ENV_DISPLAY = _CONFIG_ENV.replace(os.path.expanduser("~"), "~", 1)

# Where each .env-provided variable came from, for the startup summary and
# actionable error messages. Values are display labels, never secrets.
ENV_SOURCES: dict[str, str] = {}

try:  # dotenv is present in the venv; stay importable without it (e.g. in tests)
    from dotenv import dotenv_values, load_dotenv

    # 1) backend/.env — developer override. override=True so empty/stale vars
    #    exported in the shell environment must NOT shadow the values
    #    configured here (long-standing behavior; fresh users have no file).
    if os.path.isfile(_BACKEND_ENV):
        for _k in (dotenv_values(_BACKEND_ENV) or {}):
            ENV_SOURCES[_k] = "backend/.env"
        load_dotenv(_BACKEND_ENV, override=True)

    # 2) Config-dir .env — fill-gaps fallback (never overrides). VC_AUTH_TOKEN
    #    is deliberately SKIPPED: the token is opt-in per run mode — `yapcode
    #    up` unsets it for zero-config localhost and `yapcode network` exports
    #    it explicitly. Reading it here would force token auth onto every
    #    localhost run.sh user.
    if os.path.isfile(_CONFIG_ENV):
        for _k, _v in (dotenv_values(_CONFIG_ENV) or {}).items():
            if _k == "VC_AUTH_TOKEN" or _v is None or (os.getenv(_k) or "").strip():
                continue
            os.environ[_k] = _v
            ENV_SOURCES[_k] = _CONFIG_ENV_DISPLAY
except Exception:
    pass


# --- config provenance (startup summary + actionable errors) -----------------

# Provider API keys the voice layer can mint sessions with (any one suffices).
VOICE_KEY_VARS: tuple[str, ...] = ("GEMINI_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY")


def _source_of(var: str) -> str:
    """Display label for where a variable's effective value came from."""
    if not (os.getenv(var) or "").strip():
        return "not set"
    return ENV_SOURCES.get(var, "process environment")


def voice_keys_found() -> list[tuple[str, str]]:
    """(var, source) for each configured voice provider key."""
    return [(v, _source_of(v)) for v in VOICE_KEY_VARS if (os.getenv(v) or "").strip()]


def env_files_checked() -> str:
    """Human-readable list of the .env locations consulted, with presence."""
    return (f"backend/.env ({'present' if os.path.isfile(_BACKEND_ENV) else 'not present'}) "
            f"and {_CONFIG_ENV_DISPLAY} "
            f"({'present' if os.path.isfile(_CONFIG_ENV) else 'not found'})")


def missing_key_detail(var: str) -> str:
    """Actionable error body for a missing provider key/setting — says where we
    looked and how to fix it, instead of a bare 'not set'."""
    return (f"{var} is not set on the server. Looked in {env_files_checked()}. "
            f"Add it with `yapcode config`, or re-run the setup wizard (`yapcode up`).")


def summary() -> str:
    """One-line config provenance banner for startup logs. Names and sources
    only — never secret values."""
    keys = voice_keys_found()
    voice = ", ".join(f"{v} (from {src})" for v, src in keys) if keys else "NONE FOUND"
    token = (f"set (from {_source_of('VC_AUTH_TOKEN')}; required from ALL callers, "
             "including localhost)" if AUTH_TOKEN else "not set (loopback-only access)")
    roots = (os.getenv("ALLOWED_PROJECT_ROOTS") or "").strip() or "(not set)"
    return f"voice keys: {voice} · auth token: {token} · allowed roots: {roots}"


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
# back to ~/.yapcode/tmux.
SESSION_STORE_DIR: str = os.path.abspath(os.path.expanduser(
    os.getenv("VC_SESSION_STORE") or os.path.join(_REPO_ROOT, ".yapcode", "tmux")
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
