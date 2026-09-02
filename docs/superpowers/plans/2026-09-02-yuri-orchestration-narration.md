# Yuri Orchestration & Narration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Yuri a voice over her own domain — event-driven narration with quiet/normal/verbose modes, mission-level voice commands, and one home for agent selection.

**Architecture:** A pure `NarrationService` turns a `YuriEvent` (or a poll result) into a spoken line, gated by a mode stored server-side. Narration ownership is split per event type between the poll result and the SSE stream, declared once in `yuri/narration/policy.py`, because all four events the poll loop already narrates are also `speakable` — the naive hybrid speaks everything twice. The backend attaches a `narration` field to both carriers; the frontend's whole rule becomes "if it has a narration line, inject it". No orchestrator class: `SessionService`/`MissionService` already do that work.

**Tech Stack:** Python 3.14 (`backend/.venv`), FastAPI, stdlib `sqlite3`, `unittest`; Next 16 / React 19 / TS, `node --test`.

**Spec:** `docs/superpowers/specs/2026-09-02-yuri-orchestration-narration-design.md` — read it first. Also read `docs/yuri/follow-ups.md`: two entries there shape this design.

## Global Constraints

- **Commit as you go.** Small, focused commits per task (unlike the foundation build, commits are now authorized). End every commit message with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Work on branch `feat/yuri-orchestration` off `main`.
- **No new dependencies**, Python or npm. `requirements.txt`, `requirements.lock`, `package-lock.json` unchanged.
- **`backend/tests/test_tools_dispatch.py` is the regression contract.** The seventeen existing tools' result keys stay byte-identical; the five new mission tools plus `set_narration` are additive. `test_every_definition_has_name_and_object_params` must still pass.
- **Test output must be pristine** — zero warnings. Verify: `cd backend && .venv/bin/python -W always::ResourceWarning -m unittest discover -s tests 2>&1 | grep -c ResourceWarning` → `0`.
- **The suite must be green in BOTH `~/Yuri` states** — present (`mkdir -p ~/Yuri/memory/projects ~/Yuri/journal ~/Yuri/workspace`) and absent (`rm -rf ~/Yuri`). Leave it absent when you finish. Tests always use a temp `Home`; never the real one.
- Baseline: **282 backend tests, 9 frontend tests**, both green and pristine.
- Every new backend file starts with `from __future__ import annotations`, carries a short WHY docstring, uses type hints. Match the style of `backend/yuri/services/*.py`.
- **Layering:** `api → services → domain/store/events → providers`. `yuri/narration/` is pure — no store, no provider, no I/O.
- **Honesty (spec §38/§5.2):** narration is generated from event payload fields, never free text. A turn-completion line says the agent *finished and said X* — never that the work succeeded.
- Backend focused run: `cd backend && .venv/bin/python -m unittest tests.<module> -v`. Full: `.venv/bin/python -m unittest discover -s tests`. Frontend: `cd frontend && npm test` and `npx tsc --noEmit`.

---

## File structure

**Created**

| Path | Responsibility |
|---|---|
| `backend/yuri/narration/__init__.py` | package marker |
| `backend/yuri/narration/policy.py` | `Mode`, `Owner`, `NARRATION_OWNER`, `speaks(event_type, severity, mode)` |
| `backend/yuri/narration/service.py` | `NarrationService.line_for` / `.line_for_poll` |
| `backend/yuri/services/router.py` | `AgentRouter.select` |
| `backend/tests/test_narration_policy.py` | ownership table invariants + mode filter |
| `backend/tests/test_narration_service.py` | phrasing + the three honesty rules |
| `backend/tests/test_agent_router.py` | selection order |
| `backend/tests/test_mission_resolve.py` | `MissionService.resolve` |
| `backend/tests/test_mission_tools.py` | the five mission voice tools |
| `backend/tests/test_narration_api.py` | mode storage, tool, REST, context |
| `frontend/lib/narration.ts` | the inject rule + the mode type, as a pure function |
| `frontend/lib/narration.test.ts` | its tests |

**Modified**

| Path | Change |
|---|---|
| `backend/yuri/services/missions.py` | `resolve(ref)`; `pause` interrupts live sessions first |
| `backend/yuri/services/sessions.py` | take an `AgentRouter`; `poll()` attaches `narration` |
| `backend/yuri/app.py` | build `AgentRouter` + `NarrationService`, add to `Container` |
| `backend/yuri/api/routes.py` | `GET`/`PUT /yuri/narration`; `narration` on SSE frames; `narration_mode` in `/yuri/context` |
| `backend/tools.py` | five mission tools + `set_narration` |
| `frontend/lib/operating.ts` | mission-command and narration-mode bullets |
| `frontend/components/VoiceAgent.tsx` | inject `res.narration`; narration `EventSource`; mode toggle |

---

## Task 1: NarrationService + the ownership policy

**Files:**
- Create: `backend/yuri/narration/__init__.py`, `backend/yuri/narration/policy.py`, `backend/yuri/narration/service.py`
- Test: `backend/tests/test_narration_policy.py`, `backend/tests/test_narration_service.py`

**Interfaces — Produces:**
```python
# yuri.narration.policy
Mode = Literal["quiet", "normal", "verbose"]
MODES: tuple[Mode, ...] = ("quiet", "normal", "verbose")
DEFAULT_MODE: Mode = "normal"
Owner = Literal["poll", "stream", "stream_verbose", "none"]
NARRATION_OWNER: dict[str, Owner]          # one entry per EventType, exhaustive
ALWAYS_SPEAK: frozenset[str]               # blocks-on-the-user types, never suppressed
def normalize_mode(value: object) -> Mode  # unknown/None -> DEFAULT_MODE
def speaks(event_type: str, severity: str, mode: Mode) -> bool
def owner_of(event_type: str) -> Owner
# yuri.narration.service
class NarrationService:
    def line_for(self, event: YuriEvent, mode: Mode) -> str | None
    def line_for_poll(self, result: dict, session_name: str | None, agent_name: str, mode: Mode) -> str | None
```

- [ ] **Step 1: Write `backend/tests/test_narration_policy.py`**

```python
"""The narration ownership table is the structural guard against Yuri saying
everything twice: all four events the poll loop narrates are ALSO speakable, so
each event type must be claimed by exactly one carrier (spec section 3).

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.event import DEFAULTS, EventType  # noqa: E402
from yuri.narration import policy  # noqa: E402


def _all_event_types() -> list[str]:
    return [v for k, v in vars(EventType).items() if k.isupper()]


class OwnershipTable(unittest.TestCase):
    def test_every_event_type_is_owned_exactly_once(self):
        for t in _all_event_types():
            self.assertIn(t, policy.NARRATION_OWNER, f"{t} has no owner")
        self.assertEqual(len(policy.NARRATION_OWNER), len(_all_event_types()),
                         "the table owns a type that is not an EventType")

    def test_no_type_is_owned_by_both_carriers(self):
        # The table is a dict, so double-ownership can only show up as an owner
        # value that means "both" — assert the vocabulary is closed instead.
        for t, owner in policy.NARRATION_OWNER.items():
            self.assertIn(owner, ("poll", "stream", "stream_verbose", "none"), f"{t}: {owner}")

    def test_the_four_poll_owned_types_are_exactly_the_ones_poll_carries(self):
        poll_owned = {t for t, o in policy.NARRATION_OWNER.items() if o == "poll"}
        self.assertEqual(poll_owned, {
            EventType.APPROVAL_REQUESTED, EventType.SESSION_QUESTION,
            EventType.AGENT_ERROR, EventType.SESSION_TURN_COMPLETED})

    def test_mission_events_belong_to_the_stream(self):
        for t in (EventType.MISSION_CREATED, EventType.MISSION_STATUS_CHANGED,
                  EventType.SESSION_LOST):
            self.assertEqual(policy.owner_of(t), "stream")

    def test_debug_texture_is_verbose_only(self):
        for t in (EventType.TOOL_STARTED, EventType.COST_UPDATED):
            self.assertEqual(policy.owner_of(t), "stream_verbose")

    def test_user_caused_events_are_never_narrated(self):
        for t in (EventType.SESSION_CREATED, EventType.SESSION_MESSAGE_SENT,
                  EventType.APPROVAL_RESOLVED, EventType.SESSION_INTERRUPTED,
                  EventType.SESSION_STOPPED, EventType.PROJECT_REGISTERED,
                  EventType.MEMORY_REMEMBERED):
            self.assertEqual(policy.owner_of(t), "none")

    def test_unknown_type_owner_is_none_not_a_crash(self):
        self.assertEqual(policy.owner_of("something.invented"), "none")


class ModeFilter(unittest.TestCase):
    def _speaks(self, t, mode):
        sev = DEFAULTS.get(t, ("info", False))[0]
        return policy.speaks(t, sev, mode)

    def test_quiet_still_asks_the_user(self):
        # A mode that swallowed a permission request would strand the agent
        # waiting on an answer the user was never asked for.
        self.assertTrue(self._speaks(EventType.APPROVAL_REQUESTED, "quiet"))
        self.assertTrue(self._speaks(EventType.SESSION_QUESTION, "quiet"))

    def test_quiet_speaks_warnings_and_errors(self):
        self.assertTrue(self._speaks(EventType.AGENT_ERROR, "quiet"))
        self.assertTrue(self._speaks(EventType.SESSION_LOST, "quiet"))

    def test_quiet_suppresses_ordinary_progress(self):
        self.assertFalse(self._speaks(EventType.SESSION_TURN_COMPLETED, "quiet"))
        self.assertFalse(self._speaks(EventType.MISSION_CREATED, "quiet"))
        self.assertFalse(self._speaks(EventType.MISSION_STATUS_CHANGED, "quiet"))

    def test_normal_speaks_progress_but_not_debug_texture(self):
        self.assertTrue(self._speaks(EventType.SESSION_TURN_COMPLETED, "normal"))
        self.assertTrue(self._speaks(EventType.MISSION_CREATED, "normal"))
        self.assertFalse(self._speaks(EventType.TOOL_STARTED, "normal"))
        self.assertFalse(self._speaks(EventType.COST_UPDATED, "normal"))

    def test_verbose_adds_the_debug_texture(self):
        self.assertTrue(self._speaks(EventType.TOOL_STARTED, "verbose"))
        self.assertTrue(self._speaks(EventType.COST_UPDATED, "verbose"))
        self.assertTrue(self._speaks(EventType.SESSION_TURN_COMPLETED, "verbose"))

    def test_never_narrated_types_stay_silent_in_every_mode(self):
        for mode in policy.MODES:
            self.assertFalse(self._speaks(EventType.SESSION_CREATED, mode), mode)
            self.assertFalse(self._speaks(EventType.APPROVAL_RESOLVED, mode), mode)

    def test_normalize_mode(self):
        self.assertEqual(policy.normalize_mode("quiet"), "quiet")
        self.assertEqual(policy.normalize_mode("VERBOSE"), "verbose")
        self.assertEqual(policy.normalize_mode(" normal "), "normal")
        for bad in (None, "", "loud", 7, {}):
            self.assertEqual(policy.normalize_mode(bad), policy.DEFAULT_MODE, repr(bad))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it** — `cd backend && .venv/bin/python -m unittest tests.test_narration_policy -v` → FAIL with `ModuleNotFoundError: yuri.narration`.

- [ ] **Step 3: Write `backend/yuri/narration/__init__.py`** (empty file) **and `backend/yuri/narration/policy.py`**

```python
"""Which carrier narrates which event, and what each mode speaks.

Yuri has two ways to learn something happened: the per-session poll result the
frontend already drains, and the domain event stream. Both are correct, and all
four events the poll loop narrates are ALSO marked speakable in
domain/event.py's DEFAULTS — so without a declared split she says everything
twice. This module is that declaration.

Poll owns the session-turn events because its result is the only carrier that
sees EVERY sub-question of a multi-question AskUserQuestion (the tmux runner
notifies only from its hook path — see docs/yuri/follow-ups.md). The stream owns
mission-level state, which poll cannot see at all.
"""
from __future__ import annotations

from typing import Literal

from yuri.domain.event import EventType

Mode = Literal["quiet", "normal", "verbose"]
MODES: tuple[Mode, ...] = ("quiet", "normal", "verbose")
DEFAULT_MODE: Mode = "normal"

Owner = Literal["poll", "stream", "stream_verbose", "none"]

# Exactly one owner per EventType. Enforced by test_narration_policy.
NARRATION_OWNER: dict[str, Owner] = {
    # Poll owns these: it carries them reliably, including sub-questions.
    EventType.APPROVAL_REQUESTED: "poll",
    EventType.SESSION_QUESTION: "poll",
    EventType.SESSION_TURN_COMPLETED: "poll",
    EventType.AGENT_ERROR: "poll",
    # Stream owns mission-level state and lost contact — poll cannot see them.
    EventType.MISSION_CREATED: "stream",
    EventType.MISSION_STATUS_CHANGED: "stream",
    EventType.SESSION_LOST: "stream",
    # Texture: only when the user asked to hear everything.
    EventType.TOOL_STARTED: "stream_verbose",
    EventType.COST_UPDATED: "stream_verbose",
    # Never narrated: the user caused these, so saying them is telling them
    # what they just did. session.created also fires on a rehydration REVIVAL
    # (payload.revived) and must never be announced as new work starting.
    EventType.SESSION_CREATED: "none",
    EventType.SESSION_MESSAGE_SENT: "none",
    EventType.APPROVAL_RESOLVED: "none",
    EventType.SESSION_INTERRUPTED: "none",
    EventType.SESSION_STOPPED: "none",
    EventType.PROJECT_REGISTERED: "none",
    EventType.MEMORY_REMEMBERED: "none",
}

# Blocks on the user: never suppressed, whatever the mode. "Be quiet" means
# stop chattering, not stop asking — a suppressed permission request would
# strand the agent waiting on an answer the user was never asked for.
ALWAYS_SPEAK: frozenset[str] = frozenset({
    EventType.APPROVAL_REQUESTED, EventType.SESSION_QUESTION})

_LOUD_SEVERITIES = frozenset({"warning", "error"})


def normalize_mode(value: object) -> Mode:
    """Coerce anything to a valid mode; unknown input falls back to the default."""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in MODES:
            return v  # type: ignore[return-value]
    return DEFAULT_MODE


def owner_of(event_type: str) -> Owner:
    """Which carrier narrates this type. An unrecognized type is never narrated
    rather than being a crash — a new EventType is caught by the policy test,
    not by a 500 at runtime."""
    return NARRATION_OWNER.get(event_type, "none")


def speaks(event_type: str, severity: str, mode: Mode) -> bool:
    """Whether this event is spoken in this mode."""
    own = owner_of(event_type)
    if own == "none":
        return False
    if event_type in ALWAYS_SPEAK:
        return True
    if own == "stream_verbose":
        return mode == "verbose"
    if mode == "quiet":
        return severity in _LOUD_SEVERITIES
    return True
```

- [ ] **Step 4: Run** `tests.test_narration_policy -v` → PASS.

- [ ] **Step 5: Write `backend/tests/test_narration_service.py`**

```python
"""Yuri's spoken lines. Generated from event payload fields, never free text, so
the honesty rules (spec section 5.2) are structural: a turn-completion line says
the agent finished and quotes what it SAID — never that the work succeeded.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.narration.service import NarrationService  # noqa: E402


class Lines(unittest.TestCase):
    def setUp(self):
        self.n = NarrationService()

    def _line(self, type, payload, mode="normal"):
        return self.n.line_for(YuriEvent.make(type, payload=payload), mode)

    def test_mission_created(self):
        line = self._line(EventType.MISSION_CREATED,
                          {"title": "Fix billing", "project": "pm-tool"})
        self.assertIn("Fix billing", line)
        self.assertIn("pm-tool", line)

    def test_mission_completed_and_failed(self):
        done = self._line(EventType.MISSION_STATUS_CHANGED,
                          {"title": "Fix billing", "from": "running", "to": "completed"})
        self.assertIn("Fix billing", done)
        failed = self._line(EventType.MISSION_STATUS_CHANGED,
                            {"title": "Fix billing", "from": "running", "to": "failed",
                             "reason": "tests did not pass"})
        self.assertIn("failed", failed)
        self.assertIn("tests did not pass", failed)

    def test_waiting_for_approval_is_silent_the_approval_speaks(self):
        self.assertIsNone(self._line(EventType.MISSION_STATUS_CHANGED,
                                     {"title": "t", "from": "running",
                                      "to": "waiting_for_approval"}))

    def test_session_lost_is_honest_about_what_happened(self):
        line = self._line(EventType.SESSION_LOST, {"session_name": "billing"})
        self.assertIn("billing", line)
        self.assertRegex(line.lower(), r"lost|didn't survive|did not survive")

    def test_verbose_texture(self):
        tool = self._line(EventType.TOOL_STARTED,
                          {"tool_name": "Read", "agent_name": "Claude Code"}, mode="verbose")
        self.assertIn("Read", tool)
        self.assertIsNone(self._line(EventType.TOOL_STARTED,
                                     {"tool_name": "Read"}, mode="normal"))
        cost = self._line(EventType.COST_UPDATED,
                          {"cost_usd": 0.1234, "session_name": "billing"}, mode="verbose")
        self.assertIn("0.12", cost)

    def test_never_narrated_types_return_none(self):
        self.assertIsNone(self._line(EventType.SESSION_CREATED, {"name": "x"}))
        self.assertIsNone(self._line(EventType.APPROVAL_RESOLVED, {"status": "allowed"}))

    def test_poll_owned_types_are_not_narrated_from_the_stream(self):
        # Otherwise the user hears the turn twice: once from poll, once here.
        for t in (EventType.SESSION_TURN_COMPLETED, EventType.APPROVAL_REQUESTED,
                  EventType.SESSION_QUESTION, EventType.AGENT_ERROR):
            self.assertIsNone(self._line(t, {"assistant_text": "x", "description": "y",
                                             "text": "z", "message": "m"}), t)


class PollLines(unittest.TestCase):
    def setUp(self):
        self.n = NarrationService()

    def _poll(self, result, mode="normal", name="billing", agent="Claude Code"):
        return self.n.line_for_poll(result, name, agent, mode)

    def test_permission_asks_and_names_the_action(self):
        line = self._poll({"status": "needs_permission",
                           "prompt": {"kind": "permission", "text": "run rm -rf build",
                                      "tool_name": "Bash", "options": ["allow", "deny"]}})
        self.assertIn("rm -rf build", line)
        self.assertRegex(line.lower(), r"approve|deny|permission")

    def test_dangerous_risk_is_surfaced_before_asking(self):
        line = self._poll({"status": "needs_permission", "risk": "dangerous",
                           "prompt": {"kind": "permission", "text": "run rm -rf /",
                                      "tool_name": "Bash", "options": ["allow", "deny"]}})
        self.assertRegex(line.lower(), r"destructive|dangerous")

    def test_question_reads_numbered_options(self):
        line = self._poll({"status": "needs_choice",
                           "prompt": {"kind": "choice", "text": "Which one?",
                                      "options": ["Train-Us", "Train"]}})
        self.assertIn("Which one?", line)
        self.assertIn("(1)", line)
        self.assertIn("(2)", line)

    def test_completed_quotes_the_agent_and_never_claims_success(self):
        line = self._poll({"status": "completed",
                           "assistant_text": "I changed two files and ran the tests."})
        self.assertIn("changed two files", line)
        # The honesty rule: the line reports that the agent finished and what it
        # said. It must not assert the work itself succeeded.
        for forbidden in ("it's fixed", "it is fixed", "the work is done",
                          "successfully completed", "everything works"):
            self.assertNotIn(forbidden, line.lower(), forbidden)

    def test_completed_attributes_the_request(self):
        line = self._poll({"status": "completed", "assistant_text": "done",
                           "request": "list the files in backend"})
        self.assertIn("list the files in backend", line)

    def test_error_is_reported_plainly(self):
        line = self._poll({"status": "error", "error": "claude exited 1"})
        self.assertIn("claude exited 1", line)

    def test_quiet_suppresses_completion_but_never_a_prompt(self):
        self.assertIsNone(self._poll({"status": "completed", "assistant_text": "x"},
                                     mode="quiet"))
        self.assertIsNotNone(self._poll({"status": "needs_permission",
                                         "prompt": {"kind": "permission", "text": "run ls",
                                                    "tool_name": "Bash", "options": []}},
                                        mode="quiet"))
        self.assertIsNotNone(self._poll({"status": "error", "error": "boom"}, mode="quiet"))

    def test_working_and_idle_are_not_narrated(self):
        self.assertIsNone(self._poll({"status": "working"}))
        self.assertIsNone(self._poll({"status": "idle"}))

    def test_a_prompt_status_without_a_prompt_payload_is_not_narrated(self):
        # Defensive: poll can report needs_permission with no prompt attached.
        self.assertIsNone(self._poll({"status": "needs_permission"}))

    def test_long_assistant_text_is_capped(self):
        line = self._poll({"status": "completed", "assistant_text": "x" * 5000})
        self.assertLess(len(line), 1200)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6: Run it** → FAIL (`ModuleNotFoundError: yuri.narration.service`).

- [ ] **Step 7: Write `backend/yuri/narration/service.py`**

```python
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
```

- [ ] **Step 8: Run** `tests.test_narration_service -v` until green, then the full suite.

Run: `cd backend && .venv/bin/python -m unittest discover -s tests`
Expected: `OK`, 282 + your new tests.

- [ ] **Step 9: Commit**

```bash
git add backend/yuri/narration backend/tests/test_narration_policy.py backend/tests/test_narration_service.py
git commit -m "$(cat <<'EOF'
feat(narration): add the narration policy and service

Yuri records everything and says almost nothing: her voice reacts to three
per-session statuses phrased by three hardcoded strings in the frontend.
This is the backend half of giving her a voice over her own domain.

The policy table exists because all four events the poll loop narrates are
also marked speakable — so subscribing to the event stream without a
declared split makes her say everything twice. Ownership is per event type,
declared once, and two tests enforce that every type is claimed exactly
once. Quiet mode never suppresses a permission request: a mode that could
would strand the agent waiting on an answer the user was never asked for.

The service is pure and builds every line from payload fields, which is
what makes the honesty rules structural — a turn-completion line can only
report that the agent finished and quote what it said.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: AgentRouter

**Files:**
- Create: `backend/yuri/services/router.py`
- Modify: `backend/yuri/services/sessions.py` (take the router), `backend/yuri/app.py` (build it, add to `Container`)
- Test: `backend/tests/test_agent_router.py`

**Interfaces — Produces:**
```python
class AgentRouter:
    def __init__(self, registry: AgentRegistry, default_agent: str = "claude-code")
    def select(self, project: Project, requested: str | None = None) -> AgentProvider
Container.router: AgentRouter          # new field, after `registry`
SessionService(..., router: AgentRouter)   # keyword, defaults to building one from registry+default_agent
```

- [ ] **Step 1: Write the failing test**

```python
"""Agent selection has one home. Order: explicit request, then the project's
default, then the container's default (plan section 18). The routing rules that
section lists as future — task type, cost, latency, capability, workload — are
deliberately absent: a router with one agent to route to would be speculation.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.project import Project  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.router import AgentRouter  # noqa: E402


class Router(unittest.TestCase):
    def setUp(self):
        self.a = FakeAgentProvider()                 # id "fake"
        self.b = FakeAgentProvider()
        self.b.id = "other"
        self.reg = AgentRegistry()
        self.reg.register(self.a)
        self.reg.register(self.b)
        self.router = AgentRouter(self.reg, default_agent="fake")

    def _project(self, default_agent=None):
        return Project(slug="p", name="P", root_path="/tmp/p", default_agent=default_agent)

    def test_explicit_request_wins(self):
        self.assertIs(self.router.select(self._project("fake"), requested="other"), self.b)

    def test_project_default_when_nothing_requested(self):
        self.assertIs(self.router.select(self._project("other")), self.b)

    def test_container_default_when_project_has_none(self):
        self.assertIs(self.router.select(self._project()), self.a)

    def test_unknown_requested_agent_raises_naming_what_exists(self):
        with self.assertRaises(KeyError) as cm:
            self.router.select(self._project(), requested="opencode")
        msg = str(cm.exception)
        self.assertIn("opencode", msg)
        self.assertIn("fake", msg)

    def test_unknown_project_default_falls_back_rather_than_failing(self):
        # A project configured for an agent that is no longer registered must
        # not make its sessions unstartable — the user did not ask for it now.
        self.assertIs(self.router.select(self._project("retired-agent")), self.a)

    def test_empty_string_request_is_treated_as_no_request(self):
        self.assertIs(self.router.select(self._project("other"), requested=""), self.b)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: yuri.services.router`).

- [ ] **Step 3: Write `backend/yuri/services/router.py`**

```python
"""Which agent runs a mission's work.

Extracted from SessionService.start, where the choice was inlined. It exists so
the routing rules plan section 18 lists as future — task type, model
availability, cost, latency, capability matching, current workload — have one
home when there is a second agent to route to. Adding them now would be a
router with nothing to choose between.
"""
from __future__ import annotations

import logging

from yuri.domain.project import Project
from yuri.providers.base import AgentProvider
from yuri.providers.registry import AgentRegistry

log = logging.getLogger("yuri.router")


class AgentRouter:
    def __init__(self, registry: AgentRegistry, default_agent: str = "claude-code"):
        self.registry = registry
        self.default_agent = default_agent

    def select(self, project: Project, requested: str | None = None) -> AgentProvider:
        """Explicit request, then the project's default, then the global default.

        An unknown *requested* id raises KeyError naming what exists — tools.py
        turns that into a soft error the voice model recovers from. An unknown
        *project* default only warns and falls back: the user did not ask for it
        now, and a retired agent id in a stored row must not make that project's
        sessions unstartable.
        """
        wanted = (requested or "").strip()
        if wanted:
            return self.registry.get(wanted)     # raises KeyError naming known ids
        if project.default_agent:
            try:
                return self.registry.get(project.default_agent)
            except KeyError:
                log.warning("project %s prefers agent %r, which is not registered; "
                            "falling back to %s", project.slug, project.default_agent,
                            self.default_agent)
        return self.registry.get(self.default_agent)
```

- [ ] **Step 4: Wire it in.** In `backend/yuri/services/sessions.py`:
  - add `router: AgentRouter | None = None` as a keyword arg on `__init__`, storing `self.router = router or AgentRouter(registry, default_agent)`;
  - in `start()`, replace `agent = self.registry.get(agent_id or project.default_agent or self.default_agent)` with `agent = self.router.select(project, agent_id)`;
  - leave `adopt()`'s `self.registry.get(self.default_agent)` alone — adoption is Claude-specific (it resumes a `claude --resume` session), so routing does not apply.

  In `backend/yuri/app.py`: add `router: AgentRouter` to `Container` (after `registry`), build it in `build_container` before `SessionService`, pass it in, and include it in the `Container(...)` construction.

- [ ] **Step 5: Run the full suite.** The existing `test_session_service` tests exercise `start()` and must pass unchanged — the selection order is identical, just relocated.

Run: `cd backend && .venv/bin/python -m unittest discover -s tests`
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add backend/yuri/services/router.py backend/yuri/services/sessions.py backend/yuri/app.py backend/tests/test_agent_router.py
git commit -m "$(cat <<'EOF'
feat(router): give agent selection one home

The choice was inlined in SessionService.start. AgentRouter keeps the same
order — explicit request, project default, global default — and exists so
the rules plan section 18 defers (task type, cost, latency, capability,
workload) have somewhere to land once there is a second agent.

One behavior change: a project whose stored default_agent is no longer
registered now warns and falls back instead of raising, so a retired agent
id cannot make that project's sessions unstartable. An explicitly requested
unknown agent still raises, naming what exists.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Mission resolution + the five mission voice tools

**Files:**
- Modify: `backend/yuri/services/missions.py` (`resolve`, `pause` interrupts first, `speech_detail`), `backend/tools.py` (five tools)
- Test: `backend/tests/test_mission_resolve.py`, `backend/tests/test_mission_tools.py`

**Interfaces — Produces:**
```python
MissionService.ACTIVE: tuple[str, ...] = ("running", "waiting_for_approval", "paused", "queued")
MissionService.resolve(ref: str) -> Mission        # ValueError on ambiguity or no match
MissionService.active() -> list[Mission]
MissionService.speech_detail(mission_id: str) -> dict
    # {mission_id, title, goal, status, project, agents, sessions:[{name,status}],
    #  pending_approval: str|None, last_event: str|None}
# tools: list_missions, mission_status, pause_mission, resume_mission, cancel_mission
```

- [ ] **Step 1: Write `backend/tests/test_mission_resolve.py`**

```python
"""Mission references arrive as speech. Resolution refuses to guess: a wrong
session pick sends the user's instruction to the wrong agent, and a wrong
mission pick cancels the wrong work.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.missions import MissionService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402


class MissionResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(config, "YURI_HOME",
                                          os.path.join(self.tmp.name, "Yuri"))]
        [p.start() for p in self.patches]
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.svc = MissionService(self.store, EventBus(), Journal(self.home))
        self.project = Project(slug="p", name="P", root_path="/tmp/p")
        self.store.projects.insert(self.project)

    def tearDown(self):
        self.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    def _m(self, title, status=None):
        m = self.svc.create(self.project, title, created_by="voice")
        if status and status != m.status:
            self.svc.set_status(m, status, by="test")
        return m

    def test_full_id_and_unique_prefix(self):
        m = self._m("Fix billing")
        self.assertEqual(self.svc.resolve(m.id).id, m.id)
        self.assertEqual(self.svc.resolve(m.id[:8]).id, m.id)

    def test_exact_title_case_insensitive(self):
        m = self._m("Fix billing")
        self.assertEqual(self.svc.resolve("fix BILLING").id, m.id)

    def test_word_overlap_against_active_titles(self):
        m = self._m("Fix the Cashfree payment flow")
        self.assertEqual(self.svc.resolve("cashfree").id, m.id)
        self.assertEqual(self.svc.resolve("the payment one").id, m.id)

    def test_deictic_reference_picks_the_sole_active_mission(self):
        m = self._m("Fix billing")
        for ref in ("", "it", "that", "this one", "the current one", "the mission"):
            self.assertEqual(self.svc.resolve(ref).id, m.id, ref)

    def test_deictic_reference_with_two_active_missions_refuses(self):
        self._m("Fix billing")
        self._m("Update the docs")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("it")
        msg = str(cm.exception)
        self.assertIn("Fix billing", msg)
        self.assertIn("Update the docs", msg)

    def test_ambiguous_overlap_refuses_and_lists_candidates(self):
        self._m("Fix billing in web")
        self._m("Fix billing in mobile")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("fix billing")
        msg = str(cm.exception)
        self.assertIn("web", msg)
        self.assertIn("mobile", msg)

    def test_no_match_names_the_active_missions(self):
        self._m("Fix billing")
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("something unrelated entirely")
        self.assertIn("Fix billing", str(cm.exception))

    def test_no_missions_at_all_says_so(self):
        with self.assertRaises(ValueError) as cm:
            self.svc.resolve("anything")
        self.assertRegex(str(cm.exception).lower(), r"no .*missions")

    def test_a_completed_mission_is_still_reachable_by_exact_title_or_id(self):
        # Fuzzy matching is scoped to ACTIVE missions so "the payment one" means
        # live work — but an exact reference must still find finished work.
        m = self._m("Fix billing")
        self.svc.set_status(m, "completed", by="test")
        self.assertEqual(self.svc.resolve(m.id).id, m.id)
        self.assertEqual(self.svc.resolve("Fix billing").id, m.id)

    def test_fuzzy_prefers_active_over_completed_with_the_same_words(self):
        old = self._m("Fix billing")
        self.svc.set_status(old, "completed", by="test")
        live = self._m("Fix billing again")
        self.assertEqual(self.svc.resolve("billing").id, live.id)


class SpeechDetail(unittest.TestCase):
    def setUp(self):
        MissionResolve.setUp(self)  # same fixture

    def tearDown(self):
        MissionResolve.tearDown(self)

    def test_shape_is_speakable_not_a_dump(self):
        m = self.svc.create(self.project, "Fix billing", created_by="voice")
        d = self.svc.speech_detail(m.id)
        self.assertEqual(set(d), {"mission_id", "title", "goal", "status", "project",
                                  "agents", "sessions", "pending_approval", "last_event"})
        self.assertEqual(d["title"], "Fix billing")
        self.assertEqual(d["project"], "P")
        self.assertEqual(d["sessions"], [])

    def test_unknown_id_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.svc.speech_detail("nope")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run** → FAIL (`AttributeError: 'MissionService' object has no attribute 'resolve'`).

- [ ] **Step 3: Add to `backend/yuri/services/missions.py`**

Add `import re` and these members (place `ACTIVE` beside `GOAL_MAX`):

```python
ACTIVE: tuple[str, ...] = ("running", "waiting_for_approval", "paused", "queued")

# Spoken references to "the mission" rather than a name. Resolution treats these
# as "the one obvious mission" and refuses when more than one is active.
_DEICTIC = frozenset({"", "it", "that", "this", "this one", "that one", "the current one",
                      "current", "the mission", "mission", "the current mission",
                      "the one", "the active one"})
_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))
```

and these methods:

```python
    def active(self) -> list[Mission]:
        """Missions that are live work, newest first."""
        out: list[Mission] = []
        for status in ACTIVE:
            out.extend(self.store.missions.list(status=status))
        return sorted(out, key=lambda m: m.updated_at, reverse=True)

    def resolve(self, ref: str) -> Mission:
        """Resolve a spoken mission reference, refusing to guess.

        Order: exact id, unique id prefix, exact title (any status), then word
        overlap against ACTIVE titles. A deictic phrase ("it", "the current
        one") means the sole active mission. Ambiguity raises ValueError listing
        the candidates — cancelling the wrong mission is worse than asking.
        """
        ref = " ".join((ref or "").strip().split())
        low = ref.lower()
        active = self.active()

        def _refuse(candidates: list[Mission], lead: str) -> ValueError:
            names = ", ".join(f'"{m.title}"' for m in candidates)
            return ValueError(f"{lead} {names}. Ask the user which one.")

        if low in _DEICTIC:
            if len(active) == 1:
                return active[0]
            if not active:
                raise ValueError("There are no active missions right now.")
            raise _refuse(active, f"Which mission? {len(active)} are active:")

        exact = self.store.missions.get(ref)
        if exact is not None:
            return exact

        all_missions = self.store.missions.list(limit=500)
        prefix = [m for m in all_missions if ref and m.id.startswith(ref)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise _refuse(prefix, "That id prefix matches several missions:")

        titled = [m for m in all_missions if m.title.lower() == low]
        if len(titled) == 1:
            return titled[0]
        if len(titled) > 1:
            # Same title twice: prefer a live one, else refuse.
            live = [m for m in titled if m.status in ACTIVE]
            if len(live) == 1:
                return live[0]
            raise _refuse(titled, "Several missions have that name:")

        # Fuzzy: scoped to ACTIVE, so "the payment one" means live work.
        wanted = _words(low)
        if wanted:
            hits = [m for m in active if wanted & _words(m.title)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise _refuse(hits, f"{len(hits)} active missions match that:")

        if not all_missions:
            raise ValueError("There are no missions yet.")
        if active:
            raise _refuse(active, f"I could not match '{ref}'. Active missions:")
        raise ValueError(f"I could not match '{ref}', and nothing is active right now.")

    def speech_detail(self, mission_id: str) -> dict:
        """A mission shaped for speaking, not the full detail() dump."""
        m = self.get(mission_id)
        project = self.store.projects.get(m.project_id)
        sessions = self.store.sessions.list(mission_id=m.id)
        pending = None
        for s in sessions:
            a = self.store.approvals.pending_for_session(s.id)
            if a is not None:
                pending = a.description or a.tool_name
                break
        events = self.store.events.list(mission_id=m.id, limit=1)
        return {"mission_id": m.id, "title": m.title, "goal": m.goal, "status": m.status,
                "project": project.name if project else None,
                "agents": sorted({s.agent_id for s in sessions}),
                "sessions": [{"name": s.name, "status": s.status} for s in sessions],
                "pending_approval": pending,
                "last_event": events[-1].type if events else None}
```

Also make `pause` interrupt live sessions before transitioning (plan §15 — "stop" must work while an agent is running), mirroring `cancel`'s stop-then-transition ordering. Add an `interrupt_sessions` hook beside the existing `stop_sessions`:

```python
        # Injected by the container (SessionService.interrupt_many) to avoid a
        # circular import, same as stop_sessions.
        self.interrupt_sessions: Callable[[list[AgentSession]], Awaitable[None]] | None = None
```

and in `pause`:

```python
    async def pause(self, mission_id: str, by: str) -> Mission:
        m = self.get(mission_id)
        live = self.store.sessions.list(mission_id=m.id, live_only=True)
        if live and self.interrupt_sessions is not None:
            # Interrupt BEFORE transitioning so a stop-triggered status change
            # cannot race the pause (same ordering cancel() already uses).
            await self.interrupt_sessions(live)
        self.set_status(m, "paused", by)
        return m
```

- [ ] **Step 4: Add `SessionService.interrupt_many` and wire it.** In `backend/yuri/services/sessions.py`, beside `stop_many`:

```python
    async def interrupt_many(self, rows: list[AgentSession]) -> None:
        """Interrupt each session, surviving a provider that fails on one."""
        for r in rows:
            try:
                await self.interrupt(r.native_session_id)
            except Exception:
                log.exception("interrupt failed for %s", r.native_session_id[:8])
```

In `build_container`, beside `missions.stop_sessions = sessions.stop_many`, add
`missions.interrupt_sessions = sessions.interrupt_many`.

- [ ] **Step 5: Run** `tests.test_mission_resolve -v` until green, plus `tests.test_mission_service` (the existing pause test must still pass — it has no live sessions, so the new branch is a no-op there).

- [ ] **Step 6: Write `backend/tests/test_mission_tools.py`**

```python
"""The five mission voice tools. They must not change any existing tool's
result keys — test_tools_dispatch is the contract for those.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import tools  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


class MissionTools(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
            mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri")),
        ]
        [p.start() for p in self.patches]
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    async def _start(self, name="s1"):
        return await self.c.sessions.start("proj", name=name)

    def test_all_five_tools_are_exposed_with_object_params(self):
        names = {d["name"] for d in tools.TOOL_DEFINITIONS}
        for t in ("list_missions", "mission_status", "pause_mission",
                  "resume_mission", "cancel_mission"):
            self.assertIn(t, names, t)
        for d in tools.TOOL_DEFINITIONS:
            self.assertEqual(d["parameters"]["type"], "object")

    async def test_list_missions_shape_and_status_filter(self):
        out = await self._start()
        res = await tools.dispatch_tool("list_missions", {})
        self.assertEqual(list(res), ["missions"])
        m = res["missions"][0]
        for k in ("id", "title", "goal", "status", "project", "agents", "sessions"):
            self.assertIn(k, m)
        self.assertEqual(m["id"], out["mission_id"])
        self.assertEqual(await tools.dispatch_tool("list_missions", {"status": "completed"}),
                         {"missions": []})

    async def test_mission_status_resolves_by_title_and_deictically(self):
        out = await self._start(name="Fix billing")
        by_title = await tools.dispatch_tool("mission_status", {"mission": "fix billing"})
        self.assertEqual(by_title["mission_id"], out["mission_id"])
        deictic = await tools.dispatch_tool("mission_status", {})
        self.assertEqual(deictic["mission_id"], out["mission_id"])

    async def test_pause_resume_cancel(self):
        out = await self._start()
        paused = await tools.dispatch_tool("pause_mission", {})
        self.assertEqual(set(paused), {"mission_id", "status", "title", "message"})
        self.assertEqual(paused["status"], "paused")
        self.assertEqual((await tools.dispatch_tool("resume_mission", {}))["status"], "running")
        self.assertEqual((await tools.dispatch_tool("cancel_mission", {}))["status"], "cancelled")

    async def test_pause_interrupts_a_live_session_first(self):
        out = await self._start()
        await tools.dispatch_tool("pause_mission", {})
        self.assertIn(("interrupt", out["session_id"]), self.fake.calls)

    async def test_an_invalid_transition_is_a_soft_error(self):
        await self._start()
        await tools.dispatch_tool("cancel_mission", {})
        with self.assertRaises(ValueError):      # soft: {"ok": false, "error": ...}
            await tools.dispatch_tool("resume_mission", {})

    async def test_ambiguity_is_a_soft_error_listing_candidates(self):
        await self._start(name="Fix billing in web")
        await self._start(name="Fix billing in mobile")
        with self.assertRaises(ValueError) as cm:
            await tools.dispatch_tool("pause_mission", {"mission": "fix billing"})
        self.assertIn("web", str(cm.exception))

    async def test_unknown_mission_is_a_soft_error(self):
        await self._start()
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("mission_status", {"mission": "no such thing"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 7: Add the tools to `backend/tools.py`.** Definitions (after `remember`):

```python
    {
        "type": "function",
        "name": "list_missions",
        "description": "List Yuri's missions — the units of work. Call this when the user asks what's running, what you're working on, or what happened. Omit status for everything; pass a status to filter (running, waiting_for_approval, paused, completed, failed, cancelled).",
        "parameters": {
            "type": "object",
            "properties": {"status": {"type": "string", "description": "Optional status filter."}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "mission_status",
        "description": "Details of one mission: its goal, status, which agents are on it, its sessions and any pending approval. Omit mission to mean the one active mission.",
        "parameters": {
            "type": "object",
            "properties": {"mission": {"type": "string", "description": "Mission title, id, or a phrase from its title. Omit for the current one."}},
            "required": [],
        },
    },
```

plus `pause_mission`, `resume_mission`, `cancel_mission` with the same single
optional `mission` property and descriptions along the lines of "Pause a
mission, interrupting any agent currently working on it." / "Resume a paused
mission." / "Cancel a mission and stop its agents. This ends the work — confirm
with the user first."

Handlers in `dispatch_tool` (before `raise KeyError`):

```python
    if name == "list_missions":
        c = container()
        status = (args.get("status") or "").strip() or None
        missions = c.missions.list(status=status) if status else c.missions.active()
        projects = {p.id: p.name for p in c.projects.registered()}
        out = []
        for m in missions:
            sessions = c.store.sessions.list(mission_id=m.id)
            out.append({"id": m.id, "title": m.title, "goal": m.goal, "status": m.status,
                        "project": projects.get(m.project_id),
                        "agents": sorted({s.agent_id for s in sessions}),
                        "sessions": [s.name for s in sessions if s.name]})
        return {"missions": out}

    if name == "mission_status":
        c = container()
        return c.missions.speech_detail(c.missions.resolve(args.get("mission", "")).id)

    if name in ("pause_mission", "resume_mission", "cancel_mission"):
        c = container()
        m = c.missions.resolve(args.get("mission", ""))
        verb = name.split("_")[0]
        try:
            m = await getattr(c.missions, verb)(m.id, by="voice")
        except InvalidTransition as exc:
            raise ValueError(str(exc)) from exc     # soft error the model can recover from
        return {"mission_id": m.id, "title": m.title, "status": m.status,
                "message": f'Mission "{m.title}" is now {m.status}.'}
```

Add `from yuri.domain.mission import InvalidTransition` to `tools.py`'s imports.

- [ ] **Step 8: Add the prompt bullet to `frontend/lib/operating.ts`** (at the end of the rule list):

```
- MISSIONS: a mission is a unit of work; a session is one agent running inside it. When the user asks "what are you working on", "what's running", "how's it going" or "what happened", call list_missions or mission_status rather than list_sessions — missions are what they mean. "Pause that", "stop the payment one", "cancel it" map to pause_mission / cancel_mission; refer to missions by their title. If a mission reference is ambiguous the tool will tell you which ones matched — read those back and ask which, never guess. Confirm before cancelling: it ends the work.
```

- [ ] **Step 9: Run** `tests.test_mission_tools tests.test_tools_dispatch -v` → both green (the latter proves no existing tool's keys changed), then the full suite.

- [ ] **Step 10: Commit**

```bash
git add backend/yuri/services/missions.py backend/yuri/services/sessions.py backend/yuri/app.py backend/tools.py frontend/lib/operating.ts backend/tests/test_mission_resolve.py backend/tests/test_mission_tools.py
git commit -m "$(cat <<'EOF'
feat(missions): add mission-level voice commands

Yuri's voice could only talk about sessions, so "what are you working on"
had no good answer and "pause that" had nothing to pause. Five tools —
list_missions, mission_status, pause_mission, resume_mission,
cancel_mission — plus MissionService.resolve for spoken references.

Resolution refuses to guess. It matches an id, a unique id prefix, an exact
title, then word overlap against active titles, and treats "it" or "the
current one" as the sole active mission — but ambiguity raises, listing the
candidates. A wrong session pick sends an instruction to the wrong agent; a
wrong mission pick cancels the wrong work.

pause_mission interrupts live sessions before transitioning, so a
stop-triggered status change cannot race the pause — the ordering cancel()
already used.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Narration mode — storage, voice tool, REST, context

**Files:**
- Modify: `backend/yuri/app.py` (`narration` on `Container`, mode accessors), `backend/tools.py` (`set_narration`), `backend/yuri/api/routes.py` (`GET`/`PUT /yuri/narration`, `narration_mode` in `/yuri/context`), `frontend/lib/operating.ts`
- Test: `backend/tests/test_narration_api.py`

**Interfaces — Produces:**
```python
Container.narration: NarrationService     # new field, after `memory`
yuri.app.narration_mode() -> Mode         # reads settings, normalized, defaults "normal"
yuri.app.set_narration_mode(mode) -> Mode # validates via normalize_mode, persists
SETTINGS_KEY = "narration_mode"
# tool: set_narration {mode} -> {mode, message}
# GET  /yuri/narration -> {mode, modes}
# PUT  /yuri/narration {mode} -> {mode}
# /yuri/context gains narration_mode
```

- [ ] **Step 1: Write the failing test**

```python
"""The narration mode is one value, reachable three ways — voice, REST and the
UI toggle — so they can never disagree. It is remembered across sessions and
surfaced at connect.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import tools  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.api.routes import build_router  # noqa: E402
from yuri.narration.policy import DEFAULT_MODE, MODES  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


class NarrationMode(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
            mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri")),
        ]
        [p.start() for p in self.patches]
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"),
                                     FakeAgentProvider())

        async def guard():
            return None
        app = FastAPI()
        app.include_router(build_router(guard))
        self.client = TestClient(app)

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    def test_default_is_normal(self):
        self.assertEqual(yapp.narration_mode(), DEFAULT_MODE)
        self.assertEqual(self.client.get("/yuri/narration").json(),
                         {"mode": "normal", "modes": list(MODES)})

    def test_set_and_persist(self):
        self.assertEqual(yapp.set_narration_mode("quiet"), "quiet")
        self.assertEqual(yapp.narration_mode(), "quiet")
        # Survives a fresh read of the same store, i.e. it is really persisted.
        self.assertEqual(self.c.store.settings.get("narration_mode"), "quiet")

    def test_rest_round_trip(self):
        r = self.client.put("/yuri/narration", json={"mode": "verbose"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "verbose")
        self.assertEqual(self.client.get("/yuri/narration").json()["mode"], "verbose")

    def test_rest_rejects_an_unknown_mode(self):
        r = self.client.put("/yuri/narration", json={"mode": "loud"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(yapp.narration_mode(), DEFAULT_MODE)   # unchanged

    async def test_voice_tool_sets_it(self):
        out = await tools.dispatch_tool("set_narration", {"mode": "quiet"})
        self.assertEqual(out["mode"], "quiet")
        self.assertIn("quiet", out["message"].lower())
        self.assertEqual(yapp.narration_mode(), "quiet")

    async def test_voice_tool_rejects_an_unknown_mode_softly(self):
        with self.assertRaises(ValueError) as cm:
            await tools.dispatch_tool("set_narration", {"mode": "loud"})
        self.assertIn("quiet", str(cm.exception))     # names the valid modes
        self.assertEqual(yapp.narration_mode(), DEFAULT_MODE)

    def test_tool_is_exposed(self):
        d = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "set_narration")
        self.assertEqual(d["parameters"]["required"], ["mode"])

    def test_context_carries_the_mode(self):
        yapp.set_narration_mode("verbose")
        ctx = self.client.get("/yuri/context").json()
        self.assertEqual(ctx["narration_mode"], "verbose")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.**

In `backend/yuri/app.py`: add `narration: NarrationService` to `Container` (after `memory`), build it in `build_container`, and add module functions:

```python
SETTINGS_NARRATION_MODE = "narration_mode"


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
```

In `backend/tools.py`, add the definition:

```python
    {
        "type": "function",
        "name": "set_narration",
        "description": "Change how much you narrate. 'quiet' = only problems and things needing the user's answer; 'normal' = meaningful progress; 'verbose' = every tool and cost update too. Call this when the user says be quiet, stop narrating, tell me everything, or go back to normal. The setting is remembered.",
        "parameters": {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["quiet", "normal", "verbose"]}},
            "required": ["mode"],
        },
    },
```

and the handler:

```python
    if name == "set_narration":
        from yuri.app import set_narration_mode
        mode = set_narration_mode(args.get("mode"))
        blurb = {"quiet": "I'll only speak up for problems and anything needing your answer.",
                 "normal": "Back to normal narration.",
                 "verbose": "I'll narrate everything, including each tool call."}[mode]
        return {"mode": mode, "message": blurb}
```

In `backend/yuri/api/routes.py`: a `NarrationUpdate(BaseModel)` with `mode: str` in `schemas.py`, then

```python
    @r.get("/narration")
    async def get_narration():
        return {"mode": narration_mode(), "modes": list(MODES)}

    @r.put("/narration")
    async def put_narration(body: NarrationUpdate):
        try:
            return {"mode": set_narration_mode(body.mode)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
```

and add `"narration_mode": narration_mode(),` to `/yuri/context`'s returned dict.

Add the prompt bullet to `frontend/lib/operating.ts`:

```
- NARRATION: you narrate in one of three modes — quiet (only problems and things needing the user's answer), normal (meaningful progress), verbose (every tool call and cost too). "Be quiet", "stop narrating", "less" → set_narration quiet. "Tell me everything", "verbose", "more detail" → verbose. "Normal" → normal. It's remembered between conversations, so if it's already quiet don't apologise for being quiet — that's what they asked for.
```

- [ ] **Step 4: Run** `tests.test_narration_api tests.test_yuri_api -v` → green (the latter's programmatic auth enumeration now covers the two new routes automatically — confirm the count went up), then the full suite.

- [ ] **Step 5: Commit**

```bash
git add backend/yuri/app.py backend/tools.py backend/yuri/api/routes.py backend/yuri/api/schemas.py frontend/lib/operating.ts backend/tests/test_narration_api.py
git commit -m "$(cat <<'EOF'
feat(narration): make the mode settable by voice, REST and the UI

One value in the settings table, reachable three ways so they cannot
disagree, remembered between conversations, and surfaced in /yuri/context
so a fresh voice session honours it without being told twice.

narration_mode() absorbs a corrupt stored value rather than raising;
set_narration_mode() raises naming the valid modes, because a caller
setting a mode deserves to be told it was wrong.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Attach `narration` to the poll result and the SSE frame

**Files:**
- Modify: `backend/yuri/services/sessions.py` (`poll()` attaches it), `backend/yuri/api/routes.py` (SSE frames), `backend/yuri/app.py` (pass `narration` into `SessionService`)
- Test: `backend/tests/test_narration_wiring.py`

**Interfaces — Produces:**
```python
SessionService(..., narration: NarrationService | None = None, mode_reader: Callable[[], Mode] | None = None)
    # poll()'s returned dict gains `narration: str | None`
# /yuri/events/stream frames gain `narration: str | None` beside the event fields
```

The mode is read through an injected callable rather than imported, so a test
can set the mode without a container and `SessionService` keeps no dependency on
`yuri.app` (which imports it — a direct import would be a cycle).

- [ ] **Step 1: Write the failing test**

```python
"""The narration field is the whole frontend contract: if a carrier has a line,
inject it. This pins that BOTH carriers attach it, that neither narrates the
other's events (or the user hears everything twice), and that the mode is
honoured on both.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.domain.event import EventType  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402

PERM = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"],
        "request_id": "r1"}


class PollNarration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
            mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri")),
        ]
        [p.start() for p in self.patches]
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    async def _sid(self):
        return (await self.c.sessions.start("proj", name="billing"))["session_id"]

    async def test_completed_carries_a_line_quoting_the_agent(self):
        sid = await self._sid()
        self.fake.script(sid, {"status": "completed", "assistant_text": "I changed two files."})
        res = self.c.sessions.poll(sid)
        self.assertIn("narration", res)
        self.assertIn("changed two files", res["narration"])

    async def test_permission_carries_a_line(self):
        sid = await self._sid()
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.assertIn("rm -rf build", self.c.sessions.poll(sid)["narration"])

    async def test_working_and_idle_carry_none(self):
        sid = await self._sid()
        self.assertIsNone(self.c.sessions.poll(sid).get("narration"))

    async def test_quiet_mode_suppresses_completion_but_not_permission(self):
        sid = await self._sid()
        yapp.set_narration_mode("quiet")
        self.fake.script(sid, {"status": "completed", "assistant_text": "done"})
        self.assertIsNone(self.c.sessions.poll(sid)["narration"])
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.assertIsNotNone(self.c.sessions.poll(sid)["narration"])

    async def test_the_result_keys_are_otherwise_unchanged(self):
        # narration is additive: everything the frontend already reads survives.
        sid = await self._sid()
        self.fake.script(sid, {"status": "completed", "assistant_text": "x", "session_id": sid})
        res = self.c.sessions.poll(sid)
        for k in ("status", "session_id", "assistant_text"):
            self.assertIn(k, res)


class StreamNarration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        PollNarration.setUp(self)

    def tearDown(self):
        PollNarration.tearDown(self)

    async def _frames(self, published):
        """Drive the SSE generator far enough to read the replayed frames."""
        from fastapi import FastAPI
        from yuri.api.routes import build_router

        async def guard():
            return None
        app = FastAPI()
        app.include_router(build_router(guard))
        for e in published:
            self.c.store.events.insert(e)      # replay path reads the repo
        route = next(r for r in app.routes if getattr(r, "path", "") == "/yuri/events/stream")
        resp = await route.endpoint()
        out = []
        agen = resp.body_iterator
        try:
            for _ in range(len(published)):
                chunk = await agen.__anext__()
                if chunk.startswith("data: "):
                    out.append(json.loads(chunk[6:].strip()))
        finally:
            await agen.aclose()
        return out

    async def test_mission_created_frame_carries_a_line(self):
        from yuri.domain.event import YuriEvent
        e = YuriEvent.make(EventType.MISSION_CREATED,
                           payload={"title": "Fix billing", "project": "P"})
        [frame] = await self._frames([e])
        self.assertIn("Fix billing", frame["narration"])
        self.assertEqual(frame["type"], EventType.MISSION_CREATED)

    async def test_poll_owned_events_carry_none_on_the_stream(self):
        # The anti-double-speak guarantee, at the wire level.
        from yuri.domain.event import YuriEvent
        events = [YuriEvent.make(EventType.SESSION_TURN_COMPLETED,
                                 payload={"assistant_text": "done"}),
                  YuriEvent.make(EventType.APPROVAL_REQUESTED,
                                 payload={"description": "run rm -rf build"})]
        frames = await self._frames(events)
        for f in frames:
            self.assertIsNone(f["narration"], f["type"])

    async def test_verbose_only_events_are_none_at_normal(self):
        from yuri.domain.event import YuriEvent
        e = YuriEvent.make(EventType.TOOL_STARTED, payload={"tool_name": "Read"})
        [frame] = await self._frames([e])
        self.assertIsNone(frame["narration"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run** → FAIL (no `narration` key).

- [ ] **Step 3: Implement.**

`backend/yuri/services/sessions.py` — add to `__init__`:

```python
                 narration: NarrationService | None = None,
                 mode_reader: Callable[[], Mode] | None = None):
        ...
        self.narration = narration or NarrationService()
        # The mode lives in the store, which yuri.app reads — importing app here
        # would be a cycle, so the container injects a reader instead.
        self._mode_reader = mode_reader or (lambda: DEFAULT_MODE)
```

and at the end of `poll()`, replacing `return res`:

```python
        # The frontend's whole rule is "if it has a narration line, inject it".
        # Poll owns the four session-turn events (yuri/narration/policy.py); the
        # stream must not also narrate them or the user hears each one twice.
        agent = self.registry.get(row.agent_id).name if row else ""
        res = {**res, "narration": self.narration.line_for_poll(
            res, row.name if row else None, agent, self._mode_reader())}
        return res
```

Note the early `if row is None: return res` path returns before this — give it a
`narration: None` too so the key is always present:

```python
        if row is None:
            return {**res, "narration": None}
```

Also enrich the permission branch so the `dangerous` prefix can fire: after
`self.approvals.record_request(row, prompt)`, capture the returned `Approval`
and set `res = {**res, "risk": approval.risk}` before narration runs. (This is
the poll-path risk limitation from spec §5.3 — it uses whatever risk the
recorded approval has, which is `confirm` when `tool_input` was absent.)

`backend/yuri/api/routes.py` — in the SSE generator, replace both
`json.dumps(e.to_dict(), default=str)` sites with a helper:

```python
        def _frame(e) -> str:
            mode = narration_mode()
            payload = {**e.to_dict(),
                       "narration": c.narration.line_for(e, mode)}
            return f"data: {json.dumps(payload, default=str)}\n\n"
```

`backend/yuri/app.py` — pass both into `SessionService`:

```python
        sessions = SessionService(store, bus, journal, registry, projects, approvals, missions,
                                  default_agent=default_agent, router=router,
                                  narration=narration, mode_reader=narration_mode)
```

- [ ] **Step 4: Run** `tests.test_narration_wiring tests.test_session_service tests.test_yuri_api -v` → all green, then the full suite. `test_session_service`'s existing poll assertions must still pass — `narration` is additive.

- [ ] **Step 5: Commit**

```bash
git add backend/yuri/services/sessions.py backend/yuri/api/routes.py backend/yuri/app.py backend/tests/test_narration_wiring.py
git commit -m "$(cat <<'EOF'
feat(narration): attach the narration line to both carriers

The poll result and each SSE frame now carry `narration: str | None`, so
the frontend's entire rule becomes "if it has a line, inject it" and the
wording stays server-side where it is testable.

The anti-double-speak guarantee is now pinned at the wire level: a
poll-owned event carries None on the stream, so subscribing to both cannot
make Yuri say the same thing twice.

SessionService reads the mode through an injected callable rather than
importing yuri.app, which imports it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Frontend — inject the line, subscribe to narration, add the toggle

**Files:**
- Create: `frontend/lib/narration.ts`, `frontend/lib/narration.test.ts`
- Modify: `frontend/components/VoiceAgent.tsx`

**Interfaces — Produces:**
```ts
export type NarrationMode = "quiet" | "normal" | "verbose";
export const NARRATION_MODES: NarrationMode[];
export type NarratedFrame = { narration?: string | null };
export function narrationOf(x: NarratedFrame | null | undefined): string | null;
```

- [ ] **Step 1: Write `frontend/lib/narration.test.ts`**

```ts
// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { narrationOf, NARRATION_MODES } from "./narration.ts";

test("a frame with a narration line yields it", () => {
  assert.equal(narrationOf({ narration: "Starting \"Fix billing\"." }), "Starting \"Fix billing\".");
});

test("null, undefined, missing and empty all yield null", () => {
  assert.equal(narrationOf(null), null);
  assert.equal(narrationOf(undefined), null);
  assert.equal(narrationOf({}), null);
  assert.equal(narrationOf({ narration: null }), null);
  assert.equal(narrationOf({ narration: "" }), null);
  assert.equal(narrationOf({ narration: "   " }), null);
});

test("a non-string narration is ignored rather than injected", () => {
  // Defensive: the field crosses a network boundary.
  assert.equal(narrationOf({ narration: 42 } as never), null);
});

test("the three modes are exactly the backend's", () => {
  assert.deepEqual(NARRATION_MODES, ["quiet", "normal", "verbose"]);
});
```

- [ ] **Step 2: Run** `cd frontend && npm test` → FAIL (module not found).

- [ ] **Step 3: Write `frontend/lib/narration.ts`**

```ts
// Yuri's spoken lines are authored by the backend (see
// docs/superpowers/specs/2026-09-02-yuri-orchestration-narration-design.md §4),
// which attaches a `narration` field to both the poll result and each SSE
// frame. The frontend's entire rule is: if it has a line, inject it.
//
// Keeping that rule here — rather than inline in VoiceAgent — makes it
// testable and keeps the "who phrases it" boundary obvious.

export type NarrationMode = "quiet" | "normal" | "verbose";
export const NARRATION_MODES: NarrationMode[] = ["quiet", "normal", "verbose"];

export type NarratedFrame = { narration?: string | null };

/** The line to speak, or null. Non-strings and blanks are ignored — the field
 *  crosses a network boundary, so it is not trusted to be well-formed. */
export function narrationOf(x: NarratedFrame | null | undefined): string | null {
  if (!x || typeof x !== "object") return null;
  const n = (x as NarratedFrame).narration;
  if (typeof n !== "string") return null;
  const trimmed = n.trim();
  return trimmed.length > 0 ? trimmed : null;
}
```

- [ ] **Step 4: Run** `npm test` → 9 + 4 = 13 passing.

- [ ] **Step 5: Rewire `frontend/components/VoiceAgent.tsx`.** Four edits:

  **(a) Inject the backend's line instead of building strings.** In
  `handleClaudeResult` (~:674), keep the `setPending` / `clearPendingFor` calls
  and the `refreshSessions()` exactly as they are — that is UI state — and
  replace all three `const msg = ...; sessionRef.current?.injectUpdate(msg)`
  blocks with one, placed after the pending-card handling:

```ts
    // Wording comes from the backend so it is consistent, testable, and the
    // same for any future non-browser surface. See lib/narration.ts.
    const line = narrationOf(res);
    if (line) {
      sessionRef.current?.injectUpdate(line);
      logDebug("inject", line, { session: sid }, "backend", "voice");
    }
```

  The status branches still decide `setPending` / `clearPendingFor`; only the
  message construction goes. Import `narrationOf` from `@/lib/narration`.

  **(b) Subscribe to the narration stream.** A second `EventSource`, separate
  from the existing `/debug/stream` one (which feeds the Activity panel and must
  keep working). Add beside that effect:

```ts
  // Mission-level narration: the poll loop owns session-turn events, the stream
  // owns mission state and lost contact (backend policy decides which). Only
  // frames carrying a narration line are spoken.
  useEffect(() => {
    if (!connected) return;
    const es = new EventSource(withAuthParam(`${backendBase()}/yuri/events/stream?limit=0`));
    es.onmessage = (m) => {
      try {
        const line = narrationOf(JSON.parse(m.data));
        if (line) {
          sessionRef.current?.injectUpdate(line);
          logDebug("inject", line, undefined, "backend", "voice");
        }
      } catch {
        /* malformed frame; ignore */
      }
    };
    return () => es.close();
  }, [connected]);
```

  Gate it on the voice session being connected — narrating into a closed session
  is pointless, and it avoids a stream open on a page nobody is talking to.
  `limit=0` skips the replay so reconnecting does not re-speak history; if the
  backend clamps `limit` to a minimum of 1, use `limit=1` and drop the first
  frame instead.

  **(c) The mode toggle.** Beside the existing provider/backend toggles: read
  `GET /api/yuri/narration` on mount into state, render a three-way control, and
  `PUT` on change. Follow the existing toggles' markup and class names. Because
  the mode is server-side, do **not** persist it to `localStorage` — that would
  create a second source of truth that can disagree with voice.

  **(d) Re-read the mode when the voice model changes it.** `set_narration` is a
  tool call, so the existing `onEvent` `tool_call` handler can refetch:

```ts
      if (e.type === "tool_call" && e.name === "set_narration") refreshNarrationMode();
```

- [ ] **Step 6: Verify.**

Run: `cd frontend && npx tsc --noEmit` → clean; `npm test` → 13 passing.
Run: `cd backend && .venv/bin/python -m unittest discover -s tests` → OK, pristine.

Then grep to prove the old strings are gone:
`grep -c "Claude update" frontend/components/VoiceAgent.tsx` → should be 0 in
the injection paths (a comment mentioning the old mechanism is fine; the
template literals must be gone).

- [ ] **Step 7: Commit**

```bash
git add frontend/lib/narration.ts frontend/lib/narration.test.ts frontend/components/VoiceAgent.tsx
git commit -m "$(cat <<'EOF'
feat(voice): speak the backend's narration and add the mode toggle

The three hardcoded "[Claude update] …" template literals are gone. The
frontend's whole rule is now: if a carrier has a narration line, inject it.
Prompt cards stay client-side — that is UI state, and scopedClearPending's
cross-session safeguard is untouched.

A second EventSource carries mission-level narration, separate from the
Activity panel's /debug/stream and gated on the voice session being
connected. The mode toggle reads and writes the server value rather than
localStorage, so voice and UI cannot disagree.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Live verification

Not automatable — the double-speak failure is invisible to unit tests, since
both carriers can be individually correct while the user hears everything twice
(spec §12). Record the results in
`docs/superpowers/plans/2026-09-02-yuri-orchestration-verification.md`.

Start both servers:

```bash
cd backend && ./run.sh
```
```bash
cd frontend && npm run dev
```

- [ ] **Nothing is said twice.** Connect, `start_session`, `tell_claude` with
  something that triggers a permission prompt, answer it, let the turn finish.
  Each of: the mission starting, the permission request, and the turn
  completion should be spoken **once**. This is the acceptance gate.
- [ ] **Mission narration arrives.** "Starting …" when a session starts; the
  status line when a mission pauses or is cancelled.
- [ ] **Mission commands work by voice.** "What are you working on?" →
  `list_missions`. "What's the status of the yuri-code one?" →
  `mission_status`. "Pause that" → paused, and the agent is interrupted.
  "Cancel it" → she confirms first, then cancels.
- [ ] **Ambiguity is refused, not guessed.** Start two sessions with similar
  names, then "pause the yuri one" — she should read back the candidates and
  ask which, never pick.
- [ ] **Modes work by voice and stick.** "Be quiet" → only problems and
  questions; verify a permission prompt is STILL spoken in quiet mode. "Tell me
  everything" → tool-level narration appears. Reload the page: the mode
  survives, and the UI toggle shows it.
- [ ] **The UI toggle and voice agree.** Change it in the UI, ask her to
  narrate normally, confirm both reflect the same value.
- [ ] **Nothing regressed.** The live-verified foundation path still works:
  connect → start → tell → permission → answer → close. Terminal attaches,
  Activity panel still populates, `cost_usd` still shows.
- [ ] **Final:** full backend suite OK and pristine in both `~/Yuri` states;
  `npm test`; `npx tsc --noEmit`. Report anything spoken twice, spoken wrongly,
  or not spoken at all.
