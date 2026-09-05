import { strict as assert } from "node:assert";
import { test } from "node:test";
import { activeHandle, agentHue, dockTabs, FALLBACK_HUE, liveDot } from "./dock.ts";
import type { Sess } from "./sessions.ts";

const s = (handle: string, over: Partial<Sess> = {}): Sess => ({
  handle, session_id: handle, cwd: "/tmp/" + handle, model: "m", status: "stopped", ...over,
});

test("each known provider has its own colour and the orb's is the fallback", () => {
  assert.notEqual(agentHue("claude-code"), agentHue("opencode"));
  assert.equal(agentHue("who-dis"), FALLBACK_HUE);
  assert.equal(agentHue(null), FALLBACK_HUE);
  assert.equal(agentHue(undefined), FALLBACK_HUE);
});

test("tabs put what needs a decision first, then work, then the rest", () => {
  const tabs = dockTabs([
    s("quiet"), s("busy", { status: "running" }), s("ask", { status: "needs_permission" }),
  ]);
  assert.deepEqual(tabs.map((t) => t.handle), ["ask", "busy", "quiet"]);
});

test("errors rank above running but below a decision", () => {
  const tabs = dockTabs([
    s("busy", { status: "running" }), s("bad", { status: "error" }),
    s("ask", { status: "needs_choice" }),
  ]);
  assert.deepEqual(tabs.map((t) => t.handle), ["ask", "bad", "busy"]);
});

test("equal-rank tabs keep their incoming order so they don't shuffle on poll", () => {
  const first = dockTabs([s("a"), s("b"), s("c")]).map((t) => t.handle);
  assert.deepEqual(first, ["a", "b", "c"]);
  // Same statuses, re-fetched: order must be stable.
  assert.deepEqual(dockTabs([s("a"), s("b"), s("c")]).map((t) => t.handle), first);
});

test("the user's pick is honoured while it exists", () => {
  const tabs = [s("a"), s("b")];
  assert.equal(activeHandle(tabs, "b"), "b");
  assert.equal(activeHandle(tabs, null), "a");
});

test("a vanished pick falls back to the first tab, not to nothing", () => {
  assert.equal(activeHandle([s("a")], "gone"), "a");
  assert.equal(activeHandle([], "gone"), null);
});

test("the live dot reports the most urgent session, not the selected one", () => {
  assert.equal(liveDot([s("a"), s("b", { status: "needs_permission" })]), "attn");
  assert.equal(liveDot([s("a"), s("b", { running: true })]), "working");
  assert.equal(liveDot([s("a")]), "idle");
  assert.equal(liveDot([]), "idle");
});

test("a live-but-idle session leaves the dot idle", () => {
  // The dot sits inches from the status strip that says "Ready". Painting it
  // green off status === "running" made the two disagree on screen.
  assert.equal(liveDot([s("a", { status: "running", running: false })]), "idle");
});
