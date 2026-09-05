// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  NARRATION_MODES,
  NARRATION_REPLAY_LIMIT,
  MAX_PENDING_INJECTIONS,
  createSpokenGate,
  enqueueInjection,
  isBlockingNarration,
  isNarrationMode,
  narrationOf,
  replayLimitFor,
  type PendingInjection,
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

// --- hasSeen ---------------------------------------------------------------
// A separate read of the same seen-set lineFor uses, for a caller (the
// onYuriEvent fan-out) that needs to know "is this a replay?" independent of
// whether the frame has anything to say.

test("hasSeen is false for an unseen id, and true once lineFor has consumed it", () => {
  const gate = createSpokenGate();
  assert.equal(gate.hasSeen({ id: "e1", narration: "Starting." }), false);
  gate.lineFor({ id: "e1", narration: "Starting." });
  assert.equal(gate.hasSeen({ id: "e1", narration: "Starting." }), true);
});

test("hasSeen does not itself mark an id seen", () => {
  const gate = createSpokenGate();
  assert.equal(gate.hasSeen({ id: "e1", narration: "Starting." }), false);
  assert.equal(gate.hasSeen({ id: "e1", narration: "Starting." }), false); // called twice
  // lineFor still delivers it — hasSeen alone must not have consumed it.
  assert.equal(gate.lineFor({ id: "e1", narration: "Starting." }), "Starting.");
});

test("hasSeen treats a frame with no usable id as unseen", () => {
  const gate = createSpokenGate();
  assert.equal(gate.hasSeen({ narration: "No id." }), false);
  assert.equal(gate.hasSeen({ id: 7, narration: "Bad id." }), false);
  assert.equal(gate.hasSeen(null), false);
});

test("a seeded id reads as seen", () => {
  const gate = createSpokenGate();
  gate.seed(["e1"]);
  assert.equal(gate.hasSeen({ id: "e1", narration: "Old news." }), true);
  assert.equal(gate.hasSeen({ id: "e2", narration: "Fresh news." }), false);
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

test("a failed seed narrows the replay to one line, not fifty", () => {
  // The gate is what makes a replay silent. Unseeded, every replayed event is
  // spoken as news — and each line costs a full model response, so a wide
  // replay would read out minutes of history at connect. 1 is the floor the
  // backend allows (_clamp_limit is max(1, ...)), so "no replay" is impossible.
  assert.equal(replayLimitFor(true), NARRATION_REPLAY_LIMIT);
  assert.equal(replayLimitFor(false), 1);
});

const texture = (t: string): PendingInjection => ({ text: t });

test("the injection queue is bounded, dropping the oldest", () => {
  // Verbose mode publishes a tool.started per tool call while the drain fires
  // one response.create per item, so an unbounded queue falls behind for the
  // rest of the turn and narrates tool calls from minutes ago.
  const q: PendingInjection[] = [];
  for (let i = 0; i < MAX_PENDING_INJECTIONS; i++) {
    assert.equal(enqueueInjection(q, texture(`line ${i}`)), 0);
  }
  assert.equal(q.length, MAX_PENDING_INJECTIONS);
  assert.equal(enqueueInjection(q, texture("newest")), 1);
  assert.equal(q.length, MAX_PENDING_INJECTIONS);
  assert.equal(q[0].text, "line 1");                             // oldest evicted
  assert.equal(q[q.length - 1].text, "newest");                  // newest kept
});

test("the injection bound is a real ceiling under a burst", () => {
  const q: PendingInjection[] = [];
  let dropped = 0;
  for (let i = 0; i < 500; i++) dropped += enqueueInjection(q, texture(`t${i}`));
  assert.equal(q.length, MAX_PENDING_INJECTIONS);
  assert.equal(dropped, 500 - MAX_PENDING_INJECTIONS);
  assert.equal(q[q.length - 1].text, "t499");
  // A nonsense cap must not produce an empty queue that silently swallows the
  // line the caller just handed us.
  const one: PendingInjection[] = [];
  enqueueInjection(one, texture("only"), 0);
  assert.deepEqual(one, [texture("only")]);
});

test("the bound can never evict a blocking ask", () => {
  // The regression this guards: the cap was added for verbose texture, but the
  // SAME queue carries the poll's needs_permission / needs_choice line. A tool
  // burst — exactly the condition the cap exists for — could evict the ask, and
  // poll_status hands back each buffered result once, so it is never
  // re-offered. This is the frontend half of the backend's ALWAYS_SPEAK set.
  const q: PendingInjection[] = [];
  for (let i = 0; i < MAX_PENDING_INJECTIONS + 20; i++) enqueueInjection(q, texture(`tool ${i}`));
  assert.equal(q.length, MAX_PENDING_INJECTIONS);

  const ask = { text: "Claude needs permission to run rm -rf build.", blocking: true };
  const dropped = enqueueInjection(q, ask);
  assert.equal(dropped, 1, "something non-blocking must go instead");
  assert.equal(q.length, MAX_PENDING_INJECTIONS);
  assert.ok(q.includes(ask), "the ask survived");

  // Keep burying it in texture: it still survives, and only texture is shed.
  for (let i = 0; i < 100; i++) enqueueInjection(q, texture(`more ${i}`));
  assert.equal(q.length, MAX_PENDING_INJECTIONS);
  assert.ok(q.includes(ask), "the ask survived a 100-line burst");
  assert.equal(q.filter((x) => x.blocking).length, 1);
});

test("an all-blocking queue grows past the cap rather than dropping an ask", () => {
  // Nine pending permission asks is not a runaway to be trimmed — each is a
  // question the agent is stalled on. Texture is what the cap exists to shed.
  const q: PendingInjection[] = [];
  let dropped = 0;
  for (let i = 0; i < MAX_PENDING_INJECTIONS + 3; i++) {
    dropped += enqueueInjection(q, { text: `ask ${i}`, blocking: true });
  }
  assert.equal(dropped, 0);
  assert.equal(q.length, MAX_PENDING_INJECTIONS + 3);
});

test("blocking is read off the carrier, mirroring ALWAYS_SPEAK", () => {
  // Poll results carry `status`; SSE frames carry `type`.
  assert.equal(isBlockingNarration({ status: "needs_permission" }), true);
  assert.equal(isBlockingNarration({ status: "needs_choice" }), true);
  assert.equal(isBlockingNarration({ type: "approval.requested" }), true);
  assert.equal(isBlockingNarration({ type: "session.question" }), true);
  // Everything else is texture the cap may shed.
  for (const x of [{ status: "completed" }, { status: "error" }, { type: "tool.started" },
                   { type: "mission.status_changed" }, {}, null, undefined, "needs_permission",
                   { status: 7 }]) {
    assert.equal(isBlockingNarration(x), false, JSON.stringify(x) ?? String(x));
  }
});
