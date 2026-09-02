import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from provider_contract import AgentProviderContract  # noqa: E402
from yuri.providers.base import ProviderEvent  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


class FakeProviderContract(AgentProviderContract):
    def make_provider(self):
        return FakeAgentProvider()

    def _fire_event(self, handle):
        self.p.emit(handle, ProviderEvent("turn_completed", {"assistant_text": "done"}))

    async def test_scripted_poll_is_consumed_in_order(self):
        h = await self.p.create_session(self.ctx, self.opts())
        self.p.script(h, {"status": "needs_permission"})
        self.p.script(h, {"status": "completed", "assistant_text": "ok"})
        self.assertEqual(self.p.poll(h)["status"], "needs_permission")
        self.assertEqual(self.p.poll(h)["status"], "completed")
        self.assertEqual(self.p.poll(h)["status"], "idle")


# unittest's TestLoader collects every TestCase subclass reachable at module
# scope, including ones merely imported by name — not just ones defined here.
# Left alone, `AgentProviderContract` would be discovered a second time as its
# own (abstract, unusable) test case. Drop the name once FakeProviderContract
# has captured it as a base class; the subclass is unaffected.
del AgentProviderContract


if __name__ == "__main__":
    unittest.main()
