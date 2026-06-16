// Orb gate-timing tests. Run with Node's built-in runner (no deps):
//     node --test lib/orb.test.ts     # from frontend/

import { test } from "node:test";
import assert from "node:assert/strict";
import { rmsAmp, orbTarget, envelopeStep } from "./orb.ts";

// A buffer pegged at 128 is pure silence (the byte time-domain midpoint).
const SILENCE = new Uint8Array(256).fill(128);
// A loud buffer alternating between the extremes — well above the gate.
const LOUD = Uint8Array.from({ length: 256 }, (_, i) => (i % 2 ? 255 : 0));

test("silence reads as zero amplitude", () => {
  assert.equal(rmsAmp(SILENCE), 0);
});

test("loud input produces a non-zero amplitude (clamped to 1)", () => {
  const amp = rmsAmp(LOUD);
  assert.ok(amp > 0);
  assert.ok(amp <= 1);
});

test("CORE GATE: a closed gate pins the target to 0 even with loud input", () => {
  // This is the bug fix: before `ready`, the mic analyser is already live but
  // the orb must not scale. The gate (readyRef) keeps the target at 0.
  assert.equal(orbTarget(false, [LOUD, LOUD]), 0);
});

test("an open gate scales from the loudest analyser", () => {
  // Loudest source wins: silence + loud => loud's amplitude.
  assert.equal(orbTarget(true, [SILENCE, LOUD]), rmsAmp(LOUD));
});

test("an open gate over only silence is still 0", () => {
  assert.equal(orbTarget(true, [SILENCE]), 0);
});

test("the envelope stays at rest while the gate keeps the target at 0", () => {
  // Drive several frames with a closed gate from a non-zero starting point: the
  // envelope must decay toward 0 and never be pushed up by mic input.
  let smoothed = 0.5;
  for (let i = 0; i < 100; i++) {
    smoothed = envelopeStep(smoothed, orbTarget(false, [LOUD]));
  }
  assert.ok(smoothed < 1e-3, `expected near-0, got ${smoothed}`);
});

test("the envelope rises once the gate opens", () => {
  let smoothed = 0;
  const target = orbTarget(true, [LOUD]);
  for (let i = 0; i < 20; i++) smoothed = envelopeStep(smoothed, target);
  assert.ok(smoothed > 0.1, `expected the orb to rise, got ${smoothed}`);
});
