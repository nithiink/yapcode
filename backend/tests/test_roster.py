import os, sys, tempfile, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from yuri.domain.specialist import BUILTINS, ROLES, Specialist  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.roster import (DuplicateSpecialist, NoSpecialist,  # noqa: E402
                                  RosterService, SpecialistInUse)
from yuri.store.sqlite import SqliteStore  # noqa: E402


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.registry = AgentRegistry()
        self.fake = FakeAgentProvider()
        self.registry.register(self.fake)
        self.svc = RosterService(self.store, self.bus, self.registry)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_seed_is_idempotent(self):
        self.assertEqual(self.svc.seed(), len(BUILTINS))
        self.assertEqual(self.svc.seed(), 0, "seeding twice duplicated the builtins")
        self.assertEqual(len(self.svc.list()), len(BUILTINS))

    def test_seed_falls_back_to_a_configured_provider(self):
        # BUILTINS name claude-code and opencode; only `fake` is registered
        # here. A roster pointing at a provider the user does not run is a
        # roster of broken buttons, so every seeded specialist must be runnable.
        self.svc.seed()
        for s in self.svc.list():
            self.assertIn(s.provider_id, self.registry.ids(), s.name)

    def test_names_are_unique_among_the_live(self):
        self.svc.create(name="Auditor", role="reviewer", provider_id="fake")
        with self.assertRaises(DuplicateSpecialist):
            self.svc.create(name="Auditor", role="reviewer", provider_id="fake")

    def test_create_rejects_an_unregistered_provider_with_an_actionable_message(self):
        with self.assertRaises(ValueError) as ctx:
            self.svc.create(name="Ghost", role="tester", provider_id="hermes")
        self.assertIn("hermes", str(ctx.exception))
        self.assertIn("fake", str(ctx.exception), "the message should name what IS available")

    def test_rename_keeps_the_slug(self):
        s = self.svc.create(name="Auditor", role="reviewer", provider_id="fake")
        renamed = self.svc.update(s.id, name="Chief Auditor")
        self.assertEqual(renamed.slug, s.slug,
                         "the slug moved; a live session launched with the old one")

    def test_a_builtin_cannot_be_archived_but_can_be_edited(self):
        self.svc.seed()
        builtin = self.svc.by_name("Reviewer")
        self.svc.update(builtin.id, system_prompt="Be terse.")
        self.assertEqual(self.svc.by_name("Reviewer").system_prompt, "Be terse.")
        with self.assertRaises(SpecialistInUse):
            self.svc.archive(builtin.id)

    def test_archive_refuses_while_a_live_task_holds_it(self):
        from yuri.domain.mission import Mission
        from yuri.domain.project import Project
        from yuri.domain.task import Task
        from yuri.domain.workflow import Workflow
        p = Project(slug="p", name="P", root_path="/tmp/p")
        self.store.projects.insert(p)
        m = Mission(title="m", project_id=p.id)
        self.store.missions.insert(m)
        w = Workflow(mission_id=m.id, status="running")
        self.store.workflows.insert(w)
        s = self.svc.create(name="Auditor", role="reviewer", provider_id="fake")
        t = Task(workflow_id=w.id, ordinal=0, title="t", specialist_id=s.id, status="running")
        self.store.tasks.insert(t)
        with self.assertRaises(SpecialistInUse):
            self.svc.archive(s.id)

    def test_candidates_prefers_an_exact_role_then_capabilities(self):
        exact = self.svc.create(name="R1", role="reviewer", provider_id="fake",
                                capabilities=("code_review", "git"))
        self.svc.create(name="D1", role="developer", provider_id="fake",
                        capabilities=("code_review", "git", "coding"))
        got = self.svc.candidates("reviewer", frozenset({"code_review"}))
        self.assertEqual(got[0].id, exact.id)

    def test_candidates_excludes_anyone_missing_a_required_capability(self):
        self.svc.create(name="R1", role="reviewer", provider_id="fake",
                        capabilities=("code_review",))
        self.assertEqual(self.svc.candidates("reviewer", frozenset({"browser"})), [])

    def test_candidates_returns_empty_rather_than_raising(self):
        # An empty list is a real answer the caller has to say out loud.
        self.assertEqual(self.svc.candidates("verifier"), [])

    def test_resolve_raises_with_a_message_that_says_what_to_do(self):
        with self.assertRaises(NoSpecialist) as ctx:
            self.svc.resolve("verifier", frozenset({"testing"}))
        msg = str(ctx.exception)
        self.assertIn("verifier", msg)
        self.assertIn("testing", msg)

    def test_resolve_honours_a_pin_over_the_ranking(self):
        self.svc.create(name="R1", role="reviewer", provider_id="fake",
                        capabilities=("code_review",))
        pinned = self.svc.create(name="R2", role="reviewer", provider_id="fake",
                                 capabilities=("code_review",))
        self.assertEqual(self.svc.resolve("reviewer", pinned=pinned.id).id, pinned.id)

    def test_resolve_refuses_a_pin_that_cannot_do_the_work(self):
        # Silently substituting someone else would run the task on an agent
        # the user did not choose.
        pinned = self.svc.create(name="R2", role="reviewer", provider_id="fake")
        with self.assertRaises(NoSpecialist):
            self.svc.resolve("reviewer", frozenset({"browser"}), pinned=pinned.id)

    def test_resolve_refuses_an_archived_pin(self):
        s = self.svc.create(name="Old", role="tester", provider_id="fake")
        s.archived = True
        self.store.specialists.update(s)
        with self.assertRaises(NoSpecialist):
            self.svc.resolve("tester", pinned=s.id)


if __name__ == "__main__":
    unittest.main()


class CandidateOrderingTests(RosterTests):
    """Spec §7.32: a role has a preferred provider, and it orders without
    excluding."""

    def _fake_as(self, agent_id):
        """FakeAgentProvider hardcodes id = "fake"; the registry keys on it,
        so a second selectable provider needs a distinct id."""
        p = FakeAgentProvider()
        p.id = agent_id
        self.registry.register(p)
        return agent_id

    def test_the_role_s_preferred_provider_ranks_first(self):
        from yuri.domain.specialist import ROLE_PREFERENCE
        pref = ROLE_PREFERENCE["reviewer"]
        self._fake_as("other")
        self._fake_as(pref)
        wrong = self.svc.create(name="R-other", role="reviewer", provider_id="other",
                                capabilities=("code_review",))
        right = self.svc.create(name="R-pref", role="reviewer", provider_id=pref,
                                capabilities=("code_review",))
        got = [s.id for s in self.svc.candidates("reviewer", frozenset({"code_review"}))]
        self.assertEqual(got[0], right.id,
                         "the role's preferred provider did not rank first")
        self.assertIn(wrong.id, got, "preference must order, never exclude")

    def test_a_store_failure_is_not_reported_as_nobody_available(self):
        # Swallowing it would make resolve() tell the user to create a
        # specialist when the database is actually unreadable — the same
        # failure as a view rendering a failed fetch as an empty list.
        class Boom:
            def list(self, include_archived=False):
                raise RuntimeError("database is locked")
        self.store.specialists = Boom()
        with self.assertRaises(RuntimeError):
            self.svc.candidates("reviewer")
