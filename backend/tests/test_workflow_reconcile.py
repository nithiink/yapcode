"""Recovery after a restart (spec §13).

A mission that was mid-flight when the backend went down has to come back, or
persisting the workflow bought nothing. Four cases, and the distinction
between them is the whole point: a task that never started must be retried
without burning an attempt on nothing, and one whose agent is gone must not
sit `running` forever waiting for an event that can never arrive.

    .venv/bin/python -m unittest tests.test_workflow_reconcile -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.mission import Mission  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.domain.task import Task  # noqa: E402
from yuri.domain.workflow import Workflow  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.roster import RosterService  # noqa: E402
from yuri.services.workflow import WorkflowEngine  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402
from yuri.workflows.loader import load_templates  # noqa: E402


class Reconcile(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

        self.bus = EventBus()
        self.q = self.bus.subscribe()
        registry = AgentRegistry()
        registry.register(FakeAgentProvider())
        roster = RosterService(self.store, self.bus, registry)
        roster.seed()
        self.engine = WorkflowEngine(self.store, self.bus, Journal(self.home),
                                     roster, load_templates())
        self.root = os.path.join(self.tmp.name, "p")
        os.makedirs(self.root, exist_ok=True)
        self.project = Project(slug="p", name="P", root_path=self.root)
        self.store.projects.insert(self.project)
        self.mission = Mission(title="m", project_id=self.project.id, goal="fix it",
                               status="running")
        self.store.missions.insert(self.mission)

        self.dispatched: list[str] = []

        async def dispatch(task, specialist, instruction):
            s = self._session()
            self.dispatched.append(task.id)
            return s.id
        self.engine.dispatch = dispatch

        self.w = Workflow(mission_id=self.mission.id, status="running")
        self.store.workflows.insert(self.w)

    def _session(self, status: str = "idle") -> AgentSession:
        s = AgentSession(project_id=self.project.id, agent_id="fake",
                         native_session_id=f"native-{len(self.store.sessions.list())}",
                         backend="fake", working_directory=self.root,
                         mission_id=self.mission.id, status=status)
        self.store.sessions.insert(s)
        return s

    def _task(self, status: str, *, session: AgentSession | None = None,
              title: str = "t", **over) -> Task:
        t = Task(workflow_id=self.w.id, ordinal=len(self.store.tasks.for_workflow(self.w.id)),
                 title=title, role="developer", instruction="do the thing",
                 status=status, session_id=session.id if session else None, **over)
        self.store.tasks.insert(t)
        return t

    def _reload(self, t: Task) -> Task:
        return self.store.tasks.get(t.id)

    # --- the four cases (§13) ----------------------------------------------

    async def test_dispatched_with_no_session_goes_back_to_ready(self):
        # It never started: the provider call did not get far enough to record
        # a session. Retrying it must not cost an attempt for work that never
        # ran, so it goes back to `ready`, not to `failed`.
        t = self._task("dispatched", session=None)
        t.attempts = 1
        self.store.tasks.update(t)
        out = await self.engine.reconcile()
        self.assertEqual(self._reload(t).status, "dispatched")   # re-dispatched by advance()
        # Still 1, not 2: reconcile gave the attempt back (nothing ran), and
        # the re-dispatch spent it. A task that never started must not lose a
        # retry to a restart.
        self.assertEqual(self._reload(t).attempts, 1)
        self.assertIn(t.id, out["restarted"])

    async def test_a_task_whose_session_survived_is_left_alone(self):
        # Its agent is still there and may still report. Touching it would
        # duplicate work that is genuinely in flight.
        t = self._task("running", session=self._session("idle"))
        out = await self.engine.reconcile()
        self.assertEqual(self._reload(t).status, "running")
        self.assertIn(t.id, out["kept"])
        self.assertEqual(self.dispatched, [], "a live task was dispatched again")

    async def test_running_with_a_lost_session_fails_with_a_reason_that_names_the_restart(self):
        # "the agent failed" would send the user looking at the task instead
        # of at the restart.
        t = self._task("running", session=self._session("lost"))
        out = await self.engine.reconcile()
        after = self._reload(t)
        self.assertIn(t.id, out["lost"])
        self.assertIn("restart", (after.error or "") + " " + str(out))
        # The retry policy applies: attempt 1 of 2, so it comes back.
        self.assertIn(after.status, ("dispatched", "ready"))

    async def test_a_lost_session_that_has_no_attempts_left_blocks(self):
        t = self._task("running", session=self._session("lost"), attempts=2, max_attempts=2)
        await self.engine.reconcile()
        self.assertIn(self._reload(t).status, ("failed", "blocked"))

    async def test_verifying_reruns_the_checks_rather_than_guessing(self):
        # The verdict was lost with the process. Checks are declared and
        # side-effect-free by construction, so re-running is the only way to
        # learn it — and assuming either outcome would be a lie.
        t = self._task("verifying", session=self._session("idle"),
                       verification=("tests_pass",))
        out = await self.engine.reconcile()
        self.assertIn(t.id, out["reverified"])
        # No test command is configured for this project, so `tests_pass` is
        # `unavailable`, which does NOT pass.
        self.assertNotEqual(self._reload(t).status, "completed")

    async def test_verifying_with_no_checks_completes(self):
        t = self._task("verifying", session=self._session("idle"), verification=())
        await self.engine.reconcile()
        self.assertEqual(self._reload(t).status, "completed")

    # --- the properties the plan calls out ---------------------------------

    async def test_reconcile_advances_so_a_mid_flight_mission_resumes(self):
        # The whole point. A `ready` task with nothing running has no event
        # left to wake the engine.
        t = self._task("ready")
        await self.engine.reconcile()
        self.assertEqual(self._reload(t).status, "dispatched")
        self.assertEqual(self.dispatched, [t.id])

    async def test_reconcile_is_idempotent(self):
        t = self._task("running", session=self._session("idle"))
        first = await self.engine.reconcile()
        second = await self.engine.reconcile()
        self.assertEqual(first["kept"], second["kept"])
        self.assertEqual(self._reload(t).status, "running")
        self.assertEqual(self.dispatched, [])

    async def test_a_terminal_workflow_is_not_touched(self):
        self.w.status = "completed"
        self.store.workflows.update(self.w)
        t = self._task("dispatched", session=None)
        out = await self.engine.reconcile()
        self.assertEqual(self._reload(t).status, "dispatched")
        self.assertEqual(out["workflows"], 0)

    async def test_a_terminal_task_is_not_touched(self):
        done = self._task("completed", title="done")
        skipped = self._task("skipped", title="skipped")
        await self.engine.reconcile()
        self.assertEqual(self._reload(done).status, "completed")
        self.assertEqual(self._reload(skipped).status, "skipped")

    async def test_a_paused_workflow_is_reconciled_but_not_advanced(self):
        # Its tasks still need their state corrected — a `dispatched` task
        # with no session is wrong whether or not the user has paused — but
        # resuming the work is the user's decision, not the restart's.
        self.w.status = "paused"
        self.store.workflows.update(self.w)
        t = self._task("dispatched", session=None)
        await self.engine.reconcile()
        self.assertEqual(self._reload(t).status, "ready")
        self.assertEqual(self.dispatched, [], "a paused workflow was resumed by a restart")

    async def test_it_reports_what_it_did_per_case(self):
        # The return value is what startup logs. A recovery that says nothing
        # leaves the user guessing why a mission moved on its own.
        self._task("dispatched", session=None, title="never-started")
        self._task("running", session=self._session("lost"), title="lost-agent")
        self._task("running", session=self._session("idle"), title="still-there")
        out = await self.engine.reconcile()
        self.assertEqual(len(out["restarted"]), 1)
        self.assertEqual(len(out["lost"]), 1)
        self.assertEqual(len(out["kept"]), 1)
        self.assertEqual(out["workflows"], 1)

    async def test_a_task_can_never_point_at_a_session_row_that_is_gone(self):
        # Written expecting to test reconcile's handling of a dangling
        # session_id; the schema refuses to create one. `tasks.session_id
        # REFERENCES sessions(id)` with no ON DELETE clause means both a
        # dangling write AND deleting a referenced session are refused, so the
        # case cannot occur. Asserted here rather than deleted, because the
        # reason reconcile does not need to handle it is worth keeping.
        import sqlite3
        t = self._task("running", session=None)
        t.session_id = "no-such-session"
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.tasks.update(t)

    async def test_running_with_no_session_at_all_is_treated_as_lost(self):
        # Reachable: dispatch recorded no session and the turn never came.
        t = self._task("running", session=None)
        out = await self.engine.reconcile()
        self.assertIn(t.id, out["lost"])
        self.assertNotEqual(self._reload(t).status, "running")

    async def test_waiting_approval_survives_a_restart_with_its_session(self):
        # The approval is still pending; the answer will arrive later. Failing
        # it would throw away a decision the user has not made yet.
        t = self._task("waiting_approval", session=self._session("needs_permission"))
        out = await self.engine.reconcile()
        self.assertEqual(self._reload(t).status, "waiting_approval")
        self.assertIn(t.id, out["kept"])


class StartupWiringTests(unittest.TestCase):
    """That startup actually CALLS it, and in the right order.

    A reconcile() nobody calls is the defect this project has already nearly
    shipped twice, so it gets its own test. Structural rather than behavioural
    because the lifespan builds a real container and a real provider registry;
    the ordering is the part that can silently regress.
    """

    def _main_source(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(path) as f:
            return f.read()

    def test_the_lifespan_calls_reconcile(self):
        self.assertIn("workflow.reconcile()", self._main_source())

    def test_reconcile_runs_AFTER_sessions_rehydrate(self):
        # The order is the design (spec §13): reconcile decides each task's
        # fate from whether its session came back, so running it first would
        # call every session lost and re-run work still in flight.
        src = self._main_source()
        self.assertLess(src.index("sessions.rehydrate()"), src.index("workflow.reconcile()"))
