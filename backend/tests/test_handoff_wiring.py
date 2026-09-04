"""The handoff is actually SENT, and results are actually ingested.

test_handoff.py tests the module. This tests that the engine reaches it —
which is the failure this project has to guard against specifically: a
verify.py that existed while the engine still completed unconditionally was
exactly this shape of defect, caught late.

    .venv/bin/python -m unittest tests.test_handoff_wiring -v
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
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services import handoff  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.roster import RosterService  # noqa: E402
from yuri.services.workflow import WorkflowEngine  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402
from yuri.workflows.loader import load_templates  # noqa: E402


class Wiring(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

        self.bus = EventBus()
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
        self.mission = Mission(title="m", project_id=self.project.id,
                               goal="stop the cycle detector hanging")
        self.store.missions.insert(self.mission)

        # Records the BRIEF, which the other engine harness deliberately
        # discards — it is the whole subject here.
        self.sent: dict[str, str] = {}

        async def dispatch(task, specialist, instruction):
            s = AgentSession(project_id=self.project.id, agent_id=specialist.provider_id,
                             native_session_id=f"native-{len(self.sent)}",
                             backend="fake", working_directory=self.root,
                             mission_id=self.mission.id)
            self.store.sessions.insert(s)
            self.sent[task.id] = instruction
            return s.id
        self.engine.dispatch = dispatch

        self.w = await self.engine.create(self.mission, "bug-fix", goal="fix the hang")
        await self.engine.resume(self.w.id)

    def _task(self, fragment: str):
        for t in self.store.tasks.for_workflow(self.w.id):
            if fragment.lower() in t.title.lower():
                return t
        raise AssertionError(f"no task matching {fragment!r}")

    def _brief(self, fragment: str) -> str:
        return self.sent[self._task(fragment).id]

    async def _run(self, fragment: str, text: str):
        """Finish a task the way a real turn does."""
        t = self._task(fragment)
        await self.engine.on_task_finished(t.id, ok=True, result={"assistant_text": text})

    # --- what the agent is actually sent -----------------------------------

    async def test_the_first_brief_carries_the_mission_and_the_instruction(self):
        await self.engine.advance(self.w.id)
        brief = self._brief("investigate")
        self.assertIn("THE MISSION:", brief)
        self.assertIn("stop the cycle detector hanging", brief)
        self.assertIn("Find the root cause", brief)

    async def test_a_later_brief_carries_what_the_earlier_task_reported(self):
        # The end-to-end path: turn text -> artifact -> the next agent's brief.
        await self.engine.advance(self.w.id)
        await self._run("investigate", "the visited set in cycle.py is never cleared")
        brief = self._brief("fix the bug")
        self.assertIn("Previous findings", brief)
        self.assertIn("visited set", brief)
        # Attributed, not asserted.
        self.assertIn("reported", brief)
        self.assertIn("Investigate", brief)

    async def test_a_brief_never_carries_a_task_it_did_not_depend_on(self):
        # §7.10. `test` depends on `implement`, not on `investigate`, so the
        # investigator's finding must not be in the tester's brief.
        await self.engine.advance(self.w.id)
        await self._run("investigate", "SECRET-FROM-INVESTIGATE")
        await self._run("fix the bug", "changed the loop")
        brief = self._brief("run the tests")
        self.assertIn("changed the loop", brief)
        self.assertNotIn("SECRET-FROM-INVESTIGATE", brief)

    async def test_a_retrys_brief_says_why_the_last_attempt_did_not_stand(self):
        await self.engine.advance(self.w.id)
        t = self._task("investigate")
        # on_task_finished advances itself, so the retry is dispatched inside
        # this call — `self.sent[t.id]` is already the SECOND brief.
        await self.engine.on_task_finished(t.id, ok=False, error="the agent ran out of context")
        self.assertEqual(self.store.tasks.get(t.id).attempts, 2, "it was not retried")
        brief = self.sent[t.id]
        self.assertIn("WHY THE LAST ATTEMPT DID NOT STAND", brief)
        self.assertIn("ran out of context", brief)

    async def test_a_brief_is_never_bigger_than_the_budget(self):
        await self.engine.advance(self.w.id)
        await self._run("investigate", "x" * 50_000)
        self.assertLessEqual(len(self._brief("fix the bug")), handoff.HANDOFF_MAX)

    # --- what is recorded on the way back ----------------------------------

    async def test_a_finished_turn_leaves_an_artifact_behind(self):
        await self.engine.advance(self.w.id)
        await self._run("investigate", "the cause is in cycle.py")
        arts = self.store.artifacts.for_task(self._task("investigate").id)
        self.assertEqual([a.kind for a in arts], ["summary"])
        self.assertIn("cycle.py", arts[0].body)

    async def test_a_task_that_failed_verification_still_left_its_findings(self):
        # An attempt whose findings were discarded because the tests were red
        # is an attempt the workflow learns nothing from.
        await self.engine.advance(self.w.id)
        await self._run("investigate", "found it")
        await self._run("fix the bug", "fixed it")
        t = self._task("run the tests")
        await self.engine.on_task_finished(t.id, ok=True, result={"assistant_text": "3 failed"})
        self.assertNotEqual(self.store.tasks.get(t.id).status, "completed")
        arts = self.store.artifacts.for_task(t.id)
        self.assertTrue(arts, "a task that failed its checks filed nothing")

    async def test_ingesting_cannot_stop_the_workflow(self):
        # advance() runs from a bus subscriber: an exception here would stop
        # every OTHER task from being scheduled.
        await self.engine.advance(self.w.id)
        original = handoff.ingest_result

        def boom(*a, **k):
            raise RuntimeError("the store fell over")
        handoff.ingest_result = boom
        try:
            await self._run("investigate", "found it")
        finally:
            handoff.ingest_result = original
        self.assertEqual(self.store.tasks.get(self._task("investigate").id).status, "completed")
        self.assertIn(self._task("fix the bug").id, self.sent, "the next task was not dispatched")

    async def test_a_failing_handoff_still_sends_the_instruction(self):
        # Briefed on less is recoverable; never started is a dead workflow.
        original = handoff.build_handoff

        def boom(*a, **k):
            raise RuntimeError("nope")
        handoff.build_handoff = boom
        try:
            await self.engine.advance(self.w.id)
        finally:
            handoff.build_handoff = original
        self.assertIn("Find the root cause", self._brief("investigate"))

    # --- timing, which nothing recorded before -----------------------------

    async def test_a_dispatched_task_records_when_it_started(self):
        await self.engine.advance(self.w.id)
        self.assertTrue(self.store.tasks.get(self._task("investigate").id).started_at)

    async def test_a_finished_task_records_when_it_ended(self):
        await self.engine.advance(self.w.id)
        await self._run("investigate", "done")
        t = self.store.tasks.get(self._task("investigate").id)
        self.assertEqual(t.status, "completed")
        self.assertTrue(t.ended_at)
        self.assertGreaterEqual(t.ended_at, t.started_at)

    async def test_a_retry_clears_the_end_time_the_failed_attempt_left(self):
        # Otherwise the timeline shows a task that ended before it started.
        await self.engine.advance(self.w.id)
        t = self._task("investigate")
        first_start = self.store.tasks.get(t.id).started_at
        await self.engine.on_task_finished(t.id, ok=False, error="boom")
        after = self.store.tasks.get(t.id)
        self.assertEqual(after.status, "dispatched")
        self.assertIsNone(after.ended_at, "the retry kept the failed attempt's end time")
        self.assertGreaterEqual(after.started_at, first_start)

    async def test_a_task_that_ran_out_of_attempts_records_when_it_ended(self):
        # The failure path's stamp. max_attempts is 2, so the second failure
        # is terminal.
        await self.engine.advance(self.w.id)
        t = self._task("investigate")
        await self.engine.on_task_finished(t.id, ok=False, error="boom")
        await self.engine.on_task_finished(t.id, ok=False, error="boom again")
        done = self.store.tasks.get(t.id)
        self.assertIn(done.status, ("failed", "blocked"))
        self.assertTrue(done.ended_at)


class HandoffEventTests(Wiring):
    """`handoff.passed` (spec §11) — the only audible sign that one agent's
    work reached the next."""

    def _handoffs(self) -> list:
        return [e for e in self._drain() if e.type == "handoff.passed"]

    def _drain(self) -> list:
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.q = self.bus.subscribe()

    async def test_no_event_when_the_handoff_carried_nothing(self):
        # An event announcing a handoff that carried nothing would be telling
        # the user about the absence of news.
        await self.engine.advance(self.w.id)
        self.assertEqual(self._handoffs(), [])

    async def test_an_event_when_findings_actually_moved(self):
        await self.engine.advance(self.w.id)
        self._drain()
        await self._run("investigate", "the visited set is never cleared")
        [ev] = self._handoffs()
        self.assertEqual(ev.payload["from_title"], "Investigate the bug")
        self.assertEqual(ev.payload["to_title"], "Fix the bug")
        self.assertEqual(ev.payload["findings"], 1)
        self.assertTrue(ev.payload["to_specialist"])

    async def test_it_says_a_sentence_naming_both_ends(self):
        from yuri.narration.service import NarrationService
        await self.engine.advance(self.w.id)
        self._drain()
        await self._run("investigate", "found it")
        [ev] = self._handoffs()
        line = NarrationService().line_for(ev, "verbose")
        self.assertIn("Investigate the bug", line)
        self.assertIn("Passing", line)

    async def test_it_is_silent_unless_the_user_asked_for_everything(self):
        # A stream_verbose owner speaks in `verbose` and nowhere else:
        # texture, not news.
        from yuri.narration.service import NarrationService
        await self.engine.advance(self.w.id)
        self._drain()
        await self._run("investigate", "found it")
        [ev] = self._handoffs()
        for mode in ("normal", "quiet"):
            self.assertIsNone(NarrationService().line_for(ev, mode), mode)


class VerdictsAreKeptTests(Wiring):
    """A verdict that lives only in an event is a verdict the timeline cannot
    show after a reload."""

    async def test_a_completed_task_keeps_the_verdicts_that_passed_it(self):
        await self.engine.advance(self.w.id)
        await self._run("investigate", "found it")     # no checks declared
        t = self.store.tasks.get(self._task("investigate").id)
        self.assertEqual(t.result.get("verification"), [])

    async def test_a_task_that_failed_verification_keeps_why(self):
        await self.engine.advance(self.w.id)
        await self._run("investigate", "found it")
        await self._run("fix the bug", "fixed it")
        t = self._task("run the tests")
        await self.engine.on_task_finished(t.id, ok=True, result={"assistant_text": "done"})
        kept = self.store.tasks.get(t.id).result.get("verification")
        self.assertTrue(kept, "the verdicts were not kept on the task")
        # No test command is configured, so tests_pass could not RUN — which
        # is not a pass, and must not read like one.
        self.assertEqual(kept[0]["check"], "tests_pass")
        self.assertEqual(kept[0]["verdict"], "unavailable")

    async def test_recording_the_verdicts_does_not_lose_the_agents_text(self):
        await self.engine.advance(self.w.id)
        await self._run("investigate", "the cause is here")
        t = self.store.tasks.get(self._task("investigate").id)
        self.assertEqual(t.result.get("assistant_text"), "the cause is here")
