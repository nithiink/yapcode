"""Regression tests for the CodeQL-driven security hardening:

- py/polynomial-redos: tmux key names are length-bounded before the chord regex.
- py/path-injection:   session ids / cwds are validated against an allowlist at
                       the sink (session control dir, transcript glob, project root).
- py/full-ssrf:        the realtime-token mint URL is pinned to an allowed host.

    python -m unittest discover -s backend/tests
"""
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import main  # noqa: E402
import tmux_runner  # noqa: E402
import transcript  # noqa: E402


class NormalizeKeyReDoS(unittest.TestCase):
    def test_overlong_key_is_rejected_before_regex(self):
        with self.assertRaises(ValueError):
            tmux_runner._normalize_key("C-" + "-C" * 200)

    def test_normal_keys_still_pass(self):
        self.assertEqual(tmux_runner._normalize_key("C-c"), "C-c")
        self.assertEqual(tmux_runner._normalize_key("a"), "a")


class SessionIdValidation(unittest.TestCase):
    def test_traversal_rejected(self):
        for bad in ["../../etc", "a/b", "..", "with space", "*", ""]:
            with self.assertRaises(ValueError):
                tmux_runner.validate_session_id(bad)

    def test_uuid_like_accepted(self):
        sid = "0b9f1c2d-3e4f-5a6b-7c8d-9e0f1a2b3c4d"
        self.assertEqual(tmux_runner.validate_session_id(sid), sid)

    def test_tmuxsession_rejects_unsafe_handle(self):
        with self.assertRaises(ValueError):
            tmux_runner._TmuxSession("../escape", cwd="/tmp", model="opus")


class ResolveWithinRoots(unittest.TestCase):
    def test_contained_dir_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "proj")
            os.mkdir(sub)
            with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": root}):
                self.assertEqual(config.resolve_within_roots(sub), os.path.realpath(sub))

    def test_outside_root_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": root}):
                with self.assertRaises(ValueError):
                    config.resolve_within_roots("/etc")

    def test_traversal_escape_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": root}):
                with self.assertRaises(ValueError):
                    config.resolve_within_roots(os.path.join(root, "..", "..", "etc"))

    def test_fails_closed_without_roots(self):
        with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": ""}):
            with self.assertRaises(ValueError):
                config.resolve_within_roots("/tmp")


class ReadTimelineHandleGuard(unittest.TestCase):
    def test_invalid_handle_returns_not_found_without_globbing(self):
        with mock.patch.object(transcript, "_find") as find:
            out = transcript.read_timeline("../../*")
            self.assertEqual(out, {"found": False, "events": []})
            find.assert_not_called()


class MintHostAllowlist(unittest.TestCase):
    def test_openai_host_allowed(self):
        main._assert_allowed_mint_host("https://api.openai.com/v1/realtime/client_secrets")

    def test_arbitrary_host_rejected(self):
        with self.assertRaises(main.HTTPException):
            main._assert_allowed_mint_host("https://evil.example.com/steal")

    def test_non_https_rejected(self):
        with self.assertRaises(main.HTTPException):
            main._assert_allowed_mint_host("http://api.openai.com/v1/realtime/client_secrets")

    def test_configured_azure_host_allowed(self):
        with mock.patch.object(main, "AZURE_ENDPOINT", "https://my-azure.openai.azure.com"):
            main._assert_allowed_mint_host(
                "https://my-azure.openai.azure.com/openai/v1/realtime/client_secrets")


class OriginAllowlistGating(unittest.TestCase):
    """The broad localhost-any-port / RFC1918 Origin regex must apply ONLY when a
    token is configured. In the default tokenless loopback mode the Origin
    allowlist is the sole cross-origin gate for the command-executing backend, so
    it must fall back to the exact frontend origins — otherwise a hostile page on
    another localhost port or a LAN origin could drive the backend cross-origin."""

    def tearDown(self):
        # Restore config to the ambient environment for the other test classes.
        importlib.reload(config)

    def _reload(self, token=None, regex=None):
        env = dict(os.environ)
        env.pop("VC_AUTH_TOKEN", None)
        env.pop("VC_ALLOWED_ORIGIN_REGEX", None)
        if token is not None:
            env["VC_AUTH_TOKEN"] = token
        if regex is not None:
            env["VC_ALLOWED_ORIGIN_REGEX"] = regex
        # clear=True so the popped vars are genuinely absent during the reload.
        with mock.patch.dict(os.environ, env, clear=True):
            importlib.reload(config)
            return config

    def test_tokenless_mode_allows_only_exact_frontend_origins(self):
        c = self._reload()  # default: no token, no explicit regex
        self.assertIsNone(c.ALLOWED_ORIGIN_REGEX)
        self.assertTrue(c.origin_allowed("http://localhost:3000"))
        self.assertTrue(c.origin_allowed("http://127.0.0.1:3000"))
        for bad in ("http://localhost:9999", "http://10.0.0.5",
                    "http://192.168.1.10:8080", "http://172.16.0.1"):
            self.assertFalse(c.origin_allowed(bad), bad)

    def test_network_mode_allows_broad_lan_origins(self):
        c = self._reload(token="secret")  # VC_AUTH_TOKEN set -> broad regex applies
        self.assertTrue(c.origin_allowed("http://192.168.1.10:8080"))
        self.assertTrue(c.origin_allowed("http://localhost:9999"))
        self.assertTrue(c.origin_allowed("http://localhost:3000"))

    def test_explicit_regex_override_honored_without_token(self):
        c = self._reload(regex=r"^https://app\.example\.com$")
        self.assertTrue(c.origin_allowed("https://app.example.com"))
        self.assertFalse(c.origin_allowed("http://localhost:9999"))

    def test_empty_explicit_regex_disables_regex(self):
        c = self._reload(regex="")
        self.assertIsNone(c.ALLOWED_ORIGIN_REGEX)
        self.assertTrue(c.origin_allowed("http://localhost:3000"))  # exact list only
        self.assertFalse(c.origin_allowed("http://10.0.0.5"))


if __name__ == "__main__":
    unittest.main()
