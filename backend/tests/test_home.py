import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri.home import Home  # noqa: E402


class HomeLayout(unittest.TestCase):
    def test_ensure_creates_layout_idempotently(self):
        with tempfile.TemporaryDirectory() as d:
            h = Home(os.path.join(d, "Yuri")).ensure()
            for p in [h.memory_dir, h.projects_memory_dir, h.journal_dir, h.workspace_dir]:
                self.assertTrue(os.path.isdir(p), p)
            self.assertTrue(os.path.isfile(h.user_memory_path))
            with open(h.user_memory_path) as f:
                first = f.read()
            self.assertIn("# What Yuri knows about you", first)
            mode = stat.S_IMODE(os.stat(h.path).st_mode)
            self.assertEqual(mode, 0o700)
            # second call must not clobber the memory file
            with open(h.user_memory_path, "a") as f:
                f.write("- keep me\n")
            Home(h.path).ensure()
            with open(h.user_memory_path) as f:
                self.assertIn("keep me", f.read())
            self.assertEqual(h.db_path, os.path.join(h.path, "yuri.db"))

    def test_home_joins_allowed_roots_only_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "Yuri")
            with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d}), \
                 mock.patch.object(config, "YURI_HOME", home):
                self.assertNotIn(os.path.realpath(home), config.allowed_project_roots())
                Home(home).ensure()
                self.assertIn(os.path.realpath(home), config.allowed_project_roots())


if __name__ == "__main__":
    unittest.main()
