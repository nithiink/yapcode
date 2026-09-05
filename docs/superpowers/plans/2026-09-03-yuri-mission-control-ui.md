# Yuri Phase 6 — Mission Control UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Yuri's single voice-first screen into an eight-view application shell with voice always present.

**Architecture:** `app/layout.tsx` becomes a three-region grid — nav, routed `{children}`, and a persistent voice rail. The rail and its `VoiceProvider` live in the layout, *above* the routed children, because a layout survives route changes and a route does not: the voice connection and both SSE subscriptions must never remount. `VoiceAgent.tsx` (2,129 lines) is decomposed first, mechanically, before any new UI exists.

**Tech Stack:** Next 16.2.6 (App Router), React 19.2.6, TypeScript 5.6.3, `node --test` for tests, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-03-yuri-mission-control-ui-design.md`

## Global Constraints

- **No new dependencies.** Nothing added to `frontend/package.json`.
- **No new backend endpoints.** The backend already exposes 21 `/yuri/*` routes. Session close/send go through `/api/tools/execute`, which the UI already uses for `read_transcript`.
- **Tests are `node --test lib/*.test.ts`.** Tests only run if they live in `frontend/lib/` and end in `.test.ts`. Imports carry the `.ts` extension (`from "./narration.ts"`), matching `lib/narration.test.ts`.
- **No jsdom, no React Testing Library.** Components are verified by `npx tsc --noEmit` and by looking at them in a browser. Testability comes from logic living in `lib/`.
- **Two SSE streams, each owned once, in `VoiceProvider` — never one per view.** A subscriber cannot opt out of the replay (`_clamp_limit` is `max(1, min(limit, MAX))`), and that replay is what re-narrated old turns in Phase 4. Both existing dedupes (monotonic `seq` for `/debug/stream`, id-based gate for Yuri events) move across unchanged.
- **SSE connects straight to `backendBase()`** with the token as a query param, because `EventSource` cannot send headers. `lib/api.ts` is REST-only; do not route streams through the proxy.
- **Actions keep no local truth.** Call the endpoint, let the resulting event update state. The button disables in flight.
- **Existing CSS language.** Tokens in `app/globals.css` (`--bg --panel --panel2 --ink --mut --dim --acc --line --line2 --good --warn --danger --plan`), Anton via `--disp` for headings, Archivo via `--body`. Every grid/flex track gets `min-width: 0` (see the comment at `globals.css:175`).
- **One breakpoint: `max-width: 900px`.** Below it, nav and main are `display: none` and the rail is full width.
- **Baseline to keep green:** 604 backend tests, 33 frontend tests, `npx tsc --noEmit` clean.
- **Acceptance for every extraction step (Tasks 1–3):** `npx tsc --noEmit` clean, `npm test` green, and the current screen renders *identically* in a browser. If the rendered output differs, the move was wrong.

## File Structure

**New `lib/` modules (all pure, all testable):**
| File | Responsibility |
|---|---|
| `lib/timeline.ts` | `TimelineItem`, `ToolItem` types; `splitPlan`, `toolState`, `toolSummary`, `isFlatObject`, `fmtPayload` |
| `lib/format.ts` | `clip`, `abbrevHome`, `fmtLogTime`, `fmtLogTimeTitle` |
| `lib/sessions.ts` | `Sess` type, `sessionStatus`, `MODES`, `MODE_LABEL`, `BACKEND_LABEL` |
| `lib/voiceui.ts` | `orbCaption`, `connectionParams`, `MODEL_OPTIONS`, `PROVIDER_LABEL`, `NARRATION_LABEL` |
| `lib/yuriTypes.ts` | `Approval`, `Mission`, `Project`, `Agent`, `YuriEvent` response types |
| `lib/api.ts` | typed REST wrapper over `/api/yuri/[...path]` |
| `lib/dashboard.ts` | the three Dashboard bands + nav badge counts |

**New components:**
| File | Responsibility |
|---|---|
| `components/VoiceProvider.tsx` | connection, transcript, injection queue, both SSE streams, tool dispatch, `useYuri()` |
| `components/conversation/Timeline.tsx` | `renderConversation` and its row components |
| `components/conversation/MarkdownLite.tsx` | markdown-lite renderer |
| `components/conversation/ToolCall.tsx` | tool call row + `PayloadView` |
| `components/ConversationRail.tsx` | the rail: orb, controls, transcript, prompt card |
| `components/SessionCard.tsx` | one session's card |
| `components/ApprovalCard.tsx` | one approval, shared by Dashboard and Approvals |
| `components/ActivityFeed.tsx` | the debug feed and its filter |
| `components/ui/CopyBtn.tsx` | copy-to-clipboard button |
| `components/shell/Nav.tsx` | left nav with state badges |

**New routes:** `app/{missions,projects,agents,sessions,approvals,terminal,activity}/page.tsx`; `app/page.tsx` becomes the Dashboard; `app/layout.tsx` becomes the shell.

---
## Task 1: Extract the pure helpers into `lib/`, with tests

**This is the task the rest of the phase rests on.** Twelve functions are already pure and are untestable *only* because they live in a component file. Moving them puts them in reach of `node --test`, and it is what lets later components stay thin.

**Files:**
- Create: `frontend/lib/timeline.ts`, `frontend/lib/format.ts`, `frontend/lib/sessions.ts`, `frontend/lib/voiceui.ts`
- Test: `frontend/lib/timeline.test.ts`, `frontend/lib/format.test.ts`, `frontend/lib/sessions.test.ts`
- Modify: `frontend/components/VoiceAgent.tsx` (delete the moved code, import it instead)

**Interfaces — Produces** (later tasks import these exact names):
```ts
// lib/timeline.ts
// The REAL shape, verified against VoiceAgent.tsx at HEAD. An earlier draft of
// this plan sketched separate "user"/"assistant" variants; there is one "turn"
// variant carrying a role. `id` is REQUIRED on the tool variant -- it is the
// React key at VoiceAgent.tsx:168,171,173, so an optional id renders
// key="tool-undefined" and duplicates keys when there are several.
export type TimelineItem =
  | { kind: "turn"; role: "user" | "assistant"; text: string; final: boolean }
  | { kind: "tool"; id: number; name: string; ok?: boolean; args?: unknown; result?: unknown };
export type ToolItem = Extract<TimelineItem, { kind: "tool" }>;
export function splitPlan(text: string): { lead: string; plan: string | null };
export function toolState(item: ToolItem): "done" | "working" | "error";
export function toolSummary(name: string, args: unknown, result: unknown): string;
export function isFlatObject(v: unknown): v is Record<string, unknown>;
export function fmtPayload(v: unknown): string;

// lib/format.ts
export function clip(s: string, n?: number): string;      // default n = 90
export function abbrevHome(path: string): string;
export function fmtLogTime(ts: string): string;
export function fmtLogTimeTitle(ts: string): string;

// lib/sessions.ts
export type Sess = { /* exactly the type at VoiceAgent.tsx:269, moved verbatim */ };
export function sessionStatus(s: Sess): { cls: string; lead: string; task: string };
export const MODES: { id: string; label: string; title: string }[];
export const MODE_LABEL: Record<string, string>;
export const BACKEND_LABEL: Record<ClaudeBackend, string>;

// lib/voiceui.ts
export function orbCaption(connected: boolean, muted: boolean, vstate: VoiceState): string;
export function connectionParams(provider: VoiceProvider, model: string): Partial<RealtimeOptions>;
export const MODEL_OPTIONS: Record<VoiceProvider, { value: string; label: string }[]>;
export const PROVIDER_LABEL: Record<VoiceProvider, string>;
export const NARRATION_LABEL: Record<NarrationMode, { label: string; title: string }>;
```

**The exact source lines to move** (from `frontend/components/VoiceAgent.tsx`, current HEAD):

| Symbol | Line | Destination |
|---|---|---|
| `TimelineItem` type | 26 | `lib/timeline.ts` |
| `splitPlan` | 33 | `lib/timeline.ts` |
| `fmtPayload` | 87 | `lib/timeline.ts` |
| `fmtLogTime` | 100 | `lib/format.ts` |
| `fmtLogTimeTitle` | 109 | `lib/format.ts` |
| `ToolItem` type | 115 | `lib/timeline.ts` |
| `isFlatObject` | 119 | `lib/timeline.ts` |
| `clip` | 126 | `lib/format.ts` |
| `toolState` | 130 | `lib/timeline.ts` |
| `toolSummary` | 140 | `lib/timeline.ts` |
| `Sess` type | 269 | `lib/sessions.ts` |
| `abbrevHome` | 304 | `lib/format.ts` |
| `sessionStatus` | 311 | `lib/sessions.ts` |
| `MODES`, `MODE_LABEL` | 357, 363 | `lib/sessions.ts` |
| `MODEL_OPTIONS` | 375 | `lib/voiceui.ts` |
| `connectionParams` | 389 | `lib/voiceui.ts` |
| `PROVIDER_LABEL` | 406 | `lib/voiceui.ts` |
| `BACKEND_LABEL` | 412 | `lib/sessions.ts` |
| `NARRATION_LABEL` | 419 | `lib/voiceui.ts` |
| `orbCaption` | 428 | `lib/voiceui.ts` |

**Move the bodies and their comments verbatim.** Several carry hard-won reasoning (`fmtLogTime`'s note that the backend stamps UTC and the viewer wants local; `isFlatObject`'s note about when a grid beats a code block). Losing those comments is losing the reason.

- [ ] **Step 1: Write the failing tests**

`frontend/lib/format.test.ts`:
```ts
// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { abbrevHome, clip, fmtLogTime, fmtLogTimeTitle } from "./format.ts";

test("clip leaves a short string alone", () => {
  assert.equal(clip("short"), "short");
});

test("clip truncates with an ellipsis at the given width", () => {
  assert.equal(clip("abcdefghij", 5), "abcd…");
  assert.equal(clip("abcdefghij", 5).length, 5);
});

test("clip's default width is 90", () => {
  assert.equal(clip("x".repeat(90)).length, 90);
  assert.equal(clip("x".repeat(91)).length, 90);
});

test("abbrevHome shortens a macOS home path", () => {
  assert.equal(abbrevHome("/Users/ankur/projects/yuri"), "~/projects/yuri");
});

test("abbrevHome shortens a Linux home path", () => {
  assert.equal(abbrevHome("/home/ankur/projects/yuri"), "~/projects/yuri");
});

test("abbrevHome leaves a path outside any home alone", () => {
  assert.equal(abbrevHome("/tmp/scratch"), "/tmp/scratch");
});

test("fmtLogTime renders a local clock with milliseconds", () => {
  // Built from local parts so the assertion does not depend on the runner's zone.
  const d = new Date(2026, 8, 3, 14, 5, 9, 42);
  assert.equal(fmtLogTime(d.toISOString()), "14:05:09.042");
});

test("fmtLogTime falls back to the raw UTC slice when unparseable", () => {
  assert.equal(fmtLogTime("not-a-date-at-all-xx"), "e-at-all-xx".slice(0, 12));
});

test("fmtLogTimeTitle returns the input unchanged when unparseable", () => {
  assert.equal(fmtLogTimeTitle("nonsense"), "nonsense");
});
```

`frontend/lib/timeline.test.ts`:
```ts
// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { fmtPayload, isFlatObject, splitPlan, toolState, toolSummary } from "./timeline.ts";

test("splitPlan returns the whole text as lead when there is no plan", () => {
  assert.deepEqual(splitPlan("just a reply"), { lead: "just a reply", plan: null });
});

test("isFlatObject accepts an all-primitive object", () => {
  assert.equal(isFlatObject({ a: 1, b: "x", c: true, d: null }), true);
});

test("isFlatObject rejects nesting, arrays and non-objects", () => {
  assert.equal(isFlatObject({ a: { b: 1 } }), false);
  assert.equal(isFlatObject([1, 2]), false);
  assert.equal(isFlatObject("str"), false);
  assert.equal(isFlatObject(null), false);
});

test("fmtPayload renders nullish as an em dash", () => {
  assert.equal(fmtPayload(null), "—");
  assert.equal(fmtPayload(undefined), "—");
});

test("fmtPayload passes a string through and pretty-prints an object", () => {
  assert.equal(fmtPayload("raw"), "raw");
  assert.equal(fmtPayload({ a: 1 }), '{\n  "a": 1\n}');
});

test("fmtPayload survives a circular structure", () => {
  const o: Record<string, unknown> = {};
  o.self = o;
  assert.equal(typeof fmtPayload(o), "string");   // must not throw
});

test("toolState reads error from ok:false", () => {
  assert.equal(toolState({ kind: "tool", name: "t", ok: false }), "error");
});

test("toolState reads working and error out of the result status", () => {
  assert.equal(toolState({ kind: "tool", name: "t", result: { status: "working" } }), "working");
  assert.equal(toolState({ kind: "tool", name: "t", result: { status: "error" } }), "error");
});

test("toolState defaults to done", () => {
  assert.equal(toolState({ kind: "tool", name: "t", result: { status: "idle" } }), "done");
  assert.equal(toolState({ kind: "tool", name: "t" }), "done");
});

test("toolSummary never returns an empty string for a known tool", () => {
  // The row reads as an action; an empty gloss would render a blank line.
  assert.notEqual(toolSummary("tell_claude", { message: "go" }, {}).trim(), "");
});

test("toolSummary survives junk args and results", () => {
  assert.equal(typeof toolSummary("tell_claude", null, null), "string");
  assert.equal(typeof toolSummary("unknown_tool_name", 42, "x"), "string");
});
```

`frontend/lib/sessions.test.ts`:
```ts
// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { MODES, MODE_LABEL, sessionStatus, type Sess } from "./sessions.ts";

const base: Sess = {
  handle: "h1", session_id: "s1", cwd: "/tmp/proj", model: "opus",
  status: "idle",
};

test("every mode has a label and a title", () => {
  for (const m of MODES) {
    assert.ok(m.id && m.label && m.title, `mode ${JSON.stringify(m)} is incomplete`);
    assert.equal(MODE_LABEL[m.id], m.label);
  }
});

test("an idle session reports no running task", () => {
  const st = sessionStatus(base);
  assert.ok(st.cls);
  assert.ok(st.lead);
});

test("a running turn's text becomes the task line", () => {
  const st = sessionStatus({ ...base, status: "running",
                             queue: [{ text: "fix the billing bug", state: "running" }] });
  assert.match(st.task, /billing/);
});

test("needs_permission is surfaced as its own lead", () => {
  const st = sessionStatus({ ...base, status: "needs_permission" });
  assert.notEqual(st.lead, sessionStatus(base).lead);
});
```

- [ ] **Step 2: Run the tests and watch them fail for the right reason**

```bash
cd frontend && npm test
```
Expected: FAIL — `Cannot find module './format.ts'` (and the same for `timeline.ts`, `sessions.ts`). If it fails for any other reason, stop and read why.

- [ ] **Step 3: Create the four `lib/` modules**

Move each symbol from the table above into its destination file **verbatim, with its comments**, adding `export` and `import type` lines as needed. `lib/timeline.ts`, `lib/format.ts` and `lib/sessions.ts` must have no React import — they are pure. `lib/voiceui.ts` imports its types from `./realtime.ts` and `./voice.ts` as `VoiceAgent.tsx` does today.

Give each file a one-line module docstring comment saying what it holds and why it is separate, matching the house style in `lib/narration.ts`.

- [ ] **Step 4: Run the tests**

```bash
cd frontend && npm test
```
Expected: PASS, 33 existing + the new ones.

- [ ] **Step 5: Rewire `VoiceAgent.tsx`**

Delete the moved declarations and import them instead. `VoiceAgent.tsx` should drop by roughly 400 lines and gain one import block. Nothing else in it changes.

- [ ] **Step 6: Verify nothing moved by accident**

```bash
cd frontend && npx tsc --noEmit && npm test
```
Expected: clean, green. Then start the app and confirm the screen renders **identically** — conversation rows, tool call rows, session cards, the activity feed, the mode buttons. A rendered difference means a move was wrong.

- [ ] **Step 7: Commit**

```bash
git add frontend/lib frontend/components/VoiceAgent.tsx
git commit -m "$(cat <<'EOF'
refactor(ui): move the pure helpers out of VoiceAgent into lib/

Twelve functions were already pure and untestable only because they lived in
a component file, which the frontend harness (node --test lib/*.test.ts)
cannot reach. Moved verbatim, comments included -- several carry the reason
they exist, like fmtLogTime's note that the backend stamps UTC and the viewer
wants local time.

They now have tests, including the cases that would render wrong rather than
crash: a circular payload, junk tool args, and clip's exact width.

No behaviour change; the screen renders identically.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---
## Task 2: Extract the presentational components

Still no behaviour change. This is the second half of the decomposition: the JSX comes out so that Task 3 can lift the state, and so the rail and the views can share the same pieces.

**Files:**
- Create: `frontend/components/conversation/MarkdownLite.tsx`, `frontend/components/conversation/ToolCall.tsx`, `frontend/components/conversation/Timeline.tsx`, `frontend/components/ui/CopyBtn.tsx`, `frontend/components/SessionCard.tsx`, `frontend/components/ActivityFeed.tsx`
- Modify: `frontend/components/VoiceAgent.tsx`

**Interfaces — Consumes:** everything Task 1 put in `lib/timeline.ts`, `lib/format.ts`, `lib/sessions.ts`.

**Interfaces — Produces:**
```tsx
export function MarkdownLite({ md }: { md: string }): ReactElement;
export function PayloadView({ value }: { value: unknown }): ReactElement;   // in ToolCall.tsx
export function ToolCall(props: { item: ToolItem; open: boolean; onToggle: () => void }): ReactElement;
export function Timeline({ items }: { items: TimelineItem[] }): ReactElement;
export function CopyBtn({ text }: { text: string }): ReactElement;

export function SessionCard(props: {
  s: Sess;
  open: boolean;                                  // transcript expanded
  live: boolean;                                  // this session is the one being watched
  modeBusy: boolean;
  onToggleTranscript: () => void;
  onWatch: () => void;
  onSwitchMode: (mode: string) => void;
  transcript: TimelineItem[];
}): ReactElement;

export function ActivityFeed(props: {
  events: DebugEvent[];
  filter: string;
  onFilter: (v: string) => void;
}): ReactElement;
export type DebugEvent = { /* the type at VoiceAgent.tsx:346, moved verbatim */ };
```

- [ ] **Step 1: Move `MarkdownLite`, `PayloadView`, `ToolCall`**

From `VoiceAgent.tsx` lines 45 (`MarkdownLite`), 166 (`PayloadView`), 188 (`ToolCall`). `PayloadView` lives beside `ToolCall` because it is only used there and they change together.

`ToolCall` currently reads its open/closed state from `VoiceAgent`'s local state. Lift that to the two props above (`open`, `onToggle`) rather than giving the component its own state — the parent owns which row is expanded, exactly as it does today.

- [ ] **Step 2: Move `renderConversation` into `Timeline.tsx`**

From line 234. It becomes a component wrapping the same loop:
```tsx
export function Timeline({ items }: { items: TimelineItem[] }) {
  return <>{renderConversation(items)}</>;
}
```
Keep `renderConversation` itself unchanged and unexported beside it. **Do not rewrite this function.** It is where the conversation's rendering behaviour lives, and a rewrite would lose it silently.

- [ ] **Step 3: Move `CopyBtn`** (line 323) to `components/ui/CopyBtn.tsx`, beside the existing `components/ui/Icon.tsx`.

- [ ] **Step 4: Extract `SessionCard`**

The session `<div className="sess">` block (currently `VoiceAgent.tsx` ~1810–1990, inside the `sessions.map((s) => …)` call). Take the JSX as-is and replace each closed-over variable with the prop of the same name from the Produces block. The `sessions.map` in `VoiceAgent` becomes:
```tsx
{sessions.map((s) => (
  <SessionCard
    key={s.handle}
    s={s}
    open={openSession === s.handle}
    live={liveSession === s.handle}
    modeBusy={modeBusy === s.handle}
    onToggleTranscript={() => toggleTranscript(s.handle)}
    onWatch={() => setLiveSession(s.handle)}
    onSwitchMode={(m) => switchMode(s.handle, m)}
    transcript={transcript}
  />
))}
```

- [ ] **Step 5: Extract `ActivityFeed`**

The `<div className="panel debugpanel">` block (~1991 to the end of that panel), plus the `DebugEvent` type from line 346 and the filter input. The filter's `useState` stays in the parent and arrives as `filter` / `onFilter`, so the feed is presentational.

- [ ] **Step 6: Verify**

```bash
cd frontend && npx tsc --noEmit && npm test
```
Expected: clean, green (no new tests — these are components, and per the Global Constraints we do not add a component-test framework).

Then in a browser: expand a tool call row, expand a session transcript, watch a session live, switch a mode, filter the activity feed. All must behave exactly as before.

- [ ] **Step 7: Commit**

```bash
git add frontend/components
git commit -m "$(cat <<'EOF'
refactor(ui): extract the presentational components from VoiceAgent

MarkdownLite, PayloadView, ToolCall, the conversation timeline, CopyBtn, the
session card and the activity feed come out as components. Moved, not
rewritten: renderConversation in particular holds the conversation's rendering
behaviour, and a rewrite would lose it quietly.

Where a piece read the parent's local state (which tool row is open, which
session is expanded), it now takes that as a prop rather than growing its own
state -- the parent still owns it, exactly as before.

No behaviour change.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `VoiceProvider` — lift the connection, the streams and the dispatch

The last extraction task, and the one the shell depends on. After it, `VoiceAgent.tsx` is a thin composition and everything stateful is reachable from any view.

**Files:**
- Create: `frontend/components/VoiceProvider.tsx`
- Modify: `frontend/components/VoiceAgent.tsx`

**Interfaces — Produces:**
```tsx
export type YuriContext = {
  // voice
  connected: boolean; muted: boolean; vstate: VoiceState;
  provider: VoiceProvider; model: string;
  connect: () => void; disconnect: () => void; toggleMute: () => void;
  setProvider: (p: VoiceProvider) => void; setModel: (m: string) => void;

  // conversation
  timeline: TimelineItem[];
  pending: Pending;                       // the live prompt card, or null

  // shared data the nav badges and every view read
  sessions: Sess[];
  approvals: Approval[];
  missions: Mission[];
  agents: Agent[];
  narrationMode: NarrationMode;
  setNarrationMode: (m: NarrationMode) => void;

  // streams
  debugEvents: DebugEvent[];
  onYuriEvent: (fn: (ev: YuriEvent) => void) => () => void;   // subscribe; returns unsubscribe

  // actions
  callTool: (name: string, args: Record<string, unknown>) => Promise<unknown>;
  refresh: (what: "sessions" | "approvals" | "missions" | "agents") => Promise<void>;
};

export function VoiceProvider({ children }: { children: ReactNode }): ReactElement;
export function useYuri(): YuriContext;      // throws if used outside the provider
```

**The boundary, restated because it is easy to get wrong:** the provider owns global-and-continuous state — the connection, the transcript, the injection queue, both streams — plus only the shared lists the nav badges need (`sessions`, `approvals`, `missions`, `agents`). It does **not** hold a mission's steps, a project's detail, or a session's transcript. Views fetch those. Without that line the context becomes a god-object every view re-renders on.

- [ ] **Step 1: Create the provider and move the state in**

Move from `VoiceAgent.tsx`, unchanged:
- the realtime connection effects and refs (around lines 442–520)
- `esRef` (line 524) and its `/debug/stream?limit=300` subscription with its **monotonic-seq dedupe**
- `narrationEsRef` (line 528) and the Yuri events subscription with its **id-based gate**
- the injection queue calls into `lib/narration.ts` (`enqueueInjection`, `createSpokenGate`) — including the rule that a blocking item is never evicted
- `fetchSessions` and its 2.5s interval (kept deliberately — see Global Constraints)
- the tool dispatch that POSTs `/api/tools/execute`
- the duplicate-start guard, and the narration-mode read/write, which becomes
  `yget<{mode: NarrationMode}>("/narration")` and
  `yput("/narration", { mode })` once `lib/api.ts` exists in Task 4 (until then
  it keeps its current `fetch`, so this task stays independently shippable)

**Do not change any of this logic.** It is the most defect-prone code in the frontend and it currently works.

- [ ] **Step 2: Add `onYuriEvent` as the fan-out**

Views must not open their own stream. The provider keeps a `Set` of listeners and calls them from the single `narrationEsRef` handler, **after** the existing id gate:
```tsx
const listeners = useRef(new Set<(ev: YuriEvent) => void>());
const onYuriEvent = useCallback((fn: (ev: YuriEvent) => void) => {
  listeners.current.add(fn);
  return () => { listeners.current.delete(fn); };
}, []);
// inside the existing es.onmessage, after the gate has accepted the event:
listeners.current.forEach((fn) => { try { fn(ev); } catch { /* a view bug must not break the stream */ } });
```
The `try` is deliberate and mirrors `ClaudeRunner._notify` on the backend: a bug in one view's handler must not kill the stream for the others.

- [ ] **Step 3: Wrap and consume**

`VoiceAgent.tsx` keeps rendering the current three-panel screen, but reads everything from `useYuri()` instead of its own state. It is now a composition of `ConversationRail`-shaped JSX, `SessionCard` and `ActivityFeed`. `app/page.tsx` becomes:
```tsx
import VoiceAgent from "@/components/VoiceAgent";
import { VoiceProvider } from "@/components/VoiceProvider";

export default function Home() {
  return <VoiceProvider><VoiceAgent /></VoiceProvider>;
}
```
(Task 4 moves the provider up into the layout; wrapping the page here first keeps this task independently shippable.)

- [ ] **Step 4: Verify — this is the highest-risk step in the phase**

```bash
cd frontend && npx tsc --noEmit && npm test
```
Then, with the backend running, in a browser:
- Connect voice. Speak. The transcript fills.
- Start a session by voice; the session card appears.
- Trigger an approval and confirm the prompt card appears **once**, and that Yuri speaks the ask **once**.
- Reload the page and confirm she does **not** re-narrate old turns (the id gate).
- Confirm the activity feed is not duplicating rows (the seq dedupe).

Those last two are exactly the bugs this code was written to prevent, so they are the acceptance criteria.

- [ ] **Step 5: Commit**

```bash
git add frontend/components frontend/app/page.tsx
git commit -m "$(cat <<'EOF'
refactor(ui): lift the connection, streams and dispatch into VoiceProvider

Everything global and continuous now lives in one provider: the realtime
connection, the transcript, the injection queue, both SSE subscriptions and
tool dispatch. Views reach it through useYuri().

Both replay dedupes moved unchanged -- monotonic seq for /debug/stream, the id
gate for Yuri events. They are why a reload does not re-narrate old turns, and
they are the reason views must never open their own stream: a subscriber
cannot opt out of the replay, since the backend clamps limit to at least 1.

onYuriEvent fans the accepted events out to view listeners, each call guarded,
mirroring ClaudeRunner._notify: a bug in one view's handler must not kill the
stream for the others.

The provider deliberately does NOT hold per-view detail -- a mission's steps, a
project's sessions. Views fetch those, or the context becomes a god-object
every view re-renders on.

No behaviour change.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---
## Task 4: The shell — layout, nav, rail, eight routes

**Files:**
- Modify: `frontend/app/layout.tsx`, `frontend/app/page.tsx`, `frontend/app/globals.css`
- Create: `frontend/components/shell/Nav.tsx`, `frontend/components/ConversationRail.tsx`, `frontend/lib/yuriTypes.ts`, `frontend/lib/api.ts`, `frontend/lib/dashboard.ts`, `frontend/lib/dashboard.test.ts`
- Create (placeholder pages): `frontend/app/{missions,projects,agents,sessions,approvals,terminal,activity}/page.tsx`

**Interfaces — Produces:**
```ts
// lib/yuriTypes.ts — mirrors the backend dataclasses exactly
export type Approval = {
  id: string; session_id: string; agent_id: string; mission_id: string | null;
  action: string; tool_name: string; request_id: string;
  tool_input: Record<string, unknown>;
  risk: "safe" | "confirm" | "dangerous";
  description: string;
  status: "pending" | "allowed" | "denied" | "expired" | "superseded";
  requested_at: string; resolved_at: string | null;
  resolved_by: "voice" | "ui" | "api" | "mode_switch" | null;
};
export type Mission = {
  id: string; title: string; project_id: string; goal: string | null;
  status: "draft" | "queued" | "running" | "waiting_for_approval" | "paused"
        | "completed" | "failed" | "cancelled";
  priority: number; current_step: string | null;
  created_by: string; metadata: Record<string, unknown>;
  created_at: string; updated_at: string;
};
// TWO shapes, deliberately, and they differ. GET /projects returns
// ProjectService.list()'s own rows (services/projects.py:94) which include
// UNREGISTERED discovered directories and use `path`, not `root_path`.
// GET /projects/{id} and POST /projects return the dataclass.
export type ProjectRow = {
  name: string; path: string; registered: boolean;
  // ProjectService.list() (services/projects.py:108) builds only
  // {name, path, registered:false} for UNREGISTERED discovered rows -- these
  // four are genuinely absent there, not empty. Optional, or a Projects view
  // reads undefined where the type promised a string.
  id?: string; slug?: string; kind?: "user" | "home"; default_agent?: string | null;
};
export type Project = {          // the dataclass, from detail and create
  id: string; slug: string; name: string; root_path: string;
  kind: "user" | "home"; default_agent: string | null;
  auto_approve_edits: boolean; repo_url: string | null;
  created_at: string; updated_at: string;
};
export type Agent = { id: string; name: string; online: boolean; version: string | null;
                      detail: string; checked_at: string;
                      capabilities: Record<string, unknown>; active_sessions: number };
export type MissionStep = {
  id: string; mission_id: string; ordinal: number; title: string;
  agent_id: string | null;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  session_id: string | null; result: Record<string, unknown>;
};
// Verified against backend/yuri/domain/event.py:60-70. An earlier draft wrote
// `created_at` from memory -- the field is `ts` -- and omitted agent_id,
// project_id and speakable. GET /yuri/events serializes with asdict(), so
// these are the wire names.
export type YuriEvent = {
  id: string; type: string; ts: string;
  mission_id: string | null; session_id: string | null;
  agent_id: string | null; project_id: string | null;
  severity: string; speakable: boolean;
  payload: Record<string, unknown>;
};

// lib/api.ts — REST only. SSE connects straight to backendBase(); see Global Constraints.
export class ApiError extends Error { status: number; }
export function yget<T>(path: string): Promise<T>;
export function ypost<T>(path: string, body?: unknown): Promise<T>;
export function yput<T>(path: string, body?: unknown): Promise<T>;

// lib/dashboard.ts
export type Band = { needsYou: Approval[]; blocked: BlockedItem[]; running: Sess[] };
export type BlockedItem =
  | { kind: "mission"; mission: Mission }
  | { kind: "session"; session: Sess };
export function bands(a: Approval[], m: Mission[], s: Sess[]): Band;
export function navBadges(a: Approval[], m: Mission[]): { approvals: number; missions: number };
```

- [ ] **Step 1: Write the failing test for the band selectors**

`frontend/lib/dashboard.test.ts`:
```ts
// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { bands, navBadges } from "./dashboard.ts";
import type { Approval, Mission } from "./yuriTypes.ts";
import type { Sess } from "./sessions.ts";

const approval = (over: Partial<Approval> = {}): Approval => ({
  id: "a1", session_id: "s1", agent_id: "claude-code", mission_id: "m1",
  action: "run rm -rf build", tool_name: "Bash", request_id: "r1",
  tool_input: { command: "rm -rf build" }, risk: "confirm",
  description: "", status: "pending", requested_at: "2026-09-03T00:00:00Z",
  resolved_at: null, resolved_by: null, ...over,
});
const mission = (over: Partial<Mission> = {}): Mission => ({
  id: "m1", title: "Fix billing", project_id: "p1", goal: null, status: "running",
  priority: 0, current_step: null, created_by: "voice", metadata: {},
  created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z", ...over,
});
const sess = (over: Partial<Sess> = {}): Sess => ({
  handle: "h1", session_id: "s1", cwd: "/tmp", model: "opus", status: "idle", ...over,
} as Sess);

test("only pending approvals need you", () => {
  const b = bands([approval(), approval({ id: "a2", status: "allowed" })], [], []);
  assert.deepEqual(b.needsYou.map((a) => a.id), ["a1"]);
});

test("dangerous approvals come first, then confirm, then safe", () => {
  const b = bands([
    approval({ id: "safe", risk: "safe" }),
    approval({ id: "danger", risk: "dangerous" }),
    approval({ id: "conf", risk: "confirm" }),
  ], [], []);
  assert.deepEqual(b.needsYou.map((a) => a.id), ["danger", "conf", "safe"]);
});

test("within a risk level the oldest ask comes first", () => {
  // The one that has been waiting longest is the one costing you time.
  const b = bands([
    approval({ id: "new", requested_at: "2026-09-03T00:00:10Z" }),
    approval({ id: "old", requested_at: "2026-09-03T00:00:01Z" }),
  ], [], []);
  assert.deepEqual(b.needsYou.map((a) => a.id), ["old", "new"]);
});

test("failed and waiting_for_approval missions are blocked; running is not", () => {
  const b = bands([], [
    mission({ id: "ok", status: "running" }),
    mission({ id: "wait", status: "waiting_for_approval" }),
    mission({ id: "bad", status: "failed" }),
  ], []);
  assert.deepEqual(
    b.blocked.filter((x) => x.kind === "mission").map((x) => (x as { mission: Mission }).mission.id),
    ["wait", "bad"],
  );
});

test("a lost session is blocked", () => {
  const b = bands([], [], [sess({ status: "lost" })]);
  assert.equal(b.blocked.filter((x) => x.kind === "session").length, 1);
});

test("terminal mission states are not blocked", () => {
  for (const status of ["completed", "cancelled"] as const) {
    assert.equal(bands([], [mission({ status })], []).blocked.length, 0, status);
  }
});

test("running holds only sessions actually working", () => {
  const b = bands([], [], [
    sess({ handle: "a", status: "running" }),
    sess({ handle: "b", status: "idle" }),
    sess({ handle: "c", status: "lost" }),
  ]);
  assert.deepEqual(b.running.map((s) => s.handle), ["a"]);
});

test("a session cannot be both blocked and running", () => {
  const b = bands([], [], [sess({ status: "lost" })]);
  assert.equal(b.running.length, 0);
});

test("nothing anywhere yields three empty bands, not an error", () => {
  assert.deepEqual(bands([], [], []), { needsYou: [], blocked: [], running: [] });
});

test("nav badges count pending approvals and blocked missions only", () => {
  const badges = navBadges(
    [approval(), approval({ id: "a2", status: "denied" })],
    [mission({ status: "failed" }), mission({ id: "m2", status: "running" })],
  );
  assert.deepEqual(badges, { approvals: 1, missions: 1 });
});
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd frontend && npm test
```
Expected: FAIL — `Cannot find module './dashboard.ts'`.

- [ ] **Step 3: Write `lib/yuriTypes.ts`, `lib/api.ts`, `lib/dashboard.ts`**

`lib/api.ts` wraps `fetch` against `/api/yuri/<path>`, reuses the existing auth-header helper the UI already uses, throws `ApiError` carrying `status` so a view can tell 401 from 503, and parses JSON once. Keep it under 60 lines.

`lib/dashboard.ts` implements `bands` and `navBadges` to satisfy the tests. `RISK_ORDER = { dangerous: 0, confirm: 1, safe: 2 }` drives the sort; ties break on `requested_at` ascending.

- [ ] **Step 4: Run the tests**

```bash
cd frontend && npm test
```
Expected: PASS.

- [ ] **Step 5: Build the shell**

`app/layout.tsx` renders:
```tsx
<body suppressHydrationWarning>
  <VoiceProvider>
    <div className="shell">
      <Nav />
      <main className="shell-main">{children}</main>
      <ConversationRail />
    </div>
  </VoiceProvider>
</body>
```
Keep `suppressHydrationWarning` on both `<html>` and `<body>` and the existing comment explaining why — browser extensions inject attributes there.

`ConversationRail` is the conversation half of today's `VoiceAgent`: the orb and its caption, connect/mute, provider and model selectors, the narration-mode toggle, `<Timeline>`, and the pending prompt card. It reads everything from `useYuri()`.

`Nav` lists the eight routes with `next/link`, marks the active one with `usePathname()`, and renders the two counts from `navBadges(approvals, missions)`.

- [ ] **Step 6: Add the shell CSS to `globals.css`**

```css
.shell { display: grid; grid-template-columns: 200px 1fr 380px; gap: 0; min-height: 100vh; }
/* min-width: 0 on every track — see the note at the .panel rule above. Without
   it a long path or queued prompt blows out a track and resizes the layout. */
.shell > * { min-width: 0; }
.shell-main { padding: 28px 32px; overflow-y: auto; }
.shell-nav { border-right: 1px solid var(--line); padding: 24px 14px; }
.shell-rail { border-left: 1px solid var(--line); padding: 24px 18px; display: flex;
              flex-direction: column; min-height: 0; }

/* One breakpoint. Below it the rail is the whole app: mic, transcript and the
   pending prompt card — the away-from-desk case. Desktop-first by decision. */
@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  .shell-nav, .shell-main { display: none; }
  .shell-rail { border-left: 0; }
}
```

- [ ] **Step 7: Create the seven placeholder pages**

Each is the same three lines, so the shell can be navigated before the views exist:
```tsx
"use client";
export default function Page() {
  return <h2 className="viewtitle">Missions</h2>;   // title per route
}
```
Add `.viewtitle { font-family: var(--disp); text-transform: uppercase; font-size: 19px; letter-spacing: .02em; font-weight: 400; margin: 0 0 18px; }` to `globals.css`.

`app/page.tsx` becomes the Dashboard placeholder in the same shape; Task 6 fills it.

- [ ] **Step 8: Verify the crux**

```bash
cd frontend && npx tsc --noEmit && npm test
```
Then in a browser, with voice connected: **click through all eight nav items and confirm the voice session stays connected and the transcript is not cleared.** That is the whole reason the provider sits in the layout — if navigation drops the connection, the provider is in the wrong place.

Also confirm at a narrow window (<900px) that the rail fills the screen and nav/main are gone.

- [ ] **Step 9: Commit**

```bash
git add frontend/app frontend/components frontend/lib
git commit -m "$(cat <<'EOF'
feat(ui): the Mission Control shell — nav, routed views, persistent voice rail

layout.tsx is now a three-region grid: nav, routed children, voice rail. The
rail and VoiceProvider sit in the LAYOUT, above the children, which is the
structural crux rather than a preference -- a layout survives route changes and
a route does not, so navigating no longer touches the voice session or either
SSE stream. Verified by clicking all eight nav items with voice connected.

Eight real routes, so deep links and the back button work and a tab can be
parked on Approvals. Nav badges pending approvals and blocked missions, which
is what makes the "what needs me right now" dashboard hold up when you are
looking at another view.

lib/dashboard.ts holds the band selectors with tests covering what would
otherwise render wrong rather than crash: dangerous before confirm, oldest ask
first within a level, a lost session counted as blocked and not running, and
terminal mission states counted as neither.

One breakpoint at 900px: below it the rail is the whole app.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---
## Task 5: `ApprovalCard` and the Approvals view

The most valuable view, so it lands first: an unanswered approval is the only state that costs real time.

**Files:**
- Create: `frontend/components/ApprovalCard.tsx`, `frontend/lib/approvals.ts`, `frontend/lib/approvals.test.ts`
- Modify: `frontend/app/approvals/page.tsx`, `frontend/app/globals.css`

**Interfaces — Consumes:** `Approval` from `lib/yuriTypes.ts`; `ypost` from `lib/api.ts`; `useYuri()` from `components/VoiceProvider.tsx`; `isFlatObject`, `fmtPayload` from `lib/timeline.ts`.

**Interfaces — Produces:**
```ts
// lib/approvals.ts
export const RISK_LABEL: Record<Approval["risk"], string>;   // "safe" -> "Safe", etc.
export const RISK_CLASS: Record<Approval["risk"], string>;   // -> "good" | "warn" | "danger"
export function approvalTitle(a: Approval): string;          // the one-line "what is being asked"
export function waitedFor(a: Approval, now?: number): string; // "waiting 2m" — how long it has sat
```
```tsx
export function ApprovalCard(props: {
  a: Approval;
  busy: boolean;
  onDecide: (decision: "approve" | "deny") => void;
  showInput?: boolean;      // Approvals view passes true; Dashboard omits it
}): ReactElement;
```

- [ ] **Step 1: Write the failing test**

`frontend/lib/approvals.test.ts`:
```ts
// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { RISK_CLASS, RISK_LABEL, approvalTitle, waitedFor } from "./approvals.ts";
import type { Approval } from "./yuriTypes.ts";

const a = (over: Partial<Approval> = {}): Approval => ({
  id: "a1", session_id: "s1", agent_id: "claude-code", mission_id: null,
  action: "run rm -rf build", tool_name: "Bash", request_id: "r1",
  tool_input: { command: "rm -rf build" }, risk: "confirm", description: "",
  status: "pending", requested_at: "2026-09-03T00:00:00Z",
  resolved_at: null, resolved_by: null, ...over,
});

test("every risk level has a label and a token class", () => {
  for (const risk of ["safe", "confirm", "dangerous"] as const) {
    assert.ok(RISK_LABEL[risk], risk);
    assert.ok(["good", "warn", "danger"].includes(RISK_CLASS[risk]), risk);
  }
});

test("dangerous maps to the danger token, not warn", () => {
  assert.equal(RISK_CLASS.dangerous, "danger");
});

test("approvalTitle prefers the action text", () => {
  assert.match(approvalTitle(a()), /rm -rf build/);
});

test("approvalTitle falls back to the tool name when action is empty", () => {
  // An empty title would render a blank card, which is worse than a bare tool name.
  assert.match(approvalTitle(a({ action: "", description: "" })), /Bash/);
});

test("waitedFor renders seconds, minutes and hours", () => {
  const t0 = Date.parse("2026-09-03T00:00:00Z");
  assert.match(waitedFor(a(), t0 + 5_000), /5s/);
  assert.match(waitedFor(a(), t0 + 125_000), /2m/);
  assert.match(waitedFor(a(), t0 + 7_400_000), /2h/);
});

test("waitedFor never renders a negative wait", () => {
  // Clock skew between the backend and the browser must not print "-3s".
  const t0 = Date.parse("2026-09-03T00:00:00Z");
  assert.doesNotMatch(waitedFor(a(), t0 - 3_000), /-/);
});

test("waitedFor survives an unparseable timestamp", () => {
  assert.equal(typeof waitedFor(a({ requested_at: "nonsense" })), "string");
});
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd frontend && npm test
```
Expected: FAIL — `Cannot find module './approvals.ts'`.

- [ ] **Step 3: Write `lib/approvals.ts`** to satisfy those tests. `waitedFor` clamps at zero and returns `""` for an unparseable timestamp.

- [ ] **Step 4: Run the tests** → PASS.

- [ ] **Step 5: Build `ApprovalCard`**

Renders: the risk chip (`RISK_LABEL` + `RISK_CLASS` token), `approvalTitle`, the tool name, `waitedFor`, the session and mission it belongs to, and two buttons — Allow and Deny. With `showInput`, also render `tool_input`: an aligned key/value grid via `isFlatObject`, else a `fmtPayload` code block. This is the one place the arguments are shown in full, because they are the whole basis for deciding.

Both buttons take `disabled={busy}`. A `dangerous` card gets a visibly different treatment (`--danger` border) — the label is what Yuri speaks aloud, and the screen should agree with her.

- [ ] **Step 6: Build the Approvals view**

```tsx
"use client";
export default function ApprovalsPage() {
  const { approvals, refresh, onYuriEvent } = useYuri();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Refresh on the events that invalidate this view, not on a timer.
  useEffect(() => onYuriEvent((ev) => {
    if (ev.type.startsWith("approval.")) void refresh("approvals");
  }), [onYuriEvent, refresh]);

  const decide = async (id: string, decision: "approve" | "deny") => {
    setBusy(id); setError(null);
    try {
      await ypost(`/approvals/${id}/${decision}`);
      // No local mutation: the resulting event refreshes the list.
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409
        ? "That approval was already decided — someone answered it first."
        : `Could not record the decision: ${(e as Error).message}`);
    } finally { setBusy(null); }
  };
  // …render pending first, then a "recently decided" section
}
```
The 409 branch matters: the backend maps "already resolved" to 409, and answering by voice while the page is open is a normal race, not a bug. Tell the user what happened rather than showing a generic failure.

Empty state: "Nothing is waiting on you." — the good state, said plainly.

- [ ] **Step 7: Verify**

```bash
cd frontend && npx tsc --noEmit && npm test
```
In a browser, with the backend running: trigger an approval, confirm the card appears with the right risk label and the full `tool_input`; click Allow and confirm the agent proceeds; trigger another and answer it **by voice** while the page is open, confirming the card disappears without a reload and no 409 error is shown.

- [ ] **Step 8: Commit**

```bash
git add frontend/lib frontend/components/ApprovalCard.tsx frontend/app/approvals frontend/app/globals.css
git commit -m "$(cat <<'EOF'
feat(ui): the Approvals view, and the card the Dashboard will share

The most valuable view lands first: an unanswered approval is the only state
that costs real time. The card shows tool_input IN FULL -- the one place it is
not summarised -- because those arguments are the whole basis for deciding, and
the risk fix earlier today is what made that label trustworthy.

Answering does not mutate the list locally; the resulting event refreshes it,
so the page cannot disagree with the backend that holds the
one-pending-approval invariant. A 409 is reported as what it is -- someone
answered it first, which is normal when you answer by voice with the page open
-- rather than as a generic failure.

lib/approvals.ts is tested for the cases that render wrong rather than crash:
dangerous mapping to --danger and not --warn, an empty action falling back to
the tool name instead of a blank card, and a negative wait from clock skew
never printing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: The Dashboard

**Files:**
- Modify: `frontend/app/page.tsx`, `frontend/app/globals.css`

**Interfaces — Consumes:** `bands` from `lib/dashboard.ts`; `ApprovalCard`; `sessionStatus` from `lib/sessions.ts`; `useYuri()`.

- [ ] **Step 1: Build the three bands**

`bands(approvals, missions, sessions)` is already written and tested in Task 4. The page renders its three fields in order:

1. **Needs you** — `ApprovalCard` per item, without `showInput` (the summary is enough here; the Approvals view is one click away for the full arguments).
2. **Blocked** — one row per `BlockedItem`. A blocked mission shows its title, status and the pause/resume/cancel it allows. A lost session shows its name and that it did not survive a restart.
3. **Running** — one compact row per session: name, agent, and `sessionStatus(s).task`.

- [ ] **Step 2: Get the empty state right**

When all three bands are empty, render **one** line — "Nothing needs you. Nothing is blocked." — and not three empty headings. A band with nothing in it is omitted entirely rather than rendered with a zero.

- [ ] **Step 3: Refresh on events**

```tsx
useEffect(() => onYuriEvent((ev) => {
  if (ev.type.startsWith("approval.")) void refresh("approvals");
  if (ev.type.startsWith("mission.")) void refresh("missions");
}), [onYuriEvent, refresh]);
```
Sessions already refresh on the provider's 2.5s poll, so this view does not add one.

- [ ] **Step 4: Verify**

```bash
cd frontend && npx tsc --noEmit && npm test
```
In a browser: with nothing running, confirm the single empty line. Start a session, confirm it appears under Running. Trigger an approval, confirm it jumps to the top under Needs you. Stop the backend and confirm the page shows an error rather than an empty dashboard that looks like "nothing to do".

That last one is the point of the "nothing swallows an error into an empty list" rule — an empty list and a failed fetch look identical, and only one means you can stop worrying.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "$(cat <<'EOF'
feat(ui): the Dashboard — what needs me right now

Three bands in the order that matters: unanswered approvals, then blocked or
failed work, then what is running. Built around the governing fact of the
system, which is that agents stop and wait for a human.

The empty state is the good state and says so in one line, rather than three
empty headings with zeros. And a failed fetch renders an error, never an empty
dashboard -- an empty list and a failed load look identical on screen, and only
one of them means you can stop worrying.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---
## Task 7: The Sessions and Terminal views

**Files:**
- Modify: `frontend/app/sessions/page.tsx`, `frontend/app/terminal/page.tsx`, `frontend/app/globals.css`

**Interfaces — Consumes:** `SessionCard` (Task 2); `LiveTerminal` (existing, `components/LiveTerminal.tsx`); `useYuri()`; `callTool` for the two actions `/yuri/*` does not cover.

- [ ] **Step 1: The Sessions view**

Renders `SessionCard` per session with room to breathe — a single column at the shell's main width rather than today's half-panel. Each card already shows status, agent, model, cwd, cost, the mode switcher where `supports_modes`, the transcript toggle, and the terminal link where `can_watch`. All of that landed in Phase 5.

Two actions need `callTool` rather than `/yuri/*`, per the Global Constraints:
```tsx
const close = (handle: string) => callTool("close_session", { session_id: handle });
const send  = (handle: string, message: string) => callTool("tell_claude", { session_id: handle, message });
```
Interrupt uses the real route: `ypost(`/sessions/${handle}/interrupt`)`.

Add a message box per card so a session can be driven by typing as well as by voice. Disable it while a turn is running — the backend queues, but a queued turn the user cannot see is confusing.

- [ ] **Step 2: The Terminal view**

A session picker across sessions where `can_watch` is true, and `<LiveTerminal handle={selected} />` filling the rest. When nothing is watchable, say why rather than showing an empty frame:

> "No session has a live terminal. Claude Code's CLI backend does; its SDK backend and OpenCode do not."

That sentence is accurate as of Phase 5 and is worth stating, because "no terminal" otherwise looks like a bug.

Default the picker to the only watchable session when there is exactly one, and to none when there are several — picking one arbitrarily would attach you to a session you did not choose.

- [ ] **Step 3: Verify**

```bash
cd frontend && npx tsc --noEmit && npm test
```
In a browser: start a CLI-backend Claude session, confirm it appears in the Terminal picker and streams; confirm an OpenCode session does **not** appear there and its card shows no Watch live button. Type a message into a session card and confirm the agent receives it. Interrupt a running turn.

- [ ] **Step 4: Commit**

```bash
git add frontend/app/sessions frontend/app/terminal frontend/app/globals.css
git commit -m "$(cat <<'EOF'
feat(ui): the Sessions and Terminal views

Sessions gets the cards a full-width column instead of half a panel, plus a
message box so a session can be driven by typing as well as by voice. Close and
send go through /api/tools/execute, which is what the UI already does for
read_transcript; interrupt uses the real route.

Terminal gives LiveTerminal a proper home with a picker over the sessions where
can_watch is true. When nothing is watchable it says WHY -- Claude Code's CLI
backend has a terminal, its SDK backend and OpenCode do not -- because "no
terminal" otherwise reads as a bug. With several watchable sessions the picker
defaults to none rather than attaching you to one you did not choose.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: The Missions view

**Files:**
- Create: `frontend/app/missions/[id]/page.tsx`, `frontend/lib/missions.ts`, `frontend/lib/missions.test.ts`
- Modify: `frontend/app/missions/page.tsx`

**Interfaces — Produces:**
```ts
// lib/missions.ts
export const MISSION_CLASS: Record<Mission["status"], string>;  // token class per status
export function canPause(m: Mission): boolean;
export function canResume(m: Mission): boolean;
export function canCancel(m: Mission): boolean;
```

These mirror the backend's transition table (`backend/yuri/domain/mission.py:29`) so a button is only offered when the transition is legal. Offering one the backend refuses with a 409 is a worse experience than not offering it.

- [ ] **Step 1: Write the failing test**

`frontend/lib/missions.test.ts`:
```ts
// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { MISSION_CLASS, canCancel, canPause, canResume } from "./missions.ts";
import type { Mission } from "./yuriTypes.ts";

const m = (status: Mission["status"]): Mission => ({
  id: "m1", title: "t", project_id: "p1", goal: null, status, priority: 0,
  current_step: null, created_by: "voice", metadata: {},
  created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
});

test("every status has a token class", () => {
  for (const s of ["draft", "queued", "running", "waiting_for_approval", "paused",
                   "completed", "failed", "cancelled"] as const) {
    assert.ok(MISSION_CLASS[s], s);
  }
});

// These mirror backend/yuri/domain/mission.py's TRANSITIONS table. Offering a
// control the backend refuses with a 409 is worse than not offering it.
test("pause is offered only from running and waiting_for_approval", () => {
  assert.equal(canPause(m("running")), true);
  assert.equal(canPause(m("waiting_for_approval")), true);
  assert.equal(canPause(m("paused")), false);
  assert.equal(canPause(m("draft")), false);
});

test("resume is offered only from paused", () => {
  assert.equal(canResume(m("paused")), true);
  assert.equal(canResume(m("running")), false);
});

test("cancel is offered from every non-terminal status", () => {
  for (const s of ["draft", "queued", "running", "waiting_for_approval", "paused"] as const) {
    assert.equal(canCancel(m(s)), true, s);
  }
});

test("no control is offered on a terminal mission", () => {
  for (const s of ["completed", "failed", "cancelled"] as const) {
    assert.equal(canPause(m(s)), false, s);
    assert.equal(canResume(m(s)), false, s);
    assert.equal(canCancel(m(s)), false, s);
  }
});
```

- [ ] **Step 2: Run it and watch it fail** → `Cannot find module './missions.ts'`.

- [ ] **Step 3: Write `lib/missions.ts`** against the backend table: `running → {waiting_for_approval, paused, completed, failed, cancelled}`, `waiting_for_approval → {running, paused, failed, cancelled}`, `paused → {running, cancelled}`, `draft → {queued, running, cancelled}`, `queued → {running, cancelled}`, and the three terminal states → `{}`.

- [ ] **Step 4: Run the tests** → PASS.

- [ ] **Step 5: The list view**

A row per mission: title, status chip via `MISSION_CLASS`, the project (resolve
`project_id` against the `ProjectRow[]` list — `Mission` carries only the id, so
a name needs that lookup and falls back to the id when the project row is gone),
`current_step`, and the controls `canPause`/`canResume`/`canCancel` allow. Clicking a row goes to `/missions/<id>`. Refresh on `mission.*` events. **No create button** — see the spec: missions are created by starting a session, and a create form would imply a queue that does not exist.

- [ ] **Step 6: The detail view**

Fetch through `lib/api.ts`, never a hand-rolled `fetch` — one home for the auth
header and the error shape:
`MissionService.detail()` returns five keys, not two — verified against
`backend/yuri/services/missions.py:166`:
```tsx
type MissionDetail = {
  mission: Mission; steps: MissionStep[]; sessions: Sess[];
  approvals: Approval[];          // this mission's, oldest first
  events: YuriEvent[];            // its last 50
};
const [detail, setDetail] = useState<MissionDetail | null>(null);
const [err, setErr] = useState<unknown>(null);
const load = useCallback(async () => {
  try { setDetail(await yget<MissionDetail>(`/missions/${id}`)); setErr(null); }
  catch (e) { setErr(e); }
}, [id]);
```
Render the steps ordered by `ordinal` with their status, the sessions as compact
rows linking to `/sessions`, this mission's approvals via `ApprovalCard`, and
the same controls. The `events` key gives the mission its own history without a
second request — use it rather than filtering the global feed. This is the view that fetches its own detail rather than reading it from the context — the boundary Task 3 drew.

A 404 renders "That mission no longer exists." with a link back, not a crash.

- [ ] **Step 7: Verify**

```bash
cd frontend && npx tsc --noEmit && npm test
```
In a browser: start a session to create a mission, confirm it appears; pause it and confirm Resume replaces Pause and Pause is gone; cancel it and confirm all three controls disappear. Trigger an approval and confirm the mission moves to `waiting_for_approval` without a reload.

- [ ] **Step 8: Commit**

```bash
git add frontend/lib frontend/app/missions
git commit -m "$(cat <<'EOF'
feat(ui): the Missions list and detail views

Controls are gated on the backend's own transition table
(domain/mission.py:29), mirrored and tested in lib/missions.ts: pause only from
running or waiting_for_approval, resume only from paused, nothing at all on a
terminal mission. Offering a control the backend refuses with a 409 is a worse
experience than not offering it.

Detail fetches its own steps and sessions rather than reading them from the
shared context -- the provider holds only what is global and continuous, or it
becomes a god-object every view re-renders on.

No create button: missions are created by starting a session, and phase 4 ruled
out an orchestrator, so a create form would imply a queue that does not exist.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: The Agents, Projects and Activity views

Grouped because all three are read-mostly list views a reviewer would assess together.

**Files:**
- Modify: `frontend/app/agents/page.tsx`, `frontend/app/projects/page.tsx`, `frontend/app/activity/page.tsx`, `frontend/app/globals.css`

- [ ] **Step 1: Agents**

`yget<{ agents: Agent[] }>("/agents")` returns `{agents: [{id, name, online, version, detail, checked_at, capabilities, active_sessions}]}`. One card each: name, an online/offline chip, version, `detail` (which for OpenCode carries the attached/spawnable/unavailable sentence), `active_sessions`, and the `capabilities` dict as a labelled grid of ticks and crosses.

**Render `detail`, not just `online`.** It is the field that explains a state — and the reason Phase 5 had to teach `health()` that "not running yet but spawnable" is online at all.

- [ ] **Step 2: Projects**

`yget<{ projects: ProjectRow[] }>("/projects")`. Note the shape: the field is
`path`, **not** `root_path`, and the list includes **unregistered** discovered
directories alongside registered ones — so each row shows `name`,
`abbrevHome(path)`, and a Registered / Discovered chip off `registered`. A
discovered row gets a Register button that POSTs it.

**There are no session or mission counts in this response** — do not render
them. Deriving them client-side would mean cross-referencing the sessions and
missions lists, which is a real feature rather than a label; if it is wanted,
it belongs in a follow-up with a backend change, not invented here.

The create form POSTs `{path, name?, default_agent?}` — the `ProjectCreate`
body model, whose field is `path`. The backend maps a `ValueError` to **400**;
show its message verbatim, because it names the actual constraint (the path sits
outside `ALLOWED_PROJECT_ROOTS`) and no message we invent would be as useful.

- [ ] **Step 3: Activity**

`<ActivityFeed>` from Task 2, at full width, reading `debugEvents` and the filter from `useYuri()`. No new fetching: the provider already owns that stream.

- [ ] **Step 4: Verify**

```bash
cd frontend && npx tsc --noEmit && npm test
```
In a browser: confirm both agents appear with their capabilities, and that with OpenCode not running its card reads "spawnable" and shows online rather than offline. Create a project inside an allowed root; try one outside and confirm the refusal names the roots. Confirm the activity feed still filters.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/agents frontend/app/projects frontend/app/activity frontend/app/globals.css
git commit -m "$(cat <<'EOF'
feat(ui): the Agents, Projects and Activity views

Agents renders the full capabilities dict, which is where "Claude Code is one
provider among several" stops being only a test result. It shows `detail` and
not just the online boolean, because detail is the field that explains a state
-- and the reason phase 5 had to teach health() that a not-yet-running but
spawnable OpenCode is online.

Projects gets a create form against the endpoint that already exists, and
surfaces the backend's own refusal when a path sits outside
ALLOWED_PROJECT_ROOTS, because that message names the actual constraint.

Activity is the existing feed at full width, reading the stream the provider
already owns.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Whole-shell pass — errors, the narrow layout, and the dead code

The tidy-up that makes the difference between eight views and an application.

**Files:**
- Create: `frontend/components/ViewError.tsx`
- Modify: every `app/*/page.tsx`, `frontend/app/globals.css`, `frontend/components/VoiceAgent.tsx` (delete)

- [ ] **Step 1: One error component, used everywhere**

```tsx
export function ViewError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const status = error instanceof ApiError ? error.status : undefined;
  // 401 and a network failure need different actions from the user, so they
  // must not read the same.
  const msg = status === 401 ? "Not authorised — check VC_AUTH_TOKEN."
            : status === 503 ? "Yuri's storage is unavailable. The backend may still be starting."
            : `Could not load this view: ${(error as Error).message}`;
  return <div className="viewerror">{msg}<button onClick={onRetry}>Retry</button></div>;
}
```
Replace every view's ad-hoc error rendering with it. The rule from the spec: **nothing swallows an error into an empty list.**

- [ ] **Step 2: Confirm the narrow layout is genuinely usable**

At <900px the rail is the whole app. Walk it: connect voice, see the transcript, and answer an approval from the prompt card. That is the away-from-desk case and it must work, not merely render.

- [ ] **Step 3: Delete `VoiceAgent.tsx`**

By now the rail, `SessionCard` and `ActivityFeed` have taken everything it held. Delete it, and confirm `grep -rn "VoiceAgent" frontend/app frontend/components` returns nothing.

- [ ] **Step 4: Check the decomposition actually happened**

```bash
cd frontend && wc -l components/*.tsx components/**/*.tsx lib/*.ts | sort -rn | head -12
```
Expected: nothing over ~300 lines, from one file of 2,129. If a file is still large, say which and why in the report rather than leaving it unremarked.

- [ ] **Step 5: Full verification**

```bash
cd frontend && npx tsc --noEmit && npm test
cd ../backend && .venv/bin/python -m unittest discover -s tests
```
Expected: `tsc` clean, frontend green, **604 backend tests still green** — this phase touches no backend code, so a backend failure means something unexpected happened.

- [ ] **Step 6: Commit**

```bash
git add -A frontend
git commit -m "$(cat <<'EOF'
feat(ui): one error component, the narrow layout, and VoiceAgent's removal

Every view now reports failure through ViewError, which distinguishes 401 from
503 from a network error because those need different actions from the user.
No view renders a failed fetch as an empty list -- an empty list and a failed
load look identical on screen, and only one means there is nothing to do.

VoiceAgent.tsx is gone: 2,129 lines redistributed into the rail, SessionCard,
ActivityFeed, seven lib modules and eight views, none over ~300 lines.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Definition of done for the phase

- Eight routes, all reachable from the nav, all rendering real data.
- **Navigating does not touch the voice session** — the property the whole shell was arranged around.
- A reload does not re-narrate old turns; the activity feed does not duplicate rows. Both dedupes intact.
- Nothing over ~300 lines.
- `npx tsc --noEmit` clean; frontend tests green with new coverage for `timeline`, `format`, `sessions`, `dashboard`, `approvals` and `missions`; 604 backend tests untouched and green.
- Below 900px the rail alone is usable: mic, transcript, and answering an approval.
