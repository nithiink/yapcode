import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry, build_registry  # noqa: E402


class Registry(unittest.IsolatedAsyncioTestCase):
    async def test_register_get_all_health(self):
        reg = AgentRegistry()
        ok = FakeAgentProvider()
        down = FakeAgentProvider(online=False)
        down.id = "fake-down"
        reg.register(ok)
        reg.register(down)
        self.assertIs(reg.get("fake"), ok)
        self.assertEqual(reg.ids(), ["fake", "fake-down"])
        with self.assertRaises(KeyError):
            reg.get("nope")
        health = await reg.health_all()
        self.assertTrue(health["fake"].online)
        self.assertFalse(health["fake-down"].online)

    async def test_build_registry_skips_unknown_ids(self):
        reg = build_registry("claude-code, bogus", claude_factory=lambda b: None)
        self.assertEqual(reg.ids(), ["claude-code"])

    async def test_build_registry_default(self):
        reg = build_registry("", claude_factory=lambda b: None)
        self.assertEqual(reg.ids(), ["claude-code"])


class OpenCode(unittest.IsolatedAsyncioTestCase):
    async def test_opencode_registers_when_asked(self):
        reg = build_registry("claude-code,opencode", claude_factory=lambda b: None)
        self.assertEqual(reg.ids(), ["claude-code", "opencode"])

    async def test_claude_code_is_still_the_first_and_default(self):
        # Adding a provider must not change which agent an unqualified request
        # gets. AgentRouter's fallback is the container default, and the
        # container's default is claude-code.
        reg = build_registry("claude-code,opencode", claude_factory=lambda b: None)
        self.assertEqual(reg.ids()[0], "claude-code")

    async def test_opencode_alone_is_allowed(self):
        reg = build_registry("opencode", claude_factory=lambda b: None)
        self.assertEqual(reg.ids(), ["opencode"])

    async def test_registering_opencode_does_not_touch_the_network(self):
        """Construction must be lazy: no server is acquired until a session is
        started, so a registry build cannot hang on a dead OpenCode.

        Pointed at a **reachable** OpenCode deliberately. Against a dead one
        the obvious assertions are far too weak — a build that eagerly called
        acquire() would fail, swallow it, and still leave `client` None. Only a
        server that would answer can tell "did not ask" apart from "asked and
        got nothing"."""
        with FakeOpenCode() as fake, \
                mock.patch.object(config, "OPENCODE_URL", fake.url):
            reg = build_registry("opencode", claude_factory=lambda b: None)
            p = reg.get("opencode")
            self.assertIsNone(p.server.client, "a registry build must not acquire")
            self.assertEqual(p.server.spawn_count, 0)
            self.assertFalse(p.server.owned)
            # And the reachable fake really was reachable, so the assertions
            # above are about restraint rather than an unreachable URL.
            self.assertTrue(await p.server.is_reachable())
        await p.shutdown()

    async def test_the_server_is_wired_from_config_with_a_filtered_environment(self):
        """The registry is the construction site design spec section 4 means:
        the only layer that knows both which env names are Yuri's secrets and
        where the child should run. Capture the constructor call rather than
        reading the server's privates back."""
        from yuri.providers.opencode import provider as provider_mod
        from yuri.providers.opencode import server as server_mod
        real, seen = server_mod.OpenCodeServer, {}
        real_provider, provider_kw = provider_mod.OpenCodeProvider, {}

        def capture(*a, **kw):
            seen.update(url=a[0] if a else kw.get("url"), **kw)
            return real(*a, **kw)

        def capture_provider(*a, **kw):
            provider_kw.update(kw)
            return real_provider(*a, **kw)

        with mock.patch.dict(os.environ, {"VC_AUTH_TOKEN": "yuris-shared-secret",
                                          "GEMINI_API_KEY": "voice-key",
                                          "ALLOWED_PROJECT_ROOTS": os.path.dirname(__file__)}), \
                mock.patch.object(config, "OPENCODE_URL", "http://127.0.0.1:4177"), \
                mock.patch.object(config, "OPENCODE_SERVER_PASSWORD", "hunter2"), \
                mock.patch.object(config, "OPENCODE_MODEL", "anthropic/claude-sonnet-4-5"), \
                mock.patch.object(server_mod, "OpenCodeServer", capture), \
                mock.patch.object(provider_mod, "OpenCodeProvider", capture_provider):
            build_registry("opencode", claude_factory=lambda b: None)

        self.assertEqual(seen["url"], "http://127.0.0.1:4177")
        self.assertEqual(seen["spawn"], config.OPENCODE_SPAWN)
        self.assertEqual(seen["binary"], config.OPENCODE_BIN)
        self.assertEqual(seen["password"], "hunter2")
        # cwd is the first allowed project root (design spec section 4).
        self.assertEqual(seen["cwd"], os.path.realpath(os.path.dirname(__file__)))
        env = seen["env"]
        self.assertIsNotNone(env, "the child must be given an explicit environment")
        self.assertNotIn("VC_AUTH_TOKEN", env)
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertIn("PATH", env)
        self.assertEqual(provider_kw["default_model"], "anthropic/claude-sonnet-4-5")

    async def test_an_unset_password_and_model_are_none_not_empty_strings(self):
        # "" is not a password and not a model: OpenCodeClient sends no auth
        # header for None, and the provider falls back to OpenCode's own
        # default model.
        from yuri.providers.opencode import provider as provider_mod
        from yuri.providers.opencode import server as server_mod
        real, seen = server_mod.OpenCodeServer, {}
        real_provider, provider_kw = provider_mod.OpenCodeProvider, {}

        def capture(*a, **kw):
            seen.update(kw)
            return real(*a, **kw)

        def capture_provider(*a, **kw):
            provider_kw.update(kw)
            return real_provider(*a, **kw)

        with mock.patch.object(config, "OPENCODE_SERVER_PASSWORD", ""), \
                mock.patch.object(config, "OPENCODE_MODEL", ""), \
                mock.patch.object(server_mod, "OpenCodeServer", capture), \
                mock.patch.object(provider_mod, "OpenCodeProvider", capture_provider):
            build_registry("opencode", claude_factory=lambda b: None)
        self.assertIsNone(seen["password"])
        self.assertIsNone(provider_kw["default_model"])


if __name__ == "__main__":
    unittest.main()
