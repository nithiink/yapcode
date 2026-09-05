import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import tools  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


class RememberTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
                        mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri"))]
        [p.start() for p in self.patches]
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), FakeAgentProvider())
        self.q = self.c.bus.subscribe()

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    def test_definition(self):
        d = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "remember")
        self.assertEqual(d["parameters"]["required"], ["fact"])
        self.assertIn("project", d["parameters"]["properties"])

    async def test_remember_user_fact(self):
        out = await tools.dispatch_tool("remember", {"fact": "prefers dark mode"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["path"], self.c.home.user_memory_path)
        self.assertIn("prefers dark mode", self.c.memory.read_user())
        self.assertEqual(self.q.get_nowait().type, "memory.remembered")
        self.assertIn("remembered", self.c.journal.read_today())

    async def test_remember_project_fact_resolves_folder(self):
        out = await tools.dispatch_tool("remember", {"fact": "uses uv", "project": "proj"})
        self.assertTrue(out["path"].endswith(os.path.join("memory", "projects", "proj.md")))
        self.assertIn("uses uv", self.c.memory.read_project("proj"))

    async def test_bad_project_is_soft_error(self):
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("remember", {"fact": "x", "project": "/etc"})
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("remember", {"fact": "   "})


if __name__ == "__main__":
    unittest.main()
