"""The manager and the health check, against the real fake server.

    .venv/bin/python -m unittest tests.test_mcp_manager -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.mcp.config import ServerConfig, config_path  # noqa: E402
from yuri.mcp.jsonrpc import McpError, McpTool  # noqa: E402
from yuri.mcp.manager import (CONNECTED, DESC_MAX, DISABLED, EMPTY,  # noqa: E402
                              FAILED, MAX_TOOLS_PER_SERVER, McpManager, OK,
                              clip, declaration, probe, tier_for)

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_mcp_server.py")


def cfg(name="fake", mode="ok", tier="safe", **over):
    body = dict(name=name, transport="stdio", tier=tier,
                command=sys.executable, args=(FAKE, mode))
    body.update(over)
    return ServerConfig(**body)


def mcp_json(home, name="fake", mode="ok", tier="safe", enabled=True):
    with open(config_path(home), "w") as f:
        json.dump({"servers": {name: {
            "transport": "stdio", "tier": tier, "command": sys.executable,
            "args": [FAKE, mode], "enabled": enabled}}}, f)


class ClipTests(unittest.TestCase):
    def test_a_description_is_flattened_and_bounded(self):
        self.assertEqual(clip("two\n\nlines   here"), "two lines here")
        long = clip("x" * 900)
        self.assertEqual(len(long), DESC_MAX)
        self.assertTrue(long.endswith("…"))

    def test_a_short_description_is_left_exactly_alone(self):
        self.assertEqual(clip("Echo the text back."), "Echo the text back.")


class TierTests(unittest.TestCase):
    def test_a_destructive_hint_escalates_a_safe_server(self):
        # A server volunteering that a tool is dangerous can only add a gate.
        tool = McpTool(name="wipe", description="", annotations={"destructiveHint": True})
        self.assertEqual(tier_for(cfg(tier="safe"), tool), "confirm")

    def test_a_read_only_hint_never_de_escalates_the_users_choice(self):
        # This is the direction an attacker pushes. The user's declaration wins.
        tool = McpTool(name="peek", description="", annotations={"readOnlyHint": True,
                                                                "destructiveHint": False})
        self.assertEqual(tier_for(cfg(tier="confirm"), tool), "confirm")


class DeclarationTests(unittest.TestCase):
    def test_a_declaration_is_namespaced_and_carries_tier_and_category(self):
        d = declaration(cfg(name="weather"), McpTool(name="forecast", description="Ask.",
                                                     input_schema={"type": "object"}))
        self.assertEqual(d["name"], "mcp_weather_forecast")
        self.assertEqual((d["tier"], d["category"]), ("safe", "mcp:weather"))
        self.assertEqual(d["parameters"], {"type": "object"})

    def test_a_tool_with_no_schema_still_gets_a_usable_one(self):
        # An empty `parameters` is rejected by some providers; a tool with no
        # inputs is normal, so give it the shape that means "no arguments".
        d = declaration(cfg(), McpTool(name="ping", description=""))
        self.assertEqual(d["parameters"], {"type": "object", "properties": {}})


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ok_carries_the_servers_own_name_and_its_tools(self):
        r = await probe(cfg())
        self.assertEqual(r["verdict"], OK)
        self.assertEqual((r["server_name"], r["server_version"]), ("fake-mcp", "9.9.9"))
        self.assertEqual([t["name"] for t in r["tools"]], ["echo", "no_args"])
        self.assertEqual(r["error"], "")

    async def test_a_server_offering_nothing_is_empty_and_not_ok(self):
        # It connected, so it is not `failed`; it adds no capability, so it is
        # not `ok`. Saving it is allowed, but never silently.
        r = await probe(cfg(mode="empty"))
        self.assertEqual(r["verdict"], EMPTY)
        self.assertEqual(r["tools"], [])
        self.assertEqual(r["server_name"], "fake-mcp")

    async def test_a_nonexistent_command_fails_with_the_spawn_error(self):
        r = await probe(cfg(command="definitely-not-a-real-binary", args=()))
        self.assertEqual(r["verdict"], "failed")
        self.assertIn("definitely-not-a-real-binary", r["error"])

    async def test_a_server_that_exits_immediately_fails_WITH_its_stderr(self):
        # Without the stderr the user has nothing to act on, and this is
        # exactly the moment they need it.
        r = await probe(cfg(mode="badexit"))
        self.assertEqual(r["verdict"], "failed")
        self.assertIn("MISSING_API_KEY", r["error"] + r["stderr"])

    async def test_a_server_that_never_answers_fails_on_the_timeout(self):
        # `hang` only stalls tools/call, which the probe never reaches; a
        # server that ignores the handshake is the case that would hang the
        # request, so that is the one to test.
        r = await probe(cfg(mode="deaf"), timeout=0.4)
        self.assertEqual(r["verdict"], "failed")
        self.assertIn("0.4", r["error"])

    async def test_garbage_on_stdout_does_not_stop_it_working(self):
        r = await probe(cfg(mode="noise"))
        self.assertEqual(r["verdict"], OK)

    async def test_probing_reports_tools_that_would_clash(self):
        # Registering both would make dispatch silently call the wrong one.
        r = await probe(cfg(mode="collide"))
        self.assertEqual([t["name"] for t in r["tools"]], ["no_args"])
        self.assertEqual(r["colliding_tools"], ["no-args"])

    async def test_probing_a_flood_reports_what_it_would_drop(self):
        r = await probe(cfg(mode="flood"))
        self.assertEqual(len(r["tools"]), MAX_TOOLS_PER_SERVER)
        self.assertEqual(r["dropped_tools"], 30 - MAX_TOOLS_PER_SERVER)

    async def test_a_probe_leaves_no_process_and_writes_no_config(self):
        with tempfile.TemporaryDirectory() as home:
            before = sorted(os.listdir(home))
            await probe(cfg())
            self.assertEqual(sorted(os.listdir(home)), before)


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.mgr = McpManager(self.home)
        self.addAsyncCleanup(self.mgr.close)
        self.addCleanup(self.tmp.cleanup)

    async def test_no_config_means_no_servers_and_no_error(self):
        await self.mgr.start_all()
        self.assertEqual(self.mgr.servers, {})
        self.assertEqual(self.mgr.tool_definitions(), [])
        self.assertNotIn("config_error", self.mgr.public())

    async def test_a_configured_server_connects_and_registers_its_tools(self):
        mcp_json(self.home)
        await self.mgr.start_all()
        state = self.mgr.servers["fake"]
        self.assertEqual(state.status, CONNECTED)
        # `no_args` registers as `no-args`: the slug rules use dashes so that
        # the `_` separator can never appear inside a segment, which is what
        # keeps `mcp_a_b_c` unambiguous.
        self.assertEqual([d["name"] for d in self.mgr.tool_definitions()],
                         ["mcp_fake_echo", "mcp_fake_no-args"])

    async def test_a_broken_server_fails_visibly_and_advertises_nothing(self):
        # The right failure: she cannot promise a tool that is not there.
        mcp_json(self.home, mode="badexit")
        await self.mgr.start_all()
        state = self.mgr.servers["fake"]
        self.assertEqual(state.status, FAILED)
        self.assertIn("MISSING_API_KEY", state.error)
        self.assertEqual(self.mgr.tool_definitions(), [])

    async def test_a_disabled_server_is_kept_but_not_connected(self):
        mcp_json(self.home, enabled=False)
        await self.mgr.start_all()
        self.assertEqual(self.mgr.servers["fake"].status, DISABLED)
        self.assertEqual(self.mgr.tool_definitions(), [])

    async def test_a_malformed_config_is_reported_not_swallowed(self):
        with open(config_path(self.home), "w") as f:
            f.write("{oh no")
        await self.mgr.start_all()
        self.assertIn("valid JSON", self.mgr.public()["config_error"])

    async def test_a_flood_registers_the_cap_and_says_how_many_it_dropped(self):
        mcp_json(self.home, mode="flood")
        await self.mgr.start_all()
        state = self.mgr.servers["fake"]
        self.assertEqual(len(state.tools), MAX_TOOLS_PER_SERVER)
        self.assertEqual(state.public()["dropped_tools"], 6)

    async def test_a_name_clash_registers_one_tool_and_reports_the_other(self):
        mcp_json(self.home, mode="collide")
        await self.mgr.start_all()
        self.assertEqual([d["name"] for d in self.mgr.tool_definitions()],
                         ["mcp_fake_no-args"])
        self.assertEqual(self.mgr.servers["fake"].public()["colliding_tools"], ["no-args"])

    async def test_calling_a_tool_attributes_the_answer_to_the_server(self):
        # "the weather service says 19 degrees", never "it is 19 degrees".
        mcp_json(self.home)
        await self.mgr.start_all()
        out = await self.mgr.call("mcp_fake_echo", {"text": "hello"})
        self.assertIn("fake-mcp says", out)
        self.assertIn("echo: hello", out)

    async def test_a_tool_error_is_relayed_as_the_servers_error(self):
        mcp_json(self.home, mode="error")
        await self.mgr.start_all()
        out = await self.mgr.call("mcp_fake_echo", {"text": "x"})
        self.assertIn("reported an error", out)
        self.assertIn("the thing went wrong", out)

    async def test_a_tool_that_hangs_comes_back_as_words_not_a_stuck_turn(self):
        mcp_json(self.home, mode="hang")
        await self.mgr.start_all()
        import yuri.mcp.manager as mod
        original = mod.CALL_TIMEOUT_S
        mod.CALL_TIMEOUT_S = 0.4
        try:
            out = await self.mgr.call("mcp_fake_echo", {"text": "x"})
        finally:
            mod.CALL_TIMEOUT_S = original
        self.assertIn("couldn't do that", out)

    async def test_calling_an_unknown_tool_raises_rather_than_inventing(self):
        await self.mgr.start_all()
        with self.assertRaises(McpError):
            await self.mgr.call("mcp_nope_nope", {})

    async def test_a_server_that_died_is_marked_down_instead_of_pretending(self):
        mcp_json(self.home, mode="die")
        await self.mgr.start_all()
        out = await self.mgr.call("mcp_fake_echo", {"text": "x"})   # kills it
        self.assertIn("couldn't do that", out)
        with self.assertRaises(McpError) as ctx:
            await self.mgr.call("mcp_fake_echo", {"text": "x"})     # now known down
        self.assertIn("reconnect", str(ctx.exception))
        self.assertEqual(self.mgr.tool_definitions(), [])

    async def test_disconnect_unregisters_the_tools(self):
        mcp_json(self.home)
        await self.mgr.start_all()
        self.assertTrue(self.mgr.tool_definitions())
        await self.mgr.disconnect("fake")
        self.assertEqual(self.mgr.tool_definitions(), [])
        self.assertEqual(self.mgr.servers["fake"].status, FAILED)

    async def test_reconnect_picks_up_a_config_the_user_just_fixed(self):
        # A server started (or corrected) after the backend must not need a
        # backend restart.
        mcp_json(self.home, mode="badexit")
        await self.mgr.start_all()
        self.assertEqual(self.mgr.servers["fake"].status, FAILED)
        mcp_json(self.home, mode="ok")
        state = await self.mgr.reconnect("fake")
        self.assertEqual(state.status, CONNECTED)
        self.assertTrue(self.mgr.tool_definitions())

    async def test_reconnecting_something_unconfigured_is_an_error(self):
        await self.mgr.start_all()
        with self.assertRaises(KeyError):
            await self.mgr.reconnect("ghost")

    async def test_remove_forgets_the_server_entirely(self):
        mcp_json(self.home)
        await self.mgr.start_all()
        await self.mgr.remove("fake")
        self.assertEqual(self.mgr.servers, {})

    async def test_closing_twice_is_safe(self):
        mcp_json(self.home)
        await self.mgr.start_all()
        await self.mgr.close()
        await self.mgr.close()


class RedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_public_payload_never_contains_an_env_value(self):
        # Same test shape that caught the search tool relaying an upstream body.
        with tempfile.TemporaryDirectory() as home:
            with open(config_path(home), "w") as f:
                json.dump({"servers": {"fake": {
                    "transport": "stdio", "tier": "safe", "command": sys.executable,
                    "args": [FAKE, "ok"], "env": {"WEATHER_KEY": "planted-secret"}}}}, f)
            mgr = McpManager(home)
            await mgr.start_all()
            try:
                blob = json.dumps(mgr.public())
            finally:
                await mgr.close()
        self.assertIn("WEATHER_KEY", blob)
        self.assertNotIn("planted-secret", blob)
