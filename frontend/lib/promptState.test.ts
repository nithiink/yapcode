// Multi-session safeguard tests. Run with Node's built-in runner (no deps):
//     node --test lib/promptState.test.ts     # from frontend/

import { test } from "node:test";
import assert from "node:assert/strict";
import { scopedClearPending } from "./promptState.ts";

const cardA = { sessionId: "A", kind: "permission", text: "run rm", options: ["allow", "deny"] };
const cardB = { sessionId: "B", kind: "permission", text: "run ls", options: ["allow", "deny"] };

test("a result for the SAME session clears its own card", () => {
  assert.equal(scopedClearPending(cardA, "A"), null);
});

test("a result for a DIFFERENT session leaves the card intact", () => {
  // Core safeguard: B completing must not dismiss A's on-screen prompt.
  assert.equal(scopedClearPending(cardA, "B"), cardA);
});

test("a missing sessionId never clears blindly", () => {
  assert.equal(scopedClearPending(cardA, undefined), cardA);
  assert.equal(scopedClearPending(cardA, ""), cardA);
  assert.equal(scopedClearPending(cardA, null), cardA);
});

test("no card on screen is a no-op for any session", () => {
  assert.equal(scopedClearPending(null, "A"), null);
  assert.equal(scopedClearPending(null, undefined), null);
});

test("identity is preserved (returns the same object, not a copy)", () => {
  // The card stays referentially stable so React doesn't re-raise it.
  assert.strictEqual(scopedClearPending(cardB, "A"), cardB);
});
