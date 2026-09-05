"""Pins TmuxClaudeRunner's two fragile untested paths: rehydrate(), and the
completion paths that must reach the Yuri observer. tmux is faked at `_tmux`,
the control store is a temp dir.

    python -m unittest discover -s backend/tests
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tmux_runner  # noqa: E402

H1 = "11111111-aaaa-bbbb-cccc-000000000001"
H2 = "22222222-aaaa-bbbb-cccc-000000000002"
H3 = "33333333-aaaa-bbbb-cccc-000000000003"


def _mk_ctrl(root, handle, meta, events=None, pane_alive_name=None):
    ctrl = os.path.join(root, handle)
    os.makedirs(ctrl)
    if meta is not None:
        with open(os.path.join(ctrl, "meta.json"), "w") as f:
            json.dump({"handle": handle, "cwd": "/tmp", "model": "opus",
                       "pane": pane_alive_name or f"vc_{handle[:8]}", "mode": "default",
                       "name": None, **meta}, f)
    if events:
        with open(os.path.join(ctrl, "events.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    return ctrl


class FakeTmux:
    def __init__(self, live, pane_text=""):
        self.live = set(live)
        self.pane_text = pane_text
        self.calls = []

    async def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "list-sessions":
            return 0, "\n".join(sorted(self.live))
        if args[0] == "capture-pane":
            return 0, self.pane_text
        if args[0] == "has-session":
            return (0 if args[2] in self.live else 1), ""
        return 0, ""


class _TmuxCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.patches = [
            mock.patch.object(tmux_runner, "CTRL_ROOT", self.root),
            mock.patch.object(tmux_runner.shutil, "which", lambda n: "/usr/bin/tmux"),
            mock.patch.object(tmux_runner.TmuxClaudeRunner, "_find_transcript",
                              lambda self, s: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class Rehydrate(_TmuxCase):
    async def test_live_pane_restored_dead_pane_gc(self):
        _mk_ctrl(self.root, H1, {"name": "alive"})
        _mk_ctrl(self.root, H2, {"name": "dead"})
        runner = tmux_runner.TmuxClaudeRunner()
        runner._tmux = FakeTmux(live={f"vc_{H1[:8]}"}, pane_text="accept edits on")
        restored = await runner.rehydrate()
        self.assertEqual([r["handle"] for r in restored], [H1])
        self.assertEqual(restored[0]["name"], "alive")
        self.assertEqual(restored[0]["mode"], "acceptEdits")  # from the live footer
        self.assertFalse(os.path.isdir(os.path.join(self.root, H2)))  # gc'd
        self.assertIn(H1, runner._sessions)
        with open(os.path.join(self.root, H1, "mode")) as f:
            self.assertEqual(f.read().strip(), "acceptEdits")  # resynced for the hook
        await runner.shutdown()

    async def test_unreadable_meta_left_alone(self):
        _mk_ctrl(self.root, H3, None)
        runner = tmux_runner.TmuxClaudeRunner()
        runner._tmux = FakeTmux(live={f"vc_{H3[:8]}"})
        self.assertEqual(await runner.rehydrate(), [])
        self.assertTrue(os.path.isdir(os.path.join(self.root, H3)))

    async def test_pending_permission_restored(self):
        _mk_ctrl(self.root, H1, {}, events=[
            {"event": "tool", "tool_name": "Read"},
            {"event": "needs_permission", "tool_name": "Bash",
             "tool_input": {"command": "rm x"}, "tool_use_id": "tu1"},
        ])
        runner = tmux_runner.TmuxClaudeRunner()
        runner._tmux = FakeTmux(live={f"vc_{H1[:8]}"})
        restored = await runner.rehydrate()
        self.assertEqual(restored[0]["status"], "needs_permission")
        s = runner._sessions[H1]
        self.assertEqual(s.pending.kind, "permission")
        self.assertEqual(s.pending.tool_name, "Bash")
        self.assertEqual(s.pending_tool_use_id, "tu1")
        listed = runner.list()[0]
        self.assertEqual(listed["prompt"]["tool_name"], "Bash")
        await runner.shutdown()

    async def test_idempotent(self):
        _mk_ctrl(self.root, H1, {})
        runner = tmux_runner.TmuxClaudeRunner()
        runner._tmux = FakeTmux(live={f"vc_{H1[:8]}"})
        await runner.rehydrate()
        self.assertEqual(await runner.rehydrate(), [])
        await runner.shutdown()

    async def test_no_tmux_binary_returns_empty(self):
        with mock.patch.object(tmux_runner.shutil, "which", lambda n: None):
            runner = tmux_runner.TmuxClaudeRunner()
            self.assertEqual(await runner.rehydrate(), [])


async def _anoop(*a, **kw):
    return None


async def _settled(*a, **kw):
    return "settled pane"


async def _history(*a, **kw):
    return "❯ /context\nTokens: 42\n────────\n❯ "


class TurnCompletionEvents(_TmuxCase):
    """Every path that sets status='completed' must notify the observer.

    ClaudeCodeProvider reports supports_events=True, so SessionService.poll()
    deliberately does NOT re-emit turn completions — the observer is the only
    emitter. A completion the runner never notifies about therefore produces no
    `session.turn_completed` event and no journal line at all, even though the
    row and the mission advance correctly off the return value.
    """

    async def _adopted(self):
        _mk_ctrl(self.root, H1, {"name": "alive"})
        runner = tmux_runner.TmuxClaudeRunner()
        runner._tmux = FakeTmux(live={f"vc_{H1[:8]}"})
        await runner.rehydrate()
        return runner

    async def test_advance_hard_timeout_notifies_turn_complete(self):
        runner = await self._adopted()
        runner._sessions[H1].tools_used = ["Bash"]
        seen = []
        runner.on_event = lambda h, kind, raw: seen.append((h, kind, raw))
        with mock.patch.object(tmux_runner.TmuxClaudeRunner, "ADVANCE_HARD_TIMEOUT_S", 0.01), \
             mock.patch.object(tmux_runner.TmuxClaudeRunner, "_send_message", _anoop), \
             mock.patch.object(tmux_runner.TmuxClaudeRunner, "_read_new_text", _anoop):
            res = await runner.advance(H1, "do a thing")
        self.assertEqual(res.status, "completed")
        turns = [(h, raw) for h, kind, raw in seen if kind == "turn_complete"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0][0], H1)
        self.assertEqual(set(turns[0][1]), {"assistant_text", "tools_used"})
        self.assertEqual(turns[0][1]["assistant_text"], res.assistant_text)
        self.assertEqual(turns[0][1]["tools_used"], ["Bash"])
        await runner.shutdown()

    async def test_ui_only_slash_notifies_turn_complete(self):
        """/context, /model, /permissions, /clear fire no Stop hook — the settle
        detector wins and that branch has to notify by itself."""
        runner = await self._adopted()
        seen = []
        runner.on_event = lambda h, kind, raw: seen.append((h, kind, raw))
        with mock.patch.object(tmux_runner.TmuxClaudeRunner, "_send_message", _anoop), \
             mock.patch.object(tmux_runner.TmuxClaudeRunner, "_wait_for_settle", _settled), \
             mock.patch.object(tmux_runner.TmuxClaudeRunner, "_capture_history", _history):
            res = await runner._run_slash(H1, "/context", 0.01, 0.05)
        self.assertEqual((res.status, res.assistant_text), ("completed", "Tokens: 42"))
        turns = [raw for _, kind, raw in seen if kind == "turn_complete"]
        self.assertEqual(turns, [{"assistant_text": "Tokens: 42", "tools_used": []}])
        await runner.shutdown()

    async def test_an_observer_that_raises_does_not_break_the_turn(self):
        """`_notify` is the safety net under the whole event pipeline: an
        observer bug must never break a turn. Nine call sites depend on it."""
        runner = await self._adopted()

        def boom(handle, kind, raw):
            raise RuntimeError("observer is broken")

        runner.on_event = boom
        with mock.patch.object(tmux_runner.TmuxClaudeRunner, "_send_message", _anoop), \
             mock.patch.object(tmux_runner.TmuxClaudeRunner, "_wait_for_settle", _settled), \
             mock.patch.object(tmux_runner.TmuxClaudeRunner, "_capture_history", _history), \
             self.assertLogs("yapcode.runner", level="ERROR") as logs:
            res = await runner._run_slash(H1, "/context", 0.01, 0.05)
        self.assertEqual((res.status, res.assistant_text), ("completed", "Tokens: 42"))
        self.assertTrue(any("on_event observer failed" in m for m in logs.output))
        await runner.shutdown()

    def test_notify_is_a_noop_without_an_observer(self):
        runner = tmux_runner.TmuxClaudeRunner()
        self.assertIsNone(runner.on_event)
        runner._notify("nope", "turn_complete", {"assistant_text": "x"})   # must not raise


if __name__ == "__main__":
    unittest.main()
