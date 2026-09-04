import { strict as assert } from "node:assert";
import { test } from "node:test";
import { canTypeToProvider, composerState } from "./compose.ts";
import type { ComposerInput } from "./compose.ts";

const input = (over: Partial<ComposerInput> = {}): ComposerInput => ({
  provider: "gemini",
  connected: true,
  vstate: "listening",
  sending: false,
  draft: "",
  ...over,
});

test("a connected composer with text can send, and says who it talks to", () => {
  const s = composerState(input({ draft: "how's the build?" }));
  assert.equal(s.canSend, true);
  assert.equal(s.disabled, false);
  assert.equal(s.reason, null);
  assert.match(s.placeholder, /Yuri/);
});

test("an empty draft cannot send, but the box stays usable", () => {
  const s = composerState(input({ draft: "   " }));
  assert.equal(s.canSend, false);
  assert.equal(s.disabled, false);
});

// The whole point of the fix: a box that takes text it cannot deliver is the
// original bug. Every un-sendable state must be inert AND explain itself.
test("disconnected: the box is inert and tells the user what to do", () => {
  const s = composerState(input({ connected: false, vstate: "idle", draft: "hello" }));
  assert.equal(s.canSend, false);
  assert.equal(s.disabled, true);
  assert.match(s.placeholder, /Connect voice/i);
  assert.equal(s.reason, s.placeholder);
});

test("connecting is called out separately from disconnected — it fixes itself", () => {
  const s = composerState(input({ connected: false, vstate: "connecting", draft: "hi" }));
  assert.equal(s.canSend, false);
  assert.equal(s.disabled, true);
  assert.match(s.placeholder, /Connecting/i);
});

test("a send in flight blocks a second one instead of racing it", () => {
  const s = composerState(input({ sending: true, draft: "hi" }));
  assert.equal(s.canSend, false);
  assert.equal(s.disabled, true);
});

test("a provider that cannot take typed turns names itself in the reason", () => {
  // Simulate a future transport with no typed-turn support: the state machine
  // must reach for the provider's own label, not a generic failure.
  const fake = { ...input({ draft: "hi" }), provider: "nosuch" as never };
  const s = composerState(fake);
  assert.equal(s.canSend, false);
  assert.equal(s.disabled, true);
  assert.equal(s.reason, s.placeholder);
  assert.match(s.placeholder, /can't take typed messages/i);
  assert.match(s.placeholder, /nosuch/); // names the provider, never "undefined"
});

test("a permanent provider limit outranks a transient connection state", () => {
  const s = composerState({
    ...input({ connected: false, vstate: "connecting", draft: "hi" }),
    provider: "nosuch" as never,
  });
  assert.match(s.placeholder, /can't take typed messages/i);
});

test("every shipping provider can take a typed turn", () => {
  for (const p of ["openai", "azure", "gemini"] as const) {
    assert.equal(canTypeToProvider(p), true, p);
  }
  assert.equal(canTypeToProvider("nosuch" as never), false);
});
