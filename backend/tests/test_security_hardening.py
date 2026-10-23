"""Regression tests for the CodeQL-driven security hardening:

- py/polynomial-redos: tmux key names are length-bounded before the chord regex.
- py/path-injection:   session ids / cwds are validated against an allowlist at
                       the sink (session control dir, transcript glob, project root).
- py/full-ssrf:        the realtime-token mint URL is pinned to an allowed host.
- dns-rebinding:        the HTTP Host header is validated against a local/LAN
                        allowlist so a rebound browser request can't be laundered
                        into a trusted loopback call (tokenless mode).

    python -m unittest discover -s backend/tests
"""
import asyncio
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


class HostAllowlist(unittest.TestCase):
    def test_local_and_lan_hosts_allowed(self):
        for h in ["localhost:3000", "127.0.0.1:8000", "localhost",
                  "[::1]:8000", "192.168.1.5:3000", "10.0.0.2:8000",
                  "172.16.0.9:3000"]:
            self.assertTrue(config.host_allowed(h), h)

    def test_foreign_or_missing_host_rejected(self):
        for h in ["evil.com:3000", "evil.com", "attacker.example:8000",
                  "169.254.169.254", "8.8.8.8:8000", "", None]:
            self.assertFalse(config.host_allowed(h), h)


class _FakeReq:
    """Minimal stand-in for a Starlette Request for require_auth."""
    def __init__(self, headers, client_host="127.0.0.1"):
        self.headers = headers              # lowercase-keyed, dict .get is enough
        self.query_params = {}
        self.client = type("_Client", (), {"host": client_host})()


class RequireAuthHostValidation(unittest.TestCase):
    """DNS-rebinding regression: in tokenless (loopback-trust) mode, require_auth
    must reject a request whose HTTP Host is foreign, even though it arrives from
    a loopback peer with no Origin (exactly what a rebound browser request looks
    like)."""

    def setUp(self):
        if config.AUTH_TOKEN:
            self.skipTest("Host gate only applies in tokenless mode")

    def test_rebound_foreign_host_rejected(self):
        req = _FakeReq({"host": "evil.com:8000"})  # no Origin, loopback peer
        with self.assertRaises(main.HTTPException) as ctx:
            asyncio.run(main.require_auth(req))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_local_host_allowed(self):
        req = _FakeReq({"host": "127.0.0.1:8000"})
        self.assertIsNone(asyncio.run(main.require_auth(req)))

    def test_missing_host_rejected(self):
        req = _FakeReq({})
        with self.assertRaises(main.HTTPException):
            asyncio.run(main.require_auth(req))


if __name__ == "__main__":
    unittest.main()
