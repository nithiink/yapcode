"""Reading and validating ~/Yuri/mcp.json.

Pure and separate from the manager so it can be tested without a server. Fails
CLOSED: an absent or unreadable file means no servers, the same posture
ALLOWED_PROJECT_ROOTS already takes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .naming import UnsafeName, server_slug

TRANSPORTS = ("stdio",)
# Declared in the schema but not implemented: the wire trace in jsonrpc.py was
# captured against a real stdio server, and the spec says not to ship a
# transport nobody has driven. Named here so the error can say "not yet"
# rather than "unknown", which is a different fix.
PLANNED_TRANSPORTS = ("sse", "http")
TIERS = ("safe", "confirm")

MAX_SERVERS = 8
CONFIG_NAME = "mcp.json"


class ConfigError(ValueError):
    """A message naming the server at fault, because a config error the user
    cannot locate is a config error they cannot fix."""


@dataclass(frozen=True)
class ServerConfig:
    name: str                       # the slug; the key in the file
    transport: str
    tier: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def public(self) -> dict[str, Any]:
        """What an API may return. NEVER env or header VALUES — they hold API
        keys. The names are reported so the user can see what is configured
        without the file being readable over HTTP."""
        return {"name": self.name, "transport": self.transport, "tier": self.tier,
                "command": self.command, "args": list(self.args),
                "url": self.url, "cwd": self.cwd, "enabled": self.enabled,
                "env_keys": sorted(self.env), "header_keys": sorted(self.headers)}


def config_path(home_dir: str) -> str:
    return os.path.join(home_dir, CONFIG_NAME)


def _one(name: str, raw: Any) -> ServerConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"server {name!r}: expected an object, got {type(raw).__name__}")
    try:
        slug = server_slug(name)
    except UnsafeName as exc:
        raise ConfigError(str(exc)) from exc

    transport = str(raw.get("transport") or "").strip().lower()
    if transport in PLANNED_TRANSPORTS:
        raise ConfigError(
            f"server {slug!r}: the {transport!r} transport isn't built yet — only "
            f"{TRANSPORTS[0]!r} works today.")
    if transport not in TRANSPORTS:
        raise ConfigError(
            f"server {slug!r}: transport must be one of {list(TRANSPORTS)}, got {transport!r}")

    # No default, deliberately: a default tier is a security decision made by
    # whoever left the field out. The spec's posture is that the choice is the
    # user's, so an omission is an error that names the server.
    tier = str(raw.get("tier") or "").strip().lower()
    if tier not in TIERS:
        raise ConfigError(
            f"server {slug!r}: needs \"tier\": \"safe\" or \"confirm\" — "
            "there is no default, because that would be choosing for you.")

    command = str(raw.get("command") or "").strip()
    if not command:
        raise ConfigError(f"server {slug!r}: a stdio server needs a \"command\"")

    args = raw.get("args") or []
    if not isinstance(args, list) or any(not isinstance(a, (str, int, float)) for a in args):
        raise ConfigError(f"server {slug!r}: \"args\" must be a list of strings")

    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise ConfigError(f"server {slug!r}: \"env\" must be an object")

    cwd = raw.get("cwd")
    if cwd is not None:
        cwd = os.path.expanduser(str(cwd))
        if not os.path.isdir(cwd):
            raise ConfigError(f"server {slug!r}: cwd {cwd!r} isn't a directory")

    return ServerConfig(
        name=slug, transport=transport, tier=tier, command=command,
        args=tuple(str(a) for a in args),
        env={str(k): str(v) for k, v in env.items()},
        cwd=cwd, enabled=bool(raw.get("enabled", True)),
    )


def parse(data: Any) -> list[ServerConfig]:
    """Validate a parsed config body. Raises ConfigError naming the server."""
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ConfigError("mcp.json must contain an object")
    servers = data.get("servers")
    if servers is None:
        return []
    if not isinstance(servers, dict):
        raise ConfigError('"servers" must be an object keyed by server name')
    if len(servers) > MAX_SERVERS:
        raise ConfigError(
            f"{len(servers)} servers configured; the limit is {MAX_SERVERS}. "
            "Each one is a subprocess and a slice of her prompt.")
    out = [_one(name, raw) for name, raw in servers.items()]
    seen: set[str] = set()
    for s in out:
        if s.name in seen:
            # Two different keys can slug to the same name ("My Notes" and
            # "my-notes"), which would make one silently shadow the other.
            raise ConfigError(f"two servers both resolve to the name {s.name!r}")
        seen.add(s.name)
    return out


def load(home_dir: str) -> list[ServerConfig]:
    """Read and validate the file. Absent means no servers, not an error.

    Unreadable or malformed DOES raise: a config the user wrote and got wrong
    should be reported, not silently treated as empty — that is the difference
    between "you have no servers" and "your servers are being ignored".
    """
    path = config_path(home_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            body = json.load(f)
    except ValueError as exc:
        raise ConfigError(f"{CONFIG_NAME} isn't valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"couldn't read {CONFIG_NAME}: {exc}") from exc
    return parse(body)


def save(home_dir: str, servers: list[ServerConfig]) -> str:
    """Write the file, atomically and privately.

    0600 because it holds API keys, and a temp-file rename so a crash mid-write
    cannot leave a truncated config that fails to parse on next start.
    """
    path = config_path(home_dir)
    os.makedirs(home_dir, exist_ok=True)
    body = {"servers": {s.name: {
        "transport": s.transport, "tier": s.tier, "command": s.command,
        "args": list(s.args), "env": s.env, "enabled": s.enabled,
        **({"cwd": s.cwd} if s.cwd else {}),
    } for s in servers}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path
