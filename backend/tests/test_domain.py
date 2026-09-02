import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.event import DEFAULTS, EventType, YuriEvent  # noqa: E402
from yuri.domain.mission import InvalidTransition, Mission, MissionStatus  # noqa: E402
from yuri.domain.project import Project, slugify  # noqa: E402
from yuri.domain.session import AgentSession, LIVE_STATUSES  # noqa: E402


class MissionTransitions(unittest.TestCase):
    def _m(self, status="running"):
        return Mission(title="t", project_id="p", status=status, created_by="voice")

    def test_valid_paths(self):
        m = self._m("draft")
        self.assertTrue(m.transition("running"))
        self.assertTrue(m.transition("waiting_for_approval"))
        self.assertTrue(m.transition("running"))
        self.assertTrue(m.transition("paused"))
        self.assertTrue(m.transition("running"))
        self.assertTrue(m.transition("completed"))
        self.assertEqual(m.status, "completed")

    def test_same_state_is_noop(self):
        m = self._m("running")
        before = m.updated_at
        self.assertFalse(m.transition("running"))
        self.assertEqual(m.updated_at, before)

    def test_terminal_cannot_move(self):
        for t in ["completed", "failed", "cancelled"]:
            m = self._m(t)
            with self.assertRaises(InvalidTransition):
                m.transition("running")

    def test_invalid_edge(self):
        with self.assertRaises(InvalidTransition):
            self._m("paused").transition("completed")
        with self.assertRaises(InvalidTransition):
            self._m("running").transition("nonsense")

    def test_round_trip(self):
        m = self._m()
        m.metadata = {"k": 1}
        self.assertEqual(Mission.from_dict(m.to_dict()), m)
        self.assertEqual(MissionStatus.RUNNING, "running")

    def test_status_is_always_plain_str(self):
        # Regression: MissionStatus subclasses str, so `status ==
        # MissionStatus.RUNNING` reads naturally, but `str(MissionStatus.X)`
        # yields "MissionStatus.X", not "x" — a Mission built with the enum
        # must not leak an Enum instance into to_dict()/the attribute.
        m_enum = Mission(title="t", project_id="p", status=MissionStatus.DRAFT, created_by="voice")
        self.assertIs(type(m_enum.status), str)
        self.assertIs(type(m_enum.to_dict()["status"]), str)
        self.assertEqual(m_enum.status, "draft")

        m_str = self._m("draft")
        self.assertIs(type(m_str.status), str)
        self.assertIs(type(m_str.to_dict()["status"]), str)

        m_enum.transition("running")
        self.assertIs(type(m_enum.status), str)
        self.assertIs(type(m_enum.to_dict()["status"]), str)


class ProjectSlug(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("PM Tool"), "pm-tool")
        self.assertEqual(slugify("  yuri_code!! "), "yuri-code")
        self.assertEqual(slugify(""), "project")

    def test_round_trip(self):
        p = Project(slug="x", name="X", root_path="/tmp/x", kind="home", auto_approve_edits=True)
        self.assertEqual(Project.from_dict(p.to_dict()), p)


class SessionDefaults(unittest.TestCase):
    def test_live_statuses(self):
        self.assertEqual(LIVE_STATUSES,
                         frozenset({"starting", "running", "needs_permission", "needs_choice", "idle"}))
        s = AgentSession(project_id="p", agent_id="claude-code", native_session_id="h",
                         backend="cli", working_directory="/tmp")
        self.assertEqual(s.status, "starting")
        self.assertEqual(AgentSession.from_dict(s.to_dict()), s)


class Events(unittest.TestCase):
    def test_make_applies_defaults(self):
        e = YuriEvent.make(EventType.APPROVAL_REQUESTED, session_id="s", payload={"x": 1})
        self.assertEqual((e.severity, e.speakable), ("notice", True))
        self.assertTrue(e.ts.endswith("Z"))
        self.assertEqual(YuriEvent.from_dict(e.to_dict()), e)

    def test_every_type_has_defaults(self):
        types = [v for k, v in vars(EventType).items() if k.isupper()]
        self.assertTrue(types)
        for t in types:
            self.assertIn(t, DEFAULTS, t)

    def test_explicit_override(self):
        e = YuriEvent.make(EventType.TOOL_STARTED, severity="warning", speakable=True)
        self.assertEqual((e.severity, e.speakable), ("warning", True))


if __name__ == "__main__":
    unittest.main()
