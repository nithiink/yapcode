"""ClaudeCodeProvider forwards to the existing runners and maps their hook
events into ProviderEvents. Runners are stubs — no tmux/SDK."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from provider_contract import AgentProviderContract  # noqa: E402
from yuri.providers.base import ProjectContext, SessionOptions  # noqa: E402
from yuri.providers.claude_code import ClaudeCodeProvider  # noqa: E402


class _StubRunner:
    on_event = None

    def __init__(self, backend):
        self.backend = backend
        self.sessions = {}
        self.calls = []
        self._n = 0

    # Accepts the persona kwargs because BOTH real runners now do (the CLI via
    # --agents/--agent, the SDK via ClaudeAgentOptions(agents=...)). A stub
    # that refused them would be modelling a runner that no longer exists, and
    # the provider deliberately raises rather than dropping a requested
    # persona — so refusing here would only test the refusal path.
    async def start(self, cwd, model=None, mode="default",
                    agent_slug=None, agents_json=None):
        self._n += 1
        h = f"{self.backend}-{self._n}-00000000"
        self.sessions[h] = {"handle": h, "session_id": h, "cwd": cwd, "model": model or "opus",
                            "mode": mode, "status": "idle", "cost_usd": 0.0,
                            "agent_slug": agent_slug, "agents_json": agents_json}
        return h

    async def resume(self, session_id, cwd, model=None, mode="default", name=None):
        self.sessions[session_id] = {"handle": session_id, "session_id": session_id, "cwd": cwd,
                                     "model": "opus", "mode": mode, "status": "idle",
                                     "cost_usd": 0.0}
        return session_id

    def list(self):
        return list(self.sessions.values())

    def start_advance(self, h, m):
        self.calls.append(("advance", h, m))

    def start_answer(self, h, c):
        self.calls.append(("answer", h, c))

    def start_builtin_slash(self, h, t):
        self.calls.append(("slash", h, t))

    def poll_status(self, h):
        if h not in self.sessions:
            raise KeyError(h)
        return {"status": "idle", "session_id": h}

    async def interrupt(self, h):
        self.calls.append(("interrupt", h))

    async def close(self, h):
        self.sessions.pop(h)

    async def set_mode(self, h, mode):
        self.sessions[h]["mode"] = mode
        return mode

    async def read(self, h):
        return "text"

    async def peek(self, h, lines=40):
        return "screen"

    async def send_keys(self, h, items):
        return {"session_id": h, "screen": "x", "sent": items}

    def pane_for(self, h):
        return f"vc_{h[:8]}"

    def persist_name(self, h, name):
        self.calls.append(("persist_name", h, name))

    async def rehydrate(self):
        return []

    async def shutdown(self):
        self.sessions.clear()


def _factory_holder():
    made = {}

    def factory(backend):
        made.setdefault(backend, _StubRunner(backend))
        return made[backend]
    return factory, made


class ClaudeProviderContract(AgentProviderContract):
    def make_provider(self):
        self.factory, self.made = _factory_holder()
        return ClaudeCodeProvider(runner_factory=self.factory)

    def _fire_event(self, handle):
        self.made["cli"].on_event(handle, "turn_complete", {"assistant_text": "done"})


# unittest's TestLoader collects every TestCase subclass reachable at module
# scope, including ones merely imported by name — not just ones defined here.
# Left alone, `AgentProviderContract` would be discovered a second time as its
# own (abstract, unusable) test case. Drop the name once ClaudeProviderContract
# has captured it as a base class; the subclass is unaffected.
del AgentProviderContract


class ClaudeProviderRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.factory, self.made = _factory_holder()
        self.p = ClaudeCodeProvider(runner_factory=self.factory)
        self.ctx = ProjectContext("p1", "/tmp")

    async def test_backend_option_selects_runner(self):
        cli = await self.p.create_session(self.ctx, SessionOptions(backend="cli"))
        sdk = await self.p.create_session(self.ctx, SessionOptions(backend="sdk"))
        self.assertEqual(self.p.backend_of(cli), "cli")
        self.assertEqual(self.p.backend_of(sdk), "sdk")
        self.p.send_message(sdk, "hi")
        self.assertIn(("advance", sdk, "hi"), self.made["sdk"].calls)
        tagged = {s["handle"]: s["backend"] for s in self.p.list_native()}
        self.assertEqual(tagged, {cli: "cli", sdk: "sdk"})

    async def test_sdk_handle_has_no_terminal_features(self):
        sdk = await self.p.create_session(self.ctx, SessionOptions(backend="sdk"))
        self.assertIsNone(self.p.native_pane(sdk))
        with self.assertRaises(NotImplementedError):
            self.p.run_slash(sdk, "/init")
        with self.assertRaises(NotImplementedError):
            await self.p.send_keys(sdk, [{"key": "Escape"}])

    async def test_cli_handle_terminal_features(self):
        cli = await self.p.create_session(self.ctx, SessionOptions(backend="cli"))
        self.assertEqual(self.p.native_pane(cli), f"vc_{cli[:8]}")
        self.p.run_slash(cli, "/init")
        self.assertIn(("slash", cli, "/init"), self.made["cli"].calls)
        self.assertEqual(await self.p.peek(cli), "screen")

    async def test_resume_registers_cli_owner(self):
        h = await self.p.resume("abcdefab-1111-2222-3333-444444444444", self.ctx,
                                SessionOptions(name="n"))
        self.assertEqual(self.p.backend_of(h), "cli")

    async def test_persist_name_reaches_the_owning_runner(self):
        """The CLI runner writes the display name into meta.json so it survives
        a restart. Coverage moved here from test_session_manager's deleted
        `set_session_name` shim — the provider owns this forwarding now."""
        cli = await self.p.create_session(self.ctx, SessionOptions(backend="cli"))
        sdk = await self.p.create_session(self.ctx, SessionOptions(backend="sdk"))
        self.p.persist_name(cli, "Billing Fix")
        self.assertIn(("persist_name", cli, "Billing Fix"), self.made["cli"].calls)
        self.p.persist_name(sdk, "Other")
        self.assertNotIn(("persist_name", sdk, "Other"), self.made["cli"].calls)
        with self.assertRaises(KeyError):
            self.p.persist_name("nope", "x")

    async def test_event_mapping(self):
        got = []
        self.p.set_observer(lambda h, ev: got.append((h, ev.kind, ev.payload)))
        cli = await self.p.create_session(self.ctx, SessionOptions())
        r = self.made["cli"]
        r.on_event(cli, "tool", {"tool_name": "Read", "tool_input": {"file_path": "x"}})
        r.on_event(cli, "needs_permission", {"request_id": "r1", "tool_name": "Bash",
                                             "tool_input": {"command": "rm x"},
                                             "text": "run rm x"})
        r.on_event(cli, "needs_choice", {"request_id": "r2", "text": "pick", "options": ["a"],
                                         "multi_select": False})
        r.on_event(cli, "turn_complete", {"assistant_text": "x" * 3000, "tools_used": ["Read"]})
        r.on_event(cli, "cost", {"cost_usd": 0.5, "model": "opus"})
        r.on_event(cli, "error", {"message": "boom"})
        r.on_event(cli, "unknown_kind", {})
        kinds = [k for _, k, _ in got]
        self.assertEqual(kinds, ["tool_started", "needs_permission", "needs_choice",
                                 "turn_completed", "cost_updated", "error"])
        self.assertEqual(got[1][2]["request_id"], "r1")
        self.assertEqual(got[1][2]["options"], ["allow", "deny"])
        self.assertEqual(len(got[3][2]["assistant_text"]), 2000)
        self.assertIsNone(got[4][2]["input_tokens"])


if __name__ == "__main__":
    unittest.main()
