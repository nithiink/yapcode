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
- a per-agent persona editor with colour, system prompt, skills and voice
  (Yuri's agents are *providers*, not personas — there is one persona, hers)
- learned memories with confidence scores (memory is markdown today; semantic
  memory is Phase 9)
- a 60-integration grid (there is no integration layer)

Yuri also keeps her own identity and palette rather than restyling toward the
reference's branding. Her existing `--acc` (#dd8a6a) already sits a shade off
the reference's orange, which is why the direction lands native.

## Known cost before building it for real

The orb draws 1,500 points per frame at centre. Fine on mains power; for a
laptop, drop to ~600 points and pause the loop when the tab is hidden. Cheap
to add, easy to forget.
