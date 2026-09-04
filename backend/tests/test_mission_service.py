import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.domain.mission import InvalidTransition  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.domain.task import Task  # noqa: E402
from yuri.domain.workflow import Workflow  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.missions import MissionInUse, MissionService  # noqa: E402
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

    async def test_create_writes_no_step_row(self):
        """create() used to write one `mission_steps` row titled 'work'.
        Migration 0003 drained that table and the workflow owns tasks now, so
        that row was written and never read, and `current_step` pointed at
        something nothing could advance. Both must stay gone."""
        m = self.svc.create(self.project, "Fix billing", created_by="voice", agent_id="claude-code")
        self.assertEqual(self.store.missions.steps_for(m.id), [])
        self.assertIsNone(m.current_step)
        self.assertIsNone(self.store.missions.get(m.id).current_step)
        self.assertEqual(m.status, "running")
        self.assertEqual(self._types(), ["mission.created"])
        self.assertIn("Fix billing", Journal(self.home).read_today())

    async def test_detail_steps_are_the_missions_tasks(self):
        """detail()'s `steps` key keeps its name (the mission view reads it)
        but now carries the workflow's tasks."""
        m = self.svc.create(self.project, "Fix billing", created_by="voice")
        self.assertEqual(self.svc.detail(m.id)["steps"], [])   # no workflow planned yet
        w = Workflow(mission_id=m.id, status="running")
        self.store.workflows.insert(w)
        t = Task(workflow_id=w.id, ordinal=1, title="Investigate", role="researcher")
        self.store.tasks.insert(t)
        steps = self.svc.detail(m.id)["steps"]
        self.assertEqual([(s["ordinal"], s["title"], s["status"]) for s in steps],
                         [(1, "Investigate", "pending")])
        self.assertEqual(steps[0]["id"], t.id)

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

    async def test_pause_checks_the_edge_before_interrupting(self):
        """A `queued` mission is in ACTIVE (so "pause that" resolves to it) but
        `queued → paused` is not in TRANSITIONS. Interrupting first stopped the
        agents and THEN raised, leaving the mission running-but-idle."""
        m = self.svc.create(self.project, "t", created_by="voice")
        m.status = "queued"
        self.store.missions.update(m)
        interrupted = []

        async def interrupt_many(sessions):
            interrupted.extend(s.id for s in sessions)
        self.svc.interrupt_sessions = interrupt_many
        s = AgentSession(project_id=self.project.id, agent_id="a", native_session_id="h",
                         backend="cli", working_directory="/tmp/p", mission_id=m.id,
                         status="running")
        self.store.sessions.insert(s)
        with self.assertRaises(InvalidTransition):
            await self.svc.pause(m.id, by="voice")
        self.assertEqual(interrupted, [], "agents were interrupted for a transition that failed")
        self.assertEqual(self.svc.get(m.id).status, "queued")

    async def test_pause_still_interrupts_on_a_legal_edge(self):
        m = self.svc.create(self.project, "t", created_by="voice")   # running
        interrupted = []

        async def interrupt_many(sessions):
            interrupted.extend(s.id for s in sessions)
        self.svc.interrupt_sessions = interrupt_many
        s = AgentSession(project_id=self.project.id, agent_id="a", native_session_id="h",
                         backend="cli", working_directory="/tmp/p", mission_id=m.id,
                         status="running")
        self.store.sessions.insert(s)
        await self.svc.pause(m.id, by="voice")
        self.assertEqual(interrupted, [s.id])
        self.assertEqual(self.svc.get(m.id).status, "paused")

    async def test_speech_list_shapes_and_bounds_what_is_spoken(self):
        """The list counterpart of speech_detail. tools.py used to do this
        shaping itself — the only place it reached into the store."""
        for i in range(3):
            m = self.svc.create(self.project, f"m{i}" * 60, created_by="voice",
                                goal="g" * 600)
            self.store.sessions.insert(AgentSession(
                project_id=self.project.id, agent_id="a", native_session_id=f"h{i}",
                backend="cli", working_directory="/tmp/p", mission_id=m.id,
                status="running", name="n" * 300))
        rows = self.svc.speech_list()
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(rows[0]),
                         {"id", "title", "goal", "status", "project", "agents", "sessions"})
        self.assertLessEqual(len(rows[0]["title"]), 80)
        self.assertLessEqual(len(rows[0]["goal"]), 240)
        self.assertTrue(all(len(n) <= 60 for n in rows[0]["sessions"]))
        self.assertEqual(rows[0]["project"], "P")
        self.assertEqual(rows[0]["agents"], ["a"])
        self.assertEqual(len(self.svc.speech_list(limit=2)), 2)

    async def test_speech_list_filters_by_status_and_defaults_to_active(self):
        live = self.svc.create(self.project, "live", created_by="voice")
        done = self.svc.create(self.project, "done", created_by="voice")
        self.svc.set_status(done, "completed", by="ui")
        self.assertEqual([r["id"] for r in self.svc.speech_list()], [live.id])
        self.assertEqual([r["id"] for r in self.svc.speech_list("completed")], [done.id])

    async def test_detail_shape(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        d = self.svc.detail(m.id)
        self.assertEqual(set(d), {"mission", "steps", "sessions", "approvals", "events"})
        self.assertEqual(d["mission"]["id"], m.id)
        with self.assertRaises(KeyError):
            self.svc.detail("nope")


if __name__ == "__main__":
    unittest.main()


class MissionDeleteTests(MissionServiceTests):
    """Deleting a mission is irreversible, so the guardrails are the spec."""

    def _session(self, mission_id, status="stopped", handle="h1"):
        s = AgentSession(project_id=self.project.id, agent_id="a", native_session_id=handle,
                         backend="cli", working_directory="/tmp/p",
                         mission_id=mission_id, status=status)
        self.store.sessions.insert(s)
        return s

    async def test_delete_removes_the_mission_and_emits_an_event(self):
        m = self.svc.create(self.project, "scratch", created_by="voice")
        self._types()
        await self.svc.delete(m.id, by="ui")
        self.assertIsNone(self.store.missions.get(m.id))
        self.assertEqual(self._types(), ["mission.deleted"])

    async def test_delete_refuses_while_sessions_are_live(self):
        m = self.svc.create(self.project, "busy", created_by="voice")
        live = self._session(m.id, status="running")
        with self.assertRaises(MissionInUse):
            await self.svc.delete(m.id, by="ui")
        self.assertIsNotNone(self.store.missions.get(m.id), "deleted a mission with live work")
        self.assertEqual(self.store.sessions.get(live.id).mission_id, m.id)

    async def test_delete_detaches_finished_sessions_but_keeps_their_rows(self):
        m = self.svc.create(self.project, "done", created_by="voice")
        s = self._session(m.id, status="stopped")
        await self.svc.delete(m.id, by="ui")
        row = self.store.sessions.get(s.id)
        self.assertIsNotNone(row, "a session row records a real agent session; it must survive")
        self.assertIsNone(row.mission_id)

    async def test_delete_keeps_the_missions_events(self):
        """Events are an append-only audit log. Deleting the mission must not
        rewrite the history of what it did."""
        m = self.svc.create(self.project, "audited", created_by="voice")
        # The bus does not persist; the event log is a separate subscriber. Write
        # the row directly so the assertion is about delete(), not about wiring.
        self.store.events.insert(YuriEvent.make(EventType.MISSION_CREATED, mission_id=m.id,
                                                project_id=self.project.id,
                                                payload={"title": m.title}))
        await self.svc.delete(m.id, by="ui")
        kept = self.store.events.list(mission_id=m.id, limit=50)
        self.assertEqual([e.type for e in kept], [EventType.MISSION_CREATED])

    async def test_delete_of_an_unknown_mission_raises_keyerror(self):
        with self.assertRaises(KeyError):
            await self.svc.delete("nope", by="ui")
