"""Regression tests for the CodeQL-driven security hardening:

- py/polynomial-redos: tmux key names are length-bounded before the chord regex.
- py/path-injection:   session ids / cwds are validated against an allowlist at
                       the sink (session control dir, transcript glob, project root).
- py/full-ssrf:        the realtime-token mint URL is pinned to an allowed host.

    python -m unittest discover -s backend/tests
"""
import logging
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


class OriginAllowlistCSRF(unittest.TestCase):
    """The in-app Origin allowlist must not let a page on some other local port
    drive the loopback-trusted backend when running tokenless (CSRF -> RCE)."""

    def test_tokenless_rejects_other_local_and_lan_origins(self):
        with mock.patch.object(config, "AUTH_TOKEN", ""):
            # Legit frontend origins still work with zero config.
            self.assertTrue(main._origin_permitted("http://localhost:3000"))
            self.assertTrue(main._origin_permitted("http://127.0.0.1:3000"))
            # Any other local port / LAN / internet origin is refused.
            self.assertFalse(main._origin_permitted("http://localhost:8888"))
            self.assertFalse(main._origin_permitted("http://192.168.1.50"))
            self.assertFalse(main._origin_permitted("https://evil.com"))

    def test_token_mode_keeps_lan_convenience(self):
        # With a token set, the token is the boundary; the LAN regex is a
        # convenience for legit same-network devices.
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
            self.assertTrue(main._origin_permitted("http://localhost:3000"))
            self.assertTrue(main._origin_permitted("http://192.168.1.50"))
            self.assertFalse(main._origin_permitted("https://evil.com"))


class AccessLogTokenRedaction(unittest.TestCase):
    def test_token_query_param_redacted_in_access_log(self):
        f = main._RedactTokenFilter()
        rec = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5", "GET", "/sessions/a/terminal?token=SECRET&x=1", "1.1", 101),
            None,
        )
        self.assertTrue(f.filter(rec))
        msg = rec.getMessage()
        self.assertNotIn("SECRET", msg)
        self.assertIn("[REDACTED]", msg)

    def test_non_token_paths_untouched(self):
        f = main._RedactTokenFilter()
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s", ("/health",), None)
        self.assertTrue(f.filter(rec))
        self.assertEqual(rec.getMessage(), "/health")


if __name__ == "__main__":
    unittest.main()
