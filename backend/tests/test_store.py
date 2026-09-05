import inspect
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.approval import Approval  # noqa: E402
from yuri.domain.artifact import Artifact  # noqa: E402
from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.domain.mission import Mission, MissionStep  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.domain.specialist import Specialist  # noqa: E402
from yuri.domain.task import Task  # noqa: E402
from yuri.domain.workflow import Workflow  # noqa: E402
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


class Phase7StoreTests(StoreTests):
    """The roster and workflow tables. Inherits StoreTests' fixtures."""

    _n = 0

    def _mission(self):
        # A fresh project each time: projects.root_path is unique, so reusing
        # the default slug makes the second call an IntegrityError.
        Phase7StoreTests._n += 1
        m = Mission(title="t", project_id=self._project(f"p{self._n}").id)
        self.store.missions.insert(m)
        return m

    def _wf(self, mission_id, status="running"):
        w = Workflow(mission_id=mission_id, status=status)
        self.store.workflows.insert(w)
        return w

    # --- serialisation -----------------------------------------------------

    def test_task_json_and_bool_columns_round_trip(self):
        # The whole point of extending _JSON_COLS/_BOOL_COLS. Without it a
        # list is stored as a Python repr with no exception raised anywhere.
        w = self._wf(self._mission().id)
        t = Task(workflow_id=w.id, ordinal=0, title="t", role="reviewer",
                 requires=("code_review", "git"), verification=("tests_pass",),
                 read_only=True)
        self.store.tasks.insert(t)
        back = self.store.tasks.get(t.id)
        self.assertEqual(back.requires, ("code_review", "git"))
        self.assertEqual(back.verification, ("tests_pass",))
        self.assertIs(back.read_only, True, "read_only came back as an int, not a bool")

    def test_specialist_json_and_bool_columns_round_trip(self):
        s = Specialist(name="Reviewer", role="reviewer", provider_id="claude-code",
                       tools=("Read", "Grep"), capabilities=("code_review",), builtin=True)
        self.store.specialists.insert(s)
        back = self.store.specialists.get(s.id)
        self.assertEqual(back.tools, ("Read", "Grep"))
        self.assertEqual(back.capabilities, ("code_review",))
        self.assertIs(back.builtin, True)
        self.assertIs(back.archived, False)

    # --- invariants the store enforces -------------------------------------

    def test_one_live_workflow_per_mission(self):
        m = self._mission()
        self._wf(m.id)
        with self.assertRaises(sqlite3.IntegrityError):
            self._wf(m.id)

    def test_a_superseded_workflow_does_not_block_a_new_one(self):
        m = self._mission()
        old = self._wf(m.id)
        old.status = "cancelled"
        self.store.workflows.update(old)
        self._wf(m.id)          # must not raise
        self.assertEqual(len(self.store.workflows.for_mission(m.id)), 2)
        self.assertEqual([w.status for w in self.store.workflows.for_mission(m.id, live_only=True)],
                         ["running"])

    def test_an_archived_specialist_frees_its_name_and_slug(self):
        a = Specialist(name="Reviewer", role="reviewer", provider_id="claude-code")
        self.store.specialists.insert(a)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.specialists.insert(
                Specialist(name="Reviewer", role="reviewer", provider_id="opencode"))
        a.archived = True
        self.store.specialists.update(a)
        self.store.specialists.insert(
            Specialist(name="Reviewer", role="reviewer", provider_id="opencode"))

    def test_list_hides_archived_unless_asked(self):
        s = Specialist(name="Gone", role="tester", provider_id="claude-code", archived=True)
        self.store.specialists.insert(s)
        self.assertEqual(self.store.specialists.list(), [])
        self.assertEqual([x.id for x in self.store.specialists.list(include_archived=True)],
                         [s.id])

    def test_get_by_name_and_slug_ignore_archived_rows(self):
        s = Specialist(name="Gone", role="tester", provider_id="claude-code", archived=True)
        self.store.specialists.insert(s)
        self.assertIsNone(self.store.specialists.get_by_name("Gone"))
        self.assertIsNone(self.store.specialists.get_by_slug("gone"))
        self.assertIsNotNone(self.store.specialists.get(s.id),
                             "get() by id must still find it — a task points at it")

    # --- dependencies ------------------------------------------------------

    def test_deps_come_back_as_a_map_and_absent_means_no_dependencies(self):
        w = self._wf(self._mission().id)
        a = Task(workflow_id=w.id, ordinal=0, title="a", role="researcher")
        b = Task(workflow_id=w.id, ordinal=1, title="b", role="developer")
        for t in (a, b):
            self.store.tasks.insert(t)
        self.store.tasks.add_dep(b.id, a.id)
        deps = self.store.tasks.deps_for(w.id)
        self.assertEqual(deps[b.id], {a.id})
        self.assertNotIn(a.id, deps, "a dependency-free task must be ABSENT, not empty")

    def test_adding_the_same_edge_twice_is_a_no_op(self):
        # The engine re-derives edges on retry and must not have to check first.
        w = self._wf(self._mission().id)
        a = Task(workflow_id=w.id, ordinal=0, title="a", role="researcher")
        b = Task(workflow_id=w.id, ordinal=1, title="b", role="developer")
        for t in (a, b):
            self.store.tasks.insert(t)
        self.store.tasks.add_dep(b.id, a.id)
        self.store.tasks.add_dep(b.id, a.id)
        self.assertEqual(self.store.tasks.deps_for(w.id)[b.id], {a.id})

    def test_a_task_cannot_depend_on_itself(self):
        w = self._wf(self._mission().id)
        a = Task(workflow_id=w.id, ordinal=0, title="a", role="researcher")
        self.store.tasks.insert(a)
        with self.assertRaises(ValueError):
            self.store.tasks.add_dep(a.id, a.id)

    def test_deps_are_scoped_to_one_workflow(self):
        m1, m2 = self._mission(), self._mission()
        w1, w2 = self._wf(m1.id), self._wf(m2.id)
        for w in (w1, w2):
            a = Task(workflow_id=w.id, ordinal=0, title="a", role="researcher")
            b = Task(workflow_id=w.id, ordinal=1, title="b", role="developer")
            self.store.tasks.insert(a)
            self.store.tasks.insert(b)
            self.store.tasks.add_dep(b.id, a.id)
        self.assertEqual(len(self.store.tasks.deps_for(w1.id)), 1)

    # --- holders_of, which is what makes archiving refusable ---------------

    def test_holders_of_sees_only_live_tasks_by_default(self):
        w = self._wf(self._mission().id)
        s = Specialist(name="R", role="reviewer", provider_id="claude-code")
        self.store.specialists.insert(s)
        live = Task(workflow_id=w.id, ordinal=0, title="live", specialist_id=s.id,
                    status="running")
        done = Task(workflow_id=w.id, ordinal=1, title="done", specialist_id=s.id,
                    status="completed")
        for t in (live, done):
            self.store.tasks.insert(t)
        self.assertEqual([t.id for t in self.store.tasks.holders_of(s.id)], [live.id])
        self.assertEqual(len(self.store.tasks.holders_of(s.id, live_only=False)), 2)

    # --- artifacts ---------------------------------------------------------

    def test_artifacts_are_readable_per_mission_and_per_task(self):
        m = self._mission()
        w = self._wf(m.id)
        t = Task(workflow_id=w.id, ordinal=0, title="t", role="researcher")
        self.store.tasks.insert(t)
        self.store.artifacts.insert(Artifact(mission_id=m.id, task_id=t.id,
                                             kind="finding", title="f", body="b"))
        self.store.artifacts.insert(Artifact(mission_id=m.id, kind="summary",
                                             title="s", body="b"))
        self.assertEqual(len(self.store.artifacts.for_mission(m.id)), 2)
        self.assertEqual(len(self.store.artifacts.for_task(t.id)), 1)


class MigrationConversionTests(unittest.TestCase):
    """0003 converts pre-Phase-7 mission_steps into one-task workflows.

    Built by migrating to 0002 only, inserting the old shape, then running the
    rest — otherwise the conversion has nothing to convert and the test
    passes vacuously.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "yuri.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _migrate_to(self, version):
        import yuri.store.sqlite as mod
        store = SqliteStore(self.path)
        real = os.listdir(mod._MIGRATIONS_DIR)
        keep = [f for f in real if f.endswith(".sql") and int(f.split("_", 1)[0]) <= version]
        with mock.patch.object(mod.os, "listdir", return_value=keep):
            store.migrate()
        return store

    def _old_rows(self, store, *, mission_status, step_status):
        """Write the pre-Phase-7 shape with raw SQL, not through the repos.

        This simulates a database written by an OLDER build, and today's
        dataclasses carry columns that build did not have (Project.metadata
        arrived in 0004). Inserting through the repos would write today's shape
        into yesterday's table and fail on the column, testing the test rather
        than the migration. Returns (project_id, mission_id, step_id).
        """
        con = store._conn.get()
        pid, mid, sid = "p-old", "m-old", "s-old"
        now = "2026-09-01T00:00:00Z"
        con.execute("INSERT INTO projects (id, slug, name, root_path, kind, "
                    "auto_approve_edits, created_at, updated_at) "
                    "VALUES (?,?,?,?,'user',0,?,?)", (pid, "x", "X", "/tmp/x", now, now))
        con.execute("INSERT INTO missions (id, title, project_id, status, priority, "
                    "created_by, metadata, created_at, updated_at) "
                    "VALUES (?,?,?,?,0,'voice','{}',?,?)",
                    (mid, "old work", pid, mission_status, now, now))
        con.execute("INSERT INTO mission_steps (id, mission_id, ordinal, title, status, result) "
                    "VALUES (?,?,1,'work',?,'{}')", (sid, mid, step_status))
        con.execute("UPDATE missions SET current_step = ? WHERE id = ?", (sid, mid))
        con.commit()
        return pid, mid, sid

    def test_an_old_mission_becomes_a_one_task_workflow(self):
        store = self._migrate_to(2)
        _, mission_id, step_id = self._old_rows(store, mission_status="completed",
                                                step_status="done")
        store.close()

        store = SqliteStore(self.path)
        store.migrate()                     # now runs 0003
        try:
            flows = store.workflows.for_mission(mission_id)
            self.assertEqual(len(flows), 1)
            self.assertEqual((flows[0].template, flows[0].status), ("single", "completed"))
            tasks = store.tasks.for_workflow(flows[0].id)
            self.assertEqual([(t.title, t.status) for t in tasks], [("work", "completed")])
            self.assertEqual(tasks[0].id, step_id,
                             "the task must reuse the step's id — missions.current_step "
                             "still points at it")
            con = sqlite3.connect(self.path)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM mission_steps").fetchone()[0], 0,
                                 "0003 should have drained mission_steps")
            finally:
                con.close()
        finally:
            store.close()

    def test_a_live_old_mission_gets_a_running_workflow(self):
        store = self._migrate_to(2)
        _, mission_id, _ = self._old_rows(store, mission_status="running",
                                          step_status="running")
        store.close()
        store = SqliteStore(self.path)
        store.migrate()
        try:
            flow = store.workflows.for_mission(mission_id, live_only=True)[0]
            self.assertEqual(flow.status, "running")
            self.assertEqual(store.tasks.for_workflow(flow.id)[0].status, "running")
        finally:
            store.close()


class EveryReferenceToAMissionIsHandledTests(unittest.TestCase):
    """A new table pointing at missions(id) must not silently break delete.

    This has now happened once: migration 0003 added workflows, tasks and
    artifacts, and SqliteMissions.delete — written in 0001 — knew nothing
    about them, so from Phase 7 onwards deleting any mission that had a plan
    answered IntegrityError → HTTP 500. Reported from a real run, on a
    mission the user could see and could not remove.

    Reading the schema rather than a hand-kept list, so the next table is
    caught by adding it, not by remembering to update a test.

    What this checks precisely: that `delete` MENTIONS every table that
    references the rows it removes. That is the failure that happened — three
    tables nobody thought about — and it is verified to fire against the
    original pre-fix body, which named only mission_steps and missions. It
    does NOT check that the handling is correct; test_mission_service's
    MissionDeleteTests do that behaviourally, with real rows.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(os.path.join(self.tmp.name, "y.db"))
        self.store.migrate()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def _tables(self) -> list[str]:
        con = self.store._conn.get()
        return [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

    def test_delete_mentions_every_table_that_references_a_mission(self):
        con = self.store._conn.get()
        # `sessions` is the one deliberate exception: its mission_id is
        # nullable and MissionService detaches it, because a session row
        # records an agent run that really happened.
        detached = {"sessions"}
        source = inspect.getsource(type(self.store.missions).delete)
        missing = []
        for table in self._tables():
            refs = [r for r in con.execute(f"PRAGMA foreign_key_list({table})")
                    if r["table"] == "missions"]
            if not refs or table in detached:
                continue
            if table not in source:
                missing.append(table)
        self.assertEqual(missing, [],
                         f"these tables reference missions(id) but SqliteMissions.delete "
                         f"never mentions them, so deleting a mission that has any row in "
                         f"them will raise IntegrityError: {missing}")

    def test_delete_mentions_every_table_that_references_a_task(self):
        # Same failure one level down: a table hanging off tasks blocks the
        # DELETE FROM tasks inside the same transaction.
        con = self.store._conn.get()
        source = inspect.getsource(type(self.store.missions).delete)
        missing = [
            table for table in self._tables()
            if any(r["table"] == "tasks" for r in con.execute(f"PRAGMA foreign_key_list({table})"))
            and table not in source
        ]
        self.assertEqual(missing, [], f"these reference tasks(id) and would block the "
                                      f"cascade: {missing}")
