import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.mission import InvalidTransition  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.missions import MissionService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402


class MissionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.q = self.bus.subscribe()
        self.svc = MissionService(self.store, self.bus, Journal(self.home))
        self.project = Project(slug="p", name="P", root_path="/tmp/p")
        self.store.projects.insert(self.project)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _types(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait().type)
        return out

    async def test_create_has_one_step_and_event(self):
        m = self.svc.create(self.project, "Fix billing", created_by="voice", agent_id="claude-code")
        steps = self.store.missions.steps_for(m.id)
        self.assertEqual([(s.ordinal, s.title, s.agent_id, s.status) for s in steps],
                         [(1, "work", "claude-code", "running")])
        self.assertEqual(m.status, "running")
        self.assertEqual(self._types(), ["mission.created"])
        self.assertIn("Fix billing", Journal(self.home).read_today())

    async def test_goal_set_once(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        self.svc.set_goal_if_empty(m, "x" * 600)
        self.svc.set_goal_if_empty(m, "second")
        self.assertEqual(len(self.svc.get(m.id).goal), 500)

    async def test_pause_resume_cancel_and_events(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        self._types()
        m = await self.svc.pause(m.id, by="ui")
        self.assertEqual(m.status, "paused")
        m = await self.svc.resume(m.id, by="ui")
        self.assertEqual(m.status, "running")
        stopped = []

        async def stop_many(sessions):
            stopped.extend(s.id for s in sessions)
        self.svc.stop_sessions = stop_many
        s = AgentSession(project_id=self.project.id, agent_id="a", native_session_id="h",
                         backend="cli", working_directory="/tmp/p", mission_id=m.id, status="running")
        self.store.sessions.insert(s)
        m = await self.svc.cancel(m.id, by="ui")
        self.assertEqual(m.status, "cancelled")
        self.assertEqual(stopped, [s.id])
        self.assertEqual(self._types(), ["mission.status_changed"] * 3)

    async def test_invalid_transition_raises(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        await self.svc.cancel(m.id, by="ui")
        with self.assertRaises(InvalidTransition):
            await self.svc.resume(m.id, by="ui")

    async def test_set_status_same_state_is_silent(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        self._types()  # drain mission.created
        journal_path = Journal(self.home).today_path()
        with open(journal_path, "rb") as f:
            before_journal = f.read()
        before_updated_at = m.updated_at

        result = self.svc.set_status(m, "running", by="ui")  # already running
        self.assertFalse(result)
        self.assertTrue(self.q.empty())
        with open(journal_path, "rb") as f:
            self.assertEqual(f.read(), before_journal)
        self.assertEqual(m.updated_at, before_updated_at)

        # Same guarantee through the public async path.
        m2 = await self.svc.resume(m.id, by="ui")  # already running
        self.assertEqual(m2.status, "running")
        self.assertEqual(m2.updated_at, before_updated_at)
        self.assertTrue(self.q.empty())
        with open(journal_path, "rb") as f:
            self.assertEqual(f.read(), before_journal)

    async def test_detail_shape(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        d = self.svc.detail(m.id)
        self.assertEqual(set(d), {"mission", "steps", "sessions", "approvals", "events"})
        self.assertEqual(d["mission"]["id"], m.id)
        with self.assertRaises(KeyError):
            self.svc.detail("nope")


if __name__ == "__main__":
    unittest.main()
