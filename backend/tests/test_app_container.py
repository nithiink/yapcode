"""Pins the composition root (yuri/app.py).

These are the wiring facts nothing else can check, because a broken wire here
fails SILENTLY at runtime rather than raising:

  * the observer chain (provider event -> SessionService -> EventBus): without
    it turns/questions/costs never become Yuri events and the Activity panel
    simply stays empty;
  * `missions.stop_sessions` (injected to break the Mission<->Session cycle):
    without it MissionService.cancel leaves live sessions running;
  * the default `bridge=bridge_to_event_log`: without it every Yuri event is
    persisted but never mirrored into the debug bus the UI reads;
  * `bus.start_writer()` in startup(): without it published events pile up in
    the persist queue forever and `drain()` never returns;
  * ONE ClaudeCodeProvider per process, shared with session_manager's provider
    slot: two live TmuxClaudeRunners would fight over the same tmux control dirs.

Runs with a temp home and a temp DB — never the developer's real ~/Yuri.

    python -m unittest discover -s backend/tests
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import event_log  # noqa: E402
import session_manager as sm  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.events.bus import bridge_to_event_log  # noqa: E402
from yuri.providers.base import ProviderEvent  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402


class ContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_container_wires_everything(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d}), \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")):
            fake = FakeAgentProvider()
            c = yapp.test_container(os.path.join(d, "Yuri"), fake)
            try:
                self.assertIs(yapp.container(), c)
                self.assertIs(yapp.container_or_none(), c)
                self.assertEqual(c.projects.home().kind, "home")
                self.assertIs(c.missions.stop_sessions.__self__, c.sessions)
                self.assertTrue(os.path.exists(c.home.db_path))
                self.assertEqual(c.sessions.default_agent, "fake")
                # test_container must NOT bridge into the debug bus, and must not
                # start a writer: its tests would then have to drain.
                self.assertIsNone(c.bus._bridge)
                # observer wired: a provider event lands on the bus
                q = c.bus.subscribe()
                out = await c.sessions.start("Yuri")
                fake.emit(out["session_id"],
                          ProviderEvent("turn_completed", {"assistant_text": "x", "tools_used": []}))
                types = []
                while not q.empty():
                    types.append(q.get_nowait().type)
                self.assertIn("session.turn_completed", types)
            finally:
                yapp.set_container(None)
                c.store.close()
        with self.assertRaises(RuntimeError):
            yapp.container()
        self.assertIsNone(yapp.container_or_none())

    async def test_startup_and_shutdown(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d}), \
             mock.patch.object(config, "YURI_AGENTS", "claude-code"), \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")):
            before = len(event_log.recent(0))
            c = await yapp.startup()
            try:
                self.assertEqual(c.registry.ids(), ["claude-code"])
                # ONE provider instance: the slot and the services share it.
                self.assertIs(sm.provider(), c.registry.get("claude-code"))
                self.assertIs(yapp.container(), c)
                self.assertTrue(os.path.isdir(c.home.workspace_dir))
                self.assertIs(c.bus._bridge, bridge_to_event_log)
                # The bridge actually fires: ensure_home() published
                # project.registered, which must show up in the debug bus.
                mirrored = event_log.recent(0)[before:]
                self.assertIn("yuri", {r["source"] for r in mirrored})
                # start_writer() ran, so drain() returns and rows are persisted.
                await asyncio.wait_for(c.bus.drain(), 5)
                self.assertTrue(c.store.events.list(limit=10))
            finally:
                await yapp.shutdown()
            self.assertIsNone(yapp.container_or_none())
            self.assertIsNone(sm._provider)

    async def test_claude_provider_runner_event_reaches_the_bus(self):
        """The production observer chain, end to end and with no fake shortcut:
        runner.on_event -> ClaudeCodeProvider._on_runner_event -> the observer
        build_container installed -> SessionService -> EventBus. The runner is a
        stub, so no tmux and no Claude."""
        class _StubRunner:
            def __init__(self):
                self.sessions = {}
                self.on_event = None

            async def start(self, cwd, model=None, mode="default"):
                h = "h1-" + "0" * 8
                self.sessions[h] = {"handle": h, "session_id": h, "cwd": cwd, "mode": mode,
                                    "model": model or "opus", "status": "idle", "cost_usd": 0.0}
                return h

            def list(self):
                return list(self.sessions.values())

            def persist_name(self, h, name):
                pass

            async def shutdown(self):
                pass

        from yuri.providers.claude_code import ClaudeCodeProvider
        runner = _StubRunner()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d}), \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")):
            c = yapp.test_container(os.path.join(d, "Yuri"),
                                    ClaudeCodeProvider(runner_factory=lambda b: runner),
                                    default_agent="claude-code")
            try:
                q = c.bus.subscribe()
                handle = (await c.sessions.start("Yuri"))["session_id"]
                runner.on_event(handle, "turn_complete",
                                {"assistant_text": "done", "tools_used": ["Read"]})
                types = []
                while not q.empty():
                    types.append(q.get_nowait().type)
                self.assertIn("session.turn_completed", types)
            finally:
                yapp.set_container(None)
                c.store.close()
                sm.reset()

    async def test_shutdown_persists_an_event_published_during_provider_teardown(self):
        """Teardown order: providers stop FIRST, then the queue is drained,
        then the writer stops. A provider that publishes while tearing down (a
        cancelled turn, or session.stopped under VC_KILL_SESSIONS_ON_SHUTDOWN=1)
        must still reach the store — with the writer stopped first, that event
        lands in the persist queue with no consumer and is lost."""
        class _PublishesOnShutdown(FakeAgentProvider):
            bus = None

            async def shutdown(self):
                await super().shutdown()
                self.bus.publish(YuriEvent.make(EventType.SESSION_STOPPED,
                                                payload={"native_session_id": "late"}))

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d}), \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")):
            prov = _PublishesOnShutdown()
            c = yapp.test_container(os.path.join(d, "Yuri"), prov)
            prov.bus = c.bus
            db_path = c.home.db_path
            c.bus.start_writer()            # what startup() does for the real app
            await yapp.shutdown()
            reopened = SqliteStore(db_path)
            try:
                types = [e.type for e in reopened.events.list(limit=50)]
            finally:
                reopened.close()
            self.assertIn(EventType.SESSION_STOPPED, types)

    async def test_shutdown_of_a_writerless_container_is_immediate_and_quiet(self):
        """test_container starts no writer, so drain() would block forever.
        Shutdown must skip the drain rather than stall for the timeout and log
        a warning — a test that shuts its own container down should produce no
        output at all."""
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d}), \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")):
            c = yapp.test_container(os.path.join(d, "Yuri"), FakeAgentProvider())
            c.bus.publish(YuriEvent.make(EventType.TOOL_STARTED))   # queued, no consumer
            started = time.monotonic()
            with self.assertNoLogs("yuri.app", level="WARNING"):
                await yapp.shutdown()
            self.assertLess(time.monotonic() - started, yapp.DRAIN_TIMEOUT_S)
            self.assertIsNone(yapp.container_or_none())

    async def test_a_container_without_claude_code_clears_the_provider_slot(self):
        """The one-provider invariant has to hold unconditionally: a container
        with no claude-code provider must LEAVE NO provider in session_manager,
        or a fake-provider test inherits the previous container's real one."""
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d}), \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")):
            from yuri.providers.claude_code import ClaudeCodeProvider
            first = yapp.test_container(os.path.join(d, "Yuri"),
                                        ClaudeCodeProvider(runner_factory=lambda b: None),
                                        default_agent="claude-code")
            self.assertIsNotNone(sm._provider)          # installed by build_container
            first.store.close()
            second = yapp.test_container(os.path.join(d, "Yuri2"), FakeAgentProvider())
            try:
                self.assertIsNone(sm._provider)         # NOT the previous container's
            finally:
                yapp.set_container(None)
                second.store.close()
                sm.reset()

    async def test_shutdown_without_a_container_is_a_no_op(self):
        yapp.set_container(None)
        await yapp.shutdown()
        self.assertIsNone(yapp.container_or_none())


if __name__ == "__main__":
    unittest.main()
