// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { fmtPayload, isFlatObject, splitPlan, toolState, toolSummary } from "./timeline.ts";

test("splitPlan returns the whole text as lead when there is no plan", () => {
  assert.deepEqual(splitPlan("just a reply"), { lead: "just a reply", plan: null });
});

test("isFlatObject accepts an all-primitive object", () => {
  assert.equal(isFlatObject({ a: 1, b: "x", c: true, d: null }), true);
});

test("isFlatObject rejects nesting, arrays and non-objects", () => {
  assert.equal(isFlatObject({ a: { b: 1 } }), false);
  assert.equal(isFlatObject([1, 2]), false);
  assert.equal(isFlatObject("str"), false);
  assert.equal(isFlatObject(null), false);
});

test("fmtPayload renders nullish as an em dash", () => {
  assert.equal(fmtPayload(null), "—");
  assert.equal(fmtPayload(undefined), "—");
});

test("fmtPayload passes a string through and pretty-prints an object", () => {
  assert.equal(fmtPayload("raw"), "raw");
  assert.equal(fmtPayload({ a: 1 }), '{\n  "a": 1\n}');
});

test("fmtPayload survives a circular structure", () => {
  const o: Record<string, unknown> = {};
  o.self = o;
  assert.equal(typeof fmtPayload(o), "string");   // must not throw
});

test("toolState reads error from ok:false", () => {
  assert.equal(toolState({ kind: "tool", name: "t", ok: false }), "error");
});

test("toolState reads working and error out of the result status", () => {
  assert.equal(toolState({ kind: "tool", name: "t", result: { status: "working" } }), "working");
  assert.equal(toolState({ kind: "tool", name: "t", result: { status: "error" } }), "error");
});

test("toolState defaults to done", () => {
  assert.equal(toolState({ kind: "tool", name: "t", result: { status: "idle" } }), "done");
  assert.equal(toolState({ kind: "tool", name: "t" }), "done");
});

test("toolSummary never returns an empty string for a known tool", () => {
  // The row reads as an action; an empty gloss would render a blank line.
  assert.notEqual(toolSummary("tell_claude", { message: "go" }, {}).trim(), "");
});

test("toolSummary survives junk args and results", () => {
  assert.equal(typeof toolSummary("tell_claude", null, null), "string");
  assert.equal(typeof toolSummary("unknown_tool_name", 42, "x"), "string");
});
