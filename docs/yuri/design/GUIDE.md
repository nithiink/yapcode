# Yuri OS front-end design guide

This documents the system that already exists in `frontend/app/globals.css`
and `frontend/components/shell/` — it does not propose a new one. Read
`README.md` in this folder first for *why* the shell looks the way it does;
this file is the *how*, written so the next change doesn't quietly break a
rule nobody wrote down.

## Contents

1. [Tokens](#1-tokens)
2. [The shell's layout contract](#2-the-shells-layout-contract)
3. [`position: fixed` and stacking](#3-position-fixed-and-stacking)
4. [Typography](#4-typography)
5. [The panel view pattern](#5-the-panel-view-pattern)
6. [Controls](#6-controls)
7. [Empty, loading and error states](#7-empty-loading-and-error-states)
8. [Motion](#8-motion)
9. [Mistakes already made](#9-mistakes-already-made)

---

## 1. Tokens

Every colour in the app is one of the variables below, declared once on
`:root` in `globals.css`. **Rule: never write a literal colour** (`#dd8a6a`,
`rgba(221,138,106,...)`) **in a component or a new CSS rule.** If a token
doesn't say what you need, that's a sign to reconsider the design, not to
reach for a hex value — a literal colour is invisible to a future theme pass
and to anyone grepping for "everywhere `--acc` is used."

| Token | For | Notes |
|---|---|---|
| `--void` | *(reserved — not currently defined; if you need a "beneath everything" black, add it here rather than hard-coding one)* | |
| `--bg` | The page/app background, and the base tone several gradients (orb, fades) blend from. | `#1a1917` |
| `--panel` | The resting fill for cards, inputs, code blocks, chips: `.sess`, `.apr`, `.logsearch`, `.tc-code`. The "surface one step up from the void." | `#211f1d` |
| `--panel2` | One step up again — hover/open/active states of the same surfaces (`.tcall.card[open]`, `.segbtn.on`, `.modelsel option`). Also the resting fill for floating chrome: `.vpanel`, `.dock`, `.infopop`. | `#272421` |
| `--ink` | Primary text colour. | `#e9e3d8` |
| `--mut` | Secondary/muted text — labels, metadata, placeholders that still need to read clearly. | `#928c81` |
| `--dim` | Tertiary text — the quietest readable tone: timestamps, disabled-adjacent hints, rail icons at rest. | `#6f6a61` |
| `--acc` | Yuri's colour. The orb, the primary action (`.talk`), active tab/segment state, focus rings. One accent for the whole app — don't introduce a second "primary" colour. | `#dd8a6a` |
| `--acc-ink` | Text/icon colour *on top of* `--acc` fills (the talk button's label, badge numerals) — never `--ink` on an accent background, the contrast is wrong. | `#241813` |
| `--line` | The default hairline border — dividers, card outlines, input borders. | `#322f2b` |
| `--line2` | A brighter hairline for a surface that needs to stand out more: hovered borders, `.vpanel`/`.dock` frames, `.sess`/`.apr`/`.agent-card` outlines. | `#3c3833` |
| `--good` | Success/healthy semantics — completed status, "allow" actions, online chips. | `#9cc7a4` |
| `--warn` | Caution semantics — needs-attention status, permission prompts, the "auto" mode's risk note. | `#d8b07a` |
| `--danger` | Destructive/error semantics — deny actions, error status, delete buttons. | `#d98a8a` |
| `--plan` | The one mode-specific colour outside good/warn/danger/acc: Claude's "plan" permission mode and its chip/card accent. | `#93a6c9` |

Three font-role variables (see [Typography](#4-typography)):
`--mono`, `--disp`, `--body`.

**Semantic colours are for status, not decoration.** `--good`/`--warn`/
`--danger`/`--plan` each mean one specific thing (session/mission/tool-call
state, permission mode). Don't reach for `--good` because green happens to
look nice next to something — if there's no status being communicated, use
`--ink`/`--mut`/`--dim` or `--acc`.

**Transparency is composed at the call site**, not with new tokens: e.g.
`color-mix(in srgb, var(--acc) 13%, transparent)` or `rgba(156,199,164,0.16)`
for a tinted background on `--good`. This is deliberate — it keeps the token
list short and every tint traceable back to a real token instead of a new
one-off colour.

---

## 2. The shell's layout contract

```
.shell (grid: 56px rail | 1fr stage)
├── .rail            56px icon column, fixed width
└── .stage           position: relative, overflow: hidden
    ├── .orb-canvas  position: absolute; inset: 0 — the full-stage canvas
    ├── .top         position: absolute; top strip (wordmark, voice pill, clock)
    ├── .vpanel      position: absolute — EVERY route renders inside here
    ├── .naming/.hint/.orbhome   positioned relative to the orb's own target
    └── .dock        position: absolute; bottom-right — session tabs + composer
```

- **`.stage` owns positioning.** It is the one `position: relative`
  container everything else on the canvas is placed against. Nothing inside
  it should introduce its own `position: relative` wrapper "just to be safe"
  — that creates a second positioning root and makes the next person guess
  which one a child resolves against.
- **`.vpanel` owns scrolling for its view.** It is `overflow-y: auto`
  already, for every route. **A view fills its panel and never sets a fixed
  height on its own scroll area** — no `max-height: 380px`, no `height: 300px`
  cap on the thing that is the view's actual content. Let content lay out at
  its natural height and let `.vpanel` be the one scrollbar. (A *nested*
  scroll region inside a view — a per-item transcript inside one session
  card on `/sessions`, a tool-call's code block, a plan preview inside a
  permission card — is fine to cap with its own `max-height`: it's one item
  among many, not the view's whole content. The rule is about the view's
  *primary* content, not every scrollable div anywhere on the page.)
- **The dock is a fixed-size overlay, not a layout participant.** It sits at
  `bottom-right`, `z-index: 7`, sized independently of the panel. Nothing in
  `.vpanel` should assume the dock isn't there — a view's content can run
  the full panel width/height; the dock floats on top by design.
- **`min-width: 0` on grid/flex tracks is load-bearing, not decoration.**
  `.shell > *`, `.panel`, and most view containers set it. Without it, a
  flex/grid item's automatic minimum is its content's min-content size, so
  one long unbroken string (a path, a queued prompt, a shell command) blows
  the track out and resizes the whole layout around it. If you add a new
  flex/grid container that might hold long unbroken text, add `min-width: 0`
  to it too.
- **One breakpoint (900px).** Below it the shell stops being a canvas-with-
  overlays: the rail becomes a top strip, the orb and naming block disappear
  (there's no stage left to hold them), and `.vpanel`/`.dock` become normal
  stacked blocks. Don't add a second breakpoint without a reason as clear as
  "there is no room for the canvas below this width."

---

## 3. `position: fixed` and stacking

**This is the single most valuable section in this document — read it before
adding any modal, overlay, or tooltip that needs to sit above everything.**

`position: fixed` is supposed to mean "positioned against the viewport,
ignore all ancestors." That guarantee has two separate failure modes, and
Yuri OS has hit both:

### 3a. An ancestor can hijack the fixed element's *box*

If any ancestor between the fixed element and `<html>` has a computed
`transform`, `filter`, `perspective`, `will-change: transform/filter/
perspective`, or `contain: layout/paint/strict/content` other than the
default, **that ancestor becomes the fixed element's containing block**
instead of the viewport. The fixed element is then sized and positioned
relative to that ancestor's box, not the screen — `inset: 0` no longer means
"cover the screen," it means "cover that ancestor."

`.vpanel` is a real risk here: its open/close transition animates `transform`
(`translateX(-16px)` closed → `none` open). While `data-open="true"`, the
*computed* value is `none` (which does **not** trap descendants) — but that
is one line away from being wrong again the next time someone tweaks that
transition, and during the transition itself the computed value is a live
matrix, not `none`. Don't rely on reading the current CSS and confirming
it's momentarily safe.

### 3b. An ancestor can hijack the fixed element's *paint order* — even when 3a doesn't apply

This is the one that actually bit `/sessions`'s fullscreen terminal overlay,
and it's easy to miss because the element's `getBoundingClientRect()` looks
completely correct. `.vpanel` is `position: absolute` **with an explicit
`z-index: 4`** (not `auto`) — that combination creates a new **stacking
context**, independent of 3a's transform/filter rule. Every descendant of
`.vpanel`, including a `position: fixed` one, paints *inside* that stacking
context. No matter what `z-index` the fixed descendant gives itself, it can
only out-rank other things *inside* `.vpanel`'s context — it can never paint
above `.top` (`z-index: 5`), `.orbhome`/`.hint` (`6`), or `.dock` (`7`),
because those live in the *parent* stacking context and the comparison that
matters is `.vpanel`'s own `z-index: 4` against theirs. The fixed element's
own `z-index: 50` is irrelevant once it's trapped one level down.

**The fix for both is the same: portal the overlay to `document.body`.**
`components/ui/Portal.tsx` does this with `createPortal`, gated on a mounted
flag so it renders nothing during SSR (no `document` on the server) and
nothing on the very first client paint (avoids a hydration mismatch — the
gap is one frame, never visible, since these overlays only open in response
to a click). Moving the element to be a direct child of `<body>` removes it
from `.vpanel`'s subtree entirely, which closes *both* holes at once: there's
no ancestor transform to be captured by, and no ancestor stacking context to
be capped by.

**Rule: any true fullscreen/modal overlay renders through `<Portal>`.**
Don't "check whether it currently has a problem" by inspecting today's CSS —
inspect whether it's a descendant of a positioned+z-indexed ancestor at all,
because that ancestor (or one added later between it and body) can start
capturing it with no change to the overlay's own code. Diagnosing a stuck
overlay: walk `el.parentElement` up to `<body>` and log
`getComputedStyle(n).transform / filter / perspective / willChange /
contain` for 3a, but also check each ancestor's `position` + `z-index` for
3b — and confirm with `document.elementFromPoint(x, y)` at a point that
should be covered, since a correct `getBoundingClientRect()` does not rule
out a paint-order trap.

---

## 4. Typography

Three font roles, each a CSS variable so a component never names a font
directly:

- **`--disp`** (`var(--font-display), "Arial Narrow", sans-serif`) — the
  display/headline face. Used for anything that announces a name or a
  section: `.viewtitle`, `.panel h2`, `.wordmark`, session/agent/mission
  names (`.sess .name`, `.agent-name`, `.miss-title`), the orb's `.sname`.
  Always paired with `text-transform: uppercase` at these call sites — that
  pairing is the "display" look, not the font alone.
- **`--body`** (`var(--font-body), ui-sans-serif, system-ui, -apple-system,
  sans-serif`) — everything else: body text, buttons, form inputs, chips,
  metadata. This is `body`'s own `font-family`, so most elements inherit it
  for free; it only needs restating where something else (like `.panel h2`'s
  display font) would otherwise leak in via `font: inherit` — see
  `.tx-head-actions .txtoggle`'s comment in `globals.css` for exactly that
  case.
- **`--mono`** (`ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace`)
  — anything that is data rather than prose: tool names (`.tc-name`), log
  rows (`.logscroll`), transcript tool/result lines, code blocks (`.tc-code`,
  `.planmd pre/code`), timestamps (`.clock`, `.miss-event-ts`), the attach
  command (`.cmdfield code`).

---

## 5. The panel view pattern

Every route (`app/*/page.tsx`) renders the same shape, mounted inside
`<section className="vpanel">` by `components/shell/Stage.tsx`:

```tsx
export default function Page() {
  return (
    <div className="some-view">
      <h2 className="viewtitle">Section Name</h2>
      {/* the view's own content */}
    </div>
  );
}
```

- **The `<h2 className="viewtitle">` is the view's job; the close button is
  the panel's.** `Stage.tsx` renders `.vpanel-close` once, outside
  `{children}`, for every route — a view must never render its own close
  control or a second heading that duplicates what the panel already
  supplies (see [§9](#9-mistakes-already-made), the duplicated Activity
  heading).
- **The outer div's class** (`sessions-view`, `activity-view`, `miss-view`,
  ...) exists mainly to carry `min-width: 0` and any view-specific overrides
  (e.g. `.activity-view .debugpanel { margin-top: 0; }`) — it is not a new
  layout root; `.vpanel` still owns positioning and scrolling per §2.
- **Routes are real routes, not client-side view state.** `Stage.tsx`
  derives "a panel is open" from `pathname !== "/"` rather than storing it,
  so deep links, reload, and the back button all land in the right state
  for free. Don't reintroduce a `currentView` piece of state to switch
  panels — that's exactly the sync problem routing avoids.

---

## 6. Controls

Real button classes — reuse one of these rather than inventing a new button
look:

| Class | For |
|---|---|
| `.talk` / `.talk.stop` / `.talk.connecting` | The one primary voice action. |
| `.ghost` | A secondary pill action next to `.talk` (e.g. mute). |
| `.dash-btn` (`.danger` modifier) | List-row actions shared by Dashboard/Missions/Projects rows. |
| `.apr-btn.allow` / `.apr-btn.deny` | Permission-card decisions. |
| `.txtoggle` (`.primary` / `.danger` modifiers) | Compact toggle/action buttons in session cards and modal headers (Send, Interrupt, Close, Attach). |
| `.modeseg` / `.segbtn` / `.modepill` | Segmented choice controls (permission mode, voice/route pickers). |
| `.copybtn` | Icon-only copy-to-clipboard, with transient "done" state (see `CopyBtn.tsx`). |

**Rule, proven by the mission Delete flow (`app/missions/page.tsx`): a
control that would fail must not be rendered.** Resume/Pause/Cancel/Delete
each only appear when `lib/missions.ts`'s `canResume`/`canPause`/
`canCancel`/`canDelete` says the mission is actually in a state where that
action can succeed — there is no disabled-and-greyed-out Delete button on a
mission that can't be deleted; the button simply isn't there. Prefer "not
rendered" over "rendered but disabled" whenever the reason it can't be used
is a durable state fact (not a fleeting in-flight one) — a disabled button
invites "why is this disabled?" where an absent one doesn't.

**Rule: a destructive action arms before it fires.** Delete has no browser
`confirm()` dialog and no undo. The row instead tracks which mission is
"armed" (`armed === m.id`); the first click swaps the row's single `Delete`
button for `Delete for good` / `Keep`, and only the second click actually
calls `ydelete`. This keeps the confirmation in the same row (no modal
context-switch) while still requiring a deliberate second action. Any new
irreversible action (not just delete) should follow this arm-then-fire
shape rather than a native `confirm()`.

---

## 7. Empty, loading and error states

**An empty list and a failed fetch must never look the same.** If they do,
the user reads "nothing here" when the real situation is "I don't know,
the backend is unreachable" — and only one of those two means they can stop
worrying. `components/ViewError.tsx` (`ViewError`) exists specifically to be
the one failed-load state every view renders instead of silently falling
back to its own `.empty` copy:

```tsx
{loadError ? (
  <ViewError error={loadError} onRetry={() => void load()} />
) : rows.length === 0 ? (
  <div className="empty">No projects registered or discovered.</div>
) : (
  /* the real list */
)}
```

`ViewError` also names *why* by status code rather than giving one generic
"could not load" line — 401 ("check `VC_AUTH_TOKEN`") and 503 ("Yuri's
storage is unavailable, the backend may still be starting") need different
user actions than a plain network failure, so it says which. When adding a
new view's data-loading state, reuse `ViewError` rather than writing a new
error message inline — the goal is one recognizable error shape across the
app, not eight slightly different ones.

The same "don't collapse two different situations into one look" idea shows
up elsewhere: `ActivityFeed.tsx` distinguishes "the activity stream is
unreachable" from "no matching events yet" even though both render as an
`.empty` row with no data — because one means talking to the agent will
never produce anything until the backend returns, and the other just means
nothing has happened.

---

## 8. Motion

Yuri OS uses two different kinds of motion for two different reasons, and
they are not interchangeable:

- **The orb is an eased lerp, run every animation frame in JS
  (`lib/orb.ts`'s `step`, `EASE = 0.075` — 7.5% of the remaining distance
  per frame, applied to position *and* radius together), not a CSS
  transition.** This is deliberate: a CSS transition eases toward a fixed
  duration and end state, while the orb's target itself moves continuously
  (the mouse hasn't stopped, `engaged` flips, the window resizes) — an eased
  lerp keeps re-aiming at whatever the current target is, every frame,
  which is what makes it read as alive rather than as "playing a canned
  animation." If you need a UI element to visibly track a continuously
  moving target, this is the pattern — a `setInterval`/`requestAnimationFrame`
  loop computing `pos += (target - pos) * ease`, not a CSS `transition`.
- **Everything else is a CSS transition** — `.vpanel`'s open/close
  (`opacity`/`transform`, 0.4s), `.naming`'s fade, `.orb-glow`'s scale, tool-
  call reveal. These have a fixed start and end state decided once (open vs.
  closed, hover vs. not), which is exactly what CSS transitions are for and
  cheaper than driving the same thing from JS.
- **`prefers-reduced-motion: reduce` is honoured in both systems, but
  differently.** The CSS transitions are turned off directly:
  `@media (prefers-reduced-motion: reduce) { .naming, .vpanel, .orbhome,
  .hint { transition: none; } }`. The orb's JS motion can't just be "turned
  off" the same way without her vanishing or teleporting, so `pointCount()`
  instead *reduces the cost* of the animation that keeps running — 500
  points instead of up to 1500 — while `lib/orb.ts`'s `look()` also softens
  per-state jitter/pulse under the same flag. When adding motion, decide up
  front which of these two treatments applies: something with a discrete
  end state can just have its `transition` removed; a continuous animation
  needs its intensity/cost turned down instead, not an on/off switch.
- The frame loop itself is cost-aware regardless of motion preference: it
  stops on `document.visibilitychange` when the tab is hidden (`raf` is
  cancelled, not just left running against a stale canvas), and
  `pointCount()` already drops from 1500 to 600 on a machine reporting ≤4
  cores. A hidden tab or a weak machine is not worth 1500 points a frame.

---

## 9. Mistakes already made

Real bugs, each with the fix and the one-line reason, so the same shape of
mistake isn't repeated under a different view's name.

- **Activity log capped at a fixed height inside a full-height panel**
  (`.logscroll { max-height: 380px; overflow: auto; }`) — the log became a
  short box with dead space under it no matter how tall `.vpanel` actually
  was. Fixed by removing the cap and letting `.vpanel`'s own
  `overflow-y: auto` be the single scroll owner (§2) — one scrollbar, not a
  short nested one sitting inside a tall empty one. `.logscroll` keeps only
  `overflow-x: auto`, for long unwrapped log lines.
- **The fullscreen terminal/transcript overlay was trapped by `.vpanel`'s
  stacking context** (§3b) — a real bug distinct from the transform-capture
  risk (§3a) that it superficially resembles: `.tx-overlay`'s own
  `getBoundingClientRect()` correctly reported the full viewport, but
  `document.elementFromPoint()` at a point over the terminal still returned
  `.top-mid`/the dock underneath, because `.vpanel`'s explicit `z-index: 4`
  capped everything painted inside it below `.top`/`.orbhome`/`.dock`
  (`5`/`6`/`7`) regardless of the overlay's own `z-index: 50`. Fixed by
  rendering both overlay blocks in `app/sessions/page.tsx` through the new
  `<Portal>` (`components/ui/Portal.tsx`), which mounts them directly on
  `document.body`.
- **The fullscreen terminal was capped at `width: min(1000px, 100%)`** —
  wasted space either side on any screen wider than ~1000px for a CLI that
  is 220 columns and wants width, not a narrow centred column. Fixed to
  `width: min(1600px, 100%)`, keeping the overlay's existing `3vh 3vw`
  breathing room and the `94vh` height cap.
- **The Projects "Add project" form implied it creates a folder.** It only
  ever calls the same `/projects` register endpoint the list's own per-row
  Register buttons call; the backend's `resolve_project_path` requires the
  path to already exist under `ALLOWED_PROJECT_ROOTS` and raises otherwise.
  The form's only real value over a Register button is naming the project
  at the moment you register it. Fixed by adding one `.togglehint` line
  under the form saying exactly that, instead of leaving the UI to imply a
  folder-creation feature that doesn't exist.
- **A duplicated "Activity" heading.** `ActivityFeed.tsx` used to render its
  own heading inside a panel that *also* renders `.viewtitle` — two
  "Activity" headings stacked. Already fixed by removing the component's own
  heading (see the comment at the top of `ActivityFeed.tsx`'s JSX) and
  moving the shown/total count it carried up into the page's `.viewtitle`
  via `.viewcount`. The general rule this proves: a view's content must
  never re-render what its containing panel already supplies (§5) — before
  adding a heading/close button/title anywhere inside a view, check whether
  `.vpanel`/`.viewtitle` already renders one.
- **The naming block under the orb hard-coded a proportion the code owned.**
  `.naming` used to centre itself on a literal `45%`, matching the orb's own
  home-position proportion at the time. Once `lib/orb.ts`'s `target()` began
  *clamping* that position to keep the orb clear of the dock on narrower
  windows, the orb moved but the CSS constant didn't, and her name drifted
  out from under her. Fixed by having `Orb.tsx` publish the orb's actual
  home-centre x-coordinate every frame as `--orb-x` (a CSS custom property
  on `.stage`), and `.naming` centres on `calc(var(--orb-x, 45%) * 2)`
  instead of a literal number — `45%` only remains as the *fallback* for the
  first paint before the canvas has run once. The general rule: if a visual
  position is computed by JS for any reason (clamping, responsiveness,
  animation), CSS must read that value through a custom property the JS
  publishes, not restate the formula as a second, independent copy that can
  drift.
