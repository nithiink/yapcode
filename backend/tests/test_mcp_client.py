"""The MCP stdio client, against a real subprocess speaking the real protocol.

The fake server (tests/fixtures/fake_mcp_server.py) is launched as an actual
child process, so these exercise the framing, the process plumbing and the
failure paths rather than a mock of them. It misbehaves on demand, which is
most of what needs testing here.

    .venv/bin/python -m unittest tests.test_mcp_client -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.mcp.jsonrpc import (McpError, McpTool, StdioClient,  # noqa: E402
                              child_env)

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_mcp_server.py")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, mode: str = "ok") -> StdioClient:
        c = StdioClient(sys.executable, [FAKE, mode])
        self.addAsyncCleanup(c.close)
        return c

    # --- the happy path ----------------------------------------------------

    async def test_it_handshakes_and_reports_what_the_server_calls_itself(self):
        c = await self._client()
        info = await c.start(timeout=10)
        # Its own name, not the command we ran — that is what the UI shows so
        # the user can confirm the server is the thing they meant.
        self.assertEqual((info.name, info.version), ("fake-mcp", "9.9.9"))
        self.assertTrue(c.alive)

    async def test_it_lists_tools_with_their_schemas(self):
        c = await self._client()
        await c.start(timeout=10)
        tools = await c.list_tools(timeout=10)
        self.assertEqual([t.name for t in tools], ["echo", "no_args"])
        echo = tools[0]
        # inputSchema is already JSON Schema, which is the shape
        # TOOL_DEFINITIONS.parameters uses, so it passes through untranslated.
        self.assertEqual(echo.input_schema["properties"]["text"]["type"], "string")
        self.assertEqual(echo.input_schema["required"], ["text"])

    async def test_it_calls_a_tool_and_returns_its_text(self):
        c = await self._client()
        await c.start(timeout=10)
        text, is_error = await c.call_tool("echo", {"text": "hello"}, timeout=10)
        self.assertEqual(text, "echo: hello")
        self.assertFalse(is_error)

    async def test_ids_are_matched_so_concurrent_calls_do_not_cross(self):
        # Replies are matched by id, not by arrival order. Without that, two
        # calls in flight would resolve to each other's answers.
        c = await self._client()
        await c.start(timeout=10)
        results = await asyncio.gather(*[
            c.call_tool("echo", {"text": str(i)}, timeout=10) for i in range(8)
        ])
        self.assertEqual([t for t, _ in results], [f"echo: {i}" for i in range(8)])

    # --- the failures ------------------------------------------------------

    async def test_a_tool_error_is_reported_as_an_error_not_as_a_transport_failure(self):
        # isError means the server ANSWERED and the tool failed. Collapsing
        # that into "couldn't reach the server" would lose the only useful
        # part — what actually went wrong.
        c = await self._client("error")
        await c.start(timeout=10)
        text, is_error = await c.call_tool("echo", {}, timeout=10)
        self.assertTrue(is_error)
        self.assertIn("went wrong", text)

    async def test_a_hanging_tool_times_out_and_says_how_long_it_waited(self):
        c = await self._client("hang")
        await c.start(timeout=10)
        with self.assertRaises(McpError) as ctx:
            await c.call_tool("echo", {}, timeout=1)
        self.assertIn("1 second", str(ctx.exception))

    async def test_a_server_that_dies_mid_call_reports_its_stderr(self):
        # Without the stderr the user gets "the server exited" and has nothing
        # to act on. With it they get the actual reason.
        c = await self._client("die")
        await c.start(timeout=10)
        with self.assertRaises(McpError) as ctx:
            await c.call_tool("echo", {}, timeout=10)
        msg = str(ctx.exception)
        self.assertIn("exited", msg)
        self.assertIn("ran out of everything", msg)

    async def test_a_server_that_cannot_start_reports_why(self):
        c = await self._client("badexit")
        with self.assertRaises(McpError):
            await c.start(timeout=10)
        # The handshake failure path must keep the stderr, since that is where
        # a misconfigured server explains itself.
        self.assertIn("MISSING_API_KEY", c.stderr_tail)

    async def test_junk_on_stdout_is_skipped_rather_than_fatal(self):
        # Servers really do print stray logs to stdout. The framing is
        # per-line, so a bad line is skipped and the next one still parses.
        c = await self._client("noise")
        await c.start(timeout=10)
        tools = await c.list_tools(timeout=10)
        self.assertEqual([t.name for t in tools], ["echo", "no_args"])

    async def test_a_missing_command_says_so_before_spawning(self):
        c = StdioClient("definitely-not-a-real-binary-xyz")
        with self.assertRaises(McpError) as ctx:
            await c.start(timeout=5)
        self.assertIn("PATH", str(ctx.exception))

    async def test_an_unknown_method_surfaces_the_servers_own_message(self):
        c = await self._client()
        await c.start(timeout=10)
        with self.assertRaises(McpError) as ctx:
            await c._request("prompts/list", None, timeout=5)
        self.assertIn("no such method", str(ctx.exception))

    async def test_closing_twice_is_safe_and_kills_the_child(self):
        c = await self._client()
        await c.start(timeout=10)
        await c.close()
        self.assertFalse(c.alive)
        await c.close()

    async def test_calling_after_close_fails_clearly(self):
        c = await self._client()
        await c.start(timeout=10)
        await c.close()
        with self.assertRaises(McpError) as ctx:
            await c.call_tool("echo", {}, timeout=5)
        self.assertIn("isn't running", str(ctx.exception))


class EnvironmentTests(unittest.TestCase):
    def test_a_child_does_not_inherit_this_backends_secrets(self):
        # A third-party subprocess has no business reading GEMINI_API_KEY.
        os.environ["GEMINI_API_KEY"] = "secret-value"
        os.environ["VC_AUTH_TOKEN"] = "also-secret"
        try:
            env = child_env(None)
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertNotIn("VC_AUTH_TOKEN", env)
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("VC_AUTH_TOKEN", None)

    def test_the_configured_env_is_passed_through(self):
        env = child_env({"WEATHER_API_KEY": "abc"})
        self.assertEqual(env["WEATHER_API_KEY"], "abc")

    def test_path_is_widened_for_a_gui_launched_backend(self):
        # A backend started from a GUI inherits a minimal PATH, which is how
        # `spawn uvx ENOENT` happens even though uvx works in a terminal.
        env = child_env(None)
        self.assertIn("/opt/homebrew/bin", env["PATH"])
        self.assertIn("/usr/local/bin", env["PATH"])

    def test_the_configured_env_can_override_an_inherited_one(self):
        os.environ["LANG"] = "en_GB.UTF-8"
        self.assertEqual(child_env({"LANG": "C"})["LANG"], "C")


class ToolHintTests(unittest.TestCase):
    def test_a_destructive_hint_is_read(self):
        t = McpTool(name="wipe", description="", annotations={"destructiveHint": True})
        self.assertTrue(t.destructive)

    def test_a_missing_or_read_only_hint_is_not_destructive(self):
        self.assertFalse(McpTool(name="x", description="").destructive)
        self.assertFalse(McpTool(name="x", description="",
                                 annotations={"readOnlyHint": True}).destructive)
