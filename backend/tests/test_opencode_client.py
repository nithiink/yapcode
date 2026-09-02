"""The HTTP seam. Every /api/* response is wrapped in {"data": ...} (design
spec section 2) — the first thing that broke the live probe — and OpenCode's
InvalidRequestError must arrive upstream as a ValueError, which is what
tools.py turns into a soft error the voice model can recover from.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

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

    async def test_base_url_trailing_slash_is_normalised(self):
        with FakeOpenCode() as fake:
            c = OpenCodeClient(fake.url + "/")
            try:
                self.assertIsInstance(await c.get("/api/session"), list)
                self.assertFalse(c.base_url.endswith("/"))
            finally:
                await c.close()


if __name__ == "__main__":
    unittest.main()
