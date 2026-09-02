import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

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


if __name__ == "__main__":
    unittest.main()
