"""Agent selection has one home. Order: explicit request, then the project's
default, then the container's default (plan section 18). The routing rules that
section lists as future — task type, cost, latency, capability, workload — are
deliberately absent: a router with one agent to route to would be speculation.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.project import Project  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.router import AgentRouter  # noqa: E402


class Router(unittest.TestCase):
    def setUp(self):
        self.a = FakeAgentProvider()                 # id "fake"
        self.b = FakeAgentProvider()
        self.b.id = "other"
        self.reg = AgentRegistry()
        self.reg.register(self.a)
        self.reg.register(self.b)
        self.router = AgentRouter(self.reg, default_agent="fake")

    def _project(self, default_agent=None):
        return Project(slug="p", name="P", root_path="/tmp/p", default_agent=default_agent)

    def test_explicit_request_wins(self):
        self.assertIs(self.router.select(self._project("fake"), requested="other"), self.b)

    def test_project_default_when_nothing_requested(self):
        self.assertIs(self.router.select(self._project("other")), self.b)

    def test_container_default_when_project_has_none(self):
        self.assertIs(self.router.select(self._project()), self.a)

    def test_unknown_requested_agent_raises_naming_what_exists(self):
        with self.assertRaises(KeyError) as cm:
            self.router.select(self._project(), requested="opencode")
        msg = str(cm.exception)
        self.assertIn("opencode", msg)
        self.assertIn("fake", msg)

    def test_unknown_project_default_falls_back_rather_than_failing(self):
        # A project configured for an agent that is no longer registered must
        # not make its sessions unstartable — the user did not ask for it now.
        self.assertIs(self.router.select(self._project("retired-agent")), self.a)

    def test_empty_string_request_is_treated_as_no_request(self):
        self.assertIs(self.router.select(self._project("other"), requested=""), self.b)


if __name__ == "__main__":
    unittest.main()
