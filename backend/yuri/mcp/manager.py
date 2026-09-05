"""Connecting to MCP servers and exposing their tools as Yuri's.

The manager owns process lifetime and the registration shape. It deliberately
does NOT own trust decisions: the tier comes from the user's config, and the
only thing a server gets to say about its own privileges is "actually, this
one is dangerous" — see the escalation rule below and the spec's §2.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .config import ConfigError, ServerConfig, load
from .jsonrpc import McpError, McpTool, ServerInfo, StdioClient
from .naming import tool_name

log = logging.getLogger("yuri.mcp")

CONNECT_TIMEOUT_S = 10      # a slow server must not delay startup
CALL_TIMEOUT_S = 30         # a hung tool must not hold a voice turn
MAX_TOOLS_PER_SERVER = 24   # 500 advertised tools would swamp the prompt
DESC_MAX = 300

# Status names are part of the API and the UI's dot colour. "disabled" is
# distinct from "failed" on purpose: the guide's rule is that empty and broken
# never look the same.
CONNECTED, FAILED, DISABLED = "connected", "failed", "disabled"

OK, EMPTY, FAILED_VERDICT = "ok", "empty", "failed"


def clip(desc: str) -> str:
    """Flatten and bound a third-party description.

    Not a defence against injection — §2 is explicit that it isn't. It bounds
    a string written by someone else that lands in her system prompt, because
    a 5,000-word description is a denial of service on the prompt budget.
    """
    flat = " ".join(str(desc or "").split())
    return flat if len(flat) <= DESC_MAX else flat[: DESC_MAX - 1].rstrip() + "…"


def tier_for(cfg: ServerConfig, tool: McpTool) -> str:
    """The user's declared tier, which a server may only make stricter.

    `destructiveHint: true` escalates safe -> confirm; `readOnlyHint` is
    ignored entirely, because de-escalation is the direction an attacker
    pushes.
    """
    return "confirm" if tool.destructive else cfg.tier


# The two-call confirm flow needs somewhere to put the token, and a
# confirm-tier server's schema is written by the server, so we add the field.
# Prefixed so it can never collide with a property the server itself defines.
CONFIRM_ARG = "yuri_confirm"
CONFIRM_NOTE = (
    " Needs the user's spoken agreement: call it once without "
    f"{CONFIRM_ARG} to hear what it would do, tell the user, then call it "
    f"again passing the {CONFIRM_ARG} token from the first call.")


def declaration(cfg: ServerConfig, tool: McpTool) -> dict[str, Any]:
    """One entry for TOOL_DEFINITIONS."""
    tier = tier_for(cfg, tool)
    schema = dict(tool.input_schema or {"type": "object", "properties": {}})
    desc = clip(tool.description)
    if tier == "confirm":
        props = dict(schema.get("properties") or {})
        props[CONFIRM_ARG] = {
            "type": "string",
            "description": ("The token returned by the first call. Only pass it "
                            "after the user has agreed out loud."),
        }
        schema["properties"] = props
        schema.setdefault("type", "object")
        # Clipped first, then annotated: the clip bounds what the SERVER wrote,
        # and our own instruction must not be the part that gets cut.
        desc = desc + CONFIRM_NOTE
    return {
        "type": "function",
        "name": tool_name(cfg.name, tool.name),
        "description": desc,
        "parameters": schema,
        "tier": tier,
        "category": f"mcp:{cfg.name}",
    }


def dedupe(cfg: ServerConfig, tools: list[McpTool]) -> tuple[list[McpTool], list[str]]:
    """Drop tools whose registered name is already taken, and name them.

    Slugging means two upstream names can land on one registered name — a
    server offering both `no_args` and `no-args` produces `mcp_x_no-args`
    twice. Registering both would make dispatch pick whichever came first,
    silently calling the wrong tool. Keeping the first and reporting the
    clash is the only honest option: dropping quietly would make the
    capability map lie by omission.
    """
    kept: list[McpTool] = []
    seen: set[str] = set()
    clashes: list[str] = []
    for tool in tools:
        registered = tool_name(cfg.name, tool.name)
        if registered in seen:
            clashes.append(tool.name)
            continue
        seen.add(registered)
        kept.append(tool)
    return kept, clashes


@dataclass
class ServerState:
    config: ServerConfig
    status: str = FAILED
    error: str = ""
    client: StdioClient | None = None
    info: ServerInfo | None = None
    tools: list[McpTool] = field(default_factory=list)
    dropped: int = 0          # advertised beyond MAX_TOOLS_PER_SERVER
    colliding: list[str] = field(default_factory=list)   # names that slug alike

    def public(self) -> dict[str, Any]:
        """What GET /yuri/mcp returns. Inherits config.public()'s redaction —
        env and header VALUES never appear."""
        body = self.config.public()
        body.update({
            "status": self.status,
            "error": self.error,
            "tool_count": len(self.tools),
            "tools": [tool_name(self.config.name, t.name) for t in self.tools],
            "server_name": self.info.name if self.info else "",
            "server_version": self.info.version if self.info else "",
        })
        if self.dropped:
            # Named, never silent: a dropped tool the map doesn't mention is
            # the map lying by omission.
            body["dropped_tools"] = self.dropped
        if self.colliding:
            body["colliding_tools"] = list(self.colliding)
        return body


async def probe(cfg: ServerConfig, *, timeout: float = CONNECT_TIMEOUT_S) -> dict[str, Any]:
    """Start a candidate server, ask what it offers, stop it. Persists nothing.

    This is what POST /yuri/mcp/test runs. Three verdicts, because the remedy
    differs: `ok` (usable), `empty` (connected but offers nothing — a warning,
    not a pass), `failed` (the reason, verbatim, including the stderr tail —
    "failed to connect" on its own is a dead end for the user).
    """
    client = StdioClient(cfg.command, list(cfg.args), cfg.env, cfg.cwd)
    try:
        info = await client.start(timeout)
        tools = await client.list_tools(timeout)
    except McpError as exc:
        return {"verdict": FAILED_VERDICT, "error": str(exc),
                "stderr": client.stderr_tail, "tools": [], "server_name": "",
                "server_version": ""}
    finally:
        await client.close()

    kept, colliding = dedupe(cfg, tools[:MAX_TOOLS_PER_SERVER])
    return {
        "verdict": OK if kept else EMPTY,
        "error": "",
        "stderr": client.stderr_tail,
        # Its own name, so the user can confirm it is the thing they meant.
        "server_name": info.name,
        "server_version": info.version,
        "tools": [{"name": t.name, "description": clip(t.description),
                   "tier": tier_for(cfg, t)} for t in kept],
        "dropped_tools": max(0, len(tools) - MAX_TOOLS_PER_SERVER),
        "colliding_tools": colliding,
    }


class McpManager:
    """The set of configured servers and the tools they currently provide."""

    def __init__(self, home_dir: str):
        self.home_dir = home_dir
        self.servers: dict[str, ServerState] = {}
        self.config_error = ""

    # --- config ------------------------------------------------------------

    def read_config(self) -> list[ServerConfig]:
        """Load the file, keeping a bad one visible instead of silent.

        A config error is recorded and surfaced by the API rather than raised:
        one malformed entry must not stop the backend from starting, but the
        user has to be able to see why their servers vanished.
        """
        try:
            configs = load(self.home_dir)
            self.config_error = ""
            return configs
        except ConfigError as exc:
            self.config_error = str(exc)
            log.warning("mcp.json unusable: %s", exc)
            return []

    # --- lifecycle ---------------------------------------------------------

    async def start_all(self) -> None:
        """Connect every enabled server. Best effort; never blocking.

        Servers are connected concurrently: eight servers each taking the
        full connect timeout would otherwise be 80 seconds of startup.
        """
        configs = self.read_config()
        self.servers = {c.name: ServerState(config=c) for c in configs}
        for state in self.servers.values():
            if not state.config.enabled:
                state.status = DISABLED
        await asyncio.gather(*(self._connect(s) for s in self.servers.values()
                               if s.config.enabled))

    async def _connect(self, state: ServerState) -> None:
        cfg = state.config
        client = StdioClient(cfg.command, list(cfg.args), cfg.env, cfg.cwd)
        try:
            state.info = await client.start(CONNECT_TIMEOUT_S)
            tools = await client.list_tools(CONNECT_TIMEOUT_S)
        except McpError as exc:
            await client.close()
            state.status, state.client, state.tools = FAILED, None, []
            tail = client.stderr_tail.strip()
            state.error = f"{exc}\n{tail}" if tail else str(exc)
            log.warning("mcp server %s did not start: %s", cfg.name, state.error)
            return
        state.client = client
        kept, state.colliding = dedupe(cfg, tools[:MAX_TOOLS_PER_SERVER])
        state.dropped = max(0, len(tools) - MAX_TOOLS_PER_SERVER)
        state.tools = kept
        state.status, state.error = CONNECTED, ""
        if state.dropped:
            log.warning("mcp server %s advertised %d tools; registered %d",
                        cfg.name, len(tools), len(state.tools))
        log.info("mcp server %s connected: %d tools", cfg.name, len(state.tools))

    async def reconnect(self, name: str) -> ServerState:
        """Retry one server, picking up any config change to it.

        Re-reads the file so a user who fixed a command in the UI does not
        also need to restart the backend.
        """
        fresh = {c.name: c for c in self.read_config()}
        if name not in fresh:
            raise KeyError(name)
        await self.disconnect(name)
        state = ServerState(config=fresh[name])
        self.servers[name] = state
        if not state.config.enabled:
            state.status = DISABLED
            return state
        await self._connect(state)
        return state

    async def disconnect(self, name: str) -> None:
        """Stop a server and unregister its tools.

        Unregistering is not optional: a stale declaration makes her offer
        something that will fail, which is worse than not offering it.
        """
        state = self.servers.get(name)
        if not state:
            return
        if state.client:
            await state.client.close()
        state.client, state.tools, state.info = None, [], None
        state.dropped, state.colliding = 0, []
        if state.status == CONNECTED:
            state.status, state.error = FAILED, "disconnected"

    async def remove(self, name: str) -> None:
        await self.disconnect(name)
        self.servers.pop(name, None)

    async def close(self) -> None:
        for name in list(self.servers):
            await self.disconnect(name)

    # --- what she can do ---------------------------------------------------

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Declarations for every tool of every CONNECTED server.

        Derived, never cached: a server that is down contributes nothing, so
        the capability map cannot promise a tool that is not there.
        """
        out: list[dict[str, Any]] = []
        for state in self.servers.values():
            if state.status != CONNECTED:
                continue
            out.extend(declaration(state.config, t) for t in state.tools)
        return out

    def find(self, name: str) -> tuple[ServerState, McpTool] | None:
        for state in self.servers.values():
            if state.status != CONNECTED:
                continue
            for tool in state.tools:
                if tool_name(state.config.name, tool.name) == name:
                    return state, tool
        return None

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one MCP tool and return text attributed to its server.

        The attribution is load-bearing, not decoration: her prompt already
        separates what something SAID from what was VERIFIED, and a third
        party's answer is the former. So "the weather service says 19
        degrees", never "it is 19 degrees".
        """
        found = self.find(name)
        if not found:
            raise McpError(f"{name} isn't available right now")
        state, tool = found
        client = state.client
        if not client or not client.alive:
            state.status, state.error = FAILED, "the server stopped"
            state.tools = []
            raise McpError(f"the {state.config.name} server stopped; reconnect it to use this")
        label = state.info.name if state.info and state.info.name else state.config.name
        try:
            text, is_error = await client.call_tool(tool.name, arguments, CALL_TIMEOUT_S)
        except McpError as exc:
            return f"{label} couldn't do that: {exc}"
        if is_error:
            return f"{label} reported an error: {text}"
        return f"{label} says: {text}"

    def public(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "servers": [self.servers[n].public() for n in sorted(self.servers)]
        }
        if self.config_error:
            body["config_error"] = self.config_error
        return body
