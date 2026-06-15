"""Tests for the `mute` voice tool — the command that lets the agent mute its
own microphone in the UI. Muting is a client-side action, so the backend just
acknowledges; the frontend reacts to the tool_call event and flips the mic.

No registry or tmux needed (the handler is stateless):

    python -m unittest discover -s backend/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tools  # noqa: E402


class MuteToolDefinition(unittest.TestCase):
    def _mute_def(self):
        return next((t for t in tools.TOOL_DEFINITIONS if t.get("name") == "mute"), None)

    def test_mute_tool_is_exposed(self):
        self.assertIsNotNone(self._mute_def(), "mute tool missing from TOOL_DEFINITIONS")

    def test_mute_takes_no_required_arguments(self):
        # The model calls `mute` with no args, so the schema must not demand any.
        params = self._mute_def()["parameters"]
        self.assertEqual(params.get("properties", {}), {})
        self.assertEqual(params.get("required", []), [])


class MuteDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_returns_muted_ack(self):
        out = await tools.dispatch_tool("mute", {})
        self.assertIs(out["muted"], True)
        self.assertIn("unmute", out["message"].lower())

    async def test_dispatch_ignores_stray_arguments(self):
        # The schema has no params, but a model may still send some; don't choke.
        out = await tools.dispatch_tool("mute", {"unexpected": "value"})
        self.assertIs(out["muted"], True)


if __name__ == "__main__":
    unittest.main()
