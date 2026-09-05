import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tools as tools_mod  # noqa: E402
from yuri.mcp.naming import (UnsafeName, is_mcp, server_slug,  # noqa: E402
                             slug, split, tool_name)


class SlugTests(unittest.TestCase):
    def test_slugs_are_lowercase_alphanumeric_and_dashed(self):
        self.assertEqual(slug("My Weather Service"), "my-weather-service")
        self.assertEqual(slug("  Notes!!  "), "notes")
        self.assertEqual(slug("A/B\\C"), "a-b-c")

    def test_a_slug_never_contains_the_separator(self):
        # SEP is an underscore, so an underscore INSIDE a slug would make
        # mcp_a_b_c ambiguous — server "a" with tool "b_c", or server "a_b"
        # with tool "c"?
        self.assertNotIn("_", slug("my_weather_service"))
        self.assertEqual(slug("my_weather"), "my-weather")

    def test_a_name_with_nothing_usable_in_it_is_refused(self):
        for bad in ["", "   ", "!!!", "___", "///"]:
            with self.assertRaises(UnsafeName, msg=bad):
                server_slug(bad)


class NamespacingTests(unittest.TestCase):
    def test_a_tool_is_namespaced_by_its_server(self):
        self.assertEqual(tool_name("weather", "get_forecast"), "mcp_weather_get-forecast")

    def test_a_server_can_never_shadow_a_native_tool(self):
        # THE reason the namespace exists. A server called "mission" must not
        # be able to register "cancel_mission" — the destructive tool the
        # confirmation gate protects.
        native = {d["name"] for d in tools_mod.TOOL_DEFINITIONS}
        for server in ["mission", "session", "yuri", "", "tell"]:
            for tool in ["cancel_mission", "tell_claude", "start_session", "send_keys"]:
                try:
                    name = tool_name(server or "x", tool)
                except UnsafeName:
                    continue
                self.assertNotIn(name, native, f"{server}/{tool} collided")

    def test_no_native_tool_looks_like_an_mcp_one(self):
        # The other direction: dispatch routes on the prefix, so a native tool
        # starting with mcp_ would be sent to a server that does not exist.
        for d in tools_mod.TOOL_DEFINITIONS:
            self.assertFalse(is_mcp(d["name"]), d["name"])

    def test_a_hostile_name_cannot_escape_the_slug_rules(self):
        for hostile in ["../../etc", "a b; rm -rf /", 'x"; drop table', "a\nb", "a\x00b"]:
            name = tool_name("srv", hostile)
            self.assertRegex(name, r"^mcp_srv_[a-z0-9-]+$")


class SplitTests(unittest.TestCase):
    def test_a_name_round_trips(self):
        self.assertEqual(split(tool_name("weather", "get_forecast")),
                         ("weather", "get-forecast"))
        self.assertEqual(split(tool_name("my-notes", "list")), ("my-notes", "list"))

    def test_splitting_a_non_mcp_name_raises_rather_than_guessing(self):
        # The caller routes a dispatch on this. A plausible-looking guess would
        # call the wrong tool.
        for bad in ["cancel_mission", "mcp", "mcp_", "mcp_only", "mcp__x", ""]:
            with self.assertRaises(UnsafeName, msg=bad):
                split(bad)
