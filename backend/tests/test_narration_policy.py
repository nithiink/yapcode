"""The narration ownership table is the structural guard against Yuri saying
everything twice: all four events the poll loop narrates are ALSO speakable, so
each event type must be claimed by exactly one carrier (spec section 3).

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.event import DEFAULTS, EventType  # noqa: E402
from yuri.narration import policy  # noqa: E402


def _all_event_types() -> list[str]:
    return [v for k, v in vars(EventType).items() if k.isupper()]


class OwnershipTable(unittest.TestCase):
    def test_every_event_type_is_owned_exactly_once(self):
        for t in _all_event_types():
            self.assertIn(t, policy.NARRATION_OWNER, f"{t} has no owner")
        self.assertEqual(len(policy.NARRATION_OWNER), len(_all_event_types()),
                         "the table owns a type that is not an EventType")

    def test_no_type_is_owned_by_both_carriers(self):
        # The table is a dict, so double-ownership can only show up as an owner
        # value that means "both" — assert the vocabulary is closed instead.
        for t, owner in policy.NARRATION_OWNER.items():
            self.assertIn(owner, ("poll", "stream", "stream_verbose", "none"), f"{t}: {owner}")

    def test_the_four_poll_owned_types_are_exactly_the_ones_poll_carries(self):
        poll_owned = {t for t, o in policy.NARRATION_OWNER.items() if o == "poll"}
        self.assertEqual(poll_owned, {
            EventType.APPROVAL_REQUESTED, EventType.SESSION_QUESTION,
            EventType.AGENT_ERROR, EventType.SESSION_TURN_COMPLETED})

    def test_mission_events_belong_to_the_stream(self):
        for t in (EventType.MISSION_CREATED, EventType.MISSION_STATUS_CHANGED,
                  EventType.SESSION_LOST):
            self.assertEqual(policy.owner_of(t), "stream")

    def test_debug_texture_is_verbose_only(self):
        for t in (EventType.TOOL_STARTED, EventType.COST_UPDATED):
            self.assertEqual(policy.owner_of(t), "stream_verbose")

    def test_user_caused_events_are_never_narrated(self):
        for t in (EventType.SESSION_CREATED, EventType.SESSION_MESSAGE_SENT,
                  EventType.APPROVAL_RESOLVED, EventType.SESSION_INTERRUPTED,
                  EventType.SESSION_STOPPED, EventType.PROJECT_REGISTERED,
                  EventType.MEMORY_REMEMBERED):
            self.assertEqual(policy.owner_of(t), "none")

    def test_unknown_type_owner_is_none_not_a_crash(self):
        self.assertEqual(policy.owner_of("something.invented"), "none")


class ModeFilter(unittest.TestCase):
    def _speaks(self, t, mode):
        sev = DEFAULTS.get(t, ("info", False))[0]
        return policy.speaks(t, sev, mode)

    def test_quiet_still_asks_the_user(self):
        # A mode that swallowed a permission request would strand the agent
        # waiting on an answer the user was never asked for.
        self.assertTrue(self._speaks(EventType.APPROVAL_REQUESTED, "quiet"))
        self.assertTrue(self._speaks(EventType.SESSION_QUESTION, "quiet"))

    def test_quiet_speaks_warnings_and_errors(self):
        self.assertTrue(self._speaks(EventType.AGENT_ERROR, "quiet"))
        self.assertTrue(self._speaks(EventType.SESSION_LOST, "quiet"))

    def test_quiet_suppresses_ordinary_progress(self):
        self.assertFalse(self._speaks(EventType.SESSION_TURN_COMPLETED, "quiet"))
        self.assertFalse(self._speaks(EventType.MISSION_CREATED, "quiet"))
        self.assertFalse(self._speaks(EventType.MISSION_STATUS_CHANGED, "quiet"))

    def test_normal_speaks_progress_but_not_debug_texture(self):
        self.assertTrue(self._speaks(EventType.SESSION_TURN_COMPLETED, "normal"))
        self.assertTrue(self._speaks(EventType.MISSION_CREATED, "normal"))
        self.assertFalse(self._speaks(EventType.TOOL_STARTED, "normal"))
        self.assertFalse(self._speaks(EventType.COST_UPDATED, "normal"))

    def test_verbose_adds_the_debug_texture(self):
        self.assertTrue(self._speaks(EventType.TOOL_STARTED, "verbose"))
        self.assertTrue(self._speaks(EventType.COST_UPDATED, "verbose"))
        self.assertTrue(self._speaks(EventType.SESSION_TURN_COMPLETED, "verbose"))

    def test_never_narrated_types_stay_silent_in_every_mode(self):
        for mode in policy.MODES:
            self.assertFalse(self._speaks(EventType.SESSION_CREATED, mode), mode)
            self.assertFalse(self._speaks(EventType.APPROVAL_RESOLVED, mode), mode)

    def test_normalize_mode(self):
        self.assertEqual(policy.normalize_mode("quiet"), "quiet")
        self.assertEqual(policy.normalize_mode("VERBOSE"), "verbose")
        self.assertEqual(policy.normalize_mode(" normal "), "normal")
        for bad in (None, "", "loud", 7, {}):
            self.assertEqual(policy.normalize_mode(bad), policy.DEFAULT_MODE, repr(bad))


if __name__ == "__main__":
    unittest.main()
