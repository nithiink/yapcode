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

from claude_runner import Prompt, SDKClaudeRunner, _Session  # noqa: E402
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


def _sdk_runner_with_session(pending: bool = True):
    r = SDKClaudeRunner(default_model="opus")
    s = _Session("bbbbbbbb-0000-0000-0000-000000000000", "/tmp", "opus")
    if pending:
        s.pending = Prompt(kind="permission", text="run rm -rf /tmp/x",
                           options=["allow", "deny"], tool_name="Bash")
        # SDK runner gates on _decision (the parked can_use_tool future)
        # rather than a decision file.
        s._decision = asyncio.get_running_loop().create_future()
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


class TmuxAmbiguousAndMultiQuestion(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_answer_releases_claim_for_retry(self):
        # An unclear reply writes no decision and keeps the prompt pending; the
        # claim must release so the SAME prompt can be answered again.
        r, s = _runner_with_session()
        r.start_answer(s.handle, "maybe")        # claims prompt_seq 1
        self.assertEqual(s.answer_claimed, s.prompt_seq)
        r._bg[s.handle].cancel()
        await asyncio.sleep(0)
        res = await r.answer(s.handle, "maybe", seq=1)  # decide_permission -> None
        self.assertNotEqual(res.status, "error")
        self.assertEqual(s.status, "needs_permission")
        self.assertEqual(s.answer_claimed, -1)   # claim released
        self.assertIsNotNone(s.pending)          # prompt still parked
        r.start_answer(s.handle, "allow")        # the retry must not fail fast
        r._bg[s.handle].cancel()
        await asyncio.sleep(0)

    async def test_deny_then_allow_on_same_prompt_fails_fast(self):
        # A claimed "deny" can't be raced into an "allow" on the same prompt —
        # the second answer fails fast regardless of choice.
        r, s = _runner_with_session()
        r.start_answer(s.handle, "deny")
        with self.assertRaisesRegex(ValueError, "already being answered"):
            r.start_answer(s.handle, "allow")
        r._bg[s.handle].cancel()
        await asyncio.sleep(0)

    async def test_multi_question_advance_bumps_prompt_seq(self):
        # Advancing a multi-question form parks a fresh prompt: prompt_seq must
        # bump so an answer bound to the previous sub-question can't auto-advance
        # the next one.
        r, s = _runner_with_session(pending=False)
        s.pending = Prompt(kind="choice", text="Q1", options=["a", "b"],
                           tool_name="AskUserQuestion")
        s.questions = [{"question": "Q1", "options": ["a", "b"], "multi": False},
                       {"question": "Q2", "options": ["c", "d"], "multi": False}]
        s.q_index = 0
        s.prompt_seq = 1
        s.status = "needs_choice"

        async def _fake_answer_question(sess, choice):
            return True  # more questions remain -> form advances

        r._answer_question = _fake_answer_question
        res = await r.answer(s.handle, "a", seq=1)
        self.assertNotEqual(res.status, "error")
        self.assertEqual(s.q_index, 1)
        self.assertEqual(s.prompt_seq, 2)        # next sub-question is a new park
        self.assertEqual(s.status, "needs_choice")


class TmuxInterruptClose(unittest.IsolatedAsyncioTestCase):
    """interrupt/close write a deny directly, bypassing the prompt_seq/claim
    guard, to unblock the parked hook. Verify that bypass still can't let a
    stale in-flight answer write a second decision afterwards."""

    @staticmethod
    def _patch(r, writes):
        async def _noop_tmux(*a, **k):
            return ""
        r._tmux = _noop_tmux
        r._write_decision = lambda sess, choice: writes.append(choice) or False

    async def test_interrupt_denies_pending_and_keeps_guard(self):
        r, s = _runner_with_session()
        writes = []
        self._patch(r, writes)
        r.start_answer(s.handle, "allow")        # an answer is in flight (claims seq 1)
        await r.interrupt(s.handle)
        self.assertEqual(writes, ["deny"])       # teardown unblocked the parked hook
        self.assertIsNone(s.pending)
        self.assertEqual(s.status, "completed")
        # the stale answer (bound to seq 1) must not now write a 2nd decision
        res = await r.answer(s.handle, "allow", seq=1)
        self.assertEqual(res.status, "error")
        await asyncio.sleep(0)

    async def test_close_denies_pending_to_unblock_hook(self):
        r, s = _runner_with_session()
        writes = []
        self._patch(r, writes)
        r.start_answer(s.handle, "deny")
        bg = r._bg.get(s.handle)
        await r.close(s.handle)
        self.assertEqual(writes, ["deny"])       # parked hook unblocked on teardown
        self.assertNotIn(s.handle, r._sessions)  # session removed
        await asyncio.sleep(0)
        self.assertTrue(bg.done())               # in-flight answer cancelled


class SDKAnswerClaim(unittest.IsolatedAsyncioTestCase):
    """Mirror of AnswerClaim for the SDK runner — same guard, different gate
    (a parked can_use_tool future instead of a tmux decision file)."""

    async def test_no_pending_prompt_fails_fast(self):
        r, s = _sdk_runner_with_session(pending=False)
        with self.assertRaisesRegex(ValueError, "already resolved"):
            r.start_answer(s.handle, "allow")
        self.assertNotIn(s.handle, r._bg)

    async def test_second_answer_for_same_prompt_fails_fast(self):
        r, s = _sdk_runner_with_session()
        r.start_answer(s.handle, "allow")
        with self.assertRaisesRegex(ValueError, "already being answered"):
            r.start_answer(s.handle, "allow")
        r._bg[s.handle].cancel()
        await asyncio.sleep(0)

    async def test_stale_answer_does_not_decide_a_newer_prompt(self):
        r, s = _sdk_runner_with_session()
        s.prompt_seq = 2  # a newer prompt parked since the answer was requested
        res = await r.answer(s.handle, "allow", seq=1)
        self.assertEqual(res.status, "error")
        self.assertIn("already answered", res.error)
        self.assertFalse(s._decision.done())  # stale answer resolved nothing


class SDKSetModeClaim(unittest.IsolatedAsyncioTestCase):
    """The headline 'allow that AND switch to auto' scenario at the runner
    level: set_mode's covered-prompt auto-approve must yield exactly one
    decision. (No tmux equivalent — tmux set_mode drives real terminal I/O;
    the guard logic is identical in both runners.)"""

    async def test_set_mode_skips_auto_approve_when_prompt_already_claimed(self):
        r, s = _sdk_runner_with_session()
        s.answer_claimed = s.prompt_seq          # answer_prompt already claimed it
        await r.set_mode(s.handle, "auto")
        self.assertEqual(s.mode, "auto")
        self.assertNotIn(s.handle, r._bg)        # set_mode started no 2nd answer
        self.assertFalse(s._decision.done())

    async def test_set_mode_auto_approves_unclaimed_covered_prompt_exactly_once(self):
        r, s = _sdk_runner_with_session()
        fut = s._decision
        await r.set_mode(s.handle, "auto")       # 'switch to auto' alone
        self.assertEqual(s.answer_claimed, s.prompt_seq)  # set_mode claimed it
        self.assertIn(s.handle, r._bg)
        for _ in range(5):                        # let the answer task resolve fut
            if fut.done():
                break
            await asyncio.sleep(0)
        self.assertTrue(fut.done())               # the prompt was decided...
        self.assertEqual(fut.result(), "allow")   # ...exactly once, as an allow
        r._bg[s.handle].cancel()                  # task now parked at _stop.wait()
        await asyncio.sleep(0)

    async def test_set_mode_acceptedits_leaves_uncovered_prompt(self):
        r, s = _sdk_runner_with_session()         # pending tool is Bash
        await r.set_mode(s.handle, "acceptEdits")  # covers edits, not Bash
        self.assertEqual(s.mode, "acceptEdits")
        self.assertNotIn(s.handle, r._bg)         # start_answer was never called...
        self.assertFalse(s._decision.done())      # ...so the prompt stays parked


if __name__ == "__main__":
    unittest.main()
