"""Regression tests for the one-decision-per-prompt guard.

Saying "allow that and switch to auto mode" used to produce TWO allow
decisions: the voice model called both answer_prompt and set_mode, and
set_mode's covered-prompt auto-approve raced the queued answer task. Worse,
the duplicate answer was queued unbound — when it finally ran it could
approve a LATER prompt the user never heard. Answers now claim the specific
prompt (prompt_seq) and duplicates fail fast instead of queueing:

    python -m unittest discover -s backend/tests
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from claude_runner import Prompt  # noqa: E402
from tmux_runner import TmuxClaudeRunner, _TmuxSession  # noqa: E402


def _runner_with_session(pending: bool = True):
    r = TmuxClaudeRunner(default_model="opus")
    s = _TmuxSession("aaaaaaaa-0000-0000-0000-000000000000", "/tmp", "opus")
    if pending:
        s.pending = Prompt(kind="permission", text="run rm -rf /tmp/x",
                           options=["allow", "deny"], tool_name="Bash")
        s.pending_tool_use_id = "toolu_1"
        s.prompt_seq = 1
        s.status = "needs_permission"
    r._sessions[s.handle] = s
    return r, s


class AnswerClaim(unittest.IsolatedAsyncioTestCase):
    async def test_no_pending_prompt_fails_fast(self):
        r, s = _runner_with_session(pending=False)
        with self.assertRaisesRegex(ValueError, "already resolved"):
            r.start_answer(s.handle, "allow")
        self.assertNotIn(s.handle, r._bg)

    async def test_second_answer_for_same_prompt_fails_fast(self):
        r, s = _runner_with_session()
        r.start_answer(s.handle, "allow")
        with self.assertRaisesRegex(ValueError, "already being answered"):
            r.start_answer(s.handle, "allow")
        # exactly one answer task exists — nothing queued behind it
        self.assertEqual(s._extra_tasks, [])
        r._bg[s.handle].cancel()
        await asyncio.sleep(0)

    async def test_stale_answer_does_not_decide_a_newer_prompt(self):
        # An answer bound to prompt 1 runs after prompt 2 parked (the old
        # queued-allow hazard): it must refuse instead of approving prompt 2.
        r, s = _runner_with_session()
        s.prompt_seq = 2  # a newer prompt parked since the answer was requested
        res = await r.answer(s.handle, "allow", seq=1)
        self.assertEqual(res.status, "error")
        self.assertIn("already answered", res.error)
        self.assertIsNotNone(s.pending)  # the newer prompt is untouched

    async def test_new_prompt_is_claimable_after_previous_resolved(self):
        r, s = _runner_with_session()
        r.start_answer(s.handle, "allow")
        r._bg[s.handle].cancel()
        await asyncio.sleep(0)
        # next prompt parks: seq bumps, the old claim must not block it
        s.pending = Prompt(kind="permission", text="run ls",
                           options=["allow", "deny"], tool_name="Bash")
        s.prompt_seq += 1
        r._harvest_finished(s.handle)
        r.start_answer(s.handle, "allow")  # must not raise
        r._bg[s.handle].cancel()
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
