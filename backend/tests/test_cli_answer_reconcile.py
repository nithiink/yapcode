"""Regression tests for retiring a prompt the user answered directly in the CLI.

AskUserQuestion menus and the ExitPlanMode dialog render in the live TUI, so the
user can answer them with the keyboard — and then no voice answer() fires to
clear `s.pending`, leaving list()/poll_status reporting a prompt the user already
dealt with (a stale card in the UI). TmuxClaudeRunner._reconcile_pending watches
the pane and drops such a prompt once its menu/dialog leaves the screen.

Risky-tool permission prompts must NOT be reconciled this way: their PreToolUse
hook parks with no on-screen menu, so an empty pane is not an external answer.

Requires the backend deps (claude_agent_sdk) — run with the project venv:
    backend/.venv/bin/python -m unittest discover -s backend/tests
Skips cleanly if the SDK isn't importable.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tmux_runner
    from claude_runner import Prompt
except ModuleNotFoundError as e:  # pragma: no cover - env-dependent
    print(f"SKIP test_cli_answer_reconcile: {e} (run with the backend venv)")
    sys.exit(0)


MENU_PANE = "Pick one\n  1. yes\n  2. no\n  Enter to select · ↑/↓ to navigate"
PLAN_PANE = "Plan ready to execute. Would you like to proceed?\n  1. Yes\n  2. No"
BLANK_PANE = "❯ \n(idle prompt box)"


def _runner_with_session(prompt: Prompt, status: str):
    runner = tmux_runner.TmuxClaudeRunner()
    s = tmux_runner._TmuxSession("h1", cwd="/tmp", model="opus")
    s.pending = prompt
    s.pending_tool_use_id = "tu-1"
    s.status = status
    runner._sessions[s.handle] = s
    return runner, s


def _capture_returns(runner, *panes):
    """Make _capture return `panes` in order (last value repeats)."""
    seq = list(panes)

    async def fake_capture(_s):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    runner._capture = fake_capture


class CliAnswerReconcile(unittest.IsolatedAsyncioTestCase):
    async def test_question_cleared_after_menu_leaves_screen(self):
        runner, s = _runner_with_session(
            Prompt(kind="choice", text="pick one", options=["yes", "no"],
                   tool_name="AskUserQuestion"),
            "needs_choice")
        _capture_returns(runner, MENU_PANE, BLANK_PANE, BLANK_PANE)
        await runner._reconcile_pending(s)        # menu on screen -> seen
        self.assertIsNotNone(s.pending)
        await runner._reconcile_pending(s)        # gone (strike 1) -> still held
        self.assertIsNotNone(s.pending)
        await runner._reconcile_pending(s)        # gone (strike 2) -> cleared
        self.assertIsNone(s.pending)
        self.assertIsNone(s.pending_tool_use_id)
        self.assertEqual(s.status, "running")

    async def test_not_cleared_before_menu_renders(self):
        # The hook fired needs_choice but the TUI hasn't drawn the menu yet; an
        # empty pane must not be mistaken for an answer.
        runner, s = _runner_with_session(
            Prompt(kind="choice", text="pick one", options=["yes"],
                   tool_name="AskUserQuestion"),
            "needs_choice")
        _capture_returns(runner, BLANK_PANE)
        for _ in range(5):
            await runner._reconcile_pending(s)
        self.assertIsNotNone(s.pending)

    async def test_single_offscreen_poll_does_not_clear(self):
        # One stray capture caught mid-redraw must not retire the prompt.
        runner, s = _runner_with_session(
            Prompt(kind="choice", text="pick one", options=["yes"],
                   tool_name="AskUserQuestion"),
            "needs_choice")
        _capture_returns(runner, MENU_PANE, BLANK_PANE, MENU_PANE)
        await runner._reconcile_pending(s)        # seen
        await runner._reconcile_pending(s)        # gone once (strike 1)
        await runner._reconcile_pending(s)        # back on screen -> strikes reset
        self.assertIsNotNone(s.pending)
        self.assertEqual(s._pending_gone_strikes, 0)

    async def test_risky_permission_never_reconciled(self):
        # A parked-hook permission has no on-screen menu; the empty pane must
        # never clear it (only voice/mode/interrupt resolve it).
        runner, s = _runner_with_session(
            Prompt(kind="permission", text="run Bash", options=["allow", "deny"],
                   tool_name="Bash"),
            "needs_permission")
        _capture_returns(runner, BLANK_PANE)
        for _ in range(5):
            await runner._reconcile_pending(s)
        self.assertIsNotNone(s.pending)
        self.assertEqual(s.status, "needs_permission")

    async def test_exit_plan_dialog_cleared(self):
        runner, s = _runner_with_session(
            Prompt(kind="permission", text="exit plan mode", options=["allow", "deny"],
                   tool_name="ExitPlanMode"),
            "needs_permission")
        _capture_returns(runner, PLAN_PANE, BLANK_PANE, BLANK_PANE)
        await runner._reconcile_pending(s)
        await runner._reconcile_pending(s)
        await runner._reconcile_pending(s)
        self.assertIsNone(s.pending)

    async def test_skipped_while_turn_lock_held(self):
        # A voice answer() holds _turn_lock while driving the menu; its transient
        # disappearance must not be read as a CLI answer.
        runner, s = _runner_with_session(
            Prompt(kind="choice", text="pick one", options=["yes"],
                   tool_name="AskUserQuestion"),
            "needs_choice")
        _capture_returns(runner, MENU_PANE, BLANK_PANE, BLANK_PANE)
        await s._turn_lock.acquire()
        try:
            for _ in range(5):
                await runner._reconcile_pending(s)
        finally:
            s._turn_lock.release()
        self.assertIsNotNone(s.pending)


if __name__ == "__main__":
    unittest.main()
