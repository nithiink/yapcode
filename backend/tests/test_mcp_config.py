import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.mcp.config import (MAX_SERVERS, ConfigError, ServerConfig,  # noqa: E402
                             config_path, load, parse, save)


def srv(**over):
    base = {"transport": "stdio", "command": "uvx", "args": ["mcp-weather"], "tier": "safe"}
    base.update(over)
    return base


class ParseTests(unittest.TestCase):
    def test_a_valid_server_parses(self):
        [s] = parse({"servers": {"weather": srv(env={"K": "v"})}})
        self.assertEqual((s.name, s.transport, s.tier, s.command), ("weather", "stdio", "safe", "uvx"))
        self.assertEqual(s.args, ("mcp-weather",))
        self.assertEqual(s.env, {"K": "v"})
        self.assertTrue(s.enabled)

    def test_nothing_configured_is_no_servers_and_not_an_error(self):
        for empty in [None, {}, {"servers": None}, {"servers": {}}]:
            self.assertEqual(parse(empty), [], repr(empty))

    def test_tier_has_no_default_and_its_absence_names_the_server(self):
        # A default tier is a security decision made by whoever omitted the
        # field. The error has to say WHICH server, or it cannot be fixed.
        with self.assertRaises(ConfigError) as ctx:
            parse({"servers": {"weather": srv(tier=None)}})
        self.assertIn("weather", str(ctx.exception))
        self.assertIn("no default", str(ctx.exception))

    def test_an_unknown_tier_is_refused(self):
        with self.assertRaises(ConfigError):
            parse({"servers": {"w": srv(tier="sensitive")}})

    def test_a_planned_transport_says_not_yet_rather_than_unknown(self):
        # "not built yet" and "you typo'd it" are different fixes.
        for t in ("sse", "http"):
            with self.assertRaises(ConfigError) as ctx:
                parse({"servers": {"w": srv(transport=t)}})
            self.assertIn("isn't built yet", str(ctx.exception))

    def test_an_unknown_transport_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            parse({"servers": {"w": srv(transport="carrier-pigeon")}})
        self.assertIn("transport must be", str(ctx.exception))

    def test_a_stdio_server_without_a_command_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            parse({"servers": {"w": srv(command="  ")}})
        self.assertIn("needs a", str(ctx.exception))

    def test_bad_arg_and_env_shapes_are_refused(self):
        for bad in [srv(args="not a list"), srv(args=[{"nested": 1}]), srv(env="nope")]:
            with self.assertRaises(ConfigError):
                parse({"servers": {"w": bad}})

    def test_a_name_is_slugged_and_an_unusable_one_is_refused(self):
        [s] = parse({"servers": {"My Weather!": srv()}})
        self.assertEqual(s.name, "my-weather")
        with self.assertRaises(ConfigError):
            parse({"servers": {"!!!": srv()}})

    def test_two_names_that_slug_the_same_are_refused(self):
        # Otherwise one silently shadows the other.
        with self.assertRaises(ConfigError) as ctx:
            parse({"servers": {"My Notes": srv(), "my-notes": srv()}})
        self.assertIn("resolve to the name", str(ctx.exception))

    def test_too_many_servers_is_refused_with_the_reason(self):
        many = {f"s{i}": srv() for i in range(MAX_SERVERS + 1)}
        with self.assertRaises(ConfigError) as ctx:
            parse({"servers": many})
        self.assertIn(str(MAX_SERVERS), str(ctx.exception))

    def test_disabled_is_kept_not_dropped(self):
        [s] = parse({"servers": {"w": srv(enabled=False)}})
        self.assertFalse(s.enabled)

    def test_a_cwd_that_is_not_a_directory_is_refused(self):
        with self.assertRaises(ConfigError):
            parse({"servers": {"w": srv(cwd="/definitely/not/here")}})


class RedactionTests(unittest.TestCase):
    def test_public_reports_key_names_and_never_their_values(self):
        # This shape is what the API returns. env holds API keys.
        [s] = parse({"servers": {"w": srv(env={"WEATHER_API_KEY": "super-secret"})}})
        pub = s.public()
        blob = json.dumps(pub)
        self.assertIn("WEATHER_API_KEY", pub["env_keys"])
        self.assertNotIn("super-secret", blob)
        self.assertNotIn("env", pub)
        self.assertNotIn("headers", pub)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_absent_file_is_no_servers(self):
        self.assertEqual(load(self.home), [])

    def test_malformed_json_raises_rather_than_reading_as_empty(self):
        # "you have no servers" and "your servers are being ignored" are very
        # different things to tell someone.
        with open(config_path(self.home), "w") as f:
            f.write("{not json")
        with self.assertRaises(ConfigError) as ctx:
            load(self.home)
        self.assertIn("valid JSON", str(ctx.exception))

    def test_save_then_load_round_trips(self):
        s = ServerConfig(name="weather", transport="stdio", tier="confirm",
                         command="uvx", args=("mcp-weather",), env={"K": "v"})
        save(self.home, [s])
        [back] = load(self.home)
        self.assertEqual((back.name, back.tier, back.command, back.args, back.env),
                         ("weather", "confirm", "uvx", ("mcp-weather",), {"K": "v"}))

    def test_the_file_is_written_private_because_it_holds_keys(self):
        save(self.home, [ServerConfig(name="w", transport="stdio", tier="safe",
                                      command="x", env={"KEY": "secret"})])
        mode = stat.S_IMODE(os.stat(config_path(self.home)).st_mode)
        self.assertEqual(mode, 0o600, oct(mode))

    def test_save_leaves_no_temp_file_behind(self):
        save(self.home, [ServerConfig(name="w", transport="stdio", tier="safe", command="x")])
        self.assertEqual([f for f in os.listdir(self.home) if f.endswith(".tmp")], [])

    def test_saving_nothing_produces_a_readable_empty_config(self):
        save(self.home, [])
        self.assertEqual(load(self.home), [])
