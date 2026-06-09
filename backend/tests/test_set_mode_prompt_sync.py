"""Prompt-state-sync regression tests for the `set_mode` tool.

Bug: switching to a mode that covers a pending permission (auto: anything,
acceptEdits: file edits) auto-approves it in the terminal, but the frontend kept
showing the prompt as pending because the `set_mode` result carried no machine
signal — only prose. The fix adds a structured `prompt_resolved` flag the
frontend uses to dismiss the stale card and resume polling.

These tests exercise `dispatch_tool("set_mode", ...)` with the session registry
stubbed out, so they run without tmux or a live Claude CLI:

    python -m unittest backend.tests.test_set_mode_prompt_sync   # from repo root
    python -m unittest discover -s backend/tests                 # all backend tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tools  # noqa: E402


def _patch_session(prompt):
    """Patch the registry so `set_mode` sees one session whose pending prompt is
    `prompt` (a dict or None), and a no-op async mode switch echoing the target."""
    handle = "sess-1"
    sess = {"handle": handle, "name": "demo", "prompt": prompt}

    async def fake_set_mode(sid, mode):
        return mode

    return mock.patch.multiple(
        tools,
        resolve_session=mock.Mock(return_value=handle),
        list_all_sessions=mock.Mock(return_value=[sess]),
        set_session_mode=mock.AsyncMock(side_effect=fake_set_mode),
    )


PERM = lambda tool: {"kind": "permission", "text": f"run {tool}", "tool_name": tool}


class SetModePromptSync(unittest.IsolatedAsyncioTestCase):
    async def _set_mode(self, prompt, mode):
        with _patch_session(prompt):
            return await tools.dispatch_tool(
                "set_mode", {"session_id": "sess-1", "mode": mode})

    async def test_auto_covers_any_pending_permission(self):
        out = await self._set_mode(PERM("Bash"), "auto")
        self.assertEqual(out["mode"], "auto")
        self.assertIs(out["prompt_resolved"], True)
        self.assertIn("approved under the new mode", out["message"])

    async def test_accept_edits_covers_pending_edit(self):
        out = await self._set_mode(PERM("Edit"), "acceptEdits")
        self.assertIs(out["prompt_resolved"], True)

    async def test_accept_edits_does_not_cover_pending_bash(self):
        out = await self._set_mode(PERM("Bash"), "acceptEdits")
        # Mode switched, but the prompt still needs a human answer — the frontend
        # must keep the card up, so the flag is explicitly False.
        self.assertIs(out["prompt_resolved"], False)
        self.assertIn("still", out["message"])

    async def test_no_pending_prompt_omits_flag(self):
        out = await self._set_mode(None, "auto")
        self.assertEqual(out["mode"], "auto")
        self.assertNotIn("prompt_resolved", out)
        self.assertNotIn("message", out)

    async def test_pending_question_is_not_a_permission(self):
        # A pending AskUserQuestion choice is never auto-approved by a mode
        # switch, so it carries no resolution flag.
        out = await self._set_mode(
            {"kind": "choice", "text": "pick one", "tool_name": "AskUserQuestion"},
            "auto")
        self.assertNotIn("prompt_resolved", out)


if __name__ == "__main__":
    unittest.main()
