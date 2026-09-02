"""Mission references arrive as speech. Resolution refuses to guess: a wrong
session pick sends the user's instruction to the wrong agent, and a wrong
mission pick cancels the wrong work.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri.domain.approval import Approval  # noqa: E402
from yuri.domain.mission import Mission  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.missions import MissionService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402


class MissionResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(config, "YURI_HOME",
                                          os.path.join(self.tmp.name, "Yuri"))]
        [p.start() for p in self.patches]
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.svc = MissionService(self.store, EventBus(), Journal(self.home))
        self.project = Project(slug="p", name="P", root_path="/tmp/p")
        self.store.projects.insert(self.project)

    def tearDown(self):
        self.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    def _m(self, title, status=None):
        m = self.svc.create(self.project, title, created_by="voice")
        if status and status != m.status:
            self.svc.set_status(m, status, by="test")
        return m

    def test_full_id_and_unique_prefix(self):
        m = self._m("Fix billing")
        self.assertEqual(self.svc.resolve(m.id).id, m.id)
        self.assertEqual(self.svc.resolve(m.id[:8]).id, m.id)

    def test_exact_title_case_insensitive(self):
        m = self._m("Fix billing")
        self.assertEqual(self.svc.resolve("fix BILLING").id, m.id)

    def test_word_overlap_against_active_titles(self):
        m = self._m("Fix the Cashfree payment flow")
        self.assertEqual(self.svc.resolve("cashfree").id, m.id)
        self.assertEqual(self.svc.resolve("the payment one").id, m.id)

    def test_deictic_reference_picks_the_sole_active_mission(self):
        m = self._m("Fix billing")
        for ref in ("", "it", "that", "this one", "the current one", "the mission"):
            self.assertEqual(self.svc.resolve(ref).id, m.id, ref)

    def test_deictic_reference_with_two_active_missions_refuses(self):
        self._m("Fix billing")
        self._m("Update the docs")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("it")
        msg = str(cm.exception)
        self.assertIn("Fix billing", msg)
        self.assertIn("Update the docs", msg)

    def test_ambiguous_overlap_refuses_and_lists_candidates(self):
        self._m("Fix billing in web")
        self._m("Fix billing in mobile")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("fix billing")
        msg = str(cm.exception)
        self.assertIn("web", msg)
        self.assertIn("mobile", msg)

    def test_no_match_names_the_active_missions(self):
        self._m("Fix billing")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("something unrelated entirely")
        self.assertIn("Fix billing", str(cm.exception))

    def test_no_missions_at_all_says_so(self):
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("anything")
        self.assertRegex(str(cm.exception).lower(), r"no .*missions")

    def test_a_completed_mission_is_still_reachable_by_exact_title_or_id(self):
        # Fuzzy matching is scoped to ACTIVE missions so "the payment one" means
        # live work — but an exact reference must still find finished work.
        m = self._m("Fix billing")
        self.svc.set_status(m, "completed", by="test")
        self.assertEqual(self.svc.resolve(m.id).id, m.id)
        self.assertEqual(self.svc.resolve("Fix billing").id, m.id)

    def test_fuzzy_prefers_active_over_completed_with_the_same_words(self):
        old = self._m("Fix billing")
        self.svc.set_status(old, "completed", by="test")
        live = self._m("Fix billing again")
        self.assertEqual(self.svc.resolve("billing").id, live.id)

    # --- adversarial: can resolution ever silently pick the WRONG mission? ---

    def test_two_active_missions_sharing_a_content_word_refuse(self):
        self._m("Fix the payment flow")
        self._m("Update the payment docs")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("payment")
        msg = str(cm.exception)
        self.assertIn("Fix the payment flow", msg)
        self.assertIn("Update the payment docs", msg)

    def test_a_title_word_is_never_read_as_an_id_prefix(self):
        # "cafe" is valid hex, so a naive `id.startswith(ref)` scan can match a
        # DIFFERENT mission's id. An exact title must win.
        other = Mission(title="Deploy the gateway", project_id=self.project.id,
                        id="cafe0000-0000-4000-8000-000000000000", status="running")
        self.store.missions.insert(other)
        mine = self._m("cafe")
        self.assertEqual(self.svc.resolve("cafe").id, mine.id)

    def test_a_short_hex_word_is_matched_against_titles_not_ids(self):
        # "face" is valid hex and short enough to prefix a real uuid by chance.
        # It must reach the fuzzy step, not silently select the id it prefixes.
        decoy = Mission(title="Deploy the gateway", project_id=self.project.id,
                        id="face0000-0000-4000-8000-000000000000", status="running")
        self.store.missions.insert(decoy)
        mine = self._m("Wash the face")
        self.assertEqual(self.svc.resolve("face").id, mine.id)

    def test_a_non_hex_phrase_is_never_treated_as_an_id_prefix(self):
        # A spoken phrase must not enter the id-prefix branch at all, or a
        # multi-mission prefix "match" would refuse a resolvable reference.
        self._m("Fix the Cashfree payment flow")
        self.assertEqual(self.svc.resolve("cashfree").id,
                         self.svc.resolve("the payment one").id)

    def test_an_exact_title_wins_over_a_longer_title_containing_it(self):
        short = self._m("Fix billing")
        self._m("Fix billing in web")
        self.assertEqual(self.svc.resolve("Fix billing").id, short.id)

    def test_stopwords_do_not_manufacture_ambiguity(self):
        # Both titles contain "the"; matching on it would refuse a reference
        # that is actually unambiguous.
        docs = self._m("Update the docs")
        self._m("Fix the billing")
        self.assertEqual(self.svc.resolve("the docs one").id, docs.id)

    def test_deictic_finds_a_waiting_for_approval_mission(self):
        m = self._m("Fix billing", status="waiting_for_approval")
        self.assertEqual(self.svc.resolve("it").id, m.id)
        self.assertEqual(self.svc.resolve("").id, m.id)

    def test_deictic_ignores_terminal_missions(self):
        done = self._m("Old work")
        self.svc.set_status(done, "completed", by="test")
        live = self._m("New work")
        self.assertEqual(self.svc.resolve("it").id, live.id)

    def test_deictic_with_no_active_mission_says_so(self):
        done = self._m("Old work")
        self.svc.set_status(done, "cancelled", by="test")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("")
        self.assertRegex(str(cm.exception).lower(), r"no active missions")

    def test_a_refusal_never_reads_out_an_unbounded_list(self):
        for i in range(12):
            self._m(f"Mission number {i} with a very {'long ' * 40}title")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("it")
        msg = str(cm.exception)
        self.assertLess(len(msg), 1000, msg)
        self.assertIn("more", msg)

    def test_same_title_twice_prefers_the_live_one(self):
        old = self._m("Fix billing")
        self.svc.set_status(old, "completed", by="test")
        live = self._m("Fix billing")
        self.assertEqual(self.svc.resolve("fix billing").id, live.id)

    def test_same_title_twice_both_active_refuses(self):
        self._m("Fix billing")
        self._m("Fix billing")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("fix billing")
        self.assertIn("Fix billing", str(cm.exception))

    def test_active_is_the_documented_status_set(self):
        self.assertEqual(MissionService.ACTIVE,
                         ("running", "waiting_for_approval", "paused", "queued"))


class SpeechDetail(unittest.TestCase):
    def setUp(self):
        MissionResolve.setUp(self)  # same fixture

    def tearDown(self):
        MissionResolve.tearDown(self)

    def test_shape_is_speakable_not_a_dump(self):
        m = self.svc.create(self.project, "Fix billing", created_by="voice")
        d = self.svc.speech_detail(m.id)
        self.assertEqual(set(d), {"mission_id", "title", "goal", "status", "project",
                                  "agents", "sessions", "pending_approval", "last_event"})
        self.assertEqual(d["title"], "Fix billing")
        self.assertEqual(d["project"], "P")
        self.assertEqual(d["sessions"], [])

    def test_unknown_id_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.svc.speech_detail("nope")

    def test_no_sessions_no_project_row_and_no_events_do_not_raise(self):
        # The missions table has a FK on project_id, so a truly orphaned row
        # cannot be inserted — but projects.get() can still miss (a project
        # deleted out from under us), and speech_detail must not raise.
        m = self.svc.create(self.project, "Orphan", created_by="voice")
        with mock.patch.object(self.store.projects, "get", return_value=None):
            d = self.svc.speech_detail(m.id)
        self.assertIsNone(d["project"])
        self.assertIsNone(d["pending_approval"])
        self.assertIsNone(d["last_event"])
        self.assertEqual(d["agents"], [])
        self.assertEqual(d["sessions"], [])

    def test_pending_approval_text_is_clipped(self):
        m = self.svc.create(self.project, "Fix billing", created_by="voice")
        row = AgentSession(project_id=self.project.id, agent_id="fake", native_session_id="h1",
                           backend="cli", working_directory="/tmp/p", mission_id=m.id,
                           status="needs_permission", name="s1")
        self.store.sessions.insert(row)
        self.store.approvals.insert(Approval(session_id=row.id, agent_id="fake", action="run",
                                             tool_name="Bash", request_id="r1",
                                             description="p" * 5000))
        d = self.svc.speech_detail(m.id)
        self.assertLess(len(d["pending_approval"]), 400)
        self.assertEqual(d["sessions"], [{"name": "s1", "status": "needs_permission"}])
        self.assertEqual(d["agents"], ["fake"])


if __name__ == "__main__":
    unittest.main()
