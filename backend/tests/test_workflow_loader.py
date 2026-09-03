import os, sys, tempfile, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from yuri.workflows.loader import (MAX_TASKS_PER_WORKFLOW, TemplateError,  # noqa: E402
                                   load_templates, parse_templates, render, validate)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.templates = load_templates()

    def test_the_six_shipped_templates_load_and_validate(self):
        self.assertEqual(set(self.templates),
                         {"single", "bug-fix", "feature", "code-review", "refactor", "research"})
        for name, t in self.templates.items():
            validate(t)                       # must not raise
            self.assertTrue(t.description.strip(), name)
            self.assertTrue(t.tasks, name)

    def test_single_is_one_task_so_todays_behaviour_is_a_template(self):
        self.assertEqual(len(self.templates["single"].tasks), 1)

    def test_every_dependency_names_a_task_in_the_same_template(self):
        for name, t in self.templates.items():
            ids = {x.id for x in t.tasks}
            for task in t.tasks:
                for dep in task.depends_on:
                    self.assertIn(dep, ids, f"{name}: {task.id} depends on unknown {dep}")

    def test_read_only_tasks_never_have_write_roles(self):
        for name, t in self.templates.items():
            for task in t.tasks:
                if task.read_only:
                    self.assertIn(task.role, ("researcher", "reviewer", "verifier", None),
                                  f"{name}: {task.id} is read_only but role={task.role}")

    def test_goal_is_substituted_and_nothing_else_is(self):
        t = self.templates["bug-fix"]
        rendered = render(t, "the login form drops the CSRF token")
        joined = " ".join(x.instruction for x in rendered)
        self.assertIn("the login form drops the CSRF token", joined)
        self.assertNotIn("{goal}", joined)
        # No other placeholder syntax is honoured: a template language is a
        # language to maintain.
        self.assertNotIn("{{", joined)

    def test_render_leaves_a_brace_in_the_goal_alone(self):
        rendered = render(self.templates["single"], "fix the {weird} name")
        self.assertIn("{weird}", rendered[0].instruction)


class ValidationTests(unittest.TestCase):
    def _write(self, tmp, body):
        path = os.path.join(tmp, "t.json")
        with open(path, "w") as f:
            f.write(body)
        # parse_templates, not load_templates: these fixtures are
        # deliberately broken, and load_templates validates unconditionally
        # so a caller can tell from the call site that what it returned is
        # safe to run.
        return parse_templates(tmp)["t"]

    def test_a_cycle_is_rejected_at_load_not_at_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._write(tmp, """
            {"name":"t","description":"d","tasks":[
              {"id":"a","role":"developer","depends_on":["b"]},
              {"id":"b","role":"tester","depends_on":["a"]}]}""")
            with self.assertRaises(TemplateError) as ctx:
                validate(t)
            self.assertIn("cycle", str(ctx.exception).lower())

    def test_an_unknown_role_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._write(tmp, """
            {"name":"t","description":"d","tasks":[{"id":"a","role":"wizard"}]}""")
            with self.assertRaises(TemplateError):
                validate(t)

    def test_too_many_tasks_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = ",".join(
                '{"id":"t%d","role":"developer"}' % i for i in range(MAX_TASKS_PER_WORKFLOW + 1))
            t = self._write(tmp, '{"name":"t","description":"d","tasks":[%s]}' % tasks)
            with self.assertRaises(TemplateError):
                validate(t)

    def test_duplicate_task_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._write(tmp, """
            {"name":"t","description":"d","tasks":[
              {"id":"a","role":"developer"},{"id":"a","role":"tester"}]}""")
            with self.assertRaises(TemplateError):
                validate(t)


class UnconditionalValidationTests(unittest.TestCase):
    def test_load_templates_validates_a_custom_directory_too(self):
        # The asymmetry this replaces — validating only the shipped directory —
        # meant a caller could not tell from the call site whether what they
        # loaded was safe to run.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "t.json"), "w") as f:
                f.write(
                    '{"name":"t","description":"d","tasks":['
                '{"id":"a","role":"developer","depends_on":["b"]},'
                '{"id":"b","role":"tester","depends_on":["a"]}]}')
            with self.assertRaises(TemplateError):
                load_templates(tmp)
            parse_templates(tmp)      # the raw path is still available

    def test_an_agent_task_with_no_role_is_rejected_at_authoring_time(self):
        # Task.__post_init__ rejects this too, but only when the engine builds
        # the row — after the user was told their workflow was fine.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "t.json"), "w") as f:
                f.write(
                    '{"name":"t","description":"d","tasks":[{"id":"a","title":"x"}]}')
            with self.assertRaises(TemplateError) as ctx:
                load_templates(tmp)
            self.assertIn("no role", str(ctx.exception))

    def test_a_verification_task_needs_no_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "t.json"), "w") as f:
                f.write(
                    '{"name":"t","description":"d","tasks":['
                '{"id":"a","title":"x","kind":"verification",'
                '"verification":["tests_pass"]}]}')
            self.assertIn("t", load_templates(tmp))
