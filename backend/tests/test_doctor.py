import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri import doctor  # noqa: E402


class Doctor(unittest.TestCase):
    def test_reports_each_check_and_exit_code(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")), \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d, "GEMINI_API_KEY": "x"}), \
             mock.patch.object(doctor.shutil, "which", lambda n: None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main([])
            out = buf.getvalue()
            for label in ["home", "database", "allowed roots", "claude", "tmux", "voice keys"]:
                self.assertIn(label, out, label)
            self.assertEqual(code, 1)  # claude/tmux missing → non-zero
            self.assertTrue(os.path.isfile(os.path.join(d, "Yuri", "yuri.db")))

    def test_ok_when_tools_present(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")), \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d, "GEMINI_API_KEY": "x"}), \
             mock.patch.object(doctor.shutil, "which", lambda n: "/usr/bin/" + n):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(doctor.main([]), 0)

    def test_missing_allowed_roots_is_a_problem_even_though_home_is_reachable(self):
        # config.allowed_project_roots() always appends Yuri's own home once it
        # exists (home.ensure() runs earlier in main()), so a naive "roots is
        # non-empty" check can never fail. With no *project* root configured,
        # doctor must still flag it — only Yuri's own home would be reachable.
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")), \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": "", "GEMINI_API_KEY": "x"}), \
             mock.patch.object(doctor.shutil, "which", lambda n: "/usr/bin/" + n):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main([])
            line = next(l for l in buf.getvalue().splitlines() if "allowed roots" in l)
            self.assertIn("✗", line)
            self.assertEqual(code, 1)

    def test_allowed_roots_ok_when_a_real_project_root_is_configured(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")), \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d, "GEMINI_API_KEY": "x"}), \
             mock.patch.object(doctor.shutil, "which", lambda n: "/usr/bin/" + n):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main([])
            line = next(l for l in buf.getvalue().splitlines() if "allowed roots" in l)
            self.assertIn("✓", line)
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
