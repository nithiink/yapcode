# Yuri Phase 6 — Mission Control UI

Turn the voice-first single screen into an eight-view application shell, with
voice always present. Frontend-only: the backend already exposes 21 `/yuri/*`
routes covering nearly everything the views need.

## 1. Why this shape

Phase 5 built a great deal you cannot currently see: missions with a state
machine, approvals with risk labels, an event log, narration modes, and a
second coding agent. Today's UI is one screen with two or three panels, so
none of it is visible. Phase 7 (multi-agent missions) would be substantially
harder to trust without a screen that shows what is happening across agents.

The Dashboard is built around **"what needs me right now"** because the
governing fact of the system is that agents stop and wait for a human. An
unanswered approval is the only state that costs real time, so surfacing it is
the screen's job. Everything else is one click away.

## 2. The shell

`app/layout.tsx` becomes a three-region CSS grid: a slim left **nav**, the
routed `{children}` as **main**, and the **voice rail** on the right.

**The rail and its provider live in the layout, above `{children}`.** This is
the structural crux, not a preference. The voice connection and both
`EventSource` subscriptions currently live inside `VoiceAgent`'s hooks
(`components/VoiceAgent.tsx:524,528`). In the App Router a layout persists
across route changes, so navigating re-renders only `main`. Put voice inside a
route instead and every navigation tears down and rebuilds the session —
Yuri would drop mid-sentence on a click.

Eight routes, each a client `page.tsx`: `/` (Dashboard), `/missions`,
`/projects`, `/agents`, `/sessions`, `/approvals`, `/terminal`, `/activity`.
Real URLs, so deep links and the back button work and a tab can be parked on
Approvals. Client components throughout — everything reads a live backend
behind auth, so there is no server-rendering benefit to chase.

Grid at the top level, `<nav> 1fr <rail>`; rail ~380px (matching today's
conversation panel), nav ~200px. Every track carries `min-width: 0` — the
lesson already commented in `globals.css:175`: without it a long path or
queued prompt blows out a track and resizes the whole layout.

**Nav shows state, not just labels.** Approvals badges a pending count;
Missions badges blocked or failed. That is what makes the "what needs me"
choice hold when you are parked on another view — the nav is the alarm. Counts
come from the shared context, so no extra polling.

**Phone is one media query, not a second layout.** At `max-width: 900px` — one
breakpoint, stated as a number so it does not drift — nav and main become
`display: none` and the rail takes the full width: mic, transcript and the
pending prompt card. Above it, the full shell; the rail's 380px and the nav's
200px leave ~320px of main at the breakpoint, which is why the cutover is
there rather than lower. Desktop-first by decision; the narrow case is a
deliberate reduction, not a stub.

## 3. Decomposing `VoiceAgent.tsx`

2,129 lines, of which lines 1–435 are helpers, types and constants and line
436 opens a single 1,693-line component. It cannot host eight more views, so
the split happens first — and it is the step that can be verified mechanically
before any new UI exists.

**Pure functions to `lib/` — the main testability win.** Twelve are already
pure and untestable *only because they live in a component file*:
`splitPlan`, `toolSummary`, `toolState`, `sessionStatus`, `abbrevHome`,
`fmtLogTime`, `fmtLogTimeTitle`, `orbCaption`, `connectionParams`, `clip`,
`isFlatObject`, `fmtPayload`. They move to `lib/timeline.ts`,
`lib/sessions.ts` and `lib/format.ts`, where the existing `node --test`
harness reaches them. Types and label maps go with them.

**Presentational components come out unchanged.** `MarkdownLite`,
`PayloadView`, `ToolCall`, `renderConversation` → `components/conversation/`;
`CopyBtn` → `components/ui/`; the session-card JSX →
`components/SessionCard.tsx`; the debug panel → `components/ActivityFeed.tsx`.

**Move, do not rewrite.** `renderConversation` and the injection queue hold
behaviour that was expensive to get right: the blocking-item rule that must
never evict a pending ask, and the id dedupe that stops replayed history being
re-narrated. A rewrite would lose them quietly.

**`VoiceProvider`** owns the realtime connection, the transcript, the
injection queue and gate, both SSE subscriptions, and tool dispatch. Exposed
as `useYuri()`.

**The provider/view boundary, stated explicitly:** the provider owns
global-and-continuous state plus only the counts the nav badges need. Each
view fetches its own detail on mount. Without that line the context becomes a
god-object every view re-renders on, which is what makes shells like this
sluggish.

Target: no file over ~300 lines.

## 4. Data flow

**Two streams, each owned once, in the provider — never one per view.**
`/debug/stream?limit=300` (deduped by monotonic seq) and the Yuri events
stream (deduped by id). The reason is concrete: `_clamp_limit` is
`max(1, min(limit, MAX))`, so a subscriber cannot opt out of the replay by
asking for `limit=0`. Every new subscription re-delivers history — the bug
that made Yuri re-narrate old turns on reconnect in Phase 4. Eight
subscribing views would reintroduce it eight times. Both dedupes move across
unchanged.

**Views refresh on events, not on timers.** A view mounts, fetches, and
subscribes to the event types that invalidate it: a mission view on
`mission.status_changed`, Approvals on `approval.requested` /
`approval.resolved`. Event-driven invalidation, so open tabs are not polling
loops.

**One deliberate exception:** the session list keeps its 2.5s poll. It carries
live turn state (running, queued, current task) the event stream does not
fully describe, and it works today. A proven loop beats an event mapping that
has to be right first.

**`lib/api.ts`** is a thin typed wrapper over the existing
`/api/yuri/[...path]` proxy — one home for auth headers and error shape, so no
view hand-rolls `fetch`. It covers REST only: both SSE streams already connect
straight to `backendBase()` with the token as a query param, because
`EventSource` cannot send headers. Do not route them through the proxy.

**Actions keep no local truth.** Approve, deny, pause, resume, cancel,
interrupt: call the endpoint, let the resulting event update state. The button
disables in flight and re-enables on the event or an error. No optimistic
mutation of a list the backend also owns — the backend holds the
one-pending-approval invariant and the mission state machine, and two sources
of truth for those would drift.

## 5. The views

- **Dashboard** — three bands: pending approvals (with risk label and inline
  allow/deny), then blocked or failed (missions `waiting_for_approval` or
  `failed`, sessions `lost`), then what is running. The empty state is the good
  state and says so, rather than showing three empty headings.
- **Approvals** — the same cards with full context: tool name, the actual
  arguments, risk label, session and mission. The one view that shows
  `tool_input` in full rather than a summary, since it is the whole basis for
  deciding — and what the risk fix made trustworthy.
- **Missions** — list with status, project, agent, cost, and the
  pause/resume/cancel the API exposes; detail shows steps and sessions. **No
  create button**: missions are created by starting a session, and Phase 4
  ruled out an orchestrator, so a create form would imply a queue that does
  not exist.
- **Sessions** — today's cards with room: status, agent, model, cwd, cost,
  mode switcher where the provider has modes, transcript, and the terminal link
  where `can_watch` is true.
- **Agents** — one card per provider: health, version, the full `capabilities`
  dict `/agents` already returns, and active session count. Where "Claude Code
  is one provider among several" becomes visible rather than only tested.
- **Projects** — list with session and mission counts, plus a create form
  (`POST /projects` exists).
- **Terminal** — the existing `LiveTerminal` at full width, with a picker over
  sessions where `can_watch` is true.
- **Activity** — the debug feed with its existing filter, full width.

**Scope decision on session actions.** `/yuri/*` has `interrupt` but no close
or send. Rather than add backend surface, those two go through
`/api/tools/execute`, which is what the UI already does for `read_transcript`.
It is the voice-tool surface, which is slightly odd, but it is the established
pattern, needs no backend work, and keeps this phase frontend-only. Promoting
them to real `/yuri/*` routes later is a small separate change.

## 6. Testing

New tests come from the extracted pure functions, which currently have none,
plus the new selector logic: grouping approvals by risk, deriving nav badge
counts, ordering the Dashboard's bands. All reachable by the existing
`node --test` harness over `lib/*.test.ts`.

Components are verified by `tsc --noEmit` and by looking at them in a real
browser. **No jsdom or React Testing Library** — adding a component-test
framework is its own project and would balloon this phase. Testability comes
instead from keeping logic in `lib/` and components thin, which is why the
extraction in §3 is the foundation and not a tidy-up.

Acceptance for every extraction step: `tsc` clean, `npm test` green, and the
current screen renders identically. If the output differs, the move was wrong.

## 7. Error handling

Per region. A view whose fetch fails renders its own error with a retry; nav
and rail keep working. The rail survives a backend restart unaided —
`EventSource` reconnects and both dedupes make the replay harmless. An auth
failure is distinguished from a network failure, because they need different
actions from the user.

Nothing swallows an error into an empty list: an empty list and a failed fetch
look identical on screen, and only one of them means "nothing to do".

## 8. Out of scope

No mission creation, no cost dashboards, no multi-agent orchestration UI (that
is Phase 7, and would be designed against features that do not exist yet), no
component-test framework, no design-system change, no phone views beyond the
rail fallback, and no new backend endpoints.

## 9. Rollout order, which is the risk order

1. **Extraction**, verified green after each move, current screen unchanged.
2. **The shell** — nav, rail, eight empty routes.
3. **Views, most valuable first**: Approvals, Dashboard, Sessions, Missions,
   Agents, Terminal, Activity, Projects.

Each view is independently shippable. Stopping after Approvals leaves a real
improvement rather than a half-migration.
