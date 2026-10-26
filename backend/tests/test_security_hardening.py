"""Regression tests for the CodeQL-driven security hardening:

- py/polynomial-redos: tmux key names are length-bounded before the chord regex.
- py/path-injection:   session ids / cwds are validated against an allowlist at
                       the sink (session control dir, transcript glob, project root).
- py/full-ssrf:        the realtime-token mint URL is pinned to an allowed host.
- origin allowlist:    the broad LAN/any-port Origin regex is applied only in
                       token mode; tokenless (loopback) mode accepts the exact
                       trusted frontend origins only.

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


class OriginAllowlistTokenGating(unittest.TestCase):
    """The broad LAN/any-port Origin regex must only apply when a token is set.

    In tokenless loopback mode the Origin allowlist is the sole browser boundary
    for the command-executing backend (incl. the browser-direct terminal WS), so
    it must not accept every localhost port / private-LAN host — only the exact
    trusted frontend origins in ALLOWED_ORIGINS.
    """

    # Origin env vars that must be cleared so defaults (not a dev's .env) decide.
    _ORIGIN_ENV = ("VC_AUTH_TOKEN", "VC_ALLOWED_ORIGIN_REGEX", "VC_ALLOWED_ORIGINS")

    def _reload_config(self, env):
        patch = mock.patch.dict(os.environ, env, clear=False)
        patch.start()
        for k in self._ORIGIN_ENV:
            if k not in env:
                os.environ.pop(k, None)
        self.addCleanup(patch.stop)
        # Restore the module to its import-time (unpatched) state for later tests.
        self.addCleanup(importlib.reload, config)
        return importlib.reload(config)

    def test_tokenless_rejects_other_localhost_ports_and_lan(self):
        cfg = self._reload_config({})  # no VC_AUTH_TOKEN -> loopback/tokenless
        # Exact trusted frontend origins still work.
        self.assertTrue(cfg.origin_allowed("http://localhost:3000"))
        self.assertTrue(cfg.origin_allowed("http://127.0.0.1:3000"))
        # The vulnerable vectors are now rejected.
        self.assertFalse(cfg.origin_allowed("http://localhost:9999"))
        self.assertFalse(cfg.origin_allowed("http://127.0.0.1:8080"))
        self.assertFalse(cfg.origin_allowed("http://192.168.1.50:8080"))
        self.assertFalse(cfg.origin_allowed("http://10.0.0.5:5173"))

    def test_token_mode_keeps_lan_convenience(self):
        cfg = self._reload_config({"VC_AUTH_TOKEN": "s3cret-test-token"})
        # With a token (the real boundary) the broad LAN regex applies again so
        # same-network phones/laptops can reach the backend.
        self.assertTrue(cfg.origin_allowed("http://192.168.1.50:3000"))
        self.assertTrue(cfg.origin_allowed("https://10.0.0.5:3000"))
        self.assertTrue(cfg.origin_allowed("http://localhost:3000"))

    def test_explicit_regex_override_still_honored(self):
        cfg = self._reload_config({"VC_ALLOWED_ORIGIN_REGEX": r"^https://app\.example\.com$"})
        self.assertTrue(cfg.origin_allowed("https://app.example.com"))
        self.assertFalse(cfg.origin_allowed("http://192.168.1.50:8080"))

    def test_explicit_empty_regex_disables_it(self):
        cfg = self._reload_config(
            {"VC_AUTH_TOKEN": "s3cret-test-token", "VC_ALLOWED_ORIGIN_REGEX": ""})
        # Empty override disables the regex even in token mode; only exact list.
        self.assertFalse(cfg.origin_allowed("http://192.168.1.50:3000"))
        self.assertTrue(cfg.origin_allowed("http://localhost:3000"))


if __name__ == "__main__":
    unittest.main()
