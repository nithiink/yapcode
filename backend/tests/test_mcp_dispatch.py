"""MCP tools reaching the voice model, and the gate that guards the risky ones.

    .venv/bin/python -m unittest tests.test_mcp_dispatch -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import tools as tools_mod  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.mcp.config import ServerConfig  # noqa: E402
from yuri.mcp.manager import CONFIRM_ARG  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_mcp_server.py")


def cfg(name="fake", mode="ok", tier="safe"):
    return ServerConfig(name=name, transport="stdio", tier=tier,
                        command=sys.executable, args=(FAKE, mode))


class McpDispatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = os.path.join(self.tmp.name, "Yuri")
        self.patches = [mock.patch.object(config, "YURI_HOME", home)]
        [p.start() for p in self.patches]
        self.c = yapp.test_container(home, FakeAgentProvider())
        tools_mod._pending_confirm = None
        self.addAsyncCleanup(self._down)

    async def _down(self):
        await self.c.mcp.close()
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    async def _connect(self, mode="ok", tier="safe"):
        state = self.c.mcp.servers.setdefault("fake", None)
        from yuri.mcp.manager import ServerState
        state = ServerState(config=cfg(mode=mode, tier=tier))
        self.c.mcp.servers["fake"] = state
        await self.c.mcp._connect(state)
        self.assertEqual(state.status, "connected", state.error)
        return state

    # --- registration -------------------------------------------------------

    async def test_a_connected_servers_tools_reach_the_model(self):
        await self._connect()
        names = [d["name"] for d in tools_mod.tools_for_model()]
        self.assertIn("mcp_fake_echo", names)

    async def test_the_model_never_sees_our_own_bookkeeping_fields(self):
        # tier and category are unknown properties to a provider's function
        # schema; sending them is an API error, not a cosmetic one.
        await self._connect()
        for d in tools_mod.tools_for_model():
            self.assertEqual(set(d) - {"type", "name", "description", "parameters"}, set())

    async def test_native_tools_are_unaffected_by_a_connected_server(self):
        before = {d["name"] for d in tools_mod.TOOL_DEFINITIONS}
        await self._connect()
        self.assertEqual({d["name"] for d in tools_mod.TOOL_DEFINITIONS}, before)

    # --- calling ------------------------------------------------------------

    async def test_a_safe_tool_runs_and_the_answer_is_attributed(self):
        await self._connect()
        out = await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi"})
        self.assertTrue(out["ran"])
        self.assertEqual(out["server"], "fake")
        self.assertIn("fake-mcp says", out["message"])

    async def test_a_tool_from_a_server_that_is_gone_is_a_soft_error(self):
        # A ValueError, which the endpoint turns into {ok: false, error} — the
        # model reads it back instead of the turn dying.
        with self.assertRaises(ValueError):
            await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi"})

    # --- the gate -----------------------------------------------------------

    async def test_a_confirm_tier_tool_does_not_run_on_the_first_call(self):
        await self._connect(tier="confirm")
        first = await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi"})
        self.assertFalse(first["ran"])
        self.assertTrue(first[CONFIRM_ARG])
        self.assertIn("Nothing has happened yet", first["message"])

    async def test_it_runs_on_the_second_call_with_the_token(self):
        await self._connect(tier="confirm")
        first = await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi"})
        second = await tools_mod.dispatch_tool(
            "mcp_fake_echo", {"text": "hi", CONFIRM_ARG: first[CONFIRM_ARG]})
        self.assertTrue(second["ran"])
        self.assertIn("echo: hi", second["message"])

    async def test_the_confirm_token_is_never_passed_on_to_the_server(self):
        # It is ours, not part of the server's schema.
        await self._connect(tier="confirm")
        first = await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi"})
        second = await tools_mod.dispatch_tool(
            "mcp_fake_echo", {"text": "hi", CONFIRM_ARG: first[CONFIRM_ARG]})
        self.assertNotIn(CONFIRM_ARG, second["message"])

    async def test_a_wrong_token_arms_again_instead_of_running(self):
        await self._connect(tier="confirm")
        await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi"})
        out = await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi", CONFIRM_ARG: "nope"})
        self.assertFalse(out["ran"])

    async def test_a_token_is_single_use(self):
        await self._connect(tier="confirm")
        first = await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi"})
        token = first[CONFIRM_ARG]
        await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi", CONFIRM_ARG: token})
        again = await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi", CONFIRM_ARG: token})
        self.assertFalse(again["ran"], "a spent token ran the tool a second time")

    async def test_a_token_armed_for_one_tool_does_not_run_another(self):
        await self._connect(tier="confirm")
        first = await tools_mod.dispatch_tool("mcp_fake_echo", {"text": "hi"})
        other = await tools_mod.dispatch_tool(
            "mcp_fake_no-args", {CONFIRM_ARG: first[CONFIRM_ARG]})
        self.assertFalse(other["ran"], "a token armed for echo ran no-args")

    async def test_a_destructive_hint_gates_a_tool_on_a_safe_server(self):
        # The server volunteered that the tool is dangerous; that can only add
        # a gate, never remove one.
        await self._connect(mode="destructive", tier="safe")
        self.assertEqual(tools_mod.tier_of("mcp_fake_wipe"), "confirm")
        self.assertIn("mcp_fake_wipe", tools_mod.confirm_tools())
        out = await tools_mod.dispatch_tool("mcp_fake_wipe", {})
        self.assertFalse(out["ran"])

    async def test_a_read_only_hint_does_not_ungate_a_confirm_server(self):
        # `echo` advertises readOnlyHint: true. The user said confirm.
        await self._connect(mode="ok", tier="confirm")
        self.assertEqual(tools_mod.tier_of("mcp_fake_echo"), "confirm")
