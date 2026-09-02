import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.session import AgentSession  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.services.approvals import ApprovalService  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402

PROMPT = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
          "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"],
          "request_id": "req-1"}

# No request_id — exercises the synthesized-id fallback (the real path always
# carries one; this simulates a misbehaving upstream caller).
PROMPT_NO_REQID = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
                   "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"]}
PROMPT_NO_REQID_OTHER = {"kind": "permission", "text": "force push", "tool_name": "Bash",
                         "tool_input": {"command": "git push --force"}, "options": ["allow", "deny"]}


class ApprovalServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.q = self.bus.subscribe()
        self.svc = ApprovalService(self.store, self.bus, Journal(self.home))
        self.sess = AgentSession(project_id="p", agent_id="claude-code", native_session_id="h1",
                                 backend="cli", working_directory="/tmp", mission_id="m1",
                                 name="billing")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _events(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out

    def test_record_request_is_idempotent_and_classifies_risk(self):
        a1 = self.svc.record_request(self.sess, PROMPT)
        a2 = self.svc.record_request(self.sess, PROMPT)
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(a1.risk, "dangerous")
        self.assertEqual(a1.mission_id, "m1")
        self.assertEqual(a1.session_id, self.sess.id)
        evs = self._events()
        self.assertEqual([e.type for e in evs], ["approval.requested"])
        self.assertEqual(evs[0].payload["session_name"], "billing")

    def test_new_request_supersedes_stale_pending(self):
        a1 = self.svc.record_request(self.sess, PROMPT)
        a2 = self.svc.record_request(self.sess, {**PROMPT, "request_id": "req-2"})
        self.assertNotEqual(a1.id, a2.id)
        self.assertEqual(self.store.approvals.get(a1.id).status, "superseded")
        self.assertEqual(self.svc.pending(), [a2])

    def test_no_request_id_dedups_the_identical_prompt(self):
        # Same session, same tool_name, same tool_input, no request_id, session
        # untouched between calls -> must dedup to one Approval, not crash on the
        # request_id UNIQUE constraint and not create a duplicate.
        a1 = self.svc.record_request(self.sess, PROMPT_NO_REQID)
        a2 = self.svc.record_request(self.sess, PROMPT_NO_REQID)
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(len(self.svc.pending()), 1)

    def test_no_request_id_never_lets_a_different_prompt_inherit_a_decision(self):
        # Reproduction from code review: same session, same tool, no request_id,
        # but a genuinely DIFFERENT tool_input (a different command). The second
        # call must never return the first (already-resolved) approval — that
        # would silently let "git push --force" inherit an allow that was only
        # ever given for "rm -rf build".
        a1 = self.svc.record_request(self.sess, PROMPT_NO_REQID)
        self.svc.resolve(a1.id, "allowed", by="voice")
        a2 = self.svc.record_request(self.sess, PROMPT_NO_REQID_OTHER)
        self.assertNotEqual(a1.id, a2.id)
        self.assertEqual(a2.status, "pending")
        self.assertEqual(a2.description, "force push")
        self.assertEqual(self.store.approvals.get(a1.id).status, "allowed")
        self.assertEqual(self.svc.pending(), [a2])

    def test_no_request_id_supersedes_a_still_pending_different_prompt(self):
        # Same as above, but the first request was never resolved (still
        # pending) when the second, different prompt arrives — must supersede,
        # per the one-decision-per-prompt rule, not silently reuse or duplicate.
        a1 = self.svc.record_request(self.sess, PROMPT_NO_REQID)
        a2 = self.svc.record_request(self.sess, PROMPT_NO_REQID_OTHER)
        self.assertNotEqual(a1.id, a2.id)
        self.assertEqual(self.store.approvals.get(a1.id).status, "superseded")
        self.assertEqual(a2.status, "pending")
        self.assertEqual(self.svc.pending(), [a2])

    def test_synthesized_request_id_is_content_addressed_and_tagged(self):
        a1 = self.svc.record_request(self.sess, PROMPT_NO_REQID)
        self.assertTrue(a1.request_id.startswith("synth:"))
        a2 = self.svc.record_request(self.sess, {**PROMPT_NO_REQID, "tool_input": {"command": "other"}})
        self.assertNotEqual(a1.request_id, a2.request_id)

    def test_missing_request_id_logs_a_warning(self):
        with self.assertLogs("yuri.services.approvals", level="WARNING") as cm:
            self.svc.record_request(self.sess, PROMPT_NO_REQID)
        self.assertTrue(any("request_id" in line for line in cm.output))

    def test_resolve_by_session_allow_deny_ambiguous(self):
        self.svc.record_request(self.sess, PROMPT)
        with self.assertRaises(ValueError):
            self.svc.resolve_by_session(self.sess, "hmm maybe", by="voice")
        a = self.svc.resolve_by_session(self.sess, "yes go ahead", by="voice")
        self.assertEqual((a.status, a.resolved_by), ("allowed", "voice"))
        self.assertIsNone(self.svc.resolve_by_session(self.sess, "yes", by="voice"))
        ev = [e for e in self._events() if e.type == "approval.resolved"][0]
        self.assertEqual(ev.payload["status"], "allowed")

    def test_resolve_by_id_twice_fails(self):
        a = self.svc.record_request(self.sess, PROMPT)
        self.svc.resolve(a.id, "denied", by="ui")
        with self.assertRaises(ValueError):
            self.svc.resolve(a.id, "allowed", by="ui")
        with self.assertRaises(KeyError):
            self.svc.resolve("nope", "allowed", by="ui")

    def test_journal_line_written_on_resolve(self):
        a = self.svc.record_request(self.sess, PROMPT)
        self.svc.resolve(a.id, "denied", by="ui")
        self.assertIn("denied", Journal(self.home).read_today())


if __name__ == "__main__":
    unittest.main()
