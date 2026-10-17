"""Regression tests for the CodeQL-driven security hardening:

- py/polynomial-redos: tmux key names are length-bounded before the chord regex.
- py/path-injection:   session ids / cwds are validated against an allowlist at
                       the sink (session control dir, transcript glob, project root).
- py/full-ssrf:        the realtime-token mint URL is pinned to an allowed host.
- dns-rebinding:        the Host header is validated against an allowlist so an
                       Origin-less same-origin GET can't reach the loopback backend.

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


class HostHeaderAllowlist(unittest.TestCase):
    """DNS-rebinding defense: the Host header is validated so an Origin-less
    same-origin GET from a rebound attacker page can't reach a loopback-trusted
    (tokenless) backend. See backend/main.py require_auth / _ws_access_ok."""

    def _reload_config(self, env):
        import importlib
        with mock.patch.dict(os.environ, env, clear=False):
            importlib.reload(config)
            return config

    def tearDown(self):
        import importlib
        for var in ("VC_ALLOWED_HOSTS", "VC_ALLOWED_HOST_REGEX"):
            os.environ.pop(var, None)
        importlib.reload(config)

    def test_loopback_and_lan_hosts_allowed(self):
        for host in ["localhost", "localhost:8000", "127.0.0.1:8000",
                     "[::1]:8000", "192.168.1.5:3000", "10.0.0.2:8000",
                     "172.16.3.4:8000"]:
            self.assertTrue(config.host_allowed(host), host)

    def test_rebinding_attacker_host_rejected(self):
        for host in ["evil.com", "evil.com:8000", "attacker.example:8000",
                     "127.0.0.1.evil.com:8000"]:
            self.assertFalse(config.host_allowed(host), host)

    def test_missing_host_not_blocked(self):
        # HTTP/1.1 clients always send Host and the rebinding vector always
        # carries one; a missing Host is a non-browser edge client.
        self.assertTrue(config.host_allowed(None))
        self.assertTrue(config.host_allowed(""))

    def test_extra_hosts_via_env(self):
        cfg = self._reload_config({"VC_ALLOWED_HOSTS": "my.tunnel.example"})
        try:
            self.assertTrue(cfg.host_allowed("my.tunnel.example:8000"))
            self.assertFalse(cfg.host_allowed("evil.com:8000"))
        finally:
            self.tearDown()

    def test_check_disabled_when_regex_empty_and_no_hosts(self):
        cfg = self._reload_config({"VC_ALLOWED_HOST_REGEX": ""})
        try:
            self.assertTrue(cfg.host_allowed("evil.com:8000"))
        finally:
            self.tearDown()


if __name__ == "__main__":
    unittest.main()
