"""What one specialist is told about another's work (spec §9, §7.10).

The rule the whole module exists for: an agent must NOT automatically receive
every other agent's history. It receives artifacts from the dependencies it
actually waited on, and nothing else.

    .venv/bin/python -m unittest tests.test_handoff -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.artifact import Artifact  # noqa: E402
from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.domain.mission import Mission  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.task import Task  # noqa: E402
from yuri.domain.workflow import Workflow  # noqa: E402
from yuri.services.handoff import (BODY_MAX, HANDOFF_MAX, Handoff,  # noqa: E402
                                   build_handoff, files_from_events,
                                   ingest_result)
from yuri.store.sqlite import SqliteStore  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(os.path.join(self.tmp.name, "y.db"))
        self.store.migrate()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

        self.project = Project(slug="p", name="P", root_path=self.tmp.name)
        self.store.projects.insert(self.project)
        self.mission = Mission(project_id=self.project.id, title="the bug",
                               goal="stop the cycle detector hanging")
        self.store.missions.insert(self.mission)
        self.w = Workflow(mission_id=self.mission.id, status="running")
        self.store.workflows.insert(self.w)
        self.a = self._task(0, "investigate")
        self.b = self._task(1, "fix it")
        self.c = self._task(2, "something unrelated")

    def _task(self, ordinal: int, title: str, **over) -> Task:
        t = Task(workflow_id=self.w.id, ordinal=ordinal, title=title,
                 role="developer", instruction=f"please {title}", **over)
        self.store.tasks.insert(t)
        return t

    def _artifact(self, task: Task, kind: str, title: str, body: str) -> Artifact:
        a = Artifact(mission_id=self.mission.id, task_id=task.id, kind=kind,
                     title=title, body=body)
        self.store.artifacts.insert(a)
        return a


class HandoffTests(Base):
    def test_only_artifacts_from_satisfied_dependencies_are_carried(self):
        # §7.10: an unrelated task's findings must not leak in.
        self._artifact(self.a, "finding", "the cause", "the cycle came from A")
        self._artifact(self.c, "finding", "unrelated", "a note from C")
        h = build_handoff(self.store, self.b, deps={self.a.id})
        bodies = " ".join(x.body for x in h.previous)
        self.assertIn("from A", bodies)
        self.assertNotIn("from C", bodies)

    def test_render_is_capped_and_drops_oldest_first(self):
        # An overflowing handoff makes the task fail for a reason the user
        # cannot see, so truncation is explicit and keeps the newest.
        for i in range(50):
            self._artifact(self.a, "finding", f"f{i}", "x" * 500)
        text = build_handoff(self.store, self.b, deps={self.a.id}).render()
        self.assertLessEqual(len(text), HANDOFF_MAX)
        self.assertIn("f49", text)
        self.assertIn("earlier findings omitted", text)

    def test_render_always_states_the_mission_goal(self):
        h = build_handoff(self.store, self.b, deps=set())
        self.assertIn(self.mission.goal, h.render())

    def test_render_falls_back_to_the_mission_title_when_there_is_no_goal(self):
        # `goal` is optional on Mission. A handoff with no statement of what
        # the work is for is the one thing this must never render.
        m = Mission(project_id=self.project.id, title="tidy the imports", goal=None)
        self.store.missions.insert(m)
        w = Workflow(mission_id=m.id, status="running")
        self.store.workflows.insert(w)
        t = Task(workflow_id=w.id, ordinal=0, title="t", role="developer")
        self.store.tasks.insert(t)
        self.assertIn("tidy the imports", build_handoff(self.store, t, deps=set()).render())

    def test_an_empty_handoff_renders_the_goal_and_nothing_misleading(self):
        text = build_handoff(self.store, self.a, deps=set()).render()
        self.assertNotIn("Previous findings", text)

    def test_the_tasks_own_instruction_is_always_in_what_it_is_sent(self):
        # The engine sends render() INSTEAD of the raw instruction, so an
        # instruction that fell out of it would silently send the agent an
        # empty brief.
        text = build_handoff(self.store, self.b, deps=set()).render()
        self.assertIn("please fix it", text)

    def test_an_artifact_is_attributed_not_asserted(self):
        # Another agent's claim is something SAID. Same rule as everywhere
        # else in her prompt.
        self._artifact(self.a, "finding", "the cause", "it is the visited set")
        text = build_handoff(self.store, self.b, deps={self.a.id}).render()
        self.assertIn("investigate", text)      # who said it
        self.assertIn("reported", text)

    def test_a_single_enormous_artifact_is_clipped_not_dropped(self):
        # Dropping it would leave the agent with no context at all; clipping
        # says so.
        self._artifact(self.a, "finding", "huge", "y" * (HANDOFF_MAX * 3))
        text = build_handoff(self.store, self.b, deps={self.a.id}).render()
        self.assertLessEqual(len(text), HANDOFF_MAX)
        self.assertIn("huge", text)
        self.assertIn("clipped", text)

    def test_files_touched_come_from_the_dependencies_file_lists(self):
        self._artifact(self.a, "file_list", "files", "cycle.py\ngraph.py")
        h = build_handoff(self.store, self.b, deps={self.a.id})
        self.assertEqual(h.files_touched, ("cycle.py", "graph.py"))
        self.assertIn("cycle.py", h.render())

    def test_retry_notes_reach_the_next_attempt(self):
        h = Handoff(mission_goal="g", previous=(), summary="", files_touched=(),
                    notes="the reviewer asked for error handling on the empty case")
        self.assertIn("error handling", h.render())

    def test_a_retrys_handoff_carries_why_the_last_attempt_failed(self):
        # Without this a retry is the same instruction sent twice, and the
        # second attempt fails the same way.
        self.b.attempts = 1
        self.b.error = "tests_pass failed: 3 tests failed in test_cycle.py"
        self.store.tasks.update(self.b)
        text = build_handoff(self.store, self.b, deps=set()).render()
        self.assertIn("test_cycle.py", text)

    def test_a_first_attempt_carries_no_notes(self):
        self.b.error = "a stale error from somewhere else"
        self.store.tasks.update(self.b)
        self.assertEqual(build_handoff(self.store, self.b, deps=set()).notes, "")


class IngestTests(Base):
    def test_ingest_always_produces_a_summary_artifact(self):
        # The implicit one is the honest default: a handoff must never come
        # back empty just because a specialist did not know about a tool.
        out = ingest_result(self.store, self.a, "I found the bug in cycle.py", ())
        self.assertEqual([x.kind for x in out], ["summary"])
        self.assertIn("cycle.py", out[0].body)

    def test_ingest_records_files_touched_as_a_file_list(self):
        out = ingest_result(self.store, self.a, "done", ("a.py", "b.py"))
        self.assertEqual({x.kind for x in out}, {"summary", "file_list"})

    def test_an_agent_that_said_nothing_is_recorded_as_having_said_nothing(self):
        # Not an empty body: the next task would read that as "no findings"
        # rather than "the agent produced no text", which are different.
        out = ingest_result(self.store, self.a, "   ", ())
        self.assertIn("without producing any text", out[0].body)

    def test_what_is_ingested_is_what_the_next_task_reads(self):
        # The two halves have to meet, or artifacts pile up unread.
        ingest_result(self.store, self.a, "the visited set is never cleared", ("cycle.py",))
        text = build_handoff(self.store, self.b, deps={self.a.id}).render()
        self.assertIn("visited set", text)
        self.assertIn("cycle.py", text)

    def test_an_enormous_result_is_clipped_on_the_way_in(self):
        out = ingest_result(self.store, self.a, "z" * 100_000, ())
        self.assertLessEqual(len(out[0].body), BODY_MAX)

    def test_ingesting_twice_does_not_duplicate_the_summary(self):
        # A retry re-ingests. Two summaries of the same task would make the
        # next handoff say everything twice.
        ingest_result(self.store, self.a, "first attempt", ())
        ingest_result(self.store, self.a, "second attempt", ())
        kinds = [x.kind for x in self.store.artifacts.for_task(self.a.id)]
        self.assertEqual(kinds.count("summary"), 1)
        [summary] = [x for x in self.store.artifacts.for_task(self.a.id) if x.kind == "summary"]
        self.assertIn("second attempt", summary.body)


class FilesFromEventsTests(Base):
    def _tool_event(self, session_id: str, tool: str, inp: dict, ts: str | None = None):
        ev = YuriEvent.make(EventType.TOOL_STARTED, mission_id=self.mission.id,
                            session_id=session_id,
                            payload={"tool_name": tool, "tool_input": inp})
        if ts:
            ev.ts = ts
        self.store.events.insert(ev)
        return ev

    def test_files_are_read_off_the_write_tools(self):
        self.a.session_id = "sess-1"
        self._tool_event("sess-1", "Edit", {"file_path": "/p/cycle.py"})
        self._tool_event("sess-1", "Write", {"file_path": "/p/new.py"})
        self._tool_event("sess-1", "Read", {"file_path": "/p/untouched.py"})
        self._tool_event("sess-1", "Bash", {"command": "ls"})
        files = files_from_events(self.store, self.a)
        self.assertEqual(files, ("/p/cycle.py", "/p/new.py"))

    def test_a_task_with_no_session_reports_no_files(self):
        self.assertEqual(files_from_events(self.store, self.a), ())

    def test_another_tasks_edits_in_a_reused_session_are_not_claimed(self):
        # A specialist's second task reuses the session, so scoping by
        # session alone would attribute the first task's files to the second.
        self.a.session_id = self.b.session_id = "sess-1"
        self._tool_event("sess-1", "Edit", {"file_path": "/p/early.py"},
                         ts="2020-01-01T00:00:00+00:00")
        self.b.started_at = "2020-06-01T00:00:00+00:00"
        self._tool_event("sess-1", "Edit", {"file_path": "/p/late.py"},
                         ts="2020-07-01T00:00:00+00:00")
        self.assertEqual(files_from_events(self.store, self.b), ("/p/late.py",))

    def test_the_same_file_edited_twice_is_listed_once(self):
        self.a.session_id = "sess-1"
        self._tool_event("sess-1", "Edit", {"file_path": "/p/cycle.py"})
        self._tool_event("sess-1", "Edit", {"file_path": "/p/cycle.py"})
        self.assertEqual(files_from_events(self.store, self.a), ("/p/cycle.py",))

    def test_a_malformed_tool_input_is_skipped_not_fatal(self):
        self.a.session_id = "sess-1"
        self._tool_event("sess-1", "Edit", {"file_path": None})
        self._tool_event("sess-1", "Edit", {})
        self._tool_event("sess-1", "Edit", {"file_path": "/p/real.py"})
        self.assertEqual(files_from_events(self.store, self.a), ("/p/real.py",))
