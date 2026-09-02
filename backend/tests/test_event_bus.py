import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import event_log  # noqa: E402
from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.events.bus import EventBus, bridge_to_event_log, summarize  # noqa: E402


class _MemRepo:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def insert(self, e):
        if self.fail:
            raise RuntimeError("disk on fire")
        self.rows.append(e)

    def list(self, **kw):
        return list(self.rows)


class Bus(unittest.IsolatedAsyncioTestCase):
    async def test_fanout_persist_and_bridge(self):
        repo = _MemRepo()
        bridged = []
        bus = EventBus(repo=repo, bridge=bridged.append)
        bus.start_writer()
        q = bus.subscribe()
        try:
            e = bus.publish(YuriEvent.make(EventType.MISSION_CREATED, mission_id="m",
                                           payload={"title": "Fix it"}))
            got = await asyncio.wait_for(q.get(), 1.0)
            self.assertEqual(got.id, e.id)
            await bus.drain()
            self.assertEqual([r.id for r in repo.rows], [e.id])
            self.assertEqual(bridged, [e])
        finally:
            bus.unsubscribe(q)
            await bus.stop_writer()

    async def test_slow_subscriber_drops(self):
        bus = EventBus()
        q = bus.subscribe()
        for _ in range(q.maxsize + 10):
            bus.publish(YuriEvent.make(EventType.TOOL_STARTED))
        self.assertEqual(q.qsize(), q.maxsize)
        bus.unsubscribe(q)

    async def test_publish_never_raises(self):
        bus = EventBus(repo=_MemRepo(fail=True), bridge=lambda e: 1 / 0)
        bus.start_writer()
        try:
            bus.publish(YuriEvent.make(EventType.TOOL_STARTED))
            await bus.drain()  # writer swallows the repo error
        finally:
            await bus.stop_writer()

    async def test_stop_writer_reports_a_writer_that_died_of_a_real_error(self):
        """The old `except (asyncio.CancelledError, Exception)` was a bare
        except in disguise — a writer that crashed vanished silently."""
        bus = EventBus(repo=_MemRepo())

        async def boom():
            raise RuntimeError("writer crashed")

        bus._writer = asyncio.create_task(boom())
        await asyncio.sleep(0)
        with self.assertLogs("yuri.events", level="ERROR") as logs:
            await bus.stop_writer()
        self.assertTrue(any("writer crashed" in m for m in logs.output))
        self.assertFalse(bus.writer_running())
        await bus.stop_writer()          # idempotent

    async def test_stop_writer_swallows_only_the_writers_own_cancellation(self):
        bus = EventBus(repo=_MemRepo())
        bus.start_writer()
        await asyncio.sleep(0)
        await bus.stop_writer()          # the writer's CancelledError is ours: no raise
        self.assertFalse(bus.writer_running())

    async def test_bridge_to_event_log(self):
        event_log._buffer.clear()
        e = YuriEvent.make(EventType.APPROVAL_REQUESTED, session_id="s1",
                           payload={"description": "run rm -rf build", "session_name": "billing"})
        bridge_to_event_log(e)
        rec = event_log.recent(1)[0]
        self.assertEqual((rec["source"], rec["dest"], rec["kind"]), ("yuri", "ui", e.type))
        self.assertEqual(rec["session"], "billing")
        self.assertIn("rm -rf build", rec["summary"])
        self.assertEqual(rec["detail"]["id"], e.id)

    async def test_bridge_skips_debug_events_the_runners_already_log(self):
        """The runners log their own `tool: Bash` / send / cost lines with
        provider-specific detail, so mirroring the domain event for the same
        signal printed every Activity row twice. `debug` severity is exactly
        that duplicated set."""
        event_log._buffer.clear()
        for t in (EventType.TOOL_STARTED, EventType.SESSION_MESSAGE_SENT, EventType.COST_UPDATED):
            bridge_to_event_log(YuriEvent.make(t, session_id="s1", payload={"tool_name": "Bash"}))
        self.assertEqual(event_log.recent(10), [])
        # ...and the meaningful ones still come through.
        for t in (EventType.SESSION_TURN_COMPLETED, EventType.APPROVAL_REQUESTED,
                  EventType.AGENT_ERROR, EventType.SESSION_LOST, EventType.MISSION_CREATED):
            bridge_to_event_log(YuriEvent.make(t, session_id="s1", payload={}))
        self.assertEqual(len(event_log.recent(10)), 5)

    def test_summaries(self):
        self.assertEqual(summarize(YuriEvent.make(EventType.MISSION_CREATED,
                                                  payload={"title": "T"})), "mission created: T")
        self.assertEqual(summarize(YuriEvent.make(EventType.SESSION_TURN_COMPLETED,
                                                  payload={"assistant_text": "x" * 500}))[:16],
                         "turn completed: ")
        self.assertEqual(summarize(YuriEvent.make(EventType.TOOL_STARTED,
                                                  payload={"tool_name": "Read"})), "tool: Read")


if __name__ == "__main__":
    unittest.main()
