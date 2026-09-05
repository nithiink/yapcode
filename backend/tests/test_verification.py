"""Verification — the checks, and the engine actually running them (spec §10).

The property under test in this whole file is one sentence: a task reaches
`completed` only when every declared check produced a PASS. Before Task 9 the
engine entered `verifying`, published the check names and completed
unconditionally, so a `bug-fix` workflow marked its test task done with the
suite red. Half these tests are about the checks; the other half — the wiring
class at the bottom — are about that, because a verification module the engine
does not call is exactly the failure it was written to prevent.

    cd backend && .venv/bin/python -m unittest tests.test_verification -v
"""
import asyncio
import inspect
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.approval import Approval  # noqa: E402
from yuri.domain.artifact import Artifact  # noqa: E402
from yuri.domain.event import DEFAULTS, EventType  # noqa: E402
from yuri.domain.mission import Mission  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.domain.task import Task  # noqa: E402
from yuri.domain.workflow import Workflow  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.narration import policy  # noqa: E402
from yuri.narration.service import NarrationService  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.roster import RosterService  # noqa: E402
from yuri.services.verify import (CHECKS, DETAIL_MAX, CheckContext,  # noqa: E402
                                  VerificationResult, failures, passed, reason,
                                  run_checks, verify_config)
from yuri.services.workflow import WorkflowEngine  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402
from yuri.workflows.loader import VERIFY_NAMES, load_templates  # noqa: E402


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.root)
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.project = Project(slug="p", name="P", root_path=self.root)
        self.store.projects.insert(self.project)
        self.mission = Mission(title="m", project_id=self.project.id, goal="g")
        self.store.missions.insert(self.mission)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def mission_with(self, **verify_cfg) -> Mission:
        """A mission carrying verification commands. `Project` has no metadata
        column today (see verify.verify_config), so the mission is the source
        that actually exists — the resolver prefers the project the moment it
        grows one."""
        return Mission(title="m", project_id=self.project.id,
                       metadata={"verify": dict(verify_cfg)})

    def insert_task(self, **kw) -> Task:
        """A persisted task. `artifacts.task_id` is a real foreign key, so a
        check that reads artifacts has to be given a task that exists."""
        w = Workflow(mission_id=self.mission.id, status="running")
        self.store.workflows.insert(w)
        t = Task(workflow_id=w.id, ordinal=0, title=kw.pop("title", "the work"),
                 role=kw.pop("role", "developer"), **kw)
        self.store.tasks.insert(t)
        return t

    def ctx(self, **kw) -> CheckContext:
        base = dict(store=self.store, mission=self.mission, project=self.project,
                    cwd=self.root, timeout_s=30.0)
        base.update(kw)
        return CheckContext(**base)


class ContractTests(_Base):
    """The two sides that must not drift, and the rule that outranks everything."""

    def test_checks_keys_are_exactly_the_loader_s_frozen_names(self):
        # A template may only declare a name in VERIFY_NAMES, and the loader
        # validates that at authoring time. If this module implemented a
        # different set, one side would name a check the other cannot run — a
        # template that validates and then verifies nothing.
        self.assertEqual(set(CHECKS), set(VERIFY_NAMES))

    def test_every_check_is_a_coroutine_function(self):
        for name, fn in CHECKS.items():
            self.assertTrue(inspect.iscoroutinefunction(fn), name)

    def test_a_verification_result_refuses_an_unknown_verdict(self):
        with self.assertRaises(ValueError):
            VerificationResult("tests_pass", "probably", "")

    def test_unavailable_is_not_a_pass(self):
        # The whole module in one assertion.
        self.assertFalse(passed([VerificationResult("tests_pass", "unavailable", "x")]))
        self.assertFalse(passed([VerificationResult("tests_pass", "fail", "x")]))
        self.assertTrue(passed([VerificationResult("tests_pass", "pass", "x")]))

    async def test_no_checks_declared_is_a_pass(self):
        self.assertTrue(passed(await run_checks((), self.ctx())))

    def test_the_module_never_runs_a_command_through_a_shell(self):
        # Project config is user data. Interpolating it into a shell string is
        # how L1/L2 injection bugs got into this repo before (5149db7).
        import yuri.services.verify as mod
        src = inspect.getsource(mod)
        # assertFalse, not assertNotIn: the failure message must name the
        # token, not print the whole module back at you.
        for token in ("shell=True", "create_subprocess_shell", "os.system", "os.popen"):
            self.assertFalse(token in src,
                             f"{token} must never appear in yuri/services/verify.py")

    async def test_an_unknown_check_name_is_unavailable_not_ignored(self):
        res = await run_checks(("vibes_good",), self.ctx())
        self.assertEqual([r.verdict for r in res], ["unavailable"])
        self.assertFalse(passed(res))

    async def test_a_check_that_raises_is_unavailable_not_a_crash(self):
        async def boom(ctx):
            raise RuntimeError("kaboom")
        CHECKS["boom_check"] = boom
        try:
            res = await run_checks(("boom_check",), self.ctx())
        finally:
            del CHECKS["boom_check"]
        self.assertEqual(res[0].verdict, "unavailable")
        self.assertIn("kaboom", res[0].detail)

    def test_reason_names_the_check_that_said_no(self):
        why = reason([VerificationResult("tests_pass", "fail", "2 failed"),
                      VerificationResult("diff_scoped", "pass", "fine")])
        self.assertIn("tests_pass", why)
        self.assertIn("2 failed", why)
        self.assertNotIn("diff_scoped", why)
        self.assertEqual(len(failures([VerificationResult("a", "pass"),
                                       VerificationResult("b", "unavailable")])), 1)


class CommandCheckTests(_Base):
    async def test_a_project_with_no_test_command_cannot_claim_tests_pass(self):
        # The single most dangerous thing this feature could do is report a
        # pass for a check that never executed.
        res = await run_checks(("tests_pass",), self.ctx(mission=self.mission_with()))
        self.assertEqual([r.verdict for r in res], ["unavailable"])
        self.assertFalse(passed(res))

    async def test_the_detail_names_the_missing_configuration(self):
        res = await run_checks(("tests_pass",), self.ctx())
        self.assertIn("verify", res[0].detail)
        self.assertIn("tests", res[0].detail)

    async def test_a_tests_directory_is_never_taken_as_a_test_command(self):
        # Commands come from config, never guessed: inferring `pytest` from a
        # directory would run a command the user never chose, in their tree.
        os.makedirs(os.path.join(self.root, "tests"))
        res = await run_checks(("tests_pass",), self.ctx())
        self.assertEqual(res[0].verdict, "unavailable")

    async def test_a_passing_command_passes(self):
        m = self.mission_with(tests="sh -c 'exit 0'")
        res = await run_checks(("tests_pass",), self.ctx(mission=m))
        self.assertEqual(res[0].verdict, "pass")
        self.assertTrue(passed(res))

    async def test_a_failing_command_fails_and_carries_its_output(self):
        m = self.mission_with(tests="sh -c 'echo boom >&2; exit 1'")
        res = await run_checks(("tests_pass",), self.ctx(mission=m))
        self.assertEqual(res[0].verdict, "fail")
        self.assertIn("boom", res[0].detail)

    async def test_the_detail_is_capped_so_a_huge_log_never_reaches_the_voice(self):
        # A 10MB pytest log must not reach the voice model or the event log.
        m = self.mission_with(tests="sh -c 'head -c 400000 /dev/zero | tr \"\\0\" x; exit 1'")
        res = await run_checks(("tests_pass",), self.ctx(mission=m))
        self.assertEqual(res[0].verdict, "fail")
        self.assertLessEqual(len(res[0].detail), DETAIL_MAX)

    async def test_the_detail_keeps_the_tail_where_the_summary_lives(self):
        m = self.mission_with(
            tests="sh -c 'head -c 300000 /dev/zero | tr \"\\0\" x; "
                  "echo; echo 2 failed in test_billing.py; exit 1'")
        res = await run_checks(("tests_pass",), self.ctx(mission=m))
        self.assertIn("2 failed in test_billing.py", res[0].detail)

    async def test_a_hung_check_is_killed_and_reported_as_a_failure(self):
        m = self.mission_with(tests="sleep 30")
        res = await run_checks(("tests_pass",), self.ctx(mission=m, timeout_s=1))
        self.assertEqual(res[0].verdict, "fail")
        self.assertIn("timed out", res[0].detail.lower())

    async def test_a_hung_check_does_not_leave_the_child_running(self):
        marker = os.path.join(self.root, "still-alive")
        m = self.mission_with(
            tests=f"sh -c 'sleep 3; touch {marker}'")
        res = await run_checks(("tests_pass",), self.ctx(mission=m, timeout_s=1))
        self.assertEqual(res[0].verdict, "fail")
        await asyncio.sleep(3.5)
        self.assertFalse(os.path.exists(marker),
                         "the killed command's child survived and kept working")

    async def test_a_command_that_does_not_exist_is_unavailable(self):
        m = self.mission_with(tests="definitely-not-a-real-binary-9f2")
        res = await run_checks(("tests_pass",), self.ctx(mission=m))
        self.assertEqual(res[0].verdict, "unavailable")
        self.assertFalse(passed(res))

    async def test_an_unparseable_command_is_unavailable_not_a_crash(self):
        m = self.mission_with(tests="echo 'unbalanced")
        res = await run_checks(("tests_pass",), self.ctx(mission=m))
        self.assertEqual(res[0].verdict, "unavailable")

    async def test_a_missing_working_directory_is_unavailable(self):
        m = self.mission_with(tests="sh -c 'exit 0'")
        res = await run_checks(("tests_pass",),
                               self.ctx(mission=m, project=None,
                                        cwd=os.path.join(self.tmp.name, "gone")))
        self.assertEqual(res[0].verdict, "unavailable")

    async def test_the_command_runs_in_the_mission_s_cwd_not_ours(self):
        # A test command resolved against the backend's own directory would
        # verify the wrong repository and pass.
        open(os.path.join(self.root, "only-here"), "w").close()
        m = self.mission_with(tests="test -f only-here")
        res = await run_checks(("tests_pass",), self.ctx(mission=m))
        self.assertEqual(res[0].verdict, "pass")

    async def test_typecheck_reads_its_own_key(self):
        m = self.mission_with(tests="sh -c 'exit 0'", typecheck="sh -c 'exit 3'")
        res = await run_checks(("tests_pass", "typecheck_pass"), self.ctx(mission=m))
        self.assertEqual([r.verdict for r in res], ["pass", "fail"])
        self.assertFalse(passed(res))

    async def test_every_declared_check_runs_even_after_one_fails(self):
        m = self.mission_with(tests="sh -c 'exit 1'")
        res = await run_checks(("tests_pass", "typecheck_pass"), self.ctx(mission=m))
        self.assertEqual([r.check for r in res], ["tests_pass", "typecheck_pass"])

    def test_the_project_wins_over_the_mission_when_it_carries_config(self):
        # `Project` has no metadata field today; the resolver reads it via
        # getattr so it takes over the day the column lands.
        class _P:
            root_path = "/tmp"
            metadata = {"verify": {"tests": "from-project"}}
        cfg = verify_config(CheckContext(project=_P(),
                                         mission=self.mission_with(tests="from-mission")))
        self.assertEqual(cfg["tests"], "from-project")


class DiffScopedTests(_Base):
    def _repo(self) -> str:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.path.join(self.tmp.name, "gitconfig"),
               "GIT_CONFIG_SYSTEM": os.devnull}
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env=env)
        return self.root

    def _task(self, **result) -> Task:
        return Task(workflow_id="w", ordinal=0, title="fix", role="developer",
                    result=dict(result))

    async def test_a_task_that_declared_no_scope_is_unavailable(self):
        self._repo()
        res = await run_checks(("diff_scoped",), self.ctx(task=self._task()))
        self.assertEqual(res[0].verdict, "unavailable")
        self.assertFalse(passed(res))

    async def test_a_change_inside_the_declared_scope_passes(self):
        self._repo()
        os.makedirs(os.path.join(self.root, "src"))
        open(os.path.join(self.root, "src", "a.py"), "w").close()
        t = self._task(expected_paths=["src"])
        res = await run_checks(("diff_scoped",), self.ctx(task=t))
        self.assertEqual(res[0].verdict, "pass")

    async def test_a_new_file_outside_the_scope_fails_and_is_named(self):
        # Plain `git diff` cannot see a CREATED file, which is exactly the
        # escape this check exists to catch.
        self._repo()
        os.makedirs(os.path.join(self.root, "src"))
        open(os.path.join(self.root, "src", "a.py"), "w").close()
        open(os.path.join(self.root, "secrets.env"), "w").close()
        t = self._task(expected_paths=["src/"])
        res = await run_checks(("diff_scoped",), self.ctx(task=t))
        self.assertEqual(res[0].verdict, "fail")
        self.assertIn("secrets.env", res[0].detail)

    async def test_a_glob_scope_is_honoured(self):
        self._repo()
        open(os.path.join(self.root, "a.py"), "w").close()
        t = self._task(expected_paths=["*.py"])
        res = await run_checks(("diff_scoped",), self.ctx(task=t))
        self.assertEqual(res[0].verdict, "pass")

    async def test_a_directory_that_is_not_a_git_repo_is_unavailable(self):
        t = self._task(expected_paths=["src"])
        res = await run_checks(("diff_scoped",), self.ctx(task=t))
        self.assertEqual(res[0].verdict, "unavailable")
        self.assertFalse(passed(res))


class ReviewApprovedTests(_Base):
    def setUp(self):
        super().setUp()
        self.task = self.insert_task(title="review", role="reviewer")

    def _artifact(self, body: str, kind: str = "review") -> None:
        self.store.artifacts.insert(Artifact(
            mission_id=self.mission.id, task_id=self.task.id, kind=kind,
            title="r", body=body))

    async def _verdict(self) -> VerificationResult:
        return (await run_checks(("review_approved",), self.ctx(task=self.task)))[0]

    async def test_an_explicit_approval_passes(self):
        self._artifact("Looks fine.\nVERDICT: approved")
        self.assertEqual((await self._verdict()).verdict, "pass")

    async def test_changes_requested_fails(self):
        self._artifact("VERDICT: changes-requested")
        r = await self._verdict()
        self.assertEqual(r.verdict, "fail")
        self.assertIn("changes-requested", r.detail)

    async def test_a_review_with_no_verdict_line_is_unavailable_not_a_pass(self):
        # An agent that forgot the line has not approved anything, and reading
        # sentiment out of prose is the machine deciding for the reviewer.
        self._artifact("seems ok to me")
        r = await self._verdict()
        self.assertEqual(r.verdict, "unavailable")
        self.assertFalse(passed([r]))

    async def test_no_artifact_at_all_is_unavailable(self):
        r = await self._verdict()
        self.assertEqual(r.verdict, "unavailable")

    async def test_the_last_verdict_line_wins(self):
        self._artifact("VERDICT: approved\nOn reflection:\nVERDICT: changes-requested")
        self.assertEqual((await self._verdict()).verdict, "fail")

    async def test_a_verdict_in_a_non_review_artifact_still_counts(self):
        self._artifact("VERDICT: LGTM", kind="summary")
        self.assertEqual((await self._verdict()).verdict, "pass")

    async def test_the_word_verdict_in_prose_is_not_a_verdict(self):
        self._artifact("My verdict is that this is fine, honestly")
        self.assertEqual((await self._verdict()).verdict, "unavailable")


class HumanOkTests(_Base):
    def setUp(self):
        super().setUp()
        self.session = AgentSession(project_id=self.project.id, agent_id="fake",
                                    native_session_id="n1", backend="fake",
                                    working_directory=self.root,
                                    mission_id=self.mission.id)
        self.store.sessions.insert(self.session)
        self.task = self.insert_task(title="ship", session_id=self.session.id)

    def _approval(self, status: str) -> Approval:
        a = Approval(session_id=self.session.id, agent_id="fake", action="deploy",
                     tool_name="Bash", request_id=f"r-{status}", description="deploy it",
                     mission_id=self.mission.id)
        if status != "pending":
            a.resolve(status, "voice")
        self.store.approvals.insert(a)
        return a

    async def _verdict(self) -> VerificationResult:
        return (await run_checks(("human_ok",), self.ctx(task=self.task)))[0]

    async def test_an_allowed_approval_passes(self):
        self._approval("allowed")
        self.assertEqual((await self._verdict()).verdict, "pass")

    async def test_a_denied_approval_fails(self):
        self._approval("denied")
        self.assertEqual((await self._verdict()).verdict, "fail")

    async def test_an_unanswered_approval_is_unavailable_not_a_yes(self):
        self._approval("pending")
        r = await self._verdict()
        self.assertEqual(r.verdict, "unavailable")
        self.assertFalse(passed([r]))

    async def test_never_asked_is_unavailable(self):
        self.assertEqual((await self._verdict()).verdict, "unavailable")


class NarrationTests(unittest.TestCase):
    """verification.failed is the line that saves the user a trip to the logs."""

    def test_the_event_type_is_owned_registered_and_loud_enough_for_quiet_mode(self):
        self.assertEqual(policy.NARRATION_OWNER[EventType.VERIFICATION_FAILED], "stream")
        self.assertEqual(DEFAULTS[EventType.VERIFICATION_FAILED], ("warning", True))
        self.assertTrue(policy.speaks(EventType.VERIFICATION_FAILED, "warning", "quiet"))

    def _line(self, payload, mode="normal"):
        from yuri.domain.event import YuriEvent
        return NarrationService().line_for(
            YuriEvent.make(EventType.VERIFICATION_FAILED, mission_id="m", payload=payload),
            mode)

    def test_the_line_names_the_check_and_what_it_saw(self):
        line = self._line({"title": "run the tests", "will_retry": True,
                           "failed": [{"check": "tests_pass", "verdict": "fail",
                                       "detail": "2 failed in test_billing.py"}]})
        self.assertIn("tests failed", line.lower())
        self.assertIn("test_billing.py", line)
        self.assertIn("Trying again", line)

    def test_a_final_failure_does_not_promise_another_attempt(self):
        line = self._line({"title": "run the tests", "will_retry": False,
                           "failed": [{"check": "tests_pass", "detail": "1 failed"}]})
        self.assertNotIn("Trying again", line)

    def test_task_failed_stays_silent_when_verification_already_said_it(self):
        from yuri.domain.event import YuriEvent
        svc = NarrationService()
        payload = {"title": "run the tests", "reason": "tests_pass: 2 failed",
                   "will_retry": True}
        self.assertIsNotNone(svc.line_for(
            YuriEvent.make(EventType.TASK_FAILED, payload=payload), "normal"))
        self.assertIsNone(svc.line_for(
            YuriEvent.make(EventType.TASK_FAILED, payload={**payload, "derived": True}),
            "normal"))


class EngineWiringTests(unittest.IsolatedAsyncioTestCase):
    """The half that matters: the engine RUNS the checks.

    A verification module the engine never calls is a feature that passes its
    own review and is unreachable from the product — the exact failure this
    task exists to end.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.root)
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.q = self.bus.subscribe()
        registry = AgentRegistry()
        registry.register(FakeAgentProvider())
        self.roster = RosterService(self.store, self.bus, registry)
        self.roster.seed()
        self.engine = WorkflowEngine(self.store, self.bus, Journal(self.home),
                                     self.roster, load_templates())
        self.project = Project(slug="p", name="P", root_path=self.root)
        self.store.projects.insert(self.project)
        self.dispatched: list[str] = []

        async def dispatch(task, specialist, instruction):
            s = AgentSession(project_id=self.project.id, agent_id=specialist.provider_id,
                             native_session_id=f"native-{len(self.dispatched)}",
                             backend="fake", working_directory=self.root,
                             mission_id=self.mission.id)
            self.store.sessions.insert(s)
            self.dispatched.append(task.id)
            return s.id
        self.engine.dispatch = dispatch
        self.mission = Mission(title="m", project_id=self.project.id, goal="fix it")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _configure(self, **verify_cfg):
        self.mission.metadata = {"verify": dict(verify_cfg)}

    def _insert_mission(self):
        self.store.missions.insert(self.mission)

    def _types(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait().type)
        return out

    async def _one_task_workflow(self, verification):
        self._insert_mission()
        w = await self.engine.create(self.mission, "single", goal="do it", tasks=[
            {"id": "t1", "role": "developer", "title": "the work",
             "instruction": "do it", "verification": list(verification)}])
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        return w, self.store.tasks.for_workflow(w.id)[0]

    async def test_a_task_whose_tests_fail_does_not_complete(self):
        # The behaviour this whole task exists to end. The agent reported
        # success; the suite did not.
        self._configure(tests="sh -c 'echo 2 failed in test_billing.py; exit 1'")
        w, t = await self._one_task_workflow(("tests_pass",))
        await self.engine.on_task_finished(t.id, ok=True)
        self.assertNotEqual(self.store.tasks.get(t.id).status, "completed")
        # Second attempt fails the same way: the error the user is shown names
        # the check and what it saw. (After the FIRST failure the retry has
        # already re-dispatched and cleared `error`, by design.)
        await self.engine.on_task_finished(t.id, ok=True)
        after = self.store.tasks.get(t.id)
        self.assertEqual(after.status, "blocked")
        self.assertIn("tests_pass", after.error or "")
        self.assertIn("test_billing.py", after.error or "")

    async def test_a_failing_check_routes_through_the_normal_retry_policy(self):
        self._configure(tests="sh -c 'exit 1'")
        w, t = await self._one_task_workflow(("tests_pass",))
        await self.engine.on_task_finished(t.id, ok=True)
        # First failure: re-dispatched by advance(), attempt 2.
        self.assertEqual(len(self.dispatched), 2)
        t2 = self.store.tasks.get(t.id)
        self.assertEqual(t2.status, "dispatched")
        await self.engine.on_task_finished(t.id, ok=True)
        # Attempts exhausted: blocked, and the workflow parks on the human.
        self.assertEqual(self.store.tasks.get(t.id).status, "blocked")
        self.assertEqual(self.store.workflows.get(w.id).status, "waiting_for_human")

    async def test_a_verification_failure_publishes_the_event_that_names_it(self):
        self._configure(tests="sh -c 'echo boom; exit 1'")
        w, t = await self._one_task_workflow(("tests_pass",))
        self._types()                      # drain create/dispatch
        await self.engine.on_task_finished(t.id, ok=True)
        events = []
        while not self.q.empty():
            events.append(self.q.get_nowait())
        failed = [e for e in events if e.type == EventType.VERIFICATION_FAILED]
        self.assertEqual(len(failed), 1)
        p = failed[0].payload
        self.assertEqual([f["check"] for f in p["failed"]], ["tests_pass"])
        self.assertIn("boom", p["failed"][0]["detail"])
        # And it renders a sentence, on the carrier that owns it.
        self.assertIsNotNone(NarrationService().line_for(failed[0], "normal"))

    async def test_no_test_command_configured_fails_the_task_rather_than_passing_it(self):
        # No `verify` config at all: `unavailable`, which is NOT a pass. A
        # project that never configured a test command cannot claim its tests
        # passed, and the error says where to configure one.
        w, t = await self._one_task_workflow(("tests_pass",))
        await self.engine.on_task_finished(t.id, ok=True)
        self.assertNotEqual(self.store.tasks.get(t.id).status, "completed")
        await self.engine.on_task_finished(t.id, ok=True)
        after = self.store.tasks.get(t.id)
        self.assertEqual(after.status, "blocked")
        self.assertIn("verify", after.error or "")

    async def test_a_passing_check_completes_the_task(self):
        self._configure(tests="sh -c 'exit 0'")
        w, t = await self._one_task_workflow(("tests_pass",))
        await self.engine.on_task_finished(t.id, ok=True)
        self.assertEqual(self.store.tasks.get(t.id).status, "completed")
        self.assertEqual(self.store.workflows.get(w.id).status, "completed")

    async def test_no_checks_declared_still_passes_through_verifying(self):
        # The timeline must never show work completing without the step that
        # decided it (spec §7.2).
        w, t = await self._one_task_workflow(())
        self._types()
        await self.engine.on_task_finished(t.id, ok=True)
        types = self._types()
        self.assertIn(EventType.TASK_VERIFYING, types)
        self.assertIn(EventType.TASK_COMPLETED, types)
        self.assertLess(types.index(EventType.TASK_VERIFYING),
                        types.index(EventType.TASK_COMPLETED))
        self.assertEqual(self.store.tasks.get(t.id).status, "completed")

    async def test_the_checks_run_in_the_mission_s_own_working_directory(self):
        open(os.path.join(self.root, "only-here"), "w").close()
        self._configure(tests="test -f only-here")
        w, t = await self._one_task_workflow(("tests_pass",))
        await self.engine.on_task_finished(t.id, ok=True)
        self.assertEqual(self.store.tasks.get(t.id).status, "completed")

    async def test_a_bug_fix_workflow_stops_at_the_test_task_when_the_suite_is_red(self):
        # End to end on a SHIPPED template: `bug-fix` declares tests_pass on
        # its test task, and that task used to complete with the suite red.
        self._configure(tests="sh -c 'exit 1'")
        self._insert_mission()
        w = await self.engine.create(self.mission, "bug-fix", goal="fix it")
        await self.engine.resume(w.id)
        await self.engine.advance(w.id)
        for _ in range(12):
            live = [t for t in self.store.tasks.for_workflow(w.id)
                    if t.status in ("dispatched", "running", "verifying")]
            if not live:
                break
            for t in live:
                await self.engine.on_task_finished(t.id, ok=True)
        by_title = {t.title.lower(): t for t in self.store.tasks.for_workflow(w.id)}
        test_task = next(t for k, t in by_title.items() if "tests_pass" in t.verification)
        self.assertEqual(test_task.status, "blocked")
        self.assertNotEqual(self.store.workflows.get(w.id).status, "completed")
        self.assertNotEqual(self.store.missions.get(self.mission.id).status, "completed")


if __name__ == "__main__":
    unittest.main()


class ProjectConfigReachesTheChecksTests(unittest.IsolatedAsyncioTestCase):
    """The chain from `PUT /projects/{id}/verify` to a task actually blocking.

    Every link existed before migration 0004 except the first, so the whole
    chain reported `unavailable` and every test task failed — correct, and
    permanently. This asserts the chain end to end rather than each link.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, project, **over):
        return CheckContext(project=project, mission=None, task=None, store=None,
                            cwd=self.root, **over)

    async def test_a_configured_project_can_answer_tests_pass(self):
        project = Project(slug="x", name="X", root_path=self.root,
                          metadata={"verify": {"tests": "true"}})
        res = await run_checks(("tests_pass",), self._ctx(project))
        self.assertEqual([r.verdict for r in res], ["pass"])
        self.assertTrue(passed(res))

    async def test_a_configured_project_reports_a_real_failure(self):
        project = Project(slug="x", name="X", root_path=self.root,
                          metadata={"verify": {"tests": "sh -c 'echo 2 failed >&2; exit 1'"}})
        res = await run_checks(("tests_pass",), self._ctx(project))
        self.assertEqual(res[0].verdict, "fail")
        self.assertIn("2 failed", res[0].detail,
                      "the detail is what Yuri reads aloud; it must name what failed")
        self.assertFalse(passed(res))

    async def test_an_unconfigured_project_is_unavailable_and_that_fails(self):
        project = Project(slug="x", name="X", root_path=self.root)
        res = await run_checks(("tests_pass",), self._ctx(project))
        self.assertEqual(res[0].verdict, "unavailable")
        self.assertFalse(passed(res), "a check that never ran must never pass a task")

    async def test_the_project_wins_over_a_mission_override_being_absent(self):
        project = Project(slug="x", name="X", root_path=self.root,
                          metadata={"verify": {"tests": "true"}})
        self.assertEqual(verify_config(self._ctx(project)), {"tests": "true"})
