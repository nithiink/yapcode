"""Names for MCP tools, and the rules that make them collision-proof.

A configured server supplies tool names. Registering them as-is would let a
server called `mission` advertise `cancel_mission` and shadow the real one —
which is the destructive tool the confirmation gate exists to protect. So every
MCP tool is namespaced, and the namespace is built from slugs that cannot
contain the separator.
"""
from __future__ import annotations

import re

PREFIX = "mcp"
SEP = "_"
# The tool-name character set every realtime provider accepts. Anything else in
# a server or tool name is replaced rather than passed through: a name is only
# useful if the model can actually emit it.
_UNSAFE = re.compile(r"[^a-z0-9]+")
NAME_MAX = 64


class UnsafeName(ValueError):
    """A server name that cannot be made into a usable slug."""


def slug(text: str) -> str:
    """Lowercase, [a-z0-9] and single dashes. Never empty, never a separator.

    Dashes rather than underscores on purpose: `SEP` is an underscore, so a
    slug that could contain one would make `mcp_a_b_c` ambiguous — is the
    server `a` and the tool `b_c`, or the server `a_b` and the tool `c`? With
    dashes inside slugs the split is unambiguous at the first two underscores.
    """
    out = _UNSAFE.sub("-", (text or "").strip().lower()).strip("-")
    return out[:NAME_MAX]


def server_slug(name: str) -> str:
    s = slug(name)
    if not s:
        raise UnsafeName(
            f"{name!r} has no letters or digits in it, so I can't make a name from it.")
    return s


def tool_name(server: str, tool: str) -> str:
    """`mcp_<server>_<tool>`, both parts slugged."""
    t = slug(tool)
    if not t:
        raise UnsafeName(f"the server advertised a tool named {tool!r}, which I can't use")
    return f"{PREFIX}{SEP}{server_slug(server)}{SEP}{t}"


def is_mcp(name: str) -> bool:
    return name.startswith(f"{PREFIX}{SEP}")


def split(name: str) -> tuple[str, str]:
    """`mcp_weather_get-forecast` -> ("weather", "get-forecast").

    Raises for anything that is not an MCP tool name, rather than returning a
    plausible-looking guess — the caller uses this to route a dispatch, and a
    wrong answer would call the wrong tool.
    """
    if not is_mcp(name):
        raise UnsafeName(f"{name!r} is not an MCP tool name")
    rest = name[len(PREFIX) + len(SEP):]
    server, sep, tool = rest.partition(SEP)
    if not sep or not server or not tool:
        raise UnsafeName(f"{name!r} is missing a server or a tool part")
    return server, tool
