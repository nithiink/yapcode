import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.approval import Approval  # noqa: E402
from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.domain.mission import Mission, MissionStep  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.store.base import PendingApprovalExists  # noqa: E402
from yuri.store.sqlite import SCHEMA_VERSION, SqliteStore  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "yuri.db")
        self.store = SqliteStore(self.path)
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _project(self, slug="p"):
        p = Project(slug=slug, name=slug.upper(), root_path="/tmp/" + slug)
        self.store.projects.insert(p)
        return p

    def test_migrate_idempotent_and_versioned(self):
        self.store.migrate()
        self.assertEqual(self.store.settings.get("schema_version"), SCHEMA_VERSION)
        con = sqlite3.connect(self.path)
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertTrue({"projects", "missions", "mission_steps", "sessions", "approvals",
                         "events", "settings"} <= names)

    def test_project_round_trip(self):
        p = Project(slug="x", name="X", root_path="/tmp/x", auto_approve_edits=True)
        self.store.projects.insert(p)
        self.assertEqual(self.store.projects.get(p.id), p)
        self.assertEqual(self.store.projects.get_by_slug("x"), p)
        self.assertEqual(self.store.projects.get_by_root("/tmp/x"), p)
        p.name = "Y"
        self.store.projects.update(p)
        self.assertEqual(self.store.projects.get(p.id).name, "Y")
        self.assertEqual([q.id for q in self.store.projects.list()], [p.id])

    def test_mission_and_steps(self):
        p = Project(slug="x", name="X", root_path="/tmp/x")
        self.store.projects.insert(p)
        m = Mission(title="fix", project_id=p.id, metadata={"a": 1})
        self.store.missions.insert(m)
        st = MissionStep(mission_id=m.id, ordinal=1, title="work")
        self.store.missions.insert_step(st)
        self.assertEqual(self.store.missions.get(m.id), m)
        self.assertEqual(self.store.missions.steps_for(m.id), [st])
        m.transition("paused")
        self.store.missions.update(m)
        self.assertEqual([x.id for x in self.store.missions.list(status="paused")], [m.id])
        self.assertEqual(self.store.missions.list(status="running"), [])
        st.status = "done"
        self.store.missions.update_step(st)
        self.assertEqual(self.store.missions.steps_for(m.id)[0].status, "done")

    def test_session_lookups(self):
        p = Project(slug="x", name="X", root_path="/tmp/x")
        self.store.projects.insert(p)
        s = AgentSession(project_id=p.id, agent_id="claude-code", native_session_id="h1",
                         backend="cli", working_directory="/tmp/x", status="running")
        self.store.sessions.insert(s)
        self.assertEqual(self.store.sessions.get_by_native("h1"), s)
        s2 = AgentSession(project_id=p.id, agent_id="claude-code", native_session_id="h2",
                          backend="cli", working_directory="/tmp/x", status="stopped")
        self.store.sessions.insert(s2)
        self.assertEqual([x.id for x in self.store.sessions.list(live_only=True)], [s.id])
        s.status = "lost"
        self.store.sessions.update(s)
        self.assertEqual(self.store.sessions.list(live_only=True), [])

    def test_one_pending_approval_per_session(self):
        a1 = Approval(session_id="s1", agent_id="a", action="run", tool_name="Bash", request_id="r1")
        self.store.approvals.insert(a1)
        a2 = Approval(session_id="s1", agent_id="a", action="run", tool_name="Bash", request_id="r2")
        with self.assertRaises(PendingApprovalExists):
            self.store.approvals.insert(a2)
        self.assertEqual(self.store.approvals.pending_for_session("s1"), a1)
        a1.resolve("denied", "voice")
        self.store.approvals.update(a1)
        self.store.approvals.insert(a2)  # allowed now
        self.assertEqual(self.store.approvals.get_by_request("r2"), a2)
        self.assertEqual([x.id for x in self.store.approvals.list(status="denied")], [a1.id])

    def test_events_filter_and_order(self):
        for i in range(3):
            self.store.events.insert(YuriEvent.make(EventType.TOOL_STARTED, mission_id="m1",
                                                    payload={"i": i}))
        self.store.events.insert(YuriEvent.make(EventType.TOOL_STARTED, mission_id="m2"))
        got = self.store.events.list(mission_id="m1")
        self.assertEqual([e.payload["i"] for e in got], [0, 1, 2])
        self.assertEqual(len(self.store.events.list(limit=2)), 2)
        since = got[1].ts
        later = self.store.events.list(mission_id="m1", since=since)
        self.assertTrue(all(e.ts >= since for e in later))

    def test_delete_mission_removes_it_and_its_steps(self):
        m = Mission(title="scratch", project_id=self._project().id)
        self.store.missions.insert(m)
        self.store.missions.insert_step(MissionStep(mission_id=m.id, ordinal=0, title="one"))
        self.store.missions.insert_step(MissionStep(mission_id=m.id, ordinal=1, title="two"))
        self.assertEqual(len(self.store.missions.steps_for(m.id)), 2)

        self.store.missions.delete(m.id)
        self.assertIsNone(self.store.missions.get(m.id))
        self.assertEqual(self.store.missions.steps_for(m.id), [],
                         "steps outlived the mission that owned them")

    def test_delete_mission_leaves_other_missions_and_their_steps_alone(self):
        pid = self._project().id
        keep = Mission(title="keep", project_id=pid)
        drop = Mission(title="drop", project_id=pid)
        for m in (keep, drop):
            self.store.missions.insert(m)
            self.store.missions.insert_step(MissionStep(mission_id=m.id, ordinal=0, title="s"))

        self.store.missions.delete(drop.id)
        self.assertIsNotNone(self.store.missions.get(keep.id))
        self.assertEqual(len(self.store.missions.steps_for(keep.id)), 1)

    def test_deleting_a_missing_mission_is_a_no_op(self):
        self.store.missions.delete("nope")          # must not raise

    def test_detaching_sessions_keeps_the_rows_and_clears_the_link(self):
        """A session row is the record of a real agent session. The mission
        going away does not mean the session never happened, so the row stays
        and only the link is cleared."""
        pid = self._project().id
        m = Mission(title="scratch", project_id=pid)
        keep_mission = Mission(title="keeper", project_id=pid)
        self.store.missions.insert(m)
        self.store.missions.insert(keep_mission)
        mine = AgentSession(project_id=pid, agent_id="fake", native_session_id="h1",
                            backend="cli", working_directory="/tmp", mission_id=m.id,
                            status="stopped")
        other = AgentSession(project_id=pid, agent_id="fake", native_session_id="h2",
                             backend="cli", working_directory="/tmp",
                             mission_id=keep_mission.id, status="stopped")
        self.store.sessions.insert(mine)
        self.store.sessions.insert(other)

        self.store.sessions.detach_mission(m.id)
        self.assertIsNone(self.store.sessions.get(mine.id).mission_id)
        self.assertEqual(self.store.sessions.get(other.id).mission_id, keep_mission.id,
                         "detached a session belonging to a different mission")

    def test_settings_json(self):
        self.store.settings.set("k", {"x": [1, 2]})
        self.assertEqual(self.store.settings.get("k"), {"x": [1, 2]})
        self.assertEqual(self.store.settings.get("missing", 7), 7)

    def test_threads_get_their_own_connection(self):
        errors = []

        def work(n):
            try:
                self.store.settings.set(f"t{n}", n)
            except Exception as e:  # pragma: no cover
                errors.append(e)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(self.store.settings.get("t3"), 3)

    def test_close_closes_connections_opened_on_other_threads(self):
        """close() has to actually close them, not just forget them.

        Connections are per-thread and only ever USED by their owning thread,
        but close() runs on whichever thread tears the store down — sqlite's
        same-thread assertion would otherwise make `close()` raise and (when
        that raise is swallowed) silently leave the connection open, so a
        clean shutdown never checkpoints its WAL. The event bus hits this on
        the real path: it persists via asyncio.to_thread, i.e. on an executor
        thread we get no shutdown hook on.

        The probe deliberately runs on the OWNING thread: sqlite raises the same
        `ProgrammingError` class for "wrong thread" as for "closed database", so
        probing from the closing thread would pass whether or not close() worked.
        """
        opened, released = threading.Event(), threading.Event()
        probe: dict[str, str] = {}

        def work():
            con = self.store._conn.get()             # this thread's connection
            self.store.settings.set("from-a-thread", 1)
            opened.set()
            released.wait(5)
            try:
                con.execute("SELECT 1")
                probe["state"] = "still open"
            except sqlite3.ProgrammingError as exc:
                probe["state"] = str(exc)

        t = threading.Thread(target=work)
        t.start()
        self.assertTrue(opened.wait(5))
        self.assertGreaterEqual(len(self.store._conn._all), 2)   # main thread + the worker
        self.store.close()                                       # from the MAIN thread
        released.set()
        t.join(5)
        self.assertIn("closed", probe.get("state", "").lower())


if __name__ == "__main__":
    unittest.main()
