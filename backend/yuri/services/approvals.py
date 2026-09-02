"""Approvals (spec §5.4). The provider decides what it wants to do; Yuri owns
the record and the decision. Fails closed: an ambiguous spoken answer is an
error the caller must re-ask, never an allow."""
from __future__ import annotations

import hashlib
import json
import logging

from claude_runner import decide_permission
from yuri.domain.approval import Approval
from yuri.domain.event import EventType, YuriEvent
from yuri.domain.risk import risk_for
from yuri.domain.session import AgentSession
from yuri.events.bus import EventBus
from yuri.services.journal import Journal
from yuri.store.base import PendingApprovalExists, Store

log = logging.getLogger("yuri.services.approvals")


class ApprovalService:
    def __init__(self, store: Store, bus: EventBus, journal: Journal):
        self.store = store
        self.bus = bus
        self.journal = journal

    def get(self, approval_id: str) -> Approval:
        a = self.store.approvals.get(approval_id)
        if a is None:
            raise KeyError(f"unknown approval: {approval_id}")
        return a

    def pending(self) -> list[Approval]:
        return self.store.approvals.list(status="pending")

    def list(self, status: str | None = None) -> list[Approval]:
        return self.store.approvals.list(status=status)

    def record_request(self, session: AgentSession, prompt: dict) -> Approval:
        tool_name = str(prompt.get("tool_name") or "")
        tool_input = prompt.get("tool_input") or {}
        # request_id dedup key: the caller's id when it supplied one, otherwise a
        # key synthesized from session/tool/tool_input. Content-addressed (NOT
        # time-addressed): the real path always carries a request_id
        # (claude_runner.Prompt.request_id defaults to uuid4, forwarded by the
        # provider), so hitting this fallback means something upstream misbehaved
        # — log it so that's visible. The hash must depend only on things that
        # are identical for a repeated identical prompt (session, tool, tool_input)
        # and different for any other prompt, so that:
        #   - the same prompt replayed twice dedups to one Approval (get_by_request
        #     below must run against this key regardless of source, or the second
        #     insert crashes on the request_id UNIQUE constraint instead of
        #     returning the existing row), and
        #   - a *different* prompt on the same session (e.g. a different tool_input)
        #     never collides with — and so never inherits the decision of — an
        #     earlier, unrelated approval. A timestamp-based key (e.g.
        #     last_activity_at, which nothing between calls necessarily advances)
        #     would fail the second guarantee: two distinct commands could hash to
        #     the same id and the second would silently reuse the first's already
        #     -resolved status.
        request_id = str(prompt.get("request_id") or "")
        if not request_id:
            canonical_input = json.dumps(tool_input, sort_keys=True, default=str)
            digest = hashlib.sha256(f"{session.id}:{tool_name}:{canonical_input}".encode()).hexdigest()
            request_id = f"synth:{digest[:32]}"
            log.warning(
                "record_request: prompt for session %s (tool=%s) had no request_id; "
                "synthesizing %s from session+tool+tool_input. This should not happen on the "
                "real path (claude_runner.Prompt.request_id defaults to a uuid4) — check the caller.",
                session.id, tool_name, request_id)
        existing = self.store.approvals.get_by_request(request_id)
        if existing is not None:
            return existing
        a = Approval(session_id=session.id, mission_id=session.mission_id, agent_id=session.agent_id,
                     action=tool_name or "action", tool_name=tool_name, tool_input=tool_input,
                     risk=risk_for(tool_name, tool_input), description=str(prompt.get("text") or ""),
                     request_id=request_id)
        try:
            self.store.approvals.insert(a)
        except PendingApprovalExists:
            stale = self.store.approvals.pending_for_session(session.id)
            if stale is not None:
                stale.resolve("superseded", "system")
                self.store.approvals.update(stale)
            self.store.approvals.insert(a)
        self.bus.publish(YuriEvent.make(
            EventType.APPROVAL_REQUESTED, mission_id=a.mission_id, session_id=a.session_id,
            agent_id=a.agent_id, payload={"approval_id": a.id, "risk": a.risk,
                                          "tool_name": a.tool_name, "description": a.description,
                                          "session_name": session.name,
                                          "native_session_id": session.native_session_id}))
        return a

    def resolve(self, approval_id: str, decision: str, by: str) -> Approval:
        a = self.get(approval_id)
        if a.status != "pending":
            raise ValueError(f"approval {approval_id[:8]} is already {a.status}")
        if decision not in ("allowed", "denied"):
            raise ValueError(f"decision must be allowed|denied, got {decision!r}")
        a.resolve(decision, by)
        self.store.approvals.update(a)
        self.bus.publish(YuriEvent.make(
            EventType.APPROVAL_RESOLVED, mission_id=a.mission_id, session_id=a.session_id,
            agent_id=a.agent_id, payload={"approval_id": a.id, "status": a.status, "by": by,
                                          "description": a.description, "risk": a.risk}))
        self.journal.append(f"approval {a.status} ({a.risk}) by {by}: {a.description}")
        return a

    def resolve_by_session(self, session: AgentSession, choice: str, by: str) -> Approval | None:
        a = self.store.approvals.pending_for_session(session.id)
        if a is None:
            return None
        decision = decide_permission(choice)
        if decision is None:
            raise ValueError(f"I couldn't tell if {choice!r} means allow or deny — please say allow or deny.")
        return self.resolve(a.id, "allowed" if decision == "allow" else "denied", by)
