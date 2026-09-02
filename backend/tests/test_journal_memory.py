import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.home import Home  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.memory import BadSlug, Memory  # noqa: E402


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.j = Journal(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_creates_dated_file_with_header(self):
        path = self.j.append("mission created: Fix it")
        today = datetime.date.today().isoformat()
        self.assertEqual(os.path.basename(path), f"{today}.md")
        with open(path) as f:
            text = f.read()
        self.assertTrue(text.startswith(f"# {today}\n"))
        self.assertRegex(text, r"\n- \d\d:\d\d  mission created: Fix it\n")
        self.j.append("second")
        self.assertIn("second", self.j.read_today())

    def test_read_today_caps_and_handles_missing(self):
        self.assertEqual(self.j.read_today(), "")
        self.j.append("x" * 5000)
        self.assertLessEqual(len(self.j.read_today(cap=100)), 100)

    def test_read_today_zero_and_negative_cap_return_empty(self):
        self.j.append("hello world")
        self.assertEqual(self.j.read_today(cap=0), "")
        self.assertEqual(self.j.read_today(cap=-1), "")

    def test_newlines_in_line_are_flattened(self):
        self.j.append("a\nb")
        self.assertIn("- ", self.j.read_today())
        self.assertNotIn("a\nb", self.j.read_today())

    def test_append_creates_journal_dir_if_missing(self):
        tmp2 = tempfile.TemporaryDirectory()
        try:
            home2 = Home(os.path.join(tmp2.name, "Yuri"))  # never ensure()d
            j2 = Journal(home2)
            path = j2.append("first ever line")
            self.assertTrue(os.path.exists(path))
        finally:
            tmp2.cleanup()


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.m = Memory(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remember_user(self):
        path = self.m.remember("prefers pnpm over npm")
        self.assertEqual(path, self.home.user_memory_path)
        text = self.m.read_user()
        self.assertRegex(text, r"- \d{4}-\d\d-\d\d  prefers pnpm over npm")

    def test_remember_project(self):
        path = self.m.remember("tests live in backend/tests", project_slug="yuri-code")
        self.assertEqual(path, os.path.join(self.home.projects_memory_dir, "yuri-code.md"))
        self.assertIn("tests live in", self.m.read_project("yuri-code"))

    def test_bad_slug_rejected(self):
        for bad in ["../etc", "a/b", "UPPER", "", "x" * 65, "sp ace"]:
            with self.assertRaises(BadSlug):
                self.m.remember("x", project_slug=bad)

    def test_empty_fact_rejected(self):
        with self.assertRaises(ValueError):
            self.m.remember("   ")

    def test_read_user_cap_keeps_tail(self):
        for i in range(200):
            self.m.remember(f"fact {i}")
        out = self.m.read_user(cap=300)
        self.assertLessEqual(len(out), 300)
        self.assertIn("fact 199", out)  # most recent survives the cap

    def test_project_header_written_once(self):
        self.m.remember("first fact", project_slug="proj")
        self.m.remember("second fact", project_slug="proj")
        text = self.m.read_project("proj")
        self.assertEqual(text.count("# Project notes: proj"), 1)

    def test_read_project_never_written_returns_empty(self):
        self.assertEqual(self.m.read_project("never-seen"), "")

    def test_read_user_zero_and_negative_cap_return_empty(self):
        self.m.remember("prefers pnpm over npm")
        self.assertEqual(self.m.read_user(cap=0), "")
        self.assertEqual(self.m.read_user(cap=-1), "")

    def test_read_project_zero_and_negative_cap_return_empty(self):
        self.m.remember("tests live in backend/tests", project_slug="yuri-code")
        self.assertEqual(self.m.read_project("yuri-code", cap=0), "")
        self.assertEqual(self.m.read_project("yuri-code", cap=-1), "")

    def test_max_length_slug_accepted(self):
        slug = "a" * 64
        path = self.m.remember("ok", project_slug=slug)
        self.assertEqual(path, os.path.join(self.home.projects_memory_dir, f"{slug}.md"))

    def test_slug_with_null_byte_rejected(self):
        with self.assertRaises(BadSlug):
            self.m.remember("x", project_slug="a\x00b")

    def test_slug_dot_and_dotdot_rejected(self):
        for bad in [".", "..", "%2e%2e", "%2e%2e%2fetc"]:
            with self.assertRaises(BadSlug):
                self.m.remember("x", project_slug=bad)

    def test_fact_containing_path_traversal_is_just_content(self):
        path = self.m.remember("../../etc/passwd is not a path here")
        self.assertEqual(path, self.home.user_memory_path)
        real_memory_dir = os.path.realpath(self.home.memory_dir)
        self.assertTrue(os.path.realpath(path).startswith(real_memory_dir + os.sep))

    def test_remember_does_not_write_outside_memory_dir(self):
        before = set(os.listdir(self.tmp.name))
        try:
            self.m.remember("x", project_slug="..")
        except BadSlug:
            pass
        after = set(os.listdir(self.tmp.name))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
