import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.artifact import ARTIFACT_KINDS, Artifact  # noqa: E402
from yuri.domain.task import (TASK_TRANSITIONS, TERMINAL_TASK,  # noqa: E402
                              InvalidTaskTransition, Task)
from yuri.domain.workflow import (LIVE_WORKFLOW, TERMINAL_WORKFLOW,  # noqa: E402
                                  WORKFLOW_TRANSITIONS, InvalidWorkflowTransition, Workflow)


class TaskTransitionTests(unittest.TestCase):
    def _task(self, status="pending"):
        return Task(workflow_id="w1", ordinal=0, title="t", role="developer", status=status)

    def test_the_happy_path_is_walkable(self):
        t = self._task()
        for nxt in ["ready", "dispatched", "running", "verifying", "completed"]:
            self.assertTrue(t.transition(nxt), nxt)
        self.assertIn(t.status, TERMINAL_TASK)

    def test_same_state_is_a_silent_no_op(self):
        self.assertFalse(self._task("running").transition("running"))

    def test_a_terminal_task_can_never_move_again(self):
        for status in TERMINAL_TASK:
            t = self._task(status)
            self.assertEqual(TASK_TRANSITIONS[status], frozenset(), status)
            with self.assertRaises(InvalidTaskTransition):
                t.transition("running")

    def test_pending_cannot_skip_straight_to_dispatched(self):
        # `ready` is the only gate that checks dependencies. Skipping it is how
        # a task would start before its dependency finished.
        with self.assertRaises(InvalidTaskTransition):
            self._task("pending").transition("dispatched")

    def test_dispatched_can_go_back_to_ready(self):
        # Reconciliation after a crash: dispatched with no session never
        # actually started, so it must be re-dispatchable (spec §13).
        self.assertTrue(self._task("dispatched").transition("ready"))

    def test_failed_can_retry_to_ready_or_give_up_to_blocked(self):
        self.assertIn("ready", TASK_TRANSITIONS["failed"])
        self.assertIn("blocked", TASK_TRANSITIONS["failed"])

    def test_blocked_is_not_terminal_because_a_human_can_retry_it(self):
        self.assertNotIn("blocked", TERMINAL_TASK)
        self.assertIn("ready", TASK_TRANSITIONS["blocked"])

    def test_every_reachable_status_is_a_key_in_the_table(self):
        # A status reachable by transition but absent as a key raises KeyError
        # deep inside transition() instead of failing here.
        reachable = {s for edges in TASK_TRANSITIONS.values() for s in edges}
        self.assertEqual(reachable - set(TASK_TRANSITIONS), set())

    def test_started_at_is_set_once_and_ended_at_on_stopping(self):
        t = self._task()
        t.transition("ready"); t.transition("dispatched"); t.transition("running")
        first = t.started_at
        self.assertIsNotNone(first)
        t.transition("verifying"); t.transition("completed")
        self.assertEqual(t.started_at, first, "started_at moved on a later transition")
        self.assertIsNotNone(t.ended_at)

    def test_blocked_records_an_end_time_too(self):
        # The timeline needs a duration even for work a human can restart.
        t = self._task("failed")
        t.transition("blocked")
        self.assertIsNotNone(t.ended_at)


class TaskShapeTests(unittest.TestCase):
    def test_json_fields_survive_a_round_trip_as_tuples(self):
        t = Task.from_dict({"workflow_id": "w", "ordinal": 1, "title": "t",
                            "role": "reviewer", "requires": ["coding"],
                            "verification": ["tests_pass"]})
        self.assertIsInstance(t.requires, tuple)
        self.assertIsInstance(t.verification, tuple)

    def test_attempts_default_to_one_retry(self):
        t = Task(workflow_id="w", ordinal=0, title="t", role="developer")
        self.assertEqual((t.attempts, t.max_attempts), (0, 2))
        self.assertTrue(t.can_retry)
        t.attempts = 2
        self.assertFalse(t.can_retry)

    def test_an_agent_task_needs_a_role_or_a_specialist(self):
        with self.assertRaises(ValueError):
            Task(workflow_id="w", ordinal=0, title="t", kind="agent_task")
        Task(workflow_id="w", ordinal=0, title="t", kind="agent_task", role="reviewer")
        Task(workflow_id="w", ordinal=0, title="t", kind="agent_task", specialist_id="s1")
        # A verification task runs no agent, so it needs neither.
        Task(workflow_id="w", ordinal=0, title="t", kind="verification")

    def test_unknown_kind_and_unknown_role_are_rejected(self):
        with self.assertRaises(ValueError):
            Task(workflow_id="w", ordinal=0, title="t", kind="magic")
        with self.assertRaises(ValueError):
            Task(workflow_id="w", ordinal=0, title="t", role="wizard")


class WorkflowTests(unittest.TestCase):
    def test_waiting_for_human_is_reachable_and_recoverable(self):
        # Bounds and deadlock land here, and the user must be able to resume;
        # hitting a retry limit must not kill a mission they only had to look at.
        w = Workflow(mission_id="m1", status="running")
        self.assertTrue(w.transition("waiting_for_human"))
        self.assertIn("running", WORKFLOW_TRANSITIONS["waiting_for_human"])
        self.assertNotIn("waiting_for_human", TERMINAL_WORKFLOW)

    def test_terminal_workflow_statuses_have_no_edges(self):
        for status in TERMINAL_WORKFLOW:
            self.assertEqual(WORKFLOW_TRANSITIONS[status], frozenset(), status)

    def test_illegal_edges_raise(self):
        with self.assertRaises(InvalidWorkflowTransition):
            Workflow(mission_id="m", status="draft").transition("completed")

    def test_live_and_terminal_together_cover_every_status(self):
        # A status in neither set is invisible to both the one-live index and
        # the completion check.
        self.assertEqual(set(LIVE_WORKFLOW) | TERMINAL_WORKFLOW, set(WORKFLOW_TRANSITIONS))
        self.assertEqual(set(LIVE_WORKFLOW) & TERMINAL_WORKFLOW, set())


class ArtifactTests(unittest.TestCase):
    def test_kind_is_validated(self):
        Artifact(mission_id="m", kind="summary", title="t", body="b")
        with self.assertRaises(ValueError):
            Artifact(mission_id="m", kind="vibes", title="t", body="b")
        self.assertIn("summary", ARTIFACT_KINDS)
