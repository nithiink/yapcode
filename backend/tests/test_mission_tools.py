"""The five mission voice tools. They must not change any existing tool's
result keys — test_tools_dispatch is the contract for those.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

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


class MissionTools(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
            mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri")),
        ]
        [p.start() for p in self.patches]
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    async def _start(self, name="s1"):
        return await self.c.sessions.start("proj", name=name)

    def test_all_five_tools_are_exposed_with_object_params(self):
        names = {d["name"] for d in tools.TOOL_DEFINITIONS}
        for t in ("list_missions", "mission_status", "pause_mission",
                  "resume_mission", "cancel_mission"):
            self.assertIn(t, names, t)
        for d in tools.TOOL_DEFINITIONS:
            self.assertEqual(d["parameters"]["type"], "object")

    async def test_list_missions_shape_and_status_filter(self):
        out = await self._start()
        res = await tools.dispatch_tool("list_missions", {})
        self.assertEqual(list(res), ["missions"])
        m = res["missions"][0]
        for k in ("id", "title", "goal", "status", "project", "agents", "sessions"):
            self.assertIn(k, m)
        self.assertEqual(m["id"], out["mission_id"])
        self.assertEqual(await tools.dispatch_tool("list_missions", {"status": "completed"}),
                         {"missions": []})

    async def test_mission_status_resolves_by_title_and_deictically(self):
        out = await self._start(name="Fix billing")
        by_title = await tools.dispatch_tool("mission_status", {"mission": "fix billing"})
        self.assertEqual(by_title["mission_id"], out["mission_id"])
        deictic = await tools.dispatch_tool("mission_status", {})
        self.assertEqual(deictic["mission_id"], out["mission_id"])

    async def test_pause_resume_cancel(self):
        await self._start()
        paused = await tools.dispatch_tool("pause_mission", {})
        self.assertEqual(set(paused), {"mission_id", "status", "title", "message"})
        self.assertEqual(paused["status"], "paused")
        self.assertEqual((await tools.dispatch_tool("resume_mission", {}))["status"], "running")
        self.assertEqual((await tools.dispatch_tool("cancel_mission", {}))["status"], "cancelled")

    async def test_pause_interrupts_a_live_session_first(self):
        out = await self._start()
        await tools.dispatch_tool("pause_mission", {})
        self.assertIn(("interrupt", out["session_id"]), self.fake.calls)

    async def test_pause_still_transitions_when_the_hook_is_unwired(self):
        # The hook is optional (a container that never wired it, or a
        # MissionService built directly by a test) — pause must not depend on it.
        await self._start()
        self.c.missions.interrupt_sessions = None
        paused = await tools.dispatch_tool("pause_mission", {})
        self.assertEqual(paused["status"], "paused")
        self.assertNotIn("interrupt", [c[0] for c in self.fake.calls])

    async def test_pause_survives_a_provider_that_fails_to_interrupt(self):
        out = await self._start()
        with mock.patch.object(self.fake, "interrupt", side_effect=RuntimeError("wedged")), \
                self.assertLogs("yuri.sessions", level="ERROR"):   # also silences the traceback
            paused = await tools.dispatch_tool("pause_mission", {})
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["mission_id"], out["mission_id"])

    async def test_an_invalid_transition_is_a_soft_error(self):
        await self._start()
        await tools.dispatch_tool("cancel_mission", {})
        # Resolve by name: a cancelled mission is no longer active, so a
        # deictic ref would refuse before the transition was ever attempted.
        with self.assertRaises(ValueError) as cm:      # soft: {"ok": false, "error": ...}
            await tools.dispatch_tool("resume_mission", {"mission": "s1"})
        self.assertIn("not allowed", str(cm.exception))

    async def test_a_deictic_reference_with_nothing_active_is_a_soft_error(self):
        await self._start()
        await tools.dispatch_tool("cancel_mission", {})
        with self.assertRaises(ValueError) as cm:
            await tools.dispatch_tool("mission_status", {})
        self.assertIn("no active missions", str(cm.exception).lower())

    async def test_ambiguity_is_a_soft_error_listing_candidates(self):
        await self._start(name="Fix billing in web")
        await self._start(name="Fix billing in mobile")
        with self.assertRaises(ValueError) as cm:
            await tools.dispatch_tool("pause_mission", {"mission": "fix billing"})
        self.assertIn("web", str(cm.exception))

    async def test_unknown_mission_is_a_soft_error(self):
        await self._start()
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("mission_status", {"mission": "no such thing"})

    async def test_list_missions_clips_a_long_goal(self):
        out = await self._start()
        m = self.c.missions.get(out["mission_id"])
        self.c.missions.set_goal_if_empty(m, "g" * 500)
        res = await tools.dispatch_tool("list_missions", {})
        self.assertLess(len(res["missions"][0]["goal"]), 300)

    async def test_list_missions_is_bounded(self):
        for i in range(3):
            await self._start(name=f"s{i}")
        with mock.patch.object(tools, "MISSION_LIST_MAX", 2):
            res = await tools.dispatch_tool("list_missions", {})
        self.assertEqual(len(res["missions"]), 2)

    async def test_cancel_stops_the_live_session(self):
        out = await self._start()
        await tools.dispatch_tool("cancel_mission", {})
        self.assertIn(("stop", out["session_id"]), self.fake.calls)


if __name__ == "__main__":
    unittest.main()
