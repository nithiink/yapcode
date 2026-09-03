"""WorkflowEngine — the scheduler (spec §8, §12).

The engine is tested against a recording `dispatch` double, never against a
real session: `dispatch is None` is a dry run, and a test-supplied hook records
(task_id, specialist_id) without starting anything. That is the whole reason
this is testable before Task 10 wires it to SessionService.

    cd backend && .venv/bin/python -m unittest tests.test_workflow_engine -v
"""
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.mission import Mission  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.roster import RosterService  # noqa: E402
from yuri.services.workflow import (IN_FLIGHT, MAX_MISSION_RUNTIME_S,  # noqa: E402
                                    MAX_PARALLEL_READONLY, MAX_SESSIONS_PER_MISSION,
                                    WorkflowEngine)
from yuri.store.sqlite import SqliteStore  # noqa: E402
from yuri.workflows.loader import load_templates  # noqa: E402


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.q = self.bus.subscribe()
        registry = AgentRegistry()
        registry.register(FakeAgentProvider())
        self.roster = RosterService(self.store, self.bus, registry)
        self.roster.seed()
        self.engine = WorkflowEngine(self.store, self.bus, Journal(self.home),
                                     self.roster, load_templates())
        self.project = Project(slug="p", name="P", root_path="/tmp/p")
        self.store.projects.insert(self.project)
        self.dispatched: list[tuple[str, str]] = []

        async def dispatch(task, specialist, instruction):
            # The double inserts a real session row rather than returning a
            # bare string: `tasks.session_id` carries a foreign key into
            # `sessions` (migration 0003), and §13's reconciliation reads that
            # link to tell a task whose agent never started from one whose did.
            # A double that returned an unbacked id would be testing an engine
            # that could not persist what dispatch gives it.
            # The native handle is per-DISPATCH, not per-task: `sessions` has a
            # one-live-row-per-handle index, and a retry of the same task is a
            # second, genuinely different agent run.
            s = AgentSession(project_id=self.project.id, agent_id=specialist.provider_id,
                             native_session_id=f"native-{len(self.dispatched)}",
                             backend="fake", working_directory="/tmp/p",
                             mission_id=self.mission.id)
            self.store.sessions.insert(s)
            self.dispatched.append((task.id, specialist.id))
            return s.id
        self.engine.dispatch = dispatch

        self.mission = Mission(title="m", project_id=self.project.id, goal="fix it")
        self.store.missions.insert(self.mission)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _types(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait().type)
        return out

    async def _bugfix(self):
        w = await self.engine.create(self.mission, "bug-fix", goal="fix it")
        return w

    async def _finish_all_dispatched(self):
        for task_id, _ in list(self.dispatched):
            await self.engine.on_task_finished(task_id, ok=True)

    # --- the core loop -----------------------------------------------------

    async def test_create_builds_the_graph_and_dispatches_nothing(self):
        w = await self._bugfix()
        tasks = self.store.tasks.for_workflow(w.id)
        self.assertEqual(len(tasks), 4)
        self.assertEqual(self.dispatched, [], "create() dispatched; only advance() may")
        self.assertEqual(w.status, "draft")

    async def test_advance_dispatches_only_the_dependency_free_task(self):
        w = await self._bugfix()
        await self.engine.resume(w.id)          # draft -> running
        got = await self.engine.advance(w.id)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].title.lower()[:11], "investigate"[:11])
        self.assertEqual(got[0].status, "dispatched")

    async def test_advance_is_idempotent(self):
        # Called after every state change AND on rehydrate. A double dispatch
        # would run the same work twice on a real provider.
        w = await self._bugfix()
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        await self.engine.advance(w.id)
        self.assertEqual(len(self.dispatched), 1)

    async def test_a_dependent_task_waits_until_its_dependency_completes(self):
        w = await self._bugfix()
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        first = self.dispatched[0][0]
        self.assertEqual(len(self.dispatched), 1)
        await self.engine.on_task_finished(first, ok=True)
        self.assertEqual(len(self.dispatched), 2, "the next task did not start")

    async def test_the_whole_workflow_runs_to_completion_and_completes_the_mission(self):
        w = await self._bugfix()
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        for _ in range(10):
            pending = [t for t in self.store.tasks.for_workflow(w.id)
                       if t.status in ("dispatched", "running", "verifying")]
            if not pending:
                break
            for t in pending:
                await self.engine.on_task_finished(t.id, ok=True)
        self.assertEqual(self.store.workflows.get(w.id).status, "completed")
        self.assertEqual(self.store.missions.get(self.mission.id).status, "completed")

    # --- the concurrency rule ---------------------------------------------

    async def test_never_two_writers_at_once(self):
        # MAX_WRITERS = 1. There is no worktree isolation, so two agents
        # writing one tree corrupt it. This is the most important bound here.
        w = await self.engine.create(self.mission, "single", goal="g", tasks=[
            {"id": "w1", "role": "developer", "title": "write one"},
            {"id": "w2", "role": "developer", "title": "write two"},
        ])
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        self.assertEqual(len(self.dispatched), 1)

    async def test_read_only_tasks_run_in_parallel_up_to_the_cap(self):
        tasks = [{"id": f"r{i}", "role": "researcher", "title": f"read {i}",
                  "read_only": True} for i in range(MAX_PARALLEL_READONLY + 2)]
        w = await self.engine.create(self.mission, "single", goal="g", tasks=tasks)
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        self.assertEqual(len(self.dispatched), MAX_PARALLEL_READONLY)

    async def test_a_reader_does_not_start_beside_a_running_writer(self):
        # The mirror of the test below, and the one the original rule got
        # wrong: with the writer dispatched first, a naive reader-cap check
        # lets the reader join it.
        w = await self.engine.create(self.mission, "single", goal="g", tasks=[
            {"id": "w", "role": "developer", "title": "write"},
            {"id": "r", "role": "researcher", "title": "read", "read_only": True},
        ])
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        titles = [self.store.tasks.get(t).title for t, _ in self.dispatched]
        self.assertEqual(titles, ["write"], f"a reader joined a live writer: {titles}")

    async def test_a_writer_does_not_start_beside_a_running_reader(self):
        w = await self.engine.create(self.mission, "single", goal="g", tasks=[
            {"id": "r", "role": "researcher", "title": "read", "read_only": True},
            {"id": "w", "role": "developer", "title": "write"},
        ])
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        titles = [self.store.tasks.get(t).title for t, _ in self.dispatched]
        self.assertEqual(titles, ["read"], f"a writer joined a live reader: {titles}")

    async def test_three_readers_stop_at_the_cap_and_a_third_writer_never_joins(self):
        # The two orderings above cover reader/writer mixing; these are the
        # remaining two of the four orderings the rule has to get right.
        w = await self.engine.create(self.mission, "single", goal="g", tasks=[
            {"id": "r0", "role": "researcher", "title": "r0", "read_only": True},
            {"id": "r1", "role": "researcher", "title": "r1", "read_only": True},
            {"id": "r2", "role": "researcher", "title": "r2", "read_only": True},
        ])
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        self.assertEqual(len(self.dispatched), MAX_PARALLEL_READONLY)
        # A second advance must not sneak the third reader past the cap.
        await self.engine.advance(w.id)
        self.assertEqual(len(self.dispatched), MAX_PARALLEL_READONLY)

    async def test_the_concurrency_invariant_holds_across_a_whole_mixed_run(self):
        # The four orderings above each check one advance(). This checks the
        # invariant itself — "a writer runs alone, readers cap at 2" — after
        # every state change of a mixed graph driven to completion, which is
        # where an off-by-one in the running counts would actually show up.
        mix = [True, False, True, True, False, True, False, False, True]
        w = await self.engine.create(self.mission, "single", goal="g", tasks=[
            {"id": f"t{i}", "role": "researcher" if ro else "developer",
             "title": f"t{i}", "read_only": ro} for i, ro in enumerate(mix)])
        await self.engine.resume(w.id)

        def check():
            live = [t for t in self.store.tasks.for_workflow(w.id)
                    if t.status in ("dispatched", "running", "verifying",
                                    "waiting_approval")]
            writers = [t.title for t in live if not t.read_only]
            readers = [t.title for t in live if t.read_only]
            self.assertLessEqual(len(writers), 1, f"two writers: {writers}")
            self.assertLessEqual(len(readers), MAX_PARALLEL_READONLY, readers)
            self.assertFalse(writers and readers,
                             f"a writer ran beside a reader: {writers} / {readers}")

        for _ in range(60):
            await self.engine.advance(w.id)
            check()
            live = [t for t in self.store.tasks.for_workflow(w.id)
                    if t.status in ("dispatched", "running", "verifying")]
            if not live:
                break
            # Finish one at a time, so every intermediate state is inspected.
            await self.engine.on_task_finished(live[0].id, ok=True)
            check()
        self.assertEqual(self.store.workflows.get(w.id).status, "completed")
        self.assertEqual(len(self.dispatched), len(mix))

    # --- refusals and bounds ----------------------------------------------

    async def test_a_paused_workflow_dispatches_nothing(self):
        w = await self._bugfix()
        await self.engine.resume(w.id)
        await self.engine.pause(w.id, by="ui")
        self.assertEqual(await self.engine.advance(w.id), [])
        self.assertEqual(self.dispatched, [])

    async def test_a_cancelled_workflow_dispatches_nothing_and_skips_its_tasks(self):
        w = await self._bugfix()
        await self.engine.resume(w.id)
        await self.engine.cancel(w.id, by="ui", reason="changed my mind")
        self.assertEqual(await self.engine.advance(w.id), [])
        left = [t.status for t in self.store.tasks.for_workflow(w.id)]
        self.assertTrue(all(s in ("cancelled", "skipped") for s in left), left)

    async def test_a_failed_task_retries_once_then_blocks(self):
        w = await self.engine.create(self.mission, "single", goal="g")
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        tid = self.dispatched[0][0]
        await self.engine.on_task_finished(tid, ok=False, error="boom")
        # The plan asserted "ready" here. That is wrong, and hiding a stall:
        # a `ready` task with no agent running has nothing left to wake the
        # engine, so the second attempt would never happen and `blocked` would
        # be unreachable. The retry is automatic and observable — the attempt
        # counter and the task.failed event are what record the first failure.
        self.assertEqual(self.store.tasks.get(tid).status, "dispatched")
        self.assertEqual(self.store.tasks.get(tid).attempts, 2)
        self.assertEqual(len(self.dispatched), 2, "the retry never ran")
        self.assertIn("task.failed", self._types())
        await self.engine.advance(w.id)
        await self.engine.on_task_finished(tid, ok=False, error="boom again")
        t = self.store.tasks.get(tid)
        self.assertEqual(t.status, "blocked")
        self.assertEqual(t.attempts, 2)
        self.assertEqual(self.store.workflows.get(w.id).status, "waiting_for_human",
                         "a blocked task must stop the workflow for a human, not fail it")

    async def test_a_deadlock_waits_for_a_human_and_says_which_tasks_block(self):
        # Reachable by blocking a task that others depend on. A silent stall is
        # indistinguishable from work in progress.
        w = await self.engine.create(self.mission, "single", goal="g", tasks=[
            {"id": "a", "role": "developer", "title": "a"},
            {"id": "b", "role": "tester", "title": "b", "depends_on": ["a"]},
        ])
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        tid = self.dispatched[0][0]
        seen: list[str] = []
        for _ in range(3):
            await self.engine.on_task_finished(tid, ok=False, error="no")
            await self.engine.advance(w.id)
            seen += self._types()
        self.assertEqual(self.store.workflows.get(w.id).status, "waiting_for_human")
        self.assertIn("workflow.deadlocked", seen)

    async def test_the_deadlock_event_names_the_blocking_tasks(self):
        # "waiting_for_human" with no reason is a stall the user cannot act
        # on; the payload has to say which task to look at.
        w = await self.engine.create(self.mission, "single", goal="g", tasks=[
            {"id": "a", "role": "developer", "title": "the stuck one"},
            {"id": "b", "role": "tester", "title": "b", "depends_on": ["a"]},
        ])
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        tid = self.dispatched[0][0]
        for _ in range(2):
            await self.engine.on_task_finished(tid, ok=False, error="no")
            await self.engine.advance(w.id)
        blocking = None
        while not self.q.empty():
            e = self.q.get_nowait()
            if e.type == "workflow.deadlocked":
                blocking = e.payload.get("blocking")
        self.assertEqual(blocking, ["the stuck one"])

    async def test_create_refuses_more_tasks_than_the_bound(self):
        from yuri.services.workflow import MAX_TASKS_PER_WORKFLOW
        tasks = [{"id": f"t{i}", "role": "developer", "title": "t"}
                 for i in range(MAX_TASKS_PER_WORKFLOW + 1)]
        with self.assertRaises(Exception):
            await self.engine.create(self.mission, "single", goal="g", tasks=tasks)

    async def test_a_task_can_never_create_a_task(self):
        # The bounds only mean something if nothing but create/append writes
        # to `tasks`. This asserts the engine exposes no such path.
        public = [n for n in dir(self.engine) if not n.startswith("_")]
        self.assertNotIn("add_task_from_agent", public)
        self.assertNotIn("spawn", public)

    async def test_a_task_with_no_specialist_fails_loudly_instead_of_stalling(self):
        # NoSpecialist must not leave the task `ready`: that reads as a
        # deadlock with no cause, and the resolver's message is the only thing
        # that tells the user what to create.
        for s in self.roster.list():
            if s.role == "developer":
                s.archived = True
                self.store.specialists.update(s)
        w = await self.engine.create(self.mission, "single", goal="g")
        await self.engine.resume(w.id)
        self.assertEqual(await self.engine.advance(w.id), [])
        t = self.store.tasks.for_workflow(w.id)[0]
        self.assertEqual(t.status, "failed")
        self.assertIn("developer", (t.error or ""))

    # --- assignment and retry ---------------------------------------------

    async def test_assign_pins_a_specialist_and_survives_a_re_dispatch(self):
        w = await self.engine.create(self.mission, "single", goal="g")
        task = self.store.tasks.for_workflow(w.id)[0]
        reviewer = self.roster.by_name("Reviewer")
        await self.engine.assign(task.id, reviewer.id, by="voice")
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        self.assertEqual(self.dispatched[0][1], reviewer.id)

    async def test_assign_refuses_once_the_task_has_started(self):
        w = await self.engine.create(self.mission, "single", goal="g")
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        tid = self.dispatched[0][0]
        with self.assertRaises(ValueError):
            await self.engine.assign(tid, self.roster.by_name("Reviewer").id, by="voice")

    async def test_retry_gives_a_blocked_task_one_more_attempt(self):
        w = await self.engine.create(self.mission, "single", goal="g")
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        tid = self.dispatched[0][0]
        for _ in range(2):
            await self.engine.on_task_finished(tid, ok=False, error="no")
            await self.engine.advance(w.id)
        self.assertEqual(self.store.tasks.get(tid).status, "blocked")
        t = await self.engine.retry(tid, by="ui")
        self.assertEqual(t.status, "ready")
        # The two failures that led here stay on the record; the human's
        # retry raises the ceiling instead of erasing the count.
        self.assertEqual(t.attempts, 2)
        self.assertTrue(t.can_retry)
        await self.engine.advance(w.id)
        self.assertEqual(len(self.dispatched), 3)

    async def test_skip_lets_the_workflow_move_past_a_blocked_task(self):
        w = await self.engine.create(self.mission, "single", goal="g", tasks=[
            {"id": "a", "role": "developer", "title": "a"},
            {"id": "b", "role": "tester", "title": "b", "depends_on": ["a"]},
        ])
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        a = self.dispatched[0][0]
        await self.engine.skip(a, by="ui")
        await self.engine.advance(w.id)
        self.assertEqual(len(self.dispatched), 2, "skipping did not unblock the dependent")

    async def test_dry_run_without_a_dispatch_hook_marks_nothing_dispatched(self):
        # The container injects dispatch (Task 10). Until then advance() must
        # not silently pretend it started work.
        self.engine.dispatch = None
        w = await self._bugfix()
        await self.engine.resume(w.id)
        self.assertEqual(await self.engine.advance(w.id), [])
        self.assertTrue(all(t.status in ("pending", "ready")
                            for t in self.store.tasks.for_workflow(w.id)))

    async def test_a_dry_run_is_not_reported_as_a_deadlock(self):
        # "no dispatcher is wired" is a configuration fact, not a fact about
        # the task graph. Calling it a deadlock would send a healthy workflow
        # to waiting_for_human on every start-up before Task 10 lands.
        self.engine.dispatch = None
        w = await self._bugfix()
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        self.assertEqual(self.store.workflows.get(w.id).status, "running")
        self.assertNotIn("workflow.deadlocked", self._types())

    # --- workflow lifecycle -----------------------------------------------

    async def test_one_live_workflow_per_mission(self):
        await self._bugfix()
        with self.assertRaises(Exception):
            await self._bugfix()

    async def test_create_publishes_workflow_created_naming_its_tasks(self):
        w = await self._bugfix()
        found = None
        while not self.q.empty():
            e = self.q.get_nowait()
            if e.type == "workflow.created":
                found = e
        self.assertIsNotNone(found, "nothing announced the plan")
        self.assertEqual(found.payload.get("template"), "bug-fix")
        self.assertEqual(len(found.payload.get("tasks") or []), 4)
        self.assertEqual(found.mission_id, self.mission.id)
        self.assertEqual(w.template, "bug-fix")

    async def test_a_dispatch_hook_that_always_throws_is_bounded_not_looped(self):
        # The attempt has to be spent before the provider call, or a hook that
        # throws every time leaves the task cycling failed → ready → failed
        # forever with attempts stuck at 0.
        calls = []

        async def boom(task, specialist, instruction):
            calls.append(task.id)
            raise RuntimeError("the runner would not start")
        self.engine.dispatch = boom
        w = await self.engine.create(self.mission, "single", goal="g")
        await self.engine.resume(w.id)
        for _ in range(6):
            await self.engine.advance(w.id)
        t = self.store.tasks.for_workflow(w.id)[0]
        # One attempt, then parked for a human — NOT a spin. The task rests on
        # `failed` rather than `ready`, which is the state retry() acts on.
        self.assertEqual(len(calls), 1, f"advance() spun: {len(calls)} attempts")
        self.assertEqual(t.status, "failed")
        self.assertEqual(t.attempts, 1, "the attempt was not spent")
        self.assertIn("would not start", t.error or "")
        self.assertEqual(self.store.workflows.get(w.id).status, "waiting_for_human")
        # A human decision buys exactly one more attempt, and the second
        # failure exhausts them.
        await self.engine.retry(t.id, by="ui")
        await self.engine.advance(w.id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.tasks.get(t.id).status, "blocked")

    async def test_create_refuses_an_empty_graph(self):
        # An empty workflow completes on its first advance() and takes the
        # mission `completed` with it — a mission reported done over no work.
        with self.assertRaises(ValueError):
            await self.engine.create(self.mission, "single", goal="g", tasks=[])

    async def test_a_cancelled_dependency_does_not_satisfy_its_dependent(self):
        # `completed` and `skipped` satisfy a dependency; `cancelled` must not.
        # Running `b` here would hand its agent a handoff with a hole in it
        # that nothing downstream could detect.
        w = await self.engine.create(self.mission, "single", goal="g", tasks=[
            {"id": "a", "role": "developer", "title": "a"},
            {"id": "b", "role": "tester", "title": "b", "depends_on": ["a"]},
        ])
        a = self.store.tasks.for_workflow(w.id)[0]
        a.transition("cancelled")
        self.store.tasks.update(a)
        await self.engine.resume(w.id)
        self.assertEqual(await self.engine.advance(w.id), [])
        self.assertEqual(self.store.tasks.for_workflow(w.id)[1].status, "pending")
        self.assertEqual(self.store.workflows.get(w.id).status, "waiting_for_human")

    async def test_a_terminal_mission_dispatches_nothing(self):
        # The same defect as a paused workflow, one level up: the mission is
        # cancelled and the workflow row has not caught up yet.
        w = await self._bugfix()
        await self.engine.resume(w.id)
        m = self.store.missions.get(self.mission.id)
        m.transition("cancelled")
        self.store.missions.update(m)
        self.assertEqual(await self.engine.advance(w.id), [])
        self.assertEqual(self.dispatched, [])

    async def test_a_late_success_for_a_failed_task_is_dropped(self):
        # `failed` has no legal edge to `verifying`, so a late, contradictory
        # report of success must be dropped rather than raise — and must not
        # resurrect a task the engine has already reported as failed.
        async def boom(task, specialist, instruction):
            raise RuntimeError("nope")
        self.engine.dispatch = boom
        w = await self.engine.create(self.mission, "single", goal="g")
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        t = self.store.tasks.for_workflow(w.id)[0]
        self.assertEqual(t.status, "failed")
        await self.engine.on_task_finished(t.id, ok=True)
        self.assertEqual(self.store.tasks.get(t.id).status, "failed")

    async def test_a_finish_for_a_task_that_never_started_is_dropped(self):
        w = await self._bugfix()
        pending = [t for t in self.store.tasks.for_workflow(w.id)
                   if t.status == "pending"]
        await self.engine.on_task_finished(pending[-1].id, ok=True)
        self.assertEqual(self.store.tasks.get(pending[-1].id).status, "pending")

    async def test_a_late_finish_for_a_terminal_task_is_ignored(self):
        # A provider can report a turn for work the engine already gave up on
        # or skipped. Replaying it through the transition table would raise.
        w = await self.engine.create(self.mission, "single", goal="g")
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        tid = self.dispatched[0][0]
        await self.engine.skip(tid, by="ui")
        await self.engine.on_task_finished(tid, ok=True)
        self.assertEqual(self.store.tasks.get(tid).status, "skipped")


if __name__ == "__main__":
    unittest.main()


class BoundsBehaviourTests(EngineTests):
    """The two bounds whose real behaviour was only described in comments."""

    async def test_a_workflow_older_than_the_runtime_bound_waits_for_a_human(self):
        w = await self.engine.create(self.mission, "single", goal="g")
        await self.engine.resume(w.id)
        # Age it past the bound. Rewriting created_at is the honest way to test
        # this: faking a clock would test the fake.
        #
        # RE-READ first: `w` is the draft object create() returned, and
        # resume() moved the STORED row to running. Writing the stale object
        # back would put `draft` over `running` and advance() would then refuse
        # for the wrong reason entirely.
        w = self.store.workflows.get(w.id)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=MAX_MISSION_RUNTIME_S + 60)
        w.created_at = old.isoformat().replace("+00:00", "Z")
        self.store.workflows.update(w)

        self.assertEqual(await self.engine.advance(w.id), [])
        self.assertEqual(self.store.workflows.get(w.id).status, "waiting_for_human",
                         "a workflow past MAX_MISSION_RUNTIME_S must park, not keep going")
        self.assertEqual(self.dispatched, [])

    async def test_a_fresh_workflow_is_not_parked_by_the_runtime_bound(self):
        w = await self.engine.create(self.mission, "single", goal="g")
        await self.engine.resume(w.id)
        self.assertEqual(len(await self.engine.advance(w.id)), 1)

    async def test_the_session_bound_is_deliberately_not_enforced_here(self):
        """`MAX_SESSIONS_PER_MISSION` is declared in this module but NOT applied
        by the engine, and that is the current contract, not an oversight.

        The engine cannot count what the bound counts: `dispatch` is opaque, so
        it cannot tell a new session from a reused one, and counting live
        session ROWS would park a healthy workflow on any template with more
        tasks than the bound, because nothing in this phase reaps a finished
        task's session. Task 10's hook is the only code that knows.

        This asserts the behaviour so that moving the enforcement here later
        fails loudly rather than silently changing what a long workflow does.
        """
        n = MAX_SESSIONS_PER_MISSION + 2
        tasks = [{"id": f"t{i}", "role": "developer", "title": f"step {i}",
                  "depends_on": ([f"t{i - 1}"] if i else [])} for i in range(n)]
        w = await self.engine.create(self.mission, "single", goal="g", tasks=tasks)
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        for _ in range(n * 2):
            live = [t for t in self.store.tasks.for_workflow(w.id) if t.status in IN_FLIGHT]
            if not live:
                break
            for t in live:
                await self.engine.on_task_finished(t.id, ok=True)
        self.assertEqual(self.store.workflows.get(w.id).status, "completed",
                         f"a {n}-task workflow parked; the session bound leaked into the engine")
        self.assertEqual(len(self.dispatched), n)
