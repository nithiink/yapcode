"""The HTTP seam. Every /api/* response is wrapped in {"data": ...} (design
spec section 2) — the first thing that broke the live probe — and OpenCode's
InvalidRequestError must arrive upstream as a ValueError, which is what
tools.py turns into a soft error the voice model can recover from.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.providers.opencode.client import (  # noqa: E402
    OpenCodeClient, OpenCodeError, OpenCodeRequestError)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_unwraps_the_data_envelope(self):
        with FakeOpenCode() as fake:
            fake.state.new_session("/tmp/x")
            c = OpenCodeClient(fake.url)
            try:
                out = await c.get("/api/session")
                # Unwrapped: a list of sessions, not {"data": [...]}.
                self.assertIsInstance(out, list)
                self.assertEqual(out[0]["location"]["directory"], "/tmp/x")
            finally:
                await c.close()

    async def test_post_unwraps_and_sends_json(self):
        with FakeOpenCode() as fake:
            c = OpenCodeClient(fake.url)
            try:
                out = await c.post("/api/session",
                                   {"location": {"directory": "/tmp/y"}})
                self.assertTrue(out["id"].startswith("ses_"))
            finally:
                await c.close()

    async def test_invalid_request_becomes_a_valueerror(self):
        with FakeOpenCode() as fake:
            c = OpenCodeClient(fake.url)
            try:
                with self.assertRaises(OpenCodeRequestError) as cm:
                    await c.post("/api/session", {})      # missing location
                self.assertIsInstance(cm.exception, ValueError)
                self.assertIn("location", str(cm.exception))
            finally:
                await c.close()

    async def test_server_error_becomes_an_opencode_error(self):
        with FakeOpenCode() as fake:
            fake.state.fail_next = (500, {"message": "boom"})
            c = OpenCodeClient(fake.url)
            try:
                with self.assertRaises(OpenCodeError):
                    await c.get("/api/session")
            finally:
                await c.close()

    async def test_unreachable_server_becomes_an_opencode_error(self):
        # Port 1 is reserved and nothing listens there.
        c = OpenCodeClient("http://127.0.0.1:1", timeout=1.0)
        try:
            with self.assertRaises(OpenCodeError):
                await c.get("/api/session")
        finally:
            await c.close()

    async def test_password_is_sent_when_configured(self):
        with FakeOpenCode() as fake:
            fake.state.require_password = "s3cret"
            ok = OpenCodeClient(fake.url, password="s3cret")
            bad = OpenCodeClient(fake.url)
            try:
                self.assertIsInstance(await ok.get("/api/session"), list)
                with self.assertRaises(OpenCodeError):
                    await bad.get("/api/session")
            finally:
                await ok.close()
                await bad.close()

    async def test_the_password_never_reaches_a_log_or_a_repr(self):
        c = OpenCodeClient("http://127.0.0.1:1", password="s3cret")
        try:
            self.assertNotIn("s3cret", repr(c))
            self.assertNotIn("s3cret", str(c))
        finally:
            await c.close()

    async def test_query_params_are_passed(self):
        with FakeOpenCode() as fake:
            sid = fake.state.new_session()
            fake.state.push_event(sid, "a")
            fake.state.push_event(sid, "b")
            c = OpenCodeClient(fake.url)
            try:
                after0 = await c.get(f"/api/session/{sid}/history", after=0)
                after1 = await c.get(f"/api/session/{sid}/history", after=1)
                self.assertEqual(len(after0), 2)
                self.assertEqual(len(after1), 1)
            finally:
                await c.close()

    async def test_a_blank_cursor_param_is_treated_as_no_cursor(self):
        """httpx renders both None and "" as `after=`, and an unset cursor on the
        first poll is naturally None -- so a blank value has to mean "from the
        start", not crash the handler thread."""
        with FakeOpenCode() as fake:
            sid = fake.state.new_session()
            fake.state.push_event(sid, "a")
            fake.state.push_event(sid, "b")
            c = OpenCodeClient(fake.url)
            try:
                for blank in (None, ""):
                    got = await c.get(f"/api/session/{sid}/history", after=blank)
                    self.assertEqual(len(got), 2, f"after={blank!r}")
            finally:
                await c.close()

    async def test_a_malformed_cursor_param_is_a_clean_request_error(self):
        """Loud as a 400 the caller raises on -- not as a daemon-thread traceback,
        which would break the suite's pristine output."""
        with FakeOpenCode() as fake:
            sid = fake.state.new_session()
            c = OpenCodeClient(fake.url)
            try:
                with self.assertRaises(OpenCodeRequestError):
                    await c.get(f"/api/session/{sid}/history", after="abc")
            finally:
                await c.close()

    async def test_base_url_trailing_slash_is_normalised(self):
        with FakeOpenCode() as fake:
            c = OpenCodeClient(fake.url + "/")
            try:
                self.assertIsInstance(await c.get("/api/session"), list)
                self.assertFalse(c.base_url.endswith("/"))
            finally:
                await c.close()


class Auth(unittest.IsolatedAsyncioTestCase):
    """The auth mechanism, as MEASURED against `opencode serve` with
    OPENCODE_SERVER_PASSWORD set -- the OpenAPI declares no security scheme:

        no header               -> 401
        x-opencode-password     -> 401   <- what this client sent originally
        Basic with empty user   -> 401
        Basic opencode:<pw>     -> 200

    The first guess was wrong in production and right in the fake, because the
    fake had been written to agree with it. These tests exist so the fake can
    never drift back into being agreeable.
    """

    async def test_it_sends_http_basic_not_a_custom_header(self):
        with FakeOpenCode() as fake:
            fake.state.require_password = "pw"
            c = OpenCodeClient(fake.url, password="pw")
            try:
                self.assertIsInstance(await c.get("/api/session"), list)
                header = c._headers()
                self.assertTrue(header["Authorization"].startswith("Basic "))
                self.assertNotIn("x-opencode-password", header)
                user, _, password = base64.b64decode(
                    header["Authorization"][6:]).decode().partition(":")
                self.assertEqual((user, password), ("opencode", "pw"))
            finally:
                await c.close()

    async def test_the_username_is_configurable_but_never_empty(self):
        """Basic with an empty username is refused by the real server, so an
        empty configured value must fall back rather than be sent as-is."""
        with FakeOpenCode() as fake:
            fake.state.require_password = "pw"
            for given, want in (("opencode", "opencode"), ("someone", "someone"),
                                ("", "opencode")):
                c = OpenCodeClient(fake.url, password="pw", username=given)
                try:
                    user = base64.b64decode(
                        c._headers()["Authorization"][6:]).decode().partition(":")[0]
                    self.assertEqual(user, want, given)
                    self.assertIsInstance(await c.get("/api/session"), list)
                finally:
                    await c.close()

    async def test_a_wrong_or_missing_password_is_refused(self):
        with FakeOpenCode() as fake:
            fake.state.require_password = "pw"
            for password in ("wrong", None):
                c = OpenCodeClient(fake.url, password=password)
                try:
                    with self.assertRaises(OpenCodeError, msg=repr(password)):
                        await c.get("/api/session")
                finally:
                    await c.close()

    async def test_no_authorization_header_when_no_password_is_set(self):
        c = OpenCodeClient("http://127.0.0.1:1")
        try:
            self.assertEqual(c._headers(), {})
        finally:
            await c.close()


if __name__ == "__main__":
    unittest.main()
