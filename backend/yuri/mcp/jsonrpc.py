"""A minimal MCP client over stdio.

The whole protocol we use, verified by hand against `uvx mcp-server-time` on
2026-09-04:

    -> {"jsonrpc":"2.0","id":1,"method":"initialize",
        "params":{"protocolVersion":"2025-06-18","capabilities":{},
                  "clientInfo":{"name":"yuri","version":"…"}}}
    <- {"result":{"protocolVersion":"…","capabilities":{…},"serverInfo":{…}}}
    -> {"jsonrpc":"2.0","method":"notifications/initialized"}      (no id)
    -> {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    <- {"result":{"tools":[{"name","description","inputSchema","annotations"}]}}
    -> {"jsonrpc":"2.0","id":3,"method":"tools/call",
        "params":{"name":"…","arguments":{…}}}
    <- {"result":{"content":[{"type":"text","text":"…"}],"isError":false}}

Newline-delimited JSON on the child's stdin/stdout. Only stdio is implemented:
the HTTP transports carry the same bodies over POST, but that is reasoning
rather than a measurement, and the spec says not to ship a transport nobody
has driven.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("yuri.mcp")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "yuri"
CLIENT_VERSION = "0.1"

# How much of the child's stderr to keep. A server that cannot start says why
# there and exits — "command not found", a missing key, a traceback — and that
# text is the only thing the user can act on, so it is captured rather than
# discarded to /dev/null.
STDERR_TAIL = 2000
# A single line of JSON longer than this is not a protocol message. Bounded so
# a server printing a megabyte of logs to stdout cannot exhaust memory.
LINE_MAX = 1_000_000

# Environment variables a child needs to be able to run at all. The rest of the
# parent environment is NOT inherited: an MCP server has no business reading
# this process's GEMINI_API_KEY or VC_AUTH_TOKEN.
_INHERIT = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
            "TERM", "TMPDIR", "SSL_CERT_FILE", "SYSTEMROOT", "APPDATA")
# Where package managers put things. A backend launched from a GUI inherits a
# minimal PATH, which is how `spawn uvx ENOENT` happens even though `uvx` works
# in the user's terminal — a trap project-yuri hit and documented
# (apps/daemon/src/job-manager.ts:13-26).
_PATH_EXTRA = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin",
               "~/.bun/bin", "~/.cargo/bin", "~/.npm-global/bin")


class McpError(RuntimeError):
    """A server could not be reached or spoke nonsense, with a readable why."""


@dataclass(frozen=True)
class ServerInfo:
    name: str
    version: str


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def destructive(self) -> bool:
        """The server's own hint that this tool is dangerous.

        Trusted in ONE direction only: it can escalate a tool to `confirm`,
        never de-escalate one the user declared `confirm`. A server may not
        free itself — see the spec's §2.
        """
        return bool(self.annotations.get("destructiveHint"))


def child_env(extra: dict[str, str] | None) -> dict[str, str]:
    """The environment a server is launched with.

    An allowlist, not the parent environment: a third-party subprocess should
    not inherit this backend's API keys. PATH is widened because a
    GUI-launched parent has a minimal one.
    """
    env = {k: os.environ[k] for k in _INHERIT if k in os.environ}
    path = env.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    for extra_dir in _PATH_EXTRA:
        resolved = os.path.expanduser(extra_dir)
        if resolved not in parts:
            parts.append(resolved)
    env["PATH"] = os.pathsep.join(parts)
    for k, v in (extra or {}).items():
        env[str(k)] = str(v)
    return env


class StdioClient:
    """One connected server. Not reusable after close()."""

    def __init__(self, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | None = None):
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._stderr: list[str] = []
        self._next_id = 0
        self.server_info: ServerInfo | None = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def stderr_tail(self) -> str:
        joined = "".join(self._stderr)
        return joined[-STDERR_TAIL:] if len(joined) > STDERR_TAIL else joined

    async def start(self, timeout: float) -> ServerInfo:
        """Spawn, handshake, and return what the server calls itself."""
        if not shutil.which(self.command) and not os.path.isabs(self.command):
            raise McpError(
                f"{self.command!r} isn't on the PATH, so I can't start this server. "
                "Check the command, or give its full path.")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env(self.env), cwd=self.cwd,
                # Its own group, so close() can kill a server that spawned
                # children without leaving them orphaned.
                start_new_session=True,
            )
        except OSError as exc:
            raise McpError(f"couldn't start {self.command!r}: {exc}") from exc

        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

        try:
            result = await self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            }, timeout=timeout)
        except McpError:
            # The handshake is where a broken server fails, and its stderr is
            # the only useful diagnostic. Attach it before unwinding.
            await self.close()
            raise
        info = result.get("serverInfo") or {}
        self.server_info = ServerInfo(name=str(info.get("name") or self.command),
                                      version=str(info.get("version") or "?"))
        # A notification: no id, and no reply is expected or waited for.
        self._notify("notifications/initialized")
        return self.server_info

    async def close(self) -> None:
        for task in (self._reader, self._stderr_reader):
            if task and not task.done():
                task.cancel()
        proc = self._proc
        self._proc = None
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        # Anything still waiting will never be answered; say so rather than
        # leaving a caller awaiting a future nobody will resolve.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(McpError("the server was disconnected"))
        self._pending.clear()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # --- the three methods -------------------------------------------------

    async def list_tools(self, timeout: float) -> list[McpTool]:
        result = await self._request("tools/list", None, timeout=timeout)
        out: list[McpTool] = []
        for raw in (result.get("tools") or []):
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            out.append(McpTool(
                name=str(raw["name"]),
                description=str(raw.get("description") or ""),
                input_schema=raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {},
                annotations=raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {},
            ))
        return out

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        timeout: float) -> tuple[str, bool]:
        """Returns (text, is_error).

        `isError: true` is a TOOL failure, not a transport one — the server
        answered. It comes back as text so the model can relay what went wrong
        instead of a generic failure, which is the distinction the honesty
        rules in her prompt turn on.
        """
        result = await self._request("tools/call",
                                     {"name": name, "arguments": arguments or {}},
                                     timeout=timeout)
        parts = [str(c.get("text") or "") for c in (result.get("content") or [])
                 if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            # Non-text content (an image, a resource link) is not something she
            # can say out loud. Say that, rather than returning "".
            kinds = sorted({str(c.get("type")) for c in (result.get("content") or [])
                            if isinstance(c, dict)})
            text = (f"(the tool returned {', '.join(kinds)} rather than text)"
                    if kinds else "(the tool returned nothing)")
        return text, bool(result.get("isError"))

    # --- plumbing ----------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if not proc or not proc.stdin or proc.returncode is not None:
            raise McpError("the server isn't running")
        proc.stdin.write((json.dumps(payload) + "\n").encode())

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        try:
            self._write(body)
        except McpError:
            # A notification has no reply, so a failure here has nobody to
            # report to and must not mask the caller's actual work.
            log.debug("mcp: could not send notification %s", method)

    async def _request(self, method: str, params: dict[str, Any] | None,
                       timeout: float) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            body["params"] = params
        try:
            self._write(body)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise McpError(
                f"the server didn't answer {method} within {timeout:g} seconds") from exc
        finally:
            self._pending.pop(req_id, None)

    async def _read_stdout(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break                      # EOF: the server exited
                text = line.decode(errors="replace").strip()
                if not text or len(text) > LINE_MAX:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    # Servers do print stray logs to stdout. Not fatal — the
                    # framing is per-line, so skip it and keep reading.
                    log.debug("mcp: non-JSON on stdout: %s", text[:120])
                    continue
                self._deliver(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("mcp: stdout reader stopped", exc_info=True)
        finally:
            # EOF with requests outstanding means the server died mid-call.
            # Resolve them, with the stderr that explains why.
            reason = self.stderr_tail.strip()
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(McpError(
                        "the server exited" + (f": {reason[-400:]}" if reason else "")))

    def _deliver(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        req_id = msg.get("id")
        if req_id is None:
            return                             # a notification from the server
        fut = self._pending.get(req_id if isinstance(req_id, int) else -1)
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            fut.set_exception(McpError(str(err.get("message") or "the server returned an error")))
            return
        result = msg.get("result")
        fut.set_result(result if isinstance(result, dict) else {})

    async def _read_stderr(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            while True:
                chunk = await proc.stderr.readline()
                if not chunk:
                    break
                self._stderr.append(chunk.decode(errors="replace"))
                # Keep the tail bounded without re-joining on every line.
                if len(self._stderr) > 400:
                    del self._stderr[:200]
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("mcp: stderr reader stopped", exc_info=True)
