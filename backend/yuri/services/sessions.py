"""SessionService (spec §5.1) — the seam where the existing voice tools meet
the Yuri domain. Every handler in tools.py that touches a session goes through
here, so mission/session/approval rows and events happen as a side effect of
flows that already exist. Provider calls are forwarded unchanged in shape.

WHY the odd-looking bits:

* `resolve()` accepts four ref forms (Yuri id, native handle, unique handle
  prefix, case-insensitive name) because the voice model habitually passes
  whichever it last heard — usually the *name*. An unresolvable ref raises
  KeyError listing the active names so tools.py can turn it into a soft error
  the model recovers from; an *ambiguous* prefix OR name also raises rather
  than silently picking a session, because picking the wrong one sends a
  message to the wrong agent.
* Names are de-duped on every path that puts a row INTO the live set — a new
  session (`_pick_name`), a revived `lost` row and a rehydrated insert (both
  `_dedupe_name`). `_pick_name` only knows about rows that are live *right
  now*, so a name it handed out can collide with one held by a row that later
  comes back; without the de-dupe two live rows share a name and resolve(name)
  silently picks whichever the store returns first.
* Events are emitted by exactly one path. A provider whose
  `capabilities().supports_events` is True already streams turn/question/error
  signals through the observer (`on_provider_event`), so `poll()` updates rows
  and mission state for such a provider but does NOT re-emit them — otherwise
  every turn would be recorded twice. Approvals are safe on both paths because
  `ApprovalService.record_request` dedups on `request_id`.
* `stop()` pauses the mission; it never completes it. Closing a session is not
  evidence the work succeeded (spec §38: never report unverified success).
* `set_mode()` snapshots the pending prompt BEFORE switching, because the
  runner resolves a now-covered prompt asynchronously — reading it afterwards
  races with that resolution.
"""
from __future__ import annotations

import logging
import os
import shlex
from typing import Callable

from claude_runner import normalize_mode
from permissions import mode_covers
from yuri.domain.event import EventType, YuriEvent
from yuri.domain.mission import InvalidTransition
from yuri.domain.session import AgentSession
from yuri.events.bus import EventBus
from yuri.narration.policy import DEFAULT_MODE, Mode
from yuri.narration.service import NarrationService
from yuri.providers.base import AgentProvider, ProjectContext, ProviderEvent, SessionOptions
from yuri.providers.registry import AgentRegistry
from yuri.services.approvals import ApprovalService
from yuri.services.journal import Journal
from yuri.services.missions import MissionService
from yuri.services.projects import ProjectService
from yuri.services.router import AgentRouter
from yuri.store.base import Store

log = logging.getLogger("yuri.sessions")

# poll() statuses that mean "a turn is in flight" (claude_runner.Status uses
# "running"; the runners' own poll_status shortcut uses "working").
_IN_FLIGHT = ("working", "running")


class SessionService:
    def __init__(self, store: Store, bus: EventBus, journal: Journal, registry: AgentRegistry,
                 projects: ProjectService, approvals: ApprovalService, missions: MissionService,
                 default_agent: str = "claude-code", router: AgentRouter | None = None,
                 narration: NarrationService | None = None,
                 mode_reader: Callable[[], Mode] | None = None):
        self.store = store
        self.bus = bus
        self.journal = journal
        self.registry = registry
        self.projects = projects
        self.approvals = approvals
        self.missions = missions
        self.default_agent = default_agent
        self.router = router or AgentRouter(registry, default_agent)
        self.narration = narration or NarrationService()
        # The mode lives in the store, which yuri.app reads — importing app here
        # would be a cycle, so the container injects a reader instead.
        self._mode_reader = mode_reader or (lambda: DEFAULT_MODE)

    # --- lookup ---------------------------------------------------------------

    def _native_map(self) -> tuple[dict[str, tuple[AgentProvider, dict]], set[str]]:
        """(handle -> (provider, runner dict), ids of providers that answered).

        A provider whose `list_native()` raises must not take the whole service
        down with it — resolve/list/poll all read this — so failures are logged
        and that provider is simply absent from the second element. rehydrate()
        needs that distinction: "the provider could not be enumerated" is not
        the same claim as "its sessions are gone".
        """
        out: dict[str, tuple[AgentProvider, dict]] = {}
        answered: set[str] = set()
        for p in self.registry.all():
            try:
                listed = p.list_native()
            except Exception:
                log.exception("list_native failed for provider %s", p.id)
                continue
            answered.add(p.id)
            for s in listed or []:
                handle = s.get("handle")
                if handle:
                    out[handle] = (p, s)
        return out, answered

    def _native(self) -> dict[str, tuple[AgentProvider, dict]]:
        return self._native_map()[0]

    def _provider_for(self, handle: str) -> AgentProvider:
        row = self.row_for(handle)
        if row is not None:
            try:
                return self.registry.get(row.agent_id)
            except KeyError:
                pass
        entry = self._native().get(handle)
        if entry is not None:
            return entry[0]
        raise KeyError(f"unknown session: {handle}")

    def _agent_name(self, agent_id: str | None) -> str:
        """A provider's display name, or "" when that agent is not registered.
        Guarded for the same reason `_provider_for` is: a stored row outlives
        its provider whenever YURI_AGENTS changes between runs."""
        if not agent_id:
            return ""
        try:
            return self.registry.get(agent_id).name
        except KeyError:
            return ""

    def row_for(self, handle: str) -> AgentSession | None:
        return self.store.sessions.get_by_native(handle)

    def live_rows(self) -> list[AgentSession]:
        """Session rows in a live status. Public so the API layer can count and
        list sessions without reaching into the store itself (spec §5.8)."""
        return self.store.sessions.list(live_only=True)

    def get(self, session_id: str) -> AgentSession:
        """One session row by its Yuri id. KeyError when there is no such row."""
        row = self.store.sessions.get(session_id)
        if row is None:
            raise KeyError(f"unknown session: {session_id}")
        return row

    def _active_names(self, native: dict) -> list[str]:
        return sorted(r.name for r in self.live_rows()
                      if r.name and r.native_session_id in native)

    def resolve(self, ref: str) -> str:
        """A Yuri session id, native handle, unique handle prefix or name (any
        case) -> the native handle. KeyError when nothing (or more than one
        thing) matches."""
        ref = (ref or "").strip()
        native = self._native()
        if ref in native:
            return ref
        row = self.store.sessions.get(ref) if ref else None
        if row is not None and row.native_session_id in native:
            return row.native_session_id
        low = ref.lower()
        # A name is de-duped on every path into the live set, so more than one
        # live row answering to it means that invariant broke. Refuse, exactly
        # as the prefix branch below does: silently picking one would send the
        # message to the wrong agent.
        name_hits = list(dict.fromkeys(
            r.native_session_id for r in self.live_rows()
            if r.name and r.name.lower() == low and r.native_session_id in native))
        if len(name_hits) > 1:
            raise KeyError(f"'{ref}' is ambiguous — {len(name_hits)} live sessions answer to that "
                           f"name. Active session names: {self._active_names(native)}.")
        if name_hits:
            return name_hits[0]
        hits = [h for h in native if ref and h.startswith(ref)]
        if len(hits) == 1:
            return hits[0]
        names = self._active_names(native) or "(none named yet)"
        if len(hits) > 1:
            raise KeyError(f"'{ref}' is ambiguous — it matches {len(hits)} sessions. "
                           f"Active session names: {names}.")
        raise KeyError(f"no session matches '{ref}'. Active session names: {names}.")

    def list(self) -> list[dict]:
        rows = {r.native_session_id: r for r in self.live_rows()}
        out: list[dict] = []
        for handle, (p, s) in self._native().items():
            r = rows.get(handle)
            out.append({**s, "agent_id": p.id, "name": r.name if r else None,
                        "mission_id": r.mission_id if r else None,
                        "yuri_session_id": r.id if r else None})
        return out

    def native_pane(self, ref: str) -> str | None:
        try:
            handle = self.resolve(ref)
            return self._provider_for(handle).native_pane(handle)
        except KeyError:
            return None

    # --- names ------------------------------------------------------------------

    def _taken(self, exclude: str | None = None) -> set[str]:
        return {r.name.lower() for r in self.live_rows() if r.name and r.id != exclude}

    def default_name_for(self, cwd: str) -> str:
        base = os.path.basename(os.path.normpath(cwd)) or "session"
        taken = self._taken()
        if base.lower() not in taken:
            return base
        i = 2
        while f"{base} {i}".lower() in taken:
            i += 1
        return f"{base} {i}"

    def _pick_name(self, requested: str | None, cwd: str) -> str:
        name = " ".join((requested or "").split())
        if name and name.lower() not in self._taken():
            return name
        return self.default_name_for(cwd)

    def _dedupe_name(self, name: str | None, exclude: str | None = None) -> str | None:
        """`name`, or the first free `f"{base} {i}"` when a live row already
        holds it — the same walk `default_name_for` uses.

        Called on every path that re-admits or inserts a row into the live set
        (the revive branch and the rehydrate insert in `rehydrate()`).
        `_pick_name` can only see rows that are live at the time it runs, so a
        name it handed out may already belong to a row that is about to come
        back; two live rows with one name make `resolve(name)` ambiguous, and
        ambiguity there means messages reaching the wrong agent. Renaming the
        arrival is the same choice the pre-Yuri session_manager made in its
        rehydrate path ("defensive de-dupe in case two restored metas carried
        the same name"); that defence was lost in the move to this service."""
        if not name:
            return name
        taken = self._taken(exclude=exclude)
        base, candidate, i = name, name, 2
        while candidate.lower() in taken:
            candidate = f"{base} {i}"
            i += 1
        return candidate

    # --- lifecycle ----------------------------------------------------------------

    async def start(self, project_ref: str, backend: str = "cli", mode: str = "default",
                    model: str | None = None, name: str | None = None, created_by: str = "voice",
                    agent_id: str | None = None) -> dict:
        project = self.projects.resolve_or_create(project_ref)
        agent = self.router.select(project, agent_id)
        sess_name = self._pick_name(name, project.root_path)
        mission = self.missions.create(project, sess_name, created_by=created_by, agent_id=agent.id)
        mode = normalize_mode(mode)
        try:
            handle = await agent.create_session(ProjectContext(project.id, project.root_path),
                                                SessionOptions(backend=backend, mode=mode, model=model,
                                                               name=sess_name))
        except Exception as exc:
            # Never leave a `running` mission with no session behind. The
            # bookkeeping is best-effort: a failure in it must not mask the
            # provider's own exception, which is what the caller needs to hear.
            try:
                self.missions.set_status(mission, "failed", by="system",
                                         reason=f"{agent.name} unavailable: {exc}")
                self.bus.publish(YuriEvent.make(EventType.AGENT_ERROR, mission_id=mission.id,
                                                agent_id=agent.id, project_id=project.id,
                                                payload={"message": str(exc)}))
            except Exception:
                log.exception("failed to record the mission failure for %s", mission.id)
            raise
        backend_tag = agent.backend_of(handle) or backend
        row = AgentSession(project_id=project.id, agent_id=agent.id, native_session_id=handle,
                           backend=backend_tag, working_directory=project.root_path,
                           mission_id=mission.id, status="idle", name=sess_name, mode=mode, model=model)
        self.store.sessions.insert(row)
        step = self.store.missions.steps_for(mission.id)[0]
        step.session_id = row.id
        self.store.missions.update_step(step)
        self._persist_name(agent, handle, sess_name)
        self.bus.publish(YuriEvent.make(EventType.SESSION_CREATED, mission_id=mission.id, session_id=row.id,
                                        agent_id=agent.id, project_id=project.id,
                                        payload={"name": sess_name, "native_session_id": handle,
                                                 "backend": backend_tag, "mode": mode,
                                                 "cwd": project.root_path}))
        return {"session_id": handle, "name": sess_name, "project_path": project.root_path,
                "backend": backend_tag, "mode": mode,
                "message": f"Started {agent.name} session '{sess_name}' in {project.root_path}.",
                "mission_id": mission.id, "yuri_session_id": row.id}

    async def adopt(self, native_id: str, cwd: str, name: str | None = None) -> dict:
        agent = self.registry.get(self.default_agent)
        # "Already adopted?" means "the provider already has this session" —
        # NOT "it has a tmux pane". Backends without a TUI (the SDK runner)
        # always return None from native_pane(), so keying off the pane would
        # adopt an already-adopted session a second time and leave a duplicate
        # mission + session row behind. Asking the provider (rather than the
        # row) also keeps a session that has since died re-adoptable.
        row = self.row_for(native_id)
        entry = self._native().get(native_id)
        if entry is not None:
            owner, native = entry
            # Both branches must hand Task 17 the same shape (main.py puts
            # `attach` straight into a message the user reads): the real pane or
            # None. And never echo the caller's raw, unresolved cwd ref — use
            # the resolved path the row or the runner already holds.
            return {"session_id": native_id, "name": row.name if row else None,
                    "cwd": self._resolved_cwd(row, native, cwd),
                    "attach": self._attach_for(owner, native_id), "already": True,
                    "mission_id": row.mission_id if row else None}
        project = self.projects.resolve_or_create(cwd)
        sess_name = self._pick_name(name, project.root_path)
        mission = self.missions.create(project, sess_name, created_by="handoff", agent_id=agent.id)
        handle = await agent.resume(native_id, ProjectContext(project.id, project.root_path),
                                    SessionOptions(backend="cli", name=sess_name))
        row = AgentSession(project_id=project.id, agent_id=agent.id, native_session_id=handle, backend="cli",
                           working_directory=project.root_path, mission_id=mission.id, status="idle",
                           name=sess_name)
        self.store.sessions.insert(row)
        self._persist_name(agent, handle, sess_name)
        self.bus.publish(YuriEvent.make(EventType.SESSION_CREATED, mission_id=mission.id, session_id=row.id,
                                        agent_id=agent.id, project_id=project.id,
                                        payload={"name": sess_name, "native_session_id": handle,
                                                 "backend": "cli", "adopted": True}))
        return {"session_id": handle, "name": sess_name, "cwd": project.root_path,
                "attach": self._attach_for(agent, handle), "already": False,
                "mission_id": mission.id}

    def _attach_for(self, provider: AgentProvider, handle: str) -> str | None:
        """The co-drive command, or None when this backend has no pane. Never
        guess a pane name: a fabricated `tmux attach -t vc_xxxxxxxx` reaches the
        user as an instruction that fails when they paste it."""
        pane = provider.native_pane(handle)
        return f"tmux attach -t {pane}" if pane else None

    def _resolved_cwd(self, row: AgentSession | None, native: dict, ref: str) -> str:
        cwd = (row.working_directory if row else None) or native.get("cwd") or ""
        return cwd or self.projects.resolve_or_create(ref).root_path

    def _persist_name(self, agent: AgentProvider, handle: str, name: str) -> None:
        persist = getattr(agent, "persist_name", None)
        if persist:
            try:
                persist(handle, name)
            except Exception:
                log.debug("persist_name failed", exc_info=True)

    def _touch(self, row: AgentSession | None, status: str | None = None) -> None:
        if row is None:
            return
        if status:
            row.status = status
        row.touch()
        self.store.sessions.update(row)

    def send(self, ref: str, message: str) -> dict:
        handle = self.resolve(ref)
        row = self.row_for(handle)
        if row is not None and row.mission_id:
            self.missions.set_goal_if_empty(self.missions.get(row.mission_id), message)
        self._provider_for(handle).send_message(handle, message)
        self._touch(row, "running")
        self.bus.publish(self._ev(EventType.SESSION_MESSAGE_SENT, row, handle, {"message": message[:500]}))
        return {"status": "working", "session_id": handle}

    def answer(self, ref: str, choice: str) -> dict:
        handle = self.resolve(ref)
        row = self.row_for(handle)
        if row is not None:
            # Resolve BEFORE forwarding: an ambiguous spoken answer raises here
            # and must not reach the agent as a decision (fail closed).
            self.approvals.resolve_by_session(row, choice, by="voice")   # None when it's a choice prompt
        self._provider_for(handle).answer(handle, choice)
        self._touch(row, "running")
        return {"status": "working", "session_id": handle}

    def answer_approval(self, approval_id: str, decision: str, by: str) -> dict:
        """Record a UI/API decision on one approval and forward it to the agent.

        Owned by the service, not the route: the route's own copy of this flow
        skipped the `_touch`/mission update, so after an approve the session row
        still read `needs_permission` and the mission `waiting_for_approval`
        until the next poll happened to heal it.

        `forwarded` stays honest — the decision is recorded either way (that is
        the user's answer), and `forwarded` says only whether a live agent was
        actually told. Raises KeyError (unknown approval) / ValueError (already
        resolved, bad decision) for the caller to map."""
        a = self.approvals.resolve(approval_id, decision, by=by)
        out = {**a.to_dict(), "forwarded": False}
        row = self.store.sessions.get(a.session_id)
        # Nothing live to tell (row missing, or already stopped) is not an
        # error; it just did not reach an agent.
        if row is None or not row.is_live:
            return out
        try:
            self.registry.get(row.agent_id).answer(row.native_session_id,
                                                   "allow" if decision == "allowed" else "deny")
        except Exception as exc:      # the decision stands even if the agent is gone
            log.warning("approval %s recorded but not forwarded: %s", a.id, exc)
            return {**out, "error": str(exc)}
        self._touch(row, "running")
        self._mission_to(row, "running", "approval answered")
        return {**out, "forwarded": True}

    def poll(self, ref: str) -> dict:
        handle = self.resolve(ref)
        p = self._provider_for(handle)
        res = p.poll(handle)
        row = self.row_for(handle)
        if row is None:
            return {**res, "narration": None}
        status = res.get("status")
        emits = not p.capabilities().supports_events   # otherwise the observer already did
        if status == "needs_permission":
            prompt = res.get("prompt")
            if prompt:
                approval = self.approvals.record_request(row, prompt)   # dedups on request_id
                res = {**res, "risk": approval.risk}
            self._touch(row, "needs_permission")
            self._mission_to(row, "waiting_for_approval", "agent asked for permission")
        elif status == "needs_choice":
            self._touch(row, "needs_choice")
            if emits:
                self.bus.publish(self._ev(EventType.SESSION_QUESTION, row, handle,
                                          {"text": (res.get("prompt") or {}).get("text", "")}))
        elif status == "completed":
            self._touch(row, "idle")
            self._mission_to(row, "running", None)
            if emits:
                self._turn_completed(row, handle, res.get("assistant_text", ""), [])
        elif status == "error":
            self._touch(row, "idle")
            if emits:
                self.bus.publish(self._ev(EventType.AGENT_ERROR, row, handle, {"message": res.get("error", "")}))
            self._fail_if_alone(row, res.get("error") or "agent error")
        elif status in _IN_FLIGHT:
            self._touch(row, "running")
        # The frontend's whole rule is "if it has a narration line, inject it".
        # Poll owns the four session-turn events (yuri/narration/policy.py); the
        # stream must not also narrate them or the user hears each one twice.
        # Name the agent from the provider we ALREADY resolved, never from a
        # fresh `registry.get(row.agent_id)`: a stored row outlives its provider
        # whenever YURI_AGENTS changes between runs (which is exactly why
        # `_provider_for` guards the same lookup), and a KeyError here is
        # swallowed by the frontend's "transient; keep polling" catch — the
        # session would then poll every 1.5s and never narrate again.
        res = {**res, "narration": self.narration.line_for_poll(
            res, row.name, p.name, self._mode_reader())}
        return res

    async def interrupt(self, ref: str) -> dict:
        handle = self.resolve(ref)
        await self._provider_for(handle).interrupt(handle)
        row = self.row_for(handle)
        self._touch(row, "idle")
        self.bus.publish(self._ev(EventType.SESSION_INTERRUPTED, row, handle, {}))
        return {"status": "interrupted", "session_id": handle}

    async def stop(self, ref: str) -> dict:
        handle = self.resolve(ref)
        await self._provider_for(handle).stop(handle)
        row = self.row_for(handle)
        self._touch(row, "stopped")
        self.bus.publish(self._ev(EventType.SESSION_STOPPED, row, handle, {}))
        if row is not None and row.mission_id:
            others = [s for s in self.store.sessions.list(mission_id=row.mission_id, live_only=True)
                      if s.id != row.id]
            if not others:
                # paused, never completed: a closed session is not a finished job.
                self._mission_to(row, "paused", "session closed")
        return {"status": "closed", "session_id": handle}

    async def stop_many(self, rows: list[AgentSession]) -> None:
        """Best effort: one wedged provider must not strand the rest (this is
        what MissionService.cancel depends on to finish cancelling)."""
        for r in rows:
            try:
                await self.stop(r.native_session_id)
            except KeyError:
                # resolve() found no live handle. "It is gone" is only an honest
                # claim when the owning provider actually ANSWERED list_native()
                # and did not list this handle — _native_map() swallows a
                # provider whose list_native() raises, and after that every one
                # of its handles fails resolve() with KeyError. Claiming
                # `stopped` there would be exactly the unverified assertion the
                # sibling branch below was fixed not to make (spec §38), so use
                # rehydrate()'s `answered` distinction.
                if r.agent_id in self._native_map()[1]:
                    self._touch(r, "stopped")
                else:
                    log.warning("cannot verify that session %s stopped — provider %s did not "
                                "answer; marking it lost", r.id, r.agent_id)
                    self._touch(r, "lost")
            except Exception:
                # We do NOT know this session ended, so we must not record that
                # it did (spec §38). `lost` is the honest label: it drops out of
                # the live set so the cancel can finish, without claiming a
                # clean close we have no evidence for.
                log.exception("stop failed for session %s; marking it lost", r.id)
                self._touch(r, "lost")

    async def interrupt_many(self, rows: list[AgentSession]) -> None:
        """Interrupt each session, surviving a provider that fails on one.

        Unlike stop_many this records nothing on failure: an interrupt that did
        not land leaves the session exactly as it was (still live, still
        whatever status it held), so there is no honest status change to make
        (spec §38). MissionService.pause depends on this returning rather than
        raising, so one wedged provider cannot block the pause."""
        for r in rows:
            try:
                await self.interrupt(r.native_session_id)
            except Exception:
                log.exception("interrupt failed for session %s (%s)", r.id, r.agent_id)

    async def set_mode(self, ref: str, mode: str) -> dict:
        handle = self.resolve(ref)
        p = self._provider_for(handle)
        native = self._native().get(handle, (None, {}))[1]
        prompt = native.get("prompt")           # snapshot BEFORE the switch (runner resolves async)
        new_mode = await p.set_mode(handle, mode)
        row = self.row_for(handle)
        if row is not None:
            row.mode = new_mode
            self._touch(row)
        out: dict = {"session_id": handle, "mode": new_mode}
        if prompt and prompt.get("kind") == "permission":
            if mode_covers(new_mode, prompt.get("tool_name", "")):
                if row is not None:
                    a = self.store.approvals.pending_for_session(row.id)
                    if a is not None:
                        try:
                            self.approvals.resolve(a.id, "allowed", by="mode_switch")
                        except ValueError:
                            log.warning("approval %s was already resolved before the mode switch", a.id)
                out["prompt_resolved"] = True
                out["message"] = (f"Mode is now '{new_mode}'. The pending permission ({prompt['text']}) "
                                  "was approved under the new mode — the session is continuing.")
            else:
                out["prompt_resolved"] = False
                out["message"] = (f"Mode is now '{new_mode}', but the pending permission ({prompt['text']}) "
                                  "is NOT covered by it and still needs an allow/deny from the user.")
        return out

    def rename(self, ref: str, name: str) -> dict:
        handle = self.resolve(ref)
        row = self.row_for(handle)
        clean = " ".join((name or "").split())
        if not clean:
            raise ValueError("name cannot be empty")
        if clean.lower() in self._taken(exclude=row.id if row else None):
            raise ValueError(f"the name '{clean}' is already used by another session; pick a different one")
        if row is not None:
            row.name = clean
            self._touch(row)
            if row.mission_id:
                m = self.missions.get(row.mission_id)
                m.title = clean
                self.store.missions.update(m)
        self._persist_name(self._provider_for(handle), handle, clean)
        return {"session_id": handle, "name": clean, "message": f"Renamed the session to '{clean}'."}

    async def peek(self, ref: str, lines: int = 40) -> dict:
        handle = self.resolve(ref)
        p = self._provider_for(handle)
        screen = await p.peek(handle, lines)
        # `screen is None` means "this backend has no live screen" — an empty
        # string means "the screen is blank", which is a different thing.
        out: dict = {"session_id": handle,
                     "screen": screen if screen is not None else (await p.read(handle)) or "(no output yet)"}
        if screen is None:
            out["note"] = "This backend has no live screen; showing accumulated text."
        native = self._native().get(handle, (None, {}))[1]
        if native.get("prompt"):
            out["pending_prompt"] = native["prompt"]
            out["note_prompt"] = "This session is waiting on the prompt above — answer it with answer_prompt."
        return out

    async def read(self, ref: str) -> dict:
        handle = self.resolve(ref)
        return {"session_id": handle, "text": await self._provider_for(handle).read(handle)}

    async def send_keys(self, ref: str, items: list[dict]) -> dict:
        handle = self.resolve(ref)
        return await self._provider_for(handle).send_keys(handle, items)

    def run_slash(self, ref: str, text: str) -> dict:
        handle = self.resolve(ref)
        self._provider_for(handle).run_slash(handle, text)
        self._touch(self.row_for(handle), "running")
        return {"status": "working", "session_id": handle, "sent": text}

    def handoff_info(self, ref: str) -> dict:
        handle = self.resolve(ref)
        p = self._provider_for(handle)
        row = self.row_for(handle)
        native = self._native().get(handle, (None, {}))[1]
        cwd = (row.working_directory if row else None) or native.get("cwd", "")
        # `--resume` takes the AGENT's session id, which is not the handle on
        # every backend (the SDK runner's handle is Yuri-side; session_id is the
        # id Claude itself knows). The pre-Yuri handoff helper always used the
        # native session_id — keep that.
        resume_id = native.get("session_id") or handle
        resume = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(resume_id)}"
        pane = p.native_pane(handle)
        return {"session_id": handle, "name": row.name if row else None, "cwd": cwd,
                # Live co-drive: join the SAME process (CLI/tmux only).
                "attach_command": f"tmux attach -t {pane}" if pane else None,
                # Solo takeover: resume in a SEPARATE process.
                "resume_command": resume, "command": resume}

    # --- provider events → domain -------------------------------------------------

    def on_provider_event(self, agent_id: str, handle: str, ev: ProviderEvent) -> None:
        """Observer callback. Called from provider sync/async paths; must never
        raise (a bug here would break a turn)."""
        try:
            row = self.row_for(handle)
            if row is None:
                return
            k, p = ev.kind, ev.payload
            if k == "tool_started":
                # agent_name is what verbose narration reads to say
                # "<agent> is using <tool>" — no other publisher needs it, and
                # nothing else in the payload can supply it (agent_id lives at
                # the event level, and the narration service is pure).
                self.bus.publish(self._ev(EventType.TOOL_STARTED, row, handle,
                                          {"tool_name": p.get("tool_name"), "tool_input": p.get("tool_input"),
                                           "agent_name": self._agent_name(row.agent_id or agent_id)}))
            elif k == "needs_permission":
                self.approvals.record_request(row, {**p, "kind": "permission"})
                self._touch(row, "needs_permission")
                self._mission_to(row, "waiting_for_approval", "agent asked for permission")
            elif k == "needs_choice":
                self._touch(row, "needs_choice")
                self.bus.publish(self._ev(EventType.SESSION_QUESTION, row, handle,
                                          {"text": p.get("text", ""), "options": p.get("options", [])}))
            elif k == "turn_completed":
                self._touch(row, "idle")
                self._mission_to(row, "running", None)
                self._turn_completed(row, handle, p.get("assistant_text", ""), p.get("tools_used", []))
            elif k == "cost_updated":
                # Merge only what this event actually carried: a cost tick with
                # no model must not erase the model we already know.
                md = dict(row.runtime_metadata)
                for key in ("cost_usd", "model", "input_tokens", "output_tokens"):
                    if p.get(key) is not None:
                        md[key] = p[key]
                row.runtime_metadata = md
                self._touch(row)
                self.bus.publish(self._ev(EventType.COST_UPDATED, row, handle, dict(p)))
            elif k == "error":
                self.bus.publish(self._ev(EventType.AGENT_ERROR, row, handle, {"message": p.get("message", "")}))
                self._fail_if_alone(row, p.get("message") or "agent error")
        except Exception:
            log.exception("on_provider_event failed (%s %s)", agent_id, ev.kind)

    def _turn_completed(self, row: AgentSession, handle: str, text: str, tools_used: list) -> None:
        self.bus.publish(self._ev(EventType.SESSION_TURN_COMPLETED, row, handle,
                                  {"assistant_text": (text or "")[:2000], "tools_used": list(tools_used or [])}))
        self.journal.append(f"turn completed in '{row.name or handle[:8]}': {' '.join((text or '').split())[:160]}")

    def _mission_to(self, row: AgentSession, to: str, reason: str | None) -> None:
        if not row.mission_id:
            return
        try:
            self.missions.set_status(self.missions.get(row.mission_id), to, by="system", reason=reason)
        except InvalidTransition:
            pass   # e.g. paused mission receiving a late completion — leave it

    def _fail_if_alone(self, row: AgentSession, reason: str) -> None:
        """One session erroring fails the mission only when it was the mission's
        last live session — a sibling still working means the mission isn't dead."""
        if not row.mission_id:
            return
        others = [s for s in self.store.sessions.list(mission_id=row.mission_id, live_only=True)
                  if s.id != row.id]
        if not others:
            self._mission_to(row, "failed", reason)

    def _ev(self, event_type: str, row: AgentSession | None, handle: str, payload: dict) -> YuriEvent:
        payload = {**payload, "native_session_id": handle, "session_name": row.name if row else None}
        return YuriEvent.make(event_type, mission_id=row.mission_id if row else None,
                              session_id=row.id if row else None, agent_id=row.agent_id if row else None,
                              project_id=row.project_id if row else None, payload=payload)

    # --- restart ------------------------------------------------------------------------

    async def rehydrate(self) -> list[dict]:
        """Re-adopt what survived a backend restart: providers reattach, rows
        whose handle did not come back are marked `lost`, and handles with no
        row (pre-Yuri sessions) are recorded with mission_id=None."""
        restored: list[dict] = []
        for p in self.registry.all():
            try:
                restored.extend(await p.rehydrate())
            except Exception:
                log.exception("rehydrate failed for %s", p.id)
        native, answered = self._native_map()
        # A provider we could not enumerate tells us nothing about its sessions;
        # declaring them lost would be an unverified claim. A row whose agent is
        # not registered at all IS lost — nothing can ever serve it again.
        unreachable = {p.id for p in self.registry.all()} - answered
        for r in self.live_rows():
            if r.native_session_id in native or r.agent_id in unreachable:
                continue
            r.status = "lost"
            r.touch()
            self.store.sessions.update(r)
            self.bus.publish(self._ev(EventType.SESSION_LOST, r, r.native_session_id, {}))
            self.journal.append(f"session '{r.name or r.native_session_id[:8]}' lost across restart")
        for handle, (p, s) in native.items():
            existing = self.row_for(handle)
            if existing is not None:
                # A handle that comes back must revive its row. `lost` is only
                # ever a best guess: a provider can legitimately return partial
                # results without raising — tmux_runner.rehydrate() returns []
                # when tmux is missing, _live_panes() returns set() on any
                # non-zero rc (e.g. a server still starting), and it skips a
                # live pane whose meta is unreadable or whose _adopt failed. If
                # we never revived, that transient would permanently detach the
                # session: the row drops out of live_rows(), so resolve(name)
                # raises and list() reports it with no name or mission.
                if existing.status == "lost":
                    # Re-admitting a row to the live set must re-check its name:
                    # while it was `lost`, _pick_name could have handed that name
                    # to a NEW session, and two live rows with one name make
                    # resolve(name) ambiguous.
                    was = existing.name
                    existing.name = self._dedupe_name(existing.name, exclude=existing.id)
                    self._touch(existing, "idle")
                    if existing.name != was:
                        log.info("revived session %s was renamed '%s' -> '%s' (name taken while lost)",
                                 handle[:8], was, existing.name)
                        self._persist_name(p, handle, existing.name)
                    self.bus.publish(self._ev(EventType.SESSION_CREATED, existing, handle,
                                              {"name": existing.name, "native_session_id": handle,
                                               "backend": existing.backend, "revived": True}))
                    self.journal.append(
                        f"session '{existing.name or handle[:8]}' came back after being marked lost")
                continue
            cwd = s.get("cwd") or ""
            if not cwd:
                # resolve_project_path("") falls back to the first allowed root,
                # which would file this session under an arbitrary project.
                log.warning("rehydrated session %s reports no cwd; not recorded", handle[:8])
                continue
            try:
                project = self.projects.resolve_or_create(cwd)
            except ValueError:
                log.warning("rehydrated session %s has cwd outside allowed roots; not recorded", handle[:8])
                continue
            row = AgentSession(project_id=project.id, agent_id=p.id, native_session_id=handle,
                               # backend/mode are NOT NULL columns; a runner that
                               # reports them as None must not break startup.
                               backend=s.get("backend") or "cli", working_directory=project.root_path,
                               # Two restored metas can carry the same name (and a
                               # live row may already hold it) — see _dedupe_name.
                               status="idle", name=self._dedupe_name(s.get("name")),
                               mode=s.get("mode") or "default", model=s.get("model"))
            self.store.sessions.insert(row)
            if row.name and row.name != s.get("name"):
                self._persist_name(p, handle, row.name)
        return restored
