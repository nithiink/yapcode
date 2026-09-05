// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { abbrevHome, clip, fmtLogTime, fmtLogTimeTitle } from "./format.ts";

test("clip leaves a short string alone", () => {
  assert.equal(clip("short"), "short");
});

test("clip truncates with an ellipsis at the given width", () => {
  assert.equal(clip("abcdefghij", 5), "abcd…");
  assert.equal(clip("abcdefghij", 5).length, 5);
});

test("clip's default width is 90", () => {
  assert.equal(clip("x".repeat(90)).length, 90);
  assert.equal(clip("x".repeat(91)).length, 90);
});

test("abbrevHome shortens a macOS home path", () => {
  assert.equal(abbrevHome("/Users/ankur/projects/yuri"), "~/projects/yuri");
});

test("abbrevHome shortens a Linux home path", () => {
  assert.equal(abbrevHome("/home/ankur/projects/yuri"), "~/projects/yuri");
});

test("abbrevHome leaves a path outside any home alone", () => {
  assert.equal(abbrevHome("/tmp/scratch"), "/tmp/scratch");
});

test("fmtLogTime renders a local clock with milliseconds", () => {
  // Built from local parts so the assertion does not depend on the runner's zone.
  const d = new Date(2026, 8, 3, 14, 5, 9, 42);
  assert.equal(fmtLogTime(d.toISOString()), "14:05:09.042");
});

test("fmtLogTime falls back to the raw UTC slice when unparseable", () => {
  const ts = "not-a-date-at-all-xx";
  assert.equal(fmtLogTime(ts), ts.slice(11, 23));
});

test("fmtLogTimeTitle returns the input unchanged when unparseable", () => {
  assert.equal(fmtLogTimeTitle("nonsense"), "nonsense");
});
