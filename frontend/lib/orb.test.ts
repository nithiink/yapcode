import { strict as assert } from "node:assert";
import { test } from "node:test";
import { CORNER, look, orbState, pointCount, sphere, step, target } from "./orb.ts";

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

test("only waiting pulses, and every state keeps her own hue", () => {
  const pulse = (s: Parameters<typeof look>[0]) =>
    look(s, 0).alpha !== look(s, 40).alpha;
  assert.ok(pulse("waiting"), "the one state that costs the user time must pulse");
  for (const s of ["idle", "working"] as const) {
    assert.ok(!pulse(s), `${s} should not pulse — it spends the signal`);
  }
  // "speaking" breathes via scale, not alpha.
  assert.ok(look("speaking", 0).scale !== look("speaking", 20).scale);
  assert.equal(look("speaking", 0).alpha, look("speaking", 40).alpha);

  for (const s of ["idle", "working", "waiting", "speaking"] as const) {
    assert.match(look(s, 3).hue, /^#d[d9]/, `${s} is not Yuri's colour`);
  }
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
  assert.equal(orbState("listening", [{ running: true }], 0), "working");
  assert.equal(orbState("listening", [{ status: "stopped" }], 0), "idle");
  assert.equal(orbState("idle", [], 0), "idle");
});

test("a live session with no turn in flight is idle, not working", () => {
  // status "running" is the process being up, not work happening. Reading it
  // as work made her spin whenever anything at all was open.
  assert.equal(orbState("listening", [{ status: "running", running: false }], 0), "idle");
});

test("a modest machine or reduced motion gets the cheap cloud", () => {
  assert.ok(pointCount(false, 10) > pointCount(false, 4));
  assert.ok(pointCount(true, 10) < pointCount(false, 10));
});
