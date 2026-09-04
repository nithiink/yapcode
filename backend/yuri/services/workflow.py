"""WorkflowEngine — the orchestrator (spec §8, bounds §12).

It owns exactly ONE decision: what happens next. It never talks to a provider.
`dispatch` is an injected coroutine (the container wires it to SessionService
in Task 10, the same way `MissionService.stop_sessions` is injected), and
`dispatch is None` is a real, supported mode — a dry run that schedules
nothing. That is what makes the scheduler testable before a single session
exists, and it is also the state the process is in for the instant between
construction and wiring, when `advance()` must not pretend it started work.

Two properties carry the whole design and are each pinned by a test:

  advance() refuses unless the workflow is EXACTLY `running`. That is the
  first line of the method, not a later check, because a paused workflow that
  still dispatches is the worst defect this design can have.

  advance() is idempotent. It is called after every task state change and
  again on rehydrate, so calling it twice must dispatch once. The guarantee is
  structural, not defensive: `pending → ready → dispatched` is the only path
  to a provider call, every step of it goes through Task.transition() and the
  store, and a `dispatched` task is no longer `ready`.

No task may create tasks. `create()` is the only writer of `tasks` rows here,
and nothing reachable from a provider reaches it — which is the only reason
the bounds below mean anything at all.
"""
from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, replace
from typing import Awaitable, Callable

from yuri.domain.event import EventType, YuriEvent
from yuri.domain.ids import utcnow
from yuri.domain.mission import TERMINAL as MISSION_TERMINAL
from yuri.domain.mission import TRANSITIONS as MISSION_TRANSITIONS
from yuri.domain.mission import Mission
from yuri.domain.specialist import Specialist
from yuri.domain.task import Task
from yuri.domain.workflow import Workflow
from yuri.events.bus import EventBus
# The checks themselves live outside the engine: the engine decides what
# happens next, verify.py decides what a check MEANS. That split is why
# "`unavailable` never passes" is one rule in one file rather than a default
# the scheduler could soften.
from yuri.services import verify
from yuri.services.journal import Journal
from yuri.services.roster import NoSpecialist, RosterService
from yuri.store.base import Store
# Imported, never redeclared: the bound is a property of a workflow
# *definition*, and loader.validate() is what can reject one at authoring time
# — before any task runs. Re-exported here only so callers that hold the
# engine can read the bound they are subject to.
from yuri.workflows.loader import (MAX_TASKS_PER_WORKFLOW, Template,  # noqa: F401
                                   TemplateError, TemplateTask, render, validate)

# Spec §12. Each has a test that proves the bound HOLDS, not that the constant
# was written down.
MAX_TASK_ATTEMPTS = 2
MAX_PARALLEL_READONLY = 2
# There is no git worktree isolation in this phase (§7.15 marks it future), so
# this constant is the only thing standing between two agents and a corrupted
# working tree. Raising it requires worktrees first.
MAX_WRITERS = 1
MAX_MISSION_RUNTIME_S = 14400          # 4 hours
# Declared here because it is a §12 bound, but NOT enforced in this module, and
# that is a deliberate refusal rather than an omission: `dispatch` is opaque to
# the engine, so the engine cannot tell whether a dispatch starts a new session
# or reuses one — it cannot count the thing this bounds. Counting live session
# ROWS instead would trip on any template with more than four tasks, since
# nothing in this phase stops a finished task's session, and it would park a
# perfectly healthy workflow on `waiting_for_human` with one agent working.
# The dispatch hook (Task 10) is the only code that knows, and is where this
# belongs. A bound enforced in the wrong place is worse than one enforced late.
MAX_SESSIONS_PER_MISSION = 4

# In-flight for the purposes of the concurrency budget: the agent may be
# touching the tree right now. `waiting_approval` counts — the agent is
# parked mid-edit with a half-applied change, which is the most dangerous
# moment to start anything beside it.
IN_FLIGHT: tuple[str, ...] = ("dispatched", "running", "verifying", "waiting_approval")
# Statuses that no longer hold a place in the graph. `blocked` and `failed` are
# deliberately absent: they still need a human, so they still count as work
# remaining, which is what turns them into a deadlock rather than a silent end.
DONE: tuple[str, ...] = ("completed", "skipped", "cancelled")
# Statuses that SATISFY a dependency (spec §7.2: "completed (or skipped)").
# `cancelled` is deliberately not one, even though it is terminal: a task whose
# input was cancelled has no input, and running it anyway would hand an agent
# a handoff with a hole in it that nothing downstream could detect. It stays
# `pending` and surfaces as a deadlock naming the cancelled task instead.
SATISFIES: tuple[str, ...] = ("completed", "skipped")
# Statuses a finish report may arrive for: the engine asked, and is waiting.
# A report for anything else (a task reset to `ready` after a failure, one
# already blocked, skipped or cancelled) is stale and is dropped — replaying
# it through the transition table would raise.
FINISHABLE: tuple[str, ...] = ("dispatched", "running", "verifying", "waiting_approval")
# A task the user may still re-point at another specialist. Once it is
# in-flight the instruction has already gone to an agent, so switching would
# leave the running one unaccounted for.
ASSIGNABLE: tuple[str, ...] = ("pending", "ready", "failed", "blocked")

ERROR_MAX = 500        # `error` is provider output and is read back to the user
BLOCKING_MAX = 8       # how many task titles a deadlock payload names


@dataclass(frozen=True)
class PlannedTask(TemplateTask):
    """One task in a graph the engine is about to build.

    A SUBCLASS of the loader's TemplateTask, adding the single field a
    *template* deliberately cannot carry: specialist ids are per-install, so a
    shipped template can only name a role. A spoken plan ("use OpenCode to
    implement it and Claude Code to review it") pins one. Subclassing rather
    than defining a parallel shape means loader.validate() applies to this
    unchanged — a hand-built or spoken graph is held to exactly the same
    standard as a shipped template, and there is no second, weaker copy of the
    cycle and role checks to keep in step.
    """
    specialist_id: str | None = None


class WorkflowBound(Exception):
    """A bound from spec §12 was reached. Never a failure: the workflow goes to
    `waiting_for_human`, because a bound is a decision point and the user is
    the one who takes it."""


class WorkflowEngine:
    def __init__(self, store: Store, bus: EventBus, journal: Journal,
                 roster: RosterService, templates: dict[str, Template]):
        self.store = store
        self.bus = bus
        self.journal = journal
        self.roster = roster
        self.templates = templates
        # Injected by the container (Task 10) to avoid a cycle — SessionService
        # depends on the store the engine also holds. `None` is a dry run:
        # advance() schedules, resolves and reports, and starts nothing.
        self.dispatch: Callable[[Task, Specialist, str], Awaitable[str]] | None = None

    # --- reads ------------------------------------------------------------

    def get(self, workflow_id: str) -> Workflow:
        w = self.store.workflows.get(workflow_id)
        if w is None:
            raise KeyError(f"unknown workflow: {workflow_id}")
        return w

    def get_task(self, task_id: str) -> Task:
        t = self.store.tasks.get(task_id)
        if t is None:
            raise KeyError(f"unknown task: {task_id}")
        return t

    # --- create -----------------------------------------------------------

    async def create(self, mission: Mission, template_name: str, goal: str,
                     overrides: dict[str, dict] | None = None,
                     tasks: list[dict] | None = None) -> Workflow:
        """Build a mission's task graph. Dispatches NOTHING — the workflow is
        born `draft`, and only advance() may start work, so a spoken plan can
        be read back and confirmed before anything runs (spec §14.1).

        `tasks` supplies the graph directly, in which case `template_name` is
        not recorded: claiming a template the graph did not come from would
        make the timeline lie about where the plan came from.

        `overrides` is keyed by template task id and patches its fields
        (`instruction`, `role`, `specialist_id`, `read_only`, …) — how "use
        OpenCode to implement it and Claude Code to review it" reaches the
        graph without a template per phrasing.
        """
        if tasks is not None:
            spec = [_planned_from_mapping(d) for d in tasks]
            from_template = None
        else:
            tpl = self.templates.get(template_name)
            if tpl is None:
                raise ValueError(
                    f"unknown workflow template: {template_name!r}; available: "
                    f"{', '.join(sorted(self.templates)) or 'none'}")
            spec = [PlannedTask(**asdict(t)) for t in render(tpl, goal)]
            from_template = tpl.name

        if overrides:
            spec = [_apply_override(t, overrides.get(t.id, {})) for t in spec]
        # A pinned specialist implies its role, and filling it in here means
        # everything downstream — validate(), the Task row, roster.resolve() —
        # sees one uniform shape instead of each having to special-case a task
        # that names a specialist but no role.
        spec = [t if t.role or not t.specialist_id
                else replace(t, role=self.roster.get(t.specialist_id).role)
                for t in spec]

        if not spec:
            # An empty graph would complete on its first advance() and take the
            # mission to `completed` with it — a mission reported done over no
            # work at all, which is the worst possible way for a caller's bug
            # to surface.
            raise ValueError("a workflow needs at least one task")
        # Bound first, and BEFORE any row is written: a partially-built
        # workflow left behind by a refused create would occupy the mission's
        # one live slot with a graph nobody asked for.
        if len(spec) > MAX_TASKS_PER_WORKFLOW:
            raise WorkflowBound(
                f"{len(spec)} tasks exceeds the MAX_TASKS_PER_WORKFLOW bound of "
                f"{MAX_TASKS_PER_WORKFLOW}; split this into separate missions")
        # Everything else — cycles, unknown roles, a depends_on naming nothing,
        # duplicate ids, an agent_task with no role — is the loader's job, and
        # reusing it means a hand-built graph is held to exactly the same
        # standard as a shipped template rather than to a second, weaker copy.
        validate(Template(name=from_template or "custom", description="",
                          tasks=tuple(spec)))

        live = self.store.workflows.for_mission(mission.id, live_only=True)
        if live:
            # Mirrors 0003's workflows_one_live partial index. Checked here too
            # so the caller gets a sentence instead of an IntegrityError.
            raise ValueError(
                f"mission '{mission.title}' already has a live workflow; cancel it "
                "before planning another")
        version = 1 + max((w.version for w in self.store.workflows.for_mission(mission.id)),
                          default=0)

        w = Workflow(mission_id=mission.id, template=from_template, version=version)
        self.store.workflows.insert(w)
        rows: dict[str, Task] = {}
        for ordinal, ts in enumerate(spec, start=1):
            t = Task(workflow_id=w.id, ordinal=ordinal, title=ts.title, kind=ts.kind,
                     role=ts.role, specialist_id=ts.specialist_id,
                     instruction=ts.instruction, requires=ts.requires,
                     verification=ts.verification, read_only=ts.read_only,
                     max_attempts=MAX_TASK_ATTEMPTS)
            self.store.tasks.insert(t)
            rows[ts.id] = t
        # Edges after every row exists, so a forward reference (a template may
        # list a task before the one it depends on) resolves.
        for ts in spec:
            for dep in ts.depends_on:
                self.store.tasks.add_dep(rows[ts.id].id, rows[dep].id)

        self._publish(EventType.WORKFLOW_CREATED, w, payload={
            "workflow_id": w.id, "template": from_template, "goal": goal,
            "tasks": [{"id": rows[ts.id].id, "title": ts.title, "role": ts.role,
                       "read_only": ts.read_only,
                       "depends_on": [rows[d].id for d in ts.depends_on]}
                      for ts in spec]})
        self.journal.append(
            f"workflow planned for '{mission.title}': "
            f"{from_template or 'custom'}, {len(spec)} task(s)")
        return w

    # --- the core loop ----------------------------------------------------

    async def advance(self, workflow_id: str) -> list[Task]:
        """Promote what is ready, dispatch what fits, and decide what a stall
        means. Returns the tasks it actually dispatched.

        Spec §8's six steps, in order. Every one of them is load-bearing.
        """
        w = self.store.workflows.get(workflow_id)
        if w is None or w.status != "running":
            # 1. FIRST line, not a later check: a paused, draft,
            # waiting_for_human or cancelled workflow that still dispatched
            # would make every pause control in the UI a lie.
            return []

        m = self.store.missions.get(w.mission_id)
        if m is not None and m.status in MISSION_TERMINAL:
            # The mission was cancelled, failed or completed out from under a
            # workflow still marked `running`. Starting an agent for it would
            # be the same defect as dispatching for a paused workflow, one
            # level up. Deliberately only a REFUSAL: keeping the workflow row
            # in step with its mission is the container's wiring (Task 10),
            # and guessing at it here would race the service that owns it.
            return []

        tasks = {t.id: t for t in self.store.tasks.for_workflow(w.id)}
        deps = self.store.tasks.deps_for(w.id)

        # A bound, not a failure (§12). Checked here because advance() is the
        # only method called on every task change, so it is the one place that
        # reliably sees the workflow's age. Task 11's reconcile() checks it too,
        # for a workflow whose tasks have all silently stopped reporting.
        if self._age_s(w) > MAX_MISSION_RUNTIME_S:
            self._to_human(w, "runtime", [t.title for t in tasks.values()
                                          if t.status in IN_FLIGHT])
            return []

        # 2. pending → ready once every dependency is done. `ready` is the only
        # gate that looks at dependencies (domain/task.py), which is what makes
        # it impossible for a task to start before its inputs exist.
        self._promote(tasks, deps)

        if self.dispatch is None:
            # A dry run. Deliberately returns BEFORE the deadlock check: "no
            # dispatcher is wired" is a fact about this process's
            # configuration, not about the task graph, and reporting it as a
            # deadlock would send every healthy workflow to waiting_for_human
            # on start-up.
            return []

        # 3. The concurrency budget starts from what is ALREADY in flight, so
        # a second advance() in the same instant cannot overshoot it.
        in_flight = [t for t in tasks.values() if t.status in IN_FLIGHT]
        writers = sum(1 for t in in_flight if not t.read_only)
        readers = sum(1 for t in in_flight if t.read_only)

        started: list[Task] = []
        for t in sorted((x for x in tasks.values() if x.status == "ready"),
                        key=lambda x: x.ordinal):
            # A writing task runs ALONE. Both branches consider BOTH counts.
            # Checking only the reader cap in the reader branch is the bug this
            # rule exists to prevent: whenever the writer happened to be
            # dispatched first, a reader would join it, and a reader looking at
            # a tree mid-edit reports findings about code that no longer
            # exists — findings that then propagate through the handoff into
            # every task downstream, where nothing can tell they are stale.
            if t.read_only:
                # `writers` is tested for truthiness, NOT against MAX_WRITERS:
                # ANY live writer excludes a reader, and that stays true even
                # if MAX_WRITERS is ever raised, because what makes a reader
                # unsafe is an unisolated tree being edited at all, not how
                # many agents are editing it.
                if writers or readers >= MAX_PARALLEL_READONLY:
                    continue
            else:
                if writers >= MAX_WRITERS or readers:
                    continue
            if not await self._dispatch_one(t, w):
                continue
            started.append(t)
            if t.read_only:
                readers += 1
            else:
                writers += 1

        # `remaining` counts `blocked` and `failed` as work still outstanding:
        # both need a human, and treating them as finished is what would let a
        # workflow report success over a task that never ran.
        remaining = [t for t in tasks.values() if t.status not in DONE]

        # 5. Nothing running, nothing started, work left → deadlocked. NOT
        # `failed`: nothing is broken, a decision is needed. A silent stall is
        # indistinguishable from work in progress, which is the failure the
        # presence line already taught us to avoid.
        if not started and not in_flight and remaining:
            blocking = ([t.title for t in remaining if t.status in ("blocked", "failed")]
                        or [t.title for t in remaining])
            self._to_human(w, "deadlocked", blocking)
            return []

        # 6. Everything terminal → the workflow, and the mission, are done.
        if not remaining:
            self._complete(w)
        return started

    def _promote(self, tasks: dict[str, Task], deps: dict[str, set[str]]) -> None:
        for t in tasks.values():
            if t.status != "pending":
                continue
            # deps_for() OMITS a dependency-free task rather than mapping it to
            # an empty set, so this must be `.get(id, set())` — indexing would
            # raise on exactly the tasks that are ready first.
            blockers = deps.get(t.id, set())
            # An edge naming a task that is not in this workflow BLOCKS. It
            # should be unreachable (create() is the only writer of edges and
            # only ever links ids it just inserted), but failing closed means
            # the worst case is a deadlock the user can see, not a task
            # dispatched without the input it declared.
            if all(d in tasks and tasks[d].status in SATISFIES for d in blockers):
                t.transition("ready")
                self.store.tasks.update(t)

    async def _dispatch_one(self, t: Task, w: Workflow) -> bool:
        """Resolve, dispatch, mark `dispatched`. False means "did not start",
        and the caller must not count it against the concurrency budget.

        Returns False without touching the task only in the dry run. Every
        other refusal leaves the task in a state that SAYS why: a role nobody
        can fill goes to `failed` carrying the resolver's actionable message,
        never quietly back to `ready` — a `ready` task nobody dispatches looks
        exactly like a deadlock with no cause.
        """
        if self.dispatch is None:
            return False
        try:
            specialist = self.roster.resolve(
                t.role or "", frozenset(t.requires), pinned=t.specialist_id)
        except NoSpecialist as exc:
            # `failed`, and it STAYS failed — the retry policy is deliberately
            # not applied. Re-resolving the same role against the same roster
            # will fail identically, so a retry would only burn the attempts
            # and land on `blocked`, whose message is about exhaustion rather
            # than about the roster. What the user has to do is create or
            # un-archive a specialist, and the resolver's message says exactly
            # that; advance() then names this task as what is blocking.
            self._mark_failed(t, w, str(exc), will_retry=False)
            return False

        # The attempt is spent BEFORE the call, not after it succeeds. Counting
        # it afterwards leaves a provider that throws every time in an
        # unbounded loop: the task fails, the retry policy sees attempts still
        # at 0, puts it back to `ready`, and the next advance() tries again
        # forever. Spending it here also matches what an attempt means — a task
        # that was started and never reported back has consumed one just as
        # surely as one that reported an error.
        t.attempts += 1
        t.specialist_id = specialist.id
        t.error = None
        instruction = self._instruction(t)
        try:
            session_id = await self.dispatch(t, specialist, instruction)
        except Exception as exc:                      # noqa: BLE001
            # A provider that could not be started is a task failure with a
            # reason, not an engine crash: advance() is driven from a bus
            # subscriber, and raising here would stop every OTHER task in the
            # workflow from being scheduled.
            self._fail(t, w, f"could not start the agent: {exc}", auto_retry=False)
            return False

        t.session_id = session_id or t.session_id
        t.transition("dispatched")
        self.store.tasks.update(t)
        self._publish(EventType.TASK_DISPATCHED, w, payload={
            "workflow_id": w.id, "task_id": t.id, "title": t.title, "role": t.role,
            "specialist_id": specialist.id, "specialist": specialist.name,
            "agent_id": specialist.provider_id, "read_only": t.read_only,
            "attempt": t.attempts, "session_id": t.session_id})
        self.journal.append(f"task '{t.title}' → {specialist.name} (attempt {t.attempts})")
        return True

    def _instruction(self, t: Task) -> str:
        """What the specialist is told. The constructed handoff (spec §9) is
        Task 8's; until it lands the task's own instruction is the whole of it,
        which is honest — an empty handoff is better than a transcript dump
        §7.10 forbids."""
        return t.instruction or t.title

    # --- the engine's own event sink --------------------------------------

    async def on_task_finished(self, task_id: str, ok: bool, error: str | None = None,
                               result: dict | None = None) -> None:
        """A dispatched task's agent has reported. Drives it to `completed`, or
        applies the retry policy, and then advances — on EVERY outcome.

        This is the only public method that advances, and it must: it is the
        one hook a running mission is driven by. A retriable failure that did
        not advance would leave the task `ready` with nothing left to wake the
        engine — no agent is running, so no further event arrives — so the
        second attempt would never happen, `blocked` would be unreachable, and
        MAX_TASK_ATTEMPTS would bound nothing. That stall is invisible: a
        `ready` task looks exactly like work about to start.

        The human-facing operations (resume/retry/skip/assign) deliberately do
        NOT advance; their caller does, which lets a user fix several tasks
        before releasing the work.
        """
        t = self.store.tasks.get(task_id)
        if t is None:
            return
        if t.status not in FINISHABLE:
            # A provider can report a turn for work the engine is no longer
            # waiting on: one it already gave up on, skipped or cancelled, or
            # one a retriable failure has already put back to `ready`. Dropping
            # it is not defensive — every one of those states has no legal edge
            # to `verifying`, so replaying the report would raise, and a late
            # success must not un-skip work the user dropped by hand.
            return
        w = self.store.workflows.get(t.workflow_id)
        if w is None:
            return

        if t.status == "dispatched":
            # The provider confirmed. `dispatched` exists precisely as the
            # window between "we asked" and "it answered" (spec §7.2), and
            # closing it here is what lets reconciliation tell a task that
            # never started from one that did.
            t.transition("running")
            self.store.tasks.update(t)
        elif t.status == "waiting_approval":
            # The turn landed while the task was still parked on an approval
            # (the answer and the turn can arrive in either order). `running`
            # is the only edge out of it towards a verdict.
            t.transition("running")
            self.store.tasks.update(t)

        if not ok:
            self._fail(t, w, error or "the agent did not report a reason")
            await self.advance(w.id)
            return

        if result:
            t.result = dict(result)
        # `verifying` is entered even with no checks declared, so the timeline
        # never shows work completing without the step that decided it (§7.2).
        t.transition("verifying")
        self.store.tasks.update(t)
        self._publish(EventType.TASK_VERIFYING, w, payload={
            "workflow_id": w.id, "task_id": t.id, "title": t.title,
            "checks": list(t.verification)})
        # THE CHECKS ACTUALLY RUN. Until they did, every `verification` entry
        # in every shipped template was decoration: a test task reported
        # complete with the suite red, and the reviewer — and then the user —
        # was handed that as success.
        #
        # No checks declared = pass (there was nothing to prove), but the task
        # still came through `verifying` above. A check that could not RUN is
        # `unavailable`, and `passed()` does not count that as a pass: a
        # project with no test command cannot claim `tests_pass`.
        results = await self._verify(t, w)
        if not verify.passed(results):
            self._verification_failed(t, w, results)
            await self.advance(w.id)
            return
        t.transition("completed")
        self.store.tasks.update(t)
        self._publish(EventType.TASK_COMPLETED, w, payload={
            "workflow_id": w.id, "task_id": t.id, "title": t.title,
            "specialist_id": t.specialist_id, "attempts": t.attempts})
        self.journal.append(f"task '{t.title}' completed")
        await self.advance(w.id)

    # --- verification (spec §10) ------------------------------------------

    async def _verify(self, t: Task, w: Workflow) -> list[verify.VerificationResult]:
        """Run the task's declared checks and report every verdict.

        The engine never judges: services/verify.py owns what a check means,
        and this method only assembles what the checks are allowed to look at.
        Keeping the two apart is what lets `unavailable` be a single rule in
        one place instead of a default the scheduler could quietly soften.

        This AWAITS the checks inline, so a task's verification holds up the
        rest of this pass: `on_task_finished` is driven from the dispatcher's
        consumer loop. That is why verify.py bounds every command with a
        timeout — the bound is the whole of the protection, and a suite slower
        than DEFAULT_TIMEOUT_S is reported as a failure rather than waited on.
        """
        if not t.verification:
            return []
        m = self.store.missions.get(w.mission_id)
        project = self.store.projects.get(m.project_id) if m is not None else None
        ctx = verify.CheckContext(
            store=self.store, task=t, mission=m, project=project,
            # The mission's cwd, which is the ONLY tree a check may run in: a
            # test command resolved against the backend's own directory would
            # verify the wrong repository and pass.
            cwd=getattr(project, "root_path", None))
        return await verify.run_checks(t.verification, ctx)

    def _verification_failed(self, t: Task, w: Workflow,
                             results: list[verify.VerificationResult]) -> None:
        """Say which check said no and why, then route the task to `failed`
        through the normal path so the retry policy applies unchanged.

        Going through `_fail` rather than transitioning by hand is the point:
        a verification failure is a task failure, and it gets the same second
        attempt, the same `blocked` on exhaustion and the same deadlock report
        as an agent that errored. A parallel failure path here would be a
        second, weaker copy of the retry policy.
        """
        bad = verify.failures(results)
        why = verify.reason(results)
        self._publish(EventType.VERIFICATION_FAILED, w, payload={
            "workflow_id": w.id, "task_id": t.id, "title": t.title,
            "checks": list(t.verification),
            "failed": [r.to_dict() for r in bad],
            "reason": why[:ERROR_MAX],
            "attempt": t.attempts, "max_attempts": t.max_attempts,
            "will_retry": t.can_retry})
        self.journal.append(
            f"task '{t.title}' failed verification: {why}")
        # `derived=True`: verification.failed above just said the same reason
        # WITH the failing check named, so narrating task.failed too would tell
        # the user the same thing twice (narration/policy.py's opening rule).
        self._fail(t, w, why or "verification did not pass", derived=True)

    def _mark_failed(self, t: Task, w: Workflow, reason: str, *,
                     will_retry: bool, derived: bool = False) -> None:
        """Land a task on `failed` with a reason the user can act on, and say
        so. Applies no policy — the callers decide what happens next.

        `derived=True` marks a failure whose reason another carrier has ALREADY
        spoken (today: verification.failed). It is the same marker
        MissionService.set_status uses, read by narration/service.py, and it
        exists because ownership is per FACT, not per event type."""
        reason = " ".join((reason or "").split())[:ERROR_MAX]
        if t.status == "ready":
            # The table offers no `ready → failed` edge, and must not grow one:
            # `ready` means "dependencies satisfied, nothing asked of a
            # provider yet". A dispatch that dies before the provider is
            # reached (no specialist, a runner that would not start) is still
            # an ATTEMPTED dispatch, so it passes through `dispatched` — which
            # is never persisted, because the row is written once, below, and
            # already says `failed` by then.
            t.transition("dispatched")
        t.error = reason
        t.transition("failed")
        self.store.tasks.update(t)
        self._publish(EventType.TASK_FAILED, w, payload={
            "workflow_id": w.id, "task_id": t.id, "title": t.title,
            "specialist_id": t.specialist_id, "attempt": t.attempts,
            "max_attempts": t.max_attempts, "reason": reason,
            "will_retry": will_retry, "derived": derived})
        self.journal.append(f"task '{t.title}' failed: {reason}")

    def _fail(self, t: Task, w: Workflow, reason: str, *, auto_retry: bool = True,
              derived: bool = False) -> None:
        """Fail a task, then apply the retry policy. Exhausting the attempts
        lands on `blocked` and NOT on a failed workflow: `blocked` means "a
        human is needed", and the human retrying it is the entire reason the
        state exists (domain/task.py).

        `auto_retry=False` is for a failure raised INSIDE advance()'s dispatch
        loop. Putting such a task back to `ready` would strand it: the loop has
        already passed it, so nothing re-dispatches it this pass, and resting
        at `ready` is the one state `retry()` refuses — the user would be left
        with a parked workflow and a retry control that cannot act on it.
        Resting at `failed` is understood by both retry() and the deadlock
        report.
        """
        self._mark_failed(t, w, reason, will_retry=auto_retry and t.can_retry,
                          derived=derived)
        if t.can_retry:
            if auto_retry:
                t.transition("ready")
                self.store.tasks.update(t)
            return
        t.transition("blocked")
        self.store.tasks.update(t)
        self._publish(EventType.TASK_BLOCKED, w, payload={
            "workflow_id": w.id, "task_id": t.id, "title": t.title,
            "attempts": t.attempts, "reason": reason})

    # --- human interventions ----------------------------------------------
    #
    # None of these advances. The caller does, explicitly — which lets the
    # API/voice layer reassign, retry and skip several tasks and release the
    # work once, and keeps advance()'s "only one path to a provider" property
    # readable at the call site instead of hidden in five methods.

    async def retry(self, task_id: str, by: str) -> Task:
        """Put a failed or blocked task back in the queue, by hand.

        Raises the ceiling by one rather than resetting `attempts`: the two
        failures that led here are the record of what really ran, and erasing
        them would make the third attempt look like the first on the timeline.
        The bound stops the MACHINE from retrying, never the human.
        """
        t = self.get_task(task_id)
        if t.status not in ("failed", "blocked"):
            raise ValueError(
                f"task '{t.title}' is {t.status}; only a failed or blocked task "
                "can be retried")
        w = self.get(t.workflow_id)
        if not t.can_retry:
            t.max_attempts += 1
        t.error = None
        t.transition("ready")
        self.store.tasks.update(t)
        self.journal.append(f"task '{t.title}' retried by {by}")
        # A blocked task is what sent the workflow to waiting_for_human, so
        # clearing it has to hand the workflow back — otherwise advance()
        # refuses (its first line) and the retry is silently inert.
        self._back_to_running(w, by)
        return t

    async def skip(self, task_id: str, by: str) -> Task:
        """Drop a task and let its dependents proceed: `skipped` satisfies a
        dependency exactly as `completed` does, which is what makes a blocked
        branch recoverable without editing the graph."""
        t = self.get_task(task_id)
        w = self.get(t.workflow_id)
        if t.status == "dispatched":
            # The table offers no dispatched → skipped edge, and must not grow
            # one: `ready` is the un-dispatch state (spec §13 uses it for a
            # task whose session never started), so skipping in-flight work is
            # honestly "take it back, then drop it".
            t.transition("ready")
        # For `running`/`verifying`/`waiting_approval` this raises: an agent is
        # mid-turn and there is no path from there to `skipped`. Cancel the
        # workflow or let the turn land — silently pretending the work was
        # dropped while the agent keeps editing the tree would be worse.
        t.transition("skipped")
        self.store.tasks.update(t)
        self.journal.append(f"task '{t.title}' skipped by {by}")
        self._back_to_running(w, by)
        return t

    async def assign(self, task_id: str, specialist_id: str, by: str) -> Task:
        """Pin the specialist a task dispatches to. Validated now, not at
        dispatch: a pin that cannot cover the task's `requires` would otherwise
        surface as a failed task minutes later, long after the user was told
        the choice was accepted."""
        t = self.get_task(task_id)
        if t.status not in ASSIGNABLE:
            raise ValueError(
                f"task '{t.title}' is {t.status}; a task can only be reassigned "
                f"while it is {' or '.join(ASSIGNABLE)}")
        if t.role:
            # Raises NoSpecialist, whose message already names the role, the
            # missing capabilities and the fix.
            s = self.roster.resolve(t.role, frozenset(t.requires), pinned=specialist_id)
        else:
            s = self.roster.get(specialist_id)
            if s.archived:
                raise ValueError(f"'{s.name}' is archived and cannot take new work")
        t.specialist_id = s.id
        t.updated_at = utcnow()
        self.store.tasks.update(t)
        self.journal.append(f"task '{t.title}' assigned to {s.name} by {by}")
        return t

    async def pause(self, workflow_id: str, by: str) -> None:
        """Stop scheduling. In-flight tasks are NOT interrupted here — the
        engine holds no session handles; stopping agents is the caller's
        (MissionService.pause already does it for the mission)."""
        w = self.get(workflow_id)
        if w.transition("paused"):
            self.store.workflows.update(w)
            self._note_status(w, "paused", by)

    async def resume(self, workflow_id: str, by: str = "ui") -> None:
        """draft/paused/waiting_for_human → running. Schedules nothing: the
        caller advances, so a plan can be confirmed and released in two
        observable steps rather than one."""
        w = self.get(workflow_id)
        if w.transition("running"):
            self.store.workflows.update(w)
            self._note_status(w, "running", by)

    async def cancel(self, workflow_id: str, by: str, reason: str | None = None) -> None:
        """Cancel the workflow and every task that had not finished.

        Completed and skipped tasks keep their status: they really did happen,
        and rewriting them to `cancelled` would erase work the user may want to
        see. Only what was outstanding is cancelled.
        """
        w = self.get(workflow_id)
        for t in self.store.tasks.for_workflow(w.id):
            if t.status in DONE:
                continue
            t.transition("cancelled")
            t.error = t.error or reason
            self.store.tasks.update(t)
        if w.transition("cancelled"):
            self.store.workflows.update(w)
            self._note_status(w, "cancelled", by, reason)

    # --- terminal outcomes ------------------------------------------------

    def _to_human(self, w: Workflow, why: str, blocking: list[str]) -> None:
        """A bound or a deadlock: `waiting_for_human`, never `failed` (§12).

        The payload names the blocking tasks because `waiting_for_human` with
        no reason is a stall the user cannot act on — and this event is the
        only thing that distinguishes "stuck" from "still working".

        Every reason rides on `workflow.deadlocked`: spec §11 declares no
        second event for a bound, and inventing one would leave a type with no
        narration owner (test_narration_policy). `why` separates them.
        """
        if w.transition("waiting_for_human"):
            self.store.workflows.update(w)
        self._publish(EventType.WORKFLOW_DEADLOCKED, w, payload={
            "workflow_id": w.id, "why": why, "blocking": blocking[:BLOCKING_MAX],
            "blocking_count": len(blocking)})
        self.journal.append(
            f"workflow {w.id[:8]} needs a human ({why}): {', '.join(blocking[:BLOCKING_MAX])}")

    def _complete(self, w: Workflow) -> None:
        if w.transition("completed"):
            self.store.workflows.update(w)
        self._publish(EventType.WORKFLOW_COMPLETED, w, payload={"workflow_id": w.id})
        self.journal.append(f"workflow {w.id[:8]} completed")
        m = self.store.missions.get(w.mission_id)
        if m is None:
            return
        # The mission may have been paused or cancelled out from under the
        # workflow. Check the table rather than letting transition() raise:
        # finishing the last task must not 500 because someone paused the
        # mission a second earlier.
        if "completed" not in MISSION_TRANSITIONS.get(m.status, frozenset()):
            return
        frm = m.status
        m.transition("completed")
        self.store.missions.update(m)
        # NOT marked `derived`, deliberately: this is the event that SPEAKS
        # the mission finishing, because it carries the title and so produces
        # a better sentence than workflow.completed can ('"the billing fix" is
        # done.'). workflow.completed is owner "none" for that reason. If the
        # mission cannot transition (paused or cancelled out from under us, the
        # guard above), nothing is spoken — which is right: the user did that.
        self.bus.publish(YuriEvent.make(
            EventType.MISSION_STATUS_CHANGED, mission_id=m.id, project_id=m.project_id,
            payload={"from": frm, "to": "completed", "by": "system", "reason": None,
                     "title": m.title}))
        self.journal.append(f"mission '{m.title}': {frm} → completed")

    # --- plumbing ---------------------------------------------------------

    def _back_to_running(self, w: Workflow, by: str) -> None:
        """Hand a waiting_for_human workflow back to the scheduler. Only from
        that state: a paused or draft workflow was stopped on purpose, and a
        retry must not restart it behind the user's back."""
        if w.status == "waiting_for_human" and w.transition("running"):
            self.store.workflows.update(w)
            self._note_status(w, "running", by)

    def _publish(self, type: str, w: Workflow, payload: dict) -> None:
        self.bus.publish(YuriEvent.make(type, mission_id=w.mission_id, payload=payload))

    def _note_status(self, w: Workflow, to: str, by: str,
                        reason: str | None = None) -> None:
        # Workflow status rides on workflow.created's type rather than getting
        # a type of its own: spec §11 lists no workflow.status_changed, and an
        # event type with no narration owner fails test_narration_policy. The
        # journal carries the audit trail.
        self.journal.append(f"workflow {w.id[:8]} → {to} by {by}"
                            + (f" ({reason})" if reason else ""))

    @staticmethod
    def _age_s(w: Workflow) -> float:
        try:
            started = datetime.datetime.fromisoformat(w.created_at.replace("Z", "+00:00"))
        except ValueError:
            # An unparseable timestamp must not be read as "infinitely old" —
            # that would park a healthy workflow on waiting_for_human.
            return 0.0
        return (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()


def _planned_from_mapping(d: dict) -> PlannedTask:
    """A caller-supplied task dict → a PlannedTask. Field names match the
    template schema exactly (loader's module docstring), so the two read the
    same and a graph can be moved between them without translation."""
    return PlannedTask(
        id=d["id"],
        role=d.get("role"),
        title=d.get("title", d["id"]),
        instruction=d.get("instruction", ""),
        depends_on=tuple(d.get("depends_on", ())),
        read_only=bool(d.get("read_only", False)),
        verification=tuple(d.get("verification", ())),
        requires=tuple(d.get("requires", ())),
        kind=d.get("kind", "agent_task"),
        specialist_id=d.get("specialist_id"),
    )


# `id` and `depends_on` are deliberately absent: they ARE the graph, and
# validate() proves the graph acyclic and complete. Rewiring an edge after that
# check is exactly how a cycle would get past it.
_OVERRIDABLE = ("role", "title", "instruction", "read_only", "verification",
                "requires", "kind", "specialist_id")


def _apply_override(t: PlannedTask, over: dict) -> PlannedTask:
    fields = {k: v for k, v in over.items() if k in _OVERRIDABLE}
    return replace(t, **fields) if fields else t
