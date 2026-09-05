"""Regression tests for the `set_mode` tool's `prompt_resolved` flag — the
signal the frontend uses to dismiss a stale prompt card and resume polling when
a mode switch auto-approves a pending permission.

Container-backed with a FakeAgentProvider, a temp Yuri home and a temp DB, so
these run without tmux or a live Claude CLI. The pending prompt is set up the
way the real path does it: the provider reports `needs_permission`, a poll
records the Approval row, and the prompt stays on the native session dict until
the mode switch resolves it.

    python -m unittest discover -s backend/tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import tools  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


def PERM(tool: str) -> dict:
    return {"kind": "permission", "text": f"run {tool}", "tool_name": tool,
            "tool_input": {}, "options": ["allow", "deny"], "request_id": "r1"}


class SetModePromptSync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
                        mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri"))]
        for p in self.patches:
            p.start()
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)
        self.sid = (await self.c.sessions.start("Yuri", name="demo"))["session_id"]

    async def asyncTearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def _set_mode(self, prompt, mode):
        if prompt:
            kind = "needs_permission" if prompt["kind"] == "permission" else "needs_choice"
            self.fake.script(self.sid, {"status": kind, "prompt": prompt})
            self.c.sessions.poll(self.sid)              # records the Approval row
            self.fake.sessions[self.sid]["prompt"] = prompt   # still pending on the runner
        return await tools.dispatch_tool("set_mode", {"session_id": "demo", "mode": mode})

    async def test_auto_covers_any_pending_permission(self):
        out = await self._set_mode(PERM("Bash"), "auto")
        self.assertEqual(out["mode"], "auto")
        self.assertIs(out["prompt_resolved"], True)
        self.assertIn("approved under the new mode", out["message"])

    async def test_accept_edits_covers_pending_edit(self):
        out = await self._set_mode(PERM("Edit"), "acceptEdits")
        self.assertIs(out["prompt_resolved"], True)

    async def test_accept_edits_does_not_cover_pending_bash(self):
        # Bash still needs a human answer, so the flag is explicitly False.
        out = await self._set_mode(PERM("Bash"), "acceptEdits")
        self.assertIs(out["prompt_resolved"], False)
        self.assertIn("still", out["message"])

    async def test_no_pending_prompt_omits_flag(self):
        out = await self._set_mode(None, "auto")
        self.assertEqual(out["mode"], "auto")
        self.assertNotIn("prompt_resolved", out)
        self.assertNotIn("message", out)

    async def test_pending_question_is_not_a_permission(self):
        out = await self._set_mode(
            {"kind": "choice", "text": "pick one", "tool_name": "AskUserQuestion"},
            "auto")
        self.assertNotIn("prompt_resolved", out)


if __name__ == "__main__":
    unittest.main()
