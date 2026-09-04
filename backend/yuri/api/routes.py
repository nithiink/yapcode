"""HTTP surface of the Yuri domain (spec §5.8). Routes validate, call a
service, and shape the response — no orchestration here. Built by
build_router(require_auth) so main.py's auth dependency applies to every route
without a circular import.

The two /events endpoints are the one remaining place that reads a repo
(`container().store.events`) directly: the event log has no service in front of
it — the EventBus is the write side — and adding one whose entire body proxies
`store.events.list` would put a layer in the way without owning any behavior.
Every other route goes through a service."""
from __future__ import annotations

import asyncio
import json
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from yuri.app import container, narration_mode, set_narration_mode
from yuri.domain.mission import InvalidTransition
from yuri.services.missions import MissionInUse
from yuri.narration.policy import MODES
from .schemas import NarrationUpdate, ProjectCreate

ACTIVE = ("running", "waiting_for_approval", "paused", "queued")

# A caller-supplied `limit` must never translate into an unbounded read of the
# event log (state store or SSE replay) — clamp both endpoints that accept one.
EVENTS_LIMIT_MAX = 1000


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, EVENTS_LIMIT_MAX))


def _by(request: Request) -> str:
    return "ui" if request.headers.get("origin") else "api"


def build_router(require_auth: Callable) -> APIRouter:
    r = APIRouter(prefix="/yuri", dependencies=[Depends(require_auth)])

    # --- context ------------------------------------------------------------
    @r.get("/context")
    async def context():
        c = container()
        health = await c.registry.health_all()
        missions = [m for m in c.missions.list() if m.status in ACTIVE][:20]
        projects = {p.id: p.name for p in c.projects.registered()}
        return {"home": c.home.path,
                "memory_user": c.memory.read_user(),
                "journal_today": c.journal.read_today(),
                "active_missions": [{"id": m.id, "title": m.title, "goal": m.goal, "status": m.status,
                                     "project": projects.get(m.project_id)} for m in missions],
                "agents": [{"id": p.id, "name": p.name, **health[p.id].to_dict()} for p in c.registry.all()],
                "narration_mode": narration_mode()}

    # --- narration ------------------------------------------------------------
    @r.get("/narration")
    async def get_narration():
        return {"mode": narration_mode(), "modes": list(MODES)}

    @r.put("/narration")
    async def put_narration(body: NarrationUpdate):
        try:
            return {"mode": set_narration_mode(body.mode)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # --- projects -----------------------------------------------------------
    @r.get("/projects")
    async def list_projects():
        return container().projects.list()

    @r.post("/projects", status_code=201)
    async def create_project(body: ProjectCreate):
        try:
            return container().projects.register(body.path, body.name, body.default_agent).to_dict()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @r.get("/projects/{project_id}")
    async def get_project(project_id: str):
        try:
            return container().projects.get(project_id).to_dict()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @r.put("/projects/{project_id}/verify")
    async def set_project_verify(project_id: str, body: dict):
        """Set the commands verification runs for this project.

        Body: {"tests": "...", "typecheck": "..."} — either key may be omitted
        or empty to unset it. Without this a project cannot answer tests_pass
        at all, because verification refuses to guess a command.
        """
        try:
            return container().projects.set_verify(project_id, body).to_dict()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # --- agents -------------------------------------------------------------
    @r.get("/agents")
    async def list_agents():
        c = container()
        health = await c.registry.health_all()
        live = c.sessions.live_rows()
        return {"agents": [{"id": p.id, "name": p.name, **health[p.id].to_dict(),
                            "capabilities": p.capabilities().to_dict(),
                            "active_sessions": sum(1 for s in live if s.agent_id == p.id)}
                           for p in c.registry.all()]}

    @r.get("/agents/{agent_id}/health")
    async def agent_health(agent_id: str):
        try:
            p = container().registry.get(agent_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return (await p.health()).to_dict()

    # --- missions -----------------------------------------------------------
    @r.get("/missions")
    async def list_missions(status: str | None = None):
        return {"missions": [m.to_dict() for m in container().missions.list(status=status)]}

    @r.get("/missions/{mission_id}")
    async def get_mission(mission_id: str):
        try:
            return container().missions.detail(mission_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    async def _transition(mission_id: str, action: str, request: Request):
        c = container()
        try:
            fn = getattr(c.missions, action)
            return (await fn(mission_id, by=_by(request))).to_dict()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc

    @r.post("/missions/{mission_id}/pause")
    async def pause(mission_id: str, request: Request):
        return await _transition(mission_id, "pause", request)

    @r.post("/missions/{mission_id}/resume")
    async def resume(mission_id: str, request: Request):
        return await _transition(mission_id, "resume", request)

    @r.post("/missions/{mission_id}/cancel")
    async def cancel(mission_id: str, request: Request):
        return await _transition(mission_id, "cancel", request)

    @r.delete("/missions/{mission_id}")
    async def delete_mission(mission_id: str, request: Request):
        """Permanently remove a mission. 409 while it still has live sessions —
        cancel it first, which stops its agents."""
        try:
            await container().missions.delete(mission_id, by=_by(request))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except MissionInUse as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"deleted": mission_id}

    # --- sessions -----------------------------------------------------------
    @r.get("/sessions")
    async def list_sessions():
        return {"sessions": container().sessions.list()}

    @r.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        try:
            return container().sessions.get(session_id).to_dict()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @r.post("/sessions/{session_id}/interrupt")
    async def interrupt(session_id: str):
        try:
            return await container().sessions.interrupt(session_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    # --- approvals ----------------------------------------------------------
    @r.get("/approvals")
    async def list_approvals(status: str | None = None):
        return {"approvals": [a.to_dict() for a in container().approvals.list(status=status)]}

    async def _decide(approval_id: str, decision: str, request: Request):
        # Forwarding lives in SessionService.answer_approval, which also
        # advances the session row and the mission — this route only maps the
        # errors (spec §5.8: routes validate and call a service).
        try:
            return container().sessions.answer_approval(approval_id, decision, by=_by(request))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @r.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str, request: Request):
        return await _decide(approval_id, "allowed", request)

    @r.post("/approvals/{approval_id}/deny")
    async def deny(approval_id: str, request: Request):
        return await _decide(approval_id, "denied", request)

    # --- events -------------------------------------------------------------
    @r.get("/events")
    async def list_events(mission_id: str | None = None, session_id: str | None = None,
                          since: str | None = None, limit: int = 200):
        evs = container().store.events.list(mission_id=mission_id, session_id=session_id, since=since,
                                            limit=_clamp_limit(limit))
        return {"events": [e.to_dict() for e in evs]}

    @r.get("/events/stream")
    async def stream_events(mission_id: str | None = None, limit: int = 200):
        c = container()
        limit = _clamp_limit(limit)

        def _frame(e) -> str:
            # narration_mode() is called PER EVENT (not once, before the loop) so
            # a mode change made mid-stream (PUT /yuri/narration) takes effect on
            # the very next frame, without restarting the connection.
            mode = narration_mode()
            payload = {**e.to_dict(), "narration": c.narration.line_for(e, mode)}
            return f"data: {json.dumps(payload, default=str)}\n\n"

        async def gen():
            q = c.bus.subscribe()
            try:
                for e in c.store.events.list(mission_id=mission_id, limit=limit):
                    yield _frame(e)
                while True:
                    try:
                        e = await asyncio.wait_for(q.get(), timeout=15.0)
                        if mission_id and e.mission_id != mission_id:
                            continue
                        yield _frame(e)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                c.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                          "X-Accel-Buffering": "no"})

    return r
