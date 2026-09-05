"""Guidance that used to live in the voice prompt now lives on the tools.

`frontend/lib/operating.ts` was 1,554 words, of which 1,517 were "when the
user says X, call tool Y" about a named tool. That competed with Yuri's
identity in the system prompt permanently, which is why she talked about work
and nothing else. It moved onto each tool's `description`, where the model
reads it at the moment it is deciding to call that tool.

Moving prose between two places a model reads has no natural test — the
regression is "she got worse at driving Claude", which only a conversation
reveals. These tests hold the part that IS mechanical: that every rule
arrived, and that the specific traps the old prompt had already been fixed
for did not come back with it.

    .venv/bin/python -m unittest tests.test_tool_guidance -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tools  # noqa: E402

DESCRIPTIONS = {t["name"]: t["description"] for t in tools.TOOL_DEFINITIONS}


class EveryToolExplainsItselfTests(unittest.TestCase):
    def test_every_tool_has_a_description(self):
        for name, desc in DESCRIPTIONS.items():
            self.assertTrue(desc.strip(), name)

    def test_every_moved_rule_arrived(self):
        """The one real regression risk in the move: a rule silently dropped.

        A word-count floor would catch a description vanishing but not a rule
        going missing from inside one, and would break on any rewording. So
        each entry is a distinctive phrase that could only have come from the
        bullet that moved — if a tool's guidance is dropped or reworded past
        recognition, this names which one.

        `get_handoff` is absent on purpose: its original description already
        said everything its bullet did, so nothing moved and there is nothing
        to assert.
        """
        moved = {
            "start_session": "another=true",
            "tell_claude": "Reuse an existing session",
            "rename_session": "anywhere a session_id is expected",
            "list_sessions": "QUIET MODE",
            "run_slash_command": "WITHOUT the leading slash",
            "answer_prompt": "Never answer on their behalf",
            "set_mode": "auto-approves the covered prompt",
            "interrupt_session": "close_session",
            "peek_screen": "not necessarily the only input",
            "send_keys": "peek_screen or read_session",
            "mute": "set_narration",
            "remember": "One sentence",
            "set_narration": "do not apologise",
            "list_missions": "Refer to them by title",
            "cancel_mission": "pause_mission",
        }
        for name, phrase in moved.items():
            self.assertIn(phrase, DESCRIPTIONS[name],
                          f"{name} lost the rule that moved onto it")

    def test_no_description_repeats_itself(self):
        """The bulk move duplicated guidance the original descriptions already
        carried — ten of them, before this was caught. A description that says
        the same thing twice is longer for no gain, and length is exactly what
        makes one get skipped."""
        import re
        for name, desc in DESCRIPTIONS.items():
            sents = [re.sub(r"[^a-z ]", "", x.lower()).strip()
                     for x in re.split(r"(?<=[.!])\s+", desc) if x.strip()]
            seen: list[set[str]] = []
            for x in sents:
                ws = set(x.split())
                overlap = [p for p in seen
                           if len(ws) > 4 and len(ws & p) / max(1, len(ws | p)) > 0.45]
                self.assertFalse(overlap, f"{name} restates itself")
                seen.append(ws)

    def test_no_description_is_long_enough_to_be_ignored(self):
        # Generous, but a description that is really a manual stops being read.
        for name, desc in DESCRIPTIONS.items():
            self.assertLess(len(desc), 2400, f"{name} is {len(desc)} chars")


class TheOldTrapsDidNotComeBackTests(unittest.TestCase):
    """Each of these was a bug the prompt had already been fixed for. Moving
    the text is exactly when a fix gets dropped, so they are re-asserted
    against their new home."""

    def test_be_quiet_belongs_to_narration_and_never_to_mute(self):
        # The mute bullet once listed "be quiet" beside "stop listening". Taking
        # that branch turned the microphone off — which the same prompt says the
        # user cannot undo by voice. Volume phrasings are set_narration's;
        # only listening phrasings are mute's.
        self.assertIn("be quiet", DESCRIPTIONS["set_narration"].lower())
        self.assertNotIn("be quiet", DESCRIPTIONS["mute"].lower())
        # And mute must still redirect the talk-less case rather than swallow it.
        self.assertIn("set_narration", DESCRIPTIONS["mute"])

    def test_mute_still_warns_that_voice_cannot_undo_it(self):
        d = DESCRIPTIONS["mute"].lower()
        self.assertIn("cannot be undone by voice", d)
        self.assertIn("on-screen button", d)

    def test_agent_choice_promises_no_tool_that_does_not_exist(self):
        # There is no list_agents voice tool, which is why the guidance points
        # at the context's AGENTS list instead.
        self.assertNotIn("list_agents", " ".join(DESCRIPTIONS.values()))
        start = DESCRIPTIONS["start_session"]
        self.assertIn('agent="opencode"', start)
        self.assertIn("AGENTS list in your context", start)

    def test_start_session_still_carries_the_one_per_request_rule(self):
        # The duplicate guard exists in code (START_GUARD_SECS), but the model
        # re-calling start_session after a late answer is what it guards
        # against, and the prompt is what stops it happening at all.
        d = DESCRIPTIONS["start_session"]
        self.assertIn("ONCE", d)
        self.assertIn("rename_session", d)

    def test_answer_prompt_still_says_at_most_once_and_do_not_retry(self):
        # Assert the RULE, not one phrasing of it: this test was first written
        # against wording that later turned out to duplicate the original
        # description and was trimmed, which made it fail on intact behaviour.
        d = DESCRIPTIONS["answer_prompt"].lower()
        self.assertIn("at most once", d)
        self.assertTrue("don't retry" in d or "do not retry" in d or "never retry" in d,
                        "nothing tells it not to retry a resolved prompt")

    def test_set_mode_still_owns_the_allow_and_switch_case(self):
        # "allow that and switch to auto" must call ONLY set_mode; calling
        # answer_prompt too double-answers a prompt set_mode already covered.
        d = DESCRIPTIONS["set_mode"]
        self.assertIn("ONLY", d)
        self.assertIn("answer_prompt", d)

    def test_send_keys_is_still_framed_as_the_last_resort(self):
        d = DESCRIPTIONS["send_keys"].lower()
        self.assertIn("escape hatch", d)
        # However it is worded, it must point at the dedicated tools first —
        # raw keystrokes are the thing that breaks when Claude Code's UI moves.
        self.assertTrue("use only when the dedicated tools" in d
                        or "prefer the dedicated tools" in d,
                        "send_keys no longer defers to the dedicated tools")

    def test_tell_claude_still_says_it_returns_immediately(self):
        # Without this the model goes silent waiting for a turn that takes
        # minutes, or re-calls the tool to poll.
        d = DESCRIPTIONS["tell_claude"].lower()
        self.assertIn("background", d)
        self.assertIn("do not call this again to check progress", d)
        self.assertTrue("stay available to chat" in d or "keep talking" in d
                        or "keep chatting" in d,
                        "nothing tells it to keep the conversation going")

    def test_cancel_mission_still_explains_its_two_call_gate(self):
        d = DESCRIPTIONS["cancel_mission"]
        self.assertIn("TWO", d)
        self.assertIn("confirm", d)
        self.assertIn("never cancels anything", d)
