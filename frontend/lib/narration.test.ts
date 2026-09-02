// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  NARRATION_MODES,
  NARRATION_REPLAY_LIMIT,
  createSpokenGate,
  isNarrationMode,
  narrationOf,
} from "./narration.ts";

test("a frame with a narration line yields it", () => {
  assert.equal(narrationOf({ narration: 'Starting "Fix billing".' }), 'Starting "Fix billing".');
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
  assert.equal(narrationOf({ narration: 42 }), null);
  assert.equal(narrationOf("just a string"), null);
  assert.equal(narrationOf(7), null);
});

test("the three modes are exactly the backend's", () => {
  assert.deepEqual(NARRATION_MODES, ["quiet", "normal", "verbose"]);
});

test("isNarrationMode accepts only the three modes", () => {
  // Guards the value that comes back from GET/PUT /yuri/narration.
  assert.equal(isNarrationMode("quiet"), true);
  assert.equal(isNarrationMode("normal"), true);
  assert.equal(isNarrationMode("verbose"), true);
  assert.equal(isNarrationMode("loud"), false);
  assert.equal(isNarrationMode(undefined), false);
  assert.equal(isNarrationMode(null), false);
  assert.equal(isNarrationMode(1), false);
});

// --- the stream gate ------------------------------------------------------
// /yuri/events/stream replays its newest events on every (re)connect and
// clamps `limit` to a minimum of 1, so the replay cannot be switched off.
// The gate is what stops a reconnect from re-speaking history.

test("a fresh frame speaks once; the same event redelivered stays silent", () => {
  const gate = createSpokenGate();
  assert.equal(gate.lineFor({ id: "e1", narration: "Starting." }), "Starting.");
  assert.equal(gate.lineFor({ id: "e1", narration: "Starting." }), null);
});

test("seeding an id suppresses the replay of that event", () => {
  const gate = createSpokenGate();
  gate.seed(["e1", "e2"]);
  assert.equal(gate.lineFor({ id: "e1", narration: "Old news." }), null);
  assert.equal(gate.lineFor({ id: "e3", narration: "Fresh news." }), "Fresh news.");
});

test("seed tolerates junk and an empty list", () => {
  const gate = createSpokenGate();
  gate.seed([]);
  gate.seed([undefined, null, 42, ""]);
  // Nothing was really seeded, so a real frame still speaks.
  assert.equal(gate.lineFor({ id: "e1", narration: "Fresh." }), "Fresh.");
});

test("distinct events each speak", () => {
  const gate = createSpokenGate();
  assert.equal(gate.lineFor({ id: "e1", narration: "One." }), "One.");
  assert.equal(gate.lineFor({ id: "e2", narration: "Two." }), "Two.");
});

test("a frame with no usable id still speaks (fail open, no dedupe)", () => {
  // Better to risk a repeat than to swallow a line the user needs to hear.
  const gate = createSpokenGate();
  assert.equal(gate.lineFor({ narration: "No id." }), "No id.");
  assert.equal(gate.lineFor({ narration: "No id." }), "No id.");
  assert.equal(gate.lineFor({ id: 7, narration: "Bad id." }), "Bad id.");
});

test("the gate tracks delivery, not speech: a silent frame is not re-spoken later", () => {
  // Quiet mode sends narration:null. If the mode changes and the stream
  // reconnects, that same event must not surface as news.
  const gate = createSpokenGate();
  assert.equal(gate.lineFor({ id: "e1", narration: null }), null);
  assert.equal(gate.lineFor({ id: "e1", narration: "Now audible." }), null);
});

test("a blank frame is dropped and never reaches injectUpdate", () => {
  const gate = createSpokenGate();
  assert.equal(gate.lineFor({ id: "e1" }), null);
  assert.equal(gate.lineFor(null), null);
  assert.equal(gate.lineFor("not an object"), null);
});

test("the replay limit is inside the backend's clamp and wide enough to be worth it", () => {
  // _clamp_limit is max(1, min(limit, 1000)): outside that the backend silently
  // substitutes its own number and the seed would no longer cover the replay.
  assert.ok(Number.isInteger(NARRATION_REPLAY_LIMIT));
  assert.ok(NARRATION_REPLAY_LIMIT >= 1 && NARRATION_REPLAY_LIMIT <= 1000);
  // > 1, or a reconnect narrates nothing that happened during the blip.
  assert.ok(NARRATION_REPLAY_LIMIT > 1);
});

test("a full replay is silent when the seed covered it, and the gap after it is not", () => {
  // The connect sequence: seed the newest NARRATION_REPLAY_LIMIT ids, then the
  // stream replays those same events. Nothing may be spoken. Events that
  // happened during a later blip are NOT in the seed, so they must speak —
  // that is the dividend the wider replay buys.
  const gate = createSpokenGate();
  const history = Array.from({ length: NARRATION_REPLAY_LIMIT }, (_, i) => `h${i}`);
  gate.seed(history);
  for (const id of history) {
    assert.equal(gate.lineFor({ id, narration: `stale ${id}` }), null, `${id} spoke`);
  }
  assert.equal(gate.lineFor({ id: "gap1", narration: "Missed during a blip." }), "Missed during a blip.");
  // ...and a reconnect that replays that same gap event stays silent.
  assert.equal(gate.lineFor({ id: "gap1", narration: "Missed during a blip." }), null);
});

test("the seen set is capped, and the newest ids survive eviction", () => {
  // The replayed frame is always the store's NEWEST event, so the cap can only
  // ever evict ids far older than anything the stream will replay.
  const gate = createSpokenGate(3);
  for (const id of ["a", "b", "c", "d"]) gate.lineFor({ id, narration: id });
  assert.equal(gate.lineFor({ id: "d", narration: "d" }), null); // newest kept
  assert.equal(gate.lineFor({ id: "a", narration: "a" }), "a");  // oldest evicted
});
