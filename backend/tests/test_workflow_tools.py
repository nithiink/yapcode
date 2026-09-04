"""The workflow voice tools (spec §14.1).

Three requirements the plan names, each with a test here:
  * `start_mission` reads the plan back and WAITS.
  * no voice tool creates, edits or archives a specialist.
  * a spoken step reference that is ambiguous raises rather than guessing.

    .venv/bin/python -m unittest tests.test_workflow_tools -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import tools  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402

WORKFLOW_TOOLS = ("start_mission", "describe_roster", "workflow_status", "assign_task",
                  "retry_task", "skip_task", "list_templates")


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "proj")
        os.mkdir(self.root)
        self.patches = [
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
            mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri")),
        ]
        [p.start() for p in self.patches]
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)
        tools._pending_confirm = None

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    async def _start(self, **over):
        """Through the real two calls, because that IS the surface.

        The template is named rather than inferred: these tests are about
        assign/skip/retry, and leaving them to depend on which plan a phrasing
        maps to would make them move whenever TEMPLATE_HINTS changes.
        """
        args = {"goal": "stop the cycle detector hanging", "project": self.root,
                "template": "bug-fix"}
        args.update(over)
        armed = await tools.dispatch_tool("start_mission", dict(args))
        self.assertFalse(armed["started"])
        return await tools.dispatch_tool("start_mission", {**args, "confirm": armed["confirm"]})


class TheDeclarationsTests(Base):
    def test_every_workflow_tool_is_declared_and_dispatchable(self):
        names = {d["name"] for d in tools.TOOL_DEFINITIONS}
        for tool in WORKFLOW_TOOLS:
            self.assertIn(tool, names)

    def test_they_all_declare_a_tier_and_a_category(self):
        for d in tools.TOOL_DEFINITIONS:
            if d["name"] in WORKFLOW_TOOLS:
                self.assertIn(d.get("tier", "safe"), ("safe", "confirm"), d["name"])
                self.assertEqual(d.get("category"), "orchestration", d["name"])

    def test_no_voice_tool_creates_edits_or_archives_a_specialist(self):
        # A system prompt dictated through a speech recogniser is a persona
        # nobody reviewed, and it would then run with tool access on this
        # machine. Asserted, not left to a comment.
        forbidden = ("create_specialist", "create_agent", "edit_specialist",
                     "update_specialist", "delete_specialist", "archive_specialist",
                     "new_agent", "set_persona")
        names = {d["name"] for d in tools.TOOL_DEFINITIONS}
        self.assertEqual(names & set(forbidden), set())

    def test_the_roster_tool_says_it_cannot_create_one(self):
        # So the model offers the Agents view instead of apologising vaguely.
        d = next(d for d in tools.TOOL_DEFINITIONS if d["name"] == "describe_roster")
        self.assertIn("cannot create", d["description"])


class StartMissionTests(Base):
    async def test_the_first_call_plans_and_starts_absolutely_nothing(self):
        # The whole mitigation for spoken authoring.
        out = await tools.dispatch_tool("start_mission", {"goal": "fix the hang",
                                                          "project": self.root})
        self.assertFalse(out["started"])
        self.assertTrue(out["confirm"])
        self.assertIn("NOTHING HAS STARTED", out["message"])
        self.assertEqual(self.c.missions.list(), [])
        self.assertEqual([c for c in self.fake.calls if c[0] == "start"], [])

    async def test_the_plan_names_each_step_and_who_would_do_it(self):
        out = await tools.dispatch_tool("start_mission", {"goal": "fix the hang",
                                                          "project": self.root})
        self.assertTrue(out["plan"])
        for step in out["plan"]:
            self.assertTrue(step["step"])
        # At least one step has someone: the roster is seeded.
        self.assertTrue(any(s["who"] for s in out["plan"]))

    async def test_the_second_call_runs_it(self):
        out = await self._start()
        self.assertTrue(out["started"])
        self.assertTrue(out["mission_id"])
        self.assertTrue(out["steps"])

    async def test_a_token_armed_for_one_goal_cannot_start_another(self):
        # The arm is keyed on the goal, because the first call created nothing
        # to key on — and a misheard goal is exactly what this guards.
        armed = await tools.dispatch_tool("start_mission", {"goal": "fix the hang",
                                                            "project": self.root})
        other = await tools.dispatch_tool("start_mission", {"goal": "delete the database",
                                                            "project": self.root,
                                                            "confirm": armed["confirm"]})
        self.assertFalse(other["started"], "a token for one goal started a different mission")

    async def test_the_token_is_single_use(self):
        armed = await tools.dispatch_tool("start_mission", {"goal": "fix the hang",
                                                            "project": self.root})
        args = {"goal": "fix the hang", "project": self.root, "confirm": armed["confirm"]}
        first = await tools.dispatch_tool("start_mission", dict(args))
        self.assertTrue(first["started"])
        again = await tools.dispatch_tool("start_mission", dict(args))
        self.assertFalse(again["started"], "a spent token started a second mission")

    async def test_no_goal_is_a_soft_error_that_says_what_to_ask(self):
        with self.assertRaises(ValueError) as ctx:
            await tools.dispatch_tool("start_mission", {"goal": "  "})
        self.assertIn("ask the user", str(ctx.exception).lower())

    async def test_an_unknown_template_lists_the_real_ones(self):
        with self.assertRaises(ValueError) as ctx:
            await tools.dispatch_tool("start_mission", {"goal": "g", "template": "nope"})
        self.assertIn("bug-fix", str(ctx.exception))

    async def test_the_plan_says_when_no_one_can_do_a_step(self):
        # Worth saying while the user is deciding, not as a failed task later.
        for s in self.c.roster.list():
            s.archived = True
            self.c.store.specialists.update(s)
        out = await tools.dispatch_tool("start_mission", {"goal": "g", "project": self.root})
        self.assertTrue(out["no_one_for"])
        self.assertIn("no one can do", out["message"])


class StatusAndAssignTests(Base):
    async def asyncSetUp(self):
        self.started = await self._start()
        self.mission_id = self.started["mission_id"]

    async def test_workflow_status_reads_back_the_steps(self):
        out = await tools.dispatch_tool("workflow_status", {"mission": self.mission_id})
        self.assertEqual(out["status"], "running")
        self.assertTrue(out["steps"])
        self.assertIn("who", out["steps"][0])

    async def test_a_mission_with_no_plan_says_so_rather_than_looking_empty(self):
        m = self.c.missions.create(self.c.projects.resolve_or_create(self.root),
                                   "plain", created_by="test")
        with self.assertRaises(ValueError) as ctx:
            await tools.dispatch_tool("workflow_status", {"mission": m.id})
        self.assertIn("no plan", str(ctx.exception))

    async def test_a_step_can_be_given_to_a_named_specialist(self):
        who = next(s for s in self.c.roster.list() if s.role == "reviewer")
        out = await tools.dispatch_tool("assign_task", {"mission": self.mission_id,
                                                        "task": "Review", "specialist": who.name})
        self.assertEqual(out["specialist"], who.name)

    async def test_an_unknown_specialist_lists_the_real_ones(self):
        with self.assertRaises(ValueError) as ctx:
            await tools.dispatch_tool("assign_task", {"mission": self.mission_id,
                                                      "task": "Review", "specialist": "Gandalf"})
        self.assertIn("Researcher", str(ctx.exception))

    async def test_an_ambiguous_step_reference_refuses_to_guess(self):
        # Picking one would run the wrong step. Same rule missions.resolve
        # follows.
        with self.assertRaises(ValueError) as ctx:
            await tools.dispatch_tool("assign_task", {"mission": self.mission_id,
                                                      "task": "the", "specialist": "Reviewer"})
        msg = str(ctx.exception)
        self.assertIn("matches several", msg)
        self.assertIn("ask which", msg)

    async def test_an_unmatched_step_reference_reads_the_plan_back(self):
        with self.assertRaises(ValueError) as ctx:
            await tools.dispatch_tool("workflow_status", {"mission": self.mission_id})
            await tools.dispatch_tool("assign_task", {"mission": self.mission_id,
                                                      "task": "deploy to production",
                                                      "specialist": "Reviewer"})
        self.assertIn("The plan is", str(ctx.exception))

    async def test_an_empty_step_reference_asks_which(self):
        with self.assertRaises(ValueError) as ctx:
            await tools.dispatch_tool("retry_task", {"mission": self.mission_id, "task": " "})
        self.assertIn("which step", str(ctx.exception).lower())


class SkipAndRetryTests(Base):
    async def asyncSetUp(self):
        self.started = await self._start()
        self.mission_id = self.started["mission_id"]

    async def test_skip_does_nothing_on_the_first_call(self):
        out = await tools.dispatch_tool("skip_task", {"mission": self.mission_id,
                                                      "task": "Review"})
        self.assertFalse(out["skipped"])
        self.assertIn("Nothing has been skipped yet", out["message"])

    async def test_skipping_a_step_with_a_check_says_what_stops_being_checked(self):
        # "the mission finishes without that check ever having run" is the
        # part the user needs to hear.
        out = await tools.dispatch_tool("skip_task", {"mission": self.mission_id,
                                                      "task": "Run the tests"})
        self.assertIn("tests_pass", out["message"])

    async def test_the_second_call_skips_it(self):
        armed = await tools.dispatch_tool("skip_task", {"mission": self.mission_id,
                                                        "task": "Review"})
        out = await tools.dispatch_tool("skip_task", {"mission": self.mission_id,
                                                      "task": "Review",
                                                      "confirm": armed["confirm"]})
        self.assertTrue(out["skipped"])

    async def test_a_token_armed_for_one_step_does_not_skip_another(self):
        armed = await tools.dispatch_tool("skip_task", {"mission": self.mission_id,
                                                        "task": "Review"})
        other = await tools.dispatch_tool("skip_task", {"mission": self.mission_id,
                                                        "task": "Run the tests",
                                                        "confirm": armed["confirm"]})
        self.assertFalse(other["skipped"], "a token armed for one step skipped another")

    async def test_retrying_a_step_that_has_not_failed_is_a_soft_error(self):
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("retry_task", {"mission": self.mission_id,
                                                     "task": "Review"})

    async def test_retry_reports_what_had_gone_wrong(self):
        # Retrying without changing anything usually fails the same way, so
        # the model needs the reason to say it out loud.
        w = self.c.workflow.live_for_mission(self.mission_id)
        t = self.c.workflow.tasks_of(w.id)[0]
        for _ in range(t.max_attempts + 1):
            await self.c.workflow.on_task_finished(t.id, ok=False, error="it ran out of context")
        out = await tools.dispatch_tool("retry_task", {"mission": self.mission_id,
                                                       "task": t.title})
        self.assertIn("ran out of context", out["previous_error"])


class TemplateTests(Base):
    async def test_the_plan_shapes_are_listed_with_their_steps(self):
        out = await tools.dispatch_tool("list_templates", {})
        names = [t["name"] for t in out["templates"]]
        self.assertIn("bug-fix", names)
        self.assertTrue(all(t["steps"] for t in out["templates"]))

    async def test_the_roster_is_read_back_by_name_and_role(self):
        out = await tools.dispatch_tool("describe_roster", {})
        self.assertTrue(out["specialists"])
        self.assertFalse(out["can_create_by_voice"])
        self.assertIn("role", out["specialists"][0])

    async def test_the_roster_can_be_filtered_by_role(self):
        out = await tools.dispatch_tool("describe_roster", {"role": "reviewer"})
        self.assertTrue(out["specialists"])
        self.assertEqual({s["role"] for s in out["specialists"]}, {"reviewer"})

    async def test_an_unknown_role_says_which_roles_exist(self):
        with self.assertRaises(ValueError) as ctx:
            await tools.dispatch_tool("describe_roster", {"role": "wizard"})
        self.assertIn("researcher", str(ctx.exception))


class TheGateOnlyFiresWhenSomethingRanTests(Base):
    """A confirm-tier tool that REFUSED to act has not run ungated.

    The central check was a `finally`, so a soft ValueError — cancel_mission
    for a mission that does not exist, start_mission with no goal — came back
    to the model as "the tool failed unexpectedly" instead of "which
    mission?". Both directions are pinned here.
    """

    async def test_a_refusal_reaches_the_model_as_its_own_reason(self):
        for tool, args, expect in (
            ("cancel_mission", {"mission": "no-such-mission"}, ""),
            ("start_mission", {"goal": " "}, "ask the user"),
            ("skip_task", {"mission": "no-such-mission", "task": "x"}, ""),
        ):
            with self.assertRaises(ValueError, msg=tool) as ctx:
                await tools.dispatch_tool(tool, args)
            if expect:
                self.assertIn(expect, str(ctx.exception).lower())

    async def test_a_confirm_tool_that_actually_runs_ungated_still_raises(self):
        # The protection itself must not have been weakened by the fix.
        async def ungated(name, args):
            return {"ok": "ran without asking anyone"}
        with mock.patch.object(tools, "_dispatch", ungated):
            with self.assertRaises(AssertionError):
                await tools.dispatch_tool("skip_task", {})


class TheDuplicateMissionLoopTests(Base):
    """The loop observed live on 2026-09-04, and why it ran.

    Gemini fires `start_mission` two or three times within a couple of
    seconds, carrying the SAME confirm token — the identical quirk
    `start_session`'s `_last_start` guard exists for. The first call consumed
    the token and started the mission. Every later call found the token spent,
    so the gate armed a FRESH one and answered "NOTHING HAS STARTED. Read this
    plan back and wait for the user to agree."

    That sentence was false — a mission was already running — and it was also
    an instruction, so the model read the plan back, the user said yes, and a
    second mission started. Five missions and five live OpenCode agents from
    one request.

    The gate was working exactly as designed; what was missing is that the
    gate has no idea a mission was just created for this goal.
    """

    async def _first_two_calls(self):
        args = {"goal": "build the landing page", "project": self.root}
        armed = await tools.dispatch_tool("start_mission", dict(args))
        started = await tools.dispatch_tool("start_mission", {**args, "confirm": armed["confirm"]})
        self.assertTrue(started["started"])
        return args, armed, started

    async def test_a_repeat_call_does_not_start_a_second_mission(self):
        args, armed, started = await self._first_two_calls()
        again = await tools.dispatch_tool("start_mission", {**args, "confirm": armed["confirm"]})
        self.assertFalse(again["started"])
        self.assertEqual(len(self.c.missions.active()), 1, "a second mission was created")

    async def test_a_repeat_call_never_claims_nothing_has_started(self):
        # The sentence that drove the loop. It has to be impossible to say
        # once something HAS started.
        args, armed, _ = await self._first_two_calls()
        again = await tools.dispatch_tool("start_mission", {**args, "confirm": armed["confirm"]})
        self.assertNotIn("NOTHING HAS STARTED", again["message"])
        self.assertIn("already", again["message"].lower())

    async def test_a_repeat_call_points_at_the_mission_that_is_running(self):
        # So the model can say "it's underway" instead of re-planning.
        args, armed, started = await self._first_two_calls()
        again = await tools.dispatch_tool("start_mission", {**args, "confirm": armed["confirm"]})
        self.assertEqual(again["mission_id"], started["mission_id"])
        self.assertTrue(again["already_running"])

    async def test_a_repeat_call_does_not_hand_back_a_fresh_token(self):
        # A new token is what made the next "yes" start another mission.
        args, armed, _ = await self._first_two_calls()
        again = await tools.dispatch_tool("start_mission", {**args, "confirm": armed["confirm"]})
        self.assertNotIn("confirm", again)

    async def test_a_call_with_no_token_at_all_also_refuses_to_replan(self):
        # The model sometimes drops the token entirely on the retry.
        args, _, _ = await self._first_two_calls()
        again = await tools.dispatch_tool("start_mission", dict(args))
        self.assertFalse(again["started"])
        self.assertTrue(again["already_running"])

    async def test_the_user_can_still_deliberately_start_a_second_one(self):
        # Refusing forever would make "actually, do that again separately"
        # impossible. Same escape start_session's guard has.
        args, _, _ = await self._first_two_calls()
        armed = await tools.dispatch_tool("start_mission", {**args, "another": True})
        self.assertFalse(armed["started"])
        second = await tools.dispatch_tool(
            "start_mission", {**args, "another": True, "confirm": armed["confirm"]})
        self.assertTrue(second["started"])
        self.assertEqual(len(self.c.missions.active()), 2)

    async def test_a_different_goal_is_never_blocked_by_the_guard(self):
        await self._first_two_calls()
        armed = await tools.dispatch_tool("start_mission", {"goal": "something else entirely",
                                                            "project": self.root})
        self.assertFalse(armed["started"])
        self.assertTrue(armed["confirm"], "a different goal was refused")
        self.assertIn("NOTHING HAS STARTED", armed["message"])

    async def test_the_guard_survives_a_restart_because_it_reads_the_store(self):
        # In-memory bookkeeping would forget across a reload — and uvicorn's
        # --reload restarts the process while the user is talking.
        args, armed, started = await self._first_two_calls()
        tools._last_mission = None          # as if the process had restarted
        again = await tools.dispatch_tool("start_mission", dict(args))
        self.assertTrue(again["already_running"])
        self.assertEqual(again["mission_id"], started["mission_id"])

    async def test_a_finished_mission_does_not_block_starting_it_again(self):
        # The guard is about work IN FLIGHT, not about history.
        args, armed, started = await self._first_two_calls()
        await self.c.missions.cancel(started["mission_id"], by="test")
        armed2 = await tools.dispatch_tool("start_mission", dict(args))
        self.assertTrue(armed2.get("confirm"), "a cancelled mission still blocked a new one")


class TheTemplateChoiceTests(Base):
    """"Create a website" went to the bug-fix plan, so a researcher was told
    to "Investigate the bug" for a landing page. `feature` existed the whole
    time; the default just never looked at the goal."""

    async def _plan_for(self, goal: str) -> dict:
        return await tools.dispatch_tool("start_mission", {"goal": goal, "project": self.root})

    async def test_building_something_does_not_get_the_bug_plan(self):
        out = await self._plan_for(
            "create a single-page HTML website describing what Yuri can do")
        self.assertNotEqual(out["template"], "bug-fix")
        steps = " ".join(p["step"] for p in out["plan"]).lower()
        self.assertNotIn("bug", steps, f"a website build was planned as {out['template']}")

    async def test_fixing_something_still_gets_the_bug_plan(self):
        out = await self._plan_for("fix the crash when the cycle detector hangs")
        self.assertEqual(out["template"], "bug-fix")

    async def test_each_kind_of_ask_lands_somewhere_sensible(self):
        for goal, expected in [
            ("build a landing page for the docs", "feature"),
            ("add a dark mode toggle", "feature"),
            ("fix the failing login test", "bug-fix"),
            ("the export button is broken", "bug-fix"),
            ("refactor the session manager", "refactor"),
            ("look into why startup is slow", "research"),
            ("review the changes on this branch", "code-review"),
        ]:
            out = await self._plan_for(goal)
            self.assertEqual(out["template"], expected, goal)
            # Each call arms the gate; clear it so the next goal starts clean.
            tools._pending_confirm = None

    async def test_an_ask_that_matches_nothing_gets_the_simplest_plan(self):
        # `single` is one task and one specialist: never absurd, whatever the
        # goal turns out to be. Guessing a multi-step plan for an ask we did
        # not understand is how "Investigate the bug" happened.
        out = await self._plan_for("xyzzy plugh")
        self.assertEqual(out["template"], "single")

    async def test_the_chosen_plan_is_named_so_she_can_say_it(self):
        out = await self._plan_for("build a landing page")
        self.assertIn(out["template"], out["message"])

    async def test_an_explicit_template_always_wins(self):
        out = await self._plan_for_template("build a landing page", "research")
        self.assertEqual(out["template"], "research")

    async def _plan_for_template(self, goal: str, template: str) -> dict:
        return await tools.dispatch_tool("start_mission", {"goal": goal, "template": template,
                                                            "project": self.root})
