"""Pins the permission policy the voice approval flow depends on (Phase 1
safety net — see docs/superpowers/plans/2026-09-02-yuri-foundation.md).

    python -m unittest discover -s backend/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import permissions  # noqa: E402
from claude_runner import decide_permission  # noqa: E402


class Classify(unittest.TestCase):
    def test_read_only_tools_are_safe(self):
        for t in ["Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "ToolSearch"]:
            self.assertEqual(permissions.classify(t), "safe", t)

    def test_question_tool(self):
        self.assertEqual(permissions.classify("AskUserQuestion"), "question")

    def test_everything_else_is_risky(self):
        for t in ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "mcp__foo__bar", ""]:
            self.assertEqual(permissions.classify(t), "risky", t)

    def test_chrome_mcp_prefix_is_safe(self):
        self.assertEqual(permissions.classify("mcp__claude-in-chrome__navigate"), "safe")


class ModeCovers(unittest.TestCase):
    def test_auto_covers_all(self):
        self.assertTrue(permissions.mode_covers("auto", "Bash"))

    def test_accept_edits_covers_only_edit_tools(self):
        self.assertTrue(permissions.mode_covers("acceptEdits", "Edit"))
        self.assertFalse(permissions.mode_covers("acceptEdits", "Bash"))

    def test_default_and_plan_cover_nothing(self):
        self.assertFalse(permissions.mode_covers("default", "Edit"))
        self.assertFalse(permissions.mode_covers("plan", "Edit"))


class PlanFileWrite(unittest.TestCase):
    def test_write_inside_plans_dir(self):
        fp = os.path.join(permissions._PLANS_DIR, "x.md")
        self.assertTrue(permissions.is_plan_file_write("Write", {"file_path": fp}))

    def test_write_elsewhere(self):
        self.assertFalse(permissions.is_plan_file_write("Write", {"file_path": "/tmp/x.md"}))

    def test_non_edit_tool(self):
        fp = os.path.join(permissions._PLANS_DIR, "x.md")
        self.assertFalse(permissions.is_plan_file_write("Bash", {"file_path": fp}))

    def test_traversal_out_of_plans_dir(self):
        fp = os.path.join(permissions._PLANS_DIR, "..", "settings.json")
        self.assertFalse(permissions.is_plan_file_write("Write", {"file_path": fp}))


class DecidePermission(unittest.TestCase):
    def test_allow_words(self):
        for c in ["yes", "y", "allow", "Yes, go ahead", "approve", "ok", "sure"]:
            self.assertEqual(decide_permission(c), "allow", c)

    def test_deny_words_win(self):
        for c in ["no", "deny", "yes but don't", "stop", "nope"]:
            self.assertEqual(decide_permission(c), "deny", c)

    def test_ambiguous_is_none(self):
        for c in ["", "maybe", "your call"]:
            self.assertIsNone(decide_permission(c), c)


if __name__ == "__main__":
    unittest.main()
