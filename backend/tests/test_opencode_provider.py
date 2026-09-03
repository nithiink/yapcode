"""OpenCode against the shared provider contract — the test that makes "the
AgentProvider abstraction works" a result rather than a claim, since the same
assertions already pass for the fake and for Claude Code.

Plus the properties the cursor exists to give: a completed turn is reported
exactly once, one turn's reply never leaks into the next turn's completion,
and an event type we have never seen is ignored rather than fatal (design
spec section 2.1).

And one property that is not about the cursor at all: `health()` is a pure
probe. A UI that merely renders an agent list polls it every 30s, so a health
check that acquired the server would spawn `opencode serve` for a user who
never asked for a session — destroying the laziness Task 3 exists to give.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from fake_opencode import FakeOpenCode  # noqa: E402
from provider_contract import AgentProviderContract  # noqa: E402
from yuri.providers.base import ProjectContext, SessionOptions  # noqa: E402
from yuri.providers.opencode.client import OpenCodeError  # noqa: E402
from yuri.providers.opencode.provider import OpenCodeProvider  # noqa: E402
from yuri.providers.opencode.server import OpenCodeServer  # noqa: E402

UNREACHABLE = "http://127.0.0.1:1"      # nothing listens on port 1


class _Base(unittest.IsolatedAsyncioTestCase):
    """Boots a fake OpenCode and a provider attached to it."""

    async def asyncSetUp(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.addCleanup(lambda: self.fake.__exit__(None, None, None))
        self.server = OpenCodeServer(self.fake.url, spawn=False)
        self.p = OpenCodeProvider(self.server)
        self.addAsyncCleanup(self.p.shutdown)

    async def _session(self, root: str = "/tmp") -> str:
        return await self.p.create_session(ProjectContext("p", root), SessionOptions())


class OpenCodeProviderContract(AgentProviderContract):
    """The shared contract, against a fake OpenCode server."""

    def make_provider(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.addCleanup(lambda: self.fake.__exit__(None, None, None))
        return OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))

    # supports_events is False, so the contract never calls _fire_event.


# unittest's TestLoader collects every TestCase subclass reachable at module
# scope, including ones merely imported by name and ones only meant as bases.
# Left alone, `AgentProviderContract` would be discovered a second time as its
# own (abstract, unusable) test case, and `_Base` as an empty one. Drop both
# names once the subclasses below have captured them; the subclasses keep
# their bases through __bases__ and are unaffected.


class Cursor(_Base):
    async def test_a_completed_turn_is_reported_exactly_once(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_event(h, "session.next.prompted")
        self.fake.state.push_assistant(h, "I did it.")
        self.fake.state.push_event(h, "session.next.step.completed")
        first = self.p.poll(h)
        self.assertEqual(first["status"], "completed")
        self.assertIn("I did it.", first["assistant_text"])
        self.assertEqual(first["session_id"], h)
        # Polling again must not re-report the turn.
        self.assertEqual(self.p.poll(h)["status"], "idle")

    async def test_the_cursor_advances_only_past_events_actually_returned(self):
        """The cursor is what makes an EVENT reported exactly once, proved on
        the error path — the only event type the live probe actually mapped. A
        frozen cursor would re-read, and so re-narrate, the same failure on
        every poll forever; a cursor that ran ahead would swallow the next one.

        (Completion is a separate mechanism: it is read from /message, which
        carries no seq, so `msg_seen` and the in-flight flag are what give a
        completed turn its exactly-once — not this cursor. See
        test_a_second_turn_never_re_reports_the_first_turns_reply.)
        """
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_event(h, "session.next.step.failed",
                                   {"error": {"message": "first failure"}})
        self.assertIn("first failure", self.p.poll(h)["error"])
        for _ in range(3):
            self.assertEqual(self.p.poll(h)["status"], "idle",
                             "the cursor did not advance past the failure")
        # And it did not run ahead: a genuinely new failure is still reported.
        self.p.send_message(h, "again")
        self.fake.state.push_event(h, "session.next.step.failed",
                                   {"error": {"message": "second failure"}})
        again = self.p.poll(h)
        self.assertEqual(again["status"], "error")
        self.assertIn("second failure", again["error"])

    async def test_a_second_turn_never_re_reports_the_first_turns_reply(self):
        """Completion comes from /message, which has no cursor of its own — so
        without a per-handle high-water mark the *previous* turn's finished
        assistant message would be found the instant the next turn's first
        event arrived, and reported as that turn's completion."""
        h = await self._session()
        self.p.send_message(h, "one")
        self.fake.state.push_assistant(h, "first reply")
        self.assertIn("first reply", self.p.poll(h)["assistant_text"])

        self.p.send_message(h, "two")
        res = self.p.poll(h)
        self.assertEqual(res["status"], "working",
                         "the first turn's reply was re-reported as the second turn's")
        self.fake.state.push_assistant(h, "second reply")
        done = self.p.poll(h)
        self.assertEqual(done["status"], "completed")
        self.assertIn("second reply", done["assistant_text"])
        self.assertNotIn("first reply", done["assistant_text"])

    async def test_an_unfinished_message_is_not_a_completion_and_is_not_lost(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_assistant(h, "still typing", finish="")
        self.assertEqual(self.p.poll(h)["status"], "working")
        self.fake.state.push_assistant(h, " and done")
        done = self.p.poll(h)
        self.assertEqual(done["status"], "completed")
        self.assertIn("still typing", done["assistant_text"])
        self.assertIn("and done", done["assistant_text"])

    async def test_an_unknown_event_type_is_ignored_not_fatal(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_event(h, "session.next.some.type.nobody.mapped")
        res = self.p.poll(h)             # must not raise
        self.assertEqual(res["status"], "working")

    async def test_an_unknown_event_advances_the_cursor_and_the_turn_still_completes(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_event(h, "session.next.some.type.nobody.mapped")
        self.assertEqual(self.p.poll(h)["status"], "working")
        self.fake.state.push_assistant(h, "done anyway")
        self.assertIn("done anyway", self.p.poll(h)["assistant_text"])
        self.assertEqual(self.p.poll(h)["status"], "idle")

    async def test_a_failure_while_idle_does_not_rewind_msg_seen(self):
        """Messages are only fetched while a turn is in flight, so a failure
        arriving when none is (an interrupted step, or the first poll after a
        restart) reads no messages at all. Treating that as "the session has
        zero messages" marks every reply the user already heard as unread, and
        the next completion re-narrates the whole session -- persisted, so it
        survives the restart too."""
        h = await self._session()
        self.p.send_message(h, "one")
        self.fake.state.push_assistant(h, "REPLY-ONE")
        self.assertEqual(self.p.poll(h)["assistant_text"], "REPLY-ONE")
        seen = self.p.runtime_metadata_for(h)["opencode_msg_seen"]
        self.assertEqual(seen, 1)

        # Idle now. A failed step arrives.
        self.fake.state.push_event(h, "session.next.step.failed",
                                   {"error": {"message": "provider 401"}})
        self.assertEqual(self.p.poll(h)["status"], "error")
        self.assertEqual(self.p.runtime_metadata_for(h)["opencode_msg_seen"], seen,
                         "an idle failure rewound msg_seen")

        self.p.send_message(h, "two")
        self.fake.state.push_assistant(h, "REPLY-TWO")
        res = self.p.poll(h)
        self.assertEqual(res["assistant_text"], "REPLY-TWO")
        self.assertNotIn("REPLY-ONE", res["assistant_text"])

    async def test_a_failure_mid_turn_still_consumes_the_partial_reply(self):
        """The other direction: when messages WERE read, the abandoned turn's
        half-written reply must not survive into the next completion."""
        h = await self._session()
        self.p.send_message(h, "one")
        self.fake.state.push_assistant(h, "half a sent", finish="")
        self.fake.state.push_event(h, "session.next.step.failed",
                                   {"error": {"message": "boom"}})
        self.assertEqual(self.p.poll(h)["status"], "error")

        self.p.send_message(h, "two")
        self.fake.state.push_assistant(h, "the real answer")
        res = self.p.poll(h)
        self.assertEqual(res["assistant_text"], "the real answer")

    async def test_a_failed_step_becomes_an_error_with_the_message(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_event(h, "session.next.step.failed",
                                   {"error": {"message": "HTTP 401: Model not supported"}})
        res = self.p.poll(h)
        self.assertEqual(res["status"], "error")
        self.assertIn("401", res["error"])
        # Reported once: the cursor moved past the failure.
        self.assertEqual(self.p.poll(h)["status"], "idle")

    async def test_working_while_a_turn_is_in_flight_then_idle(self):
        """All three states, in order — the in-flight flag clearing on
        completion is the half the name promises and is real behaviour."""
        h = await self._session()
        self.assertEqual(self.p.poll(h)["status"], "idle")
        self.p.send_message(h, "do it")
        self.assertEqual(self.p.poll(h)["status"], "working")
        self.fake.state.push_assistant(h, "finished")
        self.assertEqual(self.p.poll(h)["status"], "completed")
        self.assertEqual(self.p.poll(h)["status"], "idle")

    async def test_the_assistant_text_is_capped(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_assistant(h, "x" * 9000)
        res = self.p.poll(h)
        self.assertEqual(res["status"], "completed")
        self.assertLessEqual(len(res["assistant_text"]), 2000)

    async def test_polling_an_unregistered_handle_raises_keyerror(self):
        # Not just any handle: one the SERVER knows about but the provider was
        # never asked to run. Adopting it silently is what rehydrate refuses.
        theirs = self.fake.state.new_session("/tmp/their-own-work")
        with self.assertRaises(KeyError):
            self.p.poll(theirs)

    async def test_a_broken_server_surfaces_rather_than_being_swallowed(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.fail_next = (500, {"_tag": "Internal", "message": "boom"})
        with self.assertRaises(OpenCodeError):
            self.p.poll(h)


class Surface(_Base):
    async def test_capabilities_tell_the_truth(self):
        caps = self.p.capabilities()
        self.assertEqual(caps.permission_modes, ())
        self.assertFalse(caps.supports_events)
        self.assertFalse(caps.interactive_terminal)
        self.assertFalse(caps.slash_commands)
        self.assertFalse(caps.send_keys)
        self.assertFalse(caps.supports_resume)
        self.assertTrue(caps.supports_interrupt)
        self.assertTrue(caps.supports_rehydrate)
        self.assertTrue(caps.cost_tracking)

    async def test_the_unsupported_surface_raises_notimplemented_naming_opencode(self):
        h = await self._session()
        for call in (self.p.set_mode(h, "plan"),
                     self.p.send_keys(h, [{"key": "Escape"}]),
                     self.p.resume(h, ProjectContext("p", "/tmp"), SessionOptions())):
            with self.assertRaises(NotImplementedError) as cm:
                await call
            self.assertIn("OpenCode", str(cm.exception))
        with self.assertRaises(NotImplementedError) as cm:
            self.p.run_slash(h, "/init")
        self.assertIn("OpenCode", str(cm.exception))
        self.assertIsNone(await self.p.peek(h))
        self.assertIsNone(self.p.native_pane(h))
        self.assertIsNone(self.p.backend_of(h))

    async def test_the_sync_methods_work_from_inside_a_running_event_loop(self):
        """The whole reason the bridge owns its own loop. Yuri calls the sync
        provider methods from inside her event loop (tools.py → SessionService
        → provider.poll), where `run_coroutine_threadsafe(...).result()` onto
        that same loop would deadlock and `asyncio.run()` would raise outright.
        Every async test here exercises this; this one says so out loud."""
        caller = asyncio.get_running_loop()
        self.assertTrue(caller.is_running())
        h = await self._session()
        self.p.send_message(h, "do it")
        self.assertEqual(self.p.poll(h)["status"], "working")
        self.assertIsNot(self.p._loop, caller,
                         "the bridge is running on the caller's own loop")

    async def test_an_observer_is_stored_and_never_called(self):
        seen = []
        self.p.set_observer(lambda handle, ev: seen.append((handle, ev)))
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_assistant(h, "done")
        self.assertEqual(self.p.poll(h)["status"], "completed")
        self.assertEqual(seen, [], "supports_events is False; nothing may be emitted")

    async def test_answering_with_nothing_pending_is_a_soft_error(self):
        # A ValueError, so tools.py's existing soft-error path turns it into
        # words the voice model recovers from. Task 5 adds the real replies.
        h = await self._session()
        with self.assertRaises(ValueError):
            self.p.answer(h, "allow")

    async def test_read_returns_the_assistant_transcript(self):
        h = await self._session()
        self.assertEqual(await self.p.read(h), "")
        self.fake.state.push_assistant(h, "line one. ")
        self.fake.state.push_assistant(h, "line two.")
        text = await self.p.read(h)
        self.assertIn("line one.", text)
        self.assertIn("line two.", text)

    async def test_interrupt_reaches_opencode_and_ends_the_turn(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        await self.p.interrupt(h)
        self.assertEqual(self.fake.state.interrupts, [h])
        self.assertEqual(self.p.poll(h)["status"], "idle")

    async def test_an_interrupted_partial_reply_never_joins_the_next_turn(self):
        """An interrupt is exactly when an unfinished reply is most likely to
        be sitting there. If it survives, the next turn completes with text
        the agent never finished saying, answering a different question."""
        h = await self._session()
        self.p.send_message(h, "one")
        self.fake.state.push_assistant(h, "partial reply mid-typing", finish="")
        self.assertEqual(self.p.poll(h)["status"], "working")
        await self.p.interrupt(h)

        self.p.send_message(h, "two")
        self.fake.state.push_assistant(h, "final answer")
        res = self.p.poll(h)
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["assistant_text"], "final answer")
        self.assertNotIn("partial", res["assistant_text"])

    async def test_list_native_is_shaped_like_the_other_providers(self):
        h = await self._session("/tmp")
        row, = [s for s in self.p.list_native() if s["handle"] == h]
        self.assertEqual(row["session_id"], h)
        self.assertEqual(row["cwd"], "/tmp")
        self.assertEqual(row["mode"], "")
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["queued"], 0)
        self.assertEqual(row["cost_usd"], 0.0)
        self.assertEqual(row["backend"], "opencode")
        self.p.send_message(h, "do it")
        row, = [s for s in self.p.list_native() if s["handle"] == h]
        self.assertEqual(row["status"], "working")

    async def test_list_native_ignores_sessions_yuri_never_ran(self):
        mine = await self._session()
        theirs = self.fake.state.new_session("/tmp/their-own-work")
        handles = {s["handle"] for s in self.p.list_native()}
        self.assertIn(mine, handles)
        self.assertNotIn(theirs, handles)

    async def test_a_session_the_server_lost_is_no_longer_claimed(self):
        # SessionService marks its own row lost; the provider's job is simply
        # not to keep claiming a handle OpenCode no longer has.
        h = await self._session()
        del self.fake.state.sessions[h]
        self.assertEqual(self.p.list_native(), [])

    async def test_cost_comes_from_the_session(self):
        h = await self._session()
        self.fake.state.sessions[h]["cost"] = 0.25
        listed = {s["handle"]: s for s in self.p.list_native()}
        self.assertEqual(listed[h]["cost_usd"], 0.25)

    async def test_stop_forgets_the_handle_without_deleting_the_session(self):
        h = await self._session()
        await self.p.stop(h)
        self.assertNotIn(h, {s["handle"] for s in self.p.list_native()})
        # The session still exists server-side: it is durable and not ours to
        # delete. The fake implements no delete route at all, so stop()
        # returning without raising is itself the evidence that none was called.
        self.assertIn(h, self.fake.state.sessions)

    async def test_the_model_is_passed_when_configured(self):
        p = OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False),
                             default_model="google/gemini-x")
        try:
            h = await p.create_session(ProjectContext("p", "/tmp"), SessionOptions())
            self.assertEqual(self.fake.state.sessions[h]["model"],
                             {"providerID": "google", "id": "gemini-x"})
        finally:
            await p.shutdown()

    async def test_an_explicit_option_model_beats_the_default(self):
        p = OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False),
                             default_model="google/gemini-x")
        try:
            h = await p.create_session(ProjectContext("p", "/tmp"),
                                       SessionOptions(model="anthropic/claude-opus"))
            self.assertEqual(self.fake.state.sessions[h]["model"],
                             {"providerID": "anthropic", "id": "claude-opus"})
        finally:
            await p.shutdown()

    async def test_no_model_key_is_sent_when_none_is_configured(self):
        h = await self._session()
        self.assertNotIn("model", self.fake.state.sessions[h])


class Health(unittest.IsolatedAsyncioTestCase):
    async def test_health_probes_and_never_acquires_the_server(self):
        """THE point of this test: a UI that merely rendered an agent list
        polls health every 30s. If health acquired the server it would run
        `opencode serve` for a user who never asked for a session, destroying
        the laziness server.py exists to give.

        Checked against a REACHABLE server, deliberately: acquiring one that
        answers succeeds silently and leaves `client` set, which is the only
        unambiguous evidence. Probing leaves it None.
        """
        with FakeOpenCode() as fake:
            server = OpenCodeServer(fake.url, spawn=True)
            p = OpenCodeProvider(server)
            try:
                h = await p.health()
                self.assertTrue(h.online)
                self.assertIsNone(server.client, "a health poll acquired the server")
                self.assertFalse(server.owned)
                self.assertEqual(server.spawn_count, 0)
            finally:
                await p.shutdown()

    async def test_health_on_an_absent_server_does_not_spawn_one(self):
        server = OpenCodeServer(UNREACHABLE, spawn=True, binary="/nonexistent/opencode")
        p = OpenCodeProvider(server)
        try:
            h = await p.health()
            self.assertFalse(h.online)
            self.assertTrue(h.detail)
            self.assertEqual(server.spawn_count, 0, "a health poll spawned OpenCode")
            self.assertFalse(server.owned)
            self.assertIsNone(server.client)
        finally:
            await p.shutdown()

    async def test_an_unreachable_server_makes_health_offline_not_a_crash(self):
        p = OpenCodeProvider(OpenCodeServer(UNREACHABLE, spawn=False))
        try:
            h = await p.health()
            self.assertFalse(h.online)
            self.assertTrue(h.detail)
        finally:
            await p.shutdown()

    async def test_a_spawnable_server_reports_online_not_offline(self):
        """`online` is what the connect-time AGENTS block and the voice prompt
        gate on, and the prompt says not to use an agent that was offline. A
        server that is merely not running YET, with spawning allowed and the
        binary present, serves the next session fine — so reporting it offline
        made Yuri refuse the first "use OpenCode" of every boot, in the DEFAULT
        configuration, for an agent that works."""
        p = OpenCodeProvider(OpenCodeServer(UNREACHABLE, spawn=True,
                                            binary=sys.executable))
        try:
            h = await p.health()
            self.assertTrue(h.online, "a spawnable OpenCode reported offline")
            self.assertIn("will start one", h.detail)
        finally:
            await p.shutdown()

    async def test_an_unspawnable_server_reports_offline_with_the_reason(self):
        """The other two states must stay offline, and say which."""
        for kwargs, expect in (
                ({"spawn": False}, "OPENCODE_SPAWN=0"),
                ({"spawn": True, "binary": "definitely-not-a-binary"},
                 "not found or is not executable")):
            p = OpenCodeProvider(OpenCodeServer(UNREACHABLE, **kwargs))
            try:
                h = await p.health()
                self.assertFalse(h.online, kwargs)
                self.assertIn(expect, h.detail, kwargs)
            finally:
                await p.shutdown()

    async def test_health_is_cached_so_a_ui_poll_cannot_hammer_the_server(self):
        with FakeOpenCode() as fake:
            p = OpenCodeProvider(OpenCodeServer(fake.url, spawn=False))
            try:
                first = await p.health()
                self.assertTrue(first.online)
                second = await p.health()
                self.assertIs(first, second)
                probes = [c for c in fake.state.calls if c == ("GET", "/api/session")]
                self.assertEqual(len(probes), 1)
            finally:
                await p.shutdown()


class Teardown(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _bridge_threads() -> list[str]:
        return [t.name for t in threading.enumerate()
                if t.name.startswith(OpenCodeProvider.THREAD_NAME)]

    async def test_shutdown_joins_the_bridge_thread(self):
        self.assertEqual(self._bridge_threads(), [])
        with FakeOpenCode() as fake:
            p = OpenCodeProvider(OpenCodeServer(fake.url, spawn=False))
            self.assertTrue(await p.create_session(ProjectContext("p", "/tmp"),
                                                  SessionOptions()))
            self.assertEqual(len(self._bridge_threads()), 1,
                             "the sync/async bridge never started a loop thread")
            await p.shutdown()
            self.assertEqual(self._bridge_threads(), [], "shutdown leaked a thread")

    async def test_shutdown_leaves_an_attached_server_running(self):
        """Yuri never stops a server she did not start. Task 3 proves it at the
        server level; this is the same rule seen through the provider."""
        with FakeOpenCode() as fake:
            server = OpenCodeServer(fake.url, spawn=False)
            p = OpenCodeProvider(server)
            h = await p.create_session(ProjectContext("p", "/tmp"), SessionOptions())
            self.assertFalse(server.owned)
            await p.shutdown()
            self.assertEqual(p.list_native(), [])
            # Still answering, and the session is still there.
            p2 = OpenCodeProvider(OpenCodeServer(fake.url, spawn=False))
            try:
                self.assertTrue((await p2.health()).online)
                self.assertIn(h, fake.state.sessions)
            finally:
                await p2.shutdown()

    async def test_shutdown_without_ever_touching_the_server_is_a_no_op(self):
        p = OpenCodeProvider(OpenCodeServer(UNREACHABLE, spawn=False))
        await p.shutdown()          # must not raise, spawn a thread, or hang
        self.assertEqual(p.list_native(), [])


del AgentProviderContract
del _Base


class TerminalHandoff(unittest.IsolatedAsyncioTestCase):
    """`opencode -s <id> --mini` is the command that opens ONE session.

    Worth pinning because two neighbouring forms do NOT work, both measured
    against 1.18.25: `opencode attach <url> --session <id>` lands in a new
    session, and the root TUI without --mini does too. --mini is the interface
    that replays history. Anyone "simplifying" this line should read that
    matrix in the verification doc first.
    """

    async def asyncSetUp(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.addCleanup(lambda: self.fake.__exit__(None, None, None))
        self.p = OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))
        self.addAsyncCleanup(self.p.shutdown)

    async def test_it_names_the_session_and_the_mini_interface(self):
        h = await self.p.create_session(
            ProjectContext("p", "/tmp/proj"), SessionOptions())
        cmd = self.p.resume_command(h)
        self.assertIn(f"-s {h}", cmd)
        self.assertIn("--mini", cmd)
        self.assertIn("/tmp/proj", cmd)
        # Not `attach`: that form ignores --session.
        self.assertNotIn("attach", cmd)

    async def test_an_unknown_handle_offers_no_command(self):
        self.assertIsNone(self.p.resume_command("ses_nope"))

    async def test_no_live_terminal_view_is_claimed(self):
        """The TUI is a separate process reading the shared store, and a live
        session's messages are not reliably persisted while it runs, so a
        "Watch live" pane would show an empty view exactly when the user
        wanted to watch. can_open_terminal stays False."""
        h = await self.p.create_session(
            ProjectContext("p", "/tmp/proj"), SessionOptions())
        self.assertFalse(self.p.can_open_terminal(h))
        self.assertIsNone(self.p.native_pane(h))


class Transcript(unittest.IsolatedAsyncioTestCase):
    """How an OpenCode session is actually watched.

    There is no browser terminal view for OpenCode, and there does not need to
    be: /message is the same source poll reads, so the transcript panel shows
    the turn as it lands. Deliberately the API rather than OpenCode's SQLite
    store -- a live session's messages are not reliably written there while it
    runs, so the store would render an empty conversation exactly when someone
    wanted to watch.
    """

    async def asyncSetUp(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.addCleanup(lambda: self.fake.__exit__(None, None, None))
        self.p = OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))
        self.addAsyncCleanup(self.p.shutdown)
        self.h = await self.p.create_session(
            ProjectContext("p", "/tmp"), SessionOptions())

    async def test_an_empty_session_is_not_found(self):
        self.assertEqual(await self.p.transcript(self.h),
                         {"found": False, "events": []})

    async def test_it_reads_oldest_first_though_the_api_is_newest_first(self):
        """The API returns newest first; a transcript reads down the page."""
        self.fake.state.push_user(self.h, "first question")
        self.fake.state.push_assistant(self.h, "first answer")
        self.fake.state.push_user(self.h, "second question")
        tx = await self.p.transcript(self.h)
        self.assertTrue(tx["found"])
        self.assertEqual([e["text"] for e in tx["events"]],
                         ["first question", "first answer", "second question"])
        self.assertEqual([e["kind"] for e in tx["events"]],
                         ["user", "assistant", "user"])

    async def test_assistant_text_comes_from_content_not_the_top_level(self):
        """Observed live: an assistant message's top-level `text` is absent and
        the words are in content[].text. Reading `text` gives empty rows."""
        self.fake.state.push_assistant(self.h, "the answer")
        tx = await self.p.transcript(self.h)
        self.assertEqual(tx["events"], [{"kind": "assistant", "text": "the answer"}])

    async def test_an_unknown_message_type_is_skipped_not_fatal(self):
        self.fake.state.push_message(self.h, {"id": "msg_x", "type": "mystery",
                                              "time": {"created": 1}})
        self.fake.state.push_user(self.h, "still here")
        tx = await self.p.transcript(self.h)
        self.assertEqual([e["text"] for e in tx["events"]], ["still here"])

    async def test_an_unknown_handle_has_no_transcript(self):
        self.assertEqual(await self.p.transcript("ses_nope"),
                         {"found": False, "events": []})

    async def test_a_server_that_goes_away_is_empty_not_an_error(self):
        """A transcript is a convenience; losing the server must not raise
        into the UI's 2.5s poll."""
        self.fake.__exit__(None, None, None)
        self.assertEqual(await self.p.transcript(self.h),
                         {"found": False, "events": []})

if __name__ == "__main__":
    unittest.main()
