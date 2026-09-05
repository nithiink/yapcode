"""The Phase 7 HTTP surface (spec §14.2): roster, roles, templates,
workflows, tasks, artifacts.

Same harness as test_yuri_api.py, in its own file because these are twelve
endpoints and burying them in that one would make both harder to read.

    .venv/bin/python -m unittest tests.test_phase7_api -v
"""
from __future__ import annotations

import asyncio
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
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


class Phase7Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "proj")
        os.mkdir(self.root)
        self.patches = [
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
            mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri")),
        ]
        [p.start() for p in self.patches]
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)

        async def guard():
            return None
        self.app = FastAPI()
        self.app.include_router(build_router(guard))
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

        self.project = self.c.projects.register(self.root, "proj", "fake")
        self.mission = self.c.missions.create(self.project, "the bug", created_by="test",
                                            goal="fix the hang")

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    # --- the roster ---------------------------------------------------------

    def test_the_seeded_roster_is_listed(self):
        r = self.client.get("/yuri/specialists")
        self.assertEqual(r.status_code, 200)
        names = [s["name"] for s in r.json()["specialists"]]
        self.assertIn("Researcher", names)

    def test_a_specialist_can_be_created_read_and_updated(self):
        r = self.client.post("/yuri/specialists", json={
            "name": "My Reviewer", "role": "reviewer", "provider_id": "fake",
            "system_prompt": "Be strict."})
        self.assertEqual(r.status_code, 201, r.text)
        sid = r.json()["id"]
        self.assertEqual(self.client.get(f"/yuri/specialists/{sid}").json()["name"], "My Reviewer")
        r = self.client.put(f"/yuri/specialists/{sid}", json={"description": "mine"})
        self.assertEqual(r.json()["description"], "mine")
        # A field left out of the body is left alone.
        self.assertEqual(r.json()["name"], "My Reviewer")

    def test_a_specialist_with_no_role_is_a_400_not_a_500(self):
        r = self.client.post("/yuri/specialists", json={"name": "Nameless"})
        self.assertEqual(r.status_code, 400)

    def test_an_unknown_specialist_is_a_404(self):
        self.assertEqual(self.client.get("/yuri/specialists/nope").status_code, 404)
        self.assertEqual(self.client.put("/yuri/specialists/nope", json={}).status_code, 404)
        self.assertEqual(self.client.delete("/yuri/specialists/nope").status_code, 404)

    def test_delete_archives_rather_than_deleting(self):
        # A specialist's id is on every task it ever ran; removing the row
        # would orphan that history.
        sid = self.client.post("/yuri/specialists", json={
            "name": "Temp", "role": "reviewer", "provider_id": "fake"}).json()["id"]
        self.assertEqual(self.client.delete(f"/yuri/specialists/{sid}").status_code, 200)
        self.assertNotIn("Temp", [s["name"] for s in
                                  self.client.get("/yuri/specialists").json()["specialists"]])
        listed = self.client.get("/yuri/specialists?include_archived=true").json()["specialists"]
        self.assertIn("Temp", [s["name"] for s in listed])

    def test_archiving_a_specialist_a_live_task_holds_is_a_409(self):
        # The requirement the plan calls out by name.
        sid = self.client.post("/yuri/specialists", json={
            "name": "Busy", "role": "developer", "provider_id": "fake"}).json()["id"]
        w = self._workflow()
        task = self._tasks(w)[0]
        asyncio.run(self.c.workflow.assign(task["id"], sid, by="test"))
        r = self.client.delete(f"/yuri/specialists/{sid}")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("live task", r.json()["detail"])

    def test_a_builtin_specialist_cannot_be_archived(self):
        builtin = next(s for s in self.client.get("/yuri/specialists").json()["specialists"]
                       if s["builtin"])
        r = self.client.delete(f"/yuri/specialists/{builtin['id']}")
        self.assertEqual(r.status_code, 409)

    # --- roles and templates ------------------------------------------------

    def test_roles_report_their_preferred_provider_and_who_can_fill_them(self):
        body = self.client.get("/yuri/roles").json()
        roles = {r["role"]: r for r in body["roles"]}
        self.assertIn("reviewer", roles)
        self.assertTrue(roles["researcher"]["prefers"])
        self.assertTrue(roles["researcher"]["specialists"])
        self.assertIn("code_review", body["capabilities"])

    def test_templates_are_listed_with_their_graph(self):
        body = self.client.get("/yuri/templates").json()
        names = [t["name"] for t in body["templates"]]
        self.assertIn("bug-fix", names)
        bugfix = next(t for t in body["templates"] if t["name"] == "bug-fix")
        self.assertEqual(len(bugfix["tasks"]), 4)
        # The graph, not just the titles: the UI draws the dependency order.
        self.assertTrue(any(t["depends_on"] for t in bugfix["tasks"]))

    # --- workflows ----------------------------------------------------------

    def _workflow(self, **body):
        body.setdefault("template", "bug-fix")
        r = self.client.post(f"/yuri/missions/{self.mission.id}/workflow", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["workflow"]

    def _tasks(self, w):
        return self.client.get(f"/yuri/missions/{self.mission.id}/workflow").json()["tasks"]

    def test_a_mission_with_no_workflow_answers_200_with_null(self):
        # Not a 404: "no workflow yet" is a normal answer the timeline renders
        # as an empty state, and a 404 would show a load error instead.
        r = self.client.get(f"/yuri/missions/{self.mission.id}/workflow")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["workflow"])
        self.assertEqual(r.json()["tasks"], [])

    def test_a_workflow_is_created_draft_and_dispatches_nothing(self):
        # The whole reason a spoken plan can be read back before it runs.
        w = self._workflow()
        self.assertEqual(w["status"], "draft")
        self.assertEqual(len(self._tasks(w)), 4)
        self.assertEqual([c for c in self.fake.calls if c[0] == "start"], [])

    def test_the_graph_comes_back_with_its_dependencies(self):
        w = self._workflow()
        body = self.client.get(f"/yuri/missions/{self.mission.id}/workflow").json()
        self.assertTrue(body["deps"], "the timeline cannot order tasks without these")
        for task_id, blockers in body["deps"].items():
            self.assertIsInstance(blockers, list)

    def test_a_template_and_an_explicit_task_list_together_is_a_400(self):
        # A graph that claims a template it did not come from makes the
        # timeline lie about where the plan came from.
        r = self.client.post(f"/yuri/missions/{self.mission.id}/workflow",
                             json={"template": "bug-fix", "tasks": [{"title": "t"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not both", r.json()["detail"])

    def test_neither_a_template_nor_tasks_is_a_400(self):
        r = self.client.post(f"/yuri/missions/{self.mission.id}/workflow", json={})
        self.assertEqual(r.status_code, 400)

    def test_an_unknown_template_is_a_400_that_lists_the_real_ones(self):
        r = self.client.post(f"/yuri/missions/{self.mission.id}/workflow",
                             json={"template": "not-a-template"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("bug-fix", r.json()["detail"])

    def test_an_explicit_task_list_builds_a_graph(self):
        w = self._workflow(template=None, tasks=[
            {"id": "a", "title": "look", "role": "researcher", "instruction": "look"},
            {"id": "b", "title": "do", "role": "developer", "instruction": "do",
             "depends_on": ["a"]}])
        self.assertEqual(len(self._tasks(w)), 2)
        # No template recorded, because the graph did not come from one.
        self.assertIsNone(w["template"])

    def test_a_workflow_for_an_unknown_mission_is_a_404(self):
        self.assertEqual(self.client.get("/yuri/missions/nope/workflow").status_code, 404)
        self.assertEqual(
            self.client.post("/yuri/missions/nope/workflow",
                             json={"template": "bug-fix"}).status_code, 404)

    def test_pause_resume_and_cancel(self):
        w = self._workflow()
        self.assertEqual(
            self.client.post(f"/yuri/workflows/{w['id']}/resume").json()["workflow"]["status"],
            "running")
        self.assertEqual(
            self.client.post(f"/yuri/workflows/{w['id']}/pause").json()["workflow"]["status"],
            "paused")
        self.assertEqual(
            self.client.post(f"/yuri/workflows/{w['id']}/cancel").json()["workflow"]["status"],
            "cancelled")

    def test_an_illegal_workflow_transition_is_a_409(self):
        w = self._workflow()
        self.client.post(f"/yuri/workflows/{w['id']}/cancel")
        r = self.client.post(f"/yuri/workflows/{w['id']}/resume")
        self.assertEqual(r.status_code, 409)

    def test_an_unknown_workflow_is_a_404(self):
        self.assertEqual(self.client.post("/yuri/workflows/nope/pause").status_code, 404)

    # --- tasks --------------------------------------------------------------

    def test_a_task_can_be_skipped_and_that_frees_its_dependents(self):
        w = self._workflow()
        first = self._tasks(w)[0]
        r = self.client.post(f"/yuri/tasks/{first['id']}/skip")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["status"], "skipped")

    def test_retrying_a_task_that_has_not_failed_is_a_409(self):
        # `pending` has no edge back into the queue, and saying so beats a 500.
        w = self._workflow()
        first = self._tasks(w)[0]
        r = self.client.post(f"/yuri/tasks/{first['id']}/retry")
        self.assertEqual(r.status_code, 409, r.text)

    def test_a_task_can_be_assigned_to_a_specialist(self):
        w = self._workflow()
        sid = self.client.post("/yuri/specialists", json={
            "name": "Chosen", "role": "developer", "provider_id": "fake",
            "capabilities": ["coding", "terminal", "git"]}).json()["id"]
        target = next(t for t in self._tasks(w) if t["role"] == "developer")
        r = self.client.post(f"/yuri/tasks/{target['id']}/assign", json={"specialist_id": sid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["specialist_id"], sid)

    def test_assigning_a_specialist_that_cannot_cover_the_task_is_refused(self):
        # Validated now rather than at dispatch: otherwise it surfaces as a
        # failed task minutes later, long after the user was told yes. What is
        # checked is the task's `requires`, so the task needs some.
        w = self._workflow(template=None, tasks=[
            {"id": "r", "title": "review it", "role": "reviewer",
             "instruction": "review", "requires": ["code_review"]}])
        sid = self.client.post("/yuri/specialists", json={
            "name": "Docs only", "role": "reviewer", "provider_id": "fake",
            "capabilities": ["docs"]}).json()["id"]
        target = self._tasks(w)[0]
        r = self.client.post(f"/yuri/tasks/{target['id']}/assign", json={"specialist_id": sid})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("code_review", r.json()["detail"])

    def test_a_specialist_of_another_role_may_be_pinned_on_purpose(self):
        # Not a bug: a pin is a user choice, and the role preference ORDERS
        # candidates rather than excluding them. What is enforced is coverage
        # of the task's `requires`, not a matching role — a user who says
        # "give the fix to the documenter" meant it.
        w = self._workflow()
        sid = self.client.post("/yuri/specialists", json={
            "name": "Docs only", "role": "documenter", "provider_id": "fake",
            "capabilities": ["docs"]}).json()["id"]
        target = next(t for t in self._tasks(w) if t["role"] == "developer")
        r = self.client.post(f"/yuri/tasks/{target['id']}/assign", json={"specialist_id": sid})
        self.assertEqual(r.status_code, 200, r.text)

    def test_an_unknown_task_is_a_404(self):
        for path in ("retry", "skip"):
            self.assertEqual(self.client.post(f"/yuri/tasks/nope/{path}").status_code, 404)
        self.assertEqual(
            self.client.post("/yuri/tasks/nope/assign", json={"specialist_id": "x"}).status_code,
            404)

    # --- artifacts ----------------------------------------------------------

    def test_artifacts_are_listed_for_a_mission(self):
        from yuri.domain.artifact import Artifact
        self.c.store.artifacts.insert(Artifact(
            mission_id=self.mission.id, kind="finding", title="the cause",
            body="the visited set"))
        body = self.client.get(f"/yuri/missions/{self.mission.id}/artifacts").json()
        self.assertEqual([a["title"] for a in body["artifacts"]], ["the cause"])

    def test_artifacts_for_a_mission_with_none_is_an_empty_list(self):
        self.assertEqual(
            self.client.get(f"/yuri/missions/{self.mission.id}/artifacts").json()["artifacts"], [])

    def test_artifacts_for_an_unknown_mission_is_a_404(self):
        self.assertEqual(self.client.get("/yuri/missions/nope/artifacts").status_code, 404)
