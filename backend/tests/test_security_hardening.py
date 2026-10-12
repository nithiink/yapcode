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


class HostHeaderAllowlist(unittest.TestCase):
    def test_loopback_hosts_allowed(self):
        for h in ["localhost:8000", "127.0.0.1:8000", "[::1]:8000",
                  "::1", "localhost", "127.0.0.1"]:
            self.assertTrue(config.host_allowed(h), h)

    def test_private_lan_hosts_allowed(self):
        for h in ["192.168.1.5:8000", "10.0.0.2:8000", "172.16.0.1:3000"]:
            self.assertTrue(config.host_allowed(h), h)

    def test_attacker_domain_rejected(self):
        for h in ["evil.example:8000", "evil.example", "attacker.com:3000",
                  "8.8.8.8:8000", ""]:
            self.assertFalse(config.host_allowed(h), h)

    def test_none_rejected(self):
        self.assertFalse(config.host_allowed(None))


class TokenlessOriginRegex(unittest.TestCase):
    def test_regex_disabled_without_token(self):
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("VC_ALLOWED_ORIGIN_REGEX", None)
            with mock.patch.object(config, "AUTH_TOKEN", ""):
                self.assertIsNone(config._resolve_origin_regex())

    def test_regex_enabled_with_token(self):
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("VC_ALLOWED_ORIGIN_REGEX", None)
            with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
                self.assertEqual(config._resolve_origin_regex(),
                                 config._DEFAULT_ORIGIN_REGEX)

    def test_explicit_override_honoured(self):
        with mock.patch.dict(os.environ, {"VC_ALLOWED_ORIGIN_REGEX": r"^https://x$"}):
            with mock.patch.object(config, "AUTH_TOKEN", ""):
                self.assertEqual(config._resolve_origin_regex(), r"^https://x$")

    def test_explicit_empty_disables(self):
        with mock.patch.dict(os.environ, {"VC_ALLOWED_ORIGIN_REGEX": ""}):
            with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
                self.assertIsNone(config._resolve_origin_regex())


class _StubRequest:
    """Minimal stand-in for starlette.Request for require_auth()."""
    def __init__(self, headers, client_host="127.0.0.1"):
        self.headers = headers
        self.query_params = {}
        self.client = type("_C", (), {"host": client_host})()


class RequireAuthHostPinning(unittest.IsolatedAsyncioTestCase):
    async def test_no_origin_attacker_host_rejected_tokenless(self):
        # Simulates a DNS-rebinding same-origin GET: no Origin header, Host is the
        # attacker domain, connection lands on loopback.
        with mock.patch.object(config, "AUTH_TOKEN", ""):
            req = _StubRequest({"host": "evil.example:8000"})
            with self.assertRaises(main.HTTPException) as ctx:
                await main.require_auth(req)
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_no_origin_loopback_host_allowed_tokenless(self):
        with mock.patch.object(config, "AUTH_TOKEN", ""):
            req = _StubRequest({"host": "localhost:8000"})
            await main.require_auth(req)  # must not raise

    async def test_attacker_host_allowed_when_token_matches(self):
        # With a token configured, Host pinning is skipped and the token governs.
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
            req = _StubRequest({"host": "evil.example:8000",
                                "x-vc-token": "s3cret"})
            await main.require_auth(req)  # must not raise


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
