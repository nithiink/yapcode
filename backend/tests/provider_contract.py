"""Contract every AgentProvider must satisfy (spec §45). Subclass in a
test_*.py, set `make_provider()` and `project_root`, and the lifecycle
assertions run against your implementation."""
import unittest

from yuri.providers.base import ProjectContext, ProviderEvent, SessionOptions


class AgentProviderContract(unittest.IsolatedAsyncioTestCase):
    project_root = "/tmp"

    def make_provider(self):
        raise NotImplementedError

    def opts(self):
        return SessionOptions()

    async def asyncSetUp(self):
        self.p = self.make_provider()
        self.ctx = ProjectContext(project_id="p1", root_path=self.project_root)

    async def asyncTearDown(self):
        await self.p.shutdown()

    async def test_identity_and_capabilities(self):
        self.assertTrue(self.p.id)
        self.assertTrue(self.p.name)
        caps = self.p.capabilities()
        self.assertIsInstance(caps.to_dict()["permission_modes"], list)

    async def test_health_returns_health(self):
        h = await self.p.health()
        self.assertIsInstance(h.online, bool)
        self.assertTrue(h.checked_at.endswith("Z"))

    async def test_lifecycle_create_send_poll_stop(self):
        h = await self.p.create_session(self.ctx, self.opts())
        self.assertTrue(h)
        listed = {s["handle"]: s for s in self.p.list_native()}
        self.assertIn(h, listed)
        self.assertEqual(listed[h]["cwd"], self.project_root)
        self.p.send_message(h, "hello")
        res = self.p.poll(h)
        self.assertIn(res["status"], {"working", "idle", "completed"})
        self.assertEqual(res["session_id"], h)
        await self.p.interrupt(h)
        mode = await self.p.set_mode(h, "plan")
        self.assertEqual(mode, "plan")
        self.assertIsInstance(await self.p.read(h), str)
        await self.p.stop(h)
        self.assertNotIn(h, {s["handle"] for s in self.p.list_native()})

    async def test_unknown_handle_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.p.poll("does-not-exist")

    async def test_observer_receives_events(self):
        got = []
        self.p.set_observer(lambda h, ev: got.append((h, ev)))
        h = await self.p.create_session(self.ctx, self.opts())
        self._fire_event(h)
        self.assertTrue(got, "observer never called")
        self.assertIsInstance(got[0][1], ProviderEvent)
        await self.p.stop(h)

    def _fire_event(self, handle):
        """Subclasses trigger a provider event for `handle` here."""
        raise NotImplementedError
