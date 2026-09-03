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
        self.assertIsInstance(await self.p.read(h), str)
        await self.p.stop(h)
        self.assertNotIn(h, {s["handle"] for s in self.p.list_native()})

    async def test_unknown_handle_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.p.poll("does-not-exist")

    async def test_set_mode_matches_declared_permission_modes(self):
        """A provider must honour what its own capabilities() claims: modes work
        if it declares any, and are refused if it declares none. Claude has four;
        OpenCode has no equivalent concept."""
        modes = self.p.capabilities().permission_modes
        h = await self.p.create_session(self.ctx, self.opts())
        try:
            if modes:
                target = modes[0]
                self.assertEqual(await self.p.set_mode(h, target), target)
            else:
                with self.assertRaises(NotImplementedError):
                    await self.p.set_mode(h, "plan")
        finally:
            await self.p.stop(h)

    async def test_observer_matches_declared_event_support(self):
        """supports_events is a promise. A provider that makes it must deliver a
        ProviderEvent to the observer; one that does not must still accept an
        observer without raising, because build_container installs one on every
        provider uniformly."""
        got = []
        self.p.set_observer(lambda h, ev: got.append((h, ev)))
        h = await self.p.create_session(self.ctx, self.opts())
        try:
            if self.p.capabilities().supports_events:
                self._fire_event(h)
                self.assertTrue(got, "declares supports_events but the observer never fired")
                self.assertIsInstance(got[0][1], ProviderEvent)
            else:
                # Nothing to fire; the point is that installing one is harmless.
                self.assertEqual(got, [])
        finally:
            await self.p.stop(h)

    def _fire_event(self, handle):
        """Trigger a provider event for `handle`. Subclasses that declare
        supports_events must override this; others never have it called."""
        raise NotImplementedError(
            "this provider declares supports_events=True, so the contract test "
            "needs _fire_event to trigger one")
