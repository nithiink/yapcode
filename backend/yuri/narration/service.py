"""What Yuri actually says.

Pure: no store, no provider, no I/O. Every line is built from event payload
fields rather than free text, which is what makes the honesty rules structural
instead of aspirational — a turn-completion line can only report that the agent
finished and quote what it said, because that is all it is given.

Wording is deliberately server-side (spec section 4): it is testable here, it is
identical for any future non-browser surface, and the two load-bearing
instructions the old frontend injections carried ("this is the latest result, do
not say it is still in progress", "read the options and get their choice") are
prompt engineering that belongs with the text, not with the transport.
"""
from __future__ import annotations

from yuri.domain.event import YuriEvent
from .policy import Mode, owner_of, speaks

ASSISTANT_TEXT_CAP = 900
REQUEST_CAP = 90


def _clip(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _for_request(result: dict) -> str:
    """Name the originating request so the voice model cannot confuse this
    update with a previous prompt's — the backend threads it through as
    `request`, and the old frontend injection relied on the same trick."""
    req = _clip(str(result.get("request") or ""), REQUEST_CAP)
    return f' for your request "{req}"' if req else ""


def _numbered(options: list) -> str:
    """Number the options and separate them with semicolons: option strings can
    themselves contain commas and arrows, so a comma-join is ambiguous aloud."""
    opts = [str(o) for o in (options or [])]
    return "; ".join(f"({i + 1}) {o}" for i, o in enumerate(opts))


class NarrationService:
    """Turns an event or a poll result into one spoken line, or None."""

    def line_for(self, event: YuriEvent, mode: Mode) -> str | None:
        """Narrate a stream-owned event. Poll-owned types return None here so
        the user never hears the same thing from both carriers."""
        own = owner_of(event.type)
        if own not in ("stream", "stream_verbose"):
            return None
        if not speaks(event.type, event.severity, mode):
            return None
        p = event.payload or {}
        t = event.type

        if t == "mission.created":
            title = p.get("title") or "a new mission"
            project = p.get("project")
            where = f" in {project}" if project else ""
            return f'Starting "{title}"{where}.'

        if t == "mission.status_changed":
            title = p.get("title") or "that mission"
            to = p.get("to")
            reason = _clip(str(p.get("reason") or ""), 160)
            if to == "completed":
                return f'"{title}" is done.'
            if to == "failed":
                return f'"{title}" failed' + (f": {reason}." if reason else ".")
            if to == "paused":
                return f'"{title}" is paused.'
            if to == "cancelled":
                return f'"{title}" is cancelled.'
            # waiting_for_approval: the approval request itself speaks, and
            # saying both would announce the same thing twice.
            return None

        if t == "session.lost":
            name = p.get("session_name") or "a session"
            return (f'I lost contact with "{name}" — its agent did not survive '
                    "the restart.")

        if t == "tool.started":
            agent = p.get("agent_name") or "The agent"
            tool = p.get("tool_name") or "a tool"
            return f"{agent} is using {tool}."

        if t == "cost.updated":
            cost = p.get("cost_usd")
            if not isinstance(cost, (int, float)):
                return None
            name = p.get("session_name")
            who = f'"{name}"' if name else "This session"
            return f"{who} is at ${cost:.2f}."

        return None

    def line_for_poll(self, result: dict, session_name: str | None,
                      agent_name: str, mode: Mode) -> str | None:
        """Narrate a poll result. `result` is SessionService.poll()'s dict."""
        status = (result or {}).get("status")
        who = agent_name or "The agent"

        if status == "needs_permission":
            prompt = result.get("prompt") or {}
            text = _clip(str(prompt.get("text") or ""), 300)
            if not text:
                return None
            if not speaks("approval.requested", "notice", mode):
                return None
            lead = ("That's a destructive action — " if result.get("risk") == "dangerous"
                    else "")
            return (f"{lead}{who} needs permission{_for_request(result)} to {text}. "
                    "Ask the user to approve or deny.")

        if status == "needs_choice":
            prompt = result.get("prompt") or {}
            text = _clip(str(prompt.get("text") or ""), 300)
            if not text:
                return None
            if not speaks("session.question", "notice", mode):
                return None
            opts = _numbered(prompt.get("options") or [])
            tail = f" The options are: {opts}." if opts else ""
            return (f"{who} is asking{_for_request(result)}: {text}{tail} "
                    "Read the options to the user and get their choice.")

        if status == "completed":
            if not speaks("session.turn_completed", "info", mode):
                return None
            said = _clip(str(result.get("assistant_text") or ""), ASSISTANT_TEXT_CAP)
            # The honesty rule: report that the turn finished and quote the
            # agent. Never assert the underlying work succeeded.
            body = f" It said: {said}" if said else " It did not say anything."
            return (f"{who} finished{_for_request(result)}.{body} That is the latest "
                    "result — summarize it briefly for the user, and do not say "
                    "this request is still in progress.")

        if status == "error":
            if not speaks("agent.error", "error", mode):
                return None
            msg = _clip(str(result.get("error") or "unknown"), 300)
            return f"{who} hit an error{_for_request(result)}: {msg}. Tell the user."

        return None
