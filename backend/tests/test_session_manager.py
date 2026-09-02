"""Pins what is left in session_manager: the mandatory project sandbox
(`resolve_project_path` / `list_projects`) and the single-provider slot.

The session lookup/naming shims this file used to exercise are gone — their
behavior now lives in `SessionService` and is pinned by test_session_service.py
(handle / prefix / case-insensitive-name resolution, name de-dupe, empty and
duplicate name rejection, the listed shape) and, for name persistence, by
test_claude_provider.py's `test_persist_name_reaches_the_owning_runner`.

    python -m unittest discover -s backend/tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import session_manager as sm  # noqa: E402


class ProviderSlot(unittest.TestCase):
    """`provider()` must fail loudly, never mint a second ClaudeCodeProvider:
    two live TmuxClaudeRunners would compete over the same tmux control dirs
    and both rehydrate the same panes."""

    def setUp(self):
        self._saved = sm._provider
        sm.set_provider(None)

    def tearDown(self):
        sm.set_provider(self._saved)

    def test_no_provider_installed_raises(self):
        with self.assertRaises(RuntimeError) as cm:
            sm.provider()
        self.assertIn("no provider installed", str(cm.exception))

    def test_installed_provider_is_returned_and_reset_clears_it(self):
        from yuri.providers.claude_code import ClaudeCodeProvider
        p = ClaudeCodeProvider(runner_factory=lambda b: None)
        sm.set_provider(p)
        self.assertIs(sm.provider(), p)
        sm.reset()
        with self.assertRaises(RuntimeError):
            sm.provider()


class ResolveProjectPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "Alpha"))
        os.mkdir(os.path.join(self.root, "beta"))
        os.mkdir(self.root + "-evil")
        self._env = mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root})
        self._env.start()
        # config.allowed_project_roots() also appends Yuri's home once it exists
        # on disk (independent of ALLOWED_PROJECT_ROOTS) — point it at a path
        # inside this test's own tempdir that is never created, so these tests
        # see exactly the roots they set up, regardless of whether the real
        # ~/Yuri exists on the machine running them.
        self._home = mock.patch.object(config, "YURI_HOME", os.path.join(self.root, "Yuri"))
        self._home.start()

    def tearDown(self):
        self._home.stop()
        self._env.stop()
        import shutil
        shutil.rmtree(self.root + "-evil", ignore_errors=True)
        self.tmp.cleanup()

    def test_empty_defaults_to_first_root(self):
        self.assertEqual(sm.resolve_project_path(""), self.root)
        self.assertEqual(sm.resolve_project_path("anywhere"), self.root)

    def test_absolute_contained(self):
        self.assertEqual(sm.resolve_project_path(os.path.join(self.root, "Alpha")),
                         os.path.join(self.root, "Alpha"))

    def test_fuzzy_name_case_insensitive(self):
        self.assertEqual(sm.resolve_project_path("alpha"), os.path.join(self.root, "alpha"))
        self.assertEqual(sm.resolve_project_path("BETA"), os.path.join(self.root, "BETA"))

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            sm.resolve_project_path(os.path.join(self.root, "..", ".."))

    def test_sibling_root_rejected(self):
        with self.assertRaises(ValueError):
            sm.resolve_project_path(self.root + "-evil")

    def test_outside_rejected(self):
        with self.assertRaises(ValueError):
            sm.resolve_project_path("/etc")

    def test_symlinked_root_resolves(self):
        link = self.root + "-link"
        os.symlink(self.root, link)
        try:
            with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": link}):
                self.assertEqual(sm.resolve_project_path("alpha"),
                                 os.path.join(self.root, "alpha"))
        finally:
            os.unlink(link)

    def test_fails_closed_without_roots(self):
        with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": ""}):
            with self.assertRaises(ValueError):
                sm.resolve_project_path("alpha")

    def test_list_projects_skips_hidden(self):
        os.mkdir(os.path.join(self.root, ".hidden"))
        names = [p["name"] for p in sm.list_projects()["projects"]]
        self.assertEqual(names, ["Alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
