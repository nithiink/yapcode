"""Yuri's spoken lines. Generated from event payload fields, never free text, so
the honesty rules (spec section 5.2) are structural: a turn-completion line says
the agent finished and quotes what it SAID — never that the work succeeded.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.narration.service import NarrationService  # noqa: E402


class Lines(unittest.TestCase):
    def setUp(self):
        self.n = NarrationService()

    def _line(self, type, payload, mode="normal"):
        return self.n.line_for(YuriEvent.make(type, payload=payload), mode)

    def test_mission_created(self):
        line = self._line(EventType.MISSION_CREATED,
                          {"title": "Fix billing", "project": "pm-tool"})
        self.assertIn("Fix billing", line)
        self.assertIn("pm-tool", line)

    def test_mission_completed_and_failed(self):
        done = self._line(EventType.MISSION_STATUS_CHANGED,
                          {"title": "Fix billing", "from": "running", "to": "completed"})
        self.assertIn("Fix billing", done)
        failed = self._line(EventType.MISSION_STATUS_CHANGED,
                            {"title": "Fix billing", "from": "running", "to": "failed",
                             "reason": "tests did not pass"})
        self.assertIn("failed", failed)
        self.assertIn("tests did not pass", failed)

    def test_waiting_for_approval_is_silent_the_approval_speaks(self):
        self.assertIsNone(self._line(EventType.MISSION_STATUS_CHANGED,
                                     {"title": "t", "from": "running",
                                      "to": "waiting_for_approval"}))

    def test_session_lost_is_honest_about_what_happened(self):
        line = self._line(EventType.SESSION_LOST, {"session_name": "billing"})
        self.assertIn("billing", line)
        self.assertRegex(line.lower(), r"lost|didn't survive|did not survive")

    def test_verbose_texture(self):
        tool = self._line(EventType.TOOL_STARTED,
                          {"tool_name": "Read", "agent_name": "Claude Code"}, mode="verbose")
        self.assertIn("Read", tool)
        self.assertIsNone(self._line(EventType.TOOL_STARTED,
                                     {"tool_name": "Read"}, mode="normal"))
        cost = self._line(EventType.COST_UPDATED,
                          {"cost_usd": 0.1234, "session_name": "billing"}, mode="verbose")
        self.assertIn("0.12", cost)

    def test_never_narrated_types_return_none(self):
        self.assertIsNone(self._line(EventType.SESSION_CREATED, {"name": "x"}))
        self.assertIsNone(self._line(EventType.APPROVAL_RESOLVED, {"status": "allowed"}))

    def test_poll_owned_types_are_not_narrated_from_the_stream(self):
        # Otherwise the user hears the turn twice: once from poll, once here.
        for t in (EventType.SESSION_TURN_COMPLETED, EventType.APPROVAL_REQUESTED,
                  EventType.SESSION_QUESTION, EventType.AGENT_ERROR):
            self.assertIsNone(self._line(t, {"assistant_text": "x", "description": "y",
                                             "text": "z", "message": "m"}), t)


class PollLines(unittest.TestCase):
    def setUp(self):
        self.n = NarrationService()

    def _poll(self, result, mode="normal", name="billing", agent="Claude Code"):
        return self.n.line_for_poll(result, name, agent, mode)

    def test_permission_asks_and_names_the_action(self):
        line = self._poll({"status": "needs_permission",
                           "prompt": {"kind": "permission", "text": "run rm -rf build",
                                      "tool_name": "Bash", "options": ["allow", "deny"]}})
        self.assertIn("rm -rf build", line)
        self.assertRegex(line.lower(), r"approve|deny|permission")

    def test_dangerous_risk_is_surfaced_before_asking(self):
        line = self._poll({"status": "needs_permission", "risk": "dangerous",
                           "prompt": {"kind": "permission", "text": "run rm -rf /",
                                      "tool_name": "Bash", "options": ["allow", "deny"]}})
        self.assertRegex(line.lower(), r"destructive|dangerous")

    def test_question_reads_numbered_options(self):
        line = self._poll({"status": "needs_choice",
                           "prompt": {"kind": "choice", "text": "Which one?",
                                      "options": ["Train-Us", "Train"]}})
        self.assertIn("Which one?", line)
        self.assertIn("(1)", line)
        self.assertIn("(2)", line)

    def test_completed_quotes_the_agent_and_never_claims_success(self):
        line = self._poll({"status": "completed",
                           "assistant_text": "I changed two files and ran the tests."})
        self.assertIn("changed two files", line)
        # The honesty rule: the line reports that the agent finished and what it
        # said. It must not assert the work itself succeeded.
        for forbidden in ("it's fixed", "it is fixed", "the work is done",
                          "successfully completed", "everything works"):
            self.assertNotIn(forbidden, line.lower(), forbidden)

    def test_completed_attributes_the_request(self):
        line = self._poll({"status": "completed", "assistant_text": "done",
                           "request": "list the files in backend"})
        self.assertIn("list the files in backend", line)

    def test_error_is_reported_plainly(self):
        line = self._poll({"status": "error", "error": "claude exited 1"})
        self.assertIn("claude exited 1", line)

    def test_quiet_suppresses_completion_but_never_a_prompt(self):
        self.assertIsNone(self._poll({"status": "completed", "assistant_text": "x"},
                                     mode="quiet"))
        self.assertIsNotNone(self._poll({"status": "needs_permission",
                                         "prompt": {"kind": "permission", "text": "run ls",
                                                    "tool_name": "Bash", "options": []}},
                                        mode="quiet"))
        self.assertIsNotNone(self._poll({"status": "error", "error": "boom"}, mode="quiet"))

    def test_working_and_idle_are_not_narrated(self):
        self.assertIsNone(self._poll({"status": "working"}))
        self.assertIsNone(self._poll({"status": "idle"}))

    def test_a_prompt_status_without_a_prompt_payload_is_not_narrated(self):
        # Defensive: poll can report needs_permission with no prompt attached.
        self.assertIsNone(self._poll({"status": "needs_permission"}))

    def test_long_assistant_text_is_capped(self):
        line = self._poll({"status": "completed", "assistant_text": "x" * 5000})
        self.assertLess(len(line), 1200)


if __name__ == "__main__":
    unittest.main()
