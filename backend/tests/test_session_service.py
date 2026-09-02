import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.providers.base import ProviderEvent  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.approvals import ApprovalService  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.missions import MissionService  # noqa: E402
from yuri.services.projects import ProjectService  # noqa: E402
from yuri.services.sessions import SessionService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402

PERM = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"], "request_id": "r1"}

UUID_A = "abcdefab-1111-2222-3333-444444444444"
UUID_B = "abcdefab-9999-2222-3333-444444444444"


class SessionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "proj"))
        self.home = Home(os.path.join(self.root, "Yuri")).ensure()
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root}),
                        mock.patch.object(config, "YURI_HOME", self.home.path)]
        [p.start() for p in self.patches]
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.q = self.bus.subscribe()
        self.journal = Journal(self.home)
        self.fake = FakeAgentProvider()
        self.registry = AgentRegistry()
        self.registry.register(self.fake)
        self.projects = ProjectService(self.store, self.home, self.bus)
        self.approvals = ApprovalService(self.store, self.bus, self.journal)
        self.missions = MissionService(self.store, self.bus, self.journal)
        self.svc = SessionService(self.store, self.bus, self.journal, self.registry, self.projects,
                                  self.approvals, self.missions, default_agent="fake")
        self.fake.set_observer(lambda h, ev: self.svc.on_provider_event("fake", h, ev))
        self.missions.stop_sessions = self.svc.stop_many

    def tearDown(self):
        [p.stop() for p in self.patches]
        self.store.close()
        self.tmp.cleanup()

    def _types(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait().type)
        return out

    async def test_start_creates_project_mission_session(self):
        out = await self.svc.start("proj", created_by="voice")
        self.assertEqual(set(out), {"session_id", "name", "project_path", "backend", "mode",
                                    "message", "mission_id", "yuri_session_id"})
        self.assertEqual(out["name"], "proj")
        self.assertEqual(out["project_path"], os.path.join(self.root, "proj"))
        row = self.store.sessions.get(out["yuri_session_id"])
        self.assertEqual((row.native_session_id, row.status, row.agent_id), (out["session_id"], "idle", "fake"))
        m = self.store.missions.get(out["mission_id"])
        self.assertEqual((m.title, m.goal, m.status), ("proj", None, "running"))
        step = self.store.missions.steps_for(m.id)[0]
        self.assertEqual(step.session_id, row.id)
        self.assertEqual(self._types(), ["project.registered", "mission.created", "session.created"])

    async def test_names_dedupe_and_clash_falls_back(self):
        a = await self.svc.start("proj")
        b = await self.svc.start("proj", name="PROJ")   # clash (case-insensitive) → default
        self.assertEqual((a["name"], b["name"]), ("proj", "proj 2"))
        c = await self.svc.start("proj", name="billing")
        self.assertEqual(c["name"], "billing")
        self.assertEqual(self.svc.resolve("BILLING"), c["session_id"])
        self.assertEqual(self.svc.resolve(c["yuri_session_id"]), c["session_id"])

    async def test_resolve_unknown_lists_names(self):
        await self.svc.start("proj", name="alpha")
        with self.assertRaises(KeyError) as cm:
            self.svc.resolve("zzz")
        self.assertIn("alpha", str(cm.exception))

    async def test_resolve_prefix_is_unique_or_raises(self):
        await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"), name="one")
        await self.svc.adopt(UUID_B, os.path.join(self.root, "proj"), name="two")
        self.assertEqual(self.svc.resolve("abcdefab-1111"), UUID_A)
        with self.assertRaises(KeyError) as cm:
            self.svc.resolve("abcdefab")           # matches both — must not pick one
        self.assertIn("ambiguous", str(cm.exception).lower())

    async def test_send_sets_goal_once_and_poll_observes_permission(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self._types()
        self.assertEqual(self.svc.send(sid, "fix the payment bug"), {"status": "working", "session_id": sid})
        self.svc.send(sid, "second message")
        self.assertEqual(self.store.missions.get(out["mission_id"]).goal, "fix the payment bug")
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        res = self.svc.poll(sid)
        self.assertEqual(res["status"], "needs_permission")
        pend = self.approvals.pending()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0].risk, "dangerous")
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "waiting_for_approval")
        self.assertEqual(self.svc.row_for(sid).status, "needs_permission")
        # second poll of the same prompt must not create a second approval
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.svc.poll(sid)
        self.assertEqual(len(self.approvals.pending()), 1)

    async def test_touch_is_persisted_not_just_mutated_in_memory(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        before = self.store.sessions.get(out["yuri_session_id"])
        self.svc.send(sid, "go")
        after = self.store.sessions.get(out["yuri_session_id"])   # re-read from sqlite
        self.assertEqual(after.status, "running")
        self.assertGreaterEqual(after.last_activity_at, before.last_activity_at)

    async def test_answer_resolves_approval_and_forwards(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.svc.poll(sid)
        self.assertEqual(self.svc.answer(sid, "deny"), {"status": "working", "session_id": sid})
        self.assertEqual(self.store.approvals.list(status="denied")[0].resolved_by, "voice")
        self.assertIn(("answer", sid, "deny"), self.fake.calls)
        with self.assertRaises(ValueError):
            self.fake.script(sid, {"status": "needs_permission", "prompt": {**PERM, "request_id": "r2"}})
            self.svc.poll(sid)
            self.svc.answer(sid, "hmm")
        self.svc.answer(sid, "allow")   # clear r2 so no approval is pending
        # a choice prompt (no pending approval) just forwards
        self.svc.answer(sid, "option two")
        self.assertIn(("answer", sid, "option two"), self.fake.calls)

    async def test_poll_running_status_marks_the_row_running(self):
        """claude_runner.Status includes "running"; only the runners' poll_status
        shortcut says "working". Both mean a turn is in flight."""
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "running", "assistant_text": ""})
        self.svc.poll(sid)
        self.assertEqual(self.svc.row_for(sid).status, "running")

    async def test_poll_survives_a_row_whose_agent_is_no_longer_registered(self):
        """A stored row outlives its provider whenever YURI_AGENTS changes
        between runs — `_provider_for` guards the lookup for exactly that
        reason, and the narration lookup must too. A KeyError here is worse
        than a 500: the frontend's poll catch is "transient; keep polling", so
        the session would poll forever and never narrate again."""
        out = await self.svc.start("proj", name="billing")
        sid = out["session_id"]
        row = self.svc.row_for(sid)
        row.agent_id = "retired-agent"          # provider gone from the registry
        self.store.sessions.update(row)
        with self.assertRaises(KeyError):       # the lookup really is unguardable
            self.registry.get("retired-agent")
        self.fake.script(sid, {"status": "completed", "assistant_text": "two files changed"})
        res = self.svc.poll(sid)                # must not raise
        self.assertEqual(res["status"], "completed")
        self.assertIn("two files changed", res["narration"])
        # The line still names the agent, taken from the resolved provider.
        self.assertIn("Fake Agent", res["narration"])

    async def test_poll_needs_permission_without_a_prompt_still_parks_the_session(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "needs_permission"})     # prompt payload lost
        self.svc.poll(sid)
        self.assertEqual(self.svc.row_for(sid).status, "needs_permission")
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "waiting_for_approval")
        self.assertEqual(self.approvals.pending(), [])

    async def test_completed_poll_returns_mission_to_running(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.svc.poll(sid)
        self.fake.script(sid, {"status": "completed", "assistant_text": "done"})
        self.svc.poll(sid)
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "running")
        self.assertEqual(self.svc.row_for(sid).status, "idle")

    async def test_turn_completed_emitted_exactly_once_for_an_events_provider(self):
        """FakeAgentProvider.supports_events is True, so the observer owns the
        turn_completed event; poll() must update rows without re-emitting it."""
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self._types()
        self.fake.script(sid, {"status": "completed", "assistant_text": "done"})
        self.fake.emit(sid, ProviderEvent("turn_completed", {"assistant_text": "done", "tools_used": []}))
        self.svc.poll(sid)
        self.assertEqual([t for t in self._types() if t == "session.turn_completed"],
                         ["session.turn_completed"])

    async def test_poll_emits_for_a_provider_that_does_not_stream_events(self):
        """The other direction of the single-emitter rule: with
        supports_events=False nothing arrives via the observer, so poll() itself
        must emit turn_completed / question / error — exactly once each."""
        quiet = FakeAgentProvider(supports_events=False)
        quiet.id = "quiet"
        self.assertFalse(quiet.capabilities().supports_events)
        self.registry.register(quiet)
        out = await self.svc.start("proj", agent_id="quiet")
        sid = out["session_id"]
        self._types()
        quiet.script(sid, {"status": "completed", "assistant_text": "done"})
        self.svc.poll(sid)
        quiet.script(sid, {"status": "needs_choice", "prompt": {"kind": "choice", "text": "which one?"}})
        self.svc.poll(sid)
        quiet.script(sid, {"status": "error", "error": "boom"})
        self.svc.poll(sid)
        self.assertEqual([t for t in self._types() if t != "mission.status_changed"],
                         ["session.turn_completed", "session.question", "agent.error"])
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "failed")
        self.assertIn("turn completed", self.journal.read_today())

    async def test_error_fails_mission_when_sole_session(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "error", "error": "boom"})
        self.svc.poll(sid)
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "failed")

    async def test_error_leaves_mission_alone_when_another_session_is_live(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        row = self.store.sessions.get(out["yuri_session_id"])
        self.store.sessions.insert(AgentSession(
            project_id=row.project_id, agent_id="fake", native_session_id="sibling", backend="cli",
            working_directory=row.working_directory, mission_id=row.mission_id, status="idle"))
        self.fake.script(sid, {"status": "error", "error": "boom"})
        self.svc.poll(sid)
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "running")

    async def test_start_fails_mission_when_the_provider_cannot_start(self):
        async def boom(*a, **kw):
            raise RuntimeError("claude is not installed")
        with mock.patch.object(self.fake, "create_session", boom):
            with self.assertRaises(RuntimeError):
                await self.svc.start("proj")
        self.assertEqual([m.status for m in self.store.missions.list()], ["failed"])
        self.assertEqual(self.store.sessions.list(), [])
        self.assertIn("agent.error", self._types())

    async def test_stop_pauses_mission_not_completes(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.assertEqual(await self.svc.stop(sid), {"status": "closed", "session_id": sid})
        self.assertEqual(self.svc.row_for(sid).status, "stopped")
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "paused")
        self.assertEqual(self.svc.list(), [])

    async def test_stop_many_continues_past_a_failing_provider(self):
        a = await self.svc.start("proj")
        await self.svc.start("proj")
        real_stop = self.fake.stop

        async def flaky(handle):
            if handle == a["session_id"]:
                raise RuntimeError("tmux server went away")
            await real_stop(handle)
        self.fake.stop = flaky
        with self.assertLogs("yuri.sessions", level="ERROR"):
            await self.svc.stop_many(self.store.sessions.list(live_only=True))
        self.assertEqual(self.store.sessions.list(live_only=True), [])
        # We have no evidence the wedged one closed cleanly, so it must not be
        # recorded as "stopped" — only the one we actually stopped is.
        self.assertEqual(self.svc.row_for(a["session_id"]).status, "lost")
        self.assertEqual({r.status for r in self.store.sessions.list()}, {"lost", "stopped"})

    async def test_stop_many_marks_stopped_only_when_the_provider_answered(self):
        """An unenumerable provider fails resolve() with KeyError for EVERY one
        of its handles, so the KeyError branch cannot claim "it is gone" without
        checking that the provider actually answered (spec §38)."""
        await self.svc.start("proj")

        def boom_sync():
            raise RuntimeError("runner is wedged")
        self.fake.list_native = boom_sync
        rows = self.store.sessions.list()
        with self.assertLogs("yuri.sessions", level="WARNING"):
            await self.svc.stop_many(rows)
        self.assertEqual([r.status for r in self.store.sessions.list()], ["lost"])

    async def test_stop_many_marks_stopped_when_the_provider_answered_without_it(self):
        out = await self.svc.start("proj")
        rows = self.store.sessions.list()
        self.fake.sessions.pop(out["session_id"])        # answered, and does not have it
        await self.svc.stop_many(rows)
        self.assertEqual([r.status for r in self.store.sessions.list()], ["stopped"])

    async def test_set_mode_resolves_covered_prompt(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.svc.poll(sid)
        self.fake.sessions[sid]["prompt"] = PERM   # what list_native shows while parked
        res = await self.svc.set_mode(sid, "acceptEdits")
        self.assertIs(res["prompt_resolved"], False)
        self.assertIn("still", res["message"])
        res = await self.svc.set_mode(sid, "auto")
        self.assertIs(res["prompt_resolved"], True)
        self.assertIn("approved under the new mode", res["message"])
        self.assertEqual(self.store.approvals.list(status="allowed")[0].resolved_by, "mode_switch")
        self.fake.sessions[sid].pop("prompt")
        self.assertEqual(await self.svc.set_mode(sid, "plan"), {"session_id": sid, "mode": "plan"})
        self.assertEqual(self.svc.row_for(sid).mode, "plan")

    async def test_provider_events_are_mapped(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self._types()
        self.fake.emit(sid, ProviderEvent("tool_started", {"tool_name": "Read", "tool_input": {}}))
        self.fake.emit(sid, ProviderEvent("needs_permission", {**PERM, "request_id": "hook-1"}))
        self.fake.emit(sid, ProviderEvent("turn_completed", {"assistant_text": "ok", "tools_used": ["Read"]}))
        self.fake.emit(sid, ProviderEvent("cost_updated", {"model": "m", "cost_usd": 0.01,
                                                           "input_tokens": None, "output_tokens": None}))
        self.fake.emit(sid, ProviderEvent("error", {"message": "x"}))
        self.fake.emit("unknown-handle", ProviderEvent("error", {"message": "ignored"}))
        types = [t for t in self._types() if t != "mission.status_changed"]
        self.assertEqual(types, ["tool.started", "approval.requested", "session.turn_completed",
                                 "cost.updated", "agent.error"])
        self.assertEqual(self.svc.row_for(sid).runtime_metadata.get("cost_usd"), 0.01)
        self.assertIn("turn completed", self.journal.read_today())

    async def test_a_later_cost_event_does_not_erase_the_model(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.emit(sid, ProviderEvent("cost_updated", {"model": "sonnet", "cost_usd": 0.01}))
        self.fake.emit(sid, ProviderEvent("cost_updated", {"cost_usd": 0.02}))
        md = self.svc.row_for(sid).runtime_metadata
        self.assertEqual((md.get("model"), md.get("cost_usd")), ("sonnet", 0.02))

    async def test_rehydrate_marks_lost_and_adopts_unknown(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.sessions.pop(sid)                      # simulate: process died
        self.fake.sessions["ghost"] = {"handle": "ghost", "session_id": "ghost",
                                       "cwd": os.path.join(self.root, "proj"), "model": "m",
                                       "mode": "default", "status": "idle", "cost_usd": 0.0,
                                       "queued": 0, "backend": "cli"}
        self._types()
        await self.svc.rehydrate()
        self.assertEqual(self.svc.row_for(sid).status, "lost")
        ghost = self.svc.row_for("ghost")
        self.assertIsNotNone(ghost)
        self.assertIsNone(ghost.mission_id)
        self.assertIn("session.lost", self._types())

    async def test_rehydrate_revives_a_lost_row_when_the_handle_comes_back(self):
        """`lost` is a guess: a provider can return partial results without
        raising (no tmux, a server still starting, an unreadable meta). The next
        rehydrate that sees the handle must re-attach the row, not leave the
        mission permanently detached from a live agent."""
        out = await self.svc.start("proj", name="billing")
        sid = out["session_id"]
        parked = dict(self.fake.sessions[sid])
        self.fake.sessions.pop(sid)                  # transient: pane not listed this time
        await self.svc.rehydrate()
        self.assertEqual(self.svc.row_for(sid).status, "lost")
        with self.assertRaises(KeyError):
            self.svc.resolve("billing")
        self.fake.sessions[sid] = parked             # ...and it was alive all along
        self._types()
        await self.svc.rehydrate()
        row = self.svc.row_for(sid)
        self.assertEqual((row.status, row.name, row.mission_id), ("idle", "billing", out["mission_id"]))
        self.assertEqual(self.svc.resolve("billing"), sid)
        listed = self.svc.list()[0]
        self.assertEqual((listed["name"], listed["mission_id"], listed["yuri_session_id"]),
                         ("billing", out["mission_id"], row.id))
        self.assertIn("session.created", self._types())   # revival is recorded, not silent
        self.assertEqual(len(self.store.sessions.list()), 1)   # revived, not duplicated

    async def test_reviving_a_lost_row_does_not_re_take_a_name_given_away(self):
        """The name a `lost` row holds can be handed to a NEW session while it
        is out of the live set (`_pick_name` only excludes live rows). Reviving
        it must rename it, or two live sessions answer to one name and
        resolve(name) sends the next message to whichever the store lists
        first."""
        a = await self.svc.start("proj")                     # named "proj"
        sid_a = a["session_id"]
        parked = dict(self.fake.sessions[sid_a])
        self.fake.sessions.pop(sid_a)                        # transient rehydrate miss
        await self.svc.rehydrate()
        self.assertEqual(self.svc.row_for(sid_a).status, "lost")
        b = await self.svc.start("proj")                     # the name is free again
        self.assertEqual(b["name"], "proj")
        self.fake.sessions[sid_a] = parked                   # ...A was alive all along
        await self.svc.rehydrate()
        live = {r.native_session_id: r.name for r in self.svc.live_rows()}
        self.assertEqual(sorted(live.values()), ["proj", "proj 2"])
        self.assertEqual(live[b["session_id"]], "proj")      # the new session keeps its name
        self.assertEqual(live[sid_a], "proj 2")              # the arrival is the one renamed
        self.assertEqual(self.svc.resolve("proj"), b["session_id"])
        self.assertEqual(self.svc.resolve("proj 2"), sid_a)

    async def test_rehydrate_dedupes_two_metas_carrying_the_same_name(self):
        for h in ("g1", "g2"):
            self.fake.sessions[h] = {"handle": h, "session_id": h, "name": "billing",
                                     "cwd": os.path.join(self.root, "proj"), "model": "m",
                                     "mode": "default", "status": "idle", "cost_usd": 0.0,
                                     "queued": 0, "backend": "cli"}
        await self.svc.rehydrate()
        self.assertEqual(sorted(r.name for r in self.svc.live_rows()), ["billing", "billing 2"])
        self.assertEqual({self.svc.resolve("billing"), self.svc.resolve("billing 2")}, {"g1", "g2"})

    async def test_resolve_refuses_a_name_two_live_sessions_share(self):
        """Defence in depth behind the de-dupe: if the invariant ever breaks
        again, resolve() must refuse rather than pick — the same rule the
        prefix branch already follows."""
        a = await self.svc.start("proj", name="billing")
        b = await self.svc.start("proj", name="other")
        row = self.svc.row_for(b["session_id"])
        row.name = "billing"
        self.store.sessions.update(row)
        with self.assertRaises(KeyError) as cm:
            self.svc.resolve("billing")
        self.assertIn("ambiguous", str(cm.exception))
        self.assertEqual(self.svc.resolve(a["session_id"]), a["session_id"])   # handles still work

    async def test_re_adopting_a_stopped_handle_stays_unambiguous(self):
        """`sessions.native_session_id` deliberately has NO unique index: adopt()
        inserts a second row for a handle whose first row is `stopped`, keeping
        the closed mission's history. Pin that the duplicate cannot confuse
        lookup — only the live row answers."""
        first = await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"), name="handed")
        await self.svc.stop(UUID_A)
        again = await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"), name="handed")
        rows = [r for r in self.store.sessions.list() if r.native_session_id == UUID_A]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r.status for r in rows), ["idle", "stopped"])
        self.assertEqual(len(self.svc.live_rows()), 1)
        self.assertEqual(self.svc.resolve("handed"), UUID_A)      # not ambiguous
        self.assertEqual(self.svc.row_for(UUID_A).mission_id, again["mission_id"])
        self.assertNotEqual(again["mission_id"], first["mission_id"])

    async def test_rehydrate_skips_sessions_outside_the_sandbox(self):
        self.fake.sessions["outside"] = {"handle": "outside", "session_id": "outside", "cwd": "/etc",
                                         "model": None, "mode": None, "status": "idle",
                                         "cost_usd": 0.0, "queued": 0, "backend": None}
        self.fake.sessions["nowhere"] = {"handle": "nowhere", "session_id": "nowhere", "cwd": "",
                                         "model": None, "mode": None, "status": "idle",
                                         "cost_usd": 0.0, "queued": 0, "backend": None}
        with self.assertLogs("yuri.sessions", level="WARNING"):
            await self.svc.rehydrate()
        self.assertIsNone(self.svc.row_for("outside"))
        self.assertIsNone(self.svc.row_for("nowhere"))

    async def test_rehydrate_survives_a_provider_that_raises(self):
        out = await self.svc.start("proj")
        broken = FakeAgentProvider()
        broken.id = "broken"

        async def boom():
            raise RuntimeError("runner is wedged")

        def boom_sync():
            raise RuntimeError("runner is wedged")
        broken.rehydrate = boom
        broken.list_native = boom_sync
        self.registry.register(broken)
        row = self.store.sessions.get(out["yuri_session_id"])
        self.store.sessions.insert(AgentSession(
            project_id=row.project_id, agent_id="broken", native_session_id="orphan", backend="cli",
            working_directory=row.working_directory, status="idle"))
        with self.assertLogs("yuri.sessions", level="ERROR"):
            await self.svc.rehydrate()
        self.assertEqual(self.svc.row_for(out["session_id"]).status, "idle")   # healthy provider
        # a provider we could not enumerate must not have its sessions declared lost
        self.assertEqual(self.svc.row_for("orphan").status, "idle")

    async def test_list_shape(self):
        out = await self.svc.start("proj", name="n")
        s = self.svc.list()[0]
        for k in ["handle", "session_id", "cwd", "model", "mode", "status", "cost_usd", "backend",
                  "name", "agent_id", "mission_id", "yuri_session_id"]:
            self.assertIn(k, s)
        self.assertEqual(s["name"], "n")
        self.assertEqual(self.svc.native_pane(out["session_id"]), f"fake_{out['session_id']}")

    async def test_handoff_info_and_rename(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        info = self.svc.handoff_info(sid)
        self.assertEqual(set(info), {"session_id", "name", "cwd", "attach_command", "resume_command", "command"})
        r = self.svc.rename(sid, "Neo")
        self.assertEqual(r["name"], "Neo")
        self.assertEqual(self.svc.row_for(sid).name, "Neo")

    async def test_handoff_resumes_the_native_session_id_not_the_handle(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.sessions[sid]["session_id"] = "claude-abc"   # SDK: handle != claude session id
        info = self.svc.handoff_info(sid)
        self.assertIn("claude-abc", info["resume_command"])
        self.assertNotIn(sid, info["resume_command"])

    async def test_adopt(self):
        out = await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"), name="handed")
        self.assertFalse(out["already"])
        self.assertEqual(out["name"], "handed")
        again = await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"))
        self.assertTrue(again["already"])
        self.assertEqual(self.store.missions.get(out["mission_id"]).created_by, "handoff")

    async def test_adopt_is_already_even_when_the_backend_has_no_pane(self):
        await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"), name="handed")
        self.fake.supports_terminal = False     # e.g. the SDK backend: native_pane() -> None
        again = await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"))
        self.assertTrue(again["already"])
        self.assertEqual(again["name"], "handed")
        self.assertEqual(len(self.store.missions.list()), 1)
        self.assertEqual(len(self.store.sessions.list()), 1)

    async def test_adopt_derives_attach_the_same_way_in_both_branches(self):
        """main.py interpolates `attach` into a message the user reads, so both
        branches must produce one shape: the real pane command, or None."""
        created = await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"), name="handed")
        self.assertEqual(created["attach"], f"tmux attach -t fake_{UUID_A}")
        already = await self.svc.adopt(UUID_A, "proj")
        self.assertEqual(already["attach"], created["attach"])
        self.assertEqual(already["cwd"], os.path.join(self.root, "proj"))   # resolved, not the raw ref
        self.fake.supports_terminal = False        # paneless backend (the SDK runner)
        self.assertIsNone((await self.svc.adopt(UUID_A, "proj"))["attach"])
        await self.svc.stop(UUID_A)
        fresh = await self.svc.adopt(UUID_B, os.path.join(self.root, "proj"))
        self.assertIsNone(fresh["attach"])          # never a fabricated vc_ pane
        self.assertFalse(fresh["already"])

    async def test_adopt_can_re_adopt_a_session_the_provider_no_longer_has(self):
        await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"), name="handed")
        await self.svc.stop(UUID_A)                     # the provider drops it
        again = await self.svc.adopt(UUID_A, os.path.join(self.root, "proj"))
        self.assertFalse(again["already"])
        self.assertEqual(len(self.store.missions.list()), 2)

    async def test_interrupt_read_peek_and_slash(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.assertEqual(await self.svc.interrupt(sid), {"status": "interrupted", "session_id": sid})
        self.assertEqual((await self.svc.read(sid))["text"], "fake assistant text")
        self.assertEqual((await self.svc.peek(sid))["screen"], "fake screen")
        self.assertEqual(self.svc.run_slash(sid, "/compact"),
                         {"status": "working", "session_id": sid, "sent": "/compact"})
        self.assertEqual(self.svc.row_for(sid).status, "running")
        keys = await self.svc.send_keys(sid, [{"key": "Enter"}])
        self.assertEqual(keys["session_id"], sid)

    async def test_peek_surfaces_a_pending_prompt(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.sessions[sid]["prompt"] = PERM
        res = await self.svc.peek(sid)
        self.assertEqual(res["pending_prompt"], PERM)
        self.assertIn("answer_prompt", res["note_prompt"])

    async def test_rename_rejects_empty_and_duplicate_names(self):
        a = await self.svc.start("proj", name="alpha")
        b = await self.svc.start("proj", name="beta")
        with self.assertRaises(ValueError):
            self.svc.rename(b["session_id"], "  ")
        with self.assertRaises(ValueError):
            self.svc.rename(b["session_id"], "ALPHA")
        self.svc.rename(a["session_id"], "alpha")   # renaming to its own name is fine
        self.assertEqual(self.svc.row_for(a["session_id"]).name, "alpha")


if __name__ == "__main__":
    unittest.main()
