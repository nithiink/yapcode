"""The narration field is the whole frontend contract: if a carrier has a line,
inject it. This pins that BOTH carriers attach it, that neither narrates the
other's events (or the user hears everything twice), and that the mode is
honoured on both.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import tools  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.domain.event import EventType  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402

PERM = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"],
        "request_id": "r1"}


class PollNarration(unittest.IsolatedAsyncioTestCase):
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

    async def _sid(self):
        return (await self.c.sessions.start("proj", name="billing"))["session_id"]

    async def test_completed_carries_a_line_quoting_the_agent(self):
        sid = await self._sid()
        self.fake.script(sid, {"status": "completed", "assistant_text": "I changed two files."})
        res = self.c.sessions.poll(sid)
        self.assertIn("narration", res)
        self.assertIn("changed two files", res["narration"])

    async def test_permission_carries_a_line(self):
        sid = await self._sid()
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.assertIn("rm -rf build", self.c.sessions.poll(sid)["narration"])

    async def test_working_and_idle_carry_none(self):
        sid = await self._sid()
        self.assertIsNone(self.c.sessions.poll(sid).get("narration"))

    async def test_quiet_mode_suppresses_completion_but_not_permission(self):
        sid = await self._sid()
        yapp.set_narration_mode("quiet")
        self.fake.script(sid, {"status": "completed", "assistant_text": "done"})
        self.assertIsNone(self.c.sessions.poll(sid)["narration"])
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.assertIsNotNone(self.c.sessions.poll(sid)["narration"])

    async def test_the_result_keys_are_otherwise_unchanged(self):
        # narration is additive: everything the frontend already reads survives.
        sid = await self._sid()
        self.fake.script(sid, {"status": "completed", "assistant_text": "x", "session_id": sid})
        res = self.c.sessions.poll(sid)
        for k in ("status", "session_id", "assistant_text"):
            self.assertIn(k, res)


class OneOwnerPerFact(unittest.IsolatedAsyncioTestCase):
    """End-to-end count of the SPOKEN lines one happening produces.

    The type-level ownership table is not enough — `_fail_if_alone` turns one
    agent error into a mission failure carrying the same reason string, and a
    voice tool's own result is a third carrier. Everything that reaches
    `injectUpdate` is counted here, so a regression shows up as "she said it
    twice" rather than as a passing per-type assertion.
    """

    def setUp(self):
        PollNarration.setUp(self)

    def tearDown(self):
        tools._last_start = None       # the duplicate-start guard is module state
        PollNarration.tearDown(self)

    def _stream_lines(self, q) -> list[str]:
        """Every line the SSE stream would carry for what was just published."""
        out = []
        while not q.empty():
            line = self.c.narration.line_for(q.get_nowait(), yapp.narration_mode())
            if line:
                out.append(line)
        return out

    async def test_one_agent_error_is_spoken_exactly_once(self):
        sid = (await self.c.sessions.start("proj", name="billing"))["session_id"]
        q = self.c.bus.subscribe()
        self.fake.script(sid, {"status": "error", "error": "tmux pane died"})
        poll_line = self.c.sessions.poll(sid)["narration"]
        spoken = [l for l in [poll_line] if l] + self._stream_lines(q)
        self.assertEqual(len(spoken), 1, spoken)
        self.assertIn("tmux pane died", spoken[0])
        # Silence is not because nothing happened: the mission really failed.
        self.assertEqual([m.status for m in self.c.store.missions.list()], ["failed"])

    async def test_a_failed_start_is_spoken_exactly_once(self):
        """`start`'s provider-failure path calls set_status directly, so it is
        NOT one of _mission_to's restatements: no session row was ever
        inserted, so no poll can report it, and the agent.error beside it is
        poll-owned (silent on the stream). Nobody else speaks for this — if the
        stream stays silent too, a mission failing to start is unnarrated."""
        q = self.c.bus.subscribe()

        async def boom(*a, **kw):
            raise RuntimeError("claude is not installed")
        with mock.patch.object(self.fake, "create_session", boom):
            with self.assertRaises(RuntimeError):
                await self.c.sessions.start("proj", name="billing")
        spoken = self._stream_lines(q)
        self.assertEqual(len(spoken), 1, spoken)
        self.assertIn("billing", spoken[0])
        self.assertIn("failed", spoken[0])
        self.assertIn("claude is not installed", spoken[0])
        self.assertEqual([m.status for m in self.c.store.missions.list()], ["failed"])

    async def test_start_session_speaks_only_its_own_result(self):
        q = self.c.bus.subscribe()
        res = await tools.dispatch_tool("start_session", {"project_path": "proj", "name": "billing"})
        self.assertIn("billing", res["message"])
        self.assertEqual(self._stream_lines(q), [])

    async def test_close_session_speaks_only_its_own_result(self):
        sid = (await self.c.sessions.start("proj", name="payments"))["session_id"]
        q = self.c.bus.subscribe()
        res = await tools.dispatch_tool("close_session", {"session_id": sid})
        self.assertEqual(res["status"], "closed")
        self.assertEqual(self._stream_lines(q), [])
        self.assertEqual([m.status for m in self.c.store.missions.list()], ["paused"])

    async def test_pause_mission_speaks_only_its_own_result(self):
        await self.c.sessions.start("proj", name="payments")
        q = self.c.bus.subscribe()
        res = await tools.dispatch_tool("pause_mission", {})
        self.assertIn("paused", res["message"])
        self.assertEqual(self._stream_lines(q), [])

    async def test_a_ui_pause_is_still_narrated(self):
        # Nothing spoke for this one, so the stream must.
        out = await self.c.sessions.start("proj", name="payments")
        q = self.c.bus.subscribe()
        await self.c.missions.pause(out["mission_id"], by="ui")
        self.assertEqual(len(self._stream_lines(q)), 1)


class StreamNarration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        PollNarration.setUp(self)

    def tearDown(self):
        PollNarration.tearDown(self)

    async def _frames(self, published):
        """Drive the SSE generator far enough to read the replayed frames."""
        from fastapi import FastAPI
        from yuri.api.routes import build_router

        async def guard():
            return None
        app = FastAPI()
        app.include_router(build_router(guard))
        for e in published:
            self.c.store.events.insert(e)      # replay path reads the repo
        route = next(r for r in app.routes if getattr(r, "path", "") == "/yuri/events/stream")
        resp = await route.endpoint()
        out = []
        agen = resp.body_iterator
        try:
            for _ in range(len(published)):
                chunk = await agen.__anext__()
                if chunk.startswith("data: "):
                    out.append(json.loads(chunk[6:].strip()))
        finally:
            await agen.aclose()
        return out

    async def test_mission_created_frame_carries_a_line(self):
        from yuri.domain.event import YuriEvent
        e = YuriEvent.make(EventType.MISSION_CREATED,
                           payload={"title": "Fix billing", "project": "P"})
        [frame] = await self._frames([e])
        self.assertIn("Fix billing", frame["narration"])
        self.assertEqual(frame["type"], EventType.MISSION_CREATED)

    async def test_poll_owned_events_carry_none_on_the_stream(self):
        # The anti-double-speak guarantee, at the wire level.
        from yuri.domain.event import YuriEvent
        events = [YuriEvent.make(EventType.SESSION_TURN_COMPLETED,
                                 payload={"assistant_text": "done"}),
                  YuriEvent.make(EventType.APPROVAL_REQUESTED,
                                 payload={"description": "run rm -rf build"})]
        frames = await self._frames(events)
        for f in frames:
            self.assertIsNone(f["narration"], f["type"])

    async def test_verbose_only_events_are_none_at_normal(self):
        from yuri.domain.event import YuriEvent
        e = YuriEvent.make(EventType.TOOL_STARTED, payload={"tool_name": "Read"})
        [frame] = await self._frames([e])
        self.assertIsNone(frame["narration"])


if __name__ == "__main__":
    unittest.main()
