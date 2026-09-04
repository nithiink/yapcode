import { strict as assert } from "node:assert";
import { test } from "node:test";
import { CORNER, DOCK_W, ORB_HUE, ORB_HUE_WAITING, drift, look, persistence, orbState, pointCount, sphere, step, target } from "./orb.ts";

test("centre target scales with the viewport, the corner does not", () => {
  const small = target(1000, 800, true);
  const large = target(2400, 1400, true);
  assert.equal(small.r, large.r, "corner radius grew with the window");
  assert.equal(small.y, large.y);
  assert.equal(small.x, 1000 - CORNER.dx);
  assert.equal(large.x, 2400 - CORNER.dx);

  assert.ok(target(2400, 1400, false).r > target(1000, 800, false).r,
    "centre radius should track the viewport");
});

test("the corner is far smaller than centre, so yielding the stage is visible", () => {
  assert.ok(target(1400, 900, true).r < target(1400, 900, false).r / 3);
});

test("a lerp step closes the gap without overshooting or arriving instantly", () => {
  const from = { x: 0, y: 0, r: 0 };
  const to = { x: 100, y: 200, r: 50 };
  const one = step(from, to);
  assert.ok(one.x > 0 && one.x < to.x);
  assert.ok(one.r > 0 && one.r < to.r);

  let p = from;
  for (let i = 0; i < 200; i++) p = step(p, to);
  assert.ok(Math.abs(p.x - to.x) < 0.5, "never converged");
  assert.ok(Math.abs(p.r - to.r) < 0.5);
});

test("position and radius move together", () => {
  // A radius that eases on its own schedule makes her arrive still growing.
  const p = step({ x: 0, y: 0, r: 0 }, { x: 100, y: 100, r: 100 });
  assert.equal(p.x, p.r);
  assert.equal(p.y, p.r);
});

test("sphere points are unit-length and evenly spread", () => {
  const pts = sphere(500);
  assert.equal(pts.length, 500);
  for (const [x, y, z] of pts) {
    assert.ok(Math.abs(Math.hypot(x, y, z) - 1) < 1e-9, "point off the unit sphere");
  }
  // Even coverage: each hemisphere gets roughly half the points. A lat/long
  // grid would clump at the poles instead.
  const upper = pts.filter(([, y]) => y > 0).length;
  assert.ok(Math.abs(upper - 250) <= 2, `lopsided sphere: ${upper}/500 above the equator`);
});

test("sphere handles the degenerate sizes without NaN", () => {
  assert.deepEqual(sphere(0), []);
  const [p] = sphere(1);
  assert.ok(Number.isFinite(p[0]) && Number.isFinite(p[1]) && Number.isFinite(p[2]));
});

test("every state keeps her own hue", () => {
  for (const st of ["idle", "listening", "working", "speaking"] as const) {
    assert.equal(look(st, 3).hue, ORB_HUE, st);
  }
  // Waiting runs a touch warmer so "something needs you" is a temperature
  // change too, not brightness alone.
  assert.equal(look("waiting", 3).hue, ORB_HUE_WAITING);
});

test("every state moves, and no two move the same way", () => {
  // The reported bug: "there is no motion when she is speaking, it just keeps
  // spinning in all the stages". Previously only `waiting` varied at all and
  // speaking's 3.2% scale wobble was invisible on a sphere. A state whose
  // signature is identical to another's is a state the user cannot see.
  const sig = (st: Parameters<typeof look>[0]) => {
    const a = look(st, 0, 0.6), b = look(st, 40, 0.6);
    return JSON.stringify([
      a.spin, a.jitter, +(a.alpha !== b.alpha), +(a.scale !== b.scale),
      +(a.wave > 0), +(a.scale > 1), +(a.scale < 1),
    ]);
  };
  const states = ["idle", "listening", "working", "waiting", "speaking"] as const;
  const seen = new Map<string, string>();
  for (const st of states) {
    const k = sig(st);
    assert.ok(!seen.has(k), `${st} looks identical to ${seen.get(k)}`);
    seen.set(k, st);
  }
});

test("speaking is driven by her real voice, not a fixed animation", () => {
  // VoiceProvider already computes this envelope from live audio; it was
  // being written to a CSS variable on the DOM orb deleted in the re-shell,
  // so it reached nothing.
  const quiet = look("speaking", 0, 0);
  const loud = look("speaking", 0, 1);
  assert.ok(loud.scale > quiet.scale * 1.1, "loudness does not change her size");
  assert.ok(loud.alpha > quiet.alpha, "loudness does not change her brightness");
  assert.ok(loud.wave > quiet.wave, "the ripple does not track her voice");
  assert.ok(quiet.alpha > 0.8, "a quiet passage must not make her vanish mid-sentence");
});

test("listening contracts where speaking swells", () => {
  // Opposite gestures on purpose: the two states are adjacent in time and
  // must never be mistaken for one another.
  assert.ok(look("listening", 0, 0.8).scale < 1);
  assert.ok(look("speaking", 0, 0.8).scale > 1);
});

test("amplitude is clamped, so a bad reading cannot distort her", () => {
  for (const bad of [-5, 2, 99, NaN]) {
    const l = look("speaking", 0, bad as number);
    assert.ok(l.scale >= 1 && l.scale <= 1.19, `scale ${l.scale} for amp ${bad}`);
    assert.ok(l.alpha >= 0.86 && l.alpha <= 1.0001, `alpha ${l.alpha} for amp ${bad}`);
  }
});

test("only speaking ripples", () => {
  for (const st of ["idle", "listening", "working", "waiting"] as const) {
    assert.equal(look(st, 5, 1).wave, 0, st);
  }
  assert.ok(look("speaking", 5, 1).wave > 0);
});

test("working spins faster than idle", () => {
  assert.ok(look("working", 0).spin > look("idle", 0).spin);
});

test("a decision waiting on the user outranks everything else", () => {
  assert.equal(orbState("speaking", [{ running: true }], 1), "waiting");
  assert.equal(orbState("idle", [{ status: "needs_permission" }], 0), "waiting");
  assert.equal(orbState("idle", [{ status: "needs_choice" }], 0), "waiting");
});

test("state falls through speaking, then work, then idle", () => {
  assert.equal(orbState("speaking", [{ running: true }], 0), "speaking");
  // "connecting" stands in for "not speaking and not hearing anything" —
  // `listening` and `hearing` are their own state now.
  assert.equal(orbState("connecting", [{ running: true }], 0), "working");
  assert.equal(orbState("connecting", [{ status: "stopped" }], 0), "idle");
  assert.equal(orbState("idle", [], 0), "idle");
});

test("a live session with no turn in flight is idle, not working", () => {
  // status "running" is the process being up, not work happening. Reading it
  // as work made her spin whenever anything at all was open.
  assert.equal(orbState("connecting", [{ status: "running", running: false }], 0), "idle");
});

test("a modest machine or reduced motion gets the cheap cloud", () => {
  assert.ok(pointCount(false, 10) > pointCount(false, 4));
  assert.ok(pointCount(true, 10) < pointCount(false, 10));
});

test("at home she never sits behind the dock", () => {
  for (const [w, h] of [[1920, 1080], [1400, 800], [1100, 700], [950, 560], [901, 500]]) {
    const t = target(w, h, false);
    assert.ok(t.x + t.r <= w - DOCK_W - 1,
      w + "x" + h + ": right edge " + (t.x + t.r).toFixed(0) + " runs under the dock at " + (w - DOCK_W));
    assert.ok(t.x - t.r >= 0, w + "x" + h + ": she runs off the left edge");
  }
});

test("a wide stage centres her and applies the current proportions exactly", () => {
  // The clamp is a fallback for narrow windows, not a new layout: where she
  // fits, the numbers apply untouched. These were 0.45 across and 0.34 of the
  // short side; both changed deliberately — smaller, and actually centred.
  const t = target(1920, 1080, false);
  assert.equal(t.x, 1920 * 0.5);
  assert.equal(t.r, 1080 * 0.26);
  assert.equal(t.y, 1080 * 0.47);
});

test("she shrinks rather than overflowing when the stage is tight", () => {
  assert.ok(target(950, 560, false).r < target(1920, 1080, false).r);
});

test("she is brighter on the canvas than the flat accent token", () => {
  // The orb is ~1500 translucent squares with depth attenuation, so the hex
  // that reads as terracotta on a solid button reads as brown here. If someone
  // "fixes" the inconsistency by setting ORB_HUE back to --acc, this fails.
  const lum = (hex: string) => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const f = (v: number) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  };
  const ACCENT_TOKEN = "#dd8a6a";           // --acc in app/globals.css
  assert.ok(lum(ORB_HUE) > lum(ACCENT_TOKEN) * 1.15,
    `ORB_HUE ${ORB_HUE} is not meaningfully brighter than the token`);
});

test("idle still reads as at rest, not as switched off", () => {
  const idle = look("idle", 0).alpha;
  const working = look("working", 0).alpha;
  assert.ok(idle < working, "idle must stay quieter than working");
  assert.ok(idle > 0.6, `idle alpha ${idle} is dim enough to look broken`);
});

test("the user talking is visible, and outranked by anything needing them", () => {
  // She looked identical whether she was hearing the user or ignoring them,
  // which is the least reassuring thing a listening interface can do.
  assert.equal(orbState("listening", [], 0), "listening");
  assert.equal(orbState("hearing", [], 0), "listening");
  // Priority is unchanged: a decision waiting on the user still wins.
  assert.equal(orbState("listening", [], 1), "waiting");
  assert.equal(orbState("speaking", [], 0), "speaking");
});

test("drift never repeats visibly and stays small", () => {
  // A drift that loops reads as an animation, which is the opposite of the
  // point: she should look alive, not animated.
  const seen = new Set<string>();
  let maxOff = 0;
  for (let t = 0; t < 20000; t += 7) {
    const d = drift(t, 260);
    maxOff = Math.max(maxOff, Math.hypot(d.dx, d.dy));
    seen.add(`${d.dx.toFixed(1)},${d.dy.toFixed(1)}`);
  }
  assert.ok(seen.size > 2000, `drift path repeats: only ${seen.size} distinct positions`);
  // Two axes, so the diagonal maximum is ~2.26x the per-axis amplitude.
  assert.ok(maxOff < 260 * 0.07, `drift wanders ${maxOff.toFixed(0)}px — that is a move, not a breath`);
  assert.ok(maxOff > 260 * 0.02, `drift of ${maxOff.toFixed(0)}px is too small to notice at all`);
});

test("drift scales with her size, so the corner sways as gently as the centre", () => {
  // A fixed pixel offset that is a sway at 260px is a twitch at 54.
  const big = drift(500, 260), small = drift(500, 54);
  assert.ok(Math.hypot(big.dx, big.dy) > Math.hypot(small.dx, small.dy) * 3);
});

test("every state holds some trail, and waiting holds the least", () => {
  // Fading instead of clearing is what gives motion a wake. The pulse that
  // exists to catch the eye needs crisp edges, so it keeps the least.
  const states = ["idle", "listening", "working", "waiting", "speaking"] as const;
  for (const s of states) {
    const p = persistence(s);
    assert.ok(p > 0 && p < 1, `${s}: ${p}`);
  }
  assert.equal(Math.min(...states.map(persistence)), persistence("waiting"));
  assert.equal(Math.max(...states.map(persistence)), persistence("speaking"));
});

test("she is smaller than she was, and centred where she fits", () => {
  // 0.34 of the short side forced her permanently left of centre to clear the
  // dock; 0.26 lets her actually sit in the middle.
  const wide = target(1944, 1000, false);
  assert.equal(wide.x, 972, "not centred on a wide stage");
  assert.ok(wide.r < 1000 * 0.28, `radius ${wide.r} is not smaller`);
  // Narrow: visible and off-centre beats symmetrical and hidden.
  const narrow = target(1000, 650, false);
  assert.ok(narrow.x < 500, "should shift left rather than sit under the dock");
  assert.ok(narrow.x + narrow.r <= 1000 - DOCK_W - 1);
});
