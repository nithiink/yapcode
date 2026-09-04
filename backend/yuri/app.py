"""Composition root (spec §5.9). Builds the object graph once at startup;
tools.py and the routes fetch services from container(). Tests build their own
with a temp home and a fake provider via test_container().

WHY the shape:

* One place, one graph. Every wire that cannot be expressed as a constructor
  argument lives here and nowhere else: the provider observers (provider event
  -> SessionService), `missions.stop_sessions` / `missions.interrupt_sessions`
  (both injected to break the
  Mission<->Session cycle), `workflow.dispatch` (injected the same way, to
  break the Engine<->Session one), and `session_manager.set_provider` (the module's
  provider slot must hold the SAME ClaudeCodeProvider as the services — two
  live TmuxClaudeRunners would fight over the same tmux control dirs and both
  rehydrate the same panes).
* A module-level singleton, not a FastAPI dependency, because the callers are
  not all request handlers: tools.py is dispatched from a WebSocket-driven
  voice loop and the tmux runner's sync callbacks reach the bus too.
* `container()` raises rather than lazily building: a lazily built container
  would silently open a SECOND store and a second provider on any code path
  that runs before startup.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass
from typing import Callable

import config
import session_manager
from yuri.domain.event import YuriEvent
from yuri.domain.ids import utcnow
from yuri.events.bus import EventBus, bridge_to_event_log
from yuri.home import Home, default_home
from yuri.mcp.manager import McpManager
from yuri.narration.policy import MODES, Mode, normalize_mode
from yuri.narration.service import NarrationService
from yuri.providers.base import AgentProvider
from yuri.providers.registry import AgentRegistry, build_registry
from yuri.services.approvals import ApprovalService
from yuri.services.dispatch import WorkflowDispatcher
from yuri.services.journal import Journal
from yuri.services.memory import Memory
from yuri.services.missions import MissionService
from yuri.services.projects import ProjectService
from yuri.services.roster import RosterService
from yuri.services.router import AgentRouter
from yuri.services.sessions import SessionService
from yuri.services.workflow import WorkflowEngine
from yuri.store.base import Store
from yuri.store.sqlite import SqliteStore
from yuri.workflows.loader import load_templates

log = logging.getLogger("yuri.app")

Bridge = Callable[[YuriEvent], None]

# How long shutdown waits for the event writer to flush what is already queued.
# Bounded: a wedged writer must not hold the process open, and losing the tail
# of the event log is preferable to never exiting.
DRAIN_TIMEOUT_S = 2.0


@dataclass
class Container:
    home: Home
    store: Store
    bus: EventBus
    registry: AgentRegistry
    router: AgentRouter
    journal: Journal
    memory: Memory
    narration: NarrationService
    projects: ProjectService
    approvals: ApprovalService
    missions: MissionService
    sessions: SessionService
    roster: RosterService
    workflow: WorkflowEngine
    # The two wires between the engine and SessionService (spec §8.1). On the
    # container because startup/shutdown own its bus subscription -- nothing
    # else should reach for it.
    dispatcher: WorkflowDispatcher
    # Configured MCP servers and the tools they currently provide. Built here
    # but CONNECTED in startup(), because connecting is async and best-effort:
    # a server that will not start must not stop the backend.
    mcp: McpManager


_container: Container | None = None
_startup_error: str | None = None


class YuriUnavailable(RuntimeError):
    """There is no container: `startup()` has not run, or it failed and the host
    app chose to serve without the Yuri layer (see main.py's lifespan).

    A named type, not a bare RuntimeError, so the surfaces that sit above it can
    turn "Yuri's storage is down" into ONE clear, actionable message — a 503
    from the API, `{ok: false, error: ...}` from a voice tool — instead of a
    stack trace. Still a RuntimeError: nothing that catches RuntimeError today
    changes behavior."""


def note_startup_failure(exc: BaseException | None) -> None:
    """Record why startup failed, so container() can say so. None clears it."""
    global _startup_error
    _startup_error = None if exc is None else f"{type(exc).__name__}: {exc}"


def unavailable_message() -> str:
    if _startup_error is None:
        return "Yuri is not initialised (app startup has not run)."
    return (
        "Yuri's state layer failed to start, so missions, sessions, approvals and the "
        f"journal cannot be recorded. Cause: {_startup_error}. Check that YURI_HOME "
        f"({config.YURI_HOME}) is a writable DIRECTORY, not a file, then restart the backend.")


def container() -> Container:
    if _container is None:
        raise YuriUnavailable(unavailable_message())
    return _container


def container_or_none() -> Container | None:
    return _container


def set_container(c: Container | None) -> None:
    global _container
    _container = c


def build_container(home: Home, registry: AgentRegistry, *, bridge: Bridge | None = bridge_to_event_log,
                    default_agent: str = "claude-code") -> Container:
    """Wire the graph and install it as the process container.

    `bridge` defaults to bridge_to_event_log on purpose: with bridge=None every
    Yuri event is still persisted and still reaches SSE subscribers, but the
    Activity panel mirror silently never happens — no error, just missing
    events. Tests pass bridge=None deliberately (see test_container).
    """
    home.ensure()
    store = SqliteStore(home.db_path)
    try:
        store.migrate()
        bus = EventBus(repo=store.events, bridge=bridge)
        router = AgentRouter(registry, default_agent)
        journal = Journal(home)
        memory = Memory(home)
        narration = NarrationService()
        projects = ProjectService(store, home, bus)
        approvals = ApprovalService(store, bus, journal)
        missions = MissionService(store, bus, journal)
        sessions = SessionService(store, bus, journal, registry, projects, approvals, missions,
                                  default_agent=default_agent, router=router,
                                  narration=narration, mode_reader=narration_mode,
                                  # A plain path, not the Home object: materialiser_for()
                                  # joins it straight into OpenCode's config dir and stays
                                  # testable with a tempdir string instead of needing a Home.
                                  home=home.path)
        missions.stop_sessions = sessions.stop_many
        missions.interrupt_sessions = sessions.interrupt_many
        roster = RosterService(store, bus, registry)
        workflow = WorkflowEngine(store, bus, journal, roster, load_templates())
        dispatcher = WorkflowDispatcher(store, bus, sessions, workflow)
        # The same injection as stop_sessions above, and for the same reason:
        # the engine cannot import SessionService (which holds the store the
        # engine also holds), and until this line runs `dispatch is None` — a
        # dry run that schedules nothing. Wired HERE, at build time, not at
        # startup: a container whose engine can plan but not run would fail
        # only when a user actually released a workflow.
        workflow.dispatch = dispatcher.dispatch
        missions.sync_workflow = dispatcher.sync_workflow
        try:
            roster.seed()
        except Exception:
            # A roster that failed to seed is recoverable — the user can create
            # specialists by hand, or fix YURI_AGENTS and restart — but a
            # backend that will not start is not. Seeding is idempotent, so the
            # next startup retries it for free.
            log.exception("yuri: seeding the builtin specialists failed; the roster may be empty")
        for p in registry.all():
            # Observer is (handle, ProviderEvent); on_provider_event also wants
            # the agent id, which the provider never sends — bind it here.
            p.set_observer(functools.partial(sessions.on_provider_event, p.id))
        projects.ensure_home()
        try:
            session_manager.set_provider(registry.get("claude-code"))   # one instance per process
        except KeyError:
            # No Claude provider in this registry — clear the slot rather than
            # leaving it pointed at a PREVIOUS container's provider, or a
            # fake-provider test inherits a real one. The invariant is "the slot
            # holds this container's provider or nothing", unconditionally.
            session_manager.set_provider(None)
    except BaseException:
        # A half-built container must not leak an open sqlite connection, and
        # must never be published via set_container().
        store.close()
        raise
    c = Container(home, store, bus, registry, router, journal, memory, narration, projects, approvals, missions,
                 sessions, roster, workflow, dispatcher, McpManager(home.path))
    set_container(c)
    return c


async def startup() -> Container:
    """Build the process container and start the event writer."""
    if _container is not None:
        # Exactly one ClaudeCodeProvider (and one store) per process. A second
        # startup without a shutdown would orphan the first — two runners
        # competing over the same tmux control dirs.
        log.warning("yuri: startup() called with a container already installed; replacing it")
        await shutdown()
    home = default_home().ensure()
    registry = build_registry(config.YURI_AGENTS)
    # claude-code when it is registered, else whatever is. build_container's
    # own default is the literal "claude-code", so a deployment that set
    # YURI_AGENTS=opencode got an AgentRouter whose fallback names an agent the
    # registry does not have -- every unqualified start_session raising
    # KeyError. That turns "OpenCode is optional" into "OpenCode-only is
    # broken", which is the opposite of what optional means.
    ids = registry.ids()
    c = build_container(home, registry,
                        default_agent="claude-code" if "claude-code" in ids
                        else (ids[0] if ids else "claude-code"))
    c.bus.start_writer()
    # After the writer, because the driver's own handling publishes (task
    # dispatched/completed) and those events should be persisted, not dropped
    # into a queue nobody is reading yet.
    c.dispatcher.start()
    try:
        # Best effort, and never blocking: each server has its own bounded
        # connect timeout, and one that fails is logged and simply not
        # advertised. Because the capability map is derived from the live tool
        # list, a server that is down cannot become a capability she promises.
        await c.mcp.start_all()
    except Exception:
        log.exception("yuri: connecting MCP servers failed; continuing without them")
    note_startup_failure(None)   # a successful start clears any earlier failure
    log.info("yuri: home=%s db=%s agents=%s", home.path, home.db_path, registry.ids())
    return c


async def shutdown() -> None:
    """Stop, flush and forget everything startup() built, in the reverse order:
    driver -> providers -> drain -> event writer -> store. Safe to call twice,
    and safe on a container whose writer and driver were never started (see
    test_container)."""
    c = _container
    if c is None:
        return
    try:
        # The workflow driver goes first, before the providers: it is the only
        # thing that can START an agent, and starting one during teardown would
        # leave a live session behind a closed store.
        await c.dispatcher.stop()
        # Before the providers, and for the same reason: these are child
        # processes we spawned, and leaving one running past shutdown orphans
        # it.
        try:
            await c.mcp.close()
        except Exception:
            log.exception("yuri: stopping MCP servers failed")
        # Order matters, and it is the reverse of startup: providers stop FIRST,
        # because tearing one down can still publish (a cancelled turn, or
        # session.stopped when VC_KILL_SESSIONS_ON_SHUTDOWN=1). Only then is it
        # safe to drain, and only after that to stop the writer — draining after
        # the writer is gone would leave those last events in the queue forever.
        await c.registry.shutdown()
        if c.bus.writer_running():
            try:
                await asyncio.wait_for(c.bus.drain(), DRAIN_TIMEOUT_S)
            except TimeoutError:
                # Only wait_for's own timeout. A CancelledError here is the
                # shutdown task itself being cancelled; swallowing that would
                # discard the cancellation, so it propagates.
                log.warning("yuri: event writer did not drain in %.1fs; dropping the tail",
                            DRAIN_TIMEOUT_S)
            except Exception:
                log.exception("yuri: draining the event writer failed")
        await c.bus.stop_writer()
    finally:
        c.store.close()
        set_container(None)
        session_manager.reset()


SETTINGS_NARRATION_MODE = "narration_mode"
# When Yuri last did anything for the user. Stamped on every voice tool
# dispatch rather than on a disconnect: a closed tab, a killed browser or a
# dropped network never fires a disconnect, and a field that is usually stale
# is worse than an absent one — she would say "it's been a while" on the basis
# of nothing. This answers a slightly different question ("when did she last
# do something for them" rather than "when did the conversation end"), which
# is acceptable because it cannot be missed, and her prompt says as much.
SETTINGS_LAST_SPOKE = "last_spoke_at"


def narration_mode() -> Mode:
    """The remembered narration mode. Defaults to normal, and never raises on a
    corrupt stored value — normalize_mode absorbs it."""
    return normalize_mode(container().store.settings.get(SETTINGS_NARRATION_MODE))


def set_narration_mode(mode: object) -> Mode:
    """Persist the mode. Raises ValueError naming the valid modes on bad input —
    unlike narration_mode(), a caller setting a mode deserves to be told."""
    if not isinstance(mode, str) or mode.strip().lower() not in MODES:
        raise ValueError(f"narration mode must be one of: {', '.join(MODES)}")
    m = normalize_mode(mode)
    container().store.settings.set(SETTINGS_NARRATION_MODE, m)
    return m


def last_spoke_at() -> str | None:
    """ISO timestamp of the last voice tool dispatch, or None if never."""
    try:
        v = container().store.settings.get(SETTINGS_LAST_SPOKE)
    except Exception:
        return None
    return v if isinstance(v, str) and v else None


def stamp_last_spoke() -> None:
    """Best effort by design: this is a nicety for her opening line, and it
    must never be the reason a tool call fails."""
    try:
        container().store.settings.set(SETTINGS_LAST_SPOKE, utcnow())
    except Exception:
        log.debug("could not stamp last_spoke_at", exc_info=True)


def test_container(home_path: str, provider: AgentProvider, default_agent: str | None = None) -> Container:
    """Container for tests: one provider, a temp home, and NO event_log bridge.

    No writer is started either, so tests must never call `bus.drain()` — it
    would block forever on a repo-backed bus with nothing consuming the queue.
    """
    reg = AgentRegistry()
    reg.register(provider)
    return build_container(Home(home_path), reg, bridge=None, default_agent=default_agent or provider.id)
