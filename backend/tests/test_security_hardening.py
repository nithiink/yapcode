"""Regression tests for the CodeQL-driven security hardening:

- py/polynomial-redos: tmux key names are length-bounded before the chord regex.
- py/path-injection:   session ids / cwds are validated against an allowlist at
                       the sink (session control dir, transcript glob, project root).
- py/full-ssrf:        the realtime-token mint URL is pinned to an allowed host.
- origin-allowlist:    the broad private-LAN Origin regex is applied only in
                       token (network) mode; token-less loopback mode trusts
                       only the exact configured frontend origins.

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
    """The broad private-LAN origin regex widens the trust boundary only when a
    token guards access. Token-less loopback mode must trust ONLY the exact
    frontend origins, or any other localhost port / LAN device page the user has
    open could drive the command-executing backend cross-origin."""

    def _reload(self, env):
        # env keys not present are removed so defaults apply. patch.dict restores
        # the full environment on exit; a final reload in tearDown restores config.
        with mock.patch.dict(os.environ, env, clear=False):
            for k in ("VC_AUTH_TOKEN", "VC_ALLOWED_ORIGIN_REGEX"):
                if k not in env:
                    os.environ.pop(k, None)
            return importlib.reload(config)

    def tearDown(self):
        os.environ.pop("VC_AUTH_TOKEN", None)
        os.environ.pop("VC_ALLOWED_ORIGIN_REGEX", None)
        importlib.reload(config)

    def test_tokenless_mode_trusts_only_exact_frontend_origins(self):
        cfg = self._reload({})  # no VC_AUTH_TOKEN -> token-less loopback mode
        self.assertIsNone(cfg.ALLOWED_ORIGIN_REGEX)
        self.assertTrue(cfg.origin_allowed("http://localhost:3000"))
        # Other localhost ports and LAN hosts are no longer trusted.
        self.assertFalse(cfg.origin_allowed("http://localhost:5173"))
        self.assertFalse(cfg.origin_allowed("http://192.168.1.50"))

    def test_token_mode_allows_lan_convenience_regex(self):
        cfg = self._reload({"VC_AUTH_TOKEN": "secret"})
        self.assertIsNotNone(cfg.ALLOWED_ORIGIN_REGEX)
        self.assertTrue(cfg.origin_allowed("http://192.168.1.50"))
        self.assertTrue(cfg.origin_allowed("http://localhost:5173"))
        # A plain public site is still rejected in either mode.
        self.assertFalse(cfg.origin_allowed("http://evil.com"))

    def test_explicit_regex_override_still_honored(self):
        cfg = self._reload({"VC_ALLOWED_ORIGIN_REGEX": r"^https://app\.example\.com$"})
        self.assertTrue(cfg.origin_allowed("https://app.example.com"))
        self.assertFalse(cfg.origin_allowed("http://localhost:5173"))

    def test_explicit_empty_regex_disables(self):
        cfg = self._reload({"VC_AUTH_TOKEN": "secret", "VC_ALLOWED_ORIGIN_REGEX": ""})
        self.assertIsNone(cfg.ALLOWED_ORIGIN_REGEX)
        self.assertFalse(cfg.origin_allowed("http://192.168.1.50"))


if __name__ == "__main__":
    unittest.main()
