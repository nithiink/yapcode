"""The MCP endpoints (spec §7.2), against the real fake server.

The two that matter most here: `POST /yuri/mcp/test` must persist NOTHING
(asserted by comparing the file before and after), and no endpoint may ever
return an env value.

    .venv/bin/python -m unittest tests.test_mcp_api -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.api.routes import build_router  # noqa: E402
from yuri.mcp.config import config_path  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_mcp_server.py")
SECRET = "planted-secret-value"


def body(name="fake", mode="ok", tier="safe", **over):
    out = {"name": name, "transport": "stdio", "tier": tier,
           "command": sys.executable, "args": [FAKE, mode]}
    out.update(over)
    return out


class McpApi(unittest.TestCase):
    """One TestClient, entered as a context manager.

    That matters here and nowhere else in the suite: TestClient started per
    request runs each one on a FRESH event loop, and an asyncio subprocess
    belongs to the loop that spawned it — so a server saved by one request
    could not be closed by the next. Entering the client once pins a single
    loop for the whole test, which is also what uvicorn does in production.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "Yuri")
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
                        mock.patch.object(config, "YURI_HOME", self.home)]
        [p.start() for p in self.patches]
        self.c = yapp.test_container(self.home, FakeAgentProvider())

        async def guard():
            return None
        self.app = FastAPI()
        self.app.include_router(build_router(guard))
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        # Stop the children on the loop that spawned them, through the API,
        # before the portal (and its loop) goes away.
        for srv in self.client.get("/yuri/mcp").json()["servers"]:
            self.client.delete(f"/yuri/mcp/{srv['name']}")
        self.client.__exit__(None, None, None)
        self.client.close()
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    def _file(self) -> str:
        path = config_path(self.home)
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()

    # --- listing ------------------------------------------------------------

    def test_nothing_configured_lists_no_servers(self):
        r = self.client.get("/yuri/mcp")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["servers"], [])

    # --- the health check ---------------------------------------------------

    def test_test_returns_ok_with_the_servers_own_name_and_tools(self):
        r = self.client.post("/yuri/mcp/test", json=body())
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["verdict"], "ok")
        self.assertEqual(out["server_name"], "fake-mcp")
        self.assertEqual([t["name"] for t in out["tools"]], ["echo", "no_args"])

    def test_test_persists_absolutely_nothing(self):
        # The requirement the whole flow exists for.
        before = self._file()
        self.client.post("/yuri/mcp/test", json=body())
        self.client.post("/yuri/mcp/test", json=body(mode="badexit"))
        self.assertEqual(self._file(), before)
        self.assertEqual(self.client.get("/yuri/mcp").json()["servers"], [])

    def test_a_server_with_no_tools_is_empty_not_ok(self):
        out = self.client.post("/yuri/mcp/test", json=body(mode="empty")).json()
        self.assertEqual(out["verdict"], "empty")

    def test_a_failed_test_returns_the_reason_and_the_stderr(self):
        out = self.client.post("/yuri/mcp/test", json=body(mode="badexit")).json()
        self.assertEqual(out["verdict"], "failed")
        self.assertIn("MISSING_API_KEY", out["error"] + out["stderr"])

    def test_a_missing_tier_is_a_400_naming_the_problem(self):
        r = self.client.post("/yuri/mcp/test", json=body(tier=None))
        self.assertEqual(r.status_code, 400)
        self.assertIn("tier", r.json()["detail"])

    def test_an_unbuilt_transport_is_a_400_that_says_not_yet(self):
        r = self.client.post("/yuri/mcp/test", json=body(transport="http", url="https://x/mcp"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't built yet", r.json()["detail"])

    # --- saving -------------------------------------------------------------

    def test_saving_a_working_server_connects_it_and_registers_its_tools(self):
        r = self.client.post("/yuri/mcp", json=body())
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["server"]["status"], "connected")
        self.assertEqual(r.json()["test"]["verdict"], "ok")
        listed = self.client.get("/yuri/mcp").json()["servers"]
        self.assertEqual(listed[0]["tools"], ["mcp_fake_echo", "mcp_fake_no-args"])

    def test_a_broken_server_cannot_be_saved_at_all(self):
        # A disabled Save button is a label, not a lock — the server re-tests.
        r = self.client.post("/yuri/mcp", json=body(mode="badexit"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("MISSING_API_KEY", json.dumps(r.json()["detail"]))
        self.assertEqual(self._file(), "")

    def test_an_empty_server_is_allowed_to_save_as_a_warning_not_a_pass(self):
        r = self.client.post("/yuri/mcp", json=body(mode="empty"))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["test"]["verdict"], "empty")

    def test_saving_a_name_that_exists_is_refused_rather_than_overwriting(self):
        # Replacing would wipe env values the UI never received back.
        self.client.post("/yuri/mcp", json=body(env={"K": SECRET}))
        r = self.client.post("/yuri/mcp", json=body())
        self.assertEqual(r.status_code, 409)
        self.assertIn(SECRET, self._file())      # the key survived the refusal

    def test_the_saved_file_survives_a_restart_as_the_same_config(self):
        self.client.post("/yuri/mcp", json=body(env={"K": SECRET}))
        # A config the API accepted must be one the loader accepts, or the
        # server would save fine and then vanish at next startup.
        from yuri.mcp.config import load
        [s] = load(self.home)
        self.assertEqual((s.name, s.tier, s.env), ("fake", "safe", {"K": SECRET}))

    # --- redaction ----------------------------------------------------------

    def test_no_endpoint_ever_returns_an_env_value(self):
        saved = self.client.post("/yuri/mcp", json=body(env={"WEATHER_KEY": SECRET}))
        listed = self.client.get("/yuri/mcp")
        reconnected = self.client.post("/yuri/mcp/fake/reconnect")
        toggled = self.client.put("/yuri/mcp/fake/enabled", json={"enabled": False})
        for r in (saved, listed, reconnected, toggled):
            blob = r.text
            self.assertNotIn(SECRET, blob, r.request.url)
            self.assertIn("WEATHER_KEY", blob, r.request.url)   # the NAME is reported

    # --- removing, reconnecting, disabling ----------------------------------

    def test_delete_removes_the_entry_and_unregisters_its_tools(self):
        self.client.post("/yuri/mcp", json=body())
        r = self.client.delete("/yuri/mcp/fake")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/yuri/mcp").json()["servers"], [])
        from yuri.mcp.config import load
        self.assertEqual(load(self.home), [])

    def test_deleting_something_that_was_never_there_is_a_404(self):
        self.assertEqual(self.client.delete("/yuri/mcp/ghost").status_code, 404)

    def test_reconnect_recovers_a_server_that_is_now_working(self):
        self.client.post("/yuri/mcp", json=body())
        self.client.put("/yuri/mcp/fake/enabled", json={"enabled": False})
        self.assertEqual(self.client.get("/yuri/mcp").json()["servers"][0]["status"], "disabled")
        r = self.client.put("/yuri/mcp/fake/enabled", json={"enabled": True})
        self.assertEqual(r.json()["server"]["status"], "connected")

    def test_reconnecting_an_unknown_server_is_a_404(self):
        self.assertEqual(self.client.post("/yuri/mcp/ghost/reconnect").status_code, 404)

    def test_disabling_keeps_the_entry_and_its_keys(self):
        self.client.post("/yuri/mcp", json=body(env={"K": SECRET}))
        self.client.put("/yuri/mcp/fake/enabled", json={"enabled": False})
        self.assertIn(SECRET, self._file())
        self.assertEqual(self.client.get("/yuri/mcp").json()["servers"][0]["status"], "disabled")

    def test_a_disabled_server_offers_no_tools(self):
        self.client.post("/yuri/mcp", json=body())
        self.client.put("/yuri/mcp/fake/enabled", json={"enabled": False})
        import tools as tools_mod
        names = [d["name"] for d in tools_mod.mcp_definitions()]
        self.assertEqual(names, [])
