"""`ClaudeRunner._notify` — the guarantee that an observer bug never breaks a turn.

This is the safety net under the whole event pipeline: seven call sites in
claude_runner and nine more in tmux_runner push events through it, and the
provider's observer fans them out to the EventBus, SSE subscribers and
narration. If a raising observer propagated, a bug in narration wording would
abort the user's actual work.

It was covered by no test. The provider suites use a duck-typed stub runner
that never inherits this method, so `_notify` and its call sites were exercised
only indirectly, if at all.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from claude_runner import ClaudeRunner  # noqa: E402


class _Runner(ClaudeRunner):
    """The real _notify, with the abstract surface stubbed out — the point is
    to exercise the inherited method, not a copy of it."""

    async def start(self, cwd, model=None, mode="default"): return "h"
    async def advance(self, handle, message): ...
    async def answer(self, handle, choice, seq): ...
    async def interrupt(self, handle): ...
    async def stop(self, handle): ...
    async def read(self, handle): return ""
    def list(self): return []
    async def shutdown(self): ...
    async def close(self, handle): ...
    def poll_status(self, handle): return {}
    async def set_mode(self, handle, mode): return mode
    async def start_advance(self, handle, message): ...
    async def start_answer(self, handle, choice, seq): ...


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.r = _Runner()

    def test_no_observer_is_a_no_op(self):
        self.r.on_event = None
        self.r._notify("h", "tool", {"tool_name": "Bash"})   # must not raise

    def test_the_observer_receives_handle_kind_and_payload(self):
        seen = []
        self.r.on_event = lambda *a: seen.append(a)
        self.r._notify("h1", "turn_completed", {"assistant_text": "done"})
        self.assertEqual(seen, [("h1", "turn_completed", {"assistant_text": "done"})])

    def test_a_raising_observer_never_breaks_the_turn(self):
        """The guarantee. A narration bug must not abort the user's work."""
        def boom(handle, kind, raw):
            raise RuntimeError("observer bug")

        self.r.on_event = boom
        with self.assertLogs("yapcode.runner", level="ERROR") as cm:
            self.r._notify("h", "error", {"message": "x"})   # must not raise
        self.assertTrue(any("observer failed" in line for line in cm.output))

    def test_a_raising_observer_does_not_stop_the_next_event(self):
        """One bad event must not poison the stream behind it."""
        seen = []

        def flaky(handle, kind, raw):
            seen.append(kind)
            if kind == "bad":
                raise RuntimeError("observer bug")

        self.r.on_event = flaky
        logging.getLogger("yapcode.runner").setLevel(logging.CRITICAL)
        try:
            self.r._notify("h", "bad", {})
            self.r._notify("h", "good", {})
        finally:
            logging.getLogger("yapcode.runner").setLevel(logging.NOTSET)
        self.assertEqual(seen, ["bad", "good"])

    def test_a_non_callable_observer_is_survived(self):
        """Defensive: on_event is a plain attribute, so anything can be
        assigned to it. A TypeError here would be indistinguishable from an
        observer bug, and must be just as harmless."""
        self.r.on_event = "not callable"          # type: ignore[assignment]
        logging.getLogger("yapcode.runner").setLevel(logging.CRITICAL)
        try:
            self.r._notify("h", "tool", {})       # must not raise
        finally:
            logging.getLogger("yapcode.runner").setLevel(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
