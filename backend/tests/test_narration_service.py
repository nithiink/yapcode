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

import yuri.domain.event as event_mod  # noqa: E402
from yuri.domain.event import DEFAULTS, EventType, YuriEvent  # noqa: E402
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

    def test_a_voice_commanded_mission_is_never_narrated(self):
        # The user asked for it and start_session's own result is spoken in the
        # same breath — a second line is telling them what they just did.
        self.assertIsNone(self._line(EventType.MISSION_CREATED,
                                     {"title": "billing", "project": "proj",
                                      "created_by": "voice"}))

    def test_an_adopted_mission_is_picked_up_not_started(self):
        # adopt() creates the mission for a tmux session that was ALREADY
        # running. "Starting" asserts something that did not happen.
        line = self._line(EventType.MISSION_CREATED,
                          {"title": "billing", "project": "proj", "created_by": "handoff"})
        self.assertIn("Picking up", line)
        self.assertNotIn("Starting", line)
        self.assertIn("billing", line)

    def test_a_ui_created_mission_still_starts(self):
        for by in ("ui", "api", "system", None):
            line = self._line(EventType.MISSION_CREATED,
                              {"title": "billing", "created_by": by})
            self.assertIn("Starting", line or "", repr(by))

    def test_a_voice_commanded_status_change_is_never_narrated(self):
        for to in ("paused", "cancelled", "completed", "failed"):
            self.assertIsNone(self._line(EventType.MISSION_STATUS_CHANGED,
                                         {"title": "payments", "from": "running",
                                          "to": to, "by": "voice"}), to)

    def test_a_derived_system_status_change_is_never_narrated(self):
        # `derived` is _mission_to's marker: this restates a session-level event
        # poll owns and already spoke — failed <- agent.error (same reason
        # string), paused <- the session the user closed.
        self.assertIsNone(self._line(EventType.MISSION_STATUS_CHANGED,
                                     {"title": "billing", "from": "running", "to": "failed",
                                      "by": "system", "reason": "tmux pane died",
                                      "derived": True}))
        self.assertIsNone(self._line(EventType.MISSION_STATUS_CHANGED,
                                     {"title": "billing", "from": "running", "to": "paused",
                                      "by": "system", "reason": "session closed",
                                      "derived": True}))

    def test_an_unmarked_system_failure_is_spoken(self):
        # start()'s provider-failure path: no session row, so no poll can ever
        # report it, and the agent.error beside it is poll-owned (silent on the
        # stream). Suppressing this would leave a failed start unnarrated.
        line = self._line(EventType.MISSION_STATUS_CHANGED,
                          {"title": "billing", "from": "running", "to": "failed",
                           "by": "system", "reason": "Claude Code unavailable: boom"})
        self.assertIn("billing", line)
        self.assertIn("failed", line)
        self.assertIn("Claude Code unavailable: boom", line)

    def test_a_ui_status_change_is_still_narrated(self):
        line = self._line(EventType.MISSION_STATUS_CHANGED,
                          {"title": "payments", "from": "running", "to": "paused", "by": "ui"})
        self.assertIn("payments", line)
        self.assertIn("paused", line)

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

    def test_a_very_long_mission_title_and_project_stay_bounded(self):
        # Neither field is bounded upstream: MissionService.create caps `goal`
        # but never `title`, and titles come straight from the session name
        # via SessionService._pick_name, which only whitespace-normalizes.
        line = self._line(EventType.MISSION_CREATED,
                          {"title": "T" * 5000, "project": "P" * 5000})
        self.assertLess(len(line), 200, line)

    def test_a_very_long_mission_title_stays_bounded_in_every_status_branch(self):
        for to in ("completed", "failed", "paused", "cancelled"):
            line = self._line(EventType.MISSION_STATUS_CHANGED,
                              {"title": "T" * 5000, "to": to})
            self.assertLess(len(line), 300, (to, line))

    def test_a_very_long_session_name_stays_bounded_on_session_lost(self):
        line = self._line(EventType.SESSION_LOST, {"session_name": "S" * 5000})
        self.assertLess(len(line), 200, line)


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

    def test_error_severity_gating_reads_through_to_domain_defaults(self):
        # Guards against poll-side gating silently desyncing from stream-side
        # gating: if line_for_poll ever reverts to a hardcoded "error"
        # literal, patching DEFAULTS has no effect and this test catches it.
        original = DEFAULTS[EventType.AGENT_ERROR]
        event_mod.DEFAULTS[EventType.AGENT_ERROR] = ("info", original[1])
        try:
            self.assertIsNone(self._poll({"status": "error", "error": "boom"}, mode="quiet"))
        finally:
            event_mod.DEFAULTS[EventType.AGENT_ERROR] = original

    def test_completed_severity_gating_reads_through_to_domain_defaults(self):
        # Same guard for session.turn_completed: bumping its DEFAULTS severity
        # to something quiet-mode treats as loud must change poll's gating.
        original = DEFAULTS[EventType.SESSION_TURN_COMPLETED]
        event_mod.DEFAULTS[EventType.SESSION_TURN_COMPLETED] = ("warning", original[1])
        try:
            self.assertIsNotNone(self._poll({"status": "completed", "assistant_text": "x"},
                                            mode="quiet"))
        finally:
            event_mod.DEFAULTS[EventType.SESSION_TURN_COMPLETED] = original


if __name__ == "__main__":
    unittest.main()
