"""Regression tests for set_mode not hanging on a busy session.

A session that's mid-turn holds its _turn_lock for the whole turn (up to
ADVANCE_HARD_TIMEOUT_S). set_mode used to `async with s._turn_lock` blindly, so
switching mode while Claude was working parked the call — and with it the voice
agent — for up to minutes. It now fails fast with the lock busy instead:

    python -m unittest discover -s backend/tests
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tmux_runner import TmuxClaudeRunner, _TmuxSession  # noqa: E402


def _runner_with_session():
    r = TmuxClaudeRunner(default_model="opus")
    r.SET_MODE_LOCK_TIMEOUT_S = 0.05  # keep the test fast
    s = _TmuxSession("aaaaaaaa-0000-0000-0000-000000000000", "/tmp", "opus")
    r._sessions[s.handle] = s
    return r, s


def _patch_terminal(r, target):
    """Stub the tmux/terminal I/O so a free-lock switch runs without a real CLI;
    _detect_mode echoes the target so the switch is a deterministic no-op."""
    async def _noop_tmux(*a, **k):
        return ""

    async def _alive(_s):
        return True

    async def _capture(_s):
        return ""

    r._tmux = _noop_tmux
    r._alive = _alive
    r._capture = _capture
    r._detect_mode = lambda _pane: target
    r._write_mode = lambda _s: None


class SetModeBusy(unittest.IsolatedAsyncioTestCase):
    async def test_busy_session_fails_fast_without_changing_mode(self):
        r, s = _runner_with_session()
        await s._turn_lock.acquire()  # simulate an in-progress turn holding it
        try:
            out = await asyncio.wait_for(r.set_mode(s.handle, "auto"), timeout=1.0)
            self.assertIn("busy", out.lower())
            self.assertEqual(s.mode, "default")  # unchanged
            self.assertTrue(s._turn_lock.locked())  # still ours; set_mode didn't steal it
        finally:
            s._turn_lock.release()

    async def test_idle_session_switches_and_releases_lock(self):
        r, s = _runner_with_session()
        _patch_terminal(r, "auto")
        out = await r.set_mode(s.handle, "auto")
        self.assertEqual(out, "auto")
        self.assertEqual(s.mode, "auto")
        self.assertFalse(s._turn_lock.locked())  # released on the way out


if __name__ == "__main__":
    unittest.main()
