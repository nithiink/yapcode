"""Regression tests for the CodeQL-driven security hardening:

- py/polynomial-redos: tmux key names are length-bounded before the chord regex.
- py/path-injection:   session ids / cwds are validated against an allowlist at
                       the sink (session control dir, transcript glob, project root).
- py/full-ssrf:        the realtime-token mint URL is pinned to an allowed host.

    python -m unittest discover -s backend/tests
"""
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


class OriginAllowlistTokenGating(unittest.TestCase):
    """The broad any-localhost-port / private-LAN Origin regex must only be
    trusted in network mode (VC_AUTH_TOKEN set). In the default tokenless loopback
    mode the Origin allowlist is the sole CSRF boundary, so only the exact
    first-party frontend origins may pass."""

    def test_exact_frontend_origin_allowed_tokenless(self):
        with mock.patch.object(config, "AUTH_TOKEN", ""):
            self.assertTrue(config.origin_allowed("http://localhost:3000"))

    def test_other_localhost_port_rejected_tokenless(self):
        # A second local dev server (e.g. VS Code Live Server) must NOT be trusted
        # when no token is configured.
        with mock.patch.object(config, "AUTH_TOKEN", ""):
            self.assertFalse(config.origin_allowed("http://localhost:5500"))

    def test_lan_origin_rejected_tokenless(self):
        with mock.patch.object(config, "AUTH_TOKEN", ""):
            self.assertFalse(config.origin_allowed("http://192.168.1.50:8080"))

    def test_lan_origin_allowed_when_token_set(self):
        # With a token required, the LAN regex is a convenience and the token is
        # the real boundary — so a matching LAN origin passes the Origin check.
        with mock.patch.object(config, "AUTH_TOKEN", "sekret"):
            self.assertTrue(config.origin_allowed("http://192.168.1.50:8080"))

    def test_cors_regex_disabled_tokenless_enabled_with_token(self):
        with mock.patch.object(config, "AUTH_TOKEN", ""):
            self.assertIsNone(config.cors_allow_origin_regex())
        with mock.patch.object(config, "AUTH_TOKEN", "sekret"):
            self.assertEqual(config.cors_allow_origin_regex(), config.ALLOWED_ORIGIN_REGEX)


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


if __name__ == "__main__":
    unittest.main()
