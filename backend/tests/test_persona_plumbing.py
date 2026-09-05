"""Phase 7 plumbing: how a specialist's persona reaches a real provider (spec
§5.2, plan Task 6). materialise.py already builds the payload each provider
needs (test_materialise.py pins that); this file pins the NEXT step -- that
SessionOptions actually carries it, that ClaudeCodeProvider/tmux_runner and
OpenCodeProvider actually use it, and that SessionService wires a Specialist
into all of that with the right failure and precedence behaviour.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
import tmux_runner  # noqa: E402
from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.domain.specialist import Specialist  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.providers.base import ProjectContext, SessionOptions  # noqa: E402
from yuri.providers.claude_code import ClaudeCodeProvider  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.opencode.provider import OpenCodeProvider  # noqa: E402
from yuri.providers.opencode.server import OpenCodeServer  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.approvals import ApprovalService  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.materialise import ClaudeMaterialiser  # noqa: E402
from yuri.services.missions import MissionService  # noqa: E402
from yuri.services.projects import ProjectService  # noqa: E402
from yuri.services.sessions import SessionService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402


def _spec(**over) -> Specialist:
    base = dict(name="Code Reviewer", role="reviewer", provider_id="claude-code",
                system_prompt="Review the diff.", model="opus", color="#dd8a6a",
                description="Reviews code.")
    base.update(over)
    return Specialist(**base)


# ---------------------------------------------------------------------------
# Task 3: tmux_runner must shlex.quote --agents/--agent into the one shell
# string it hands to `tmux new-session`. This repo has already shipped a
# shell-escaping bug from interpolating untrusted text into that same string
# (5149db7); a specialist's system_prompt is exactly that kind of text.
# ---------------------------------------------------------------------------

from yuri.providers.base import ProviderUnavailable  # noqa: E402


class _FakeTmux:
    """Records every `_tmux()` call; nothing is actually alive, so
    `_await_ready` (which checks `has-session` first) returns immediately."""

    def __init__(self):
        self.calls = []

    async def __call__(self, *args):
        self.calls.append(args)
        return 0, ""


class TmuxAgentsShellEscapingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(tmux_runner, "CTRL_ROOT", self.tmp.name),
            mock.patch.object(tmux_runner.shutil, "which", lambda n: "/usr/bin/" + n),
            mock.patch.object(tmux_runner.config, "resolve_within_roots", lambda p: p),
            # Deterministic word count regardless of the environment's own
            # CLAUDE_CLI_CHROME setting -- --chrome is orthogonal to what this
            # test is pinning (the --agents/--agent escaping).
            mock.patch.object(tmux_runner, "ENABLE_CHROME", False),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(self.tmp.cleanup)

    async def _spawn_and_capture(self, agents_json, agent_slug):
        runner = tmux_runner.TmuxClaudeRunner()
        fake = _FakeTmux()
        runner._tmux = fake
        await runner.start("/tmp", model="opus", mode="default",
                           agent_slug=agent_slug, agents_json=agents_json)
        (new_session_call,) = [c for c in fake.calls if c[0] == "new-session"]
        return new_session_call[-1]   # the inner shell command string

    async def test_a_hostile_prompt_survives_the_shell_as_one_word(self):
        # Same payload test_materialise.py proves survives shlex.quote in
        # isolation; here it goes through the FULL command line tmux_runner
        # actually builds, which is the thing 5149db7 broke.
        hostile = _spec(system_prompt='Say "done" and don\'t stop; rm -rf / $(whoami)')
        materialised = await ClaudeMaterialiser().ensure(hostile)
        inner = await self._spawn_and_capture(materialised["agents_json"], materialised["agent"])

        tokens = shlex.split(inner)
        self.assertIn("--agents", tokens)
        agents_tok = tokens[tokens.index("--agents") + 1]
        self.assertEqual(agents_tok, materialised["agents_json"],
                         "the payload did not round-trip as a single shell word")
        json.loads(agents_tok)   # still parseable JSON after the round trip

        self.assertIn("--agent", tokens)
        self.assertEqual(tokens[tokens.index("--agent") + 1], materialised["agent"])

        # No extra words: the hostile `;` and `$(...)` must not have split the
        # command line into more shell tokens than the well-formed pieces
        # (VC_CTRL=..., claude, --session-id, <uuid>, --model, opus,
        # --permission-mode, default, --agents, <json>, --agent, <slug>,
        # --settings, <path>) account for.
        self.assertEqual(len(tokens), 14, f"unexpected word count in: {tokens!r}")

    async def test_no_agent_fields_means_no_extra_flags(self):
        inner = await self._spawn_and_capture(None, None)
        self.assertNotIn("--agents", inner)
        self.assertNotIn("--agent ", inner)


# ---------------------------------------------------------------------------
# Task 3 (continued): ClaudeCodeProvider must actually forward agent_slug /
# agents_json to a runner that understands them.
# ---------------------------------------------------------------------------

class _PersonaCapableStubRunner:
    """Unlike test_claude_provider.py's _StubRunner, this one DOES accept
    agent_slug/agents_json -- the positive counterpart to the contract test's
    negative (introspection-skips-unsupported-kwargs) case."""
    on_event = None

    def __init__(self, backend):
        self.backend = backend
        self.sessions = {}
        self.start_calls = []

    async def start(self, cwd, model=None, mode="default", agent_slug=None, agents_json=None):
        h = f"{self.backend}-{len(self.sessions)}"
        self.start_calls.append({"cwd": cwd, "model": model, "mode": mode,
                                 "agent_slug": agent_slug, "agents_json": agents_json})
        self.sessions[h] = {"handle": h, "session_id": h, "cwd": cwd, "model": model or "opus",
                            "mode": mode, "status": "idle", "cost_usd": 0.0}
        return h

    def list(self):
        return list(self.sessions.values())

    async def shutdown(self):
        self.sessions.clear()


class ClaudeCodeProviderForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_slug_and_agents_json_reach_a_capable_runner(self):
        made = {}

        def factory(backend):
            made[backend] = _PersonaCapableStubRunner(backend)
            return made[backend]

        p = ClaudeCodeProvider(runner_factory=factory)
        self.addAsyncCleanup(p.shutdown)
        ctx = ProjectContext("p1", "/tmp")
        h = await p.create_session(ctx, SessionOptions(
            agent_slug="code-reviewer", agents_json='{"code-reviewer": {}}'))

        self.assertEqual(made["cli"].start_calls, [{
            "cwd": "/tmp", "model": None, "mode": "default",
            "agent_slug": "code-reviewer", "agents_json": '{"code-reviewer": {}}'}])
        listed = {s["handle"]: s for s in p.list_native()}
        self.assertEqual(listed[h]["agent"], "code-reviewer")


# ---------------------------------------------------------------------------
# Task 4: OpenCodeProvider must add "agent" to the POST /api/session body
# only when opts.agent_slug is actually set -- {"agent": null} is a different
# request from omitting the key (measured live 2026-09-04).
# ---------------------------------------------------------------------------

class OpenCodeProviderAgentBodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.addCleanup(lambda: self.fake.__exit__(None, None, None))
        self.p = OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))
        self.addAsyncCleanup(self.p.shutdown)
        self.captured: list[dict] = []
        real_create = self.p._create

        async def spy(body):
            self.captured.append(body)
            return await real_create(body)
        self.p._create = spy

    async def test_agent_key_present_only_when_set(self):
        ctx = ProjectContext("p1", "/tmp")
        await self.p.create_session(ctx, SessionOptions(agent_slug="reviewer"))
        await self.p.create_session(ctx, SessionOptions())

        self.assertEqual(self.captured[0].get("agent"), "reviewer")
        self.assertNotIn("agent", self.captured[1],
                         "omitting agent_slug must omit the key, not send agent: null")

    async def test_list_native_reports_the_recorded_agent(self):
        ctx = ProjectContext("p1", "/tmp")
        h = await self.p.create_session(ctx, SessionOptions(agent_slug="reviewer"))
        listed = {s["handle"]: s for s in self.p.list_native()}
        self.assertEqual(listed[h]["agent"], "reviewer")


# ---------------------------------------------------------------------------
# Tasks 2 & 5: SessionService wires a Specialist into all of the above --
# precedence, materialisation failure, and the prepend-only-once path.
# ---------------------------------------------------------------------------

class SessionServicePersonaTests(unittest.IsolatedAsyncioTestCase):
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
                                  self.approvals, self.missions, default_agent="fake",
                                  home=self.home.path)

    def _events(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out

    # -- prepend path: FakeAgentProvider has no native persona mechanism, so
    # materialiser_for() hands back PrependMaterialiser regardless of the
    # provider's id (that branch is checked BEFORE the id dispatch).

    async def test_prepend_reaches_only_the_first_send(self):
        spec = _spec(provider_id="fake", system_prompt="You are a terse reviewer.")
        out = await self.svc.start("proj", specialist=spec)
        handle = out["session_id"]

        self.svc.send(handle, "look at this diff")
        self.svc.send(handle, "now look at that one")

        sent = [c for c in self.fake.calls if c[0] == "send_message"]
        self.assertEqual(sent[0][2], "You are a terse reviewer.\n\nlook at this diff")
        self.assertEqual(sent[1][2], "now look at that one",
                         "the specialist's prompt leaked onto a SECOND message")

    async def test_stopping_before_the_first_send_drops_the_pending_prepend(self):
        spec = _spec(provider_id="fake", system_prompt="You are a terse reviewer.")
        out = await self.svc.start("proj", specialist=spec)
        handle = out["session_id"]
        self.assertIn(handle, self.svc._pending_prepend)
        await self.svc.stop(handle)
        self.assertNotIn(handle, self.svc._pending_prepend)

    # -- precedence: the specialist's own model/permission_mode win over
    # whatever this particular start() call passed as defaults.

    async def test_specialist_model_and_mode_win_over_call_defaults(self):
        spec = _spec(provider_id="fake", model="opus-thinking", permission_mode="plan")
        out = await self.svc.start("proj", backend="cli", mode="acceptEdits",
                                   model="some-other-model", specialist=spec)
        row = self.svc.row_for(out["session_id"])
        self.assertEqual(row.model, "opus-thinking")
        self.assertEqual(row.mode, "plan")
        create_call = next(c for c in self.fake.calls if c[0] == "create_session")
        opts = create_call[3]
        self.assertEqual(opts.model, "opus-thinking")
        self.assertEqual(opts.mode, "plan")

    async def test_specialists_own_provider_is_used_when_no_agent_id_given(self):
        other = FakeAgentProvider()
        other.id = "fake2"
        self.registry.register(other)
        spec = _spec(provider_id="fake2")
        out = await self.svc.start("proj", specialist=spec)
        self.assertEqual(self.svc.row_for(out["session_id"]).agent_id, "fake2")

    # -- materialisation failure must fail start() with the materialiser's OWN
    # message, and must not leave a running mission with no session (same
    # guarantee the provider-failure path already gives).

    async def test_materialiser_failure_fails_start_with_its_own_message(self):
        # materialiser_for() checks capabilities().supports_personas BEFORE
        # dispatching on provider id, and FakeAgentProvider always answers
        # False -- so exercising OpenCodeMaterialiser's own validation needs
        # the real OpenCodeProvider (against a fake HTTP server), not a
        # relabelled fake.
        fake_server = FakeOpenCode()
        fake_server.__enter__()
        self.addCleanup(lambda: fake_server.__exit__(None, None, None))
        opencode = OpenCodeProvider(OpenCodeServer(fake_server.url, spawn=False))
        self.addAsyncCleanup(opencode.shutdown)
        self.registry.register(opencode)
        spec = _spec(provider_id="opencode")
        spec.slug = "../../evil"   # forced past slugify -- see materialise.py

        with self.assertRaises(ValueError) as cm:
            await self.svc.start("proj", specialist=spec)
        self.assertIn("unsafe specialist slug", str(cm.exception))
        self.assertEqual([m.status for m in self.store.missions.list()], ["failed"])
        self.assertEqual(self.store.sessions.list(), [])
        # The mission-failure event's own reason must be the materialiser's
        # text, not a generic "agent unavailable" -- a user who asked for this
        # specialist and got silence deserves to know THIS is why.
        error_events = [e for e in self._events() if e.type == "agent.error"]
        self.assertTrue(error_events, "no agent.error event was published")
        self.assertIn("unsafe specialist slug", error_events[-1].payload["message"])


if __name__ == "__main__":
    unittest.main()


class SdkBackendCarriesThePersonaTests(unittest.IsolatedAsyncioTestCase):
    """The SDK backend was silently dropping personas.

    `create_session` forwarded the persona only when the runner's signature
    accepted it, so an SDK-backed session with a specialist ran persona-less
    — while `list_native()` reported the slug anyway, so the system claimed a
    persona it had not applied. That is worse than silence: it removes the
    only way a user could notice.

    The SDK does support it (`ClaudeAgentOptions.agents`, a map of
    `AgentDefinition`), so the guard was hiding unbuilt work, not a real
    capability gap.
    """

    def test_the_sdk_runner_accepts_the_persona_parameters(self):
        import inspect as _inspect

        from claude_runner import ClaudeRunner, SDKClaudeRunner
        for cls in (ClaudeRunner, SDKClaudeRunner):
            params = _inspect.signature(cls.start).parameters
            self.assertIn("agent_slug", params, cls.__name__)
            self.assertIn("agents_json", params, cls.__name__)

    def test_the_agents_json_becomes_sdk_agent_definitions(self):
        from claude_runner import _agents_for_sdk
        out = _agents_for_sdk(json.dumps({
            "reviewer": {"description": "Reviews code.", "prompt": "Review the diff.",
                         "tools": ["Read", "Grep"], "model": "opus"}}))
        self.assertEqual(list(out), ["reviewer"])
        self.assertEqual(out["reviewer"].prompt, "Review the diff.")
        self.assertEqual(out["reviewer"].tools, ["Read", "Grep"])

    def test_a_definition_missing_a_required_field_is_dropped_not_raised(self):
        # AgentDefinition requires description and prompt. Constructing one
        # without them raises inside the SDK at connect time, where the error
        # names neither the specialist nor the field.
        from claude_runner import _agents_for_sdk
        self.assertIsNone(_agents_for_sdk(json.dumps({"x": {"description": "d"}})))
        self.assertIsNone(_agents_for_sdk("not json at all"))
        self.assertIsNone(_agents_for_sdk(json.dumps(["a", "list"])))


class ARequestedPersonaIsNeverSilentlyDroppedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_runner_that_cannot_take_one_fails_loudly(self):
        from yuri.providers.base import ProjectContext, SessionOptions
        from yuri.providers.claude_code import ClaudeCodeProvider

        class OldRunner:
            on_event = None
            async def start(self, cwd, model=None, mode="default"):
                return "h1"

        old = OldRunner()
        p = ClaudeCodeProvider(runner_factory=lambda backend: old)
        with self.assertRaises(ProviderUnavailable) as ctx:
            await p.create_session(ProjectContext("p1", "/tmp"),
                                   SessionOptions(agent_slug="reviewer"))
        msg = str(ctx.exception)
        self.assertIn("reviewer", msg)
        self.assertIn("backend", msg, "the message must say what to do about it")

    async def test_a_persona_less_start_still_uses_the_three_argument_form(self):
        # Older doubles that stub ClaudeRunner without the new parameters must
        # keep working for the common case.
        from yuri.providers.base import ProjectContext, SessionOptions
        from yuri.providers.claude_code import ClaudeCodeProvider

        seen = {}

        class OldRunner:
            on_event = None
            async def start(self, cwd, model=None, mode="default"):
                seen["called"] = True
                return "h1"

        old = OldRunner()
        p = ClaudeCodeProvider(runner_factory=lambda backend: old)
        self.assertEqual(await p.create_session(ProjectContext("p1", "/tmp"),
                                                SessionOptions()), "h1")
        self.assertTrue(seen.get("called"))

    async def test_list_native_never_claims_a_persona_that_was_refused(self):
        from yuri.providers.base import ProjectContext, SessionOptions
        from yuri.providers.claude_code import ClaudeCodeProvider

        class OldRunner:
            on_event = None
            async def start(self, cwd, model=None, mode="default"):
                return "h1"
            def list(self):
                return [{"handle": "h1", "session_id": "h1", "cwd": "/tmp",
                         "model": "opus", "status": "idle"}]

        old = OldRunner()
        p = ClaudeCodeProvider(runner_factory=lambda backend: old)
        with self.assertRaises(ProviderUnavailable):
            await p.create_session(ProjectContext("p1", "/tmp"),
                                   SessionOptions(agent_slug="reviewer"))
        for row in p.list_native():
            self.assertIsNone(row.get("agent"),
                              "reported a persona that was never applied")
