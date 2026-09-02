"""HTTP surface tests for /yuri/* (spec §5.8): every route sits behind
require_auth, and each endpoint's success/error shape matches the domain
services it calls. Uses a real FastAPI app (not main.py's) so the auth
dependency is injected exactly the way build_router() is designed for.

    .venv/bin/python -m unittest tests.test_yuri_api -v
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.api.routes import build_router  # noqa: E402
from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402

PERM = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"], "request_id": "r1"}

_PARAM_RE = re.compile(r"\{[^}]+\}")


class YuriApi(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
                        mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri"))]
        [p.start() for p in self.patches]
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)
        self.denied = False

        async def guard():
            if self.denied:
                raise HTTPException(status_code=401, detail="nope")
        self.router = build_router(guard)
        self.app = FastAPI()
        self.app.include_router(self.router)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    def _start(self):
        return asyncio.run(self.c.sessions.start("proj", name="s1"))

    def _stream_route(self):
        return next(rt for rt in self.router.routes if rt.path == "/yuri/events/stream")

    # --- security gate --------------------------------------------------------

    def test_auth_dependency_applies_to_every_route(self):
        """The gate is the router's own dependency, so enumerate the router's
        routes rather than hardcoding a path list -- a route added later must
        not silently escape it. Every route defined by build_router() must
        reject with 401 once the guard denies, regardless of method or path
        params."""
        self.denied = True
        tested = 0
        for route in self.router.routes:
            path = _PARAM_RE.sub("x", route.path)
            for method in route.methods - {"HEAD", "OPTIONS"}:
                resp = self.client.request(method, path)
                self.assertEqual(resp.status_code, 401, f"{method} {path} -> {resp.status_code}")
                tested += 1
        # Guards against a broken/empty enumeration silently passing the loop.
        self.assertGreaterEqual(tested, 15)

    # --- projects ---------------------------------------------------------

    def test_projects(self):
        r = self.client.get("/yuri/projects")
        self.assertEqual(r.status_code, 200)
        names = [p["name"] for p in r.json()["projects"]]
        self.assertIn("proj", names)
        r = self.client.post("/yuri/projects", json={"path": "proj", "default_agent": "fake"})
        self.assertEqual(r.status_code, 201)
        pid = r.json()["id"]
        self.assertEqual(self.client.get(f"/yuri/projects/{pid}").json()["default_agent"], "fake")
        self.assertEqual(self.client.post("/yuri/projects", json={"path": "/etc"}).status_code, 400)
        self.assertEqual(self.client.get("/yuri/projects/nope").status_code, 404)

    # --- agents -------------------------------------------------------------

    def test_agents_and_health(self):
        r = self.client.get("/yuri/agents")
        self.assertEqual(r.status_code, 200)
        a = r.json()["agents"][0]
        self.assertEqual((a["id"], a["online"]), ("fake", True))
        self.assertIn("capabilities", a)
        self.assertEqual(self.client.get("/yuri/agents/fake/health").json()["online"], True)
        self.assertEqual(self.client.get("/yuri/agents/nope/health").status_code, 404)

    # --- missions -----------------------------------------------------------

    def test_missions_flow(self):
        out = self._start()
        r = self.client.get("/yuri/missions")
        self.assertEqual([m["id"] for m in r.json()["missions"]], [out["mission_id"]])
        self.assertEqual(self.client.get("/yuri/missions?status=paused").json()["missions"], [])
        d = self.client.get(f"/yuri/missions/{out['mission_id']}").json()
        self.assertEqual(set(d), {"mission", "steps", "sessions", "approvals", "events"})
        mid = out["mission_id"]
        self.assertEqual(self.client.post(f"/yuri/missions/{mid}/pause").json()["status"], "paused")
        self.assertEqual(self.client.post(f"/yuri/missions/{mid}/resume").json()["status"], "running")
        self.assertEqual(self.client.post(f"/yuri/missions/{mid}/cancel").json()["status"], "cancelled")
        self.assertEqual(self.client.post(f"/yuri/missions/{mid}/resume").status_code, 409)
        self.assertEqual(self.client.get("/yuri/sessions").json()["sessions"], [])  # cancel stopped it

    def test_mission_not_found(self):
        self.assertEqual(self.client.get("/yuri/missions/nope").status_code, 404)
        self.assertEqual(self.client.post("/yuri/missions/nope/pause").status_code, 404)

    # --- sessions -----------------------------------------------------------

    def test_sessions_and_interrupt(self):
        out = self._start()
        r = self.client.get("/yuri/sessions").json()["sessions"]
        self.assertEqual(r[0]["yuri_session_id"], out["yuri_session_id"])
        row = self.client.get(f"/yuri/sessions/{out['yuri_session_id']}").json()
        self.assertEqual(row["native_session_id"], out["session_id"])
        r = self.client.post(f"/yuri/sessions/{out['yuri_session_id']}/interrupt")
        self.assertEqual(r.json()["status"], "interrupted")
        self.assertEqual(self.client.get("/yuri/sessions/nope").status_code, 404)
        self.assertEqual(self.client.post("/yuri/sessions/nope/interrupt").status_code, 404)

    # --- approvals ----------------------------------------------------------

    def test_approvals(self):
        out = self._start()
        self.fake.script(out["session_id"], {"status": "needs_permission", "prompt": PERM})
        self.c.sessions.poll(out["session_id"])
        pend = self.client.get("/yuri/approvals?status=pending").json()["approvals"]
        self.assertEqual(len(pend), 1)
        aid = pend[0]["id"]
        r = self.client.post(f"/yuri/approvals/{aid}/deny")
        self.assertEqual(r.json()["status"], "denied")
        self.assertTrue(r.json()["forwarded"])
        self.assertIn(("answer", out["session_id"], "deny"), self.fake.calls)
        self.assertEqual(self.client.post(f"/yuri/approvals/{aid}/approve").status_code, 409)

    def test_approve_advances_the_session_and_mission_immediately(self):
        """The route used to duplicate the front half of SessionService.answer
        and skip its `_touch`, so after a UI approve the row still read
        `needs_permission` and the mission `waiting_for_approval` until the next
        poll happened to heal them."""
        out = self._start()
        self.fake.script(out["session_id"], {"status": "needs_permission", "prompt": PERM})
        self.c.sessions.poll(out["session_id"])
        row = self.c.sessions.row_for(out["session_id"])
        self.assertEqual(row.status, "needs_permission")
        self.assertEqual(self.c.missions.get(out["mission_id"]).status, "waiting_for_approval")
        aid = self.client.get("/yuri/approvals?status=pending").json()["approvals"][0]["id"]
        r = self.client.post(f"/yuri/approvals/{aid}/approve")
        self.assertTrue(r.json()["forwarded"])
        self.assertIn(("answer", out["session_id"], "allow"), self.fake.calls)
        self.assertEqual(self.c.sessions.row_for(out["session_id"]).status, "running")
        self.assertEqual(self.c.missions.get(out["mission_id"]).status, "running")

    def test_approval_not_forwarded_when_session_not_live(self):
        """Regression: the brief's _decide() fell through to `forwarded: True`
        whenever the session lookup was skipped (row is None, or the session
        was no longer live) -- it only guarded the *provider call*, not the
        response it produced when that call never happened. Resolve an
        approval whose session has since been stopped (not a live status) and
        confirm the response honestly reports forwarded=False and answer() is
        never called on a session that no longer exists."""
        out = self._start()
        self.fake.script(out["session_id"], {"status": "needs_permission", "prompt": PERM})
        self.c.sessions.poll(out["session_id"])
        pend = self.client.get("/yuri/approvals?status=pending").json()["approvals"]
        aid = pend[0]["id"]
        asyncio.run(self.c.sessions.stop(out["session_id"]))
        r = self.client.post(f"/yuri/approvals/{aid}/deny")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "denied")
        self.assertFalse(r.json()["forwarded"])
        self.assertNotIn(("answer", out["session_id"], "deny"), self.fake.calls)

    def test_approval_not_found(self):
        self.assertEqual(self.client.post("/yuri/approvals/nope/approve").status_code, 404)

    # --- events / context -----------------------------------------------------

    def test_events_and_context(self):
        out = self._start()
        # events persist via the bus writer, which needs a running loop; the list endpoint
        # reads the repo, so insert one directly to prove the read path.
        self.c.store.events.insert(YuriEvent.make(EventType.TOOL_STARTED, mission_id=out["mission_id"]))
        evs = self.client.get(f"/yuri/events?mission_id={out['mission_id']}").json()["events"]
        self.assertEqual(evs[-1]["type"], "tool.started")
        self.c.memory.remember("likes tea")
        ctx = self.client.get("/yuri/context").json()
        self.assertEqual(set(ctx), {"home", "memory_user", "journal_today", "active_missions", "agents"})
        self.assertIn("likes tea", ctx["memory_user"])
        self.assertEqual(ctx["active_missions"][0]["title"], "s1")
        self.assertEqual(ctx["agents"][0]["id"], "fake")

    def test_context_degrades_gracefully_on_fresh_home(self):
        """No mission was ever created or journal entry appended, so
        journal/<today>.md doesn't exist yet (Home.ensure() only pre-seeds
        memory/user.md, not the journal) -- /yuri/context must still return
        200 with an empty journal/no active missions rather than erroring on
        a missing file."""
        ctx = self.client.get("/yuri/context").json()
        self.assertEqual(ctx["journal_today"], "")
        self.assertEqual(ctx["active_missions"], [])
        self.assertTrue(os.path.isdir(ctx["home"]))

    def test_events_limit_is_clamped(self):
        """A caller must not be able to force an unbounded read of the event
        log -- limit is clamped server-side, not merely by convention."""
        with mock.patch.object(self.c.store.events, "list", return_value=[]) as m:
            self.client.get("/yuri/events?limit=999999")
            self.assertLessEqual(m.call_args.kwargs["limit"], 1000)
            self.client.get("/yuri/events?limit=-5")
            self.assertGreaterEqual(m.call_args.kwargs["limit"], 1)

    async def test_events_stream_limit_is_clamped(self):
        # NOTE on approach: neither Starlette's TestClient nor
        # httpx.ASGITransport support a genuine partial read of a streaming
        # response -- both fully await the ASGI app coroutine before handing
        # back any Response, which would mean awaiting a generator that
        # never completes on its own (a ping every 15s, forever). So this
        # calls the route function directly and drives its real
        # StreamingResponse.body_iterator by hand -- the exact same closure
        # the HTTP layer would run, with no risk of hanging the suite.
        ev = YuriEvent.make(EventType.TOOL_STARTED)
        with mock.patch.object(self.c.store.events, "list", return_value=[ev]) as m:
            resp = await self._stream_route().endpoint(mission_id=None, limit=999999)
            agen = resp.body_iterator
            try:
                await agen.__anext__()
            finally:
                await agen.aclose()
        self.assertLessEqual(m.call_args.kwargs["limit"], 1000)

    async def test_events_stream_replays_then_unsubscribes_on_disconnect(self):
        out = await self.c.sessions.start("proj", name="s1")
        self.c.store.events.insert(YuriEvent.make(EventType.TOOL_STARTED, mission_id=out["mission_id"]))
        self.assertEqual(len(self.c.bus._subs), 0)
        resp = await self._stream_route().endpoint(mission_id=None, limit=200)
        agen = resp.body_iterator
        chunk = await agen.__anext__()
        self.assertTrue(chunk.startswith("data: "))
        payload = json.loads(chunk[len("data: "):].strip())
        self.assertEqual(payload["type"], "tool.started")
        # Forcing the first chunk out of the generator guarantees
        # subscribe() (its first statement) already ran.
        self.assertEqual(len(self.c.bus._subs), 1)
        # aclose() throws GeneratorExit into the generator at its current
        # suspension point -- exactly what a disconnecting client produces in
        # production -- so this proves the `finally: bus.unsubscribe(q)`
        # actually runs rather than leaking the subscriber queue forever.
        await agen.aclose()
        self.assertEqual(len(self.c.bus._subs), 0)

    def test_events_stream_denied_returns_401_without_hanging(self):
        """The dependency must fail before the endpoint body (an infinite
        generator) ever starts, so a denied request returns promptly instead
        of hanging the test suite."""
        self.denied = True
        r = self.client.get("/yuri/events/stream")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
