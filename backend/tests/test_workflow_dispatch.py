"""WorkflowDispatcher — the wires that make Phase 7 actually run (spec §8.1).

Everything here is driven end-to-end through the REAL container: the real
engine, the real SessionService, the real RosterService, and FakeAgentProvider
standing in for an agent. Nothing is stubbed between them. Tasks move because
the same events SessionService really publishes are fed back through the
driver, which is exactly what `WorkflowDispatcher.start()`'s consumer loop does
in the process — pumped by hand here so the ordering is deterministic instead
of depending on when an asyncio task happens to be scheduled.

    cd backend && .venv/bin/python -m unittest tests.test_workflow_dispatch -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.domain.artifact import Artifact  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.providers.base import ProviderEvent  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.services.workflow import MAX_SESSIONS_PER_MISSION  # noqa: E402

PERM = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"],
        "request_id": "r1"}


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
            mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri")),
        ]
        [p.start() for p in self.patches]
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)
        # The driver's own subscription is not started (no consumer task in a
        # test); this is the test's, and _pump() plays the part of the loop.
        self.q = self.c.bus.subscribe()
        self.seen: list[tuple[str, dict]] = []
        self.project = self.c.projects.resolve_or_create("proj")
        self.mission = self.c.missions.create(self.project, "Fix billing", created_by="voice",
                                              goal="the invoice totals are wrong")

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    # --- harness ----------------------------------------------------------

    async def _pump(self) -> None:
        """Drain the bus through the driver until it is quiet.

        Repeats, because handling an event publishes more of them (a task
        dispatched, a session created, a turn sent) — the same fan-out the real
        consumer loop sees, just without the scheduler in between.
        """
        while not self.q.empty():
            ev = self.q.get_nowait()
            self.seen.append((ev.type, ev.payload or {}))
            await self.c.dispatcher.on_event(ev)

    def _tasks(self, workflow_id: str) -> list:
        return self.c.store.tasks.for_workflow(workflow_id)

    def _by_title(self, workflow_id: str) -> dict:
        return {t.title: t for t in self._tasks(workflow_id)}

    def _statuses(self, workflow_id: str) -> list[tuple[str, str]]:
        return [(t.title, t.status) for t in self._tasks(workflow_id)]

    def _live(self) -> list[AgentSession]:
        return self.c.store.sessions.list(live_only=True)

    def _handle_of(self, task) -> str:
        row = self.c.store.sessions.get(task.session_id)
        self.assertIsNotNone(row, f"task '{task.title}' has no session row")
        return row.native_session_id

    def _events_for(self, task_id: str) -> list[str]:
        return [t for t, p in self.seen if p.get("task_id") == task_id]

    async def _plan(self, template: str = "bug-fix", **kw):
        w = await self.c.workflow.create(self.mission, template,
                                         self.mission.goal or "do the thing", **kw)
        await self.c.workflow.resume(w.id, by="test")
        return w

    async def _finish_turn(self, task, text: str = "done") -> None:
        """The agent speaks. This is the real path: the provider notifies its
        observer, SessionService turns that into `session.turn_completed`, and
        the driver picks it up off the bus."""
        self.fake.emit(self._handle_of(task),
                       ProviderEvent("turn_completed", {"assistant_text": text, "tools_used": []}))
        await self._pump()

    # --- the whole thing --------------------------------------------------

    async def test_a_four_task_mission_runs_to_completion(self):
        # Verification is real (spec §10): `bug-fix` declares `tests_pass` on
        # its test task and `review_approved` on its review task, and a task
        # whose checks do not PASS never reaches `completed`. So this test now
        # has to make them pass — configure the test command, and let the
        # reviewer state a verdict. Before Task 9 the same run "completed"
        # with neither, which is the lie that task removed.
        self.mission.metadata = {"verify": {"tests": "sh -c 'exit 0'"}}
        self.c.store.missions.update(self.mission)
        w = await self._plan()
        started = await self.c.workflow.advance(w.id)
        await self._pump()

        titles = ["Investigate the bug", "Fix the bug", "Run the tests", "Review the fix"]
        self.assertEqual([t.title for t in self._tasks(w.id)], titles)
        self.assertEqual([t.title for t in started], ["Investigate the bug"])
        # Dependent tasks waited: not one of them is even `ready`.
        self.assertEqual(self._statuses(w.id),
                         [(titles[0], "dispatched")] + [(t, "pending") for t in titles[1:]])

        peak = 0
        for i, title in enumerate(titles):
            task = self._by_title(w.id)[title]
            self.assertEqual(task.status, "dispatched",
                             f"'{title}' should be dispatched once its inputs are done")
            self.assertEqual(self.c.missions.get(self.mission.id).current_step, task.id,
                             "the mission should point at the task actually running")
            live = self._live()
            peak = max(peak, len(live))
            self.assertEqual(len(live), 1,
                             f"{len(live)} sessions live while '{title}' ran; sequential tasks "
                             "must never hold more than one at a time")
            # The instruction really reached an agent, through the provider.
            self.assertIn(("send_message", live[0].native_session_id),
                          [(c[0], c[1]) for c in self.fake.calls if c[0] == "send_message"])
            handle = self._handle_of(task)
            if "review_approved" in task.verification:
                self.c.store.artifacts.insert(Artifact(
                    mission_id=self.mission.id, task_id=task.id, kind="review",
                    title="review", body="Looks right.\nVERDICT: approved"))
            await self._finish_turn(task, f"finished {title}")

            done = self.c.store.tasks.get(task.id)
            self.assertEqual(done.status, "completed")
            self.assertEqual(done.result.get("assistant_text"), f"finished {title}")
            # dispatched → running → verifying → completed. `running` is never
            # persisted on the happy path (on_task_finished walks straight
            # through it), so the event trail is what pins the shape.
            self.assertEqual(self._events_for(task.id),
                             ["task.dispatched", "task.verifying", "task.completed"])
            # Reaped: a finished task's agent is stopped, so the next task's
            # session is a new one rather than a fifth idle process.
            self.assertNotIn(handle, self.fake.sessions,
                             f"'{title}' left its agent running after it finished")
            if i + 1 < len(titles):
                self.assertEqual(self.c.store.tasks.get(self._by_title(w.id)[titles[i + 1]].id).status,
                                 "dispatched", "the next task was not dispatched")

        self.assertEqual(peak, 1)
        self.assertLessEqual(peak, MAX_SESSIONS_PER_MISSION)
        self.assertEqual(self.c.workflow.get(w.id).status, "completed")
        self.assertEqual(self.c.missions.get(self.mission.id).status, "completed")
        self.assertIsNone(self.c.missions.get(self.mission.id).current_step)
        self.assertEqual(self._live(), [])
        # Four tasks, four specialists, four DISTINCT sessions over the run.
        self.assertEqual(len({t.session_id for t in self._tasks(w.id)}), 4)
        self.assertEqual(len({t.specialist_id for t in self._tasks(w.id)}), 4)
        # The mission detail view reads `steps`; it is the task graph now.
        self.assertEqual([s["title"] for s in self.c.missions.detail(self.mission.id)["steps"]],
                         titles)

    # --- the states only an approval can show -----------------------------

    async def test_approval_parks_the_task_and_resolving_releases_it(self):
        w = await self._plan()
        await self.c.workflow.advance(w.id)
        await self._pump()
        task = self._by_title(w.id)["Investigate the bug"]

        self.fake.emit(self._handle_of(task), ProviderEvent("needs_permission", PERM))
        await self._pump()
        self.assertEqual(self.c.store.tasks.get(task.id).status, "waiting_approval")
        # Nothing else started beside it: waiting_approval is in flight, and the
        # agent is parked mid-edit.
        self.assertEqual(len(self._live()), 1)

        approval = self.c.approvals.pending()[0]
        self.c.sessions.answer_approval(approval.id, "allowed", by="ui")
        await self._pump()
        self.assertEqual(self.c.store.tasks.get(task.id).status, "running",
                         "an answered approval must hand the task back to the agent")

        await self._finish_turn(task)
        self.assertEqual(self.c.store.tasks.get(task.id).status, "completed")

    # --- failures ---------------------------------------------------------

    async def test_agent_error_fails_the_task_and_the_retry_runs(self):
        w = await self._plan()
        await self.c.workflow.advance(w.id)
        await self._pump()
        task = self._by_title(w.id)["Investigate the bug"]
        first_session = task.session_id

        self.fake.emit(self._handle_of(task), ProviderEvent("error", {"message": "boom"}))
        await self._pump()
        # attempts=1 of 2, so the engine put it back and advance() re-dispatched
        # it in the same call — the retry is automatic, not a state to sit in.
        again = self.c.store.tasks.get(task.id)
        self.assertEqual((again.status, again.attempts), ("dispatched", 2))
        self.assertIn("task.failed", self._events_for(task.id))
        self.assertEqual(again.session_id, first_session,
                         "a still-live session should be reused for the retry, with its context")

        self.fake.emit(self._handle_of(again), ProviderEvent("error", {"message": "boom again"}))
        await self._pump()
        blocked = self.c.store.tasks.get(task.id)
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("boom again", blocked.error)
        # A blocked task keeps its agent: the human retrying it wants the one
        # that was in the middle of the work.
        self.assertEqual(len(self._live()), 1)
        self.assertEqual(self.c.workflow.get(w.id).status, "waiting_for_human")

    async def test_session_lost_fails_the_task_naming_the_restart(self):
        w = await self._plan()
        await self.c.workflow.advance(w.id)
        await self._pump()
        task = self._by_title(w.id)["Investigate the bug"]
        row = self.c.store.sessions.get(task.session_id)

        # Exactly what rehydrate() does for a handle whose agent did not come
        # back: mark the row `lost`, THEN publish. The order matters — the
        # retry's dispatch looks for a live session to reuse, and a row still
        # reading `idle` would hand the new attempt a dead handle.
        row.status = "lost"
        self.c.store.sessions.update(row)
        self.c.bus.publish(self.c.sessions._ev("session.lost", row, row.native_session_id, {}))
        await self._pump()
        # The reason is read off task.failed, not off the row: the task was
        # retriable, so advance() re-dispatched it in the same call and cleared
        # `error` on the way (a stale reason on a running task would be worse
        # than none). The event is the durable record.
        reasons = [p.get("reason", "") for t, p in self.seen if t == "task.failed"]
        self.assertTrue(any("restart" in r and "retry" in r for r in reasons),
                        f"no task.failed named the restart: {reasons}")
        # Retriable, so the engine re-dispatched — onto a NEW session, because
        # the lost one is no longer live.
        after = self.c.store.tasks.get(task.id)
        self.assertEqual(after.status, "dispatched")
        self.assertNotEqual(after.session_id, row.id)

    async def test_a_dispatch_that_cannot_start_fails_the_task_with_a_reason(self):
        """Never left `ready`: a ready task nobody dispatches looks exactly like
        a deadlock with no cause."""
        w = await self._plan()
        with mock.patch.object(self.c.sessions, "start",
                               side_effect=RuntimeError("the agent binary is not installed")):
            await self.c.workflow.advance(w.id)
        task = self._by_title(w.id)["Investigate the bug"]
        self.assertEqual(task.status, "failed")
        self.assertIn("the agent binary is not installed", task.error)

    # --- session economy --------------------------------------------------

    async def test_two_tasks_for_one_specialist_share_a_session(self):
        """Reuse is what keeps a long workflow under MAX_SESSIONS_PER_MISSION —
        and what lets the second task start where the first left off."""
        w = await self._plan(template="", tasks=[
            {"id": "one", "role": "developer", "title": "First half", "instruction": "do a"},
            {"id": "two", "role": "developer", "title": "Second half", "instruction": "do b",
             "depends_on": ["one"]}])
        await self.c.workflow.advance(w.id)
        await self._pump()
        one = self._by_title(w.id)["First half"]
        await self._finish_turn(one)
        two = self.c.store.tasks.get(self._by_title(w.id)["Second half"].id)
        self.assertEqual(two.status, "dispatched")
        self.assertEqual(two.session_id, one.session_id, "a second session was opened")
        self.assertEqual(len(self._live()), 1)
        # Not reaped out from under its successor.
        self.assertIn(self._handle_of(two), self.fake.sessions)

        await self._finish_turn(two)
        self.assertEqual(self.c.workflow.get(w.id).status, "completed")
        self.assertEqual(self._live(), [])

    async def test_the_session_bound_parks_the_workflow_instead_of_raising(self):
        for i in range(MAX_SESSIONS_PER_MISSION):
            self.c.store.sessions.insert(AgentSession(
                project_id=self.project.id, agent_id="fake", native_session_id=f"squatter-{i}",
                backend="fake", working_directory=self.project.root_path,
                mission_id=self.mission.id, status="running"))
        w = await self._plan()
        await self.c.workflow.advance(w.id)
        await self._pump()

        self.assertEqual(self.c.workflow.get(w.id).status, "waiting_for_human")
        whys = [p.get("why") for t, p in self.seen if t == "workflow.deadlocked"]
        self.assertIn("sessions", whys, "the bound must say it was the bound")
        task = self._by_title(w.id)["Investigate the bug"]
        self.assertEqual(task.status, "failed")
        self.assertIn(str(MAX_SESSIONS_PER_MISSION), task.error)
        self.assertEqual(len(self._live()), MAX_SESSIONS_PER_MISSION,
                         "no fifth session was started")

    # --- the mission and its workflow move together -----------------------

    async def test_pausing_the_mission_stops_the_workflow_dispatching(self):
        """`paused` is not a TERMINAL mission status, so advance()'s own guard
        does not cover it: without the container wiring this, pausing a mission
        would stop its agents and then dispatch the next task the moment an
        interrupted turn landed."""
        w = await self._plan()
        await self.c.workflow.advance(w.id)
        await self._pump()
        task = self._by_title(w.id)["Investigate the bug"]

        await self.c.missions.pause(self.mission.id, by="ui")
        self.assertEqual(self.c.workflow.get(w.id).status, "paused")

        await self._finish_turn(task)
        self.assertEqual(self.c.store.tasks.get(task.id).status, "completed",
                         "the turn that already happened should still be recorded")
        # advance() returns on its FIRST line for a workflow that is not
        # running, so the successor is not even promoted, let alone started.
        self.assertEqual(self._statuses(w.id)[1], ("Fix the bug", "pending"),
                         "a paused workflow dispatched anyway")
        self.assertEqual(self._live(), [], "the finished task's agent was still reaped")

        await self.c.missions.resume(self.mission.id, by="ui")
        self.assertEqual(self.c.workflow.get(w.id).status, "running")
        self.assertEqual(self.c.store.tasks.get(self._by_title(w.id)["Fix the bug"].id).status,
                         "dispatched", "resume() must release the work, not just unlock it")

    async def test_cancelling_the_mission_cancels_the_workflow(self):
        w = await self._plan()
        await self.c.workflow.advance(w.id)
        await self._pump()
        await self.c.missions.cancel(self.mission.id, by="ui")
        self.assertEqual(self.c.workflow.get(w.id).status, "cancelled")
        self.assertEqual({s for _, s in self._statuses(w.id)}, {"cancelled"})
        self.assertEqual(self._live(), [])

    async def test_resume_does_not_release_an_unconfirmed_plan(self):
        """A `draft` workflow is a plan the user has not confirmed (spec §14.1).
        Resuming the MISSION must not start it."""
        w = await self.c.workflow.create(self.mission, "bug-fix", "the totals are wrong")
        self.assertEqual(w.status, "draft")
        await self.c.missions.pause(self.mission.id, by="ui")
        await self.c.missions.resume(self.mission.id, by="ui")
        self.assertEqual(self.c.workflow.get(w.id).status, "draft")
        self.assertEqual(self._live(), [])

    # --- the sessions that are not tasks ----------------------------------

    async def test_events_for_a_plain_voice_session_are_ignored(self):
        """Hand-started sessions still exist and are not the engine's. An event
        for one must do nothing at all — not raise, not log, not transition."""
        w = await self._plan()
        await self.c.workflow.advance(w.id)
        await self._pump()
        before = self._statuses(w.id)

        out = await self.c.sessions.start("proj", name="hand-driven")
        await self._pump()
        self.fake.emit(out["session_id"],
                       ProviderEvent("turn_completed", {"assistant_text": "hi", "tools_used": []}))
        await self._pump()
        self.assertEqual(self._statuses(w.id), before)

    async def test_a_stale_turn_for_a_finished_task_is_dropped(self):
        w = await self._plan(template="", tasks=[
            {"id": "only", "role": "developer", "title": "The only task", "instruction": "go"}])
        await self.c.workflow.advance(w.id)
        await self._pump()
        task = self._by_title(w.id)["The only task"]
        handle = self._handle_of(task)
        await self._finish_turn(task)
        self.assertEqual(self.c.workflow.get(w.id).status, "completed")

        # The session was reaped, so re-publish the event by hand rather than
        # through a provider that no longer holds the handle.
        row = self.c.store.sessions.get(task.session_id)
        self.c.bus.publish(self.c.sessions._ev(
            "session.turn_completed", row, handle, {"assistant_text": "late", "tools_used": []}))
        await self._pump()
        self.assertEqual(self.c.store.tasks.get(task.id).result.get("assistant_text"), "done",
                         "a late turn overwrote a completed task's result")
        self.assertEqual(self.c.workflow.get(w.id).status, "completed")


class DriverLoopTests(unittest.IsolatedAsyncioTestCase):
    """The subscription itself, which the tests above bypass on purpose."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
            mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri")),
        ]
        [p.start() for p in self.patches]
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), FakeAgentProvider())

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    async def test_start_is_idempotent_and_stop_is_safe_twice(self):
        d = self.c.dispatcher
        self.assertFalse(d.running())
        await d.stop()                    # never started
        d.start()
        first = d._loop_task
        d.start()                         # a second subscriber would drive every task twice
        self.assertIs(d._loop_task, first)
        self.assertTrue(d.running())
        await d.stop()
        self.assertFalse(d.running())
        await d.stop()

    async def test_the_container_seeds_the_roster_and_wires_the_hook(self):
        self.assertEqual(sorted(s.role for s in self.c.roster.list()),
                         ["developer", "documenter", "researcher", "reviewer", "tester",
                          "verifier"])
        self.assertEqual(self.c.workflow.dispatch, self.c.dispatcher.dispatch)
        self.assertIn("bug-fix", self.c.workflow.templates)


if __name__ == "__main__":
    unittest.main()
