# Yuri OS shell — approved design direction

`shell-mockup.html` is a working mockup, not a picture of one: open it in a
browser and the orb runs, the panels open, the states switch. Approved
2026-09-04. It supersedes the shell that Phase 6 built (nav + main + right
rail), while keeping every one of Phase 6's eight views.

## The three decisions it settles

**One orb, and it is Yuri.** Not one per session, and no background
constellation. Sessions live in the dock's tabs; provider colour lives on the
tabs and the composer chip. The orb is always Yuri's terracotta (`--acc`),
shifting slightly warmer and pulsing only in the state that costs the user
time — something waiting on a decision.

**She is a persistent presence that yields the stage.** Centre and large when
nothing is engaged; glides to a fixed 54px in the top-right corner the moment
anything opens — a rail view, a session tab, the composer taking focus. She is
never hidden. Clicking her returns home, as do the Dashboard icon and the
panel's close button.

Motion is an eased lerp (7.5% toward the target per frame, position and radius
together), NOT a CSS transition. That is what makes it read as alive. The
corner size is deliberately a flat px value rather than a viewport proportion:
in the corner she is chrome, and chrome should not grow with the window.

**Views are panels over the canvas, not routes.** This follows from the point
above — if she steps aside for a feature, the feature has to appear beside her
rather than replace the space she occupies. Phase 6's eight views become the
rail's destinations, unchanged in content.

## What was deliberately NOT taken from the reference

Roughly half of the reference UI is chrome for features Yuri does not have.
Building it would produce screens that give a confident answer about nothing —
the exact failure fixed twice during Phase 6:

- a companion/bot hierarchy with delegation (Phase 4 ruled out an
  orchestrator; multi-agent missions are Phase 7)
- ~~a per-agent persona editor with colour, system prompt, skills and voice~~
  — **reversed on 2026-09-04.** Phase 7 adds exactly this: a roster of named
  specialists with a role, a system prompt, tools and a colour. The reversal is
  narrower than it reads. Yuri still has the only voice and the only
  personality the user talks to; a specialist's "persona" is a job description
  handed to a provider, not a character. See
  `docs/superpowers/specs/2026-09-04-yuri-phase-7-design.md` §3.
- learned memories with confidence scores (memory is markdown today; semantic
  memory is Phase 9)
- a 60-integration grid (there is no integration layer)

Yuri also keeps her own identity and palette rather than restyling toward the
reference's branding. Her existing `--acc` (#dd8a6a) already sits a shade off
the reference's orange, which is why the direction lands native.

## How it was actually built (2026-09-04)

**Routes stayed; they render as the panel.** The mockup switches views with
JavaScript, but the eight Phase 6 routes are kept and `{children}` mounts
inside `.vpanel`. Deep links, reload and the back button therefore all still
work, and "engaged" is *derived* from the path (`pathname !== "/"`) rather
than stored — nothing to keep in sync. The one thing the path cannot answer is
the composer or a session tab taking focus with no panel open; `Stage`'s
`touched` covers that and any navigation clears it.

**Home is what became of the Dashboard.** Its three bands of cards are one
sentence under her name, built from the same `bands()` triage
(`lib/presence.ts`), with anything needing a decision arriving in the dock.
The full lists stay one rail click away.

**The numbers live in pure modules**, under `node --test`: `lib/orb.ts`
(geometry, per-state look, her state machine), `lib/presence.ts` (the line),
`lib/dock.ts` (tab order, provider colour, the live dot).

**The orb's centre had to become responsive.** The design's 0.45 of the stage
clears the dock at 1400px but buries a third of her at ~850px, so `target()`
clamps position and radius to the space left of the dock and leaves the
design's proportions untouched wherever they fit. The naming block is
positioned from `--orb-x`, which the canvas publishes each frame — hard-coding
0.45 in the CSS is what made her name drift out from under her once the clamp
existed.

## Known cost — addressed

The orb draws up to 1,500 points per frame at centre. `pointCount()` drops to
600 on a machine reporting ≤4 cores and 500 under `prefers-reduced-motion`,
and the frame loop stops on `visibilitychange` — a hidden tab is not worth
1,500 points a frame.
