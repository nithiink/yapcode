"""OpenCode sessions outlive Yuri, so they can be re-adopted after a restart —
but only the ones she was actually running, and only with BOTH high-water
marks intact.

Two rules:

  * **A session the server has and Yuri has no row for is left alone.** It may
    be the user's own OpenCode work, and adopting it would put her in charge of
    something she was never asked to run — the same instinct as never stopping
    a server she did not start.
  * **The marks come back with the session**, or she re-narrates history the
    user already heard. There are TWO of them, and both are load-bearing:
    `opencode_cursor` (the highest `durable.seq` consumed — exactly-once for
    events) and `opencode_msg_seen` (the `/message` entries already reported —
    exactly-once for completions, because `/message` has no cursor of its own).

`MarksThroughTheStore` is the half that matters most. A test that only asked
the provider to hand its cursor back would pass against a provider whose
cursor is never written down anywhere — so these drive the marks through the
real SQLite store and a *second* SessionService, the way a restart does.

    backend/.venv/bin/python -m unittest discover -s tests
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.providers.base import ProjectContext, SessionOptions  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.opencode.provider import OpenCodeProvider  # noqa: E402
from yuri.providers.opencode.server import OpenCodeServer  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.approvals import ApprovalService  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.missions import MissionService  # noqa: E402
from yuri.services.projects import ProjectService  # noqa: E402
from yuri.services.sessions import SessionService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402

UNREACHABLE = "http://127.0.0.1:1"      # nothing listens on port 1


class Rehydrate(unittest.IsolatedAsyncioTestCase):
    """The provider half: what `rehydrate(known=…)` adopts, and with what."""

    async def asyncSetUp(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.addCleanup(lambda: self.fake.__exit__(None, None, None))

    def _provider(self) -> OpenCodeProvider:
        p = OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))
        self.addAsyncCleanup(p.shutdown)
        return p

    async def test_a_known_session_is_readopted_with_its_cursor(self):
        sid = self.fake.state.new_session("/tmp/proj")
        for _ in range(5):
            self.fake.state.push_event(sid, "session.next.prompted")
        p = self._provider()
        restored = await p.rehydrate(known={sid: {"opencode_cursor": 5,
                                                  "cwd": "/tmp/proj"}})
        self.assertEqual([r["handle"] for r in restored], [sid])
        # The cursor came back: the five old events must not be re-read.
        self.assertEqual(p.poll(sid)["status"], "idle")
        self.assertEqual(p.cursor_for(sid), 5)

    async def test_a_restored_session_without_a_cursor_starts_from_now_not_zero(self):
        # Re-reading from 0 would re-narrate everything the user already heard.
        sid = self.fake.state.new_session("/tmp/proj")
        for _ in range(3):
            self.fake.state.push_event(sid, "session.next.prompted")
        p = self._provider()
        await p.rehydrate(known={sid: {"cwd": "/tmp/proj"}})
        self.assertEqual(p.cursor_for(sid), 3)

    async def test_a_restored_session_without_msg_seen_starts_from_now_too(self):
        """The cursor's unmentioned twin. `/message` has no cursor, so the
        second mark is the only thing that stops an old reply being read as a
        new turn's — and seeding it at 0 reintroduces that bug across a
        restart."""
        sid = self.fake.state.new_session("/tmp/proj")
        self.fake.state.push_assistant(sid, "an answer from before the restart")
        self.fake.state.push_assistant(sid, "and another")
        p = self._provider()
        await p.rehydrate(known={sid: {"cwd": "/tmp/proj"}})
        self.assertEqual(p.runtime_metadata_for(sid)["opencode_msg_seen"], 2)

    async def test_an_old_reply_is_not_reported_as_the_next_turns_completion(self):
        """The behaviour the second mark exists for, stated as behaviour: the
        first turn after a restart must complete with ITS reply, not with the
        one the user already heard."""
        sid = self.fake.state.new_session("/tmp/proj")
        self.fake.state.push_assistant(sid, "OLD: answered before the restart")
        p = self._provider()
        await p.rehydrate(known={sid: {"cwd": "/tmp/proj"}})

        p.send_message(sid, "a brand new question")
        first = p.poll(sid)
        self.assertEqual(first["status"], "working")
        self.assertNotIn("OLD", first.get("assistant_text", ""))

        self.fake.state.push_assistant(sid, "NEW: the answer to that question")
        done = p.poll(sid)
        self.assertEqual(done["status"], "completed")
        self.assertIn("NEW", done["assistant_text"])
        self.assertNotIn("OLD", done["assistant_text"])

    async def test_both_marks_come_back_when_both_were_stored(self):
        sid = self.fake.state.new_session("/tmp/proj")
        for _ in range(4):
            self.fake.state.push_event(sid, "session.next.prompted")
        for text in ("one", "two", "three"):
            self.fake.state.push_assistant(sid, text)
        p = self._provider()
        await p.rehydrate(known={sid: {"opencode_cursor": 2, "opencode_msg_seen": 1,
                                       "cwd": "/tmp/proj"}})
        self.assertEqual(p.runtime_metadata_for(sid),
                         {"opencode_cursor": 2, "opencode_msg_seen": 1})

    async def test_a_mark_above_what_the_server_still_has_is_clamped(self):
        """A stored mark higher than anything the server holds can only mean the
        session was truncated or replaced. Honouring it would leave Yuri
        permanently deaf to a re-numbered stream; clamping to the server's own
        high-water mark keeps the "start from now" property instead."""
        sid = self.fake.state.new_session("/tmp/proj")
        for _ in range(3):
            self.fake.state.push_event(sid, "session.next.prompted")
        self.fake.state.push_assistant(sid, "one")
        p = self._provider()
        await p.rehydrate(known={sid: {"opencode_cursor": 99, "opencode_msg_seen": 99,
                                       "cwd": "/tmp/proj"}})
        self.assertEqual(p.runtime_metadata_for(sid),
                         {"opencode_cursor": 3, "opencode_msg_seen": 1})

    async def test_a_garbled_mark_is_treated_as_no_mark_at_all(self):
        """Corrupt `runtime_metadata` must not cost a session its re-adoption —
        and must not restore a mark that reads history from the beginning. A
        negative one is the sharper case: it is an int, so it survives the
        clamp, and `after=-1` returns the entire session."""
        sid = self.fake.state.new_session("/tmp/proj")
        for _ in range(2):
            self.fake.state.push_event(sid, "session.next.prompted")
        for stored in ("seven", None, -1, [3]):
            with self.subTest(stored=stored):
                p = self._provider()
                await p.rehydrate(known={sid: {"opencode_cursor": stored,
                                               "cwd": "/tmp/proj"}})
                self.assertEqual(p.cursor_for(sid), 2)      # from now, never 0
                self.assertEqual(p.poll(sid)["status"], "idle")

    async def test_a_session_yuri_never_ran_is_left_alone(self):
        mine = self.fake.state.new_session("/tmp/proj")
        theirs = self.fake.state.new_session("/tmp/their-own-work")
        p = self._provider()
        restored = await p.rehydrate(known={mine: {"cwd": "/tmp/proj"}})
        handles = [r["handle"] for r in restored]
        self.assertIn(mine, handles)
        self.assertNotIn(theirs, handles, "adopted a session Yuri never ran")
        self.assertNotIn(theirs, {s["handle"] for s in p.list_native()})
        with self.assertRaises(KeyError):
            p.poll(theirs)

    async def test_a_vanished_session_is_simply_not_restored(self):
        p = self._provider()
        restored = await p.rehydrate(known={"ses_gone": {"cwd": "/tmp/proj"}})
        self.assertEqual(restored, [])
        # SessionService marks the row lost; the provider just does not claim it.
        self.assertNotIn("ses_gone", {s["handle"] for s in p.list_native()})

    async def test_nothing_known_adopts_nothing(self):
        self.fake.state.new_session("/tmp/their-own-work")
        p = self._provider()
        self.assertEqual(await p.rehydrate(), [])
        self.assertEqual(await p.rehydrate(known={}), [])
        self.assertEqual(p.list_native(), [])
        # Nothing to re-adopt into means no reason to touch the network either.
        self.assertEqual(self.fake.state.calls, [])

    async def test_an_unreachable_server_rehydrates_to_nothing_without_raising(self):
        """A dead OpenCode must not break startup (design spec section 41).
        Logged at INFO, not WARNING: since rehydrate attaches rather than
        spawns, "not running yet" is the ordinary state of a fresh boot, and a
        warning every time would train the user to ignore them."""
        p = OpenCodeProvider(OpenCodeServer(UNREACHABLE, spawn=False))
        self.addAsyncCleanup(p.shutdown)
        with self.assertLogs("yuri.providers.opencode", level="INFO"):
            self.assertEqual(await p.rehydrate(known={"ses_x": {}}), [])

    async def test_the_marks_are_exported_for_persistence(self):
        p = self._provider()
        h = await p.create_session(ProjectContext("p", "/tmp/proj"), SessionOptions())
        p.send_message(h, "go")
        p.poll(h)
        meta = p.runtime_metadata_for(h)
        self.assertGreaterEqual(meta["opencode_cursor"], 1)
        self.assertEqual(meta["opencode_msg_seen"], 0)
        self.assertEqual(p.cursor_for(h), meta["opencode_cursor"])
        # Never raises for a handle it does not know: SessionService.poll asks
        # every provider for this on every tick.
        self.assertEqual(p.runtime_metadata_for("nope"), {})

    async def test_the_restored_cwd_prefers_the_row_then_the_server(self):
        mine = self.fake.state.new_session("/srv/from-the-server")
        other = self.fake.state.new_session("/srv/elsewhere")
        p = self._provider()
        restored = {r["handle"]: r for r in await p.rehydrate(
            known={mine: {"cwd": "/home/from-the-row"}, other: {}})}
        self.assertEqual(restored[mine]["cwd"], "/home/from-the-row")
        self.assertEqual(restored[other]["cwd"], "/srv/elsewhere")
        self.assertEqual(restored[mine]["backend"], "opencode")


class _ServiceBase(unittest.IsolatedAsyncioTestCase):
    """One SQLite store, one fake OpenCode, and as many SessionServices as a
    test needs — a second one is what a restart looks like from here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "proj"))
        self.home = Home(os.path.join(self.root, "Yuri")).ensure()
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root}),
                        mock.patch.object(config, "YURI_HOME", self.home.path)]
        [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.journal = Journal(self.home)
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.addCleanup(lambda: self.fake.__exit__(None, None, None))

    def _boot(self, provider=None, extra=()) -> tuple[SessionService, OpenCodeProvider]:
        """A fresh SessionService over the same store — i.e. a restart."""
        p = provider or OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))
        self.addAsyncCleanup(p.shutdown)
        bus = EventBus()
        registry = AgentRegistry()
        registry.register(p)
        for other in extra:
            registry.register(other)
        projects = self.projects = ProjectService(self.store, self.home, bus)
        svc = SessionService(self.store, bus, self.journal, registry, projects,
                             ApprovalService(self.store, bus, self.journal),
                             MissionService(self.store, bus, self.journal),
                             default_agent=p.id)
        return svc, p

    def _row(self, handle: str):
        return self.store.sessions.get_by_native(handle)


class MarksThroughTheStore(_ServiceBase):
    """The write path. The plan assumed `runtime_metadata` was already
    persisted per row — the column exists, but the only writer is the
    `cost_updated` branch of `on_provider_event`, which never runs for a
    provider that declares `supports_events=False`. Without a write path
    `rehydrate` would faithfully restore a cursor nobody ever saved."""

    async def test_a_poll_writes_both_marks_onto_the_row(self):
        svc, _ = self._boot()
        h = (await svc.start("proj"))["session_id"]
        svc.send(h, "go")
        svc.poll(h)
        self.assertEqual(self._row(h).runtime_metadata,
                         {"opencode_cursor": 1, "opencode_msg_seen": 0})
        self.fake.state.push_assistant(h, "done")
        self.assertEqual(svc.poll(h)["status"], "completed")
        self.assertEqual(self._row(h).runtime_metadata,
                         {"opencode_cursor": 1, "opencode_msg_seen": 1})

    async def test_an_interrupt_with_no_poll_behind_it_still_writes_the_mark(self):
        """interrupt() consumes the abandoned turn's messages, so the mark
        moves -- but only poll used to merge marks before persisting. Via the
        voice flow a poll follows within ~1.5s and covers it; via
        /yuri/sessions/{id}/interrupt or interrupt_many nothing does, and the
        moved mark was lost, so a restart re-narrated the abandoned reply."""
        svc, _ = self._boot()
        h = (await svc.start("proj"))["session_id"]
        svc.send(h, "go")
        self.fake.state.push_assistant(h, "half a sent", finish="")
        svc.poll(h)                                  # in flight, mark at 0
        self.assertEqual(self._row(h).runtime_metadata["opencode_msg_seen"], 0)

        await svc.interrupt(h)                       # no poll afterwards
        self.assertEqual(self._row(h).runtime_metadata["opencode_msg_seen"], 1,
                         "the interrupt's mark never reached the row")

    async def test_a_send_writes_a_rewound_cursor(self):
        """send_message rewinds the cursor to admittedSeq-1 when that is lower,
        so the admitted event is read back. That is a moved mark too."""
        svc, _ = self._boot()
        h = (await svc.start("proj"))["session_id"]
        svc.send(h, "go")
        self.assertIn("opencode_cursor", self._row(h).runtime_metadata)

    async def test_a_quiet_poll_still_records_a_cursor_that_moved(self):
        """An idle poll takes none of the status branches that persist the row,
        and its cursor still moved — server-side activity Yuri did not start
        advances it. Lose that write and the next restart re-narrates it."""
        svc, _ = self._boot()
        h = (await svc.start("proj"))["session_id"]
        for _ in range(2):
            self.fake.state.push_event(h, "session.next.prompted")
        self.assertEqual(svc.poll(h)["status"], "idle")
        self.assertEqual(self._row(h).runtime_metadata["opencode_cursor"], 2)

    async def test_the_marks_do_not_clobber_cost_or_model(self):
        """`runtime_metadata` is shared: the cost path writes cost_usd/model and
        token counts into the same column."""
        svc, _ = self._boot()
        h = (await svc.start("proj"))["session_id"]
        row = self._row(h)
        row.runtime_metadata = {"cost_usd": 1.23, "model": "anthropic/sonnet",
                                "input_tokens": 42}
        self.store.sessions.update(row)
        svc.send(h, "go")
        svc.poll(h)
        md = self._row(h).runtime_metadata
        self.assertEqual((md["cost_usd"], md["model"], md["input_tokens"]),
                         (1.23, "anthropic/sonnet", 42))
        self.assertEqual(md["opencode_cursor"], 1)

    async def test_a_provider_with_no_marks_leaves_runtime_metadata_alone(self):
        """The default on the ABC is `{}`, so every existing provider polls
        exactly as it did before."""
        fake_agent = FakeAgentProvider()
        self.assertEqual(fake_agent.runtime_metadata_for("anything"), {})
        svc, _ = self._boot(provider=fake_agent)
        h = (await svc.start("proj"))["session_id"]
        row = self._row(h)
        row.runtime_metadata = {"cost_usd": 0.5}
        self.store.sessions.update(row)
        svc.poll(h)
        self.assertEqual(self._row(h).runtime_metadata, {"cost_usd": 0.5})

    async def test_the_marks_come_back_on_the_next_boot(self):
        """The round trip, end to end. The session keeps working while Yuri is
        down, so the stored marks are deliberately BEHIND the server's — a
        restart that silently fell back to "from now" would land on 3/2 here
        and this test is the only thing that can tell the difference."""
        svc, first = self._boot()
        h = (await svc.start("proj"))["session_id"]
        svc.send(h, "go")
        self.fake.state.push_assistant(h, "the reply the user already heard")
        svc.poll(h)
        stored = dict(self._row(h).runtime_metadata)
        self.assertEqual(stored, {"opencode_cursor": 1, "opencode_msg_seen": 1})

        # Yuri goes down; OpenCode keeps going.
        await first.shutdown()
        for _ in range(2):
            self.fake.state.push_event(h, "session.next.prompted")
        self.fake.state.push_assistant(h, "written while she was away")

        svc2, second = self._boot()
        restored = await svc2.rehydrate()
        self.assertEqual([r["handle"] for r in restored], [h])
        self.assertEqual(second.runtime_metadata_for(h), stored)
        self.assertEqual(self._row(h).status, "idle")
        self.assertEqual(svc2.resolve("proj"), h)      # the row is live and named

    async def test_a_row_marked_lost_is_still_offered_so_it_can_revive(self):
        """`lost` is only ever a best guess — a provider can return partial
        results without raising. A handle that comes back must revive its row,
        which it cannot do if rehydrate never offers it."""
        svc, first = self._boot()
        h = (await svc.start("proj"))["session_id"]
        row = self._row(h)
        row.status = "lost"
        self.store.sessions.update(row)
        await first.shutdown()

        svc2, second = self._boot()
        self.assertEqual([r["handle"] for r in await svc2.rehydrate()], [h])
        self.assertEqual(self._row(h).status, "idle")

    async def test_only_this_providers_own_rows_vouch_for_a_session(self):
        """`known` is built per provider. A row belonging to a DIFFERENT agent
        must not vouch for an OpenCode session that happens to carry the same
        native id — that is rule 1 again, reached sideways, and it would hand
        the other provider's runtime_metadata over as OpenCode's marks."""
        theirs = self.fake.state.new_session(os.path.join(self.root, "proj"))
        svc, oc = self._boot(extra=[FakeAgentProvider()])
        project = self.projects.resolve_or_create(os.path.join(self.root, "proj"))
        self.store.sessions.insert(AgentSession(
            project_id=project.id, agent_id="fake", native_session_id=theirs,
            backend="cli", working_directory=project.root_path, status="idle"))
        await svc.rehydrate()
        self.assertNotIn(theirs, {s["handle"] for s in oc.list_native()})

    async def test_a_session_the_user_stopped_is_not_re_adopted(self):
        """`stop` deliberately does not delete the OpenCode session, so it is
        still on the server at the next boot. Re-adopting it would put Yuri
        back in charge of something the user told her to let go of — rule 1
        applied to her own history, not just to strangers' sessions."""
        svc, first = self._boot()
        h = (await svc.start("proj"))["session_id"]
        await svc.stop(h)
        await first.shutdown()

        svc2, second = self._boot()
        self.assertEqual(await svc2.rehydrate(), [])
        self.assertNotIn(h, {s["handle"] for s in second.list_native()})
        self.assertEqual(self._row(h).status, "stopped")


class EveryProviderAcceptsTheWidenedCall(unittest.IsolatedAsyncioTestCase):
    """SessionService.rehydrate now calls `p.rehydrate(known=...)` on EVERY
    registered provider, and its own `except Exception` swallows whatever
    comes back. So a provider that never widened its signature raises
    TypeError, gets logged, and simply stops rehydrating -- its sessions
    quietly never return after a restart, with a green test suite.

    That is the failure this pins, and it is deliberately not about OpenCode:
    it is about every provider that exists now or is added later.
    """

    async def test_no_provider_rejects_the_known_keyword(self):
        import inspect

        from yuri.providers.base import AgentProvider
        from yuri.providers.claude_code import ClaudeCodeProvider
        from yuri.providers.fake import FakeAgentProvider
        from yuri.providers.opencode.provider import OpenCodeProvider

        for cls in (AgentProvider, ClaudeCodeProvider, FakeAgentProvider,
                    OpenCodeProvider):
            sig = inspect.signature(cls.rehydrate)
            accepts = ("known" in sig.parameters
                       or any(prm.kind is inspect.Parameter.VAR_KEYWORD
                              for prm in sig.parameters.values()))
            self.assertTrue(accepts,
                            f"{cls.__name__}.rehydrate{sig} would raise TypeError "
                            "on SessionService's known= call, and the caller's "
                            "except Exception would hide it")

    async def test_a_real_rehydrate_call_reaches_every_provider(self):
        """The check above is static. This one actually makes the call
        SessionService makes, so a provider that accepts the keyword but
        chokes on it still fails here."""
        from yuri.providers.fake import FakeAgentProvider

        p = FakeAgentProvider()
        try:
            self.assertEqual(await p.rehydrate(known={"ses_x": {"cwd": "/tmp"}}), [])
        finally:
            await p.shutdown()


class StartupStaysLazy(unittest.IsolatedAsyncioTestCase):
    """main.py awaits sessions.rehydrate() inside the lifespan, before the app
    serves anything. If that acquires, a spawn-enabled OpenCode that is merely
    not running gets STARTED by Yuri's own boot -- and a binary that starts but
    never becomes ready blocks the whole lifespan for the readiness timeout.

    Design spec section 4: nothing runs `opencode serve` at Yuri startup.
    """

    async def test_rehydrate_attaches_but_never_spawns(self):
        srv = OpenCodeServer("http://127.0.0.1:1", spawn=True,
                             binary=sys.executable)
        p = OpenCodeProvider(srv)
        try:
            self.assertEqual(await p.rehydrate(known={"ses_x": {"cwd": "/tmp"}}), [])
            self.assertEqual(srv.spawn_count, 0, "startup spawned OpenCode")
            self.assertIsNone(srv.client, "startup acquired the server")
            self.assertFalse(srv.owned)
        finally:
            await p.shutdown()

    async def test_rehydrate_still_re_adopts_from_a_server_already_running(self):
        """Attach-only must not cost the feature: a server that IS up is still
        enumerated and its sessions still come back."""
        with FakeOpenCode() as fake:
            sid = fake.state.new_session("/tmp/proj")
            srv = OpenCodeServer(fake.url, spawn=True, binary=sys.executable)
            p = OpenCodeProvider(srv)
            try:
                got = await p.rehydrate(known={sid: {"cwd": "/tmp/proj"}})
                self.assertEqual([r["handle"] for r in got], [sid])
                self.assertEqual(srv.spawn_count, 0)
                self.assertFalse(srv.owned, "attached, so not owned")
            finally:
                await p.shutdown()


if __name__ == "__main__":
    unittest.main()
