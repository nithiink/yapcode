import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.specialist import (BUILTINS, ROLES, TASK_CAPABILITIES,  # noqa: E402
                                    Specialist, slugify)


class SlugTests(unittest.TestCase):
    def test_slug_is_provider_safe(self):
        # It becomes `--agent <slug>` on a command line and `<slug>.md` in
        # OpenCode's config directory, so anything needing shell quoting or
        # capable of walking a path must not survive.
        self.assertEqual(slugify("Code Reviewer"), "code-reviewer")
        self.assertEqual(slugify("  Deep   Research  "), "deep-research")
        self.assertEqual(slugify("Ops/Deploy!"), "ops-deploy")
        self.assertEqual(slugify("Tester (fast)"), "tester-fast")

    def test_slug_never_empty_and_never_path_traversal(self):
        for name in ["", "   ", "!!!", "..", "../../etc"]:
            slug = slugify(name)
            self.assertTrue(slug, f"{name!r} produced an empty slug")
            self.assertNotIn("/", slug)
            self.assertNotIn("..", slug)


class SpecialistTests(unittest.TestCase):
    def test_slug_is_derived_once_then_frozen(self):
        s = Specialist(name="Reviewer", role="reviewer", provider_id="claude-code")
        self.assertEqual(s.slug, "reviewer")
        s.name = "Senior Reviewer"
        self.assertEqual(s.slug, "reviewer",
                         "renaming moved the slug; a live session still holds the old one")

    def test_capabilities_and_tools_are_tuples_after_a_round_trip(self):
        # sqlite's _to_row does json.dumps(v, default=str): a set would be
        # stored as "frozenset({...})" with no error at all.
        s = Specialist.from_dict({
            "name": "R", "role": "reviewer", "provider_id": "claude-code",
            "capabilities": ["code_review", "git"], "tools": ["Read", "Grep"]})
        self.assertIsInstance(s.capabilities, tuple)
        self.assertIsInstance(s.tools, tuple)
        self.assertEqual(s.capabilities, ("code_review", "git"))

    def test_rejects_an_unknown_role_and_an_unknown_capability(self):
        with self.assertRaises(ValueError):
            Specialist(name="X", role="wizard", provider_id="claude-code")
        with self.assertRaises(ValueError):
            Specialist(name="X", role="reviewer", provider_id="claude-code",
                       capabilities=("telepathy",))

    def test_every_role_has_exactly_one_builtin(self):
        roles = [b["role"] for b in BUILTINS]
        self.assertEqual(sorted(roles), sorted(ROLES))
        self.assertEqual(len(roles), len(set(roles)))

    def test_builtins_declare_a_prompt_and_capabilities(self):
        for b in BUILTINS:
            self.assertTrue(b["system_prompt"].strip(), b["name"])
            self.assertTrue(b["capabilities"], b["name"])
            for cap in b["capabilities"]:
                self.assertIn(cap, TASK_CAPABILITIES)

    def test_every_builtin_actually_constructs(self):
        # BUILTINS is a tuple of kwargs dicts. A typo'd key would otherwise
        # only surface when RosterService.seed() ran.
        for b in BUILTINS:
            self.assertIsInstance(Specialist(builtin=True, **b), Specialist)

    def test_read_only_roles_get_no_write_tools(self):
        # A "researcher" that can edit files is not a researcher. This is the
        # only guard against a builtin quietly having write access.
        for b in BUILTINS:
            if b["role"] in ("researcher", "reviewer"):
                self.assertNotIn("Edit", b["tools"], b["name"])
                self.assertNotIn("Write", b["tools"], b["name"])
