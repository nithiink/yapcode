# Yuri — Orchestration & Narration (Phase 4) — Design Spec

**Date:** 2026-09-02
**Base:** `main` at `352e97c` (phases 1–3 merged as `4d47cb8`)
**Source plan:** `~/Downloads/yuri-plan.md` — phase 4, plus §14 (voice intent model), §16 (narration), §17 (orchestrator), §18 (agent selection).
**Predecessor spec:** `docs/superpowers/specs/2026-09-02-yuri-foundation-design.md`
**Carries:** `docs/yuri/follow-ups.md` — read it; two entries there directly shape this design.

---

## 1. What this increment is

Yuri currently records everything and says almost nothing. Missions, sessions,
approvals and events persist and survive a restart, but her voice still only
reacts to three per-session statuses, phrased by three hardcoded strings in the
frontend. Nothing she says knows a mission exists.

This increment gives her a voice over her own domain: narration driven by
events with quiet / normal / verbose modes, mission-level voice commands, and
one place where agent selection happens. It deliberately does **not** add an
orchestrator class.

**Out of scope** (unchanged from the foundation spec §10 unless listed here):
OpenCode and Hermes providers · Mission Control UI · multi-agent missions ·
`YuriSelfProvider` · semantic memory · cost-aware routing · parallel agents ·
the §33 file moves · renaming `VC_*`.

---

## 2. Decisions taken before design

| Decision | Choice | Why |
|---|---|---|
| Narration transport | **Hybrid, split by event type** | See §3 — the naive split double-speaks. |
| Where wording lives | **Backend `NarrationService`** | Testable, consistent, reusable by a future non-browser surface, and puts the §38 honesty rules where code can enforce them. |
| Orchestrator | **Thin — no orchestrator class** | `SessionService`/`MissionService` already do the work plan §17 assigns an orchestrator. Adding a delegating layer is the over-engineering §49 warns against. Revisit in phase 7. |
| Mode control | **Voice tool + remembered across sessions + UI toggle** | No per-mission override (deferred). |

---

## 3. The narration ownership split

**The problem.** Every one of the four events the poll loop already narrates is
also `speakable: True` in `domain/event.py`'s `DEFAULTS`:
`approval.requested`, `session.question`, `agent.error`,
`session.turn_completed`. Subscribing to the event stream and speaking
`speakable` events, while leaving the poll injection in place, speaks all four
**twice** — the Activity-feed double-log the foundation's final review caught,
but out loud.

**The rule.** Ownership is per event type, declared once in code:

| Owner | Event types | Rationale |
|---|---|---|
| **Poll** (`SessionService.poll` result) | `approval.requested`, `session.question`, `agent.error`, `session.turn_completed` | Already reliable, already verified live. Critically, the poll result carries **every sub-question** of a multi-question `AskUserQuestion`, which the observer path does not emit (`follow-ups.md`). |
| **Stream** (`/yuri/events/stream`) | `mission.created`, `mission.status_changed`, `session.lost` | Poll cannot see mission-level state at all. |
| **Stream, verbose only** | `tool.started`, `cost.updated` | The "OpenCode is inspecting the payment service" texture; too noisy below verbose. |
| **Neither** | `session.created`, `session.message_sent`, `approval.resolved`, `session.interrupted`, `session.stopped`, `project.registered`, `memory.remembered` | The user caused these; narrating them is telling them what they just did. `session.message_sent` is her own instruction going out. `session.created` also carries `revived=True` on a rehydration revival and must never be announced as new work starting. |

`NARRATION_OWNER: dict[str, Owner]` in `yuri/narration/policy.py` is the single
declaration. Two tests enforce it: every `EventType` appears exactly once, and
no type is owned by both sides.

**Why not stream-only:** reaching parity requires `tmux_runner` to notify on
every prompt and sub-question — surgery on the repo's most fragile file to
replace something that works, where a missed notify means the user is silently
not told. Revisit if phase 6/7 needs it.

---

## 4. Delivery: one field, one frontend rule

The backend attaches a **`narration`** field to both carriers:

- **Poll result** — `SessionService.poll()`'s returned dict gains
  `narration: str | None`.
- **SSE frame** — `/yuri/events/stream` frames gain `narration: str | None`
  alongside the existing event fields.

`null` means "not spoken in the current mode". The frontend rule becomes, in
full: **if it has a `narration` line, inject it.** The three hardcoded
`[Claude update] …` strings in `frontend/components/VoiceAgent.tsx` are deleted.

Mode filtering is server-side, where the remembered preference already lives.
This keeps a single source of truth and means a future phone or CLI surface
inherits identical narration.

**The prompt card stays client-side.** `handleClaudeResult` keeps setting
`pending` from `res.prompt` — that is UI state, not narration, and the existing
`scopedClearPending` safeguard (one session's result must not dismiss another's
card) is preserved untouched.

---

## 5. `NarrationService`

`backend/yuri/narration/service.py`. Pure: no I/O, no store, no provider.

```python
Mode = Literal["quiet", "normal", "verbose"]

class NarrationService:
    def line_for(self, event: YuriEvent, mode: Mode) -> str | None
    def line_for_poll(self, result: dict, row: AgentSession | None, mode: Mode) -> str | None
```

### 5.1 Mode filter

Applied before any phrasing:

| Mode | Speaks |
|---|---|
| `quiet` | `severity` in {`warning`, `error`} **plus** `approval.requested` and `session.question` — anything that blocks on the user is never suppressed. Quiet means "don't chatter", not "don't ask". |
| `normal` (default) | everything `speakable`, excluding `debug` severity. |
| `verbose` | everything `speakable`, plus the `debug`-severity stream events (`tool.started`, `cost.updated`). |

That quiet-mode carve-out is the one non-obvious rule: a mode that could swallow
a permission request would strand the agent waiting on an answer the user was
never asked for.

### 5.2 Phrasing, and the honesty rules

Every line is generated from event payload fields, never from free text, so
§38's said/did/verified distinction is structural:

| Event | Line |
|---|---|
| `mission.created` | `Starting "<title>" in <project>.` |
| `mission.status_changed` → `completed` | `"<title>" is done.` |
| → `failed` | `"<title>" failed: <reason>.` |
| → `paused` | `"<title>" is paused.` |
| → `cancelled` | `"<title>" is cancelled.` |
| → `waiting_for_approval` | *(nothing — the approval event itself speaks)* |
| `session.lost` | `I lost contact with "<name>" — its agent didn't survive the restart.` |
| `tool.started` (verbose) | `<agent> is using <tool_name>.` |
| `cost.updated` (verbose) | `<name> is at $<cost>.` |
| poll `approval.requested` | `<agent> needs permission to <description>. Approve or deny?` — prefixed `That's a destructive action — ` when `risk == "dangerous"`. |
| poll `session.question` | `<agent> is asking: <text>` + numbered options. |
| poll `session.turn_completed` | `<agent> finished<for-request>. It said: <assistant_text>` |
| poll `agent.error` | `<agent> hit an error<for-request>: <message>` |

Three rules the phrasing must obey, each with a test:

1. **Never assert completion of work, only of a turn.** `session.turn_completed`
   says the agent *finished and said X* — never "it's fixed", "it works", or
   "the tests pass". The agent's own words are quoted as the agent's.
2. **Surface risk before asking.** A `dangerous` approval is announced as
   destructive; the user should not have to infer it from the command.
3. **Attribute the request.** The existing `res.request` threading (which stops
   the model confusing this update with a previous prompt's) is preserved in the
   completion and error lines.

The two existing model-facing instructions that made these injections work —
"This is the latest result … do NOT say this request is still in progress" and
"Read the options to the user and get their choice" — move into the narration
lines. They are load-bearing prompt engineering, not decoration.

### 5.3 Known limitation, inherited

`session.turn_completed` is not emitted for a multi-question form's sub-questions
2..n, and poll-path approvals under-classify `risk` because
`AdvanceResult.to_dict()` omits `tool_input` (`follow-ups.md`). Consequence for
this design: rule 2 above degrades to a plain confirm prompt on the poll path
for a destructive command the observer did not record first. Documented, not
fixed here — fixing it means changing `AdvanceResult`, which the foundation's
regression contract covers.

---

## 6. Narration mode: storage, control, surfacing

- **Stored** in the existing `settings` table under `narration_mode`
  (`SettingsRepo`), defaulting to `normal`. Survives restarts; not per-mission.
- **Voice:** a `set_narration` tool taking `mode: quiet|normal|verbose`,
  returning the new mode and a one-line confirmation. The prompt in
  `operating.ts` gains a bullet mapping "be quiet" / "stop narrating" →
  `quiet`, "tell me everything" / "verbose" → `verbose`, "normal" → `normal`.
- **REST:** `GET /yuri/narration` and `PUT /yuri/narration` (body
  `{mode}`), both behind `require_auth` like every other `/yuri/*` route.
- **UI:** a three-state toggle beside the existing provider/backend toggles in
  `VoiceAgent.tsx`, reading and writing the REST endpoint so voice and UI can
  never disagree.
- **Context:** `/yuri/context` gains `narration_mode`, so a fresh voice session
  knows the mode without being told.

---

## 7. `AgentRouter`

`backend/yuri/services/router.py`. Extracts the selection currently inlined at
`services/sessions.py:232`:

```python
class AgentRouter:
    def select(self, project: Project, requested: str | None = None) -> AgentProvider
```

Order: explicit `requested` → `project.default_agent` → the container's default
agent. Raises `KeyError` naming the known agent ids when the requested id is
unknown — which `tools.py` turns into a soft error the voice model recovers from
("I don't have an agent called that; I have Claude Code").

Deliberately **not** in this increment (plan §18 lists them as future): task
type, model availability, cost, latency, capability matching, workload. The
class exists so those have one home; adding them now would be a router with no
second agent to route to.

`SessionService.start`/`adopt` take the router by constructor injection, so
`build_container` wires it and the existing tests keep their seam.

---

## 8. Mission voice commands

Five new tools in `TOOL_DEFINITIONS`, alongside the existing seventeen:

| Tool | Args | Returns |
|---|---|---|
| `list_missions` | `status?` | `{missions: [{id, title, goal, status, project, agent, sessions}]}` |
| `mission_status` | `mission?` | one mission's detail, shaped for speech (not the full `detail()` dump) |
| `pause_mission` | `mission?` | `{mission_id, status, message}` |
| `resume_mission` | `mission?` | same |
| `cancel_mission` | `mission?` | same |

### 8.1 `resolve_mission(ref)`

Mission references arrive as speech, so resolution mirrors
`SessionService.resolve`'s discipline — and its refusal to guess:

1. A Yuri mission id, or a unique id prefix.
2. Exact case-insensitive title match.
3. Substring/word-overlap match against the titles of **active** missions
   (`running`, `waiting_for_approval`, `paused`, `queued`).
4. Empty or a deictic phrase ("it", "that", "this one", "the current one") →
   the sole active mission if there is exactly one.

More than one match at any step raises `ValueError` listing the candidate
titles — never a silent pick. This is the same failure the foundation's final
review caught for session names (a wrong pick sends the user's instruction to
the wrong agent); a wrong mission pick cancels the wrong work, which is worse.

Zero matches raises `ValueError` naming the active missions.

`MissionService` gains `resolve(ref) -> Mission`; the tools call it.

### 8.2 Interruption

Plan §15 requires "stop" to work while an agent is running. It already does at
session level (`interrupt_session`). `pause_mission` on a mission whose sessions
are live interrupts them first, then transitions — mirroring `cancel`'s existing
stop-then-transition ordering, so a stop-triggered status change cannot race the
pause.

---

## 9. What must keep working

The foundation's regression contract plus:

- `backend/tests/test_tools_dispatch.py` — the seventeen existing tools' result
  keys stay byte-identical. The five new tools are additive.
- The live-verified voice path: connect → `start_session` → `tell_claude` →
  permission prompt → answer → `close_session`. Narration wording changes;
  *when* she speaks must not regress.
- `scopedClearPending`'s cross-session safeguard.
- The Activity panel's existing `/debug/stream` subscription — the new
  narration stream is a second, separate `EventSource`.
- 282 backend + 9 frontend tests, pristine, in both `~/Yuri` states.

---

## 10. Testing

- `test_narration_policy.py` — every `EventType` owned exactly once; no type on
  both sides; the quiet-mode carve-out for `approval.requested` /
  `session.question`.
- `test_narration_service.py` — a line for each owned event in each mode;
  `None` where suppressed; the three honesty rules (no completion claim on a
  turn, `dangerous` prefix present, request attribution preserved).
- `test_agent_router.py` — the three-step order; unknown id raises `KeyError`
  naming known ids.
- `test_mission_resolve.py` — all four ref forms; ambiguity raises listing
  candidates; zero matches raises naming active missions.
- `test_mission_tools.py` — the five tools' result keys; `pause_mission`
  interrupts live sessions before transitioning; `InvalidTransition` surfaces as
  a soft error.
- `test_narration_api.py` — `GET`/`PUT /yuri/narration` behind auth; mode
  persists; `/yuri/context` carries it.
- `frontend/lib/narration.test.ts` — the inject rule: a frame with `narration`
  injects, `null` does not; a prompt still sets the card when narration is
  suppressed.

All without tmux, Claude, network or voice keys.

---

## 11. Order of work

1. `NarrationService` + policy table (pure, no wiring) — the honesty rules
   land first and everything else is measured against them.
2. `AgentRouter` extraction — small, isolated, no behavior change.
3. Mission resolution + the five voice tools.
4. Narration mode: settings, tool, REST, `/yuri/context`.
5. Wire `narration` into the poll result and the SSE frame.
6. Frontend: delete the hardcoded strings, subscribe to the narration stream,
   add the toggle.
7. **Checkpoint — live voice run.** Mode changes by voice, mission commands,
   and no double-speaking. This one needs the user at a microphone; the
   double-speak failure is the specific thing automated tests cannot catch.

---

## 12. Risks

**Double-speaking is the failure mode of this whole increment**, and it is
invisible to unit tests: both carriers can be individually correct while the
user hears everything twice. The ownership table plus its two tests are the
structural guard; the live checkpoint is the real one.

**Narration that lies** is the other. Every phrasing is generated from payload
fields, and the three honesty rules have tests — but a line that reads naturally
while asserting more than the event proves would pass those tests. Worth reading
the generated lines aloud during review rather than only diffing them.
