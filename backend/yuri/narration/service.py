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

from yuri.domain.event import DEFAULTS, EventType, YuriEvent
from .policy import (HANDOFF, Mode, mission_created_is_news,
                     mission_status_change_is_news, origin, owner_of, speaks)

ASSISTANT_TEXT_CAP = 900
REQUEST_CAP = 90
# Names, not prose: a mission title or project/session name is a short label,
# so each cap sits well under REASON_CAP/prose caps even though none of these
# fields is bounded upstream (mission titles come straight from the session
# name via SessionService._pick_name, which only whitespace-normalizes).
TITLE_CAP = 80
PROJECT_CAP = 60
SESSION_NAME_CAP = 60
REASON_CAP = 160
# How many steps of a plan (or blocking tasks) get read aloud. A ten-step
# workflow read back in full is not a sentence the user can hold.
WORKFLOW_STEPS_CAP = 4

# How each failed check is named aloud. The check id ("tests_pass") is not a
# sentence, and "verification failed" with no subject sends the user looking
# through all five — so the phrase says which one, and the detail that follows
# says what it saw.
VERIFICATION_PHRASES: dict[str, str] = {
    "tests_pass": "The tests failed",
    "typecheck_pass": "The typecheck failed",
    "diff_scoped": "The change touched files outside what that step was meant to",
    "review_approved": "The review didn't approve it",
    "human_ok": "That step didn't get your approval",
}


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
            # One owner per FACT: a mission the user asked for by voice is
            # already reported by start_session's own spoken result (policy.py).
            if not mission_created_is_news(p.get("created_by")):
                return None
            title = _clip(str(p.get("title") or ""), TITLE_CAP) or "a new mission"
            project = _clip(str(p.get("project") or ""), PROJECT_CAP)
            where = f" in {project}" if project else ""
            if origin(p.get("created_by")) == HANDOFF:
                # adopt(): the tmux session was already running before Yuri saw
                # it. "Starting" would assert something that did not happen —
                # the honesty class spec §5.2 forbids.
                return f'Picking up "{title}"{where}.'
            return f'Starting "{title}"{where}.'

        if t == "mission.status_changed":
            # One owner per FACT: "voice" was reported by the tool result, and a
            # `derived` system change restates a session event poll already
            # spoke. An UNMARKED system change is original — start()'s
            # provider-failure path has no session row, so nothing else can
            # report it (policy.py).
            if not mission_status_change_is_news(p.get("by"), p.get("derived")):
                return None
            title = _clip(str(p.get("title") or ""), TITLE_CAP) or "that mission"
            to = p.get("to")
            reason = _clip(str(p.get("reason") or ""), REASON_CAP)
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
            name = _clip(str(p.get("session_name") or ""), SESSION_NAME_CAP) or "a session"
            return (f'I lost contact with "{name}" — its agent did not survive '
                    "the restart.")

        if t == "tool.started":
            agent = p.get("agent_name") or "The agent"
            tool = p.get("tool_name") or "a tool"
            return f"{agent} is using {tool}."

        # --- Phase 7: the workflow engine ---------------------------------
        # Every one of these is a fact only the engine knows. `line_for_poll`
        # cannot carry them (it reads a poll RESULT, never an event), so if
        # this method has no branch the fact reaches nobody at all.

        if t == "workflow.created":
            # Read the plan back. This is the mitigation for spoken authoring:
            # a misheard plan that runs unseen is the one real risk of letting
            # the user dictate a workflow.
            tasks = p.get("tasks") or []
            if not tasks:
                return None
            steps = [str(x.get("role") or x.get("title") or "").strip()
                     for x in tasks if isinstance(x, dict)]
            steps = [x for x in steps if x][:WORKFLOW_STEPS_CAP]
            if not steps:
                return None
            more = len(tasks) - len(steps)
            plan = ", then ".join(steps) + (f", and {more} more" if more > 0 else "")
            return f"Here's the plan: {plan}."

        if t == "task.dispatched":
            who = _clip(str(p.get("specialist") or ""), SESSION_NAME_CAP) or "An agent"
            title = _clip(str(p.get("title") or ""), TITLE_CAP)
            attempt = p.get("attempt")
            again = " again" if isinstance(attempt, int) and attempt > 1 else ""
            return f"{who} is starting {title}{again}." if title else f"{who} is starting{again}."

        if t == "task.completed":
            # Texture only (stream_verbose): the user hears this per step in a
            # multi-step workflow, so it stays one short clause.
            title = _clip(str(p.get("title") or ""), TITLE_CAP) or "that step"
            return f"{title} is done."

        if t == "handoff.passed":
            # Names both ends, because the fact worth hearing is that the
            # findings MOVED — "passing what the researcher found to Claude".
            to_who = _clip(str(p.get("to_specialist") or ""), SESSION_NAME_CAP)
            title = _clip(str(p.get("from_title") or ""), TITLE_CAP)
            n = p.get("findings")
            if not to_who and not title:
                return None
            what = f"what {title} found" if title else "the findings so far"
            if isinstance(n, int) and n > 1:
                what += f" ({n} notes)"
            return f"Passing {what} to {to_who}." if to_who else f"Passing on {what}."

        if t == "verification.failed":
            # WHICH check, and WHY. The detail is the tail of the real output
            # ("2 failed in test_billing.py"), which is the half of the
            # sentence that stops the user from going to look for themselves.
            failed = [x for x in (p.get("failed") or []) if isinstance(x, dict)]
            first = failed[0] if failed else {}
            phrase = VERIFICATION_PHRASES.get(str(first.get("check") or ""))
            if phrase is None:
                title = _clip(str(p.get("title") or ""), TITLE_CAP)
                phrase = f"A check on {title} failed" if title else "A check failed"
            detail = _clip(str(first.get("detail") or p.get("reason") or ""), REASON_CAP)
            rest = len(failed) - 1
            also = f" ({rest} other check also failed)" if rest == 1 else (
                f" ({rest} other checks also failed)" if rest > 1 else "")
            because = f" — {detail}" if detail else ""
            # will_retry changes what the user is expected to DO, so it is the
            # last thing said, exactly as in task.failed below.
            after = " Trying again." if p.get("will_retry") else ""
            return f"{phrase}{because}{also}.{after}"

        if t == "task.failed":
            # One owner per FACT: on the verification path the engine marks
            # this `derived`, because verification.failed just said the same
            # reason with the failing check named — a strictly better sentence.
            if p.get("derived"):
                return None
            title = _clip(str(p.get("title") or ""), TITLE_CAP) or "a step"
            reason = _clip(str(p.get("reason") or ""), REASON_CAP)
            because = f" — {reason}" if reason else ""
            # will_retry decides the whole meaning of the sentence: "I'll try
            # again" needs nothing from the user, and saying it the same way as
            # a give-up would make them get involved when they need not.
            if p.get("will_retry"):
                return f"{title} failed{because}. I'll try once more."
            return f"{title} failed{because}."

        if t == "task.blocked":
            title = _clip(str(p.get("title") or ""), TITLE_CAP) or "a step"
            reason = _clip(str(p.get("reason") or ""), REASON_CAP)
            because = f" — {reason}" if reason else ""
            attempts = p.get("attempts")
            n = f" after {attempts} attempts" if isinstance(attempts, int) and attempts > 1 else ""
            return f"I've stopped on {title}{n}{because}. It needs you."

        if t == "workflow.deadlocked":
            blocking = [str(x) for x in (p.get("blocking") or []) if str(x).strip()]
            names = ", ".join(_clip(b, TITLE_CAP) for b in blocking[:WORKFLOW_STEPS_CAP])
            count = p.get("blocking_count")
            more = ""
            if isinstance(count, int) and count > len(blocking[:WORKFLOW_STEPS_CAP]):
                more = f" and {count - len(blocking[:WORKFLOW_STEPS_CAP])} more"
            n = len(blocking[:WORKFLOW_STEPS_CAP])
            verb = "are" if (n + (1 if more else 0)) > 1 else "is"
            what = f" — {names}{more} {verb} blocked" if names else ""
            return f"The mission can't move forward{what}. It needs you."

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
            if not speaks(EventType.APPROVAL_REQUESTED,
                          DEFAULTS[EventType.APPROVAL_REQUESTED][0], mode):
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
            if not speaks(EventType.SESSION_QUESTION,
                          DEFAULTS[EventType.SESSION_QUESTION][0], mode):
                return None
            opts = _numbered(prompt.get("options") or [])
            tail = f" The options are: {opts}." if opts else ""
            return (f"{who} is asking{_for_request(result)}: {text}{tail} "
                    "Read the options to the user and get their choice.")

        if status == "completed":
            if not speaks(EventType.SESSION_TURN_COMPLETED,
                          DEFAULTS[EventType.SESSION_TURN_COMPLETED][0], mode):
                return None
            said = _clip(str(result.get("assistant_text") or ""), ASSISTANT_TEXT_CAP)
            # The honesty rule: report that the turn finished and quote the
            # agent. Never assert the underlying work succeeded.
            body = f" It said: {said}" if said else " It did not say anything."
            return (f"{who} finished{_for_request(result)}.{body} That is the latest "
                    "result — summarize it briefly for the user, and do not say "
                    "this request is still in progress.")

        if status == "error":
            if not speaks(EventType.AGENT_ERROR,
                          DEFAULTS[EventType.AGENT_ERROR][0], mode):
                return None
            msg = _clip(str(result.get("error") or "unknown"), 300)
            return f"{who} hit an error{_for_request(result)}: {msg}. Tell the user."

        return None
