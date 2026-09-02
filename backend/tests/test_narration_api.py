"""The narration mode is one value, reachable three ways — voice, REST and the
UI toggle — so they can never disagree. It is remembered across sessions and
surfaced at connect.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import tools  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.api.routes import build_router  # noqa: E402
from yuri.narration.policy import DEFAULT_MODE, MODES  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


class NarrationMode(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
            mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri")),
        ]
        [p.start() for p in self.patches]
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"),
                                     FakeAgentProvider())

        async def guard():
            return None
        app = FastAPI()
        app.include_router(build_router(guard))
        self.client = TestClient(app)

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    def test_default_is_normal(self):
        self.assertEqual(yapp.narration_mode(), DEFAULT_MODE)
        self.assertEqual(self.client.get("/yuri/narration").json(),
                         {"mode": "normal", "modes": list(MODES)})

    def test_set_and_persist(self):
        self.assertEqual(yapp.set_narration_mode("quiet"), "quiet")
        self.assertEqual(yapp.narration_mode(), "quiet")
        # Survives a fresh read of the same store, i.e. it is really persisted.
        self.assertEqual(self.c.store.settings.get("narration_mode"), "quiet")

    def test_rest_round_trip(self):
        r = self.client.put("/yuri/narration", json={"mode": "verbose"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "verbose")
        self.assertEqual(self.client.get("/yuri/narration").json()["mode"], "verbose")

    def test_rest_rejects_an_unknown_mode(self):
        r = self.client.put("/yuri/narration", json={"mode": "loud"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(yapp.narration_mode(), DEFAULT_MODE)   # unchanged

    async def test_voice_tool_sets_it(self):
        out = await tools.dispatch_tool("set_narration", {"mode": "quiet"})
        self.assertEqual(out["mode"], "quiet")
        self.assertIn("quiet", out["message"].lower())
        self.assertEqual(yapp.narration_mode(), "quiet")

    async def test_voice_tool_rejects_an_unknown_mode_softly(self):
        with self.assertRaises(ValueError) as cm:
            await tools.dispatch_tool("set_narration", {"mode": "loud"})
        self.assertIn("quiet", str(cm.exception))     # names the valid modes
        self.assertEqual(yapp.narration_mode(), DEFAULT_MODE)

    def test_tool_is_exposed(self):
        d = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "set_narration")
        self.assertEqual(d["parameters"]["required"], ["mode"])

    def test_context_carries_the_mode(self):
        yapp.set_narration_mode("verbose")
        ctx = self.client.get("/yuri/context").json()
        self.assertEqual(ctx["narration_mode"], "verbose")


if __name__ == "__main__":
    unittest.main()
