"""OpenCode asks; Yuri owns the workflow. The rule with teeth: a spoken "yes"
maps to OpenCode's "once", never "always" — granting standing permission is a
mode change, not an answer to a question.

Around that rule sit the properties that make the workflow safe rather than
merely working: an ambiguous answer never reaches OpenCode at all, a request
that is no longer pending is refused rather than answered blind, and an ask
that arrives on top of a finished turn defers that turn's completion instead
of consuming it — the two high-water marks Task 4 exists to protect.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.providers.base import ProjectContext, SessionOptions  # noqa: E402
from yuri.providers.opencode.provider import OpenCodeProvider  # noqa: E402
from yuri.providers.opencode.server import OpenCodeServer  # noqa: E402


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.addCleanup(lambda: self.fake.__exit__(None, None, None))
        self.p = OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))
        self.addAsyncCleanup(self.p.shutdown)
        self.h = await self.p.create_session(ProjectContext("p", "/tmp"),
                                             SessionOptions())


class Surfacing(_Base):
    async def test_a_pending_permission_becomes_needs_permission(self):
        self.fake.state.add_permission(self.h, "req1", "run rm -rf build",
                                       tool="bash", metadata={"command": "rm -rf build"})
        res = self.p.poll(self.h)
        self.assertEqual(res["status"], "needs_permission")
        pr = res["prompt"]
        self.assertEqual(pr["kind"], "permission")
        self.assertIn("rm -rf build", pr["text"])
        self.assertEqual(pr["options"], ["allow", "deny"])
        # request_id must be OpenCode's, so the domain's dedup keys off it
        # rather than falling back to a synthesized id.
        self.assertEqual(pr["request_id"], "req1")
        # tool_name/tool_input are what ApprovalService.record_request feeds to
        # risk_for; without them every OpenCode approval would be unlabelled.
        self.assertEqual(pr["tool_name"], "bash")
        self.assertEqual(pr["tool_input"], {"command": "rm -rf build"})
        self.assertIs(pr["multi_select"], False)

    async def test_a_pending_question_becomes_needs_choice(self):
        self.fake.state.add_question(self.h, "q1", "Which target?", ["web", "mobile"])
        res = self.p.poll(self.h)
        self.assertEqual(res["status"], "needs_choice")
        pr = res["prompt"]
        self.assertEqual(pr["kind"], "choice")
        self.assertIn("Which target?", pr["text"])
        self.assertEqual(pr["options"], ["web", "mobile"])
        self.assertEqual(pr["request_id"], "q1")
        self.assertIs(pr["multi_select"], False)

    async def test_a_permission_takes_precedence_over_history(self):
        # A blocked turn must report the ask, not "working" — the user is the
        # only thing that can unblock it.
        self.fake.state.push_event(self.h, "session.next.prompted")
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.assertEqual(self.p.poll(self.h)["status"], "needs_permission")

    async def test_a_permission_outranks_a_question_when_both_are_pending(self):
        """Both can be pending at once. Permission first, and pinned: it is the
        one that gates a side effect, carries a risk label through
        ApprovalService, and holds the one-pending-approval slot."""
        self.fake.state.add_question(self.h, "q1", "Which target?", ["web"])
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.assertEqual(self.p.poll(self.h)["status"], "needs_permission")
        self.p.answer(self.h, "allow")
        # And the question is still there, surfaced next.
        res = self.p.poll(self.h)
        self.assertEqual(res["status"], "needs_choice")
        self.assertEqual(res["prompt"]["request_id"], "q1")

    async def test_a_permission_with_no_title_still_says_something_speakable(self):
        # Narration renders "needs permission to {text}"; an empty text is
        # dropped entirely (narration/service.py), so the ask would be silent.
        self.fake.state.add_permission(self.h, "req1", "", tool="webfetch")
        pr = self.p.poll(self.h)["prompt"]
        self.assertIn("webfetch", pr["text"])

    async def test_the_prompt_text_is_clipped(self):
        self.fake.state.add_permission(self.h, "req1", "x" * 9000)
        pr = self.p.poll(self.h)["prompt"]
        self.assertLessEqual(len(pr["text"]), 2000)

    async def test_metadata_that_is_not_an_object_becomes_an_empty_tool_input(self):
        # record_request json.dumps the tool_input and risk_for indexes it; a
        # string there would be read as a mapping and mislabel the risk.
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.fake.state.permissions[self.h][0]["metadata"] = "not-an-object"
        pr = self.p.poll(self.h)["prompt"]
        self.assertEqual(pr["tool_input"], {})

    async def test_a_request_answered_out_of_band_stops_being_reported(self):
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.assertEqual(self.p.poll(self.h)["status"], "needs_permission")
        self.fake.state.permissions[self.h] = []      # answered in OpenCode's own UI
        self.assertEqual(self.p.poll(self.h)["status"], "idle")


class TheRule(_Base):
    async def test_allow_sends_once_and_never_always(self):
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.p.poll(self.h)
        self.p.answer(self.h, "allow")
        kind, sid, rid, body = self.fake.state.replies[-1]
        self.assertEqual((kind, sid, rid), ("permission", self.h, "req1"))
        self.assertEqual(body["reply"], "once")
        # THE RULE: one spoken yes must not grant a standing permission.
        self.assertNotEqual(body["reply"], "always")

    async def test_deny_sends_reject(self):
        self.fake.state.add_permission(self.h, "req1", "run rm -rf /")
        self.p.poll(self.h)
        self.p.answer(self.h, "deny")
        self.assertEqual(self.fake.state.replies[-1][3]["reply"], "reject")

    async def test_always_is_never_sent_for_any_phrasing(self):
        # Every phrasing decide_permission accepts as an allow must still be
        # "once". A provider that upgraded an enthusiastic yes to "always"
        # would be granting standing permission on the user's behalf.
        for phrasing in ("allow", "yes", "y", "sure", "ok", "approve",
                         "yes always", "always allow that"):
            self.fake.state.add_permission(self.h, f"r_{phrasing}", "run ls")
            self.assertEqual(self.p.poll(self.h)["status"], "needs_permission")
            self.p.answer(self.h, phrasing)
            reply = self.fake.state.replies[-1][3]["reply"]
            self.assertNotEqual(reply, "always",
                                f"{phrasing!r} was upgraded to a standing grant")
            self.assertEqual(reply, "once", f"{phrasing!r} did not reach OpenCode as an allow")

    async def test_every_deny_phrasing_reaches_opencode_as_reject(self):
        for phrasing in ("deny", "no", "nope", "don't", "stop", "cancel", "reject"):
            self.fake.state.add_permission(self.h, f"r_{phrasing}", "run rm -rf /")
            self.assertEqual(self.p.poll(self.h)["status"], "needs_permission")
            self.p.answer(self.h, phrasing)
            self.assertEqual(self.fake.state.replies[-1][3]["reply"], "reject",
                             f"{phrasing!r} did not reach OpenCode as a refusal")

    async def test_an_ambiguous_answer_is_refused_not_guessed(self):
        self.fake.state.add_permission(self.h, "req1", "run rm -rf /")
        self.p.poll(self.h)
        with self.assertRaises(ValueError):
            self.p.answer(self.h, "hmm maybe")
        self.assertEqual(self.fake.state.replies, [],
                         "an ambiguous answer must not reach OpenCode at all")

    async def test_an_ambiguous_answer_leaves_the_request_answerable(self):
        """Fail closed, not fail shut: the re-ask has to be able to land."""
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.p.poll(self.h)
        with self.assertRaises(ValueError):
            self.p.answer(self.h, "hmm maybe")
        self.assertEqual(self.p.poll(self.h)["status"], "needs_permission")
        self.p.answer(self.h, "yes")
        self.assertEqual(self.fake.state.replies[-1][3]["reply"], "once")

    async def test_answering_with_nothing_pending_is_a_soft_error(self):
        with self.assertRaises(ValueError):
            self.p.answer(self.h, "allow")
        self.assertEqual(self.fake.state.replies, [])

    async def test_a_stale_request_is_refused_rather_than_answered_blind(self):
        """The remembered id can die between poll and answer — answered in
        OpenCode's own UI, expired, or the session moved on. Forwarding into
        the void would report success for an approval nobody applied."""
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.p.poll(self.h)
        self.fake.state.permissions[self.h] = []
        with self.assertRaises(ValueError):
            self.p.answer(self.h, "allow")
        self.assertEqual(self.fake.state.replies, [],
                         "a dead request id must not be replied to")

    async def test_a_permission_is_never_answered_on_the_question_endpoint(self):
        self.fake.state.add_question(self.h, "q1", "Which target?", ["web"])
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.p.poll(self.h)                     # surfaces the permission
        self.p.answer(self.h, "allow")
        kinds = [r[0] for r in self.fake.state.replies]
        self.assertEqual(kinds, ["permission"])


class Questions(_Base):
    async def test_answering_a_question_uses_the_question_endpoint(self):
        self.fake.state.add_question(self.h, "q1", "Which target?", ["web", "mobile"])
        self.p.poll(self.h)
        self.p.answer(self.h, "web")
        kind, _, rid, body = self.fake.state.replies[-1]
        self.assertEqual((kind, rid), ("question", "q1"))
        # The exact value, not `in str(body)`: that would pass for a body that
        # merely mentions the option somewhere.
        self.assertEqual(body["reply"], "web")

    async def test_a_question_answer_is_not_gated_by_the_permission_words(self):
        """"no" is a legitimate answer to "Ship it?" — running it through
        decide_permission would turn an answer into a refusal to answer."""
        self.fake.state.add_question(self.h, "q1", "Ship it?", ["yes", "no"])
        self.p.poll(self.h)
        self.p.answer(self.h, "no")
        kind, _, rid, body = self.fake.state.replies[-1]
        self.assertEqual((kind, rid), ("question", "q1"))
        self.assertIn("no", str(body))

    async def test_an_option_is_matched_case_insensitively(self):
        self.fake.state.add_question(self.h, "q1", "Which target?", ["Web", "Mobile"])
        self.p.poll(self.h)
        self.p.answer(self.h, "  mobile ")
        self.assertIn("Mobile", str(self.fake.state.replies[-1][3]),
                      "the option OpenCode offered should go back verbatim")

    async def test_an_answer_in_the_users_own_words_is_forwarded_as_written(self):
        # Mirrors the Claude path, which lets the user answer a question
        # however they like rather than only by picking a listed option.
        self.fake.state.add_question(self.h, "q1", "Which target?", ["web", "mobile"])
        self.p.poll(self.h)
        self.p.answer(self.h, "whichever is faster")
        self.assertIn("whichever is faster", str(self.fake.state.replies[-1][3]))

    async def test_a_question_with_no_options_still_takes_a_free_text_answer(self):
        self.fake.state.add_question(self.h, "q1", "What should I name it?")
        res = self.p.poll(self.h)
        self.assertEqual(res["prompt"]["options"], [])
        self.p.answer(self.h, "the widget service")
        self.assertIn("the widget service", str(self.fake.state.replies[-1][3]))

    async def test_an_empty_answer_never_reaches_opencode(self):
        self.fake.state.add_question(self.h, "q1", "Which target?", ["web"])
        self.p.poll(self.h)
        with self.assertRaises(ValueError):
            self.p.answer(self.h, "   ")
        self.assertEqual(self.fake.state.replies, [])


class TheTwoMarks(_Base):
    """An ask outranks history, so it must not CONSUME history. Both marks are
    high-water marks over server-side state, so an early return defers a turn's
    completion rather than dropping it."""

    async def test_a_completion_waiting_behind_an_ask_is_deferred_not_lost(self):
        self.p.send_message(self.h, "do it")
        self.fake.state.push_assistant(self.h, "I finished the job.")
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.assertEqual(self.p.poll(self.h)["status"], "needs_permission")
        self.p.answer(self.h, "allow")
        done = self.p.poll(self.h)
        self.assertEqual(done["status"], "completed",
                         "the finished turn was swallowed by the ask")
        self.assertIn("I finished the job.", done["assistant_text"])
        # And still exactly once.
        self.assertEqual(self.p.poll(self.h)["status"], "idle")

    async def test_an_error_waiting_behind_an_ask_is_deferred_not_lost(self):
        self.p.send_message(self.h, "do it")
        self.fake.state.push_event(self.h, "session.next.step.failed",
                                   {"error": {"message": "HTTP 401: nope"}})
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.assertEqual(self.p.poll(self.h)["status"], "needs_permission")
        self.p.answer(self.h, "deny")
        res = self.p.poll(self.h)
        self.assertEqual(res["status"], "error")
        self.assertIn("401", res["error"])

    async def test_answering_resumes_the_turn_rather_than_reporting_idle(self):
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.p.poll(self.h)
        self.p.answer(self.h, "allow")
        self.assertEqual(self.p.poll(self.h)["status"], "working")
        self.fake.state.push_assistant(self.h, "listed them")
        self.assertIn("listed them", self.p.poll(self.h)["assistant_text"])

    async def test_a_repeated_ask_keeps_its_status_but_surfaces_its_prompt_once(self):
        """Yuri polls every 1.5s while the user thinks, and OpenCode keeps the
        request pending until it is answered.

        The STATUS must keep coming — SessionService reads it to hold the row
        at needs_permission and the mission at waiting_for_approval. The PROMPT
        must not: it is what narration speaks and what the frontend injects,
        and enqueueInjection deliberately never evicts a blocking item, so
        re-reporting it grew the injection queue without bound while the user
        was still deciding and Yuri would read the backlog aloud after they had
        already answered. Both Claude backends pop each result off a queue, so
        poll hands a given result back exactly once; this matches that.
        """
        self.p.send_message(self.h, "do it")
        self.fake.state.add_permission(self.h, "req1", "run ls")

        first = self.p.poll(self.h)
        self.assertEqual(first["status"], "needs_permission")
        self.assertEqual(first["prompt"]["request_id"], "req1")

        for i in range(3):
            again = self.p.poll(self.h)
            self.assertEqual(again["status"], "needs_permission", f"repeat {i}")
            self.assertNotIn("prompt", again, f"repeat {i} re-offered the prompt")

        # It is still answerable — the id is remembered, not forgotten.
        self.p.answer(self.h, "allow")
        self.assertEqual(self.fake.state.replies[-1][2], "req1")

    async def test_a_genuinely_new_ask_surfaces_even_right_after_another(self):
        """The de-dupe is per request_id, not a one-ask-per-session latch."""
        self.p.send_message(self.h, "do it")
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.assertEqual(self.p.poll(self.h)["prompt"]["request_id"], "req1")
        self.assertNotIn("prompt", self.p.poll(self.h))

        self.p.answer(self.h, "allow")
        self.fake.state.add_permission(self.h, "req2", "run rm -rf build")
        second = self.p.poll(self.h)
        self.assertEqual(second["prompt"]["request_id"], "req2")

    async def test_answering_an_unknown_handle_still_raises_keyerror(self):
        theirs = self.fake.state.new_session("/tmp/their-own-work")
        with self.assertRaises(KeyError):
            self.p.answer(theirs, "allow")


if __name__ == "__main__":
    unittest.main()
