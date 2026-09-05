import io
import os
import re
import socket
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
from fake_opencode import FakeOpenCode  # noqa: E402
from yuri import doctor  # noqa: E402


def _dead_url() -> str:
    """A URL nothing is listening on — a bound-then-closed port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class Doctor(unittest.TestCase):
    def setUp(self):
        # doctor now probes OpenCode. Pin both inputs so no test here depends
        # on the developer's .env or on whether something happens to be
        # listening on port 4096. Individual tests override either.
        for attr, val in (("YURI_AGENTS", "claude-code"),
                          ("OPENCODE_URL", _dead_url())):
            patcher = mock.patch.object(config, attr, val)
            patcher.start()
            self.addCleanup(patcher.stop)

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


class OpenCodeCheck(unittest.TestCase):
    """OpenCode is OPTIONAL. It gets a line either way, but it only gates the
    exit code when YURI_AGENTS actually asks for it — otherwise a user who has
    never installed OpenCode would see `yuri doctor` fail."""

    def _doctor(self, *, agents="claude-code,opencode", url=None,
                which=lambda n: "/usr/bin/" + n, **cfg) -> tuple[int, str]:
        with ExitStack() as stack:
            d = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(mock.patch.object(config, "YURI_HOME",
                                                  os.path.join(d, "Yuri")))
            stack.enter_context(mock.patch.dict(
                os.environ, {"ALLOWED_PROJECT_ROOTS": d, "GEMINI_API_KEY": "x"}))
            stack.enter_context(mock.patch.object(doctor.shutil, "which", which))
            stack.enter_context(mock.patch.object(config, "YURI_AGENTS", agents))
            stack.enter_context(mock.patch.object(config, "OPENCODE_URL",
                                                  url or _dead_url()))
            for attr, val in cfg.items():
                stack.enter_context(mock.patch.object(config, attr, val))
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main([])
            return code, buf.getvalue()

    def _opencode_line(self, out: str) -> str:
        """The opencode CHECK line — matched on the label column, since the
        `agents` line also contains the word."""
        return next(l for l in out.splitlines()
                    if re.match(r"\s+[^\s]\s+opencode\s", l))

    def test_a_reachable_server_is_reported_as_attached(self):
        with FakeOpenCode() as fake:
            code, out = self._doctor(url=fake.url)
        line = self._opencode_line(out)
        self.assertIn("attached", line)
        self.assertIn(fake.url, line)
        self.assertIn("✓", line)
        self.assertEqual(code, 0)

    def test_an_installed_binary_with_nothing_running_is_spawnable_and_passes(self):
        code, out = self._doctor()
        line = self._opencode_line(out)
        self.assertIn("spawnable", line)
        self.assertIn("/usr/bin/opencode", line)
        self.assertEqual(code, 0, "a spawnable OpenCode is not a problem")

    def test_no_binary_and_nothing_running_fails_when_yuri_agents_asks_for_it(self):
        code, out = self._doctor(which=lambda n: None if n == "opencode"
                                 else "/usr/bin/" + n)
        line = self._opencode_line(out)
        self.assertIn("unavailable", line)
        self.assertIn("✗", line)
        self.assertEqual(code, 1)

    def test_the_same_unavailable_opencode_is_informational_when_it_is_not_asked_for(self):
        code, out = self._doctor(agents="claude-code",
                                 which=lambda n: None if n == "opencode"
                                 else "/usr/bin/" + n)
        line = self._opencode_line(out)
        self.assertIn("unavailable", line)
        self.assertIn("YURI_AGENTS", line, "say why it does not count")
        self.assertNotIn("✗", line)
        self.assertEqual(code, 0, "an unconfigured OpenCode must not fail doctor")

    def test_attach_only_with_nothing_running_is_unavailable_not_spawnable(self):
        # OPENCODE_SPAWN=0: the binary being present is irrelevant, because
        # Yuri will never start it.
        code, out = self._doctor(OPENCODE_SPAWN=False)
        line = self._opencode_line(out)
        self.assertIn("unavailable", line)
        self.assertIn("OPENCODE_SPAWN", line)
        self.assertEqual(code, 1)

    def test_the_server_password_is_used_but_never_printed(self):
        """It authenticates the probe (a secured server must read as attached,
        not offline) and appears in no output — not doctor's, not the startup
        summary's, which prints names and sources only."""
        with FakeOpenCode() as fake:
            fake.state.require_password = "hunter2"
            code, out = self._doctor(url=fake.url, OPENCODE_SERVER_PASSWORD="hunter2")
            self.assertIn("attached", self._opencode_line(out),
                          "the password must reach the probe")
            self.assertNotIn("hunter2", out)
            self.assertEqual(code, 0)
            with mock.patch.object(config, "OPENCODE_SERVER_PASSWORD", "hunter2"):
                self.assertNotIn("hunter2", config.summary())

    def test_a_secured_server_probed_without_the_password_is_not_attached(self):
        # Proves the test above is about the password rather than about the
        # fake answering anything at all.
        with FakeOpenCode() as fake:
            fake.state.require_password = "hunter2"
            code, out = self._doctor(url=fake.url, OPENCODE_SERVER_PASSWORD="")
        self.assertNotIn("attached", self._opencode_line(out))


if __name__ == "__main__":
    unittest.main()
