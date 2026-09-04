"""Snapshot of every voice tool's result contract, taken BEFORE the provider /
service refactor. If a later task changes a key here, that is a user-visible
regression for the voice model — fix the code, not this file.

Runner is a stub injected into the container's ClaudeCodeProvider; no
tmux/Claude, a temp Yuri home and a temp DB.

Two assertions were legitimately widened when tools.py moved onto
SessionService (Task 17): start_session and list_sessions now also report the
Yuri mission/session ids. Every other key here is byte-identical to the
pre-refactor contract.

    python -m unittest discover -s backend/tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import session_manager as sm  # noqa: E402
import tools  # noqa: E402
from yuri import app as yapp  # noqa: E402


class _StubRunner:
    """Mimics ClaudeRunner's surface with recorded calls and canned answers."""

    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.next_poll = {"status": "idle"}
        self.persisted = {}

    async def start(self, cwd, model=None, mode="default"):
        h = f"h{len(self.sessions) + 1}-" + "0" * 8
        self.sessions[h] = {"handle": h, "session_id": h, "cwd": cwd, "model": model or "opus",
                            "mode": mode, "status": "idle", "cost_usd": 0.0, "queued": 0}
        self.calls.append(("start", cwd, model, mode))
        return h

    def list(self):
        return list(self.sessions.values())

    def start_advance(self, h, msg):
        self.calls.append(("advance", h, msg))
        self.sessions[h]["status"] = "working"

    def start_answer(self, h, choice):
        self.calls.append(("answer", h, choice))

    def start_builtin_slash(self, h, text):
        self.calls.append(("slash", h, text))

    def poll_status(self, h):
        return {**self.next_poll, "session_id": h}

    async def interrupt(self, h):
        self.calls.append(("interrupt", h))

    async def close(self, h):
        self.calls.append(("close", h))
        self.sessions.pop(h)

    async def set_mode(self, h, mode):
        self.sessions[h]["mode"] = mode
        return mode

    async def read(self, h):
        return "assistant text"

    async def peek(self, h, lines=40):
        return "screen"

    async def send_keys(self, h, items):
        return {"session_id": h, "screen": "after keys", "sent": items}

    def pane_for(self, h):
        return f"vc_{h[:8]}"

    def persist_name(self, h, name):
        self.persisted[h] = name

    async def shutdown(self):
        pass


class ToolsDispatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = _StubRunner()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "proj"))
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root}),
                        mock.patch.object(config, "YURI_HOME", os.path.join(self.root, "Yuri"))]
        for p in self.patches:
            p.start()
        from yuri.providers.claude_code import ClaudeCodeProvider
        # test_container installs the container AND hands the provider to
        # session_manager's shims — one provider, one stub runner.
        self.c = yapp.test_container(os.path.join(self.root, "Yuri"),
                                     ClaudeCodeProvider(runner_factory=lambda b: self.runner),
                                     default_agent="claude-code")
        tools._last_start = None

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        sm.reset()
        for p in self.patches:
            p.stop()
        tools._last_start = None
        self.tmp.cleanup()

    async def _start(self, **kw):
        return await tools.dispatch_tool("start_session", {"project_path": "proj", **kw})

    def test_every_definition_has_name_and_object_params(self):
        for d in tools.TOOL_DEFINITIONS:
            self.assertEqual(d["type"], "function")
            self.assertEqual(d["parameters"]["type"], "object")
        names = [d["name"] for d in tools.TOOL_DEFINITIONS]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("poll_session", names)  # app-level only

    async def test_list_projects_keys(self):
        out = await tools.dispatch_tool("list_projects", {})
        self.assertEqual(set(out), {"roots", "projects"})

    async def test_start_session_keys_and_default_name(self):
        out = await self._start()
        self.assertEqual(set(out), {"session_id", "name", "project_path", "backend", "mode",
                                    "message", "mission_id", "yuri_session_id"})
        self.assertEqual(out["name"], "proj")
        self.assertEqual(out["backend"], "cli")
        self.assertEqual(out["project_path"], os.path.join(self.root, "proj"))

    def test_start_session_declares_the_agent_parameter(self):
        """The voice model can only pass parameters the schema declares, so
        without this the prompt's "use OpenCode" instruction would be dropped
        and a claude-code session started instead — a silent agent switch."""
        d = next(x for x in tools.TOOL_DEFINITIONS if x["name"] == "start_session")
        agent = d["parameters"]["properties"]["agent"]
        self.assertEqual(agent["type"], "string")
        self.assertIn("opencode", agent["description"])

    async def test_start_session_forwards_the_requested_agent(self):
        """`agent` must reach AgentRouter. If it were dropped, this call would
        succeed on claude-code instead of failing."""
        with self.assertRaises(ValueError) as cm:
            await self._start(agent="not-an-agent")
        msg = str(cm.exception)
        self.assertIn("not-an-agent", msg)
        self.assertIn("claude-code", msg)   # names what IS registered
        # A failed start must not arm the duplicate guard either.
        self.assertIsNone(tools._last_start)
        self.assertNotIn("duplicate_guard", await self._start())

    async def test_start_session_duplicate_guard(self):
        first = await self._start()
        second = await self._start()
        self.assertTrue(second["duplicate_guard"])
        self.assertEqual(second["existing_session"]["session_id"], first["session_id"])
        third = await self._start(another=True)
        self.assertNotIn("duplicate_guard", third)
        self.assertEqual(third["name"], "proj 2")

    async def test_list_sessions_keys(self):
        await self._start()
        out = await tools.dispatch_tool("list_sessions", {})
        s = out["sessions"][0]
        for k in ["handle", "session_id", "cwd", "model", "mode", "status", "cost_usd",
                  "backend", "name", "agent_id", "mission_id", "yuri_session_id"]:
            self.assertIn(k, s)

    async def test_tell_claude_returns_working(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("tell_claude", {"session_id": sid, "message": "hi"})
        self.assertEqual(out, {"status": "working", "session_id": sid})
        self.assertIn(("advance", sid, "hi"), self.runner.calls)

    async def test_session_id_falls_back_to_sole_session(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("tell_claude", {"message": "hi"})
        self.assertEqual(out["session_id"], sid)

    async def test_no_sessions_is_soft_error(self):
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("tell_claude", {"message": "hi"})

    async def test_ambiguous_session_is_soft_error_listing_names(self):
        await self._start()
        await self._start(another=True, name="second")
        with self.assertRaises(ValueError) as cm:
            await tools.dispatch_tool("tell_claude", {"message": "hi"})
        self.assertIn("second", str(cm.exception))

    async def test_unknown_session_is_soft_error(self):
        await self._start()
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("tell_claude", {"session_id": "nope", "message": "x"})

    async def test_answer_prompt(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("answer_prompt", {"session_id": sid, "choice": "allow"})
        self.assertEqual(out, {"status": "working", "session_id": sid})
        self.assertIn(("answer", sid, "allow"), self.runner.calls)

    async def test_poll_session_forwards(self):
        sid = (await self._start())["session_id"]
        self.runner.next_poll = {"status": "completed", "assistant_text": "done"}
        out = await tools.dispatch_tool("poll_session", {"session_id": sid})
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["session_id"], sid)

    async def test_interrupt_and_close(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("interrupt_session", {"session_id": sid})
        self.assertEqual(out, {"status": "interrupted", "session_id": sid})
        out = await tools.dispatch_tool("close_session", {"session_id": sid})
        self.assertEqual(out, {"status": "closed", "session_id": sid})
        self.assertEqual(await tools.dispatch_tool("list_sessions", {}), {"sessions": []})

    async def test_rename(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("rename_session", {"session_id": sid, "name": "Neo"})
        self.assertEqual(set(out), {"session_id", "name", "message"})
        self.assertEqual(out["name"], "Neo")
        out = await tools.dispatch_tool("tell_claude", {"session_id": "neo", "message": "x"})
        self.assertEqual(out["session_id"], sid)

    async def test_set_mode_without_prompt(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("set_mode", {"session_id": sid, "mode": "plan"})
        self.assertEqual(out, {"session_id": sid, "mode": "plan"})

    async def test_read_and_peek(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("read_session", {"session_id": sid})
        self.assertEqual(out, {"session_id": sid, "text": "assistant text"})
        out = await tools.dispatch_tool("peek_screen", {"session_id": sid})
        self.assertEqual(out["screen"], "screen")
        self.assertEqual(out["session_id"], sid)

    async def test_get_handoff_keys(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("get_handoff", {"session_id": sid})
        self.assertEqual(set(out), {"session_id", "name", "cwd", "attach_command",
                                    "resume_command", "command"})
        self.assertTrue(out["attach_command"].startswith("tmux attach -t vc_"))
        self.assertIn("claude --resume", out["resume_command"])

    async def test_send_keys(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("send_keys", {"session_id": sid,
                                                      "items": [{"key": "Escape"}]})
        self.assertEqual(out["screen"], "after keys")
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("send_keys", {"session_id": sid, "items": []})

    async def test_run_slash_command(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("run_slash_command",
                                        {"session_id": sid, "command": "/init", "args": "x"})
        self.assertEqual(out, {"status": "working", "session_id": sid, "sent": "/init x"})
        self.assertIn(("slash", sid, "/init x"), self.runner.calls)

    async def test_list_slash_commands_keys(self):
        out = await tools.dispatch_tool("list_slash_commands", {})
        self.assertIn("commands", out)

    async def test_unknown_tool(self):
        with self.assertRaises(KeyError):
            await tools.dispatch_tool("nope", {})


if __name__ == "__main__":
    unittest.main()


class CancelMissionNeedsConfirmingTests(ToolsDispatch):
    """Cancelling ends work and stops running agents.

    The only thing standing between a misheard phrase and that happening used
    to be a sentence in the tool's description telling the model to confirm.
    A prompt instruction is not a guard — this is the same reasoning that kept
    mission DELETE off the voice surface entirely.

    Found in the field: a mission the user never named was cancelled `by:
    "voice"` two seconds after an unrelated one was cancelled from the UI.
    """

    def setUp(self):
        super().setUp()
        tools._pending_confirm = None

    def tearDown(self):
        tools._pending_confirm = None
        super().tearDown()

    async def _mission(self, title="billing fix"):
        # Clear the duplicate guard: it redirects a second start within
        # START_GUARD_SECS to the first, and these tests deliberately want two
        # distinct missions in quick succession.
        tools._last_start = None
        out = await tools.dispatch_tool("start_session",
                                        {"project_path": "proj", "name": title})
        return out["mission_id"]

    async def test_the_first_call_never_cancels(self):
        mid = await self._mission()
        out = await tools.dispatch_tool("cancel_mission", {"mission": "billing fix"})
        self.assertIs(out["cancelled"], False)
        self.assertIn("confirm", out)
        self.assertNotEqual(self.c.missions.get(mid).status, "cancelled",
                            "one call cancelled the mission")

    async def test_the_first_call_says_what_would_happen(self):
        await self._mission()
        out = await tools.dispatch_tool("cancel_mission", {"mission": "billing fix"})
        self.assertIn("billing fix", out["message"])
        self.assertIn("Nothing has been cancelled yet", out["message"])

    async def test_a_second_call_with_the_token_cancels(self):
        mid = await self._mission()
        armed = await tools.dispatch_tool("cancel_mission", {"mission": "billing fix"})
        out = await tools.dispatch_tool("cancel_mission",
                                                            {"mission": "billing fix", "confirm": armed["confirm"]})
        self.assertIs(out["cancelled"], True)
        self.assertEqual(self.c.missions.get(mid).status, "cancelled")

    async def test_an_invented_token_is_refused(self):
        mid = await self._mission()
        await tools.dispatch_tool("cancel_mission", {"mission": "billing fix"})
        out = await tools.dispatch_tool("cancel_mission",
                                                            {"mission": "billing fix", "confirm": "deadbeef"})
        self.assertIs(out["cancelled"], False)
        self.assertNotEqual(self.c.missions.get(mid).status, "cancelled")

    async def test_a_wrong_guess_burns_the_arm(self):
        # Single use, consumed whether or not it matched: otherwise a model
        # could guess repeatedly against one still-valid arm.
        mid = await self._mission()
        armed = await tools.dispatch_tool("cancel_mission", {"mission": "billing fix"})
        await tools.dispatch_tool("cancel_mission", {"mission": "billing fix", "confirm": "nope"})
        out = await tools.dispatch_tool("cancel_mission",
                                                            {"mission": "billing fix", "confirm": armed["confirm"]})
        self.assertIs(out["cancelled"], False, "the burned token still worked")
        self.assertNotEqual(self.c.missions.get(mid).status, "cancelled")

    async def test_a_token_armed_for_one_mission_cannot_cancel_another(self):
        # The exact shape of the reported bug: the wrong mission going down.
        first = await self._mission("billing fix")
        second = await self._mission("docs pass")
        armed = await tools.dispatch_tool("cancel_mission", {"mission": "billing fix"})
        out = await tools.dispatch_tool("cancel_mission",
                                                            {"mission": "docs pass", "confirm": armed["confirm"]})
        self.assertIs(out["cancelled"], False)
        self.assertNotEqual(self.c.missions.get(second).status, "cancelled")
        self.assertNotEqual(self.c.missions.get(first).status, "cancelled")

    async def test_a_stale_arm_expires(self):
        import tools as tools_mod
        mid = await self._mission()
        armed = await tools.dispatch_tool("cancel_mission", {"mission": "billing fix"})
        tools_mod._pending_confirm["ts"] -= tools_mod.CONFIRM_SECS + 1
        out = await tools.dispatch_tool("cancel_mission",
                                                            {"mission": "billing fix", "confirm": armed["confirm"]})
        self.assertIs(out["cancelled"], False)
        self.assertNotEqual(self.c.missions.get(mid).status, "cancelled")

    async def test_pause_and_resume_are_untouched_and_still_single_call(self):
        # The guard is for the destructive one only. Making pause two-step
        # would be friction with nothing to protect.
        mid = await self._mission()
        out = await tools.dispatch_tool("pause_mission", {"mission": "billing fix"})
        self.assertEqual(self.c.missions.get(mid).status, "paused")
        self.assertNotIn("confirm", out)


class TheTierGateIsEnforcedNotJustDeclaredTests(ToolsDispatch):
    """`tier` has to mean something.

    Borrowed from project-yuri, which declares a permissionTier on all ~35 of
    its tools and enforces it nowhere: the whole gate there is a console.log
    reminding the daemon that the model *should* have asked
    (apps/daemon/src/agents/tool-agent.ts:728-732). A declaration that reads as
    protection and isn't is worse than no declaration, and it is the exact
    mechanism that let Yuri cancel a mission nobody named.
    """

    def setUp(self):
        super().setUp()
        tools._pending_confirm = None

    def tearDown(self):
        tools._pending_confirm = None
        super().tearDown()

    def test_every_tool_declares_a_known_tier(self):
        for d in tools.TOOL_DEFINITIONS:
            tier = d.get("tier", "safe")
            self.assertIn(tier, ("safe", "confirm"), d["name"])

    def test_the_destructive_tool_is_the_confirm_tier_one(self):
        # If this list grows, that is a decision someone should have to make
        # deliberately — the tier is where irreversibility gets declared.
        self.assertEqual(tools.confirm_tools(), ["cancel_mission"])

    def test_tier_of_defaults_to_safe_including_for_an_unknown_name(self):
        self.assertEqual(tools.tier_of("list_projects"), "safe")
        self.assertEqual(tools.tier_of("no_such_tool"), "safe")

    async def test_a_confirm_tool_that_skips_the_gate_raises(self):
        """The central half. A confirm-tier tool whose handler forgets to
        consult the gate must fail loudly, not run ungated — a silent version
        of this bug is the one that ships."""
        original = tools.TOOL_DEFINITIONS

        async def ungated(name, args):
            return {"ok": "ran without asking anyone"}

        with mock.patch.object(tools, "_dispatch", ungated):
            with self.assertRaises(AssertionError) as ctx:
                await tools.dispatch_tool("cancel_mission", {})
        self.assertIn("cancel_mission", str(ctx.exception))
        self.assertIn("confirm", str(ctx.exception))
        self.assertIs(tools.TOOL_DEFINITIONS, original)

    async def test_a_safe_tool_needs_no_gate_and_is_not_flagged(self):
        # The enforcement must not turn every tool into a two-step.
        out = await tools.dispatch_tool("list_projects", {})
        self.assertIsInstance(out, dict)

    async def test_the_gate_is_reset_per_call_so_one_consult_cannot_cover_two(self):
        # Without the per-call reset, a confirm tool consulted once would leave
        # the flag set and the NEXT ungated call would pass the check.
        mid = await self._mission("billing fix")
        await tools.dispatch_tool("cancel_mission", {"mission": "billing fix"})

        async def ungated(name, args):
            return {"ok": True}

        with mock.patch.object(tools, "_dispatch", ungated):
            with self.assertRaises(AssertionError):
                await tools.dispatch_tool("cancel_mission", {})
        self.assertNotEqual(self.c.missions.get(mid).status, "cancelled")

    async def test_an_arm_for_one_tool_cannot_be_spent_on_another(self):
        # The gate is keyed on (tool, target). Keying it on the target alone
        # would let a token armed by one tool authorise a different one.
        mid = await self._mission("billing fix")
        armed = await tools.dispatch_tool("cancel_mission", {"mission": "billing fix"})
        # The gate returns None to mean "proceed" and a fresh token to mean
        # "refused, read this back". A token armed by cancel_mission must not
        # authorise a different tool acting on the same target.
        refused = tools._confirm_gate("some_other_tool", mid, armed["confirm"])
        self.assertIsNotNone(refused,
                             "a token armed by cancel_mission authorised another tool")
        self.assertNotEqual(self.c.missions.get(mid).status, "cancelled")

    async def _mission(self, title="billing fix"):
        tools._last_start = None
        out = await tools.dispatch_tool("start_session",
                                        {"project_path": "proj", "name": title})
        return out["mission_id"]
