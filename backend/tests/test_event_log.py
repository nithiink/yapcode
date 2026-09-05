"""Pins the debug event bus (ring buffer + fan-out + drop-on-full) that the
Activity panel and the future Yuri EventBus bridge rely on.

    python -m unittest discover -s backend/tests
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import event_log  # noqa: E402


class EventLog(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        event_log._buffer.clear()
        event_log._subscribers.clear()

    async def test_record_shape_and_truncation(self):
        rec = event_log.log_event("voice", "backend", "tool_call", "x" * 1000,
                                  session="s1", detail={"a": 1})
        self.assertEqual(set(rec), {"seq", "ts", "source", "dest", "kind", "session",
                                    "summary", "detail"})
        self.assertEqual(len(rec["summary"]), 600)
        self.assertTrue(rec["ts"].endswith("Z"))

    async def test_recent_returns_oldest_first_and_honors_limit(self):
        for i in range(5):
            event_log.log_event("a", "b", "info", str(i))
        self.assertEqual([r["summary"] for r in event_log.recent(2)], ["3", "4"])
        self.assertEqual(len(event_log.recent(0)), 5)

    async def test_subscriber_receives_live_events(self):
        q = event_log.subscribe()
        try:
            event_log.log_event("a", "b", "info", "hello")
            rec = await asyncio.wait_for(q.get(), 1.0)
            self.assertEqual(rec["summary"], "hello")
        finally:
            event_log.unsubscribe(q)

    async def test_slow_subscriber_drops_instead_of_blocking(self):
        q = event_log.subscribe()
        try:
            for i in range(q.maxsize + 50):
                event_log.log_event("a", "b", "info", str(i))
            self.assertEqual(q.qsize(), q.maxsize)
        finally:
            event_log.unsubscribe(q)


if __name__ == "__main__":
    unittest.main()
