"""WorkflowDispatcher — the two wires that make the orchestrator run (spec §8.1).

WorkflowEngine decides what happens next and nothing else; SessionService owns
agent runtimes and knows nothing about task graphs. This module is the only
place the two meet, and it is deliberately the ONLY place:

  dispatch()   the engine's injected hook. Given a task, the specialist that
               won it and the instruction, it starts (or reuses) a session,
               sends the instruction, and hands back the session id. It is
               called BY the engine and never calls advance() itself — one
               path to a provider call, and it runs through advance().

  on_event()   the other direction. Nothing polls: the engine is woken by the
               events SessionService already publishes, mapped back to the
               task that holds the session they came from.

WHY the odd-looking bits:

* MAX_SESSIONS_PER_MISSION is enforced HERE, not in the engine, and
  services/workflow.py says why at the constant: the engine cannot tell a
  dispatch that started a session from one that reused one, so it cannot count
  the thing the bound bounds. This module can, because it is what decides.
* Reuse is keyed off the TASKS that dispatched to a session, not off a field on
  the session row. `sessions` records a runtime, not an assignment; adding a
  specialist column would make two writers of one fact, and the tasks already
  carry it.
* A finished task's session is reaped, and only AFTER the engine has advanced.
  That ordering is load-bearing — see _handle().
* Narration is not this module's business. It publishes no events of its own:
  every fact it acts on already has an owner in yuri/narration/policy.py, and
  the engine owns the ones it emits. A second narration path here would
  double-speak every task.
"""
from __future__ import annotations

import asyncio
import logging

from yuri.domain.event import EventType, YuriEvent
from yuri.domain.mission import TERMINAL as MISSION_TERMINAL
from yuri.domain.mission import Mission
from yuri.domain.session import AgentSession
from yuri.domain.specialist import Specialist
from yuri.domain.task import TERMINAL_TASK, Task
from yuri.domain.workflow import Workflow
from yuri.events.bus import EventBus
from yuri.services.sessions import SessionService
from yuri.services.workflow import (IN_FLIGHT, MAX_SESSIONS_PER_MISSION, WorkflowBound,
                                    WorkflowEngine)
from yuri.store.base import Store

log = logging.getLogger("yuri.dispatch")

# The five session-level events spec §8.1 says drive a task. Anything else on
# the bus — including every event this module's own actions cause — is ignored,
# which is what keeps the driver from feeding itself.
HANDLED: frozenset[str] = frozenset({
    EventType.SESSION_TURN_COMPLETED, EventType.APPROVAL_REQUESTED,
    EventType.APPROVAL_RESOLVED, EventType.AGENT_ERROR, EventType.SESSION_LOST})

# `session.lost` is only ever published by rehydrate(), for a handle whose agent
# process did not come back. The reason has to say that, because "the agent
# failed" would send the user looking at the task instead of at the restart.
LOST_REASON = ("the agent process did not survive a backend restart, so this task's "
               "session is gone; retry it to start a fresh agent")

# How much of a turn's text a task keeps as its result. The publisher already
# clips to 2000; this is the second, independent bound, because `result` is
# stored as JSON on the row and read back into a handoff (spec §9).
RESULT_TEXT_MAX = 2000


class WorkflowDispatcher:
    def __init__(self, store: Store, bus: EventBus, sessions: SessionService,
                 engine: WorkflowEngine):
        self.store = store
        self.bus = bus
        self.sessions = sessions
        self.engine = engine
        self._q: asyncio.Queue | None = None
        self._loop_task: asyncio.Task | None = None

    # --- the engine's dispatch hook ---------------------------------------

    async def dispatch(self, task: Task, specialist: Specialist, instruction: str) -> str:
        """Start or reuse a session for `task` and send it `instruction`.

        Returns the YURI session id (AgentSession.id), never the provider's
        native handle: `tasks.session_id` is a foreign key into `sessions`
        (migration 0003), and §13's reconciliation follows it.

        Every refusal raises with a sentence the user can act on. The engine
        catches it and fails the task carrying that sentence — which is the
        point: a dispatch that quietly left the task `ready` would look exactly
        like a deadlock with no cause.
        """
        w = self.store.workflows.get(task.workflow_id)
        if w is None:
            raise LookupError(
                f"task '{task.title}' belongs to workflow {task.workflow_id[:8]}, which no "
                "longer exists")
        if w.status != "running":
            # Defence in depth behind advance()'s own first line, and the thing
            # that stops a whole pass of ready tasks failing one after another
            # once the session bound below has parked the workflow: advance()
            # read `w` before its loop, so only a re-read can see the change.
            raise RuntimeError(
                f"the workflow is {w.status}, not running; refusing to start an agent "
                f"for '{task.title}'")
        m = self.store.missions.get(w.mission_id)
        if m is None:
            raise LookupError(
                f"workflow {w.id[:8]} names mission {w.mission_id[:8]}, which no longer exists")
        project = self.store.projects.get(m.project_id)
        if project is None:
            raise LookupError(
                f"mission '{m.title}' points at a project that is no longer registered; "
                "re-register it before running this workflow")

        row = self._reusable(w, specialist)
        if row is None:
            row = await self._start(w, m, project.root_path, specialist)
        # send() is what actually hands the work over. It also claims a
        # prepend-materialised specialist's persona on the FIRST message, which
        # is exactly why the instruction must go through the service rather
        # than straight at the provider.
        self.sessions.send(row.native_session_id, instruction)
        # Optimistic: the engine marks the task `dispatched` the moment this
        # returns, and pointing at it now means the mission row is right for the
        # whole of the task's life rather than only from its first event.
        # _sync_current_step() re-derives it after every transition, so a
        # parallel pair settles on the lower ordinal a moment later.
        self._point_at(m, task.id)
        return row.id

    async def _start(self, w: Workflow, m: Mission, root: str,
                     specialist: Specialist) -> AgentSession:
        """A new session for this mission, subject to §12's session bound.

        The bound parks the WORKFLOW rather than only failing the task: four
        live agents on one mission is a decision point, not a breakage, and
        `waiting_for_human` is the state that says so (spec §12). _to_human is
        the engine's own reporter for exactly this — reason, event and journal
        line in one place — and reimplementing it here would give a bound two
        different-looking outcomes depending on who noticed it.
        """
        live = self.store.sessions.list(mission_id=m.id, live_only=True)
        if len(live) >= MAX_SESSIONS_PER_MISSION:
            in_flight = [t.title for t in self.store.tasks.for_workflow(w.id)
                         if t.status in IN_FLIGHT]
            self.engine._to_human(w, "sessions", in_flight or [s.name or s.id[:8] for s in live])
            raise WorkflowBound(
                f"mission '{m.title}' already has {len(live)} live agent sessions, the "
                f"MAX_SESSIONS_PER_MISSION bound of {MAX_SESSIONS_PER_MISSION}; finish or "
                "stop one before starting another")
        out = await self.sessions.start(root, created_by="workflow", name=specialist.name,
                                        specialist=specialist, mission=m)
        row = self.store.sessions.get(out["yuri_session_id"])
        if row is None:                      # unreachable: start() just inserted it
            raise RuntimeError(f"the session {specialist.name} started was not recorded")
        return row

    def _reusable(self, w: Workflow, specialist: Specialist) -> AgentSession | None:
        """A live session this workflow is ALREADY running this specialist in.

        Reuse is what keeps a four-task workflow from opening four sessions —
        and, with MAX_SESSIONS_PER_MISSION at 4, what keeps a longer one from
        tripping the bound on its own tail. It also keeps the agent's context:
        the second task a specialist takes starts where its first left off.

        A candidate is refused while ANOTHER task is still in flight in it. Two
        read-only tasks may legally run at once (MAX_PARALLEL_READONLY is 2),
        and if both resolve to the same specialist, sending both instructions
        into one agent would interleave two jobs in one turn queue — after
        which neither the engine nor the user can tell whose output is whose.

        Highest ordinal first: the closest predecessor is the session whose
        context is most likely to be relevant to what comes next.
        """
        tasks = self.store.tasks.for_workflow(w.id)
        busy = {t.session_id for t in tasks if t.status in IN_FLIGHT and t.session_id}
        for t in sorted(tasks, key=lambda x: x.ordinal, reverse=True):
            if not t.session_id or t.specialist_id != specialist.id or t.session_id in busy:
                continue
            row = self.store.sessions.get(t.session_id)
            if row is not None and row.is_live:
                return row
        return None

    # --- the other direction: session events → task transitions -----------

    async def on_event(self, ev: YuriEvent) -> None:
        """Wake the engine for the task that holds this event's session.

        An event for a session no task holds is ignored, silently and by
        design: plain voice-started sessions are the common case and are not
        the engine's business. Silence, not a log line — a warning per turn of
        every hand-driven session would drown the log it lives in.
        """
        if ev.type not in HANDLED or not ev.session_id:
            return
        t = self._task_for_session(ev.session_id)
        if t is None:
            return
        await self._handle(t, ev)

    def _task_for_session(self, session_id: str) -> Task | None:
        """The in-flight task holding this session, or None.

        Only IN_FLIGHT holders count, which is the same window
        `on_task_finished` accepts (its FINISHABLE): a turn that lands for a
        task already completed, skipped or given up on is stale, and replaying
        it would either raise on the transition table or un-finish work.
        """
        row = self.store.sessions.get(session_id)
        if row is None or not row.mission_id:
            return None
        for w in self.store.workflows.for_mission(row.mission_id):
            holders = [t for t in self.store.tasks.for_workflow(w.id)
                       if t.session_id == session_id and t.status in IN_FLIGHT]
            if holders:
                return min(holders, key=lambda t: t.ordinal)
        return None

    async def _handle(self, t: Task, ev: YuriEvent) -> None:
        p = ev.payload or {}
        if ev.type == EventType.APPROVAL_REQUESTED:
            self._park_on_approval(t)
        elif ev.type == EventType.APPROVAL_RESOLVED:
            self._unpark(t)
            # The only place this module advances, and it must: an approval
            # answered while the task was parked is the one outcome that frees
            # the budget without any task finishing, so nothing else would
            # wake the scheduler.
            await self.engine.advance(t.workflow_id)
        elif ev.type == EventType.SESSION_TURN_COMPLETED:
            await self.engine.on_task_finished(t.id, ok=True, result={
                "assistant_text": str(p.get("assistant_text") or "")[:RESULT_TEXT_MAX],
                "tools_used": list(p.get("tools_used") or [])})
        elif ev.type == EventType.AGENT_ERROR:
            await self.engine.on_task_finished(
                t.id, ok=False, error=str(p.get("message") or "the agent reported an error"))
        elif ev.type == EventType.SESSION_LOST:
            await self.engine.on_task_finished(t.id, ok=False, error=LOST_REASON)

        if ev.type != EventType.SESSION_LOST:
            # Reap AFTER the engine advanced, never before: advance()
            # dispatches the successor inside on_task_finished, and _reusable()
            # can only find this session while it is still live. Reaping first
            # would open a second session for the very next task of the same
            # specialist — the thing reuse exists to prevent — and, before
            # SessionService._orchestrated was taught to stand back, would also
            # have paused the mission between every two tasks.
            # Skipped for session.lost: there is nothing left to stop.
            await self._reap(t.id)
        self._sync_current_step(t.workflow_id)

    def _park_on_approval(self, t: Task) -> None:
        """running (or dispatched) → waiting_approval.

        `dispatched → waiting_approval` is not an edge (domain/task.py):
        `dispatched` is the window between "we asked" and "it answered", and a
        permission prompt IS an answer, so it closes through `running` first.
        A task already parked, or one whose turn has already landed
        (`verifying`), is left alone.
        """
        if t.status not in ("dispatched", "running"):
            return
        if t.status == "dispatched":
            t.transition("running")
        t.transition("waiting_approval")
        self.store.tasks.update(t)

    def _unpark(self, t: Task) -> None:
        if t.status != "waiting_approval":
            return
        t.transition("running")
        self.store.tasks.update(t)

    async def _reap(self, task_id: str) -> None:
        """Stop a finished task's session once no live task still holds it.

        This is what makes MAX_SESSIONS_PER_MISSION a real bound rather than
        one that always trips on a long workflow: without it every finished
        task leaves an idle agent behind, and the fifth task of any mission
        would park on `waiting_for_human` with nothing actually wrong.

        `failed` and `blocked` deliberately do NOT reap — both are waiting for
        a human to retry, and the retry wants the agent that was in the middle
        of it, with its context, not a cold one.
        """
        t = self.store.tasks.get(task_id)
        if t is None or t.status not in TERMINAL_TASK or not t.session_id:
            return
        holders = [x for x in self.store.tasks.for_workflow(t.workflow_id)
                   if x.id != t.id and x.session_id == t.session_id
                   and x.status not in TERMINAL_TASK]
        if holders:
            return
        row = self.store.sessions.get(t.session_id)
        if row is None or not row.is_live:
            return
        try:
            await self.sessions.stop(row.native_session_id)
        except Exception:       # noqa: BLE001
            # A session we could not close is a leaked agent, not a failed
            # task: the work really did finish. Log it and let the workflow
            # carry on — raising here would fail a completed task.
            log.exception("could not stop session %s after task '%s' finished", row.id, t.title)

    # --- mission lifecycle → workflow lifecycle ---------------------------

    async def sync_workflow(self, mission: Mission, to: str, by: str) -> None:
        """Keep a mission's live workflow in step when the USER moves the
        mission (MissionService.pause/resume/cancel).

        advance() refuses outright for a TERMINAL mission, but `paused` is not
        terminal — so without this, pausing a mission stops its agents and then
        happily dispatches the next task the moment an interrupted turn lands.
        That is the same defect as a paused workflow that dispatches, one level
        up. services/workflow.py says so at the check itself, and says the fix
        belongs to the container's wiring rather than to the engine, because
        the engine cannot see the mission move without racing the service that
        owns it. This is that wiring.

        Only a RUNNING workflow is paused and only a PAUSED one is resumed;
        every other live status is already a deliberate stop. A `draft`
        workflow is a plan the user has not confirmed yet (spec §14.1) and
        `waiting_for_human` is a decision they have not taken, so resuming the
        mission must release neither behind their back.
        """
        for w in self.store.workflows.for_mission(mission.id, live_only=True):
            try:
                if to == "paused" and w.status == "running":
                    await self.engine.pause(w.id, by)
                elif to == "running" and w.status == "paused":
                    await self.engine.resume(w.id, by)
                    # resume() schedules nothing by design — the caller
                    # advances. Nothing else will: the mission's agents were
                    # interrupted by the pause, so no event is coming to wake
                    # the engine, and a resume that started no work would be
                    # a button that does nothing.
                    await self.engine.advance(w.id)
                elif to in MISSION_TERMINAL:
                    await self.engine.cancel(w.id, by, reason=f"mission {to}")
            except Exception:       # noqa: BLE001
                # The mission already moved and the user was already told. A
                # workflow that would not follow is worth a log, never an
                # exception out of a lifecycle call that has already happened.
                log.exception("could not put workflow %s in step with mission %s (%s)",
                              w.id, mission.id, to)

    # --- mission.current_step ---------------------------------------------

    def _sync_current_step(self, workflow_id: str) -> None:
        """Point the mission at the task actually running now, or at nothing.

        Derived, never accumulated: recomputing from the tasks means a missed
        event or a hand intervention (retry, skip, cancel) cannot leave the
        mission row pointing at work that finished an hour ago.
        """
        w = self.store.workflows.get(workflow_id)
        if w is None:
            return
        m = self.store.missions.get(w.mission_id)
        if m is None:
            return
        live = [t for t in self.store.tasks.for_workflow(w.id) if t.status in IN_FLIGHT]
        self._point_at(m, min(live, key=lambda t: t.ordinal).id if live else None)

    def _point_at(self, m: Mission, task_id: str | None) -> None:
        if m.current_step == task_id:
            return                      # no UPDATE for a value that did not move
        m.current_step = task_id
        self.store.missions.update(m)

    # --- the subscription -------------------------------------------------

    def start(self) -> None:
        """Subscribe to the bus and start consuming, once.

        Exactly ONE subscriber for the whole engine: a second would drive every
        task twice, and `on_task_finished` is only idempotent against a STALE
        report, not against the same live one delivered twice.

        A queue consumer rather than a callback on publish(): publish() is sync
        and is called from provider threads, and the handlers below are async
        (they start sessions). The SSE route consumes the bus the same way.
        """
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._q = self.bus.subscribe()
        self._loop_task = asyncio.create_task(self._run(), name="yuri-workflow-driver")

    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    async def stop(self) -> None:
        """Unsubscribe and stop consuming. Safe on a driver that never started,
        and safe to call twice (shutdown() is)."""
        task, self._loop_task = self._loop_task, None
        q, self._q = self._q, None
        if q is not None:
            self.bus.unsubscribe(q)
        if task is None:
            return
        task.cancel()
        # `asyncio.wait` never re-raises the awaited task's CancelledError, so
        # the only one that can escape is our own caller's — same reasoning as
        # EventBus.stop_writer.
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            log.error("the workflow driver exited with an error", exc_info=task.exception())

    async def _run(self) -> None:
        q = self._q
        assert q is not None
        while True:
            ev = await q.get()
            try:
                await self.on_event(ev)
            except Exception:       # noqa: BLE001
                # One malformed event must never stop the driver: after that,
                # every remaining task in every mission would stall with no
                # sign of why.
                log.exception("the workflow driver failed on %s (%s)", ev.type, ev.id)
